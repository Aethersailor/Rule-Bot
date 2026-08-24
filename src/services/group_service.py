"""
群组服务
检查用户是否加入指定群组，并提供隐私安全的规则提交播报
"""

import asyncio
from typing import Optional
from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from ..config import Config
from ..utils.cache import TTLCache
from ..utils.privacy import log_reference


class GroupService:
    """群组验证服务"""

    MEMBERSHIP_RETRY_CALLBACK = "membership_retry"
    
    def __init__(self, config: Config, bot: Bot):
        self.config = config
        self.bot = bot
        self._group_check_enabled = bool(getattr(config, "GROUP_CHECK_ENABLED", False))
        self._announcement_group_id = getattr(config, "ANNOUNCEMENT_GROUP_ID", None)
        self._membership_cache = TTLCache[int, bool](2048, 300)
    
    def is_group_check_enabled(self) -> bool:
        """检查是否启用群组验证"""
        return self._group_check_enabled

    @staticmethod
    def _escape_markdown(text: str) -> str:
        if not text:
            return text

        text = " ".join(str(text).split()).replace("\\", "\\\\")
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    @staticmethod
    def _escape_inline_code(value: object) -> str:
        """Keep a complete dynamic value on one safe legacy-Markdown code line."""
        text = " ".join(str(value).split())
        return text.replace("\\", "\\\\").replace("`", "\\`")
    
    async def check_user_in_group(
        self, user_id: int, *, force_refresh: bool = False
    ) -> Optional[bool]:
        """检查用户是否在指定群组中"""
        if not self._group_check_enabled:
            return True  # 功能关闭时默认通过

        if not force_refresh:
            cached = self._membership_cache.get(user_id)
            if cached is not None:
                return cached
        
        try:
            chat_member = await self.bot.get_chat_member(
                chat_id=self.config.REQUIRED_GROUP_ID,
                user_id=user_id
            )
            
            # 检查用户状态
            valid_statuses = ['member', 'administrator', 'creator']
            is_member = chat_member.status in valid_statuses
            self._membership_cache.set(user_id, is_member)
            
            logger.debug(
                "群成员检查完成: user_ref={}, status={}, member={}",
                log_reference(str(user_id)),
                chat_member.status,
                is_member,
            )
            return is_member

        except TelegramError as e:
            logger.warning(
                "群成员检查失败: user_ref={}, error_type={}",
                log_reference(str(user_id)),
                type(e).__name__,
            )
            return None
        except Exception as e:
            logger.warning(
                "群成员检查异常: user_ref={}, error_type={}",
                log_reference(str(user_id)),
                type(e).__name__,
            )
            return None
    
    def get_join_group_message(self) -> str:
        """获取加入群组的提示消息"""
        if not self._group_check_enabled:
            return ""

        group_name = self._escape_markdown(self.config.REQUIRED_GROUP_NAME)
        return (
            "🔒 *加入群组后继续*\n\n"
            "👤 当前账号尚未通过群组成员验证。\n\n"
            "👥 *所需群组*\n"
            f"{group_name}\n\n"
            "✅ 加入群组后，请点击“已加入，重新验证”。"
        )

    def get_join_group_keyboard(self) -> InlineKeyboardMarkup:
        """Return a narrow-screen membership recovery keyboard."""
        keyboard = []
        group_link = str(getattr(self.config, "REQUIRED_GROUP_LINK", "")).strip()
        if group_link:
            keyboard.append(
                [InlineKeyboardButton("📢 加入群组", url=group_link)]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "✅ 已加入，重新验证",
                    callback_data=self.MEMBERSHIP_RETRY_CALLBACK,
                )
            ]
        )
        return InlineKeyboardMarkup(keyboard)

    async def announce_rule_submission(
        self,
        domain: str,
        commit_sha: str = "",
        commit_url: str = "",
        repo_path: str = "",
        rule_path: str = "",
        user_name: str = "",
    ) -> bool:
        """Best-effort broadcast that never changes the core add result."""
        if not self._announcement_group_id:
            return False

        short_sha = "".join(
            char for char in str(commit_sha or "") if char.isalnum()
        )[:8]
        group_ref = log_reference(str(self._announcement_group_id))
        safe_domain = self._escape_inline_code(domain)
        message = "📣 *直连规则已更新*\n\n"
        message += "🧾 *已写入规则*\n"
        message += f"`DOMAIN-SUFFIX,{safe_domain}`"
        details = []
        if user_name:
            details.append(f"👤 提交者：{self._escape_markdown(user_name)}")
        if details:
            message += f"\n\n{'\n'.join(details)}"

        reply_markup = None
        if commit_url:
            link_label = (
                f"🔗 查看提交 {short_sha}"
                if short_sha
                else "🔗 查看 GitHub 提交"
            )
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(link_label, url=commit_url)]]
            )

        try:
            await asyncio.wait_for(
                self.bot.send_message(
                    chat_id=self._announcement_group_id,
                    text=message,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                    disable_notification=True,
                    disable_web_page_preview=True,
                ),
                timeout=8,
            )
            logger.info(
                "群组播报已发送: group_ref={}, domain_ref={}, commit={}",
                group_ref,
                log_reference(domain),
                short_sha or "unknown",
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "群组播报超时，不影响规则提交: group_ref={}, domain_ref={}",
                group_ref,
                log_reference(domain),
            )
        except TelegramError as e:
            logger.warning(
                "群组播报失败，不影响规则提交: group_ref={}, domain_ref={}, error_type={}",
                group_ref,
                log_reference(domain),
                type(e).__name__,
            )
        except Exception as e:
            logger.warning(
                "群组播报异常，不影响规则提交: group_ref={}, domain_ref={}, error_type={}",
                group_ref,
                log_reference(domain),
                type(e).__name__,
            )
        return False
