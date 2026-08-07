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

from ..config import Config
from ..data_manager import DataManager
from ..services.dns_service import DNSService
from ..services.geoip_service import GeoIPService
from ..services.github_service import GitHubService
from ..services.domain_checker import DomainChecker
from ..services.group_service import GroupService
from ..services.matchscope_token_service import MatchScopeTokenService
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
        self.MAX_DETAIL_LINES = 6
        self.MAX_DETAIL_LINE_LENGTH = 120
        self.STATE_TTL = 1800
        self.ACTION_TTL = 900
        self.MAX_USER_STATES = 4096

        self.data_manager.register_update_callback(self._handle_data_update)
        
        # 群组服务（需要 bot 实例）
        self.group_service = None
        if application:
            self.group_service = GroupService(config, application.bot)
        self.matchscope_token_service = None
        if config.MATCHSCOPE_PUBLIC_API_ENABLED:
            self.matchscope_token_service = MatchScopeTokenService(
                config.MATCHSCOPE_TOKEN_DATABASE
                or (data_manager.data_dir / "matchscope_tokens.sqlite3"),
                config.MATCHSCOPE_TOKEN_SIGNING_KEY,
                config.MATCHSCOPE_TOKEN_TTL_DAYS,
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
                    "message": github_result["error"],
                }
            if github_result.get("exists"):
                matches = github_result.get("matches", [])
                match_info = f"第{matches[0]['line']}行" if matches else ""
                return {
                    "success": True,
                    "action": "exists",
                    "reason": "rules",
                    "message": f"域名已存在于 GitHub 规则中（{match_info}）"
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
                    "message": f"域名检查失败：{check_result['error']}"
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
                return {
                    "success": False,
                    "action": "error",
                    "message": add_result.get("error", "添加失败，未知错误"),
                    "rate_limited": add_result.get("rate_limited", False),
                }
                
        except Exception as e:
            logger.error(f"自动检查并添加域名失败: {e}")
            return {
                "success": False,
                "action": "error",
                "message": f"处理异常：{str(e)}"
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

    async def submit_matchscope_domain(
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
            "MatchScope Community" if source == "matchscope_community" else "MatchScope",
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
                self._rollback_user_add(user_id, reservation)
                return add_result
        except BaseException:
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
        identity = self.escape_markdown(self._raw_telegram_identity(user))
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
            [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
        )
        return InlineKeyboardMarkup(keyboard)

    def _format_rule_matches(self, matches: list) -> str:
        """Bound dynamic GitHub matches so Telegram messages remain below limits."""
        if not matches:
            return ""
        limit = getattr(self, "MAX_DETAIL_LINES", 6)
        line_limit = getattr(self, "MAX_DETAIL_LINE_LENGTH", 120)
        lines = []
        for match in matches[:limit]:
            rule = str(match.get("rule", ""))
            if len(rule) > line_limit:
                rule = rule[: line_limit - 3] + "..."
            lines.append(
                f"   • 第{match.get('line', '?')}行：{self.escape_markdown(rule)}"
            )
        remaining = len(matches) - limit
        if remaining > 0:
            lines.append(f"   • 另有 {remaining} 条匹配未显示")
        return "\n".join(lines)

    @staticmethod
    def _format_value_list(values: list, limit: int = 6) -> str:
        visible = [str(value)[:80] for value in list(values or [])[:limit]]
        remaining = len(values or []) - len(visible)
        if remaining > 0:
            visible.append(f"另有 {remaining} 项")
        return ", ".join(visible)

    def _target_domain(self, domain: str, check_result: dict) -> str:
        return self.domain_checker.get_target_domain_to_add(check_result) or domain

    def _submission_notice(self, user) -> str:
        identity = self._format_telegram_identity(user)
        text = (
            "⚠️ *公开范围：* 提交后，最终规则、提交时间、可选说明和提交者"
            f" {identity} 会进入公开 GitHub 历史。"
        )
        if getattr(self.config, "ANNOUNCEMENT_GROUP_ID", None):
            text += " 当前已启用群组播报，域名、结果、提交链接和提交者也会发送到群组。"
        return text

    def _build_add_review_text(self, domain: str, check_result: dict, user) -> tuple[str, str]:
        """Build a conclusion-first review that names the exact rule to be written."""
        target_domain = self._target_domain(domain, check_result)
        should_reject = self.domain_checker.should_reject(check_result)
        conclusion = (
            "⛔ *结论：不符合直连规则添加条件*"
            if should_reject
            else "✅ *结论：可以添加到直连规则*"
        )
        lines = [
            conclusion,
            "",
            f"🧭 *最终写入：* `DOMAIN-SUFFIX,{target_domain}`",
        ]
        if target_domain != domain:
            lines.append(f"📥 *输入域名：* `{domain}`")
        recommendation = str(check_result.get("recommendation", "")).strip()
        if recommendation:
            lines.extend(["", f"💡 *判断依据：* {self.escape_markdown(recommendation[:300])}"])
        detail_lines = self._format_detail_lines(check_result.get("details", []))
        if detail_lines:
            lines.extend(["", "📌 *检查详情：*", detail_lines])
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
            f"🧭 `DOMAIN-SUFFIX,{target_domain}`",
            f"👤 *提交者：* {self._format_telegram_identity(user)}",
        ]
        if description:
            lines.append(f"📝 *说明：* {self.escape_markdown(description)}")
        commit_url = add_result.get("commit_url", "")
        short_sha = str(add_result.get("commit_sha", ""))[:8]
        if commit_url:
            link_label = f"查看 GitHub 提交 {short_sha}" if short_sha else "查看 GitHub 提交"
            lines.append(f"🔗 [{link_label}]({commit_url})")
        lines.extend(["", f"本小时还可添加 {remaining} 个域名。"])
        return "\n".join(lines)

    def _build_main_menu_text(self, username: str) -> str:
        """构建主菜单文案"""
        username = self.escape_markdown(username)
        capabilities = [
            "• 查询现有规则、GEOSITE:CN、IP 与 NS 状态",
            "• 检查并提交 `DOMAIN-SUFFIX` 直连规则",
        ]
        if self.config.MATCHSCOPE_PUBLIC_API_ENABLED:
            capabilities.append("• 管理 MatchScope 社区接入与个人 Token")
        return "\n".join(
            [
                f"👋 *欢迎使用 Rule-Bot，{username}！*",
                "",
                "🧭 *Custom OpenClash Rules 规则助手*",
                *capabilities,
                "",
                "提交前会展示最终规则并再次确认；通过的规则会公开写入 GitHub。",
                "🚧 删除规则是为后续版本保留的入口，目前尚未开放。",
                "",
                f"📂 *公开仓库：* `{self.config.GITHUB_REPO}`",
                "",
                "请选择操作：",
            ]
        )

    def _build_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """构建主菜单键盘"""
        keyboard = [
            [
                InlineKeyboardButton("🔍 查询域名", callback_data="query_domain"),
                InlineKeyboardButton("➕ 添加直连规则", callback_data="add_direct_rule"),
            ],
        ]
        if self.config.MATCHSCOPE_PUBLIC_API_ENABLED:
            keyboard.append(
                [
                    InlineKeyboardButton("🔗 MatchScope 接入", callback_data="matchscope_access"),
                    InlineKeyboardButton("ℹ️ 帮助信息", callback_data="help"),
                ]
            )
        else:
            keyboard.append([InlineKeyboardButton("ℹ️ 帮助信息", callback_data="help")])
        keyboard.append(
            [
                InlineKeyboardButton(
                    "➖ 删除规则 · 暂未开放", callback_data="delete_rule"
                )
            ]
        )
        return InlineKeyboardMarkup(keyboard)

    def _build_delete_unavailable_text(self) -> str:
        """Build the visible placeholder for the planned rule-deletion workflow."""
        return (
            "➖ *删除规则*\n\n"
            "🚧 *这是为后续版本保留的入口，当前尚未开放。*\n"
            "点击本页不会删除或修改任何规则。\n\n"
            f"📂 *目标仓库：* `{self.config.GITHUB_REPO}`\n"
            f"📄 *直连规则：* `{self.config.DIRECT_RULE_FILE}`\n"
            f"📄 *代理规则：* `{self.config.PROXY_RULE_FILE}`"
        )

    def _build_help_text(self) -> str:
        """构建帮助文案"""
        lines = [
            "📖 *Rule-Bot 使用说明*",
            "",
            "*私聊命令*",
            "• `/query` 查询规则、GEOSITE:CN、IP 与 NS 状态",
            "• `/add` 检查并确认提交直连规则",
            "• `/id` 查看自己的 Telegram 用户 ID",
            "• `/help` 查看本页；填写说明时可用 `/skip` 跳过",
        ]
        if getattr(self.config, "ALLOWED_GROUP_IDS", None):
            lines.extend(
                [
                    "",
                    "*群聊*",
                    "在允许的群组中 @机器人并附带域名；检查通过后会自动写入公开 GitHub。",
                ]
            )
        else:
            lines.extend(["", "*群聊*", "群聊入口仅在管理员配置允许的群组中生效。"])
        if getattr(self.config, "MATCHSCOPE_PUBLIC_API_ENABLED", False):
            lines.extend(
                [
                    "",
                    "*MatchScope*",
                    "从主菜单阅读隐私说明、确认同意并申请个人 Token。",
                ]
            )
        lines.extend(
            [
                "",
                "*预留功能*",
                "主菜单会持续保留删除规则入口；当前仅展示状态，不会修改仓库。",
                "",
                "添加前会显示最终 `DOMAIN-SUFFIX` 规则和公开范围；域名判断基于当前规则、DoH、GeoIP 与 NS 数据。",
                f"📂 *仓库：* `{self.config.GITHUB_REPO}`",
            ]
        )
        return "\n".join(lines)

    def _build_help_keyboard(self) -> InlineKeyboardMarkup:
        """构建帮助键盘"""
        keyboard = [[InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]]
        return InlineKeyboardMarkup(keyboard)

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
            stats_text = f"📊 *当前统计：* 直连 {direct_text} · GEOSITE:CN {geosite_count:,}\n\n"

            if include_limit and user_id is not None:
                can_add, remaining = self.check_user_add_limit(user_id)
                if can_add:
                    stats_text += f"⏳ *添加限制：* 本小时内还可添加 {remaining} 个域名\n\n"
                else:
                    stats_text += "⛔ *添加限制：* 本小时内已达到添加上限，请稍后再试\n\n"

            return stats_text
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return "⚠️ *统计信息暂时无法获取，不影响输入域名。*\n\n"

    def _format_detail_lines(self, details: list) -> str:
        """格式化检查详情"""
        if not details:
            return ""

        lines = []
        for detail in details[:self.MAX_DETAIL_LINES]:
            detail = str(detail)
            if len(detail) > self.MAX_DETAIL_LINE_LENGTH:
                detail = detail[:self.MAX_DETAIL_LINE_LENGTH - 3] + "..."
            lines.append(f"   • {self.escape_markdown(detail)}")

        remaining = len(details) - self.MAX_DETAIL_LINES
        if remaining > 0:
            lines.append(f"   • 还有 {remaining} 条")

        return "\n".join(lines)

    def _build_query_prompt(self, stats_text: str) -> str:
        """构建查询提示文案"""
        return (
            "🔍 *域名查询*\n\n"
            f"{stats_text}"
            "请发送一个域名或包含域名的 URL。\n"
            "例如：`example.com`、`sub.example.com`、`https://example.com/path`\n\n"
            "若继续添加，提交前会明确显示最终可注册域名（主域名）规则。"
        )

    def _build_add_prompt(self, stats_text: str) -> str:
        """构建添加提示文案"""
        return (
            "➕ *添加直连规则*\n\n"
            f"{stats_text}"
            "请发送一个域名或包含域名的 URL。\n"
            "例如：`example.com`、`sub.example.com`、`https://example.com/path`\n\n"
            "系统会提取可注册域名（主域名）；检查完成后仍需确认，才会写入公开 GitHub。"
        )
    
    async def check_group_membership(self, update: Update) -> bool:
        """检查用户群组成员身份"""
        if not self.group_service or not self.group_service.is_group_check_enabled():
            return True
        
        user_id = update.effective_user.id
        check_result = await self.group_service.check_user_in_group(user_id)
        
        if check_result is True:
            return True

        if check_result is False:
            join_message = self.group_service.get_join_group_message()
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(join_message, parse_mode='Markdown')
            else:
                await update.message.reply_text(join_message, parse_mode='Markdown')
            return False

        error_message = "⚠️ 群组验证失败，请稍后重试。"
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(error_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(error_message, parse_mode='Markdown')
        
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
            )
            
        except Exception as e:
            logger.error(f"处理 start 命令失败: {e}")
            await update.message.reply_text(
                "服务暂时不可用，请稍后再试。",
                reply_markup=self._build_main_menu_keyboard(),
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
                "🆔 *您的 Telegram 用户 ID：* "
                f"`{user.id}`\n"
                f"👤 *{identity_label}：* {self._format_telegram_identity(user)}"
            )
            await update.message.reply_text(
                text,
                reply_markup=self._recovery_keyboard(),
                parse_mode='Markdown',
            )
        except Exception as e:
            logger.error(f"处理 id 命令失败: {e}")
            await update.message.reply_text(
                "处理失败，请重试。", reply_markup=self._recovery_keyboard()
            )

    async def query_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /query 命令"""
        if not await self.check_group_membership(update):
            return
        user_id = update.effective_user.id
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_query_domain")

        stats_text = await self._build_stats_text()
        keyboard = [[InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            self._build_query_prompt(stats_text),
            reply_markup=reply_markup,
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
            "⚠️ 无法识别该命令。请使用下方菜单，或发送 /help 查看可用命令。",
            reply_markup=self._build_main_menu_keyboard(),
        )
    
    async def skip_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /skip 命令"""
        try:
            if not await self.check_group_membership(update):
                return

            user_id = update.effective_user.id
            user_state = self.get_user_state(user_id)
            if user_state.get("state") != "waiting_description":
                await update.message.reply_text("当前没有需要跳过的说明。")
                return

            await self._add_domain_to_github_message(update.message, user_id, "")
        except Exception as e:
            logger.error(f"处理 skip 命令失败: {e}")
            await update.message.reply_text(
                "处理失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新开始"
                ),
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理回调查询"""
        query = update.callback_query
        try:
            if not self.is_update_context_allowed(update):
                await query.answer("当前会话不在允许范围内", show_alert=True)
                return
            data = query.data
            self_service_callbacks = {
                "matchscope_access",
                "matchscope_issue",
                "matchscope_revoke",
                "matchscope_delete_credential",
                "matchscope_privacy",
                "matchscope_privacy_accept",
                "matchscope_privacy_withdraw",
            }
            is_self_service_callback = data in self_service_callbacks or data.startswith(
                (
                    "matchscope_issue_confirm|",
                    "matchscope_revoke_confirm|",
                    "matchscope_privacy_withdraw_confirm|",
                )
            )
            # MatchScope issuance performs its own fresh membership check;
            # access, revocation and credential deletion remain available.
            if not is_self_service_callback and not await self.check_group_membership(update):
                return
            
            await query.answer()
            
            user_id = update.effective_user.id
            if data == "main_menu":
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
            elif data == "matchscope_access":
                await self._show_matchscope_access(query, user_id)
            elif data == "matchscope_issue":
                await self._issue_matchscope_token(query, user_id)
            elif data == "matchscope_revoke":
                await self._revoke_matchscope_token(query, user_id)
            elif data.startswith("matchscope_issue_confirm|"):
                await self._confirm_matchscope_issue(query, user_id, data)
            elif data.startswith("matchscope_revoke_confirm|"):
                await self._confirm_matchscope_revoke(query, user_id, data)
            elif data == "matchscope_delete_credential":
                await query.message.delete()
            elif data == "matchscope_privacy":
                await self._show_matchscope_privacy(query, user_id)
            elif data == "matchscope_privacy_accept":
                await self._accept_matchscope_privacy(query, user_id)
            elif data == "matchscope_privacy_withdraw":
                await self._withdraw_matchscope_privacy(query, user_id)
            elif data.startswith("matchscope_privacy_withdraw_confirm|"):
                await self._confirm_matchscope_privacy_withdraw(query, user_id, data)
            elif data.startswith("add_domain|"):
                await self._handle_add_domain_callback(query, user_id, data)
            elif data.startswith("confirm_add|"):
                await self._handle_confirm_add_callback(query, user_id, data)
            elif data == "skip_description":
                await self._handle_skip_description(query, user_id)
            elif data.startswith("admin_force_add|"):
                await self._handle_admin_force_add_callback(query, user_id, data)
            else:
                await query.edit_message_text(
                    "⚠️ 该操作已不可用，请重新选择。",
                    reply_markup=self._recovery_keyboard(),
                )
                
        except Exception as e:
            logger.error(f"处理回调失败: {e}")
            await query.edit_message_text(
                "❌ 操作失败，请重试。",
                reply_markup=self._recovery_keyboard(),
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
                "处理失败，请重试。", reply_markup=self._recovery_keyboard()
            )
    
    async def _show_main_menu(self, query, user_id: Optional[int] = None):
        """显示主菜单"""
        user_id = user_id or query.from_user.id
        self._reset_user_flow(user_id)
        username = query.from_user.first_name or query.from_user.username or "用户"
        welcome_text = self._build_main_menu_text(username)
        reply_markup = self._build_main_menu_keyboard()
        await query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

    async def _show_main_menu_message(self, message):
        """通过消息显示主菜单"""
        self._reset_user_flow(message.from_user.id)
        username = message.from_user.first_name or message.from_user.username or "用户"
        welcome_text = self._build_main_menu_text(username)
        reply_markup = self._build_main_menu_keyboard()
        await message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

    def _matchscope_endpoint(self) -> str:
        return (
            f"{self.config.MATCHSCOPE_PUBLIC_BASE_URL}"
            f"{self.config.MATCHSCOPE_PUBLIC_API_PATH}"
        )

    @staticmethod
    def _matchscope_token_is_active(status: Optional[dict]) -> bool:
        return bool(
            status
            and status.get("enabled")
            and int(status.get("expires_at", 0)) > int(time.time())
        )

    async def _show_matchscope_access(self, query, user_id: int):
        """Show self-service token status without exposing the credential."""
        self._reset_user_flow(user_id)
        if not self.matchscope_token_service:
            await query.edit_message_text(
                "MatchScope 社区接入当前未开放。",
                reply_markup=self._recovery_keyboard(),
            )
            return
        status = await self.matchscope_token_service.status(user_id)
        token_active = self._matchscope_token_is_active(status)
        consented = await self.matchscope_token_service.has_current_consent(user_id)
        active = token_active and consented
        status_text = "尚未签发或已失效"
        issue_label = "🔑 申请 Token"
        if active:
            expires = datetime.fromtimestamp(
                int(status["expires_at"]), timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            status_text = f"有效，有效期至 {expires}"
            issue_label = "🔄 重新签发 Token"
        elif token_active:
            status_text = "已暂停，请先确认当前隐私说明"
        keyboard = [[InlineKeyboardButton("🛡️ 隐私说明", callback_data="matchscope_privacy")]]
        if consented:
            keyboard.append(
                [InlineKeyboardButton(issue_label, callback_data="matchscope_issue")]
            )
        if token_active:
            keyboard.append(
                [InlineKeyboardButton("🚫 吊销当前 Token", callback_data="matchscope_revoke")]
            )
        keyboard.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")])
        await query.edit_message_text(
            "🔗 *MatchScope 社区接入*\n\n"
            "每位群成员可申请独立 Token；凭据只显示一次。\n\n"
            f"🔐 *状态：* {status_text}\n"
            f"🌐 *入口：* `{self._matchscope_endpoint()}`\n\n"
            "申请前请先阅读隐私说明。重新签发、吊销和撤回同意均需二次确认。",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def _show_matchscope_privacy(self, query, user_id: int):
        """Explain the community ingress privacy boundary before consent."""
        self._reset_user_flow(user_id)
        if not self.matchscope_token_service:
            await query.edit_message_text(
                "MatchScope 社区接入当前未开放。",
                reply_markup=self._recovery_keyboard(),
            )
            return
        consented = await self.matchscope_token_service.has_current_consent(user_id)
        keyboard = []
        if not consented:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "✅ 同意并继续", callback_data="matchscope_privacy_accept"
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "撤回同意并吊销 Token",
                        callback_data="matchscope_privacy_withdraw",
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
                [InlineKeyboardButton("↩️ 返回接入页", callback_data="matchscope_access")],
            ]
        )
        await query.edit_message_text(
            "🛡️ *MatchScope 隐私说明*\n\n"
            "• 官方 MatchScope 按默认配置只上报用于规则判断的可注册域名，"
            "并在本地去重、排除 `.cn` 和本地排除项；不主动上报 URL 路径、"
            "查询参数、网页内容或访问次数。\n"
            "• 第三方或修改版客户端不受本机器人控制；请自行确认其代码与配置"
            "实际发送的数据。\n"
            "• 若入口经过 Cloudflare、其他 CDN 或代理，相关基础设施可能处理"
            "出口 IP、请求时间和请求元数据；实际链路取决于当前部署。\n"
            "• 社区 Token 与 Telegram 用户关联，用于群成员校验、限流、"
            "续签和吊销；服务处理期间可将请求与账号关联，但不会把 Telegram "
            "身份写入规则提交。\n"
            "• 被拒绝的域名不会写入公开规则；符合规则并成功添加的域名会公开"
            "出现在 GitHub 规则和提交历史中。\n\n"
            "同意表示您理解所用客户端、网络链路、账号关联及公开提交边界。\n"
            f"当前状态：{'已同意' if consented else '尚未同意'}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def _accept_matchscope_privacy(self, query, user_id: int):
        if not self.matchscope_token_service:
            await query.edit_message_text(
                "MatchScope 社区接入当前未开放。",
                reply_markup=self._recovery_keyboard(),
            )
            return
        await self.matchscope_token_service.consent(user_id)
        await self._show_matchscope_access(query, user_id)

    async def _withdraw_matchscope_privacy(self, query, user_id: int):
        self._reset_user_flow(user_id)
        if not self.matchscope_token_service:
            await query.edit_message_text(
                "MatchScope 社区接入当前未开放。",
                reply_markup=self._recovery_keyboard(),
            )
            return
        token = self.create_pending_action(
            user_id, "matchscope_privacy_withdraw", confirmed=True
        )
        await query.edit_message_text(
            "⚠️ *确认撤回隐私同意？*\n\n"
            "撤回后，当前 MatchScope Token 会同时失效，客户端将无法继续提交。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ 确认撤回并吊销",
                            callback_data=f"matchscope_privacy_withdraw_confirm|{token}",
                        )
                    ],
                    [InlineKeyboardButton("取消", callback_data="matchscope_privacy")],
                ]
            ),
            parse_mode="Markdown",
        )

    async def _confirm_matchscope_privacy_withdraw(
        self, query, user_id: int, data: str
    ):
        token = data.split("|", 1)[1] if "|" in data else ""
        action = self.get_pending_action(
            user_id, token, "matchscope_privacy_withdraw", consume=True
        )
        if not action:
            await query.edit_message_text(
                "⌛ 此确认已过期，隐私同意和 Token 均未更改。",
                reply_markup=self._recovery_keyboard(
                    "matchscope_privacy", "🛡️ 返回隐私说明"
                ),
            )
            return
        await self.matchscope_token_service.withdraw_consent(user_id)
        await query.edit_message_text(
            "🛡️ 已撤回隐私同意并吊销当前 MatchScope Token。",
            reply_markup=self._recovery_keyboard(
                "matchscope_access", "↩️ 返回接入页"
            ),
        )

    async def _issue_matchscope_token(self, query, user_id: int):
        """Issue a token, requiring confirmation when an old token would be replaced."""
        self._reset_user_flow(user_id)
        if not await self.matchscope_token_service.has_current_consent(user_id):
            await self._show_matchscope_privacy(query, user_id)
            return
        status = await self.matchscope_token_service.status(user_id)
        if self._matchscope_token_is_active(status):
            token = self.create_pending_action(
                user_id, "matchscope_reissue", confirmed=True
            )
            await query.edit_message_text(
                "⚠️ *确认重新签发 Token？*\n\n"
                "继续后旧 Token 会立即失效，所有仍使用旧 Token 的客户端都会停止提交。",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ 确认重新签发",
                                callback_data=f"matchscope_issue_confirm|{token}",
                            )
                        ],
                        [InlineKeyboardButton("取消", callback_data="matchscope_access")],
                    ]
                ),
                parse_mode="Markdown",
            )
            return
        await self._perform_matchscope_issue(query, user_id)

    async def _confirm_matchscope_issue(self, query, user_id: int, data: str):
        token = data.split("|", 1)[1] if "|" in data else ""
        action = self.get_pending_action(
            user_id, token, "matchscope_reissue", consume=True
        )
        if not action:
            await query.edit_message_text(
                "⌛ 此确认已过期，原 Token 未更改。",
                reply_markup=self._recovery_keyboard(
                    "matchscope_access", "↩️ 返回接入页"
                ),
            )
            return
        if not await self.matchscope_token_service.has_current_consent(user_id):
            await self._show_matchscope_privacy(query, user_id)
            return
        await self._perform_matchscope_issue(query, user_id)

    async def _perform_matchscope_issue(self, query, user_id: int):
        """Freshly verify membership and deliver a one-time credential message."""
        membership = await self.group_service.check_user_in_group(
            user_id, force_refresh=True
        )
        if membership is not True:
            if membership is False:
                message = self.group_service.get_join_group_message()
            else:
                message = "⚠️ 群组验证失败，请稍后重试。"
            await query.edit_message_text(message, parse_mode="Markdown")
            return

        issued = await self.matchscope_token_service.issue(user_id)
        token = issued["token"]
        endpoint = self._matchscope_endpoint()
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
                        callback_data="matchscope_delete_credential",
                    )
                ],
            ]
        )
        await query.message.reply_text(
            "✅ *MatchScope Token 已签发*\n\n"
            f"🌐 *入口*\n`{endpoint}`\n\n"
            f"🔑 *Token*\n`{token}`\n\n"
            f"⏳ *有效期至：* {expires}\n\n"
            "请将入口与 Token 写入 MatchScope 配置；不要转发本消息。",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        await self._show_matchscope_access(query, user_id)

    async def _revoke_matchscope_token(self, query, user_id: int):
        self._reset_user_flow(user_id)
        status = await self.matchscope_token_service.status(user_id)
        if not self._matchscope_token_is_active(status):
            await query.edit_message_text(
                "ℹ️ 当前没有可吊销的有效 Token。",
                reply_markup=self._recovery_keyboard(
                    "matchscope_access", "↩️ 返回接入页"
                ),
            )
            return
        token = self.create_pending_action(
            user_id, "matchscope_revoke", confirmed=True
        )
        await query.edit_message_text(
            "⚠️ *确认吊销当前 Token？*\n\n"
            "吊销后，所有使用该 Token 的客户端都会立即停止提交。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ 确认吊销",
                            callback_data=f"matchscope_revoke_confirm|{token}",
                        )
                    ],
                    [InlineKeyboardButton("取消", callback_data="matchscope_access")],
                ]
            ),
            parse_mode="Markdown",
        )

    async def _confirm_matchscope_revoke(self, query, user_id: int, data: str):
        token = data.split("|", 1)[1] if "|" in data else ""
        action = self.get_pending_action(
            user_id, token, "matchscope_revoke", consume=True
        )
        if not action:
            await query.edit_message_text(
                "⌛ 此确认已过期，当前 Token 未更改。",
                reply_markup=self._recovery_keyboard(
                    "matchscope_access", "↩️ 返回接入页"
                ),
            )
            return
        revoked = await self.matchscope_token_service.revoke(user_id)
        text = "当前 Token 已吊销。" if revoked else "当前没有可吊销的 Token。"
        await query.edit_message_text(
            f"🚫 {text}",
            reply_markup=self._recovery_keyboard(
                "matchscope_access", "↩️ 返回接入页"
            ),
        )

    async def _start_domain_query(self, query, user_id: int):
        """开始域名查询"""
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_query_domain")

        stats_text = await self._build_stats_text()
        keyboard = [[InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            self._build_query_prompt(stats_text),
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def _start_add_direct_rule(self, query, user_id: int):
        """开始添加直连规则"""
        self._reset_user_flow(user_id)
        self.set_user_state(user_id, "waiting_add_domain")

        stats_text = await self._build_stats_text(user_id=user_id, include_limit=True)
        keyboard = [[InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            self._build_add_prompt(stats_text),
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def _show_proxy_rule_not_supported(self, query, user_id: int):
        """显示代理规则不支持"""
        self._reset_user_flow(user_id)
        keyboard = [[InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"➕ *添加代理规则*\n\n📂 *目标仓库：* `{self.config.GITHUB_REPO}`\n📄 *规则文件：* `{self.config.PROXY_RULE_FILE}`\n\n⚠️ *代理规则功能暂不支持*\n\n该功能正在开发中，敬请期待。",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def _show_delete_not_supported(self, query, user_id: int):
        """显示删除功能不支持"""
        self._reset_user_flow(user_id)
        keyboard = [[InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            self._build_delete_unavailable_text(),
            reply_markup=reply_markup,
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
        try:
            processing_msg = await update.message.reply_text("🔍 正在查询域名信息，请稍候...")
            domain = normalize_domain(domain_input)
            if not domain:
                await processing_msg.edit_text(
                    "❌ 无法识别有效域名，请检查后重试。",
                    reply_markup=self._recovery_keyboard(
                        "query_domain", "🔍 重新输入"
                    ),
                )
                return

            if is_cn_domain(domain):
                result_text = (
                    "✅ *.cn 域名默认直连，无需添加*\n\n"
                    f"📍 *查询域名：* `{domain}`\n"
                    "规则已覆盖所有以 `.cn` 结尾的域名。"
                )
                keyboard = [
                    [InlineKeyboardButton("🔍 重新查询", callback_data="query_domain")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
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
            await processing_msg.edit_text("🔍 正在检查域名 IP 和 NS 信息...")
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
                conclusion = "✅ *结论：已被直连规则覆盖，无需添加*"
            elif check_failed:
                conclusion = "⚠️ *结论：检查未完成，请稍后重试*"
            elif github_unavailable and has_china_signal:
                conclusion = (
                    "⚠️ *结论：检测到中国大陆信号，但 GitHub 状态暂不可用*"
                )
            elif github_unavailable:
                conclusion = "⚠️ *结论：GitHub 状态暂不可用，结果不完整*"
            elif has_china_signal:
                conclusion = "✅ *结论：检测到中国大陆信号，可以继续评估添加*"
            else:
                conclusion = "ℹ️ *结论：未检测到中国大陆 IP 或 NS，不建议添加*"

            lines = [conclusion, "", f"📍 *查询域名：* `{domain}`", ""]
            if github_unavailable:
                lines.append("⚠️ *GitHub 规则：* 暂时无法读取")
            elif github_exists:
                lines.append("✅ *GitHub 规则：* 已存在")
                matches = self._format_rule_matches(github_result.get("matches", []))
                if matches:
                    lines.append(matches)
            else:
                lines.append("▫️ *GitHub 规则：* 未找到")
            lines.append(
                "✅ *GEOSITE:CN：* 已存在"
                if in_geosite
                else "▫️ *GEOSITE:CN：* 未找到"
            )

            if check_failed:
                error = self.escape_markdown(str(check_result.get("error", "未知错误"))[:300])
                lines.extend(["", f"❌ *DNS/IP/NS 检查：* {error}"])
            else:
                domain_ips = self._format_value_list(check_result.get("domain_ips", []))
                registered_ips = self._format_value_list(
                    check_result.get("second_level_ips", [])
                )
                if domain_ips or registered_ips:
                    lines.extend(["", "📡 *解析结果：*"])
                    if domain_ips:
                        lines.append(f"   • 输入域名 IP：{domain_ips}")
                    if registered_ips:
                        lines.append(f"   • 可注册域名 IP：{registered_ips}")
                detail_lines = self._format_detail_lines(check_result.get("details", []))
                if detail_lines:
                    lines.extend(["", "🌍 *归属检查：*", detail_lines])
                recommendation = str(check_result.get("recommendation", "")).strip()
                if recommendation:
                    lines.extend(
                        [
                            "",
                            f"💡 *检测建议：* {self.escape_markdown(recommendation[:300])}",
                        ]
                    )
            result_text = "\n".join(lines)

            keyboard = []
            if (
                not github_unavailable
                and not github_exists
                and not in_geosite
                and not check_failed
                and has_china_signal
            ):
                token = self.create_pending_action(user_id, "add_domain", domain=domain)
                keyboard.append([InlineKeyboardButton("➕ 添加到直连规则", callback_data=f"add_domain|{token}")])
            keyboard.append([InlineKeyboardButton("🔍 重新查询", callback_data="query_domain")])
            keyboard.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")])

            await processing_msg.edit_text(
                result_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown',
            )
            self.set_user_state(user_id, "waiting_query_domain")
        except Exception as e:
            logger.error(f"域名查询失败: {e}")
            await update.message.reply_text(
                "❌ 查询失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "query_domain", "🔍 重新查询"
                ),
            )
    
    async def _handle_add_domain_input(self, update: Update, domain_input: str, user_id: int):
        """处理添加域名输入"""
        try:
            # 发送处理中消息
            processing_msg = await update.message.reply_text("🔍 正在检查域名，请稍候...")
            
            # 检查用户添加频率限制
            can_add, remaining = self.check_user_add_limit(user_id)
            if not can_add:
                keyboard = [
                    [InlineKeyboardButton("🔍 查询域名", callback_data="query_domain")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(
                    "⚠️ *添加频率限制*\n\n"
                    f"您在当前小时内已达到添加上限（{self.MAX_ADDS_PER_HOUR}个域名）。\n\n"
                    "🕐 请等待一小时后再尝试添加新域名。",
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
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(
                    "ℹ️ *.cn 域名默认直连，无需添加*\n\n"
                    "所有以 `.cn` 结尾的域名已由默认规则覆盖。",
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
                        [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await processing_msg.edit_text(
                        "ℹ️ *.cn 域名默认直连，无需添加*\n\n"
                        "所有以 `.cn` 结尾的域名已由默认规则覆盖。",
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                else:
                    await processing_msg.edit_text(
                        "❌ 无法识别有效域名，请检查后重试。",
                        reply_markup=self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新输入"
                        ),
                    )
                # 保持添加模式，便于继续输入域名
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 显示提取的可注册域名信息
            if domain != normalize_domain(domain_input):
                await processing_msg.edit_text(
                    f"🔍 已提取可注册域名（主域名）：`{domain}`\n\n正在检查域名状态...",
                    parse_mode="Markdown",
                )
            
            # 1. 防重复检查
            await processing_msg.edit_text("🔍 正在检查域名是否已存在...")
            
            # 检查 GitHub 规则
            github_result = await self.github_service.check_domain_in_rules(domain)
            if github_result.get("error"):
                error = self.escape_markdown(str(github_result["error"])[:300])
                await processing_msg.edit_text(
                    f"❌ GitHub 规则暂时无法读取：{error}",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return
            second_level = extract_second_level_domain(domain)
            
            if github_result.get("exists"):
                result_text = "ℹ️ *无需添加：规则已存在*\n\n"
                result_text += f"📍 *可注册域名：* `{domain}`\n\n"
                result_text += "📋 *找到的规则：*\n"
                result_text += self._format_rule_matches(github_result.get("matches", []))
                
                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 检查可注册域名规则
            if second_level and second_level != domain:
                second_level_result = await self.github_service.check_domain_in_rules(second_level)
                if second_level_result.get("error"):
                    error = self.escape_markdown(str(second_level_result["error"])[:300])
                    await processing_msg.edit_text(
                        f"❌ GitHub 规则暂时无法读取：{error}",
                        reply_markup=self._recovery_keyboard(
                            "add_direct_rule", "➕ 重新检查"
                        ),
                        parse_mode="Markdown",
                    )
                    return
                if second_level_result.get("exists"):
                    result_text = "ℹ️ *无需添加：可注册域名已在规则中*\n\n"
                    result_text += f"📍 *输入域名：* `{domain}`\n"
                    result_text += f"🧭 *可注册域名：* `{second_level}`\n\n"
                    result_text += "📋 *找到的规则：*\n"
                    result_text += self._format_rule_matches(
                        second_level_result.get("matches", [])
                    )
                    
                    keyboard = [
                        [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                        [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                    self.set_user_state(user_id, "waiting_add_domain")
                    return
            
            # 检查GeoSite
            in_geosite = await self.data_manager.is_domain_in_geosite(domain)
            if in_geosite:
                result_text = "ℹ️ *无需添加：GEOSITE:CN 已覆盖*\n\n"
                result_text += f"📍 *域名：* `{domain}`\n\n"
                result_text += "该域名已在 GEOSITE:CN 规则中，不需要重复添加。"
                
                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            # 2. 进行域名检查
            await processing_msg.edit_text("🔍 正在检查域名 IP 和 NS 信息...")
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            
            if "error" in check_result:
                error = self.escape_markdown(str(check_result["error"])[:300])
                await processing_msg.edit_text(
                    f"❌ 域名检查失败：{error}",
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
                keyboard.append([InlineKeyboardButton("取消", callback_data=f"confirm_add|no|{token}")])
            elif should_reject:
                # 不符合条件，拒绝添加
                result_text += "\n❌ *不符合添加条件，无法添加到直连规则。*"
                if self.is_admin(user_id):
                    result_text += "\n🛡️ *管理员权限：* 可强制添加"
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
                keyboard.append([InlineKeyboardButton("取消", callback_data=f"confirm_add|no|{token}")])
            
            keyboard.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')

            if should_reject:
                self.set_user_state(user_id, "waiting_add_domain")
            
        except Exception as e:
            logger.error(f"添加域名输入处理失败: {e}")
            await update.message.reply_text("处理失败，请重试。")
    
    async def _handle_add_domain_callback(self, query, user_id: int, data: str):
        """处理添加域名回调"""
        try:
            token = data.split("|", 1)[1] if "|" in data else ""
            action = self.get_pending_action(user_id, token, "add_domain", consume=True)
            if not action:
                await query.edit_message_text(
                    "⌛ 此操作已过期，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "query_domain", "🔍 重新查询"
                    ),
                )
                return
            domain = action.get("domain", "")
            
            # 进行域名检查
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            
            if "error" in check_result:
                error = self.escape_markdown(str(check_result["error"])[:300])
                await query.edit_message_text(
                    f"❌ 域名检查失败：{error}",
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
                keyboard.append([InlineKeyboardButton("取消", callback_data=f"confirm_add|no|{confirm_token}")])
            else:
                result_text += "\n❌ *不符合添加条件，无法添加到直连规则。*"
                if self.is_admin(user_id):
                    result_text += "\n🛡️ *管理员权限：* 可强制添加"
                    keyboard.append([
                        InlineKeyboardButton(
                            "🛡️ 管理员权限添加",
                            callback_data=self.get_admin_force_add_callback(user_id, domain)
                        )
                    ])
            
            keyboard.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')

            if should_reject:
                self.set_user_state(user_id, "waiting_add_domain")
            
        except Exception as e:
            logger.error(f"处理添加域名回调失败: {e}")
            await query.edit_message_text(
                "操作失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "query_domain", "🔍 重新查询"
                ),
            )

    async def _handle_admin_force_add_callback(self, query, user_id: int, data: str):
        """处理管理员权限强制添加回调"""
        try:
            if not self.is_admin(user_id):
                logger.warning(f"管理员权限操作被拒绝: user_id={user_id}, data={data}")
                await query.edit_message_text(
                    "❌ 当前用户没有管理员权限。",
                    reply_markup=self._recovery_keyboard(),
                )
                return

            token = data.split("|", 1)[1] if "|" in data else ""
            action = self.get_pending_action(
                user_id,
                token,
                "admin_force_add",
                consume=True,
            )
            if not action:
                await query.edit_message_text(
                    "⌛ 此管理员操作已过期，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                )
                return
            domain = action.get("domain", "")

            domain = extract_second_level_domain_for_rules(domain)
            if not domain:
                await query.edit_message_text(
                    "❌ 无效的域名格式，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
                )
                return

            if is_cn_domain(domain):
                await query.edit_message_text(
                    "ℹ️ *.cn 域名默认直连，无需手动添加。*",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 添加其他域名"
                    ),
                    parse_mode='Markdown'
                )
                return

            # 检查用户添加频率限制
            can_add, _ = self.check_user_add_limit(user_id)
            if not can_add:
                keyboard = [
                    [InlineKeyboardButton("🔍 查询域名", callback_data="query_domain")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    "⚠️ *添加频率限制*\n\n"
                    f"您在当前小时内已达到添加上限（{self.MAX_ADDS_PER_HOUR}个域名）。\n\n"
                    "🕐 请等待一小时后再尝试添加新域名。\n\n"
                    "💡 此限制是为了防止系统滥用，感谢您的理解。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return

            # 防重复检查
            github_result = await self.github_service.check_domain_in_rules(domain)
            if github_result.get("error"):
                error = self.escape_markdown(str(github_result["error"])[:300])
                await query.edit_message_text(
                    f"❌ GitHub 规则暂时无法读取：{error}",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return
            if github_result.get("exists"):
                result_text = "❌ *域名已存在于规则中*\n\n"
                result_text += f"📍 *域名：* `{domain}`\n\n"
                result_text += "📋 *找到的规则：*\n"
                result_text += self._format_rule_matches(
                    github_result.get("matches", [])
                )

                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                return

            in_geosite = await self.data_manager.is_domain_in_geosite(domain)
            if in_geosite:
                result_text = "❌ *域名已存在于 GEOSITE:CN 中*\n\n"
                result_text += f"📍 *域名：* `{domain}`\n\n"
                result_text += "该域名已在 GEOSITE:CN 规则中，不需要重复添加。"

                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
                return

            await query.edit_message_text("⏳ 正在执行管理员权限添加...")
            check_result = await self.domain_checker.check_domain_comprehensive(domain)
            if "error" in check_result:
                error = self.escape_markdown(str(check_result["error"])[:300])
                await query.edit_message_text(
                    f"❌ 域名检查失败：{error}",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                    parse_mode="Markdown",
                )
                return

            target_domain = self.domain_checker.get_target_domain_to_add(check_result) or domain
            username = self._raw_telegram_identity(query.from_user)

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
                result_text = "❌ *域名添加失败*\n\n"
                result_text += f"📍 *域名：* `{target_domain}`\n"
                error = self.escape_markdown(
                    str(add_result.get("error", "未知错误"))[:300]
                )
                result_text += f"❌ *错误：* {error}"

            keyboard = [
                [InlineKeyboardButton("➕ 继续添加", callback_data="add_direct_rule")],
                [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
            if add_result.get("success"):
                await self._announce_private_addition(
                    getattr(query.message, "chat", None),
                    target_domain,
                    add_result,
                    username,
                )
            self.set_user_state(user_id, "waiting_add_domain")

        except Exception as e:
            logger.error(f"处理管理员权限添加失败: {e}")
            await query.edit_message_text(
                "操作失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新检查"
                ),
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
                    "⌛ 此确认已过期，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                )
                return

            if decision == "no":
                # 取消添加
                keyboard = [
                    [InlineKeyboardButton("➕ 添加其他域名", callback_data="add_direct_rule")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    "❌ *已取消添加*\n\n您可以重新选择要添加的域名。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                self.set_user_state(user_id, "waiting_add_domain")
                return
            
            if decision != "yes":
                await query.edit_message_text(
                    "⚠️ 无效的确认操作，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新检查"
                    ),
                )
                return

            # 确认添加
            domain = domain_data.get("domain")
            if not domain:
                await query.edit_message_text(
                    "❌ 域名数据已失效，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
                )
                return

            target_domain = domain_data.get("target_domain") or self._target_domain(
                domain, domain_data.get("check_result", {})
            )
            
            # 询问说明
            self.set_user_state(user_id, "waiting_description", domain_data)
            
            keyboard = [
                [InlineKeyboardButton("⏭️ 跳过说明并提交", callback_data="skip_description")],
                [InlineKeyboardButton("取消并返回主菜单", callback_data="main_menu")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "📝 *可选：填写公开说明*\n\n"
                f"🧭 *最终写入：* `DOMAIN-SUFFIX,{target_domain}`\n\n"
                f"发送一行说明（最多 {self.MAX_DESCRIPTION_LENGTH} 个字符）将立即公开提交；"
                "也可点击“跳过说明并提交”。\n\n"
                f"{self._submission_notice(query.from_user)}",
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
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    "❌ *说明内容不符合要求*\n\n"
                    f"📏 *限制：* 最多 {self.MAX_DESCRIPTION_LENGTH} 个字符，且不能包含换行或控制字符\n"
                    f"📝 *您的输入：* {len(description)} 个字符\n\n"
                    f"✂️ 已截取前 {len(processed_description)} 个字符，请重新输入。\n\n"
                    "💡 请重新输入简短的说明，或发送 `/skip` 跳过说明。",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return
            
            await self._add_domain_to_github_message(update.message, user_id, processed_description)
            
        except Exception as e:
            logger.error(f"处理说明输入失败: {e}")
            await update.message.reply_text(
                "处理失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新开始"
                ),
            )
    
    async def _add_domain_to_github(self, query, user_id: int, description: str):
        """添加域名到 GitHub"""
        try:
            user_state = self.get_user_state(user_id)
            domain_data = user_state.get("data", {})
            
            domain = domain_data.get("domain")
            check_result = domain_data.get("check_result")
            
            if not domain or not check_result:
                await query.edit_message_text(
                    "❌ 操作数据已失效，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
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
            await query.edit_message_text("⏳ 正在添加域名到 GitHub 规则...")
            
            # 添加到 GitHub
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
                result_text = "❌ *域名添加失败*\n\n"
                result_text += f"📍 *域名：* `{target_domain}`\n"
                error = self.escape_markdown(
                    str(add_result.get("error", "未知错误"))[:300]
                )
                result_text += f"❌ *错误：* {error}"
            
            keyboard = [
                [InlineKeyboardButton("➕ 继续添加", callback_data="add_direct_rule")],
                [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
            if add_result.get("success"):
                await self._announce_private_addition(
                    getattr(query.message, "chat", None),
                    target_domain,
                    add_result,
                    username,
                )
            
            # 保持添加模式，便于继续输入域名
            self.set_user_state(user_id, "waiting_add_domain")
            
        except Exception as e:
            logger.error(f"添加域名到 GitHub 失败: {e}")
            await query.edit_message_text(
                "添加失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新开始"
                ),
            )
    
    async def _add_domain_to_github_message(self, message, user_id: int, description: str):
        """通过消息添加域名到 GitHub"""
        try:
            user_state = self.get_user_state(user_id)
            domain_data = user_state.get("data", {})
            
            domain = domain_data.get("domain")
            check_result = domain_data.get("check_result")
            
            if not domain or not check_result:
                await message.reply_text(
                    "❌ 操作数据已失效，尚未提交任何规则。",
                    reply_markup=self._recovery_keyboard(
                        "add_direct_rule", "➕ 重新开始"
                    ),
                )
                return
            
            # 获取要添加的目标域名
            target_domain = self.domain_checker.get_target_domain_to_add(check_result)
            if not target_domain:
                target_domain = domain
            
            # 显示添加中消息
            processing_msg = await message.reply_text("⏳ 正在添加域名到 GitHub 规则...")
            
            # 添加到 GitHub
            username = self._raw_telegram_identity(message.from_user)
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
                result_text = "❌ *域名添加失败*\n\n"
                result_text += f"📍 *域名：* `{target_domain}`\n"
                error = self.escape_markdown(
                    str(add_result.get("error", "未知错误"))[:300]
                )
                result_text += f"❌ *错误：* {error}"
            
            keyboard = [
                [InlineKeyboardButton("➕ 继续添加", callback_data="add_direct_rule")],
                [InlineKeyboardButton("🏠 返回主菜单", callback_data="main_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await processing_msg.edit_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
            if add_result.get("success"):
                await self._announce_private_addition(
                    getattr(message, "chat", None),
                    target_domain,
                    add_result,
                    username,
                )
            
            # 保持添加模式，便于继续输入域名
            self.set_user_state(user_id, "waiting_add_domain")
            
        except Exception as e:
            logger.error(f"添加域名到 GitHub 失败: {e}")
            await message.reply_text(
                "添加失败，请重试。",
                reply_markup=self._recovery_keyboard(
                    "add_direct_rule", "➕ 重新开始"
                ),
            )

 
