"""
Telegram 机器人主控制器
"""

import asyncio
from pathlib import Path
from typing import Optional
from loguru import logger

from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters
)

from .config import Config
from .data_manager import DataManager
from .handlers import HandlerManager, GroupHandler
from .healthcheck import HEALTH_PATH
from .update_processor import PerUserUpdateProcessor
from .utils.metrics import EXPORTER
from .services.matchscope_api import MatchScopeAPIServer


class RuleBot:
    """Rule-Bot 主控制器"""
    
    def __init__(self, config: Config, data_manager: DataManager):
        self.config = config
        self.data_manager = data_manager
        self.app: Optional[Application] = None
        self.handler_manager = None  # 延迟初始化
        self.group_handler = None  # 群组处理器
        self.matchscope_api = None
        self._metrics_task = None
        self._heartbeat_task = None

    async def _heartbeat_loop(self):
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        while True:
            HEALTH_PATH.touch()
            await asyncio.sleep(15)

    async def _error_handler(self, update, context):
        logger.error("Telegram 更新处理异常: {}", context.error)

    @staticmethod
    def _polling_error_handler(error):
        """Keep expected polling outages concise while PTB retries them."""
        logger.warning("Telegram polling 暂时失败，将自动重试: {}: {}", type(error).__name__, error)
    
    async def stop(self):
        """停止机器人"""
        logger.info("正在停止机器人...")
        if self.matchscope_api:
            await self.matchscope_api.stop()
            self.matchscope_api = None
        if self.handler_manager:
            await self.handler_manager.stop()
        if self._metrics_task:
            await EXPORTER.stop()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                logger.debug("心跳任务已取消")
            self._heartbeat_task = None
        if self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
            except Exception as e:
                logger.debug(f"停止 updater 失败: {e}")
            try:
                if self.app.running:
                    await self.app.stop()
            except Exception as e:
                logger.debug(f"停止 app 失败: {e}")
            try:
                if self.app.initialized:
                    await self.app.shutdown()
            except Exception as e:
                logger.debug(f"关闭 app 失败: {e}")
        if self.data_manager:
            await self.data_manager.close()
        try:
            Path(HEALTH_PATH).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除健康检查文件失败: {}", e)
        logger.info("机器人已停止")

    async def start(self, stop_event: Optional[asyncio.Event] = None):
        """启动机器人"""
        try:
            # 创建应用
            self.app = (
                Application.builder()
                .token(self.config.TELEGRAM_BOT_TOKEN)
                .concurrent_updates(PerUserUpdateProcessor(8))
                .build()
            )

            # 初始化处理器管理器（需要 app 实例）
            self.handler_manager = HandlerManager(self.config, self.data_manager, self.app)
            if (
                self.config.MATCHSCOPE_PRIVATE_API_ENABLED
                or self.config.MATCHSCOPE_PUBLIC_API_ENABLED
            ):
                self.matchscope_api = MatchScopeAPIServer(
                    self.config, self.handler_manager
                )
            
            # 初始化群组处理器
            self.group_handler = GroupHandler(self.config, self.data_manager, self.handler_manager)
            
            # 注册处理器
            self._register_handlers()
            
            # 启动轮询
            logger.info("机器人启动成功，开始轮询...")

            async with self.app:
                await self.handler_manager.start()  # 显式启动服务（如 DNS Session）
                if self.matchscope_api:
                    await self.matchscope_api.start()
                await self.app.start()
                self._metrics_task = EXPORTER.start()
                await self.app.updater.start_polling(
                    timeout=30,
                    bootstrap_retries=5,
                    allowed_updates=["message", "callback_query"],
                    drop_pending_updates=False,
                    error_callback=self._polling_error_handler,
                )
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                try:
                    # 保持运行
                    await (stop_event or asyncio.Event()).wait()
                finally:
                    # Application's context manager performs shutdown only
                    # after the updater and application have stopped.
                    if self.app.updater and self.app.updater.running:
                        await self.app.updater.stop()
                    if self.app.running:
                        await self.app.stop()
            
        except Exception as e:
            logger.error(f"机器人启动失败: {e}")
            raise
        finally:
            await self.stop()
    
    def _register_handlers(self):
        """注册所有处理器"""
        # 命令处理器
        private_only = filters.ChatType.PRIVATE
        self.app.add_handler(CommandHandler("start", self.handler_manager.start_command, filters=private_only))
        self.app.add_handler(CommandHandler("help", self.handler_manager.help_command, filters=private_only))
        self.app.add_handler(CommandHandler("id", self.handler_manager.id_command, filters=private_only))
        self.app.add_handler(CommandHandler("query", self.handler_manager.query_command, filters=private_only))
        self.app.add_handler(CommandHandler("add", self.handler_manager.add_command, filters=private_only))
        self.app.add_handler(CommandHandler("delete", self.handler_manager.delete_command, filters=private_only))
        self.app.add_handler(CommandHandler("skip", self.handler_manager.skip_command, filters=private_only))
        
        # 回调查询处理器
        self.app.add_handler(CallbackQueryHandler(self.handler_manager.handle_callback))
        
        # 群组消息处理器（处理群组中 @机器人 的消息）
        # 注意：需要在私聊消息处理器之前注册，使用 group 参数设置优先级
        if self.config.ALLOWED_GROUP_IDS:
            self.app.add_handler(MessageHandler(
                filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND & filters.Entity("mention"),
                self.group_handler.handle_group_message
            ), group=0)
            logger.info(f"群组工作模式已启用，允许的群组: {self.config.ALLOWED_GROUP_IDS}")
        
        # 私聊消息处理器（用于处理用户输入）
        self.app.add_handler(MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, 
            self.handler_manager.handle_message
        ))
        self.app.add_error_handler(self._error_handler)
        
        logger.info("所有处理器注册完成") 
