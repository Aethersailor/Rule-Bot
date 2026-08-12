# GitHub Actions

项目只保留一个长期分支，并将同一份多架构镜像发布到两个镜像仓库：

- 分支：`master`
- Docker Hub：`aethersailor/rule-bot`
- GitHub Container Registry：`ghcr.io/aethersailor/rule-bot`

`Build and Push Docker Image` 会先执行编译检查、Ruff、单元测试、依赖完整性和漏洞审计。PR 只在 GitHub Actions 内构建多架构镜像，不登录或推送镜像仓库。`master` 和基于 `master` 的手动运行只发布 `latest`；`vMAJOR.MINOR.PATCH` Git 标签只发布去掉 `v` 前缀的版本号，例如 `v0.2.0` 对应 `0.2.0`。工作流不再创建 `sha-*` 标签，并会删除 Docker Hub 中遗留的 `sha-*` 标签。

发布完成后，工作流会对实际镜像执行 amd64/arm64 依赖导入检查，并验证两个镜像仓库的架构、`org.opencontainers.image.revision` 和索引摘要完全一致。部署者可以继续使用 `aethersailor/rule-bot:latest`，也可以改用 `ghcr.io/aethersailor/rule-bot:latest`；现有 Compose 和更新方式不变。

Dependabot 更新按周处理，运行依赖和开发工具分别分组。PR 侧的只读工作流只提取并固化 Dependabot 元数据；默认分支上的独立特权工作流会复核 PR 作者、签名、head/base SHA、升级等级，以及 Docker 验证和 CodeQL 的全部必需检查。只有 minor/patch 会以 merge commit 串行自动合并，major 保持独立 PR 供人工审查。工作流摘要和互斥标签会明确标出“可自动合并”“等待 Dependabot 更新基线”或“需要人工审查”，不能把绿色但跳过合并的运行理解为已经合并。

自动合并后会显式触发并等待 `master` 的 Docker 发布和 CodeQL，避免 `GITHUB_TOKEN` 产生的合并事件不触发后续工作流。CodeQL 对 Python 和 GitHub Actions 同时执行扩展安全与质量查询，并在每次 push、PR、手动触发之外增加每周兜底扫描。

工作流引用的 Actions 全部固定到不可变 commit SHA，并保留主版本注释供 Dependabot 识别和更新，避免可移动标签带来的供应链风险。
