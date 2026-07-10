"""
域名处理工具
"""

import re
import ipaddress
from typing import Optional
from urllib.parse import urlparse

try:
    from publicsuffixlist import PublicSuffixList
    _PSL = PublicSuffixList(accept_unknown=False)
except ImportError:
    _PSL = None


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

        if _PSL:
            registrable = _PSL.privatesuffix(domain)
            public_suffix = _PSL.publicsuffix(domain)
            if not registrable or not public_suffix or registrable == public_suffix:
                return None
            return registrable
        
        # 分割域名
        parts = domain.split('.')
        
        # 至少需要两个部分才能构成域名
        if len(parts) < 2:
            return None
        
        # 获取有效顶级域名长度
        tld_length = _get_tld_length(parts)
        
        # 检查是否有足够的部分构成二级域名
        if len(parts) <= tld_length:
            return domain  # 输入的就是顶级域名或二级域名
        
        # 返回二级域名：取顶级域名前的一个部分 + 顶级域名
        return '.'.join(parts[-(tld_length + 1):])
        
    except Exception:
        return None


def _get_tld_length(domain_parts: list) -> int:
    """获取顶级域名的长度（分段数）"""
    if len(domain_parts) < 2:
        return 1
    
    # 检查常见的复合顶级域名
    last_two = '.'.join(domain_parts[-2:])
    last_three = '.'.join(domain_parts[-3:]) if len(domain_parts) >= 3 else ""
    
    # 三段顶级域名（如 .com.au, .co.uk 等）
    three_part_tlds = {
        'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au', 'asn.au', 'id.au',
        'co.uk', 'org.uk', 'net.uk', 'ac.uk', 'gov.uk', 'police.uk',
        'com.br', 'net.br', 'org.br', 'edu.br', 'gov.br',
        'com.ar', 'net.ar', 'org.ar', 'edu.ar', 'gov.ar',
        'co.jp', 'or.jp', 'ne.jp', 'gr.jp', 'ac.jp', 'go.jp', 'ed.jp',
        'co.in', 'net.in', 'org.in', 'edu.in', 'gov.in', 'ac.in',
        'co.za', 'net.za', 'org.za', 'edu.za', 'gov.za', 'ac.za',
        'co.nz', 'net.nz', 'org.nz', 'edu.nz', 'govt.nz', 'ac.nz',
        'co.kr', 'net.kr', 'org.kr', 'edu.kr', 'gov.kr', 'ac.kr',
        'com.tw', 'net.tw', 'org.tw', 'edu.tw', 'gov.tw', 'idv.tw',
        'com.sg', 'net.sg', 'org.sg', 'edu.sg', 'gov.sg', 'per.sg',
        'com.my', 'net.my', 'org.my', 'edu.my', 'gov.my',
        'com.ph', 'net.ph', 'org.ph', 'edu.ph', 'gov.ph',
        'com.hk', 'net.hk', 'org.hk', 'edu.hk', 'gov.hk', 'idv.hk',
        'com.mo', 'net.mo', 'org.mo', 'edu.mo', 'gov.mo'
    }
    
    # 两段顶级域名（包括中国的）
    two_part_tlds = {
        # 中国相关
        'com.cn', 'net.cn', 'org.cn', 'edu.cn', 'gov.cn', 'ac.cn',
        'mil.cn', 'bj.cn', 'sh.cn', 'tj.cn', 'cq.cn', 'he.cn', 'sx.cn',
        'nm.cn', 'ln.cn', 'jl.cn', 'hl.cn', 'js.cn', 'zj.cn', 'ah.cn',
        'fj.cn', 'jx.cn', 'sd.cn', 'ha.cn', 'hb.cn', 'hn.cn', 'gd.cn',
        'gx.cn', 'hi.cn', 'sc.cn', 'gz.cn', 'yn.cn', 'xz.cn', 'sn.cn',
        'gs.cn', 'qh.cn', 'nx.cn', 'xj.cn', 'tw.cn', 'hk.cn', 'mo.cn',
        
        # 其他国家和地区
        'co.uk', 'me.uk', 'ltd.uk', 'plc.uk',
        'com.au', 'net.au', 'org.au', 'edu.au',
        'co.jp', 'or.jp', 'ne.jp', 'ac.jp',
        'com.br', 'net.br', 'org.br', 'edu.br',
        'co.in', 'net.in', 'org.in', 'edu.in',
        'co.za', 'net.za', 'org.za', 'edu.za',
        'co.kr', 'net.kr', 'org.kr', 'edu.kr',
        'com.tw', 'net.tw', 'org.tw', 'edu.tw',
        'com.sg', 'net.sg', 'org.sg', 'edu.sg',
        'com.my', 'net.my', 'org.my', 'edu.my',
        'com.hk', 'net.hk', 'org.hk', 'edu.hk',
        'com.mo', 'net.mo', 'org.mo', 'edu.mo',
        
        # 欧洲
        'com.eu', 'org.eu', 'net.eu', 'edu.eu',
        'co.de', 'com.de', 'org.de', 'net.de',
        'co.fr', 'com.fr', 'org.fr', 'net.fr',
        'co.it', 'com.it', 'org.it', 'net.it',
        'co.es', 'com.es', 'org.es', 'net.es',
        'co.nl', 'com.nl', 'org.nl', 'net.nl',
        
        # 美洲
        'com.mx', 'net.mx', 'org.mx', 'edu.mx',
        'com.ar', 'net.ar', 'org.ar', 'edu.ar',
        'com.co', 'net.co', 'org.co', 'edu.co',
        'com.pe', 'net.pe', 'org.pe', 'edu.pe',
        'com.cl', 'net.cl', 'org.cl', 'edu.cl',
        'com.ve', 'net.ve', 'org.ve', 'edu.ve',
        
        # 其他
        'com.tr', 'net.tr', 'org.tr', 'edu.tr',
        'com.ru', 'net.ru', 'org.ru', 'edu.ru',
        'com.ua', 'net.ua', 'org.ua', 'edu.ua'
    }
    
    # 检查三段TLD
    if len(domain_parts) >= 3 and last_three in three_part_tlds:
        return 3
    
    # 检查两段TLD  
    if len(domain_parts) >= 2 and last_two in two_part_tlds:
        return 2
    
    # 默认单段TLD
    return 1


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


def get_domain_levels(domain: str) -> list:
    """获取域名的各级"""
    if not domain:
        return []
    
    parts = domain.split('.')
    levels = []
    
    # 从二级域名开始构建各级域名
    for i in range(len(parts) - 1, 0, -1):
        level_domain = '.'.join(parts[i-1:])
        levels.append(level_domain)
    
    return levels


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


def is_subdomain_of(subdomain: str, parent_domain: str) -> bool:
    """检查是否为子域名"""
    if not subdomain or not parent_domain:
        return False
    
    subdomain = subdomain.lower().strip()
    parent_domain = parent_domain.lower().strip()
    
    if subdomain == parent_domain:
        return True
    
    return subdomain.endswith('.' + parent_domain) 
