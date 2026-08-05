"""
GitHub 服务模块
用于操作 GitHub 上的规则文件
"""

import asyncio
import base64
import io
import time
from datetime import datetime
from typing import Optional, Dict, Any
from loguru import logger
from github import Auth, Github, GithubException, InputGitAuthor

from ..config import Config
from ..utils.cache import TTLCache
from ..utils.domain_utils import normalize_domain
from ..utils.input_safety import sanitize_identity, validate_single_line_text
from ..utils.metrics import METRICS


class GitHubService:
    """GitHub 服务"""
    
    def __init__(self, config: Config):
        self.config = config
        self.github = Github(auth=Auth.Token(config.GITHUB_TOKEN))
        self.repo = None
        self._write_lock = asyncio.Lock()
        self._file_cache = TTLCache(
            getattr(config, "GITHUB_FILE_CACHE_SIZE", 0),
            getattr(config, "GITHUB_FILE_CACHE_TTL", 0)
        )
        self._analysis_cache = TTLCache(
            getattr(config, "GITHUB_FILE_CACHE_SIZE", 0),
            getattr(config, "GITHUB_FILE_CACHE_TTL", 0)
        )
        self._initialize_repo()

    def close(self) -> None:
        """Release the underlying HTTP session."""
        try:
            self.github.close()
        except Exception as e:
            logger.debug("关闭 GitHub 客户端失败: {}", e)

    @staticmethod
    def _is_managed_rule_comment(line: str) -> bool:
        stripped = line.strip()
        if not stripped.startswith("#"):
            return False

        normalized = stripped.lower()
        return " / date: " in normalized and (
            "add by telegram user:" in normalized
            or "force add by admin:" in normalized
            or "add by matchscope" in normalized
        )

    def _target_branch(self) -> Optional[str]:
        branch = getattr(self.config, "GITHUB_BRANCH", "")
        if isinstance(branch, str):
            branch = branch.strip()
        return branch or None

    def _cache_key(self, file_path: str) -> str:
        branch = self._target_branch()
        if branch:
            return f"{branch}:{file_path}"
        return file_path

    @staticmethod
    def _analyze_rule_content(content: str) -> Dict[str, Any]:
        """Parse a rule file once for duplicate checks and statistics."""
        rule_index: Dict[str, list] = {}
        rule_count = 0
        comment_count = 0
        total_lines = 0

        for line_num, raw_line in enumerate(io.StringIO(content), 1):
            total_lines += 1
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith('#'):
                comment_count += 1
                continue
            if not line.startswith('DOMAIN-SUFFIX,'):
                continue

            rule_count += 1
            rule_domain = line[14:].strip().lower()
            if rule_domain:
                rule_index.setdefault(rule_domain, []).append({
                    "line": line_num,
                    "rule": line,
                })

        return {
            "rule_index": rule_index,
            "rule_count": rule_count,
            "comment_count": comment_count,
            "total_lines": total_lines,
        }

    async def _get_rule_analysis(
        self,
        file_path: str,
        content: str,
        sha: Optional[str],
    ) -> Dict[str, Any]:
        analysis_key = (self._cache_key(file_path), sha or "")
        cached = self._analysis_cache.get(analysis_key)
        if cached is not None:
            METRICS.inc("github.analysis_cache.hit")
            return cached

        METRICS.inc("github.analysis_cache.miss")
        analysis = await asyncio.to_thread(self._analyze_rule_content, content)
        self._analysis_cache.set(analysis_key, analysis)
        return analysis

    def _get_contents_kwargs(self) -> Dict[str, str]:
        branch = self._target_branch()
        return {"ref": branch} if branch else {}

    def _update_file_kwargs(self) -> Dict[str, str]:
        branch = self._target_branch()
        return {"branch": branch} if branch else {}
    
    def _initialize_repo(self):
        """初始化仓库连接"""
        try:
            self.repo = self.github.get_repo(self.config.GITHUB_REPO)
            branch = self._target_branch()
            if branch:
                logger.info(f"成功连接到 GitHub 仓库: {self.config.GITHUB_REPO} (目标分支: {branch})")
            else:
                logger.info(f"成功连接到 GitHub 仓库: {self.config.GITHUB_REPO} (目标分支: 默认分支)")
        except Exception as e:
            logger.error(f"连接 GitHub 仓库失败: {e}")
    
    async def get_rule_file_content(self, file_path: str, use_cache: bool = True) -> Optional[str]:
        """获取规则文件内容"""
        try:
            logger.debug(f"正在获取文件内容: {file_path}")
            cache_key = self._cache_key(file_path)
            if use_cache:
                cached = self._file_cache.get(cache_key)
                if cached and "content" in cached:
                    METRICS.inc("github.cache.hit")
                    return cached["content"]
                METRICS.inc("github.cache.miss")

            start_ts = time.perf_counter()
            # 使用 asyncio.to_thread 在线程池中执行阻塞IO
            file_content = await asyncio.to_thread(
                self.repo.get_contents,
                file_path,
                **self._get_contents_kwargs()
            )
            content = base64.b64decode(file_content.content).decode('utf-8')
            self._file_cache.set(cache_key, {"content": content, "sha": getattr(file_content, "sha", None)})
            METRICS.record_request(
                "github.get_contents",
                (time.perf_counter() - start_ts) * 1000,
                success=True
            )
            logger.debug(f"成功获取文件内容: {file_path}, 长度: {len(content)} 字符")
            return content
        except GithubException as e:
            logger.error(f"GitHub API 获取文件失败: {file_path}, status={getattr(e, 'status', 'unknown')}, message={getattr(e, 'data', {}).get('message', str(e))}")
            METRICS.record_request("github.get_contents", 0.0, success=False)
            return None
        except Exception as e:
            logger.error(f"获取文件内容失败: {file_path}, {type(e).__name__}: {e}", exc_info=True)
            METRICS.record_request("github.get_contents", 0.0, success=False)
            return None

    async def get_rule_file_data(self, file_path: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """获取规则文件内容和 SHA"""
        try:
            logger.debug(f"正在获取文件内容和 SHA: {file_path}")
            cache_key = self._cache_key(file_path)
            if use_cache:
                cached = self._file_cache.get(cache_key)
                if cached and "content" in cached and cached.get("sha"):
                    METRICS.inc("github.cache.hit")
                    return {"content": cached["content"], "sha": cached["sha"]}
                METRICS.inc("github.cache.miss")

            start_ts = time.perf_counter()
            file_content = await asyncio.to_thread(
                self.repo.get_contents,
                file_path,
                **self._get_contents_kwargs()
            )
            content = base64.b64decode(file_content.content).decode('utf-8')
            self._file_cache.set(cache_key, {"content": content, "sha": file_content.sha})
            METRICS.record_request(
                "github.get_contents",
                (time.perf_counter() - start_ts) * 1000,
                success=True
            )
            return {"content": content, "sha": file_content.sha}
        except GithubException as e:
            logger.error(
                f"GitHub API 获取文件失败: {file_path}, status={getattr(e, 'status', 'unknown')}, "
                f"message={getattr(e, 'data', {}).get('message', str(e))}"
            )
            METRICS.record_request("github.get_contents", 0.0, success=False)
            return None
        except Exception as e:
            logger.error(f"获取文件内容和 SHA 失败: {file_path}, {type(e).__name__}: {e}", exc_info=True)
            METRICS.record_request("github.get_contents", 0.0, success=False)
            return None
    
    async def check_domain_in_rules(self, domain: str, file_path: str = None) -> Dict[str, Any]:
        """检查域名是否已在规则文件中"""
        try:
            if not file_path:
                file_path = self.config.DIRECT_RULE_FILE
            
            file_data = await self.get_rule_file_data(file_path)
            if file_data is None:
                return {"exists": None, "error": "暂时无法读取 GitHub 规则文件"}

            start_ts = time.perf_counter()
            analysis = await self._get_rule_analysis(
                file_path,
                file_data["content"],
                file_data.get("sha"),
            )
            domain_lower = domain.lower().strip().rstrip('.')
            parts = domain_lower.split('.')
            found_rules = []
            for index in range(len(parts)):
                candidate = '.'.join(parts[index:])
                match_type = "exact_match" if index == 0 else "suffix_match"
                for match in analysis["rule_index"].get(candidate, []):
                    found_rules.append({**match, "type": match_type})
            found_rules.sort(key=lambda item: item["line"])
            METRICS.record_request(
                "github.check_rules",
                (time.perf_counter() - start_ts) * 1000,
                success=True
            )
            
            return {
                "exists": len(found_rules) > 0,
                "matches": found_rules,
                "file_path": file_path
            }
            
        except Exception as e:
            logger.error(f"检查域名规则失败: {e}")
            METRICS.record_request("github.check_rules", 0.0, success=False)
            return {"exists": False, "error": str(e)}
    
    async def add_domain_to_rules(
        self,
        domain: str,
        user_name: str,
        description: str = "",
        file_path: str = None,
        force_add: bool = False,
        source: str = "telegram",
    ) -> Dict[str, Any]:
        """Serialize repository writes and make duplicate callbacks idempotent."""
        async with self._write_lock:
            return await self._add_domain_to_rules_unlocked(
                domain,
                user_name,
                description,
                file_path,
                force_add,
                source,
            )

    async def _add_domain_to_rules_unlocked(
        self,
        domain: str,
        user_name: str,
        description: str = "",
        file_path: str = None,
        force_add: bool = False,
        source: str = "telegram",
    ) -> Dict[str, Any]:
        """添加域名到规则文件"""
        try:
            normalized_domain = normalize_domain(domain)
            if not normalized_domain or normalized_domain != domain.strip().lower():
                return {"success": False, "error": "无效或不安全的域名格式"}
            domain = normalized_domain
            user_name = sanitize_identity(user_name)
            description = validate_single_line_text(description, 20)
            allowed_sources = {
                "telegram",
                "matchscope_private",
                "matchscope_community",
            }
            if source not in allowed_sources:
                return {"success": False, "error": "无效的规则提交来源"}

            if not file_path:
                file_path = self.config.DIRECT_RULE_FILE

            # 检查仓库连接
            if not self.repo:
                error_msg = "GitHub 仓库连接未初始化"
                logger.error(error_msg)
                return {"success": False, "error": error_msg}
            
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                # 获取当前文件内容和 SHA
                logger.debug(f"开始添加域名 {domain} 到文件 {file_path} (尝试 {attempt}/{max_retries})")
                file_data = await self.get_rule_file_data(file_path, use_cache=(attempt == 1))
                if not file_data:
                    error_msg = f"无法获取规则文件内容: {file_path}。请检查文件是否存在，仓库访问权限是否正确。"
                    logger.error(error_msg)
                    return {"success": False, "error": error_msg}

                content = file_data["content"]
                file_sha = file_data["sha"]

                # 在线程中处理文件内容修改逻辑
                def _prepare_update():
                    # 查找插入位置
                    lines = content.split('\n')
                    insert_index = -1
                    
                    # 验证参数
                    if not domain or not isinstance(domain, str) or len(domain.strip()) == 0:
                        return None, f"无效的域名格式: {domain}"
                    
                    if not user_name or not isinstance(user_name, str) or len(user_name.strip()) == 0:
                        return None, f"无效的用户名格式: {user_name}"

                    domain_lower = domain.strip().lower()
                    for line in lines:
                        check_line = line.strip()
                        if check_line.startswith('DOMAIN-SUFFIX,'):
                            rule_domain = check_line[14:].strip().lower()
                            if rule_domain == domain_lower:
                                return None, f"域名已存在于规则文件中: {domain}"
                    
                    for i, line in enumerate(lines):
                        if "# 以下域名待提交 PR" in line:
                            insert_index = i + 1
                            break
                    
                    if insert_index == -1:
                        # 如果没找到标记，添加到文件末尾
                        insert_index = len(lines)
                        lines.append("# 以下域名待提交 PR")
                        insert_index += 1
                    
                    # 构建新规则
                    # 使用北京时间（UTC+8）
                    from datetime import timezone, timedelta
                    beijing_tz = timezone(timedelta(hours=8))
                    current_date = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
                    
                    if force_add:
                        if description:
                            comment = f"# {description} / force add by Admin: {user_name} / Date: {current_date}"
                        else:
                            comment = f"# force add by Admin: {user_name} / Date: {current_date}"
                    elif source == "matchscope_private":
                        comment = f"# add by MatchScope / Date: {current_date}"
                    elif source == "matchscope_community":
                        comment = f"# add by MatchScope Community / Date: {current_date}"
                    else:
                        if description:
                            comment = f"# {description} / add by Telegram user: {user_name} / Date: {current_date}"
                        else:
                            comment = f"# add by Telegram user: {user_name} / Date: {current_date}"
                    
                    rule = f"DOMAIN-SUFFIX,{domain}"
                    
                    # 插入新规则
                    lines.insert(insert_index, comment)
                    lines.insert(insert_index + 1, rule)
                    
                    # 重新组合内容
                    new_content = '\n'.join(lines)
                    
                    # 遵循 Conventional Commits 规范
                    if force_add:
                        commit_title = (
                            f"feat(rules): force add direct domain {domain} by Admin "
                            f"(Telegram user: {user_name})"
                        )
                    elif source == "matchscope_private":
                        commit_title = (
                            f"feat(rules): add direct domain {domain} by Rule-Bot "
                            "(from MatchScope)"
                        )
                    elif source == "matchscope_community":
                        commit_title = (
                            f"feat(rules): add direct domain {domain} by Rule-Bot "
                            "(from MatchScope Community)"
                        )
                    else:
                        commit_title = f"feat(rules): add direct domain {domain} by Telegram Bot (Telegram user: {user_name})"
                    commit_body = description if description else ""
                    full_commit_message = commit_title
                    if commit_body and commit_body.strip():
                        full_commit_message += f"\n\n{commit_body}"
                        
                    return (new_content, full_commit_message), None

                result, error = await asyncio.to_thread(_prepare_update)
                if error:
                    logger.error(error)
                    return {
                        "success": False,
                        "already_exists": error.startswith("域名已存在于规则文件中:"),
                        "error": error,
                    }

                new_content, full_commit_message = result
                
                logger.debug(f"准备提交更改: {full_commit_message.splitlines()[0]}")
                
                # 在线程中执行GitHub API调用
                def _perform_commit():
                    return self.repo.update_file(
                        file_path,
                        full_commit_message,
                        new_content,
                        file_sha,
                        committer=InputGitAuthor(
                            name=self.config.GITHUB_COMMIT_NAME,
                            email=self.config.GITHUB_COMMIT_EMAIL
                        ),
                        **self._update_file_kwargs()
                    )

                try:
                    start_ts = time.perf_counter()
                    commit_result = await asyncio.to_thread(_perform_commit)
                    METRICS.record_request(
                        "github.update_file",
                        (time.perf_counter() - start_ts) * 1000,
                        success=True
                    )
                except GithubException as e:
                    METRICS.record_request("github.update_file", 0.0, success=False)
                    if getattr(e, "status", None) == 409 and attempt < max_retries:
                        logger.warning("GitHub 更新冲突，准备重试")
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise

                # 构建 commit 链接
                commit_sha = commit_result['commit'].sha
                commit_url = f"https://github.com/{self.config.GITHUB_REPO}/commit/{commit_sha}"
                
                logger.info(f"成功添加域名 {domain} 到规则文件，commit: {commit_sha}")
                self._file_cache.pop(self._cache_key(file_path))
                self._analysis_cache.clear()
                
                return {
                    "success": True,
                    "domain": domain,
                    "file_path": file_path,
                    "commit_message": full_commit_message,
                    "commit_sha": commit_sha,
                    "commit_url": commit_url
                }

            return {"success": False, "error": "GitHub 更新冲突，多次重试失败"}

        except ValueError as e:
            logger.warning("拒绝不安全的规则元数据: {}", e)
            return {"success": False, "error": str(e)}
        except GithubException as e:
            error_details = getattr(e, 'data', {})
            error_message = error_details.get('message', str(e)) if error_details else str(e)
            logger.error(f"GitHub API 错误: status={getattr(e, 'status', 'unknown')}, message={error_message}, data={error_details}")
            return {"success": False, "error": f"GitHub API 错误: {error_message} (状态码: {getattr(e, 'status', 'unknown')})"}
        except Exception as e:
            logger.error(f"添加域名规则失败: {type(e).__name__}: {e}", exc_info=True)
            # 添加更详细的错误信息
            error_msg = f"{type(e).__name__}: {str(e)}"
            if hasattr(e, '__traceback__'):
                import traceback
                tb_str = ''.join(traceback.format_tb(e.__traceback__))
                logger.error(f"详细错误堆栈: {tb_str}")
            return {"success": False, "error": error_msg}
    
    async def remove_domain_from_rules(
        self,
        domain: str,
        user_name: str,
        file_path: str = None,
    ) -> Dict[str, Any]:
        async with self._write_lock:
            return await self._remove_domain_from_rules_unlocked(domain, user_name, file_path)

    async def _remove_domain_from_rules_unlocked(self, domain: str, user_name: str, file_path: str = None) -> Dict[str, Any]:
        """从规则文件中删除域名"""
        try:
            normalized_domain = normalize_domain(domain)
            if not normalized_domain or normalized_domain != domain.strip().lower():
                return {"success": False, "error": "无效或不安全的域名格式"}
            domain = normalized_domain
            user_name = sanitize_identity(user_name)
            if not file_path:
                file_path = self.config.DIRECT_RULE_FILE

            if not self.repo:
                error_msg = "GitHub 仓库连接未初始化"
                logger.error(error_msg)
                return {"success": False, "error": error_msg}

            max_retries = 3
            for attempt in range(1, max_retries + 1):
                file_data = await self.get_rule_file_data(file_path, use_cache=(attempt == 1))
                if not file_data:
                    return {"success": False, "error": "无法获取文件内容"}

                content = file_data["content"]
                file_sha = file_data["sha"]

                # 在线程中处理文件内容修改逻辑
                def _prepare_removal():
                    lines = content.split('\n')
                    domain_lower = domain.lower()
                    removed_lines = []
                    new_lines = []
                    
                    i = 0
                    while i < len(lines):
                        line = lines[i].strip()
                        
                        # 检查是否是要删除的域名规则
                        if line.startswith('DOMAIN-SUFFIX,'):
                            rule_domain = line[14:].strip().lower()
                            if rule_domain == domain_lower:
                                # 找到要删除的规则
                                removed_lines.append(line)
                                
                                # 检查前一行是否是相关注释
                                previous_line = lines[i-1].strip() if i > 0 else ""
                                if previous_line and self._is_managed_rule_comment(previous_line):
                                    # 删除注释行
                                    removed_lines.append(previous_line)
                                    if new_lines:
                                        new_lines.pop()  # 移除已添加的注释行
                                
                                i += 1  # 跳过当前规则行
                                continue
                        
                        new_lines.append(lines[i])
                        i += 1
                    
                    if not removed_lines:
                        return None, "未找到指定域名的规则"
                    
                    return (new_lines, removed_lines), None

                result, error = await asyncio.to_thread(_prepare_removal)
                if error:
                    return {"success": False, "error": error}
                
                new_lines, removed_lines = result
                
                # 重新组合内容
                new_content = '\n'.join(new_lines)
                
                # 提交更改（遵循 Conventional Commits 规范）
                commit_message = f"feat(rules): remove direct domain {domain} by Telegram Bot (Telegram user: {user_name})"
                
                def _perform_commit():
                    return self.repo.update_file(
                        file_path,
                        commit_message,
                        new_content,
                        file_sha,
                        committer=InputGitAuthor(
                            name=self.config.GITHUB_COMMIT_NAME,
                            email=self.config.GITHUB_COMMIT_EMAIL
                        ),
                        **self._update_file_kwargs()
                    )

                try:
                    start_ts = time.perf_counter()
                    commit_result = await asyncio.to_thread(_perform_commit)
                    METRICS.record_request(
                        "github.update_file",
                        (time.perf_counter() - start_ts) * 1000,
                        success=True
                    )
                except GithubException as e:
                    METRICS.record_request("github.update_file", 0.0, success=False)
                    if getattr(e, "status", None) == 409 and attempt < max_retries:
                        logger.warning("GitHub 更新冲突，准备重试")
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise
                
                # 构建 commit 链接
                commit_sha = commit_result['commit'].sha
                commit_url = f"https://github.com/{self.config.GITHUB_REPO}/commit/{commit_sha}"
                
                logger.info(f"成功删除域名 {domain} 从规则文件，commit: {commit_sha}")
                self._file_cache.pop(self._cache_key(file_path))
                self._analysis_cache.clear()
                
                return {
                    "success": True,
                    "domain": domain,
                    "removed_lines": removed_lines,
                    "commit_sha": commit_sha,
                    "commit_url": commit_url,
                    "file_path": file_path
                }

            return {"success": False, "error": "GitHub 更新冲突，多次重试失败"}
            
        except GithubException as e:
            logger.error(f"GitHub API 错误: {e}")
            return {"success": False, "error": f"GitHub API 错误: {e.data.get('message', str(e))}"}
        except Exception as e:
            logger.error(f"删除域名规则失败: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_file_stats(self, file_path: str = None) -> Dict[str, Any]:
        """获取规则文件统计信息"""
        try:
            if not file_path:
                file_path = self.config.DIRECT_RULE_FILE
            
            file_data = await self.get_rule_file_data(file_path)
            if not file_data or not file_data.get("content"):
                return {"error": "无法获取文件内容"}

            analysis = await self._get_rule_analysis(
                file_path,
                file_data["content"],
                file_data.get("sha"),
            )
            
            return {
                "file_path": file_path,
                "total_lines": analysis["total_lines"],
                "rule_count": analysis["rule_count"],
                "comment_count": analysis["comment_count"]
            }
            
        except Exception as e:
            logger.error(f"获取文件统计失败: {e}")
            return {"error": str(e)} 
