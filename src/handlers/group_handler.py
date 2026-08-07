"""
群组消息处理器
处理群组内 @机器人 的消息，自动提取域名并查询/添加
"""

from typing import Optional
from loguru import logger

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..config import Config
from ..data_manager import DataManager
from ..utils.text_extractor import extract_domain_for_rules, remove_bot_mention
from ..utils.domain_utils import is_cn_domain
from ..utils.privacy import log_reference


class GroupHandler:
    """群组消息处理器"""

    @staticmethod
    def _escape_inline_code(value: object, max_length: int = 253) -> str:
        """Keep a dynamic value on one safe legacy-Markdown code line."""
        text = " ".join(str(value).split())[:max_length]
        return text.replace("\\", "\\\\").replace("`", "\\`")

    def _escape_detail(self, value: object, max_length: int = 160) -> str:
        """Format a short, single-line dynamic explanation."""
        text = " ".join(str(value).split())
        if len(text) > max_length:
            text = f"{text[: max_length - 3]}..."
        text = text.replace("\\", "\\\\")
        return self.handler_manager.escape_markdown(text)

    def _build_missing_domain_text(self) -> str:
        return (
            "⚠️ *没有识别到域名*\n\n"
            "请再次 @我，并附带域名或 URL。\n\n"
            "*示例域名*\n"
            "`example.com`\n\n"
            "也可以回复一条含域名的消息，再 @我。"
        )

    def _build_cn_domain_text(self, domain: str) -> str:
        safe_domain = self._escape_inline_code(domain)
        return (
            "ℹ️ *.cn 域名无需添加*\n\n"
            f"`{safe_domain}`\n\n"
            "此类域名默认直连。"
        )
    
    def __init__(self, config: Config, data_manager: DataManager, handler_manager):
        """初始化群组处理器
        
        Args:
            config: 配置对象
            data_manager: 数据管理器
            handler_manager: 主处理器管理器（用于复用服务和核心逻辑）
        """
        self.config = config
        self.data_manager = data_manager
        self.handler_manager = handler_manager
        
        # 缓存机器人用户名（在第一次处理消息时获取）
        self._bot_username: Optional[str] = None
    
    def is_group_allowed(self, chat_id: int) -> bool:
        """检查群组是否在白名单中
        
        Args:
            chat_id: 群组 ID
            
        Returns:
            是否允许在该群组工作
        """
        return chat_id in self.config.ALLOWED_GROUP_IDS
    
    def is_bot_mentioned(self, message: Message, bot_username: str) -> bool:
        """检查消息是否 @了机器人
        
        Args:
            message: 消息对象
            bot_username: 机器人用户名
            
        Returns:
            是否提及了机器人
        """
        if not message.text or not message.entities:
            return False
        
        # 检查 entities 中是否有 mention 类型指向机器人
        for entity in message.entities:
            if entity.type != "mention":
                continue
            mention_text = message.parse_entity(entity)
            if mention_text.lower() == f"@{bot_username.lower()}":
                return True
        
        return False
    
    async def handle_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理群组消息主入口
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        try:
            message = update.effective_message
            chat = update.effective_chat
            user = update.effective_user
            
            # 基本检查
            if not message or not chat or not user:
                logger.warning("[群组处理器] 消息/群组/用户对象为空")
                return
            
            if not message.text:
                return
            
            # 只处理群组消息
            if chat.type not in ["group", "supergroup"]:
                logger.debug(f"[群组处理器] 非群组消息，类型: {chat.type}")
                return
            
            # 检查群组是否在白名单
            logger.debug(f"[群组处理器] 检查白名单 - 群组 ID: {chat.id}, 白名单: {self.config.ALLOWED_GROUP_IDS}")
            if not self.is_group_allowed(chat.id):
                logger.debug(f"[群组处理器] 群组 {chat.id} 不在白名单中，忽略消息")
                return
            
            # 仅处理包含 @mention 实体的消息
            if not message.entities or not any(entity.type == "mention" for entity in message.entities):
                return
            
            # 获取机器人用户名（首次获取后缓存）
            if not self._bot_username:
                bot = await context.bot.get_me()
                self._bot_username = bot.username
                logger.info(f"[群组处理器] 获取机器人用户名: @{self._bot_username}")
            
            # 检查是否 @了机器人
            is_mentioned = self.is_bot_mentioned(message, self._bot_username)
            if not is_mentioned:
                return
            
            logger.info(f"[群组处理器] 收到@消息 - 群组: {chat.id}, "
                       f"用户: {user.id}(@{user.username}), "
                       f"消息: {message.text[:50]}...")
            
            # 提取域名
            domain = await self._extract_domain_from_message(message)
            
            if not domain:
                # 未找到有效域名
                await message.reply_text(
                    self._build_missing_domain_text(),
                    parse_mode='Markdown'
                )
                return
            
            # 检查是否为 .cn 域名
            if is_cn_domain(domain):
                await message.reply_text(
                    self._build_cn_domain_text(domain),
                    parse_mode='Markdown'
                )
                return
            
            # 获取用户名用于 commit
            username = user.username or user.first_name or str(user.id)
            
            # 执行域名处理
            await self._process_domain_request(
                message, domain, username, user.id, user=user
            )
            
        except Exception as e:
            logger.error(f"处理群组消息失败: {e}")
            try:
                await update.effective_message.reply_text("❌ 处理失败，请稍后重试。")
            except Exception as reply_error:
                logger.debug("发送群组错误提示失败: {}", reply_error)
    
    async def _extract_domain_from_message(self, message: Message) -> Optional[str]:
        """从消息中提取域名（支持回复消息）
        
        优先从当前消息提取，如果当前消息无域名且是回复消息，则从被回复消息提取
        
        Args:
            message: 消息对象
            
        Returns:
            提取到的可注册域名（主域名），或 None
        """
        # 移除 @机器人 提及后提取域名
        text = message.text or ""
        clean_text = remove_bot_mention(text, self._bot_username) if self._bot_username else text
        
        # 1. 先从当前消息提取
        domain = extract_domain_for_rules(clean_text)
        if domain:
            logger.debug(
                "从当前消息提取到域名，domain_ref={}", log_reference(domain)
            )
            return domain
        
        # 2. 如果当前消息无域名，检查是否是回复消息
        if message.reply_to_message:
            reply_text = message.reply_to_message.text or ""
            domain = extract_domain_for_rules(reply_text)
            if domain:
                logger.debug(
                    "从回复消息提取到域名，domain_ref={}", log_reference(domain)
                )
                return domain
        
        return None
    
    async def _process_domain_request(
        self, 
        message: Message, 
        domain: str, 
        username: str,
        user_id: int,
        *,
        user=None,
    ):
        """处理域名请求：查询 + 自动添加
        
        Args:
            message: 消息对象（用于回复）
            domain: 待处理的域名
            username: 用户名（用于 commit）
            user_id: 用户 ID（用于频率限制）
        """
        result = None
        try:
            identity = (
                self.handler_manager._format_telegram_identity(user)
                if user is not None
                else self.handler_manager.escape_markdown(username)
            )
            identity = " ".join(str(identity).split())[:80]
            safe_domain = self._escape_inline_code(domain)
            processing_msg = await message.reply_text(
                "🔍 *正在检查域名*\n\n"
                f"`{safe_domain}`\n\n"
                "符合条件时，将自动写入公开 GitHub，且不再二次确认。\n\n"
                f"提交者：{identity}",
                parse_mode='Markdown',
            )
            
            # 检查用户添加频率限制
            can_add, remaining = self.handler_manager.check_user_add_limit(user_id)
            if not can_add:
                await processing_msg.edit_text(
                    "⚠️ *本小时添加次数已用完*\n\n"
                    f"每小时最多可添加 {self.handler_manager.MAX_ADDS_PER_HOUR} 个域名。\n"
                    "请稍后再试。",
                    parse_mode='Markdown'
                )
                return
            
            # 调用核心逻辑进行检查和添加
            result = await self.handler_manager.check_and_add_domain_auto(
                domain,
                username,
                user_id=user_id,
            )
            
            # 根据结果回复
            reply_markup = None
            if result["action"] == "added":
                remaining = result["rate_limit_remaining"]
                target_domain = result.get("target_domain", domain)
                safe_target = self._escape_inline_code(target_domain)
                result_text = (
                    "✅ *直连规则已添加*\n\n"
                    f"`DOMAIN-SUFFIX,{safe_target}`\n\n"
                    f"提交者：{identity}\n"
                    f"本小时剩余：{remaining} 个"
                )
                if result.get("commit_url"):
                    short_sha = str(result.get("commit_sha", ""))[:8]
                    link_label = (
                        f"🔗 查看提交 {short_sha}"
                        if short_sha
                        else "🔗 查看 GitHub 提交"
                    )
                    reply_markup = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(link_label, url=result["commit_url"])]]
                    )
                
            elif result["action"] == "exists":
                detail = self._escape_detail(result.get("message", "无需重复添加"))
                result_text = (
                    "ℹ️ *无需重复添加*\n\n"
                    f"`{safe_domain}`\n\n"
                    f"{detail}"
                )
                
            elif result["action"] == "rejected":
                detail = self._escape_detail(result.get("message", "不符合添加条件"))
                result_text = (
                    "⛔ *不符合添加条件*\n\n"
                    f"`{safe_domain}`\n\n"
                    f"{detail}"
                )
                if self.handler_manager.is_admin(user_id):
                    result_text += "\n\n管理员可使用下方按钮强制添加。"
                    keyboard = [
                        [InlineKeyboardButton(
                            "🛡️ 管理员强制添加",
                            callback_data=self.handler_manager.get_admin_force_add_callback(user_id, domain)
                        )]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                
            else:  # error
                error = self._escape_detail(
                    result.get("message", "未知错误")
                )
                result_text = (
                    "❌ *处理失败*\n\n"
                    f"`{safe_domain}`\n\n"
                    f"{error}\n\n"
                    "请稍后再次 @机器人。"
                )

            await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"处理域名请求失败: {e}")
            added = bool(result and result.get("action") == "added")
            visible_domain = (
                result.get("target_domain", domain) if added else domain
            )
            safe_domain = self._escape_inline_code(visible_domain)
            if added:
                text = (
                    "⚠️ *规则已添加，但结果页更新失败*\n\n"
                    f"`DOMAIN-SUFFIX,{safe_domain}`\n\n"
                    "请先查看 GitHub，避免重复提交。"
                )
            else:
                text = (
                    "❌ *处理失败*\n\n"
                    f"`{safe_domain}`\n\n"
                    "请稍后再次 @机器人。"
                )
            await message.reply_text(
                text,
                parse_mode='Markdown',
            )
