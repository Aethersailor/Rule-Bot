"""
数据管理模块
负责下载和管理 GeoIP、GeoSite 数据
"""

import asyncio
import aiohttp
import hashlib
import ipaddress
import json
import re
import threading
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Set, List, Pattern, Optional, Tuple, Dict, Any, Awaitable, Callable
from loguru import logger

from .config import Config
from .utils.cache import TTLCache
from .utils.metrics import METRICS
from .utils.memory import trim_memory


MAX_DOWNLOAD_BYTES = {
    "geoip": 64 * 1024 * 1024,
    "cn_ipv4": 16 * 1024 * 1024,
    "geosite": 64 * 1024 * 1024,
}


class DataManager:
    """数据管理器"""
    
    def __init__(self, config: Config):
        self.config = config
        self.geosite_domains: Set[str] = set()
        self.geosite_keywords: List[str] = []
        self.geosite_regex_patterns: List[Pattern[str]] = []
        self.geosite_includes: List[str] = []
        self._data_lock = threading.RLock()
        self._update_lock = threading.Lock()
        self._scheduler_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._update_callbacks: List[Callable[[Dict[str, bool]], Awaitable[None]]] = []
        self._geosite_cache = TTLCache(
            config.GEOSITE_CACHE_SIZE,
            config.GEOSITE_CACHE_TTL
        )
        self._geosite_stamp: Optional[Tuple[int, int]] = None
        # 默认使用容器内目录，不强制持久化
        self.data_dir = self._resolve_data_dir()
        logger.info("数据目录: {}", self.data_dir)
        self.geoip_file = self.data_dir / "geoip" / "Country-without-asn.mmdb"
        self.cn_ipv4_file = self.data_dir / "geoip" / "cn-ipv4.txt"
        self.geosite_file = self.data_dir / "geosite" / "direct-list.txt"
        self.geoip_meta = self.geoip_file.with_suffix(self.geoip_file.suffix + ".meta.json")
        self.cn_ipv4_meta = self.cn_ipv4_file.with_suffix(self.cn_ipv4_file.suffix + ".meta.json")
        self.geosite_meta = self.geosite_file.with_suffix(self.geosite_file.suffix + ".meta.json")
        
        # 确保目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "geoip").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "geosite").mkdir(parents=True, exist_ok=True)

    def _resolve_data_dir(self) -> Path:
        """解析数据目录（默认容器内路径，必要时回退到临时目录）"""
        configured = (self.config.DATA_DIR or "").strip()
        if configured:
            path = Path(configured)
            path.mkdir(parents=True, exist_ok=True)
            return path

        preferred = Path("/app/data")
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            return preferred
        except Exception as e:
            logger.warning("默认数据目录不可用: {} ({})，改用临时目录", preferred, e)

        fallback = Path(tempfile.gettempdir()) / "rule-bot"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        connector = aiohttp.TCPConnector(
            limit=4,
            limit_per_host=2,
            ttl_dns_cache=300,
            use_dns_cache=True
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=60, connect=10)
        )
        return self._session

    async def close(self):
        """关闭后台任务与共享 Session"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                # 预期中的异常：关闭时取消后台调度任务，无需向上传播
                logger.debug("后台调度任务在关闭过程中被取消")
        self._scheduler_task = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def register_update_callback(
        self,
        callback: Callable[[Dict[str, bool]], Awaitable[None]],
    ) -> None:
        """Register a listener that reloads services after atomic data updates."""
        if callback not in self._update_callbacks:
            self._update_callbacks.append(callback)

    async def _notify_updates(self, changes: Dict[str, bool]) -> None:
        if not any(changes.values()):
            return
        for callback in list(self._update_callbacks):
            try:
                await callback(changes)
            except Exception as e:
                logger.error("数据更新回调失败: {}", e)
    
    async def initialize(self):
        """初始化数据管理器"""
        try:
            # 初始下载数据
            await self._download_initial_data()
            
            # 启动定时更新任务
            self._start_scheduled_updates()
            
            logger.info("数据管理器初始化完成")
            
        except Exception as e:
            logger.error(f"数据管理器初始化失败: {e}")
            await self.close()
            raise
    
    async def _download_initial_data(self):
        """初始下载数据"""
        try:
            # 启动时先验证持久化数据；即使文件仍在更新周期内，损坏数据也必须重下。
            inspections = await asyncio.gather(
                asyncio.to_thread(
                    self._inspect_existing_data,
                    self.geoip_file,
                    "geoip",
                    self.geoip_meta,
                ),
                asyncio.to_thread(
                    self._inspect_existing_data,
                    self.cn_ipv4_file,
                    "cn_ipv4",
                    self.cn_ipv4_meta,
                ),
                asyncio.to_thread(
                    self._inspect_existing_data,
                    self.geosite_file,
                    "geosite",
                    self.geosite_meta,
                ),
            )
            existing_valid = {
                label: inspection[0]
                for label, inspection in zip(
                    ("geoip", "cn_ipv4", "geosite"), inspections
                )
            }
            need_geoip = not existing_valid["geoip"] or self._is_file_outdated(
                self.geoip_file,
                self.config.DATA_UPDATE_INTERVAL
            )
            need_cn_ipv4 = not existing_valid["cn_ipv4"] or self._is_file_outdated(
                self.cn_ipv4_file,
                self.config.DATA_UPDATE_INTERVAL
            )
            need_geosite = not existing_valid["geosite"] or self._is_file_outdated(
                self.geosite_file,
                self.config.DATA_UPDATE_INTERVAL
            )
            
            requests = []
            labels = []
            if need_geoip:
                logger.info("下载 GeoIP 数据...")
                labels.append(("geoip", self.geoip_file))
                requests.append(self._download_geoip())
            if need_cn_ipv4:
                logger.info("下载中国 IPv4 CIDR 数据...")
                labels.append(("cn_ipv4", self.cn_ipv4_file))
                requests.append(self._download_cn_ipv4())
            if need_geosite:
                logger.info("下载 GeoSite 数据...")
                labels.append(("geosite", self.geosite_file))
                requests.append(self._download_geosite())

            changes = {"geoip": False, "cn_ipv4": False, "geosite": False}
            if requests:
                results = await asyncio.gather(*requests, return_exceptions=True)
                missing_failures = []
                for (label, path), result in zip(labels, results):
                    if isinstance(result, BaseException):
                        if existing_valid[label]:
                            logger.warning("{} 更新失败，继续使用现有数据: {}", label, result)
                        else:
                            missing_failures.append(f"{label}: {result}")
                    else:
                        changes[label] = bool(result)
                if missing_failures:
                    raise RuntimeError("; ".join(missing_failures))

            geoip_changed = changes["geoip"]
            cn_ipv4_changed = changes["cn_ipv4"]
            geosite_changed = changes["geosite"]
            
            # 加载 GeoSite 数据到内存
            await self._load_geosite_data(force=True)
            if geosite_changed or geoip_changed or cn_ipv4_changed:
                trim_memory("初始化后内存修剪")
            
        except Exception as e:
            logger.error(f"初始数据下载失败: {e}")
            raise
    
    async def _download_geoip(self):
        """下载 GeoIP 数据"""
        try:
            return await self._download_with_fallback(
                self.config.GEOIP_URLS,
                self.geoip_file,
                "geoip",
                self.geoip_meta
            )
        except Exception as e:
            logger.error(f"GeoIP 数据下载失败: {e}")
            raise

    async def _download_cn_ipv4(self):
        """下载中国 IPv4 CIDR 数据"""
        try:
            return await self._download_with_fallback(
                self.config.CN_IPV4_URLS,
                self.cn_ipv4_file,
                "cn_ipv4",
                self.cn_ipv4_meta
            )
        except Exception as e:
            logger.error(f"中国 IPv4 CIDR 数据下载失败: {e}")
            raise
    
    async def _download_geosite(self):
        """下载 GeoSite 数据"""
        try:
            return await self._download_with_fallback(
                [self.config.GEOSITE_URL],
                self.geosite_file,
                "geosite",
                self.geosite_meta
            )
        except Exception as e:
            logger.error(f"GeoSite 数据下载失败: {e}")
            raise
    
    async def _load_geosite_data(self, force: bool = False):
        """加载 GeoSite 数据到内存"""
        try:
            if not self.geosite_file.exists():
                logger.warning("GeoSite 文件不存在，跳过加载")
                return

            try:
                stat = self.geosite_file.stat()
                stamp = (int(stat.st_mtime_ns), int(stat.st_size))
            except Exception:
                stamp = None

            if not force and stamp and self._geosite_stamp == stamp:
                logger.info("GeoSite 文件未变化，跳过加载")
                return
            
            logger.info("加载 GeoSite 数据到内存...")
            domains, keywords, regex_patterns, includes = await asyncio.to_thread(
                self._parse_geosite_file
            )
            
            with self._data_lock:
                self.geosite_domains = domains
                self.geosite_keywords = keywords
                self.geosite_regex_patterns = regex_patterns
                self.geosite_includes = includes
                if stamp:
                    self._geosite_stamp = stamp
                self._geosite_cache.clear()
            
            logger.info(
                "GeoSite 数据加载完成，域名: {}, 关键字: {}, 正则: {}, include: {}",
                len(domains),
                len(keywords),
                len(regex_patterns),
                len(includes)
            )
            if includes:
                logger.warning("检测到 GeoSite include 规则，当前未展开解析")
            
        except Exception as e:
            logger.error(f"GeoSite 数据加载失败: {e}")
            raise

    def _parse_geosite_file(
        self,
    ) -> tuple[Set[str], List[str], List[Pattern[str]], List[str]]:
        """Parse the potentially large GeoSite file outside the event loop."""
        domains: Set[str] = set()
        keywords: List[str] = []
        regex_patterns: List[Pattern[str]] = []
        includes: List[str] = []

        with self.geosite_file.open("r", encoding="utf-8") as handle:
            for line_num, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if line and not line.startswith("#"):
                    domain = ""
                    if line.startswith("full:"):
                        domain = line[5:]
                    elif line.startswith("domain:"):
                        domain = line[7:]
                    elif line.startswith("keyword:"):
                        keyword = line[8:].strip()
                        if keyword:
                            keywords.append(keyword.lower())
                    elif line.startswith("regexp:"):
                        pattern = line[7:].strip()
                        if pattern:
                            try:
                                regex_patterns.append(
                                    re.compile(pattern, re.IGNORECASE)
                                )
                            except re.error as error:
                                logger.warning(
                                    "无效的 GeoSite 正则（正文不写入日志）: {}",
                                    type(error).__name__,
                                )
                    elif line.startswith("include:") or line.startswith("geosite:"):
                        include_item = line.split(":", 1)[1].strip()
                        if include_item:
                            includes.append(include_item)
                    else:
                        domain = line

                    if domain:
                        domains.add(domain.lower())

                if line_num % 10000 == 0:
                    logger.info("已处理 {} 行 GeoSite 数据", line_num)

        return domains, keywords, regex_patterns, includes
    
    async def is_domain_in_geosite(self, domain: str) -> bool:
        """检查域名是否在 GeoSite 中"""
        try:
            domain = domain.lower().strip()
            if not domain:
                return False

            cached = self._geosite_cache.get(domain)
            if cached is not None:
                METRICS.inc("geosite.cache.hit")
                return cached
            METRICS.inc("geosite.cache.miss")

            with self._data_lock:
                domain_set = self.geosite_domains
                keywords = self.geosite_keywords
                regex_patterns = self.geosite_regex_patterns
            
            # 1. 直接检查完整域名
            if domain in domain_set:
                self._geosite_cache.set(domain, True)
                return True
            
            # 2. 检查是否为 GeoSite 中域名的子域名
            # 例如：查询 sub.example.com，检查 example.com 是否在 GeoSite 中
            parts = domain.split('.')
            for i in range(1, len(parts)):
                parent_domain = '.'.join(parts[i:])
                if parent_domain in domain_set:
                    self._geosite_cache.set(domain, True)
                    return True

            for keyword in keywords:
                if keyword and keyword in domain:
                    self._geosite_cache.set(domain, True)
                    return True

            for pattern in regex_patterns:
                if pattern.search(domain):
                    self._geosite_cache.set(domain, True)
                    return True
            
            # 注意：不做反向检查，因为 GeoSite 通常只包含具体域名，不需要检查子域名覆盖父域名的情况
            
            self._geosite_cache.set(domain, False)
            return False
            
        except Exception as e:
            logger.error(f"检查 GeoSite 域名失败: {e}")
            return False
    
    def _is_file_outdated(self, file_path: Path, max_age_seconds: int) -> bool:
        """检查文件是否过期"""
        if not file_path.exists():
            return True
        
        file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
        return datetime.now() - file_time > timedelta(seconds=max_age_seconds)
    
    def _start_scheduled_updates(self):
        """启动定时更新任务"""
        if self._scheduler_task and not self._scheduler_task.done():
            logger.info("定时更新任务已在运行")
            return
        self._scheduler_task = asyncio.create_task(self._scheduled_update_loop())
        logger.info("定时更新任务已启动")

    async def _scheduled_update_loop(self):
        """异步定时更新循环（与主事件循环一致）"""
        update_interval = self.config.DATA_UPDATE_INTERVAL
        while True:
            try:
                await asyncio.sleep(update_interval)
                await self._update_data_guarded()
            except asyncio.CancelledError:
                logger.info("定时更新任务已停止")
                raise
            except Exception as e:
                logger.error(f"定时更新循环异常: {e}")
                await asyncio.sleep(1)

    async def _update_data_guarded(self):
        """带并发保护的数据更新"""
        if not self._update_lock.acquire(blocking=False):
            logger.info("已有更新任务在执行，跳过本次更新")
            return
        try:
            await self._update_data()
        finally:
            self._update_lock.release()
    
    async def _update_data(self):
        """更新数据"""
        try:
            logger.info("开始定时更新数据...")
            
            # 数据源彼此独立；单个源失败不应阻止其他数据更新。
            results = await asyncio.gather(
                self._download_geoip(),
                self._download_cn_ipv4(),
                self._download_geosite(),
                return_exceptions=True,
            )
            changes = {"geoip": False, "cn_ipv4": False, "geosite": False}
            for label, result in zip(changes, results):
                if isinstance(result, BaseException):
                    logger.warning("{} 定时更新失败，保留现有数据: {}", label, result)
                else:
                    changes[label] = bool(result)

            geoip_changed = changes["geoip"]
            cn_ipv4_changed = changes["cn_ipv4"]
            geosite_changed = changes["geosite"]
            
            # 重新加载 GeoSite 数据（仅文件变化时）
            if geosite_changed:
                await self._load_geosite_data()
                trim_memory("geosite 更新后内存修剪")
            elif geoip_changed or cn_ipv4_changed:
                trim_memory("数据更新后内存修剪")

            await self._notify_updates(changes)
            
            logger.info("定时更新完成")
            
        except Exception as e:
            logger.error(f"定时更新失败: {e}") 
    
    def _load_meta(self, meta_path: Path) -> Dict[str, Any]:
        if not meta_path.exists():
            return {}
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save_meta(self, meta_path: Path, meta: Dict[str, Any]) -> None:
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=2)
            tmp_path.replace(meta_path)
        except Exception as e:
            logger.debug(f"保存 meta 失败: {e}")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _inspect_existing_data(
        self,
        path: Path,
        label: str,
        meta_path: Path,
    ) -> tuple[bool, Optional[str], Optional[int]]:
        """验证现有数据的格式及持久化 hash，供启动和条件请求复用。"""
        if not path.exists():
            return False, None, None
        try:
            metric = self._validate_download(path, label)
            digest = self._sha256_file(path)
            expected_hash = self._load_meta(meta_path).get("sha256")
            if expected_hash and digest != expected_hash:
                raise ValueError("文件 hash 与 meta 不一致")
            return True, digest, metric
        except Exception as e:
            logger.warning("{} 现有数据校验失败，将尝试重新下载: {}", label, e)
            return False, None, None

    @staticmethod
    def _validate_download(path: Path, label: str) -> int:
        """Reject truncated or wrong-content upstream responses before replace."""
        if label == "geoip":
            size = path.stat().st_size
            if size < 100_000:
                raise ValueError("GeoIP 文件过小")
            import maxminddb

            reader = maxminddb.open_database(str(path))
            reader.close()
            return size

        if label == "cn_ipv4":
            valid = 0
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    network = ipaddress.ip_network(line, strict=False)
                    if not isinstance(network, ipaddress.IPv4Network):
                        raise ValueError("中国 IPv4 CIDR 数据包含非 IPv4 条目")
                    valid += 1
            if valid < 100:
                raise ValueError("中国 IPv4 CIDR 数据有效条目不足")
            return valid

        if label == "geosite":
            valid = 0
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if len(line) > 4096 or "\x00" in line:
                        raise ValueError("GeoSite 数据包含异常长行或空字节")

                    prefix, separator, value = line.partition(":")
                    if separator and prefix in {
                        "full",
                        "domain",
                        "keyword",
                        "regexp",
                        "include",
                        "geosite",
                    }:
                        value = value.strip()
                        if not value:
                            raise ValueError(f"GeoSite {prefix} 规则内容为空")
                        if prefix == "regexp":
                            re.compile(value)
                        elif prefix in {"full", "domain"}:
                            DataManager._validate_geosite_domain(value)
                    else:
                        DataManager._validate_geosite_domain(line)
                    valid += 1
            if valid < 100:
                raise ValueError("GeoSite 数据有效条目不足")
            return valid

        raise ValueError(f"未知的数据类型: {label}")

    @staticmethod
    def _validate_geosite_domain(value: str) -> None:
        """做宽松格式校验，拒绝 HTML/JSON 等明显错误响应。"""
        if (
            not value
            or any(character.isspace() for character in value)
            or any(character in value for character in '<>{}[]"\'\\/')
            or ":" in value
        ):
            raise ValueError("GeoSite 数据包含非域名格式条目")

    @staticmethod
    def _validate_conservative_shrink(
        label: str,
        new_metric: int,
        old_metric: Optional[int],
    ) -> None:
        """已有有效基线时，拒绝一次缩减超过一半的可疑上游数据。"""
        if old_metric and new_metric * 2 < old_metric:
            raise ValueError(
                f"{label} 数据规模异常缩减: {old_metric} -> {new_metric}"
            )

    async def _download_with_fallback(
        self,
        urls: List[str],
        dest_path: Path,
        label: str,
        meta_path: Path
    ) -> bool:
        """按顺序尝试多个 URL 下载数据，支持条件更新和变更检测"""
        last_error = None
        session = await self._get_session()
        current_meta = self._load_meta(meta_path)
        existing_valid, existing_hash, existing_metric = await asyncio.to_thread(
            self._inspect_existing_data,
            dest_path,
            label,
            meta_path,
        )
        max_bytes = MAX_DOWNLOAD_BYTES[label]

        for url in urls:
            try:
                # ETag/Last-Modified 只属于生成它们的 URL，不能跨镜像复用。
                headers = {}
                if existing_valid and current_meta.get("source") == url:
                    if current_meta.get("etag"):
                        headers["If-None-Match"] = current_meta["etag"]
                    if current_meta.get("last_modified"):
                        headers["If-Modified-Since"] = current_meta["last_modified"]

                while True:
                    start_ts = time.perf_counter()
                    async with session.get(url, headers=headers) as response:
                        if response.status == 304:
                            valid_now, hash_now, metric_now = await asyncio.to_thread(
                                self._inspect_existing_data,
                                dest_path,
                                label,
                                meta_path,
                            )
                            if valid_now:
                                logger.info("{} 数据未更新（304）: {}", label, url)
                                METRICS.record_request(
                                    f"data.download.{label}",
                                    (time.perf_counter() - start_ts) * 1000,
                                    success=True
                                )
                                return False
                            if headers:
                                logger.warning(
                                    "{} 收到 304 但本地数据已损坏，改为无条件重新下载",
                                    label,
                                )
                                headers = {}
                                existing_valid = False
                                existing_hash = hash_now
                                existing_metric = metric_now
                                continue
                            last_error = "服务器在无条件请求中返回 304"
                            break
                        if response.status != 200:
                            last_error = f"下载失败，状态码: {response.status}"
                            logger.warning("{} 数据下载失败: {} (状态码: {})", label, url, response.status)
                            break

                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            try:
                                declared_size = int(content_length)
                            except ValueError as error:
                                raise ValueError("下载响应的 Content-Length 无效") from error
                            if declared_size < 0 or declared_size > max_bytes:
                                raise ValueError(
                                    f"{label} 下载响应超过 {max_bytes} 字节上限"
                                )

                        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
                        digest = hashlib.sha256()
                        size = 0
                        with tmp_path.open("wb") as handle:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                handle.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                                if size > max_bytes:
                                    raise ValueError(
                                        f"{label} 下载响应超过 {max_bytes} 字节上限"
                                    )

                        new_metric = await asyncio.to_thread(
                            self._validate_download, tmp_path, label
                        )
                        if existing_valid:
                            self._validate_conservative_shrink(
                                label,
                                new_metric,
                                existing_metric,
                            )

                        new_hash = digest.hexdigest()
                        changed = not existing_valid or existing_hash != new_hash
                        if changed:
                            tmp_path.replace(dest_path)
                        else:
                            tmp_path.unlink(missing_ok=True)

                        meta = {
                            "etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified"),
                            "sha256": new_hash,
                            "size": size,
                            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "source": url
                        }
                        self._save_meta(meta_path, meta)

                        if changed:
                            logger.info("{} 数据下载完成: {}", label, url)
                        else:
                            logger.info("{} 数据未变化（hash 相同）: {}", label, url)
                        METRICS.record_request(
                            f"data.download.{label}",
                            (time.perf_counter() - start_ts) * 1000,
                            success=True
                        )
                        return changed
            except Exception as e:
                try:
                    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
                    tmp_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    logger.debug("清理临时下载文件失败 {}: {}", tmp_path, cleanup_error)
                last_error = str(e)
                logger.warning("{} 数据下载失败: {} ({})", label, url, e)

        METRICS.record_request(
            f"data.download.{label}",
            0.0,
            success=False
        )
        raise Exception(f"{label} 数据下载失败: {last_error or '所有地址不可用'}")
