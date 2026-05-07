"""
域名检查服务
综合检查域名的各种信息
"""

import asyncio
from typing import Dict, List, Any, Optional
from loguru import logger

from .dns_service import DNSService
from .geoip_service import GeoIPService
from ..utils.domain_utils import extract_second_level_domain, normalize_domain


class DomainChecker:
    """域名检查器"""
    
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
                "domain_china_status": False,
                "second_level_china_status": False,
                "ns_china_status": False,
                "recommendation": "",
                "details": []
            }
            
            # 1. 查询域名 IP
            logger.info(f"查询域名 {normalized_domain} 的 IP 地址...")
            domain_ip_task = asyncio.create_task(
                self.dns_service.query_a_record(normalized_domain)
            )
            second_level_ip_task = None
            if second_level and second_level != normalized_domain:
                logger.info(f"查询二级域名 {second_level} 的 IP 地址...")
                second_level_ip_task = asyncio.create_task(
                    self.dns_service.query_a_record(second_level)
                )

            domain_ips = await domain_ip_task
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
                second_level_ips = await second_level_ip_task
                result["second_level_ips"] = second_level_ips
                
                if second_level_ips:
                    china_ips = []
                    for ip in second_level_ips:
                        location = self.geoip_service.get_location_info(ip)
                        if location["is_china"]:
                            china_ips.append(ip)
                        result["details"].append(f"二级域名 IP {ip}: {location['country_name']}")
                    
                    result["second_level_china_status"] = len(china_ips) > 0
                    if china_ips:
                        result["details"].append(f"二级域名有 {len(china_ips)} 个中国 IP")
                else:
                    result["details"].append("无法解析二级域名 IP")
            
            # 3. 查询 NS 服务器
            ns_domain = second_level if second_level else normalized_domain
            logger.info(f"查询域名 {ns_domain} 的 NS 记录...")
            ns_servers = await self.dns_service.query_ns_records(ns_domain)
            result["ns_servers"] = ns_servers
            
            # 检查 NS 服务器 IP 归属地
            if ns_servers:
                china_ns_count = 0
                total_ns_count = 0
                ns_summary = {}  # {ns_server: {"china": count, "foreign": count}}
                
                ns_ip_results = await asyncio.gather(
                    *(self.dns_service.query_a_record(ns) for ns in ns_servers)
                )

                for ns, ns_ips in zip(ns_servers, ns_ip_results):
                    result["ns_ips"].extend(ns_ips)
                    
                    ns_summary[ns] = {"china": 0, "foreign": 0, "ips": []}
                    
                    for ip in ns_ips:
                        location = self.geoip_service.get_location_info(ip)
                        ns_summary[ns]["ips"].append({"ip": ip, "country": location['country_name']})
                        total_ns_count += 1
                        
                        if location["is_china"]:
                            china_ns_count += 1
                            ns_summary[ns]["china"] += 1
                        else:
                            ns_summary[ns]["foreign"] += 1
                
                # 生成简洁的 NS 摘要信息
                if china_ns_count > 0:
                    result["ns_china_status"] = True
                    result["details"].append(f"NS 服务器: {china_ns_count}/{total_ns_count} 个 IP 在中国大陆")
                else:
                    result["details"].append(f"NS 服务器: 0/{total_ns_count} 个 IP 在中国大陆")
                
                # 添加详细的 NS 服务器信息（handler 会统一添加 • 符号）
                for ns, summary in ns_summary.items():
                    china_count = summary["china"]
                    foreign_count = summary["foreign"]
                    # 优化显示：有中国 IP 显示完整信息，无海外 IP 时不显示 0
                    if china_count > 0 and foreign_count > 0:
                        result["details"].append(f"{ns}: {china_count} 个中国 IP + {foreign_count} 个海外 IP")
                    elif china_count > 0:
                        result["details"].append(f"{ns}: {china_count} 个中国 IP")
                    else:
                        result["details"].append(f"{ns}: {foreign_count} 个海外 IP")
            else:
                result["details"].append("无法查询到 NS 记录")
            
            # 生成建议
            result["recommendation"] = self._generate_recommendation(result)
            
            return result
            
        except Exception as e:
            logger.error(f"域名检查失败: {e}")
            return {"error": f"域名检查失败: {str(e)}"}
    
    def _generate_recommendation(self, check_result: Dict[str, Any]) -> str:
        """根据检查结果生成建议"""
        try:
            domain_china = check_result["domain_china_status"]
            second_level_china = check_result["second_level_china_status"]
            ns_china = check_result["ns_china_status"]
            
            # 决定添加哪个域名（始终使用二级域名）
            target_domain = check_result["second_level_domain"] if check_result["second_level_domain"] else check_result["normalized_domain"]
            domain_type = "二级域名"
            
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
    
    def should_ask_confirmation(self, check_result: Dict[str, Any]) -> bool:
        """判断是否需要用户确认"""
        try:
            # 根据新逻辑，不需要确认的情况，都是直接添加或直接拒绝
            return False
            
        except Exception:
            return False
    
    def should_reject(self, check_result: Dict[str, Any]) -> bool:
        """判断是否应该拒绝添加"""
        try:
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
                logger.warning(f"无效的 check_result: {check_result}")
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
            logger.error(f"get_target_domain_to_add 失败: {e}", exc_info=True)
            return None 
