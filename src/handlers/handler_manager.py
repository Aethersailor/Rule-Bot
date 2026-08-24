"""
处理器管理器
统一管理所有 Telegram 消息处理逻辑
"""

import secrets
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from collections import defaultdict
from loguru import logger

from telegram import (
    CopyTextButton,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from ..config import Config
from ..data_manager import DataManager
from ..services.dns_service import DNSService
from ..services.geoip_service import GeoIPService
from ..services.github_service import GitHubService
from ..services.domain_checker import DomainChecker
from ..services.group_service import GroupService
from ..services.rule_bot_client_token_service import RuleBotClientTokenService
from ..utils.domain_utils import normalize_domain, extract_second_level_domain, extract_second_level_domain_for_rules, is_cn_domain
from ..utils.privacy import log_reference
from ..utils.input_safety import validate_single_line_text


class HandlerManager:
    """处理器管理器"""
    
    def __init__(self, config: Config, data_manager: DataManager, application=None):
        self.config = config
        self.data_manager = data_manager
        
        # 初始化服务
        self.dns_service = DNSService(
            config.DOH_SERVERS,
            config.NS_DOH_SERVERS,
            cache_size=config.DNS_CACHE_SIZE,
            cache_ttl=config.DNS_CACHE_TTL,
            ns_cache_size=config.NS_CACHE_SIZE,
            ns_cache_ttl=config.NS_CACHE_TTL,
            max_concurrency=config.DNS_MAX_CONCURRENCY,
            conn_limit=config.DNS_CONN_LIMIT,
            conn_limit_per_host=config.DNS_CONN_LIMIT_PER_HOST,
            timeout_total=config.DNS_TIMEOUT_TOTAL,
            timeout_connect=config.DNS_TIMEOUT_CONNECT,
        )
        self.geoip_service = GeoIPService(
            str(data_manager.geoip_file),
            str(data_manager.cn_ipv4_file),
            cache_size=config.GEOIP_CACHE_SIZE,
            cache_ttl=config.GEOIP_CACHE_TTL
        )
        self.github_service = GitHubService(config)
        self.domain_checker = DomainChecker(self.dns_service, self.geoip_service)

        # Runtime state is initialized before Telegram starts so callbacks can
        # never observe partially initialized dictionaries.
        self.user_states: Dict[int, Dict[str, Any]] = {}
        self.user_add_history: Dict[object, list] = defaultdict(list)
        self._pending_actions: Dict[tuple[int, str], Dict[str, Any]] = {}
        self._last_history_cleanup = 0
        self._last_state_cleanup = 0.0
        self.MAX_DESCRIPTION_LENGTH = 20
        self.MAX_ADDS_PER_HOUR = 50
        self.MAX_DETAIL_LINES = 4
        self.STATE_TTL = 1800
        self.ACTION_TTL = 900
        self.MAX_USER_STATES = 4096

        self.data_manager.register_update_callback(self._handle_data_update)
        
        # 群组服务（需要 bot 实例）
        self.group_service = None
        if application:
            self.group_service = GroupService(config, application.bot)
        self.rule_bot_client_token_service = None
        if config.RULE_BOT_CLIENT_COMMUNITY_API_ENABLED:
            self.rule_bot_client_token_service = RuleBotClientTokenService(
                config.RULE_BOT_CLIENT_COMMUNITY_TOKEN_DATABASE
                or (data_manager.data_dir / "rule_bot_client_tokens.sqlite3"),
                config.RULE_BOT_CLIENT_COMMUNITY_TOKEN_SIGNING_KEY,
                config.RULE_BOT_CLIENT_COMMUNITY_TOKEN_TTL_DAYS,
            )

    async def start(self):
        """启动服务"""
        if self.dns_service:
            await self.dns_service.start()
        
    async def stop(self):
        """停止服务"""
        if self.dns_service:
            await self.dns_service.close()
        if self.geoip_service:
            self.geoip_service.close()
        if self.github_service:
            self.github_service.close()

    async def _handle_data_update(self, changes: Dict[str, bool]) -> None:
        if changes.get("geoip") or changes.get("cn_ipv4"):
            self.geoip_service.reload()

    async def _announce_private_addition(
        self,
        chat,
        domain: str,
        add_result: dict,
        user_name: str,
    ) -> bool:
        """Broadcast only successful additions that originated in private chat."""
        if (
            not self.group_service
            or not add_result.get("success")
            or getattr(chat, "type", None) != "private"
        ):
            return False
        try:
            return await self.group_service.announce_rule_submission(
                domain,
                add_result.get("commit_sha", ""),
                add_result.get("commit_url", ""),
                getattr(self.config, "GITHUB_REPO", ""),
                add_result.get("file_path", ""),
                user_name,
            )
        except Exception as e:
            logger.warning("群组播报调用异常，不影响私聊添加结果: {}", e)
            return False

    async def check_and_add_domain_auto(
        self, 
        domain: str, 
        username: str, 
        description: str = "",
        *,
        user_id: int,
        source: str = "telegram",
        max_adds: Optional[int] = None,
    ) -> dict:
        """自动检查并添加域名（无需用户确认）
        
        供群组处理器调用，实现一步完成的域名检查和添加流程
        
        Args:
            domain: 待添加的域名（应为二级域名格式）
            username: 用户名（用于 commit 记录）
            description: 域名说明（可选）
            user_id: 用户 ID（用于频率限制）
            
        Returns:
            {
                "success": bool,
                "action": "added" | "exists" | "rejected" | "error",
                "message": str,
                "commit_url": str  # 仅添加成功时有值
            }
        """
        try:
            # 1. 检查是否已存在于 GitHub 规则中
            github_result = await self.github_service.check_domain_in_rules(domain)
            if github_result.get("error"):
                return {
                    "success": False,
                    "action": "error",
                    "message": "暂时无法读取 GitHub 规则",
                }
            if github_result.get("exists"):
                matches = github_result.get("matches", [])
                match_info = f"第{matches[0]['line']}行" if matches else ""
                return {
                    "success": True,
                    "action": "exists",
                    "reason": "rules",
                    "message": f"GitHub 规则已覆盖（{match_info}）"
                }
            
            # 2. 检查是否在 GeoSite 中
            in_geosite = await self.data_manager.is_domain_in_geosite(domain)
            if in_geosite:
                return {
                    "success": True,
                    "action": "exists",
                    "reason": "geosite",
                    "message": "域名已存在于 GEOSITE:CN 中，无需重复添加"
                }
            
            # 3. 进行域名综合检查
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            
            if "error" in check_result:
                return {
                    "success": False,
                    "action": "error",
                    "error_code": check_result.get("error_code", ""),
                    "message": "域名检查暂时无法完成"
                }
            
            # 4. 判断是否符合添加条件
            if self.domain_checker.should_reject(check_result):
                return {
                    "success": True,
                    "action": "rejected",
                    "message": "域名 IP 和 NS 均不在中国大陆，不符合直连规则添加条件"
                }
            
            # 5. 获取目标域名并添加到 GitHub
            target_domain = self.domain_checker.get_target_domain_to_add(check_result)
            if not target_domain:
                target_domain = domain
            
            add_result = await self._add_domain_with_limit(
                user_id,
                target_domain,
                username,
                description,
                source=source,
                max_adds=max_adds,
            )
            
            if add_result.get("success"):
                return {
                    "success": True,
                    "action": "added",
                    "message": "域名已成功添加到直连规则",
                    "commit_url": add_result.get("commit_url", ""),
                    "commit_sha": add_result.get("commit_sha", ""),
                    "target_domain": target_domain,
                    "rate_limit_remaining": add_result["rate_limit_remaining"],
                }
            elif add_result.get("already_exists"):
                return {
                    "success": True,
                    "action": "exists",
                    "reason": "rules",
                    "message": "域名已存在于 GitHub 规则中",
                }
            else:
                submission_uncertain = bool(
                    add_result.get("submission_uncertain")
                )
                return {
                    "success": False,
                    "action": "error",
                    "message": (
                        "提交结果暂时无法确认，请先查询规则，避免重复提交"
                        if submission_uncertain
                        else "暂时无法写入 GitHub 规则"
                    ),
                    "rate_limited": add_result.get("rate_limited", False),
                    "submission_uncertain": submission_uncertain,
                }
                
        except Exception as e:
            logger.error(f"自动检查并添加域名失败: {e}")
            return {
                "success": False,
                "action": "error",
                "message": "服务暂时不可用"
            }

    
    def get_user_state(self, user_id: int) -> Dict[str, Any]:
        """获取用户状态"""
        self._cleanup_transient_state()
        state = self.user_states.get(user_id)
        if state and time.monotonic() - state.get("updated_at", 0) > self.STATE_TTL:
            self.user_states.pop(user_id, None)
            state = None
        if state is None:
            self._ensure_user_state_capacity()
            self.user_states[user_id] = {
                "state": "idle",
                "data": {},
                "updated_at": time.monotonic(),
            }
        return self.user_states[user_id]

    async def submit_rule_bot_client_domain(
        self,
        domain_input: str,
        *,
        source: str,
        rate_key: object,
        max_adds: int,
    ) -> dict:
        """Normalize and submit one API domain through the Telegram business rules."""
        normalized = normalize_domain(domain_input)
        if not normalized:
            return {"status": "invalid_domain"}
        if is_cn_domain(normalized):
            return {"status": "ignored_cn", "domain": normalized}
        domain = extract_second_level_domain_for_rules(normalized)
        if not domain:
            return {"status": "invalid_domain"}

        result = await self.check_and_add_domain_auto(
            domain,
            "Rule-Bot Client",
            user_id=rate_key,
            source=source,
            max_adds=max_adds,
        )
        if result.get("action") == "added":
            return {
                "status": "added",
                "domain": result.get("target_domain", domain),
                "commit_url": result.get("commit_url", ""),
            }
        if result.get("action") == "exists":
            return {
                "status": (
                    "exists_geosite" if result.get("reason") == "geosite" else "exists_rules"
                ),
                "domain": domain,
            }
        if result.get("action") == "rejected":
            return {"status": "rejected_policy", "domain": domain}
        if result.get("rate_limited"):
            return {"status": "rate_limited", "domain": domain}
        if result.get("error_code") == "nxdomain":
            return {"status": "invalid_domain", "domain": domain}
        if result.get("error_code") == "empty_dns":
            return {"status": "rejected_policy", "domain": domain}
        return {"status": "temporary_error", "domain": domain}
    
    def set_user_state(self, user_id: int, state: str, data: Dict[str, Any] = None):
        """设置用户状态"""
        if user_id not in self.user_states:
            self._ensure_user_state_capacity()
            self.user_states[user_id] = {}
        self.user_states[user_id]["state"] = state
        self.user_states[user_id]["data"] = data or {}
        self.user_states[user_id]["updated_at"] = time.monotonic()

    def _ensure_user_state_capacity(self) -> None:
        if len(self.user_states) < self.MAX_USER_STATES:
            return

        # Prefer discarding the oldest idle conversation. Active states are
        # only evicted as a last-resort bound during an extreme user burst.
        idle_states = {
            uid: state
            for uid, state in self.user_states.items()
            if state.get("state") == "idle"
        }
        candidates = idle_states or self.user_states
        oldest_uid = min(
            candidates,
            key=lambda uid: candidates[uid].get("updated_at", 0),
        )
        self.user_states.pop(oldest_uid, None)

    def _cleanup_transient_state(self) -> None:
        now = time.monotonic()
        if now - self._last_state_cleanup < 600:
            return
        state_cutoff = now - self.STATE_TTL
        action_cutoff = now - self.ACTION_TTL
        for uid, state in list(self.user_states.items()):
            if state.get("updated_at", 0) < state_cutoff:
                self.user_states.pop(uid, None)
        for key, action in list(self._pending_actions.items()):
            if action.get("created_at", 0) < action_cutoff:
                self._pending_actions.pop(key, None)
        self._last_state_cleanup = now

    def create_pending_action(self, user_id: int, action: str, **data: Any) -> str:
        self._cleanup_transient_state()
        if len(self._pending_actions) >= 4096:
            oldest = min(
                self._pending_actions,
                key=lambda key: self._pending_actions[key].get("created_at", 0),
            )
            self._pending_actions.pop(oldest, None)
        token = secrets.token_urlsafe(6)
        self._pending_actions[(user_id, token)] = {
            "action": action,
            "data": data,
            "created_at": time.monotonic(),
        }
        return token

    def get_pending_action(
        self,
        user_id: int,
        token: str,
        expected_action: str,
        consume: bool = False,
    ) -> Optional[Dict[str, Any]]:
        self._cleanup_transient_state()
        key = (user_id, token)
        item = self._pending_actions.get(key)
        if not item or item.get("action") != expected_action:
            return None
        if time.monotonic() - item.get("created_at", 0) > self.ACTION_TTL:
            self._pending_actions.pop(key, None)
            return None
        if consume:
            self._pending_actions.pop(key, None)
        return item.get("data", {})

    def _discard_pending_actions(self, user_id: int, action_names: set) -> None:
        """Invalidate selected visible actions for one user."""
        for key, item in list(self._pending_actions.items()):
            if key[0] == user_id and item.get("action") in action_names:
                self._pending_actions.pop(key, None)
    
    def check_user_add_limit(
        self, user_id: object, max_adds: Optional[int] = None
    ) -> tuple[bool, int]:
        """检查用户添加频率限制
        
        Returns:
            tuple: (是否可以添加, 剩余次数)
        """
        self._maybe_cleanup_user_history()
        current_time = time.time()
        one_hour_ago = current_time - 3600  # 1小时前的时间戳
        
        # 清理1小时前的记录
        self.user_add_history[user_id] = [
            timestamp for timestamp in self.user_add_history[user_id]
            if timestamp > one_hour_ago
        ]
        
        # 检查当前小时内的添加次数
        current_count = len(self.user_add_history[user_id])
        limit = max_adds if max_adds is not None else self.MAX_ADDS_PER_HOUR
        remaining = limit - current_count

        return current_count < limit, remaining

    def _maybe_cleanup_user_history(self) -> None:
        now = time.time()
        if now - self._last_history_cleanup < 600:
            return
        cutoff = now - 3600
        for uid, timestamps in list(self.user_add_history.items()):
            filtered = [ts for ts in timestamps if ts > cutoff]
            if filtered:
                self.user_add_history[uid] = filtered
            else:
                self.user_add_history.pop(uid, None)
        self._last_history_cleanup = now
    
    def record_user_add(self, user_id: int) -> float:
        """记录用户添加操作"""
        current_time = time.time()
        self.user_add_history[user_id].append(current_time)
        return current_time

    def _rollback_user_add(self, user_id: int, timestamp: float) -> None:
        """回滚一次尚未成功的添加占位。"""
        history = self.user_add_history.get(user_id)
        if not history:
            return
        try:
            history.remove(timestamp)
        except ValueError:
            return
        if not history:
            self.user_add_history.pop(user_id, None)

    async def _add_domain_with_limit(
        self,
        user_id: object,
        domain: str,
        username: str,
        description: str = "",
        *,
        force_add: bool = False,
        source: str = "telegram",
        max_adds: Optional[int] = None,
    ) -> dict:
        """在唯一写入门禁内执行 GitHub 添加，并只统计成功写入。"""
        limit = max_adds if max_adds is not None else self.MAX_ADDS_PER_HOUR
        can_add, remaining = self.check_user_add_limit(user_id, limit)
        if not can_add:
            return {
                "success": False,
                "rate_limited": True,
                "rate_limit_remaining": max(0, remaining),
                "error": (
                    f"您在当前小时内已达到添加上限（{limit}个域名）。"
                    "请等待一小时后再尝试。"
                ),
            }

        reservation = self.record_user_add(user_id)
        try:
            add_result = await self.github_service.add_domain_to_rules(
                domain,
                username,
                description,
                force_add=force_add,
                source=source,
            )
            if not add_result.get("success"):
                if not add_result.get("submission_uncertain"):
                    self._rollback_user_add(user_id, reservation)
                return add_result
        except BaseException as e:
            if not getattr(e, "submission_uncertain", False):
                self._rollback_user_add(user_id, reservation)
            raise

        add_result["rate_limit_remaining"] = max(
            0,
            limit - len(self.user_add_history[user_id]),
        )
        return add_result

    def is_admin(self, user_id: int) -> bool:
        """检查是否管理员"""
        return user_id in self.config.ADMIN_USER_IDS

    def get_admin_force_add_callback(self, user_id: int, domain: str) -> str:
        """构建管理员权限添加的回调数据"""
        token = self.create_pending_action(user_id, "admin_force_add", domain=domain)
        return f"admin_force_add|{token}"
    
    def validate_description(self, description: str) -> tuple[bool, str]:
        """验证域名说明
        
        Returns:
            tuple: (是否有效, 处理后的说明)
        """
        if not description:
            return True, ""
        
        try:
            return True, validate_single_line_text(description, self.MAX_DESCRIPTION_LENGTH)
        except ValueError:
            return False, description.strip()[:self.MAX_DESCRIPTION_LENGTH]
    
    def escape_markdown(self, text: str) -> str:
        """转义 Markdown 特殊字符"""
        if not text:
            return text

        text = str(text).replace("\\", "\\\\")
        # 转义特殊字符（不包含点号，因为域名和文件路径中需要保留）
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        
        return text

    @staticmethod
    def _raw_telegram_identity(user) -> str:
        """Return the identity that is written to GitHub without inventing a username."""
        username = getattr(user, "username", None)
        if username:
            return str(username)
        first_name = getattr(user, "first_name", None)
        if first_name:
            return str(first_name)
        return str(getattr(user, "id", "未知用户"))

    def _format_telegram_identity(self, user) -> str:
        """Format a Telegram identity for Markdown-visible messages."""
        identity = self._single_line_display(self._raw_telegram_identity(user))
        identity = self.escape_markdown(identity)
        if getattr(user, "username", None):
            return f"@{identity}"
        return identity

    def _reset_user_flow(self, user_id: int) -> None:
        """Reset the visible conversation and invalidate buttons from the old flow."""
        self.set_user_state(user_id, "idle")
        for key in [key for key in self._pending_actions if key[0] == user_id]:
            self._pending_actions.pop(key, None)

    def _recovery_keyboard(
        self,
        retry_callback: Optional[str] = None,
        retry_label: str = "🔄 重试",
    ) -> InlineKeyboardMarkup:
        keyboard = []
        if retry_callback:
            keyboard.append(
                [InlineKeyboardButton(retry_label, callback_data=retry_callback)]
            )
        keyboard.append(
            [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
        )
        return InlineKeyboardMarkup(keyboard)

    def _repo_url(self) -> str:
        """Return the public repository URL without exposing it as a long text line."""
        return f"https://github.com/{self.config.GITHUB_REPO}"

    def _home_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]]
        )

    def _repo_and_home_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📂 查看公开仓库", url=self._repo_url())],
                [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")],
            ]
        )

    @staticmethod
    def _single_line_display(value: object) -> str:
        """Normalize dynamic text without discarding visible content."""
        return " ".join(str(value).split())

    def _format_rule_matches(self, matches: list, limit: Optional[int] = None) -> str:
        """Format dynamic GitHub matches as complete, independently wrapped rows."""
        if not matches:
            return ""
        limit = limit or getattr(self, "MAX_DETAIL_LINES", 4)
        lines = []
        for match in matches[:limit]:
            rule = self._single_line_display(match.get("rule", ""))
            lines.append(f"• 第 {match.get('line', '?')} 行：{self.escape_markdown(rule)}")
        remaining = len(matches) - limit
        if remaining > 0:
            lines.append(f"• 另有 {remaining} 条匹配未显示")
        return "\n".join(lines)

    @staticmethod
    def _format_value_list(values: list, limit: int = 3) -> str:
        """Format network values one-per-line for narrow Telegram clients."""
        visible = [
            HandlerManager._single_line_display(value)
            for value in list(values or [])[:limit]
        ]
        remaining = len(values or []) - len(visible)
        if remaining > 0:
            visible.append(f"另有 {remaining} 项")
        return "\n".join(f"• {value}" for value in visible)

    def _target_domain(self, domain: str, check_result: dict) -> str:
        return self.domain_checker.get_target_domain_to_add(check_result) or domain

    def _submission_notice(self, user) -> str:
        identity = self._format_telegram_identity(user)
        lines = [
            "🌍 *公开范围*",
            "• 规则、提交时间与提交者将写入公开 GitHub 历史",
            f"• 提交者：{identity}",
        ]
        if getattr(self.config, "ANNOUNCEMENT_GROUP_ID", None):
            lines.append("• 提交结果将同步至群组公告")
        return "\n".join(lines)

    def _build_add_review_text(self, domain: str, check_result: dict, user) -> tuple[str, str]:
        """Build a conclusion-first review that names the exact rule to be written."""
        target_domain = self._target_domain(domain, check_result)
        should_reject = self.domain_checker.should_reject(check_result)
        conclusion = (
            "⛔ *暂不符合添加条件*"
            if should_reject
            else "✅ *可以提交直连规则*"
        )
        lines = [
            conclusion,
            "",
            "🧾 *拟提交规则*",
            f"`DOMAIN-SUFFIX,{target_domain}`",
        ]
        if domain != target_domain:
            lines.extend(["", "🔎 *原始输入*", f"`{domain}`"])
        recommendation = str(check_result.get("recommendation", "")).strip()
        if recommendation:
            lines.extend(
                [
                    "",
                    "💡 *判断依据*",
                    self.escape_markdown(self._single_line_display(recommendation)),
                ]
            )
        if not should_reject:
            lines.extend(["", self._submission_notice(user)])
        return "\n".join(lines), target_domain

    def _build_add_success_text(
        self,
        target_domain: str,
        user,
        add_result: dict,
        remaining: int,
        description: str = "",
        *,
        admin: bool = False,
    ) -> str:
        title = "✅ *已通过管理员权限添加*" if admin else "✅ *直连规则已添加*"
        lines = [
            title,
            "",
            "🧾 *已写入规则*",
            f"`DOMAIN-SUFFIX,{target_domain}`",
            "",
            f"👤 提交者：{self._format_telegram_identity(user)}",
        ]
        if description:
            lines.append(f"📝 公开说明：{self.escape_markdown(description)}")
        commit_url = add_result.get("commit_url", "")
        short_sha = str(add_result.get("commit_sha", ""))[:8]
        if commit_url:
            link_label = f"查看 GitHub 提交 {short_sha}" if short_sha else "查看 GitHub 提交"
            lines.append(f"🔗 [{link_label}]({commit_url})")
        lines.extend(["", f"📊 本小时还可添加 {remaining} 个域名"])
        return "\n".join(lines)

    def _build_description_prompt_text(self, target_domain: str, user) -> str:
        return (
            "📝 *添加公开说明（可选）*\n\n"
            "🧾 *拟提交规则*\n"
            f"`DOMAIN-SUFFIX,{target_domain}`\n\n"
            "✍️ *填写要求*\n"
            f"请发送一行简短说明，最多 {self.MAX_DESCRIPTION_LENGTH} 个字符。\n"
            "说明发送后，系统将立即公开提交。\n\n"
            "⏭️ 如不需要说明，可直接选择跳过。\n\n"
            "🌍 *公开信息*\n"
            f"提交者：{self._format_telegram_identity(user)}"
        )

    def _build_add_failure_text(
        self, target_domain: str, add_result: Optional[dict] = None
    ) -> str:
        if add_result and add_result.get("submission_uncertain"):
            return self._build_submission_uncertain_text(target_domain)
        return (
            "❌ *直连规则添加失败*\n\n"
            "🧾 *目标规则*\n"
            f"`DOMAIN-SUFFIX,{target_domain}`\n\n"
            "🛡️ 本次操作未修改任何规则。\n"
            "请稍后重试。"
        )

    @staticmethod
    def _build_submission_uncertain_text(target_domain: str = "") -> str:
        lines = ["⚠️ *提交结果暂时无法确认*"]
        if target_domain:
            lines.extend(
                ["", "🧾 *待核对规则*", f"`DOMAIN-SUFFIX,{target_domain}`"]
            )
        lines.extend(
            [
                "",
                "🔎 请先查询该域名或检查 GitHub 提交记录。",
                "确认规则尚未写入后再重试，避免重复提交。",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _build_add_result_keyboard(add_result: dict) -> InlineKeyboardMarkup:
        if add_result.get("submission_uncertain"):
            primary = InlineKeyboardButton(
                "🔍 前往查询", callback_data="query_domain"
            )
        else:
            primary = InlineKeyboardButton(
                "➕ 继续添加", callback_data="add_direct_rule"
            )
        return InlineKeyboardMarkup(
            [
                [primary],
                [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")],
            ]
        )

    @staticmethod
    def _build_confirmed_add_fallback_text(
        target_domain: str, add_result: dict
    ) -> str:
        lines = [
            "✅ *直连规则已添加*",
            "",
            "🧾 *已写入规则*",
            f"`DOMAIN-SUFFIX,{target_domain}`",
        ]
        commit_url = add_result.get("commit_url", "")
        if commit_url:
            lines.extend(["", f"🔗 [查看 GitHub 提交]({commit_url})"])
        return "\n".join(lines)

    async def _display_callback_result(
        self,
        query,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup],
    ) -> bool:
        """Show a callback result, falling back to a new chat message."""
        try:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return True
        except Exception as display_error:
            logger.error(f"提交结果页编辑失败: {display_error}")

        reply_text = getattr(
            getattr(query, "message", None), "reply_text", None
        )
        if not callable(reply_text):
            return False
        try:
            await reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return True
        except Exception as reply_error:
            logger.error(f"提交结果兜底消息发送失败: {reply_error}")
            return False

    async def _display_message_result(
        self,
        message,
        processing_msg,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup],
    ) -> bool:
        """Show a message result, falling back from edit to a new reply."""
        if processing_msg is not None:
            try:
                await processing_msg.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown",
                )
                return True
            except Exception as display_error:
                logger.error(f"提交结果页编辑失败: {display_error}")
        try:
            await message.reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return True
        except Exception as reply_error:
            logger.error(f"提交结果兜底消息发送失败: {reply_error}")
            return False

    def _build_main_menu_text(self, username: str) -> str:
        """构建主菜单文案"""
        username = self.escape_markdown(self._single_line_display(username))
        repo = str(self.config.GITHUB_REPO).strip()
        repo_label = self.escape_markdown(repo)
        return "\n".join(
            [
                f"👋 *欢迎使用 Rule-Bot，{username}！*",
                "",
                "🧭 *直连规则查询与提交助手*",
                "",
                "📚 查询公开规则库与 `GEOSITE:CN` 收录状态",
                "🌐 检查域名、可注册域名 IP 及 NS 服务器归属",
                "🧾 提交前展示最终规则、判断依据与公开范围",
                "🌍 明确确认后，规则才会写入公开 GitHub",
                "",
                "📂 *公开仓库*",
                f"[{repo_label}](https://github.com/{repo})",
                "",
                "👇 *请选择需要使用的功能*",
            ]
        )

    def _build_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """构建主菜单键盘"""
        keyboard = [
            [
                InlineKeyboardButton("🔍 查询域名", callback_data="query_domain"),
                InlineKeyboardButton("➕ 添加直连", callback_data="add_direct_rule"),
            ],
        ]
        if self.config.RULE_BOT_CLIENT_COMMUNITY_API_ENABLED:
            keyboard.append(
                [
                    InlineKeyboardButton("🔗 Rule-Bot Client", callback_data="rule_bot_client_access"),
                    InlineKeyboardButton("ℹ️ 使用帮助", callback_data="help"),
                ]
            )
        else:
            keyboard.append([InlineKeyboardButton("ℹ️ 使用帮助", callback_data="help")])
        keyboard.append(
            [
                InlineKeyboardButton(
                    "➖ 删除规则（暂未开放）", callback_data="delete_rule"
                )
            ]
        )
        return InlineKeyboardMarkup(keyboard)

    def _build_delete_unavailable_text(self) -> str:
        """Build the visible placeholder for the planned rule-deletion workflow."""
        return (
            "➖ *删除规则（暂未开放）*\n\n"
            "🧩 此入口为后续版本的规则删除流程预留。\n"
            "当前暂未开放实际删除功能。\n\n"
            "🛡️ 进入本页面不会删除或修改任何规则。"
        )

    def _build_help_text(self) -> str:
        """构建帮助文案"""
        lines = [
            "ℹ️ *使用帮助*",
            "",
            "💬 *私聊命令*",
            "🔍 `/query` 查询域名状态",
            "➕ `/add` 检查并提交直连规则",
            "🪪 `/id` 查看 Telegram 用户 ID",
            "⏭️ `/skip` 跳过可选说明并提交",
            "ℹ️ `/help` 打开本页",
        ]
        if getattr(self.config, "ALLOWED_GROUP_IDS", None):
            lines.extend(
                [
                    "",
                    "👥 *群聊使用*",
                    "在已授权群组中 @机器人并附带域名。",
                    "检查通过后会直接公开提交，不再确认。",
                ]
            )
        else:
            lines.extend(
                ["", "👥 *群聊使用*", "仅在管理员授权的群组中生效。"]
            )
        if getattr(self.config, "RULE_BOT_CLIENT_COMMUNITY_API_ENABLED", False):
            lines.extend(
                [
                    "",
                    "🔗 *Rule-Bot Client Community 接入*",
                    "从首页申请或管理个人 Token。",
                ]
            )
        lines.extend(
            [
                "",
                "➖ *删除规则*",
                "入口已为后续版本保留，当前暂未开放。",
            ]
        )
        return "\n".join(lines)

    def _build_help_keyboard(self) -> InlineKeyboardMarkup:
        """构建帮助键盘"""
        return self._repo_and_home_keyboard()

    async def _build_stats_text(self, user_id: Optional[int] = None, include_limit: bool = False) -> str:
        """构建统计信息文案"""
        try:
            github_stats = await self.github_service.get_file_stats()
            direct_rule_count = github_stats.get("rule_count") if "error" not in github_stats else None
            geosite_count = len(self.data_manager.geosite_domains)
            direct_text = (
                f"{direct_rule_count:,}"
                if isinstance(direct_rule_count, int)
                else "暂时无法获取"
            )
            lines = [
                "📊 *当前数据*",
                f"📚 公开直连规则：{direct_text}",
                f"🇨🇳 GEOSITE:CN：{geosite_count:,}",
            ]

            if include_limit and user_id is not None:
                can_add, remaining = self.check_user_add_limit(user_id)
                if can_add:
                    lines.append(f"⏱️ 本小时还可添加：{remaining} 个域名")
                else:
                    lines.append("⏱️ 本小时已达到添加上限，请稍后再试")

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return (
                "📊 *当前数据*\n"
                "⚠️ 直连规则统计暂时无法获取，不影响继续输入"
            )

    def _format_detail_lines(
        self, details: list, limit: Optional[int] = None
    ) -> str:
        """格式化检查详情"""
        if not details:
            return ""

        limit = limit or self.MAX_DETAIL_LINES
        lines = []
        for detail in details[:limit]:
            detail = self._single_line_display(detail)
            lines.append(f"• {self.escape_markdown(detail)}")

        remaining = len(details) - limit
        if remaining > 0:
            lines.append(f"• 另有 {remaining} 条详情未显示")

        return "\n".join(lines)

    def _build_query_summary_text(
        self,
        domain: str,
        github_result: dict,
        in_geosite: bool,
        check_result: dict,
        conclusion: str,
    ) -> str:
        """Build the default query page as a compact decision summary."""
        github_unavailable = bool(github_result.get("error"))
        github_exists = bool(github_result.get("exists"))
        check_failed = "error" in check_result
        matches = github_result.get("matches", [])
        visible_domain = self._single_line_display(domain)

        if github_unavailable:
            github_status = "暂时无法读取"
        elif github_exists:
            github_status = f"已存在（{len(matches)} 条匹配）"
        else:
            github_status = "未找到"

        lines = [
            conclusion,
            "",
            f"`{visible_domain}`",
            "",
            "📋 *状态摘要*",
            f"📚 GitHub 规则库：{github_status}",
            f"🇨🇳 GEOSITE:CN：{'已存在' if in_geosite else '未找到'}",
        ]
        if check_failed:
            lines.append("🌐 网络检查：暂时无法完成")
            return "\n".join(lines)

        signals = []
        if check_result.get("domain_china_status"):
            signals.append("输入域名 IP")
        if check_result.get("second_level_china_status"):
            signals.append("可注册域名 IP")
        if check_result.get("ns_china_status"):
            signals.append("NS")
        normalized_domain = check_result.get("normalized_domain")
        registered_domain = check_result.get("second_level_domain")
        input_ip_count = len(check_result.get("domain_ips", []) or [])
        registered_ip_count = len(
            check_result.get("second_level_ips", []) or []
        )
        if (
            normalized_domain
            and registered_domain
            and normalized_domain == registered_domain
        ):
            resolution_status = (
                f"🌐 DNS 解析：输入 {input_ip_count} · 主域名同输入"
            )
        else:
            resolution_status = (
                f"🌐 DNS 解析：输入 {input_ip_count} · "
                f"可注册域名 {registered_ip_count}"
            )
        lines.extend(
            [
                f"📡 中国大陆信号：{'、'.join(signals) if signals else '未检测到'}",
                resolution_status,
            ]
        )
        recommendation = str(check_result.get("recommendation", "")).strip()
        if (
            recommendation
            and not github_unavailable
            and not github_exists
            and not in_geosite
        ):
            lines.extend(
                [
                    "",
                    "💡 *综合判断*",
                    self.escape_markdown(self._single_line_display(recommendation)),
                ]
            )
        return "\n".join(lines)

    def _build_query_detail_text(
        self,
        domain: str,
        github_result: dict,
        check_result: dict,
    ) -> str:
        """Build a bounded technical page without repeating the summary."""
        visible_domain = self._single_line_display(domain)
        lines = ["🔎 *技术详情*", "", f"`{visible_domain}`"]
        github_unavailable = bool(github_result.get("error"))
        matches = github_result.get("matches", [])
        if github_unavailable:
            lines.extend(["", "📚 *规则命中*", "GitHub 规则暂时无法读取"])
        elif matches:
            lines.extend(
                ["", "📚 *规则命中*", self._format_rule_matches(matches)]
            )

        if "error" in check_result:
            lines.extend(
                ["", "🌐 *网络检查*", "暂时无法完成，请稍后重试。"]
            )
        else:
            domain_ips = self._format_value_list(check_result.get("domain_ips", []))
            registered_ips = self._format_value_list(
                check_result.get("second_level_ips", [])
            )
            if domain_ips or registered_ips:
                lines.extend(["", "🌐 *解析结果*"])
                if domain_ips:
                    lines.extend(["🔹 输入域名 IP", domain_ips])
                if registered_ips:
                    lines.extend(["🔸 可注册域名 IP", registered_ips])
            detail_lines = self._format_detail_lines(check_result.get("details", []))
            if detail_lines:
                lines.extend(["", "📡 *归属检查*", detail_lines])

        if len(lines) == 3:
            lines.extend(["", "暂无更多技术详情。"])
        return "\n".join(lines)

    @staticmethod
    def _build_query_result_keyboard(
        token: str,
        *,
        detail: bool,
        add_callback: str = "",
    ) -> InlineKeyboardMarkup:
        keyboard = []
        if add_callback:
            keyboard.append(
                [InlineKeyboardButton("➕ 添加到直连规则", callback_data=add_callback)]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "↩️ 返回摘要" if detail else "🔎 技术详情",
                    callback_data=(
                        f"query_summary|{token}"
                        if detail
                        else f"query_details|{token}"
                    ),
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🔍 查询其他域名", callback_data="query_domain"
                ),
                InlineKeyboardButton("🏠 返回首页", callback_data="main_menu"),
            ]
        )
        return InlineKeyboardMarkup(keyboard)

    def _build_query_prompt(self, stats_text: str) -> str:
        """构建查询提示文案"""
        return (
            "🔍 *查询域名状态*\n\n"
            "请发送一个域名或 URL。系统将依次核对：\n"
            "📚 公开直连规则\n"
            "🇨🇳 `GEOSITE:CN` 收录状态\n"
            "🌐 域名及可注册域名 IP\n"
            "📡 NS 服务器归属\n\n"
            "🧪 *输入示例*\n"
            "`example.com`\n"
            "`https://example.com/path`\n\n"
            "🧾 如需继续添加，系统会先确认最终使用的\n"
            "可注册域名（主域名），并展示拟提交规则。\n\n"
            f"{stats_text.strip()}"
        )

    def _build_add_prompt(self, stats_text: str) -> str:
        """构建添加提示文案"""
        return (
            "➕ *添加直连规则*\n\n"
            "请发送一个域名或 URL。系统将提取并核对\n"
            "可注册域名（主域名），同时检查规则库、\n"
            "`GEOSITE:CN`、IP 与 NS 服务器归属。\n\n"
            "🧪 *输入示例*\n"
            "`example.com`\n"
            "`https://example.com/path`\n\n"
            "🧾 *提交说明*\n"
            "系统会先展示最终规则、判断依据与公开范围；\n"
            "只有明确确认后，才会写入公开 GitHub 仓库。\n\n"
            f"{stats_text.strip()}"
        )
    
    async def check_group_membership(
        self,
        update: Update,
        *,
        force_refresh: bool = False,
        callback_answered: bool = False,
    ) -> bool:
        """检查用户群组成员身份"""
        if not self.group_service or not self.group_service.is_group_check_enabled():
            return True
        
        user_id = update.effective_user.id
        check_result = await self.group_service.check_user_in_group(
            user_id, force_refresh=force_refresh
        )
        
        if check_result is True:
            return True

        if check_result is False:
            join_message = self.group_service.get_join_group_message()
            join_keyboard = self.group_service.get_join_group_keyboard()
            if update.callback_query:
                if not callback_answered:
                    await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    join_message,
                    reply_markup=join_keyboard,
                    parse_mode='Markdown',
                )
            else:
                await update.message.reply_text(
                    join_message,
                    reply_markup=join_keyboard,
                    parse_mode='Markdown',
                )
            return False

        error_message = (
            "⚠️ *暂时无法验证群成员*\n\n"
            "请稍后点击“重新验证”。"
        )
        retry_keyboard = self.group_service.get_join_group_keyboard()
        if update.callback_query:
            if not callback_answered:
                await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                error_message,
                reply_markup=retry_keyboard,
                parse_mode='Markdown',
            )
        else:
            await update.message.reply_text(
                error_message,
                reply_markup=retry_keyboard,
                parse_mode='Markdown',
            )
        
        return False

    def is_update_context_allowed(self, update: Update) -> bool:
        """Allow private chats and explicitly configured group chats only."""
        chat = update.effective_chat
        if not chat:
            return False
        if chat.type == "private":
            return True
        if chat.type in ("group", "supergroup"):
            return chat.id in self.config.ALLOWED_GROUP_IDS
        return False
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        try:
            # 检查群组成员身份
            if not await self.check_group_membership(update):
                return
            
            user = update.effective_user
            self._reset_user_flow(user.id)
            username = user.first_name or user.username or "用户"
            await update.message.reply_text(
                self._build_main_menu_text(username),
                reply_markup=self._build_main_menu_keyboard(),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            
        except Exception as e:
            logger.error(f"处理 start 命令失败: {e}")
            await update.message.reply_text(
                "❌ *服务暂时不可用*\n\n请稍后再试。",
                reply_markup=self._build_main_menu_keyboard(),
                parse_mode="Markdown",
            )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        if not await self.check_group_membership(update):
            return
        self._reset_user_flow(update.effective_user.id)
        await update.message.reply_text(
            self._build_help_text(),
            reply_markup=self._build_help_keyboard(),
            parse_mode='Markdown'
        )

    async def id_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /id 命令，返回用户 ID"""
        try:
            if not await self.check_group_membership(update):
                return

            user = update.effective_user
            self._reset_user_flow(user.id)
            identity_label = "用户名" if user.username else "显示名"
            text = (
                "🆔 *Telegram 用户 ID*\n\n"
                "🔢 *账号标识*\n"
                f"`{user.id}`\n\n"
                f"👤 {identity_label}：{self._format_telegram_identity(user)}"
            )
            await update.message.reply_text(
                text,
                reply_markup=self._recovery_keyboard(),
                parse_mode='Markdown',
            )
        except Exception as e:
            logger.error(f"处理 id 命令失败: {e}")
            await update.message.reply_text(
                "❌ *暂时无法读取用户 ID*\n\n请稍后重试。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )

    async def query_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /query 命令"""
        if not await self.check_group_membership(update):
            return
        user_id = update.effective_user.id
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_query_domain")

        stats_text = await self._build_stats_text()
        await update.message.reply_text(
            self._build_query_prompt(stats_text),
            reply_markup=self._home_keyboard(),
            parse_mode='Markdown'
        )

    async def add_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /add 命令"""
        if not await self.check_group_membership(update):
            return
        user_id = update.effective_user.id
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_add_domain")
        stats_text = await self._build_stats_text(user_id=user_id, include_limit=True)
        await update.message.reply_text(
            self._build_add_prompt(stats_text),
            reply_markup=self._recovery_keyboard(),
            parse_mode='Markdown',
        )
    
    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /delete 命令"""
        if not await self.check_group_membership(update):
            return
        self._reset_user_flow(update.effective_user.id)
        await update.message.reply_text(
            self._build_delete_unavailable_text(),
            reply_markup=self._recovery_keyboard(),
            parse_mode='Markdown',
        )

    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Give unknown private commands a visible route back to supported actions."""
        if not await self.check_group_membership(update):
            return
        user_id = update.effective_user.id
        self._reset_user_flow(user_id)
        await update.message.reply_text(
            "⚠️ *无法识别命令*\n\n"
            "🧭 请使用下方功能菜单，或发送 `/help` 查看完整帮助。",
            reply_markup=self._build_main_menu_keyboard(),
            parse_mode="Markdown",
        )
    
    async def skip_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /skip 命令"""
        try:
            if not await self.check_group_membership(update):
                return

            user_id = update.effective_user.id
            user_state = self.get_user_state(user_id)
            if user_state.get("state") != "waiting_description":
                await update.message.reply_text(
                    "ℹ️ *当前没有可跳过的说明*\n\n"
                    "🧾 请先开始添加直连规则，并完成提交前检查。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 开始添加"
                    ),
                    parse_mode="Markdown",
                )
                return

            await self._add_domain_to_github_message(update.message, user_id, "")
        except Exception as e:
            logger.error(f"处理 skip 命令失败: {e}")
            await update.message.reply_text(
                "❌ *暂时无法提交*\n\n"
                "🛡️ 本次操作未修改任何规则。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新开始"
                ),
                parse_mode="Markdown",
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理回调查询"""
        query = update.callback_query
        try:
            if not self.is_update_context_allowed(update):
                await query.answer("当前会话不在允许范围内", show_alert=True)
                return
            data = query.data
            chat_type = getattr(update.effective_chat, "type", None)
            is_admin_force_callback = data.startswith(
                (
                    "admin_force_add|",
                    "admin_force_add_confirm|",
                    "admin_force_add_cancel|",
                )
            )
            is_group_admin_force = (
                chat_type in ("group", "supergroup")
                and is_admin_force_callback
            )
            if (
                chat_type in ("group", "supergroup")
                and not is_group_admin_force
            ):
                await query.answer(
                    "此按钮仅用于私聊；请私聊机器人继续操作。",
                    show_alert=True,
                )
                return
            user_id = update.effective_user.id
            if is_group_admin_force:
                await self._handle_admin_force_add_callback(
                    query, user_id, data
                )
                return
            self_service_callbacks = {
                GroupService.MEMBERSHIP_RETRY_CALLBACK,
                "rule_bot_client_access",
                "rule_bot_client_issue",
                "rule_bot_client_revoke",
                "rule_bot_client_delete_credential",
                "rule_bot_client_privacy",
                "rule_bot_client_privacy_accept",
                "rule_bot_client_privacy_withdraw",
            }
            is_self_service_callback = data in self_service_callbacks or data.startswith(
                (
                    "rule_bot_client_issue_confirm|",
                    "rule_bot_client_revoke_confirm|",
                    "rule_bot_client_privacy_withdraw_confirm|",
                )
            )
            # Rule-Bot Client issuance performs its own fresh membership check;
            # access, revocation and credential deletion remain available.
            if (
                not is_self_service_callback
                and not is_group_admin_force
                and not await self.check_group_membership(update)
            ):
                return
            
            await query.answer()
            
            if data == "main_menu":
                await self._show_main_menu(query, user_id)
            elif data == GroupService.MEMBERSHIP_RETRY_CALLBACK:
                if await self.check_group_membership(
                    update,
                    force_refresh=True,
                    callback_answered=True,
                ):
                    await self._show_main_menu(query, user_id)
            elif data == "query_domain":
                await self._start_domain_query(query, user_id)
            elif data == "add_direct_rule":
                await self._start_add_direct_rule(query, user_id)
            elif data == "add_proxy_rule":
                await self._show_proxy_rule_not_supported(query, user_id)
            elif data == "delete_rule":
                await self._show_delete_not_supported(query, user_id)
            elif data == "help":
                await self._show_help(query, user_id)
            elif data == "rule_bot_client_access":
                await self._show_rule_bot_client_access(query, user_id)
            elif data == "rule_bot_client_issue":
                await self._issue_rule_bot_client_token(query, user_id)
            elif data == "rule_bot_client_revoke":
                await self._revoke_rule_bot_client_token(query, user_id)
            elif data.startswith("rule_bot_client_issue_confirm|"):
                await self._confirm_rule_bot_client_issue(query, user_id, data)
            elif data.startswith("rule_bot_client_revoke_confirm|"):
                await self._confirm_rule_bot_client_revoke(query, user_id, data)
            elif data == "rule_bot_client_delete_credential":
                await query.message.delete()
            elif data == "rule_bot_client_privacy":
                await self._show_rule_bot_client_privacy(query, user_id)
            elif data == "rule_bot_client_privacy_accept":
                await self._accept_rule_bot_client_privacy(query, user_id)
            elif data == "rule_bot_client_privacy_withdraw":
                await self._withdraw_rule_bot_client_privacy(query, user_id)
            elif data.startswith("rule_bot_client_privacy_withdraw_confirm|"):
                await self._confirm_rule_bot_client_privacy_withdraw(query, user_id, data)
            elif data.startswith("query_details|"):
                await self._show_query_result_page(
                    query, user_id, data, detail=True
                )
            elif data.startswith("query_summary|"):
                await self._show_query_result_page(
                    query, user_id, data, detail=False
                )
            elif data.startswith("add_domain|"):
                await self._handle_add_domain_callback(query, user_id, data)
            elif data.startswith("confirm_add|"):
                await self._handle_confirm_add_callback(query, user_id, data)
            elif data == "skip_description":
                await self._handle_skip_description(query, user_id)
            elif is_admin_force_callback:
                await self._handle_admin_force_add_callback(query, user_id, data)
            else:
                await query.edit_message_text(
                    "⌛ *操作已过期*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。\n"
                    "请从下方入口重新开始。",
                    reply_markup=self._recovery_keyboard(),
                    parse_mode="Markdown",
                )
                
        except Exception as e:
            logger.error(f"处理回调失败: {e}")
            await query.edit_message_text(
                "❌ *操作失败*\n\n"
                "⚠️ 页面可能未完整更新。\n"
                "🔎 若刚才涉及提交，请先查询规则再重试。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理文本消息"""
        try:
            # 检查群组成员身份
            if not await self.check_group_membership(update):
                return
            
            user_id = update.effective_user.id
            text = update.message.text.strip()
            user_state = self.get_user_state(user_id)
            
            state = user_state.get("state", "idle")
            
            if state == "waiting_query_domain":
                await self._handle_domain_query(update, text, user_id)
            elif state == "waiting_add_domain":
                await self._handle_add_domain_input(update, text, user_id)
            elif state == "waiting_description":
                await self._handle_description_input(update, text, user_id)
            else:
                # 默认处理：显示主菜单
                await self._show_main_menu_message(update.message)
                
        except Exception as e:
            logger.error(f"处理消息失败: {e}")
            await update.message.reply_text(
                "❌ *暂时无法处理消息*\n\n请稍后重试。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )
    
    async def _show_main_menu(self, query, user_id: Optional[int] = None):
        """显示主菜单"""
        user_id = user_id or query.from_user.id
        self._reset_user_flow(user_id)
        username = query.from_user.first_name or query.from_user.username or "用户"
        welcome_text = self._build_main_menu_text(username)
        reply_markup = self._build_main_menu_keyboard()
        await query.edit_message_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    async def _show_main_menu_message(self, message):
        """通过消息显示主菜单"""
        self._reset_user_flow(message.from_user.id)
        username = message.from_user.first_name or message.from_user.username or "用户"
        welcome_text = self._build_main_menu_text(username)
        reply_markup = self._build_main_menu_keyboard()
        await message.reply_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

    def _rule_bot_client_endpoint(self) -> str:
        return (
            f"{self.config.RULE_BOT_CLIENT_COMMUNITY_API_BASE_URL}"
            f"{self.config.RULE_BOT_CLIENT_COMMUNITY_API_PATH}"
        )

    @staticmethod
    def _rule_bot_client_token_is_active(status: Optional[dict]) -> bool:
        return bool(
            status
            and status.get("enabled")
            and int(status.get("expires_at", 0)) > int(time.time())
        )

    def _build_rule_bot_client_access_text(
        self, status_text: str, expires: Optional[str] = None
    ) -> str:
        lines = [
            "🔗 *Rule-Bot Client Community 接入*",
            "",
            "已验证的群组成员可申请独立 Token，",
            "用于从 Rule-Bot Client 客户端提交待判断域名。",
            "",
            "🔐 *凭据说明*",
            "Token 仅在签发时显示一次，请妥善保存。",
            "重新签发或吊销后，旧 Token 将立即失效。",
            "",
            "📌 *当前状态*",
            status_text,
        ]
        if expires:
            lines.append(f"⏳ 有效期至：`{expires}`")
        return "\n".join(lines)

    @staticmethod
    def _build_rule_bot_client_privacy_text(consented: bool) -> str:
        return (
            "🛡️ *Rule-Bot Client Community 隐私说明*\n\n"
            "📱 *Rule-Bot Client*\n"
            "默认只上报用于规则判断的可注册域名。\n"
            "本地会先去重，并排除 `.cn` 与排除项。\n\n"
            "🚫 *默认不会主动上报*\n"
            "URL 路径、查询参数、网页内容或访问次数。\n\n"
            "👤 *账号关联*\n"
            "Token 与账号关联，用于成员校验、限流、\n"
            "续签和吊销。Telegram 身份不会写入规则。\n\n"
            "🌍 *公开范围*\n"
            "成功添加的域名会公开出现在 GitHub；\n"
            "被拒绝的域名不会公开。\n\n"
            "🌐 *网络与客户端*\n"
            "若入口经过 CDN 或代理，可能处理 IP、时间\n"
            "和请求元数据。第三方客户端行为需自行确认。\n\n"
            f"📌 当前状态：*{'已同意' if consented else '尚未同意'}*"
        )

    async def _show_rule_bot_client_access(self, query, user_id: int):
        """Show self-service token status without exposing the credential."""
        self._reset_user_flow(user_id)
        if not self.rule_bot_client_token_service:
            await query.edit_message_text(
                "ℹ️ *Rule-Bot Client Community 接入*\n\n当前暂未开放。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )
            return
        status = await self.rule_bot_client_token_service.status(user_id)
        token_active = self._rule_bot_client_token_is_active(status)
        consented = await self.rule_bot_client_token_service.has_current_consent(user_id)
        active = token_active and consented
        status_text = "尚未签发或已失效"
        expires = None
        issue_label = "🔑 申请 Token"
        if active:
            expires = datetime.fromtimestamp(
                int(status["expires_at"]), timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            status_text = "有效"
            issue_label = "🔄 重新签发 Token"
        elif token_active:
            status_text = "已暂停\n请先确认当前隐私说明"
        keyboard = []
        if consented:
            keyboard.append(
                [InlineKeyboardButton(issue_label, callback_data="rule_bot_client_issue")]
            )
        keyboard.append(
            [InlineKeyboardButton("🛡️ 隐私说明", callback_data="rule_bot_client_privacy")]
        )
        if token_active:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "📋 复制接口入口",
                        copy_text=CopyTextButton(text=self._rule_bot_client_endpoint()),
                    )
                ]
            )
        if token_active:
            keyboard.append(
                [InlineKeyboardButton("🚫 吊销当前 Token", callback_data="rule_bot_client_revoke")]
            )
        keyboard.append([InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")])
        await query.edit_message_text(
            self._build_rule_bot_client_access_text(status_text, expires),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def _show_rule_bot_client_privacy(self, query, user_id: int):
        """Explain the community ingress privacy boundary before consent."""
        self._reset_user_flow(user_id)
        if not self.rule_bot_client_token_service:
            await query.edit_message_text(
                "ℹ️ *Rule-Bot Client Community 接入*\n\n当前暂未开放。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )
            return
        consented = await self.rule_bot_client_token_service.has_current_consent(user_id)
        keyboard = []
        if not consented:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "✅ 同意隐私说明", callback_data="rule_bot_client_privacy_accept"
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🚫 撤回同意并吊销 Token",
                        callback_data="rule_bot_client_privacy_withdraw",
                    )
                ]
            )
        keyboard.extend(
            [
                [
                    InlineKeyboardButton(
                        "📖 完整隐私说明",
                        url="https://github.com/Aethersailor/Rule-Bot/blob/master/PRIVACY.md",
                    )
                ],
                [InlineKeyboardButton("↩️ 返回接入页", callback_data="rule_bot_client_access")],
            ]
        )
        await query.edit_message_text(
            self._build_rule_bot_client_privacy_text(consented),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def _accept_rule_bot_client_privacy(self, query, user_id: int):
        if not self.rule_bot_client_token_service:
            await query.edit_message_text(
                "ℹ️ *Rule-Bot Client Community 接入*\n\n当前暂未开放。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )
            return
        await self.rule_bot_client_token_service.consent(user_id)
        await self._show_rule_bot_client_access(query, user_id)

    async def _withdraw_rule_bot_client_privacy(self, query, user_id: int):
        self._reset_user_flow(user_id)
        if not self.rule_bot_client_token_service:
            await query.edit_message_text(
                "ℹ️ *Rule-Bot Client Community 接入*\n\n当前暂未开放。",
                reply_markup=self._recovery_keyboard(),
                parse_mode="Markdown",
            )
            return
        token = self.create_pending_action(
            user_id, "rule_bot_client_privacy_withdraw", confirmed=True
        )
        await query.edit_message_text(
            "⚠️ *撤回隐私同意？*\n\n"
            "🚫 当前 Token 将同时吊销，使用该凭据的客户端\n"
            "将无法继续提交域名。此操作不会删除既有公开规则。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🚫 确认撤回并吊销",
                            callback_data=f"rule_bot_client_privacy_withdraw_confirm|{token}",
                        )
                    ],
                    [InlineKeyboardButton("↩️ 取消", callback_data="rule_bot_client_privacy")],
                ]
            ),
            parse_mode="Markdown",
        )

    async def _confirm_rule_bot_client_privacy_withdraw(
        self, query, user_id: int, data: str
    ):
        token = data.split("|", 1)[1] if "|" in data else ""
        action = self.get_pending_action(
            user_id, token, "rule_bot_client_privacy_withdraw", consume=True
        )
        if not action:
            await query.edit_message_text(
                "⌛ *确认已过期*\n\n"
                "隐私同意和 Token 均未更改。",
                reply_markup=self._recovery_keyboard(
                    "rule_bot_client_privacy", "🛡️ 返回隐私说明"
                ),
                parse_mode="Markdown",
            )
            return
        await self.rule_bot_client_token_service.withdraw_consent(user_id)
        await query.edit_message_text(
            "🚫 *隐私同意已撤回*\n\n"
            "🔐 当前 Rule-Bot Client Community Token 已同时吊销。\n"
            "既有公开规则未被删除或修改。",
            reply_markup=self._recovery_keyboard(
                "rule_bot_client_access", "↩️ 返回接入页"
            ),
            parse_mode="Markdown",
        )

    async def _issue_rule_bot_client_token(self, query, user_id: int):
        """Issue a token, requiring confirmation when an old token would be replaced."""
        self._reset_user_flow(user_id)
        if not await self.rule_bot_client_token_service.has_current_consent(user_id):
            await self._show_rule_bot_client_privacy(query, user_id)
            return
        status = await self.rule_bot_client_token_service.status(user_id)
        if self._rule_bot_client_token_is_active(status):
            token = self.create_pending_action(
                user_id, "rule_bot_client_reissue", confirmed=True
            )
            await query.edit_message_text(
                "⚠️ *重新签发 Token？*\n\n"
                "🔄 继续后将生成新的独立凭据，旧 Token 会立即失效。\n"
                "使用旧 Token 的客户端将无法继续提交域名。",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 确认重新签发",
                                callback_data=f"rule_bot_client_issue_confirm|{token}",
                            )
                        ],
                        [InlineKeyboardButton("↩️ 取消", callback_data="rule_bot_client_access")],
                    ]
                ),
                parse_mode="Markdown",
            )
            return
        await self._perform_rule_bot_client_issue(query, user_id)

    async def _confirm_rule_bot_client_issue(self, query, user_id: int, data: str):
        token = data.split("|", 1)[1] if "|" in data else ""
        action = self.get_pending_action(
            user_id, token, "rule_bot_client_reissue", consume=True
        )
        if not action:
            await query.edit_message_text(
                "⌛ *确认已过期*\n\n原 Token 未更改。",
                reply_markup=self._recovery_keyboard(
                    "rule_bot_client_access", "↩️ 返回接入页"
                ),
                parse_mode="Markdown",
            )
            return
        if not await self.rule_bot_client_token_service.has_current_consent(user_id):
            await self._show_rule_bot_client_privacy(query, user_id)
            return
        await self._perform_rule_bot_client_issue(query, user_id)

    async def _perform_rule_bot_client_issue(self, query, user_id: int):
        """Freshly verify membership and deliver a one-time credential message."""
        membership = await self.group_service.check_user_in_group(
            user_id, force_refresh=True
        )
        if membership is not True:
            if membership is False:
                message = self.group_service.get_join_group_message()
                reply_markup = self.group_service.get_join_group_keyboard()
            else:
                message = (
                    "⚠️ *暂时无法验证群成员*\n\n"
                    "请稍后重新申请 Token。"
                )
                reply_markup = self._recovery_keyboard(
                    "rule_bot_client_access", "↩️ 返回接入页"
                )
            await query.edit_message_text(
                message,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return

        issued = await self.rule_bot_client_token_service.issue(user_id)
        token = issued["token"]
        endpoint = self._rule_bot_client_endpoint()
        expires = datetime.fromtimestamp(
            issued["expires_at"], timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📋 复制 Token", copy_text=CopyTextButton(text=token)
                    ),
                    InlineKeyboardButton(
                        "📋 复制入口", copy_text=CopyTextButton(text=endpoint)
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ 删除凭据消息",
                        callback_data="rule_bot_client_delete_credential",
                    )
                ],
            ]
        )
        await query.message.reply_text(
            "🔐 *Rule-Bot Client Community 凭据已签发*\n\n"
            "⚠️ 此凭据只显示一次，请勿转发或公开。\n\n"
            f"🌐 *接口入口*\n`{endpoint}`\n\n"
            f"🔑 *个人 Token*\n`{token}`\n\n"
            f"⏳ *有效期至*\n`{expires}`\n\n"
            "🗑️ 妥善保存后可删除本消息；\n"
            "删除消息不会吊销 Token。",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        await self._show_rule_bot_client_access(query, user_id)

    async def _revoke_rule_bot_client_token(self, query, user_id: int):
        self._reset_user_flow(user_id)
        status = await self.rule_bot_client_token_service.status(user_id)
        if not self._rule_bot_client_token_is_active(status):
            await query.edit_message_text(
                "ℹ️ *当前没有可吊销的 Token*",
                reply_markup=self._recovery_keyboard(
                    "rule_bot_client_access", "↩️ 返回接入页"
                ),
                parse_mode="Markdown",
            )
            return
        token = self.create_pending_action(
            user_id, "rule_bot_client_revoke", confirmed=True
        )
        await query.edit_message_text(
            "⚠️ *吊销当前 Token？*\n\n"
            "🚫 吊销后，使用该 Token 的客户端将立即停止提交。\n"
            "此操作不会删除或修改已经公开的规则。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🚫 确认吊销",
                            callback_data=f"rule_bot_client_revoke_confirm|{token}",
                        )
                    ],
                    [InlineKeyboardButton("↩️ 取消", callback_data="rule_bot_client_access")],
                ]
            ),
            parse_mode="Markdown",
        )

    async def _confirm_rule_bot_client_revoke(self, query, user_id: int, data: str):
        token = data.split("|", 1)[1] if "|" in data else ""
        action = self.get_pending_action(
            user_id, token, "rule_bot_client_revoke", consume=True
        )
        if not action:
            await query.edit_message_text(
                "⌛ *确认已过期*\n\n当前 Token 未更改。",
                reply_markup=self._recovery_keyboard(
                    "rule_bot_client_access", "↩️ 返回接入页"
                ),
                parse_mode="Markdown",
            )
            return
        revoked = await self.rule_bot_client_token_service.revoke(user_id)
        text = (
            "🚫 *Token 已吊销*\n\n"
            "🔐 使用原 Token 的客户端已无法继续提交。\n"
            "既有公开规则未被删除或修改。"
            if revoked
            else "ℹ️ *当前没有可吊销的 Token*"
        )
        await query.edit_message_text(
            text,
            reply_markup=self._recovery_keyboard(
                "rule_bot_client_access", "↩️ 返回接入页"
            ),
            parse_mode="Markdown",
        )

    async def _start_domain_query(self, query, user_id: int):
        """开始域名查询"""
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_query_domain")

        stats_text = await self._build_stats_text()
        await query.edit_message_text(
            self._build_query_prompt(stats_text),
            reply_markup=self._home_keyboard(),
            parse_mode='Markdown'
        )

    async def _start_add_direct_rule(self, query, user_id: int):
        """开始添加直连规则"""
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_add_domain")

        stats_text = await self._build_stats_text(user_id=user_id, include_limit=True)
        await query.edit_message_text(
            self._build_add_prompt(stats_text),
            reply_markup=self._home_keyboard(),
            parse_mode='Markdown'
        )

    async def _show_proxy_rule_not_supported(self, query, user_id: int):
        """显示代理规则不支持"""
        self._reset_user_flow(user_id)
        await query.edit_message_text(
            "➕ *添加代理规则*\n\n"
            "🧩 此入口为后续版本预留，当前暂未开放。\n\n"
            "🛡️ 进入本页面不会添加或修改任何规则。",
            reply_markup=self._home_keyboard(),
            parse_mode='Markdown'
        )
    
    async def _show_delete_not_supported(self, query, user_id: int):
        """显示删除功能不支持"""
        self._reset_user_flow(user_id)
        await query.edit_message_text(
            self._build_delete_unavailable_text(),
            reply_markup=self._home_keyboard(),
            parse_mode='Markdown'
        )
    
    async def _show_help(self, query, user_id: Optional[int] = None):
        """显示帮助信息"""
        if user_id is not None:
            self._reset_user_flow(user_id)
        await query.edit_message_text(
            self._build_help_text(),
            reply_markup=self._build_help_keyboard(),
            parse_mode='Markdown'
        )

    async def _handle_domain_query(self, update: Update, domain_input: str, user_id: int):
        """处理域名查询"""
        processing_msg = None
        try:
            processing_msg = await update.message.reply_text("🔍 正在查询域名…")
            domain = normalize_domain(domain_input)
            if not domain:
                await processing_msg.edit_text(
                    "⚠️ *无法识别域名*\n\n"
                    "📝 请发送有效域名或包含域名的 URL，\n"
                    "并检查输入中是否存在多余字符。",
                    reply_markup=self._recovery_keyboard(
                        "query_domain", "🔍 重新输入"
                    ),
                    parse_mode="Markdown",
                )
                return

            if is_cn_domain(domain):
                result_text = (
                    "ℹ️ *.cn 域名已默认直连*\n\n"
                    "🧾 *已检查域名*\n"
                    f"`{domain}`\n\n"
                    "🇨🇳 `.cn` 域名已由现有策略默认直连，\n"
                    "无需再次写入公开规则库。"
                )
                keyboard = [
                    [InlineKeyboardButton("🔍 查询其他域名", callback_data="query_domain")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                await processing_msg.edit_text(
                    result_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown',
                )
                self.set_user_state(user_id, "waiting_query_domain")
                return

            github_result = await self.github_service.check_domain_in_rules(domain)
            in_geosite = await self.data_manager.is_domain_in_geosite(domain)
            await processing_msg.edit_text("🔍 正在检查规则与网络信息…")
            check_result = await self.domain_checker.check_domain_comprehensive(domain)

            github_unavailable = bool(github_result.get("error"))
            github_exists = bool(github_result.get("exists"))
            check_failed = "error" in check_result
            has_china_signal = bool(
                not check_failed
                and (
                    check_result.get("domain_china_status")
                    or check_result.get("second_level_china_status")
                    or check_result.get("ns_china_status")
                )
            )

            if github_exists or in_geosite:
                conclusion = "✅ *已被直连规则覆盖*"
            elif check_failed:
                conclusion = "❌ *域名检查失败*"
            elif github_unavailable and has_china_signal:
                conclusion = "⚠️ *检测到中国大陆信号*"
            elif github_unavailable:
                conclusion = "⚠️ *查询结果不完整*"
            elif has_china_signal:
                conclusion = "✅ *可以继续评估添加*"
            else:
                conclusion = "ℹ️ *暂不建议添加*"

            summary_text = self._build_query_summary_text(
                domain,
                github_result,
                in_geosite,
                check_result,
                conclusion,
            )
            detail_text = self._build_query_detail_text(
                domain, github_result, check_result
            )

            self._discard_pending_actions(
                user_id, {"query_result_pages", "add_domain"}
            )
            add_callback = ""
            if (
                not github_unavailable
                and not github_exists
                and not in_geosite
                and not check_failed
                and has_china_signal
            ):
                token = self.create_pending_action(user_id, "add_domain", domain=domain)
                add_callback = f"add_domain|{token}"

            page_token = self.create_pending_action(
                user_id,
                "query_result_pages",
                summary_text=summary_text,
                detail_text=detail_text,
                add_callback=add_callback,
            )

            await processing_msg.edit_text(
                summary_text,
                reply_markup=self._build_query_result_keyboard(
                    page_token,
                    detail=False,
                    add_callback=add_callback,
                ),
                parse_mode='Markdown',
            )
            self.set_user_state(user_id, "waiting_query_domain")
        except Exception as e:
            logger.error(f"域名查询失败: {e}")
            text = "❌ *查询失败*\n\n请稍后重试。"
            markup = self._recovery_keyboard("query_domain", "🔍 重新查询")
            if processing_msg is not None:
                await processing_msg.edit_text(
                    text, reply_markup=markup, parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=markup, parse_mode="Markdown"
                )

    async def _show_query_result_page(
        self, query, user_id: int, data: str, *, detail: bool
    ) -> None:
        """Switch between a cached query summary and its technical details."""
        token = data.split("|", 1)[1] if "|" in data else ""
        page = self.get_pending_action(
            user_id,
            token,
            "query_result_pages",
            consume=False,
        )
        if not page:
            await query.edit_message_text(
                "⌛ *查询详情已过期*\n\n"
                "🔍 缓存的检查结果已失效，请重新查询域名。",
                reply_markup=self._recovery_keyboard(
                    "query_domain", "🔍 重新查询"
                ),
                parse_mode="Markdown",
            )
            return

        try:
            await query.edit_message_text(
                page["detail_text"] if detail else page["summary_text"],
                reply_markup=self._build_query_result_keyboard(
                    token,
                    detail=detail,
                    add_callback=page.get("add_callback", ""),
                ),
                parse_mode="Markdown",
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        self.set_user_state(user_id, "waiting_query_domain")
    
    async def _handle_add_domain_input(self, update: Update, domain_input: str, user_id: int):
        """处理添加域名输入"""
        processing_msg = None
        try:
            # 发送处理中消息
            processing_msg = await update.message.reply_text("🔍 正在检查域名…")
            
            # 检查用户添加频率限制
            can_add, remaining = self.check_user_add_limit(user_id)
            if not can_add:
                keyboard = [
                    [InlineKeyboardButton("🔍 查询域名", callback_data="query_domain")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(
                    "⚠️ *本小时添加次数已用完*\n\n"
                    f"⏱️ 每个账号每小时最多添加 {self.MAX_ADDS_PER_HOUR} 个域名。\n"
                    "当前操作未修改规则，请在下一小时再试。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                # 保持添加模式，便于继续输入域名
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 检查是否为.cn域名
            normalized_input = normalize_domain(domain_input)
            if normalized_input and is_cn_domain(normalized_input):
                keyboard = [
                    [InlineKeyboardButton("🔍 查询其他域名", callback_data="query_domain")],
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(
                    "ℹ️ *.cn 域名已默认直连*\n\n"
                    "🧾 *已检查域名*\n"
                    f"`{normalized_input}`\n\n"
                    "🇨🇳 `.cn` 域名已由现有策略默认直连，\n"
                    "无需再次写入公开规则库。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                # 保持添加模式，便于继续输入域名
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 提取二级域名用于添加规则
            domain = extract_second_level_domain_for_rules(domain_input)
            if not domain:
                if normalized_input and is_cn_domain(normalized_input):
                    keyboard = [
                        [InlineKeyboardButton("🔍 查询其他域名", callback_data="query_domain")],
                        [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                        [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await processing_msg.edit_text(
                        "ℹ️ *.cn 域名已默认直连*\n\n"
                        "🧾 *已检查域名*\n"
                        f"`{normalized_input}`\n\n"
                        "🇨🇳 `.cn` 域名已由现有策略默认直连，\n"
                        "无需再次写入公开规则库。",
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                else:
                    await processing_msg.edit_text(
                        "⚠️ *无法识别域名*\n\n"
                        "📝 请发送有效域名或包含域名的 URL，\n"
                        "并检查输入中是否存在多余字符。",
                        reply_markup=self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新输入"
                        ),
                        parse_mode="Markdown",
                    )
                # 保持添加模式，便于继续输入域名
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 显示提取的可注册域名信息
            if domain != normalize_domain(domain_input):
                await processing_msg.edit_text(
                    "🔍 *已提取可注册域名*\n\n"
                    "🧾 *后续检查对象*\n"
                    f"`{domain}`\n\n"
                    "系统正在继续核对规则库与网络归属信息…",
                    parse_mode="Markdown",
                )
            
            # 1. 防重复检查
            await processing_msg.edit_text("🔍 正在检查已有规则…")
            
            # 检查 GitHub 规则
            github_result = await self.github_service.check_domain_in_rules(domain)
            if github_result.get("error"):
                await processing_msg.edit_text(
                    "❌ *暂时无法读取 GitHub 规则*\n\n"
                    "🛡️ 本次操作未提交或修改任何规则。\n"
                    "请稍后重试。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return
            second_level = extract_second_level_domain(domain)
            
            if github_result.get("exists"):
                result_text = "ℹ️ *规则已存在，无需添加*\n\n"
                result_text += "🧾 *已检查域名*\n"
                result_text += f"`{domain}`\n\n"
                result_text += "📚 *匹配规则*\n"
                result_text += self._format_rule_matches(github_result.get("matches", []))
                
                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 检查可注册域名规则
            if second_level and second_level != domain:
                second_level_result = await self.github_service.check_domain_in_rules(second_level)
                if second_level_result.get("error"):
                    await processing_msg.edit_text(
                        "❌ *暂时无法读取 GitHub 规则*\n\n"
                        "🛡️ 本次操作未提交或修改任何规则。\n"
                        "请稍后重试。",
                        reply_markup=self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新检查"
                        ),
                        parse_mode="Markdown",
                    )
                    return
                if second_level_result.get("exists"):
                    result_text = "ℹ️ *可注册域名已在规则中*\n\n"
                    result_text += f"🔎 输入域名：`{domain}`\n"
                    result_text += f"🧾 可注册域名：`{second_level}`\n\n"
                    result_text += "📚 *匹配规则*\n"
                    result_text += self._format_rule_matches(
                        second_level_result.get("matches", [])
                    )
                    
                    keyboard = [
                        [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                        [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                    self.set_user_state(user_id, "waiting_add_domain")
                    return
            
            # 检查GeoSite
            in_geosite = await self.data_manager.is_domain_in_geosite(domain)
            if in_geosite:
                result_text = "ℹ️ *GEOSITE:CN 已覆盖*\n\n"
                result_text += "🧾 *已检查域名*\n"
                result_text += f"`{domain}`\n\n"
                result_text += "🇨🇳 现有分类已覆盖，无需重复添加。"
                
                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 2. 进行域名检查
            await processing_msg.edit_text("🔍 正在检查规则与网络信息…")
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            
            if "error" in check_result:
                await processing_msg.edit_text(
                    "❌ *域名检查失败*\n\n"
                    "🛡️ 本次操作未提交或修改任何规则。\n"
                    "请稍后重试。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return
            
            # 保存检查结果到用户状态
            self.set_user_state(user_id, "domain_checked", {
                "domain": domain,
                "check_result": check_result
            })
            
            result_text, target_domain = self._build_add_review_text(
                domain, check_result, update.effective_user
            )
            
            # 根据检查结果决定下一步
            keyboard = []
            
            should_reject = self.domain_checker.should_reject(check_result)
            if self.domain_checker.should_add_directly(check_result):
                # 符合条件，提供添加选项
                token = self.create_pending_action(
                    user_id,
                    "confirm_add",
                    domain=domain,
                    target_domain=target_domain,
                    check_result=check_result,
                )
                keyboard.append([InlineKeyboardButton("✅ 确认公开提交", callback_data=f"confirm_add|yes|{token}")])
                keyboard.append([InlineKeyboardButton("↩️ 取消", callback_data=f"confirm_add|no|{token}")])
            elif should_reject:
                # 不符合条件，拒绝添加
                if self.is_admin(user_id):
                    result_text += (
                        "\n\n🛡️ *管理员操作*\n"
                        "可跳过系统判断并强制公开添加。"
                    )
                    keyboard.append([
                        InlineKeyboardButton(
                            "🛡️ 管理员权限添加",
                            callback_data=self.get_admin_force_add_callback(user_id, domain)
                        )
                    ])
                keyboard.append([InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")])
            else:
                # 默认情况（理论上不会到这里）
                token = self.create_pending_action(
                    user_id,
                    "confirm_add",
                    domain=domain,
                    target_domain=target_domain,
                    check_result=check_result,
                )
                keyboard.append([InlineKeyboardButton("✅ 确认公开提交", callback_data=f"confirm_add|yes|{token}")])
                keyboard.append([InlineKeyboardButton("↩️ 取消", callback_data=f"confirm_add|no|{token}")])

            keyboard.append([InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')

            if should_reject:
                self.set_user_state(user_id, "waiting_add_domain")
            
        except Exception as e:
            logger.error(f"添加域名输入处理失败: {e}")
            text = (
                "❌ *暂时无法完成检查*\n\n"
                "🛡️ 本次操作未提交或修改任何规则。\n"
                "请稍后重试。"
            )
            markup = self._recovery_keyboard("add_direct_rule", "➕ 重新检查")
            if processing_msg is not None:
                await processing_msg.edit_text(
                    text, reply_markup=markup, parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=markup, parse_mode="Markdown"
                )
    
    async def _handle_add_domain_callback(self, query, user_id: int, data: str):
        """处理添加域名回调"""
        try:
            token = data.split("|", 1)[1] if "|" in data else ""
            action = self.get_pending_action(user_id, token, "add_domain", consume=True)
            if not action:
                await query.edit_message_text(
                    "⌛ *操作已过期*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。\n"
                    "请从下方入口重新开始。",
                    reply_markup=self._recovery_keyboard(
                        "query_domain", "🔍 重新查询"
                    ),
                    parse_mode="Markdown",
                )
                return
            self._discard_pending_actions(user_id, {"query_result_pages"})
            domain = action.get("domain", "")
            
            # 进行域名检查
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            
            if "error" in check_result:
                await query.edit_message_text(
                    "❌ *域名检查失败*\n\n"
                    "🛡️ 本次操作未提交或修改任何规则。\n"
                    "请稍后重试。",
                    reply_markup=self._recovery_keyboard(
                        "query_domain", "🔍 重新查询"
                    ),
                    parse_mode="Markdown",
                )
                return
            
            # 保存检查结果
            self.set_user_state(user_id, "domain_checked", {
                "domain": domain,
                "check_result": check_result
            })
            
            result_text, target_domain = self._build_add_review_text(
                domain, check_result, query.from_user
            )
            
            # 根据检查结果决定下一步
            keyboard = []
            
            should_reject = self.domain_checker.should_reject(check_result)
            if not should_reject:
                confirm_token = self.create_pending_action(
                    user_id,
                    "confirm_add",
                    domain=domain,
                    target_domain=target_domain,
                    check_result=check_result,
                )
                keyboard.append([InlineKeyboardButton("✅ 确认公开提交", callback_data=f"confirm_add|yes|{confirm_token}")])
                keyboard.append([InlineKeyboardButton("↩️ 取消", callback_data=f"confirm_add|no|{confirm_token}")])
            else:
                if self.is_admin(user_id):
                    result_text += (
                        "\n\n🛡️ *管理员操作*\n"
                        "可跳过系统判断并强制公开添加。"
                    )
                    keyboard.append([
                        InlineKeyboardButton(
                            "🛡️ 管理员权限添加",
                            callback_data=self.get_admin_force_add_callback(user_id, domain)
                        )
                    ])
                keyboard.append([
                    InlineKeyboardButton(
                        "➕ 添加其他域名", callback_data="add_direct_rule"
                    )
                ])

            keyboard.append([InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')

            if should_reject:
                self.set_user_state(user_id, "waiting_add_domain")
            
        except Exception as e:
            logger.error(f"处理添加域名回调失败: {e}")
            await query.edit_message_text(
                "❌ *暂时无法完成检查*\n\n"
                "🛡️ 本次操作未提交或修改任何规则。",
                reply_markup=self._recovery_keyboard(
                    "query_domain", "🔍 重新查询"
                ),
                parse_mode="Markdown",
            )

    async def _handle_admin_force_add_callback(self, query, user_id: int, data: str):
        """处理管理员权限强制添加回调"""
        write_attempted = False
        target_domain = ""
        add_result = None
        result_text = ""
        chat = getattr(getattr(query, "message", None), "chat", None)
        is_private_chat = getattr(chat, "type", None) == "private"

        def page_markup(markup):
            return markup if is_private_chat else None

        def group_next(text: str, *, retry: bool = False) -> str:
            if is_private_chat:
                return text
            instruction = (
                "请稍后重新 @机器人并附带域名。"
                if retry
                else "继续处理请重新 @机器人并附带域名。"
            )
            return f"{text}\n\n{instruction}"

        async def answer_group(
            text: str = "", *, show_alert: bool = False
        ) -> bool:
            try:
                if text:
                    await query.answer(text, show_alert=show_alert)
                else:
                    await query.answer()
                return True
            except Exception as answer_error:
                logger.warning(f"群聊管理员按钮应答失败: {answer_error}")
                return False

        try:
            if not self.is_admin(user_id):
                logger.warning(
                    "管理员权限操作被拒绝: user_ref={}, action={}",
                    log_reference(str(user_id)),
                    data.partition("|")[0],
                )
                if not is_private_chat:
                    await answer_group(
                        "此按钮仅供发起操作的管理员使用。",
                        show_alert=True,
                    )
                    return
                await query.edit_message_text(
                    "⛔ *没有管理员权限*\n\n"
                    "当前账号不能执行强制添加。",
                    reply_markup=page_markup(self._recovery_keyboard()),
                    parse_mode="Markdown",
                )
                return

            action_name = data.split("|", 1)[0]
            token = data.split("|", 1)[1] if "|" in data else ""
            is_decision = action_name in {
                "admin_force_add_confirm",
                "admin_force_add_cancel",
            }
            expected_action = (
                "admin_force_add_decision"
                if is_decision
                else "admin_force_add"
            )
            if not is_private_chat:
                action = self.get_pending_action(
                    user_id,
                    token,
                    expected_action,
                    consume=False,
                )
                if not action:
                    await answer_group(
                        "按钮已失效或不属于当前账号，请重新 @机器人。",
                        show_alert=True,
                    )
                    return
                if not await answer_group():
                    return
            action = self.get_pending_action(
                user_id,
                token,
                expected_action,
                consume=True,
            )
            if not action:
                if not is_private_chat:
                    return
                await query.edit_message_text(
                    group_next(
                        "⌛ *操作已过期*\n\n"
                        "🛡️ 本次操作尚未提交或修改任何规则。\n"
                        "请从下方入口重新开始。"
                    ),
                    reply_markup=page_markup(
                        self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新检查"
                        )
                    ),
                    parse_mode="Markdown",
                )
                return
            domain = action.get("domain", "")

            domain = extract_second_level_domain_for_rules(domain)
            if not domain:
                await query.edit_message_text(
                    group_next(
                        "⚠️ *域名格式无效*\n\n"
                        "📝 未能提取有效的可注册域名。\n"
                        "🛡️ 本次操作尚未提交或修改任何规则。"
                    ),
                    reply_markup=page_markup(
                        self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新开始"
                        )
                    ),
                    parse_mode="Markdown",
                )
                return

            if action_name == "admin_force_add_cancel":
                await self._display_callback_result(
                    query,
                    group_next(
                        "↩️ *已取消管理员权限添加*\n\n"
                        "🛡️ 本次操作未提交或修改任何规则。"
                    ),
                    page_markup(self._recovery_keyboard()),
                )
                return

            if action_name == "admin_force_add":
                decision_token = self.create_pending_action(
                    user_id,
                    "admin_force_add_decision",
                    domain=domain,
                )
                confirmation_markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⚠️ 确认立即公开提交",
                                callback_data=(
                                    "admin_force_add_confirm|"
                                    f"{decision_token}"
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "↩️ 取消",
                                callback_data=(
                                    "admin_force_add_cancel|"
                                    f"{decision_token}"
                                ),
                            )
                        ],
                    ]
                )
                await query.edit_message_text(
                    "🛡️ *确认管理员权限添加*\n\n"
                    "🧾 *拟提交规则*\n"
                    f"`DOMAIN-SUFFIX,{domain}`\n\n"
                    "⚠️ 此操作将跳过系统判断，并立即公开写入 GitHub。\n\n"
                    "➖ 当前删除功能尚未开放，请确认规则准确无误。",
                    reply_markup=confirmation_markup,
                    parse_mode="Markdown",
                )
                return

            if is_cn_domain(domain):
                await query.edit_message_text(
                    group_next(
                        "ℹ️ *.cn 域名已默认直连*\n\n"
                        "🧾 *已检查域名*\n"
                        f"`{domain}`\n\n"
                        "🇨🇳 `.cn` 域名已由现有策略默认直连，\n"
                        "无需再次写入公开规则库。"
                    ),
                    reply_markup=page_markup(
                        self._recovery_keyboard(
                            "add_direct_rule", "➕ 添加其他域名"
                        )
                    ),
                    parse_mode='Markdown'
                )
                return

            # 检查用户添加频率限制
            can_add, _ = self.check_user_add_limit(user_id)
            if not can_add:
                keyboard = [
                    [InlineKeyboardButton("🔍 查询域名", callback_data="query_domain")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    "⚠️ *本小时添加次数已用完*\n\n"
                    f"⏱️ 每个账号每小时最多添加 {self.MAX_ADDS_PER_HOUR} 个域名。\n"
                    "当前操作未修改规则，请在下一小时再试。",
                    reply_markup=page_markup(reply_markup),
                    parse_mode='Markdown'
                )
                return

            # 防重复检查
            github_result = await self.github_service.check_domain_in_rules(domain)
            if github_result.get("error"):
                await query.edit_message_text(
                    group_next(
                        "❌ *暂时无法读取 GitHub 规则*\n\n"
                        "🛡️ 本次操作未提交或修改任何规则。\n"
                        "请稍后重试。",
                        retry=True,
                    ),
                    reply_markup=page_markup(
                        self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新检查"
                        )
                    ),
                    parse_mode="Markdown",
                )
                return
            if github_result.get("exists"):
                result_text = "ℹ️ *规则已存在，无需添加*\n\n"
                result_text += "🧾 *已检查域名*\n"
                result_text += f"`{domain}`\n\n"
                result_text += "📚 *匹配规则*\n"
                result_text += self._format_rule_matches(
                    github_result.get("matches", [])
                )

                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    group_next(result_text),
                    reply_markup=page_markup(reply_markup),
                    parse_mode='Markdown',
                )
                return

            in_geosite = await self.data_manager.is_domain_in_geosite(domain)
            if in_geosite:
                result_text = "ℹ️ *GEOSITE:CN 已覆盖*\n\n"
                result_text += "🧾 *已检查域名*\n"
                result_text += f"`{domain}`\n\n"
                result_text += "🇨🇳 现有分类已覆盖，无需重复添加。"

                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    group_next(result_text),
                    reply_markup=page_markup(reply_markup),
                    parse_mode='Markdown',
                )
                return

            await query.edit_message_text("⏳ 正在执行管理员强制添加…")
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            if "error" in check_result:
                await query.edit_message_text(
                    group_next(
                        "❌ *域名检查失败*\n\n"
                        "🛡️ 本次操作未提交或修改任何规则。\n"
                        "请稍后重试。",
                        retry=True,
                    ),
                    reply_markup=page_markup(
                        self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新检查"
                        )
                    ),
                    parse_mode="Markdown",
                )
                return

            target_domain = self.domain_checker.get_target_domain_to_add(check_result) or domain
            username = self._raw_telegram_identity(query.from_user)

            write_attempted = True
            add_result = await self._add_domain_with_limit(
                user_id,
                target_domain,
                username,
                "",
                force_add=True
            )

            if add_result.get("success"):
                remaining = add_result["rate_limit_remaining"]
                result_text = self._build_add_success_text(
                    target_domain,
                    query.from_user,
                    add_result,
                    remaining,
                    admin=True,
                )
            else:
                result_text = self._build_add_failure_text(
                    target_domain, add_result
                )

            reply_markup = self._build_add_result_keyboard(add_result)

            await self._display_callback_result(
                query,
                group_next(result_text, retry=not add_result.get("success")),
                page_markup(reply_markup),
            )
            if add_result.get("success"):
                await self._announce_private_addition(
                    getattr(query.message, "chat", None),
                    target_domain,
                    add_result,
                    username,
                )
            if is_private_chat:
                next_state = (
                    "waiting_query_domain"
                    if add_result.get("submission_uncertain")
                    else "waiting_add_domain"
                )
                self.set_user_state(user_id, next_state)

        except Exception as e:
            logger.error(f"处理管理员权限添加失败: {e}")
            confirmed_success = bool(add_result and add_result.get("success"))
            if confirmed_success:
                text = result_text or self._build_confirmed_add_fallback_text(
                    target_domain, add_result
                )
                recovery_markup = self._build_add_result_keyboard(add_result)
            else:
                text = (
                    self._build_submission_uncertain_text(target_domain)
                    if write_attempted
                    else (
                        "❌ *管理员权限添加失败*\n\n"
                        "🛡️ 本次操作未修改任何规则。"
                    )
                )
                recovery_markup = self._recovery_keyboard(
                    "query_domain" if write_attempted else "add_direct_rule",
                    "🔍 前往查询" if write_attempted else "➕ 重新检查",
                )
            if is_private_chat:
                self.set_user_state(
                    user_id,
                    "waiting_add_domain"
                    if confirmed_success
                    else "waiting_query_domain"
                    if write_attempted
                    else "waiting_add_domain",
                )
            await self._display_callback_result(
                query,
                group_next(text, retry=not confirmed_success),
                page_markup(recovery_markup),
            )
    
    async def _handle_confirm_add_callback(self, query, user_id: int, data: str):
        """处理确认添加回调"""
        try:
            parts = data.split("|", 2)
            decision = parts[1] if len(parts) == 3 else ""
            token = parts[2] if len(parts) == 3 else ""
            domain_data = self.get_pending_action(
                user_id,
                token,
                "confirm_add",
                consume=True,
            )
            if not domain_data:
                await query.edit_message_text(
                    "⌛ *确认已过期*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。\n"
                    "请从下方入口重新开始。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return

            if decision == "no":
                # 取消添加
                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回首页", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    "ℹ️ *已取消添加*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            if decision != "yes":
                await query.edit_message_text(
                    "⚠️ *确认操作无效*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return

            # 确认添加
            domain = domain_data.get("domain")
            if not domain:
                await query.edit_message_text(
                    "⌛ *域名数据已失效*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
                    parse_mode="Markdown",
                )
                return

            target_domain = domain_data.get("target_domain") or self._target_domain(
                domain, domain_data.get("check_result", {})
            )
            
            # 询问说明
            self.set_user_state(user_id, "waiting_description", domain_data)
            
            keyboard = [
                [InlineKeyboardButton("⏭️ 不填说明，直接提交", callback_data="skip_description")],
                [InlineKeyboardButton("↩️ 取消", callback_data="main_menu")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                self._build_description_prompt_text(target_domain, query.from_user),
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"处理确认添加回调失败: {e}")
            await query.edit_message_text(
                "❌ 操作失败，尚未确认提交。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新检查"
                ),
            )
    
    async def _handle_skip_description(self, query, user_id: int):
        """处理跳过说明"""
        await self._add_domain_to_github(query, user_id, "")
    
    async def _handle_description_input(self, update: Update, description: str, user_id: int):
        """处理说明输入"""
        try:
            # 验证说明内容
            is_valid, processed_description = self.validate_description(description)
            
            if not is_valid:
                keyboard = [
                    [InlineKeyboardButton("⏭️ 不填说明，直接提交", callback_data="skip_description")],
                    [InlineKeyboardButton("↩️ 取消", callback_data="main_menu")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    "⚠️ *说明不符合要求*\n\n"
                    "✍️ *填写要求*\n"
                    f"最多 {self.MAX_DESCRIPTION_LENGTH} 个字符，且只能有一行。\n"
                    f"📏 当前输入：{len(description)} 个字符。\n\n"
                    "请重新输入符合要求的说明，或直接选择跳过。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return
            
            await self._add_domain_to_github_message(update.message, user_id, processed_description)
            
        except Exception as e:
            logger.error(f"处理说明输入失败: {e}")
            await update.message.reply_text(
                "❌ *暂时无法提交说明*\n\n"
                "🛡️ 本次操作未修改任何规则。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新开始"
                ),
                parse_mode="Markdown",
            )
    
    async def _add_domain_to_github(self, query, user_id: int, description: str):
        """添加域名到 GitHub"""
        write_attempted = False
        target_domain = ""
        add_result = None
        result_text = ""
        try:
            user_state = self.get_user_state(user_id)
            domain_data = user_state.get("data", {})
            
            domain = domain_data.get("domain")
            check_result = domain_data.get("check_result")
            
            if not domain or not check_result:
                await query.edit_message_text(
                    "⌛ *操作数据已失效*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
                    parse_mode="Markdown",
                )
                return
            
            # 获取要添加的目标域名
            logger.debug(
                "准备获取目标域名，domain_ref={}，检查详情不写入日志",
                log_reference(domain),
            )
            target_domain = self.domain_checker.get_target_domain_to_add(check_result)
            if not target_domain:
                target_domain = domain
                logger.warning(
                    "无法获取目标域名，使用原始域名，domain_ref={}",
                    log_reference(domain),
                )
            
            # 获取用户名
            username = self._raw_telegram_identity(query.from_user)
            
            logger.debug(
                "已确定最终目标，domain_ref={}，用户信息和说明不写入运行日志",
                log_reference(target_domain),
            )
            
            # 显示添加中消息
            await query.edit_message_text("⏳ 正在公开提交至 GitHub…")
            
            # 添加到 GitHub
            write_attempted = True
            add_result = await self._add_domain_with_limit(
                user_id, target_domain, username, description
            )
            
            if add_result.get("success"):
                remaining = add_result["rate_limit_remaining"]
                result_text = self._build_add_success_text(
                    target_domain,
                    query.from_user,
                    add_result,
                    remaining,
                    description,
                )
            else:
                result_text = self._build_add_failure_text(
                    target_domain, add_result
                )
            
            reply_markup = self._build_add_result_keyboard(add_result)

            await self._display_callback_result(
                query,
                result_text,
                reply_markup,
            )
            if add_result.get("success"):
                await self._announce_private_addition(
                    getattr(query.message, "chat", None),
                    target_domain,
                    add_result,
                    username,
                )
            
            # 保持添加模式，便于继续输入域名
            next_state = (
                "waiting_query_domain"
                if add_result.get("submission_uncertain")
                else "waiting_add_domain"
            )
            self.set_user_state(user_id, next_state)
            
        except Exception as e:
            logger.error(f"添加域名到 GitHub 失败: {e}")
            confirmed_success = bool(add_result and add_result.get("success"))
            if confirmed_success:
                text = result_text or self._build_confirmed_add_fallback_text(
                    target_domain, add_result
                )
                markup = self._build_add_result_keyboard(add_result)
                self.set_user_state(user_id, "waiting_add_domain")
            else:
                text = (
                    self._build_submission_uncertain_text(target_domain)
                    if write_attempted
                    else (
                        "❌ *直连规则添加失败*\n\n"
                        "🛡️ 本次操作未修改任何规则。\n"
                        "请稍后重试。"
                    )
                )
                markup = self._recovery_keyboard(
                    "query_domain" if write_attempted else "add_direct_rule",
                    "🔍 前往查询" if write_attempted else "➕ 重新开始",
                )
                if write_attempted:
                    self.set_user_state(user_id, "waiting_query_domain")
            await self._display_callback_result(
                query,
                text,
                markup,
            )
    
    async def _add_domain_to_github_message(self, message, user_id: int, description: str):
        """通过消息添加域名到 GitHub"""
        processing_msg = None
        write_attempted = False
        target_domain = ""
        add_result = None
        result_text = ""
        try:
            user_state = self.get_user_state(user_id)
            domain_data = user_state.get("data", {})
            
            domain = domain_data.get("domain")
            check_result = domain_data.get("check_result")
            
            if not domain or not check_result:
                await message.reply_text(
                    "⌛ *操作数据已失效*\n\n"
                    "🛡️ 本次操作尚未提交或修改任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
                    parse_mode="Markdown",
                )
                return
            
            # 获取要添加的目标域名
            target_domain = self.domain_checker.get_target_domain_to_add(check_result)
            if not target_domain:
                target_domain = domain
            
            # 显示添加中消息
            processing_msg = await message.reply_text("⏳ 正在公开提交至 GitHub…")
            
            # 添加到 GitHub
            username = self._raw_telegram_identity(message.from_user)
            write_attempted = True
            add_result = await self._add_domain_with_limit(
                user_id, target_domain, username, description
            )
            
            if add_result.get("success"):
                remaining = add_result["rate_limit_remaining"]
                result_text = self._build_add_success_text(
                    target_domain,
                    message.from_user,
                    add_result,
                    remaining,
                    description,
                )
            else:
                result_text = self._build_add_failure_text(
                    target_domain, add_result
                )
            
            reply_markup = self._build_add_result_keyboard(add_result)

            await self._display_message_result(
                message,
                processing_msg,
                result_text,
                reply_markup,
            )
            if add_result.get("success"):
                await self._announce_private_addition(
                    getattr(message, "chat", None),
                    target_domain,
                    add_result,
                    username,
                )
            
            # 保持添加模式，便于继续输入域名
            next_state = (
                "waiting_query_domain"
                if add_result.get("submission_uncertain")
                else "waiting_add_domain"
            )
            self.set_user_state(user_id, next_state)
            
        except Exception as e:
            logger.error(f"添加域名到 GitHub 失败: {e}")
            confirmed_success = bool(add_result and add_result.get("success"))
            if confirmed_success:
                text = result_text or self._build_confirmed_add_fallback_text(
                    target_domain, add_result
                )
                markup = self._build_add_result_keyboard(add_result)
                self.set_user_state(user_id, "waiting_add_domain")
            else:
                text = (
                    self._build_submission_uncertain_text(target_domain)
                    if write_attempted
                    else (
                        "❌ *直连规则添加失败*\n\n"
                        "🛡️ 本次操作未修改任何规则。\n"
                        "请稍后重试。"
                    )
                )
                markup = self._recovery_keyboard(
                    "query_domain" if write_attempted else "add_direct_rule",
                    "🔍 前往查询" if write_attempted else "➕ 重新开始",
                )
                if write_attempted:
                    self.set_user_state(user_id, "waiting_query_domain")
            await self._display_message_result(
                message,
                processing_msg,
                text,
                markup,
            )

 
