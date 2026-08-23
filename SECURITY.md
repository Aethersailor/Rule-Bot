# Rule-Bot 安全策略 / Security Policy

## 受支持的版本

安全修复以[最新正式 Release](https://github.com/Aethersailor/Rule-Bot/releases)为支持基线。较早的正式版本不保证继续获得安全修复。

默认 Compose 使用 `aethersailor/rule-bot:latest`。`latest` 是随 `master` 更新的滚动标签，可能领先最新正式 Release，因此不能单独作为稳定版本号。`X.Y.Z` 镜像标签对应 `vX.Y.Z` Release；镜像 digest 和 OCI 标签 `org.opencontainers.image.revision` 用于确认具体构建、复现问题和回滚。

报告问题时，请提供以下任一身份信息：

- GitHub Release 版本；
- 完整镜像 digest；
- OCI revision；
- 容器实际使用的标签及拉取时间。

## 私密报告安全漏洞

请使用 GitHub 的[私密漏洞报告](https://github.com/Aethersailor/Rule-Bot/security/advisories/new)。不要为疑似安全漏洞或隐私边界问题创建公开 Issue。

报告中请尽量包含：

- 受影响的版本、镜像 digest 或 revision；
- 可以到达问题的入口；
- 预期应当成立的安全或隐私边界；
- 已脱敏的最小复现步骤；
- 已观察到的影响。

不要提交 Telegram Token、GitHub Token、Rule-Bot Client Token、签名密钥、隐藏 API 路径、未脱敏域名、私有网络地址或完整配置文件。

没有跨越安全或隐私边界的普通缺陷、部署问题和文档问题，可以在移除敏感信息后使用 [Rule-Bot Bug 表单](https://github.com/Aethersailor/Rule-Bot/issues/new?template=bug_report.yml)。

## English summary

Security fixes target the [latest published stable Release](https://github.com/Aethersailor/Rule-Bot/releases). The default Compose file uses `latest`, a rolling `master` tag that may be ahead of the latest Release. Use a `X.Y.Z` tag, image digest, or `org.opencontainers.image.revision` to identify the affected build.

Report suspected security or privacy vulnerabilities through [GitHub private vulnerability reporting](https://github.com/Aethersailor/Rule-Bot/security/advisories/new), not a public Issue. Include the affected build, reachable entry point, expected boundary, impact, and a redacted minimal reproduction. Never include credentials, hidden API paths, unredacted submissions, private addresses, or complete configuration files.
