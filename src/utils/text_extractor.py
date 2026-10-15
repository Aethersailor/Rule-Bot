"""
文本域名提取工具
从任意文本中智能提取域名
"""

import re
from typing import Optional
from .domain_utils import normalize_domain, is_valid_domain, extract_second_level_domain_for_rules


# URL 匹配正则表达式
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\'`\]\)]+',
    re.IGNORECASE
)

# 域名匹配正则表达式（匹配常见域名格式）
DOMAIN_PATTERN = re.compile(
    r'\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b',
    re.IGNORECASE
)


def extract_first_valid_domain(text: str) -> Optional[str]:
    """提取第一个有效域名
    
    优先提取 URL 中的域名，其次是纯域名格式
    
    Args:
        text: 输入文本
        
    Returns:
        第一个有效域名，如果没有则返回 None
    """
    if not text:
        return None
    
    # 1. 先尝试从 URL 提取
    urls = URL_PATTERN.findall(text)
    for url in urls:
        domain = normalize_domain(url)
        if domain and is_valid_domain(domain):
            return domain
    
    # 2. 再尝试提取纯域名
    domain_matches = DOMAIN_PATTERN.findall(text)
    for domain in domain_matches:
        normalized = normalize_domain(domain)
        if normalized and is_valid_domain(normalized):
            return normalized
    
    return None


def extract_domain_for_rules(text: str) -> Optional[str]:
    """提取用于添加规则的域名（二级域名）
    
    先提取域名，再转换为二级域名格式
    
    Args:
        text: 输入文本
        
    Returns:
        二级域名格式，如果无法提取则返回 None
    """
    domain = extract_first_valid_domain(text)
    if not domain:
        return None
    
    # 转换为二级域名（用于规则添加）
    return extract_second_level_domain_for_rules(domain)


def remove_bot_mention(text: str, bot_username: str) -> str:
    """从文本中移除 @机器人 提及
    
    Args:
        text: 原始文本
        bot_username: 机器人用户名（不含 @）
        
    Returns:
        移除提及后的文本
    """
    if not text or not bot_username:
        return text
    
    # 移除 @username 格式的提及
    pattern = re.compile(rf'@{re.escape(bot_username)}\b', re.IGNORECASE)
    return pattern.sub('', text).strip()
