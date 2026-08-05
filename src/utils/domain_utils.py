"""
域名处理工具
"""

import re
import ipaddress
from typing import Optional
from urllib.parse import urlparse

from publicsuffixlist import PublicSuffixList


_PSL = PublicSuffixList(accept_unknown=False)


def extract_domain(url_or_domain: str) -> Optional[str]:
    """从URL或域名中提取域名"""
    try:
        # 清理输入
        domain = url_or_domain.strip().lower()
        
        # 移除协议前缀
        if domain.startswith(('http://', 'https://', 'ftp://', 'ftps://')):
            parsed = urlparse(domain)
            domain = parsed.hostname or parsed.netloc
        elif '://' in domain:
            # 其他协议
            domain = domain.split('://', 1)[1].split('/')[0]
        
        # 移除www前缀（如果存在）
        if domain.startswith('www.'):
            domain = domain[4:]
        
        # 移除端口号
        if ':' in domain:
            domain = domain.split(':')[0]
        
        # 移除路径、查询参数、锚点
        if '/' in domain:
            domain = domain.split('/')[0]
        if '?' in domain:
            domain = domain.split('?')[0]
        if '#' in domain:
            domain = domain.split('#')[0]
        
        # 移除前后空格和特殊字符
        domain = domain.strip(' \t\n\r\f\v.,;')

        # Telegram accepts Unicode domains. Convert them to the ASCII form used
        # by DNS and rule providers before validation.
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        
        # 验证域名格式
        if not is_valid_domain(domain):
            return None
        
        return domain
        
    except Exception:
        return None


def extract_second_level_domain(domain: str) -> Optional[str]:
    """提取二级域名 - 使用公共后缀规则"""
    try:
        if not domain:
            return None
        
        # 清理域名
        domain = domain.strip().lower()

        if not is_valid_domain(domain):
            return None

        registrable = _PSL.privatesuffix(domain)
        public_suffix = _PSL.publicsuffix(domain)
        if not registrable or not public_suffix or registrable == public_suffix:
            return None
        return registrable
        
    except Exception:
        return None

def is_valid_domain(domain: str) -> bool:
    """验证域名格式是否正确"""
    if not domain:
        return False
    
    domain = domain.rstrip(".")

    # 规则文件只接受可注册域名，不接受单标签主机名或 IP 地址。
    if len(domain) > 253 or "." not in domain:
        return False

    try:
        ipaddress.ip_address(domain)
        return False
    except ValueError:
        pass
    
    # 域名正则表达式
    pattern = r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$'
    
    return bool(re.match(pattern, domain))

def extract_second_level_domain_for_rules(url_or_domain: str) -> Optional[str]:
    """专门用于规则添加的二级域名提取"""
    try:
        # 先提取域名
        domain = extract_domain(url_or_domain)
        if not domain:
            return None
        
        # 检查是否为.cn域名
        if domain.endswith('.cn'):
            return None  # .cn域名不允许添加
        
        # 提取二级域名
        second_level = extract_second_level_domain(domain)
        if not second_level:
            return None
        
        # 再次检查二级域名是否为.cn
        if second_level.endswith('.cn'):
            return None  # .cn域名不允许添加
        
        return second_level
        
    except Exception:
        return None


def is_cn_domain(domain: str) -> bool:
    """检查是否为.cn域名"""
    try:
        if not domain:
            return False
        return domain.lower().endswith('.cn')
    except Exception:
        return False


def normalize_domain(domain: str) -> Optional[str]:
    """标准化域名"""
    extracted = extract_domain(domain)
    if not extracted:
        return None
    
    return extracted.lower().strip()
