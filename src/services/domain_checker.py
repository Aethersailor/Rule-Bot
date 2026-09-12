"""
域名检查服务
综合检查域名的各种信息
"""

import asyncio
from typing import Dict, Any, Optional
from loguru import logger

from .dns_service import DNSService
from .geoip_service import GeoIPService
from ..utils.domain_utils import extract_second_level_domain, normalize_domain
from ..utils.privacy import log_reference


class DomainChecker:
    """域名检查器"""

    NS_MIN_RESOLVER_ANSWERS = 2
    NS_MIN_CHINA_HOSTS = 2
    NS_CHINA_QUORUM_NUMERATOR = 2
    NS_CHINA_QUORUM_DENOMINATOR = 3
    
    def __init__(self, dns_service: DNSService, geoip_service: GeoIPService):
        self.dns_service = dns_service
        self.geoip_service = geoip_service
    
    async def check_domain_comprehensive(self, domain: str) -> Dict[str, Any]:
        """综合检查域名信息"""
        try:
            # 标准化域名
            normalized_domain = normalize_domain(domain)
            if not normalized_domain:
                return {"error": "无效的域名格式"}
            
            # 获取二级域名
            second_level = extract_second_level_domain(normalized_domain)
            
            result = {
                "original_domain": domain,
                "normalized_domain": normalized_domain,
                "second_level_domain": second_level,
                "domain_ips": [],
                "second_level_ips": [],
                "ns_servers": [],
                "ns_ips": [],
                "ns_resolved_hosts": 0,
                "ns_china_hosts": 0,
                "ns_mixed_hosts": 0,
                "ns_conflict_hosts": 0,
                "ns_incomplete_hosts": 0,
                "domain_china_status": False,
                "second_level_china_status": False,
                "ns_china_status": False,
                "lookup_status": "pending",
                "recommendation": "",
                "details": []
            }
            
            # 1. 查询域名 IP
            logger.info(
                "查询域名 IP 地址，domain_ref={}",
                log_reference(normalized_domain),
            )
            domain_ip_task = asyncio.create_task(
                self.dns_service.query_a_record(normalized_domain)
            )
            second_level_ip_task = None
            if second_level and second_level != normalized_domain:
                logger.info(
                    "查询二级域名 IP 地址，domain_ref={}",
                    log_reference(second_level),
                )
                second_level_ip_task = asyncio.create_task(
                    self.dns_service.query_a_record(second_level)
                )

            ns_domain = second_level if second_level else normalized_domain
            logger.info(
                "查询域名 NS 记录，domain_ref={}",
                log_reference(ns_domain),
            )
            ns_task = asyncio.create_task(
                self.dns_service.query_ns_records(ns_domain)
            )

            if second_level_ip_task:
                domain_ips, second_level_ips, ns_servers = await asyncio.gather(
                    domain_ip_task,
                    second_level_ip_task,
                    ns_task,
                )
            else:
                domain_ips, ns_servers = await asyncio.gather(
                    domain_ip_task,
                    ns_task,
                )
                second_level_ips = []

            result["domain_ips"] = domain_ips
            
            # 检查域名 IP 归属地
            if domain_ips:
                china_ips = []
                for ip in domain_ips:
                    location = self.geoip_service.get_location_info(ip)
                    if location["is_china"]:
                        china_ips.append(ip)
                    result["details"].append(f"域名 IP {ip}: {location['country_name']}")
                
                result["domain_china_status"] = len(china_ips) > 0
                if china_ips:
                    result["details"].append(f"域名有 {len(china_ips)} 个中国 IP")
            else:
                result["details"].append("无法解析域名 IP")
            
            # 2. 如果不是二级域名，查询二级域名 IP
            if second_level and second_level != normalized_domain:
                result["second_level_ips"] = second_level_ips
                
                if second_level_ips:
                    china_ips = []
                    for ip in second_level_ips:
                        location = self.geoip_service.get_location_info(ip)
                        if location["is_china"]:
                            china_ips.append(ip)
                        result["details"].append(f"可注册域名 IP {ip}: {location['country_name']}")
                    
                    result["second_level_china_status"] = len(china_ips) > 0
                    if china_ips:
                        result["details"].append(f"可注册域名有 {len(china_ips)} 个中国 IP")
                else:
                    result["details"].append("无法解析可注册域名 IP")
            
            # 3. 处理并发查询得到的 NS 服务器
            result["ns_servers"] = ns_servers
            
            # 检查 NS 服务器 IP 归属地
            if ns_servers:
                configured_resolvers = len(
                    getattr(self.dns_service, "doh_servers", {}) or {}
                )
                required_resolvers = min(
                    self.NS_MIN_RESOLVER_ANSWERS,
                    max(1, configured_resolvers),
                )
                ns_summary = {}
                
                ns_evidence_results = await asyncio.gather(
                    *(self._query_ns_address_evidence(ns) for ns in ns_servers)
                )

                for ns, evidence in zip(ns_servers, ns_evidence_results):
                    usable_evidence = {
                        resolver: ips
                        for resolver, ips in evidence.items()
                        if isinstance(ips, list) and ips
                    }
                    ns_ips = list(
                        dict.fromkeys(
                            ip
                            for ips in usable_evidence.values()
                            for ip in ips
                        )
                    )
                    result["ns_ips"].extend(ns_ips)
                    summary = {
                        "china": 0,
                        "foreign": 0,
                        "conflict": 0,
                        "ips": [],
                        "resolver_answers": len(usable_evidence),
                    }
                    ns_summary[ns] = summary

                    if len(usable_evidence) < required_resolvers:
                        result["ns_incomplete_hosts"] += 1
                        continue

                    result["ns_resolved_hosts"] += 1
                    for ip in ns_ips:
                        location = self.geoip_service.get_location_info(ip)
                        summary["ips"].append(
                            {"ip": ip, "country": location["country_name"]}
                        )
                        if self._is_strict_china_ip(ip, location):
                            summary["china"] += 1
                        elif location["is_china"]:
                            summary["conflict"] += 1
                        else:
                            summary["foreign"] += 1

                    if summary["china"] == len(ns_ips) and ns_ips:
                        result["ns_china_hosts"] += 1
                    elif summary["china"] > 0:
                        result["ns_mixed_hosts"] += 1
                    elif summary["conflict"] > 0:
                        result["ns_conflict_hosts"] += 1

                resolved_hosts = result["ns_resolved_hosts"]
                china_hosts = result["ns_china_hosts"]
                evidence_complete = (
                    result["ns_incomplete_hosts"] == 0
                    and resolved_hosts == len(ns_servers)
                )
                has_cluster_quorum = (
                    china_hosts >= self.NS_MIN_CHINA_HOSTS
                    and china_hosts * self.NS_CHINA_QUORUM_DENOMINATOR
                    >= resolved_hosts * self.NS_CHINA_QUORUM_NUMERATOR
                    and result["ns_mixed_hosts"] == 0
                    and result["ns_conflict_hosts"] == 0
                )
                result["ns_china_status"] = bool(
                    evidence_complete and resolved_hosts and has_cluster_quorum
                )

                result["details"].append(
                    "NS 严格判定: "
                    f"{china_hosts}/{resolved_hosts} 个主机在中国大陆"
                )
                if result["ns_conflict_hosts"]:
                    result["details"].append(
                        "NS 归属数据冲突: "
                        f"{result['ns_conflict_hosts']} 个主机"
                    )
                if result["ns_mixed_hosts"]:
                    result["details"].append(
                        "NS 国内外地址混合: "
                        f"{result['ns_mixed_hosts']} 个主机"
                    )
                if result["ns_incomplete_hosts"]:
                    result["details"].append(
                        "NS 多解析器证据不足: "
                        f"{result['ns_incomplete_hosts']} 个主机"
                    )
                
                # 添加详细的 NS 服务器信息（handler 会统一添加 • 符号）
                for ns, summary in ns_summary.items():
                    china_count = summary["china"]
                    foreign_count = summary["foreign"]
                    conflict_count = summary["conflict"]
                    if summary["resolver_answers"] < required_resolvers:
                        result["details"].append(f"{ns}: 多解析器证据不足")
                    elif conflict_count:
                        result["details"].append(
                            f"{ns}: {conflict_count} 个归属冲突 IP"
                        )
                    elif china_count > 0 and foreign_count > 0:
                        result["details"].append(
                            f"{ns}: {china_count} 个严格中国 IP + "
                            f"{foreign_count} 个海外 IP"
                        )
                    elif china_count > 0:
                        result["details"].append(
                            f"{ns}: {china_count} 个严格中国 IP"
                        )
                    else:
                        result["details"].append(f"{ns}: {foreign_count} 个海外 IP")
            else:
                result["details"].append("无法查询到 NS 记录")

            # Do not turn a resolver outage or an unresolvable domain into a
            # confident "foreign" verdict. At least one address must have
            # been observed before location-based policy can be applied.
            observed_ips = (
                result["domain_ips"]
                + result["second_level_ips"]
                + result["ns_ips"]
            )
            if not observed_ips:
                classify = getattr(
                    self.dns_service, "classify_domain_resolution", None
                )
                resolution_status = (
                    await classify(second_level or normalized_domain)
                    if classify is not None
                    else "unknown"
                )
                if resolution_status == "nxdomain":
                    result["lookup_status"] = "nxdomain"
                    result["error_code"] = "nxdomain"
                    result["error"] = "域名不存在"
                    result["recommendation"] = "⚠️ 域名不存在，无法加入规则"
                elif resolution_status == "empty":
                    result["lookup_status"] = "empty"
                    result["error_code"] = "empty_dns"
                    result["error"] = "域名没有可用于归属判断的地址记录"
                    result["recommendation"] = "⚠️ 域名没有地址记录，不加入规则"
                else:
                    result["lookup_status"] = "unknown"
                    result["error"] = "暂时无法获取有效的 DNS 地址数据，请稍后重试"
                    result["recommendation"] = "⚠️ 当前无法可靠判断域名归属，请稍后重试"
                return result

            result["lookup_status"] = "ok"
            
            # 生成建议
            result["recommendation"] = self._generate_recommendation(result)
            
            return result
            
        except Exception as e:
            logger.error(
                "域名检查失败，domain_ref={}，error_type={}",
                log_reference(domain),
                type(e).__name__,
            )
            return {"error": f"域名检查失败: {str(e)}"}

    async def _query_ns_address_evidence(self, ns: str) -> Dict[str, list]:
        query_evidence = getattr(
            self.dns_service, "query_a_record_evidence", None
        )
        if callable(query_evidence):
            evidence = await query_evidence(ns)
            return evidence if isinstance(evidence, dict) else {}
        ips = await self.dns_service.query_a_record(ns)
        return {"legacy": ips} if ips else {}

    def _is_strict_china_ip(self, ip: str, location: Dict[str, Any]) -> bool:
        strict_check = getattr(
            self.geoip_service, "is_strict_china_ip", None
        )
        if callable(strict_check):
            try:
                return bool(strict_check(ip))
            except Exception:
                return False
        return bool(location.get("is_china"))
    
    def _generate_recommendation(self, check_result: Dict[str, Any]) -> str:
        """根据检查结果生成建议"""
        try:
            domain_china = check_result["domain_china_status"]
            second_level_china = check_result["second_level_china_status"]
            ns_china = check_result["ns_china_status"]
            
            # 决定添加哪个域名（始终使用二级域名）
            target_domain = check_result["second_level_domain"] if check_result["second_level_domain"] else check_result["normalized_domain"]
            domain_type = "可注册域名"
            
            # 判断是否有中国 IP（优先二级域名 IP）
            has_china_ip = second_level_china or domain_china

            # 根据检查结果生成建议
            if has_china_ip:
                return f"✅ 添加{domain_type} {target_domain}：域名 IP 在中国大陆"
            elif ns_china:
                return f"✅ 添加{domain_type} {target_domain}：NS 服务器在中国大陆"
            else:
                return f"❌ 不建议添加{domain_type} {target_domain}：域名 IP 和 NS 服务器都不在中国大陆"
                    
        except Exception as e:
            logger.error(f"生成建议失败: {e}")
            return "无法生成建议"
    
    def should_add_directly(self, check_result: Dict[str, Any]) -> bool:
        """判断是否应该直接添加（无需用户确认）"""
        try:
            if check_result.get("error") or check_result.get("lookup_status") != "ok":
                return False
            domain_china = check_result["domain_china_status"]
            second_level_china = check_result["second_level_china_status"]
            ns_china = check_result["ns_china_status"]
            
            # 域名 IP 在中国大陆，或者 IP 不在中国但 NS 在中国，都直接添加
            has_china_ip = domain_china or second_level_china
            if has_china_ip:
                return True
            if not has_china_ip and ns_china:
                return True
            return False
            
        except Exception:
            return False
    
    def should_reject(self, check_result: Dict[str, Any]) -> bool:
        """判断是否应该拒绝添加"""
        try:
            if check_result.get("error") or check_result.get("lookup_status") != "ok":
                return False
            domain_china = check_result["domain_china_status"]
            second_level_china = check_result["second_level_china_status"]
            ns_china = check_result["ns_china_status"]
            
            # 域名 IP 和 NS 都不在中国的情况拒绝添加
            has_china_ip = domain_china or second_level_china
            return (not has_china_ip and not ns_china)
            
        except Exception:
            return False
    
    def get_target_domain_to_add(self, check_result: Dict[str, Any]) -> Optional[str]:
        """获取应该添加的目标域名（始终返回二级域名）"""
        try:
            # 检查 check_result 是否有效
            if not check_result or not isinstance(check_result, dict):
                logger.warning("无效的域名检查结果，原始内容不写入日志")
                return None
            if check_result.get("error") or check_result.get("lookup_status") != "ok":
                return None
            
            # 安全获取值，提供默认值
            second_level_domain = check_result.get("second_level_domain")
            normalized_domain = check_result.get("normalized_domain")
            domain_china = check_result.get("domain_china_status", False)
            second_level_china = check_result.get("second_level_china_status", False)
            ns_china = check_result.get("ns_china_status", False)
            
            # 确保布尔值类型
            domain_china = bool(domain_china)
            second_level_china = bool(second_level_china)
            ns_china = bool(ns_china)
            
            # 始终使用二级域名
            target_domain = second_level_domain if second_level_domain else normalized_domain
            
            # 检查是否应该添加
            has_china_ip = domain_china or second_level_china
            if has_china_ip or ns_china:
                return target_domain
            
            return None
            
        except Exception as e:
            logger.error(
                "获取目标域名失败，error_type={}（异常正文不写入日志）",
                type(e).__name__,
            )
            return None 
