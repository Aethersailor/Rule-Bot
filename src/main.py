#!/usr/bin/env python3
"""
Rule-Bot Main Entry Point
Telegram 机器人用于管理 GitHub 规则文件
"""

import asyncio
import os
import sys
import psutil
import signal
import time
from loguru import logger

try:
    import resource
except ImportError:  # pragma: no cover - Windows 等平台无此模块
    resource = None

from .bot import RuleBot
from .config import Config
from .data_manager import DataManager
from .utils.memory import trim_memory


def _configure_logging():
    """配置日志格式和级别"""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_style = os.getenv("LOG_FORMAT", "compact").strip().lower()

    if log_style in ("verbose", "full", "detail"):
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level:<8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )
    else:
        log_format = (
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | "
            "{module:<16}:{line:<4} | {message}"
        )

    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format=log_format,
        enqueue=True,
        backtrace=False,
        diagnose=False
    )


def set_memory_limit():
    """Apply an address-space limit only when explicitly configured."""
    if resource is None:
        logger.info("当前平台不支持 resource 模块，跳过内存限制设置")
        return

    try:
        soft_raw = os.getenv("MEMORY_SOFT_LIMIT_MB", "").strip()
        hard_raw = os.getenv("MEMORY_HARD_LIMIT_MB", "").strip()
        if not soft_raw and not hard_raw:
            logger.info("未配置进程地址空间限制，交由容器/宿主机管理")
            return
        soft_mb = int(soft_raw or hard_raw)
        hard_mb = int(hard_raw or soft_mb)
        if hard_mb < soft_mb:
            raise ValueError("MEMORY_HARD_LIMIT_MB 不能小于 MEMORY_SOFT_LIMIT_MB")
        memory_soft = soft_mb * 1024 * 1024
        memory_hard = hard_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_soft, memory_hard))
        logger.info(f"已设置内存软限制为 {soft_mb} MB，硬限制为 {hard_mb} MB")
        
        # 记录当前内存使用情况
        try:
            process = psutil.Process()
            current_memory = process.memory_info().rss
            logger.info(f"当前内存使用: {current_memory / 1024 / 1024:.1f} MB")
        except Exception as e:
            logger.warning(f"获取当前内存使用失败: {e}")
        
    except Exception as e:
        logger.warning(f"设置内存限制失败: {e}")
        # 内存限制设置失败不影响程序运行

def log_memory_usage():
    """记录内存使用情况，接近限制时给出警告"""
    # 初始化静态变量（只初始化一次）
    if not hasattr(log_memory_usage, '_initialized'):
        log_memory_usage.last_warning_time = 0
        log_memory_usage.last_warning_level = 0
        log_memory_usage.last_normal_log = 0
        log_memory_usage._initialized = True
    
    try:
        process = psutil.Process()
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / 1024 / 1024
        
        # 边界检查，确保内存值合理
        if memory_mb < 0 or memory_mb > 1000:  # 如果内存值异常，记录但不处理
            logger.warning(f"内存值异常: {memory_mb:.1f} MB，跳过处理")
            return
        
        current_time = time.time()
        warning_cooldown = 300  # 5 分钟内不重复相同级别的警告
        
        soft_limit = int(os.getenv("MEMORY_SOFT_LIMIT_MB", "0") or 0)
        hard_limit = int(os.getenv("MEMORY_HARD_LIMIT_MB", "0") or 0)

        # Only compare against limits that are actually configured.
        if hard_limit and memory_mb > hard_limit * 0.94:
            if current_time - log_memory_usage.last_warning_time > warning_cooldown or log_memory_usage.last_warning_level != 3:
                logger.error(f"🚨 内存使用危急: {memory_mb:.1f} MB (接近 {hard_limit} MB 硬限制)")
                # 尝试主动释放一些内存
                import gc
                gc.collect()
                logger.warning("已尝试垃圾回收释放内存")
                log_memory_usage.last_warning_time = current_time
                log_memory_usage.last_warning_level = 3
        elif soft_limit and memory_mb > soft_limit * 0.94:
            if current_time - log_memory_usage.last_warning_time > warning_cooldown or log_memory_usage.last_warning_level != 2:
                logger.warning(f"⚠️ 内存使用过高: {memory_mb:.1f} MB (接近 {soft_limit} MB 软限制)")
                log_memory_usage.last_warning_time = current_time
                log_memory_usage.last_warning_level = 2
        elif memory_mb > 512:
            if current_time - log_memory_usage.last_warning_time > warning_cooldown or log_memory_usage.last_warning_level != 1:
                logger.warning(f"⚠️ 内存使用较高: {memory_mb:.1f} MB")
                log_memory_usage.last_warning_time = current_time
                log_memory_usage.last_warning_level = 1
        else:
            # 正常时只记录一次，避免刷屏
            if current_time - log_memory_usage.last_normal_log > 3600:  # 1 小时记录一次正常状态
                logger.info(f"内存使用正常: {memory_mb:.1f} MB")
                log_memory_usage.last_normal_log = current_time
                log_memory_usage.last_warning_level = 0
            
    except Exception as e:
        logger.warning(f"获取内存使用情况失败: {e}")


async def _memory_monitor(stop_event: asyncio.Event, interval: float = 600) -> None:
    """Periodically sample memory without keeping an unmanaged OS thread."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            log_memory_usage()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"内存监控出错: {e}")
            await asyncio.sleep(min(60, interval))

async def _run():
    """异步主流程（全进程单事件循环）"""
    # 初始化配置
    config = Config()
    
    logger.info("Rule-Bot 正在启动...")
    
    # 初始化数据管理器（与机器人运行保持同一事件循环）
    data_manager = DataManager(config)
    await data_manager.initialize()
    
    # 记录数据加载后的内存使用
    log_memory_usage()
    trim_memory("初始化完成后内存修剪")
    
    # 初始化机器人
    bot = RuleBot(config, data_manager)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        if not stop_event.is_set():
            logger.info("收到停止信号，正在优雅关闭...")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(request_stop))
    
    # 启动机器人
    logger.info("启动 Telegram 机器人...")
    
    # 启动定期内存检查（每 10 分钟检查一次），并与主停止事件绑定。
    monitor_task = asyncio.create_task(_memory_monitor(stop_event))
    try:
        await bot.start(stop_event)
    finally:
        stop_event.set()
        await monitor_task

def main():
    """主程序入口"""
    try:
        # 配置日志（确保早期日志也使用统一格式）
        _configure_logging()

        # 设置内存限制
        set_memory_limit()

        asyncio.run(_run())

    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭...")
    except Exception as e:
        logger.error(f"程序启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 
