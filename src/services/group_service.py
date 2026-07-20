"""
群组服务
检查用户是否加入指定群组，并提供隐私安全的规则提交播报
"""

import asyncio
from typing import Optional
from loguru import logger
from telegram import Bot
from telegram.error import TelegramError

from ..config import Config
from ..utils.cache import TTLCache


class GroupService:
    """群组验证服务"""
    
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

        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text
    
    async def check_user_in_group(self, user_id: int) -> Optional[bool]:
        """检查用户是否在指定群组中"""
        if not self._group_check_enabled:
            return True  # 功能关闭时默认通过

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
            
            logger.debug(f"用户 {user_id} 群组状态: {chat_member.status}, 是否为成员: {is_member}")
            return is_member

        except TelegramError as e:
            logger.warning(f"检查用户 {user_id} 群组成员身份失败: {e}")
            return None
        except Exception as e:
            logger.warning(f"检查用户 {user_id} 群组成员身份失败: {e}")
            return None
    
    def get_join_group_message(self) -> str:
        """获取加入群组的提示消息"""
        if not self._group_check_enabled:
            return ""
        
        message = "🔒 *使用限制*\n\n"
        message += "为了使用本机器人，请先加入我们的群组：\n\n"
        message += f"📢 *群组名称：* {self._escape_markdown(self.config.REQUIRED_GROUP_NAME)}\n"
        message += f"🔗 *加入链接：* {self._escape_markdown(self.config.REQUIRED_GROUP_LINK)}\n\n"
        message += "加入后请重新尝试使用机器人功能。"
        
        return message 

    async def announce_rule_submission(
        self,
        domain: str,
        commit_sha: str = "",
        commit_url: str = "",
    ) -> bool:
        """Best-effort broadcast that never changes the core add result."""
        if not self._announcement_group_id:
            return False

        short_sha = (commit_sha or "")[:8]
        message = (
            "📣 *直连规则更新*\n\n"
            "✅ *结果：* 已成功提交\n"
            "🧭 *类型：* `DOMAIN-SUFFIX`\n"
            f"🌐 *域名：* `{domain}`"
        )
        if commit_url and short_sha:
            message += f"\n🔗 *提交：* [查看 {short_sha}]({commit_url})"

        try:
            await asyncio.wait_for(
                self.bot.send_message(
                    chat_id=self._announcement_group_id,
                    text=message,
                    parse_mode="Markdown",
                    disable_notification=True,
                    disable_web_page_preview=True,
                ),
                timeout=8,
            )
            logger.info(
                "群组播报已发送: group={}, domain={}, commit={}",
                self._announcement_group_id,
                domain,
                short_sha or "unknown",
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "群组播报超时，不影响规则提交: group={}, domain={}",
                self._announcement_group_id,
                domain,
            )
        except TelegramError as e:
            logger.warning(
                "群组播报失败，不影响规则提交: group={}, domain={}, error={}",
                self._announcement_group_id,
                domain,
                e,
            )
        except Exception as e:
            logger.warning(
                "群组播报异常，不影响规则提交: group={}, domain={}, error={}",
                self._announcement_group_id,
                domain,
                e,
            )
        return False
