# GitHub Actions

项目只保留一个长期分支，并将同一份多架构镜像发布到两个镜像仓库：

- 分支：`master`
- Docker Hub：`aethersailor/rule-bot`
- GitHub Container Registry：`ghcr.io/aethersailor/rule-bot`

`Build and Push Docker Image` 会先执行编译检查、Ruff、单元测试、依赖完整性和漏洞审计。PR 只在 GitHub Actions 内构建多架构镜像，不登录或推送镜像仓库。`master` 和基于 `master` 的手动运行只发布 `latest`；`vMAJOR.MINOR.PATCH` Git 标签只发布去掉 `v` 前缀的版本号，例如 `v0.2.0` 对应 `0.2.0`。工作流不会创建 `sha-*` 标签；发布后会删除 Docker Hub 中除 `latest` 和版本号以外的标签，并清理 GHCR 中没有标签且未被有效镜像引用的旧版本对象。

发布完成后，工作流会对实际镜像执行 amd64/arm64 依赖导入检查，并验证两个镜像仓库的架构、`org.opencontainers.image.revision`、`org.opencontainers.image.licenses=AGPL-3.0-only` 和索引摘要完全一致。每个运行架构的压缩层总大小不得超过 40,000,000 字节，镜像历史中也不得出现复制到运行阶段的 wheelhouse 层。GHCR 中当前有效镜像引用的 amd64/arm64 清单、SBOM 和来源证明属于镜像组成部分，会予以保留。部署者可以继续使用 `aethersailor/rule-bot:latest`，也可以改用 `ghcr.io/aethersailor/rule-bot:latest`；现有 Compose 和更新方式不变。

`Sync Docker Hub Description` 在 `master` 的 `.github/DOCKERHUB_DESCRIPTION.md` 变化后同步 Docker Hub 的短说明和完整说明，也支持手动触发。该工作流只读取仓库内容，使用仓库现有的 Docker Hub Secrets 登录，并在更新后回读核对；详细部署说明仍以 README 和 Wiki 为准。

Dependabot 更新按周处理，运行依赖和开发工具分别分组。PR 侧的只读工作流只提取并固化 Dependabot 元数据；默认分支上的独立特权工作流会复核 PR 作者、签名、head/base SHA、升级等级，以及 Docker 验证和 CodeQL 的全部必需检查。只有 minor/patch 会以 merge commit 串行自动合并，major 保持独立 PR 供人工审查。工作流摘要和互斥标签会明确标出“可自动合并”“等待 Dependabot 更新基线”或“需要人工审查”，不能把绿色但跳过合并的运行理解为已经合并。

自动合并后会显式触发并等待 `master` 的 Docker 发布和 CodeQL，避免 `GITHUB_TOKEN` 产生的合并事件不触发后续工作流。CodeQL 对 Python 和 GitHub Actions 同时执行扩展安全与质量查询，并在每次 push、PR、手动触发之外增加每周兜底扫描。

工作流引用的 Actions 全部固定到不可变 commit SHA，并保留主版本注释供 Dependabot 识别和更新，避免可移动标签带来的供应链风险。
