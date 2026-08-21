"""
DNS 服务模块
使用 DoH (DNS over HTTPS) 查询域名解析
"""

import aiohttp
import asyncio
import base64
import time
from typing import Dict, List, Literal, Optional
import dns.edns
import dns.message
import dns.rcode
import dns.rdatatype
from loguru import logger

from ..utils.privacy import log_reference

from ..utils.cache import TTLCache
from ..utils.metrics import METRICS


class DNSService:
    """DNS 服务"""
    
    def __init__(
        self,
        doh_servers: Dict[str, str],
        ns_doh_servers: Dict[str, str] = None,
        cache_size: int = 1024,
        cache_ttl: int = 60,
        ns_cache_size: int = 512,
        ns_cache_ttl: int = 300,
        max_concurrency: int = 20,
        conn_limit: int = 30,
        conn_limit_per_host: int = 10,
        timeout_total: int = 10,
        timeout_connect: int = 3,
    ):
        self.doh_servers = doh_servers
        self.ns_doh_servers = ns_doh_servers or doh_servers
        self.session: Optional[aiohttp.ClientSession] = None
        self._a_cache = TTLCache(cache_size, cache_ttl)
        self._ns_cache = TTLCache(ns_cache_size, ns_cache_ttl)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._conn_limit = conn_limit
        self._conn_limit_per_host = conn_limit_per_host
        self._timeout_total = timeout_total
        self._timeout_connect = timeout_connect
        
    async def start(self):
        """启动 DNS 服务，初始化共享 Session"""
        if not self.session or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=self._conn_limit,
                limit_per_host=self._conn_limit_per_host,
                ttl_dns_cache=300,
                use_dns_cache=True
            )
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(
                    total=self._timeout_total,
                    connect=self._timeout_connect
                )
            )
            logger.info("DNS 服务已启动，Session 已初始化")

    async def close(self):
        """关闭 DNS 服务"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("DNS 服务已关闭，Session 已释放")
    
    async def query_a_record(self, domain: str, use_edns_china: bool = True) -> List[str]:
        """查询 A 记录，返回 IP 地址列表（并发查询所有 DoH 服务器）"""
        cache_key = (domain, use_edns_china)
        cached = self._a_cache.get(cache_key)
        if cached is not None:
            METRICS.inc("dns.cache.a.hit")
            return cached
        METRICS.inc("dns.cache.a.miss")
        start_ts = time.perf_counter()
        try:
            # 确保 Session 已启动
            if not self.session or self.session.closed:
                await self.start()

            # 构建 DNS 查询数据包
            query_data = self._build_dns_query(domain, use_edns_china)
            
            # 创建所有 DoH 服务器的查询任务
            tasks = []
            for server_name, server_url in self.doh_servers.items():
                task = asyncio.create_task(
                    self._perform_doh_query(server_name, server_url, query_data, self._parse_dns_response_a)
                )
                tasks.append(task)
            
            # 等待所有任务完成，并获取第一个成功的结果
            # 注意：这里我们使用 as_completed 来获取最快的结果
            for future in asyncio.as_completed(tasks):
                try:
                    ips = await future
                    if ips:
                        logger.debug(
                            "DoH 查询成功，domain_ref={}，获得 {} 个 IP",
                            log_reference(domain),
                            len(ips),
                        )
                        self._a_cache.set(cache_key, ips)
                        # 取消其他未完成的任务
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        METRICS.record_request(
                            "dns.query_a",
                            (time.perf_counter() - start_ts) * 1000,
                            success=True
                        )
                        return ips
                except Exception:
                    # 单个任务失败不影响其他任务
                    continue
            
            logger.warning(
                "所有 DoH 服务器查询域名均失败，domain_ref={}",
                log_reference(domain),
            )
            METRICS.record_request(
                "dns.query_a",
                (time.perf_counter() - start_ts) * 1000,
                success=False
            )
            return []
        except Exception as e:
            logger.error(
                "DNS 查询失败，domain_ref={}，error_type={}",
                log_reference(domain),
                type(e).__name__,
            )
            METRICS.record_request(
                "dns.query_a",
                (time.perf_counter() - start_ts) * 1000,
                success=False
            )
            return []
    
    async def query_ns_records(self, domain: str) -> List[str]:
        """查询 NS 记录，返回权威域名服务器列表（并发查询）"""
        cached = self._ns_cache.get(domain)
        if cached is not None:
            METRICS.inc("dns.cache.ns.hit")
            return cached
        METRICS.inc("dns.cache.ns.miss")
        start_ts = time.perf_counter()
        try:
            # 确保 Session 已启动
            if not self.session or self.session.closed:
                await self.start()

            # 构建 NS 查询数据包（不使用 EDNS 中国客户端，避免被过滤）
            query_data = self._build_dns_query(domain, False, record_type=2)  # NS 记录类型为 2
            
            # 创建所有 NS DoH 服务器的查询任务
            tasks = []
            for server_name, server_url in self.ns_doh_servers.items():
                task = asyncio.create_task(
                    self._perform_doh_query(server_name, server_url, query_data, self._parse_dns_response_ns)
                )
                tasks.append(task)
            
            # 等待最快的结果
            for future in asyncio.as_completed(tasks):
                try:
                    ns_servers = await future
                    if ns_servers:
                        logger.debug(
                            "DoH 查询 NS 记录成功，domain_ref={}",
                            log_reference(domain),
                        )
                        self._ns_cache.set(domain, ns_servers)
                        # 取消其他任务
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        METRICS.record_request(
                            "dns.query_ns",
                            (time.perf_counter() - start_ts) * 1000,
                            success=True
                        )
                        return ns_servers
                except Exception:
                    continue
            
            # DoH 查询失败时，尝试使用系统 DNS 作为备用
            logger.info(
                "DoH 查询 NS 记录失败，尝试系统 DNS，domain_ref={}",
                log_reference(domain),
            )
            ns_servers = await self._query_ns_system_dns(domain)
            if ns_servers:
                logger.debug(
                    "系统 DNS 查询 NS 记录成功，domain_ref={}",
                    log_reference(domain),
                )
                self._ns_cache.set(domain, ns_servers)
                METRICS.record_request(
                    "dns.query_ns",
                    (time.perf_counter() - start_ts) * 1000,
                    success=True
                )
                return ns_servers
            
            logger.warning(
                "所有 NS 记录查询方法均失败，domain_ref={}",
                log_reference(domain),
            )
            METRICS.record_request(
                "dns.query_ns",
                (time.perf_counter() - start_ts) * 1000,
                success=False
            )
            return []
            
        except Exception as e:
            logger.error(
                "NS 记录查询失败，domain_ref={}，error_type={}",
                log_reference(domain),
                type(e).__name__,
            )
            METRICS.record_request(
                "dns.query_ns",
                (time.perf_counter() - start_ts) * 1000,
                success=False
            )
            return []
    
    async def _query_ns_system_dns(self, domain: str) -> List[str]:
        """使用系统 DNS 查询 NS 记录作为备用方案"""
        try:
            ns_servers = await asyncio.to_thread(self._query_ns_system_dns_sync, domain)
            
            logger.debug(
                "系统 DNS 查询 NS 记录成功，domain_ref={}，获得 {} 个 NS 服务器",
                log_reference(domain),
                len(ns_servers),
            )
            return ns_servers
            
        except Exception as e:
            logger.warning(
                "系统 DNS 查询 NS 记录失败，domain_ref={}，error_type={}",
                log_reference(domain),
                type(e).__name__,
            )
            return []

    async def classify_domain_resolution(
        self, domain: str
    ) -> Literal["exists", "empty", "nxdomain", "unknown"]:
        """Distinguish a confirmed NXDOMAIN from a temporary resolver failure.

        The regular record helpers intentionally collapse empty answers and
        resolver failures to an empty list.  That is safe for location policy,
        but callers also need a terminal answer for names that multiple
        independent resolvers agree do not exist.
        """
        if not self.session or self.session.closed:
            await self.start()

        query_data = self._build_dns_query(domain, False)
        if not query_data:
            return "unknown"

        tasks = [
            asyncio.create_task(
                self._perform_doh_rcode_query(server_url, query_data)
            )
            for server_url in self.doh_servers.values()
        ]
        tasks.append(
            asyncio.create_task(
                asyncio.to_thread(self._query_system_dns_status_sync, domain)
            )
        )
        statuses = await asyncio.gather(*tasks, return_exceptions=True)

        outcomes = [
            status
            for status in statuses
            if isinstance(status, tuple)
            and len(status) == 2
            and isinstance(status[0], int)
            and isinstance(status[1], int)
        ]
        if any(
            rcode == dns.rcode.NOERROR and answer_count > 0
            for rcode, answer_count in outcomes
        ):
            return "exists"
        if sum(rcode == dns.rcode.NXDOMAIN for rcode, _ in outcomes) >= 2:
            logger.info(
                "多个独立解析器确认域名不存在，domain_ref={}",
                log_reference(domain),
            )
            return "nxdomain"
        if sum(
            rcode == dns.rcode.NOERROR and answer_count == 0
            for rcode, answer_count in outcomes
        ) >= 2:
            logger.info(
                "多个独立解析器确认域名没有地址记录，domain_ref={}",
                log_reference(domain),
            )
            return "empty"
        return "unknown"

    @staticmethod
    def _query_ns_system_dns_sync(domain: str) -> List[str]:
        """Run the blocking system resolver outside the event loop."""
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.timeout = 3
        resolver.lifetime = 6
        answers = resolver.resolve(domain, dns.rdatatype.NS)
        return [str(rdata).rstrip(".") for rdata in answers]
    
    def _build_dns_query(self, domain: str, use_edns_china: bool = True, record_type: int = 1) -> bytes:
        """构建 DNS 查询数据包"""
        try:
            query = dns.message.make_query(domain, record_type)
            if use_edns_china:
                query.use_edns(
                    payload=4096,
                    options=[dns.edns.ECSOption("219.0.0.0", 24, 0)],
                )
            return query.to_wire()
            
        except Exception as e:
            logger.error(
                "构建 DNS 查询包失败，domain_ref={}，error_type={}",
                log_reference(domain),
                type(e).__name__,
            )
            return b''
    
    async def _perform_doh_query(
        self,
        server_name: str,
        server_url: str,
        query_data: bytes,
        parser_func
    ) -> List[str]:
        """执行 DoH 查询通用方法"""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                async with self._semaphore:
                    encoded_query = base64.urlsafe_b64encode(query_data).decode().rstrip('=')
                    url = f"{server_url}?dns={encoded_query}"
                    
                    # 使用共享的session
                    async with self.session.get(
                        url,
                        headers={
                            'Accept': 'application/dns-message',
                            'User-Agent': 'Rule-Bot DNS Client/1.0'
                        }
                    ) as response:
                        if response.status == 200:
                            response_data = await response.read()
                            result = parser_func(response_data)
                            if result:
                                return result
                            # 如果解析结果为空但状态码200，可能是没有该记录，不一定是错误，但也重试一下
                        else:
                            # logger.warning(f"{server_name} HTTP error: {response.status}")
                            pass
                            
            except asyncio.CancelledError:
                raise # 允许被取消
            except Exception:
                # A single endpoint failure is expected; the caller races
                # several independent resolvers.
                pass
            
            # 如果不是最后一次尝试，等待一小会儿
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
        
        # 所有重试失败后抛出异常，以便外层捕捉
        raise Exception(f"{server_name} query failed after retries")

    async def _perform_doh_rcode_query(
        self, server_url: str, query_data: bytes
    ) -> Optional[tuple[int, int]]:
        """Return a DNS response code without treating NXDOMAIN as transport failure."""
        for attempt in range(2):
            try:
                async with self._semaphore:
                    encoded_query = base64.urlsafe_b64encode(query_data).decode().rstrip("=")
                    async with self.session.get(
                        f"{server_url}?dns={encoded_query}",
                        headers={
                            "Accept": "application/dns-message",
                            "User-Agent": "Rule-Bot DNS Client/1.0",
                        },
                    ) as response:
                        if response.status == 200:
                            message = dns.message.from_wire(await response.read())
                            answer_count = sum(len(rrset) for rrset in message.answer)
                            return message.rcode(), answer_count
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if attempt == 0:
                await asyncio.sleep(0.5)
        return None

    @staticmethod
    def _query_system_dns_status_sync(domain: str) -> Optional[tuple[int, int]]:
        """Return the system resolver's DNS status without exposing the name."""
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.timeout = 3
        resolver.lifetime = 6
        try:
            answer = resolver.resolve(
                domain, dns.rdatatype.A, raise_on_no_answer=False
            )
            return dns.rcode.NOERROR, len(answer)
        except dns.resolver.NXDOMAIN:
            return dns.rcode.NXDOMAIN, 0
        except dns.resolver.NoAnswer:
            return dns.rcode.NOERROR, 0
        except Exception:
            return None
    
    def _parse_dns_response_a(self, response_data: bytes) -> List[str]:
        """解析 DNS 响应中的 A 记录"""
        try:
            response = dns.message.from_wire(response_data)
            if response.rcode() != dns.rcode.NOERROR:
                return []
            return list(dict.fromkeys(
                rdata.address
                for rrset in response.answer
                if rrset.rdtype == dns.rdatatype.A
                for rdata in rrset
            ))
            
        except Exception as e:
            logger.error(f"解析 DNS 响应失败: {e}")
            return []
    
    def _parse_dns_response_ns(self, response_data: bytes) -> List[str]:
        """解析 DNS 响应中的 NS 记录"""
        try:
            response = dns.message.from_wire(response_data)
            if response.rcode() != dns.rcode.NOERROR:
                return []
            return list(dict.fromkeys(
                str(rdata.target).rstrip(".")
                for rrset in response.answer
                if rrset.rdtype == dns.rdatatype.NS
                for rdata in rrset
            ))
            
        except Exception as e:
            logger.error(f"解析 NS 记录失败: {e}")
            return []
