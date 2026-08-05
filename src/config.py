"""
配置管理模块
"""

import os
import re
from typing import Optional, Dict
from loguru import logger


class Config:
    """配置类"""
    
    def __init__(self):
        # Telegram配置
        self.TELEGRAM_BOT_TOKEN = self._get_env_required("TELEGRAM_BOT_TOKEN")
        
        # GitHub配置
        self.GITHUB_TOKEN = self._get_env_required("GITHUB_TOKEN")
        self.GITHUB_REPO = self._get_env_required("GITHUB_REPO")
        self.GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "").strip()  # 可选，默认使用仓库默认分支
        # 强制使用Rule-Bot身份，只允许自定义邮箱
        self.GITHUB_COMMIT_NAME = "Rule-Bot"
        self.GITHUB_COMMIT_EMAIL = os.getenv("GITHUB_COMMIT_EMAIL", "noreply@users.noreply.github.com")
        
        # 规则文件配置
        self.DIRECT_RULE_FILE = self._get_env_required("DIRECT_RULE_FILE")
        self.PROXY_RULE_FILE = os.getenv("PROXY_RULE_FILE", "")  # 可选，暂未启用
        
        # 日志配置
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        # 数据目录（可选）
        self.DATA_DIR = os.getenv("DATA_DIR", "").strip()

        # 性能与缓存配置
        self.DNS_CACHE_TTL = self._parse_int_env("DNS_CACHE_TTL", 60, min_value=0)
        self.DNS_CACHE_SIZE = self._parse_int_env("DNS_CACHE_SIZE", 1024, min_value=0)
        self.NS_CACHE_TTL = self._parse_int_env("NS_CACHE_TTL", 300, min_value=0)
        self.NS_CACHE_SIZE = self._parse_int_env("NS_CACHE_SIZE", 512, min_value=0)
        self.DNS_MAX_CONCURRENCY = self._parse_int_env("DNS_MAX_CONCURRENCY", 20, min_value=1)
        self.DNS_CONN_LIMIT = self._parse_int_env("DNS_CONN_LIMIT", 30, min_value=1)
        self.DNS_CONN_LIMIT_PER_HOST = self._parse_int_env("DNS_CONN_LIMIT_PER_HOST", 10, min_value=1)
        self.DNS_TIMEOUT_TOTAL = self._parse_int_env("DNS_TIMEOUT_TOTAL", 10, min_value=1)
        self.DNS_TIMEOUT_CONNECT = self._parse_int_env("DNS_TIMEOUT_CONNECT", 3, min_value=1)

        self.GEOSITE_CACHE_TTL = self._parse_int_env("GEOSITE_CACHE_TTL", 3600, min_value=0)
        self.GEOSITE_CACHE_SIZE = self._parse_int_env("GEOSITE_CACHE_SIZE", 2048, min_value=0)
        self.GEOIP_CACHE_TTL = self._parse_int_env("GEOIP_CACHE_TTL", 21600, min_value=0)
        self.GEOIP_CACHE_SIZE = self._parse_int_env("GEOIP_CACHE_SIZE", 4096, min_value=0)

        self.GITHUB_FILE_CACHE_TTL = self._parse_int_env("GITHUB_FILE_CACHE_TTL", 60, min_value=0)
        self.GITHUB_FILE_CACHE_SIZE = self._parse_int_env("GITHUB_FILE_CACHE_SIZE", 4, min_value=0)

        # 群组验证配置（用于私聊模式下验证用户是否在群组中）
        required_group_id_raw = os.getenv("REQUIRED_GROUP_ID", "").strip()
        self.REQUIRED_GROUP_NAME = os.getenv("REQUIRED_GROUP_NAME", "").strip()
        self.REQUIRED_GROUP_LINK = os.getenv("REQUIRED_GROUP_LINK", "").strip()
        self.REQUIRED_GROUP_ID = self._parse_required_group_id(required_group_id_raw)
        self.GROUP_CHECK_ENABLED = bool(
            self.REQUIRED_GROUP_ID and self.REQUIRED_GROUP_NAME and self.REQUIRED_GROUP_LINK
        )
        if required_group_id_raw and not self.REQUIRED_GROUP_ID:
            logger.warning(f"无效的 REQUIRED_GROUP_ID: {required_group_id_raw}")
        if self.REQUIRED_GROUP_ID and not self.GROUP_CHECK_ENABLED:
            logger.warning("群组验证已关闭：REQUIRED_GROUP_NAME 或 REQUIRED_GROUP_LINK 未配置")
        
        # 群组工作模式配置（允许机器人在这些群组中直接响应 @提及）
        # 支持逗号分隔的多个群组 ID，例如：-1001234567890,-1009876543210
        self.ALLOWED_GROUP_IDS = self._parse_group_ids(os.getenv("ALLOWED_GROUP_IDS", ""))

        # 私聊成功提交后的群组播报（独立配置，避免改变现有群组行为）
        announcement_group_id_raw = os.getenv("ANNOUNCEMENT_GROUP_ID", "").strip()
        self.ANNOUNCEMENT_GROUP_ID = self._parse_required_group_id(
            announcement_group_id_raw
        )
        if announcement_group_id_raw and not self.ANNOUNCEMENT_GROUP_ID:
            logger.warning(f"无效的 ANNOUNCEMENT_GROUP_ID: {announcement_group_id_raw}")

        # 管理员配置（Telegram 用户 ID 列表）
        # 支持逗号分隔的多个用户 ID，例如：123456789,987654321
        self.ADMIN_USER_IDS = self._parse_user_ids(os.getenv("ADMIN_USER_IDS", ""))
        if self.ADMIN_USER_IDS:
            logger.info(f"已加载管理员 IDs: {self.ADMIN_USER_IDS}")
        else:
            logger.info("未配置管理员 IDs（ADMIN_USER_IDS）")
        
        # 数据源URL
        # 使用 Aethersailor GeoIP 数据库
        self.GEOIP_URLS = [
            "https://gcore.jsdelivr.net/gh/Aethersailor/geoip@release/Country-without-asn.mmdb",
            "https://testingcf.jsdelivr.net/gh/Aethersailor/geoip@release/Country-without-asn.mmdb",
            "https://raw.githubusercontent.com/Aethersailor/geoip/release/Country-without-asn.mmdb",
        ]
        self.CN_IPV4_URLS = [
            "https://raw.githubusercontent.com/Aethersailor/geoip/refs/heads/release/text/cn-ipv4.txt",
            "https://gcore.jsdelivr.net/gh/Aethersailor/geoip@release/text/cn-ipv4.txt",
            "https://testingcf.jsdelivr.net/gh/Aethersailor/geoip@release/text/cn-ipv4.txt",
        ]
        self.GEOSITE_URL = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/refs/heads/release/direct-list.txt"
        
        # DoH服务器配置
        # 用于A记录查询（使用国内服务器获得准确的中国IP）
        default_doh_servers = {
            "alibaba": "https://dns.alidns.com/dns-query",
            "tencent": "https://doh.pub/dns-query",
            "cloudflare": "https://cloudflare-dns.com/dns-query"
        }
        self.DOH_SERVERS = self._parse_doh_servers(
            os.getenv("DOH_SERVERS", ""),
            default_doh_servers
        )
        
        # 用于NS记录查询（使用国际服务器避免审查）
        default_ns_doh_servers = {
            "cloudflare": "https://cloudflare-dns.com/dns-query",
            "google": "https://dns.google/dns-query",
            "quad9": "https://dns.quad9.net/dns-query"
        }
        self.NS_DOH_SERVERS = self._parse_doh_servers(
            os.getenv("NS_DOH_SERVERS", ""),
            default_ns_doh_servers
        )
        
        # 数据更新间隔（秒）
        self.DATA_UPDATE_INTERVAL = self._parse_update_interval(
            os.getenv("DATA_UPDATE_INTERVAL", "")
        )
    
    def _get_env_required(self, key: str) -> str:
        """获取必需的环境变量"""
        value = os.getenv(key)
        if not value:
            raise ValueError(f"Required environment variable {key} is not set")
        return value

    def _parse_group_ids(self, ids_str: str) -> list:
        """解析群组 ID 列表
        
        Args:
            ids_str: 逗号分隔的群组 ID 字符串
            
        Returns:
            群组 ID 整数列表
        """
        if not ids_str.strip():
            return []

        group_ids = []
        for raw_id in ids_str.split(","):
            raw_id = raw_id.strip()
            if not raw_id:
                continue
            try:
                group_ids.append(int(raw_id))
            except ValueError:
                logger.warning(f"无效的 ALLOWED_GROUP_IDS: {raw_id}")
        return group_ids

    def _parse_user_ids(self, ids_str: str) -> list:
        """解析用户 ID 列表

        Args:
            ids_str: 逗号分隔的用户 ID 字符串

        Returns:
            用户 ID 整数列表
        """
        if not ids_str.strip():
            return []

        user_ids = []
        parts = [part for part in re.split(r"[,\s;]+", ids_str.strip()) if part]
        for raw_id in parts:
            if not raw_id:
                continue
            try:
                user_ids.append(int(raw_id))
            except ValueError:
                logger.warning(f"无效的 ADMIN_USER_IDS: {raw_id}")
        return user_ids

    def _parse_required_group_id(self, group_id_raw: str) -> Optional[int]:
        """解析必需群组 ID"""
        if not group_id_raw:
            return None
        try:
            return int(group_id_raw)
        except ValueError:
            return None

    def _parse_update_interval(self, value: str) -> int:
        """解析数据更新间隔（秒）"""
        default_interval = 6 * 60 * 60
        if not value:
            return default_interval
        try:
            interval = int(value)
            if interval <= 0:
                raise ValueError
            return interval
        except ValueError:
            logger.warning(f"无效的 DATA_UPDATE_INTERVAL: {value}，使用默认值 {default_interval}")
            return default_interval

    def _parse_doh_servers(self, value: str, defaults: Dict[str, str]) -> Dict[str, str]:
        """解析 DOH 服务器配置"""
        if not value.strip():
            return defaults

        servers: Dict[str, str] = {}
        parts = [item.strip() for item in value.split(",") if item.strip()]
        for index, part in enumerate(parts, 1):
            if "=" in part:
                name, url = part.split("=", 1)
                name = name.strip() or f"server{index}"
            else:
                name = f"server{index}"
                url = part

            url = url.strip()
            if not url.startswith("https://"):
                logger.warning(f"无效的 DOH 服务器地址（必须是 https://）: {url}")
                continue
            servers[name] = url

        if not servers:
            logger.warning("未解析到有效 DOH 服务器配置，使用默认值")
            return defaults

        return servers

    def _parse_int_env(
        self,
        key: str,
        default: int,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None
    ) -> int:
        raw = os.getenv(key, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            logger.warning(f"无效的 {key}: {raw}，使用默认值 {default}")
            return default
        if min_value is not None and value < min_value:
            logger.warning(f"{key} 小于最小值 {min_value}，使用默认值 {default}")
            return default
        if max_value is not None and value > max_value:
            logger.warning(f"{key} 大于最大值 {max_value}，使用默认值 {default}")
            return default
        return value
