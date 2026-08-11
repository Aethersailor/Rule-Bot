# GitHub Actions

项目只保留一个长期分支和一个发布标签：

- 分支：`master`
- Docker Hub：`aethersailor/rule-bot:latest`

`Build and Push Docker Image` 会先执行编译检查、Ruff、单元测试、依赖完整性和漏洞审计。PR 只在 GitHub Actions 内构建多架构镜像，不登录或推送 Docker Hub；`master` 和手动触发会先发布不可变的 `sha-<commit>` 候选镜像。两个架构都通过容器内依赖导入 smoke，且构建前后都确认提交仍是当前 `master` 后，才会把同一镜像提升为 `latest`。

发布完成后会验证 amd64/arm64 manifest 中的 `org.opencontainers.image.revision` 都等于本次提交。不可变 SHA 标签保留作回滚证据；部署者仍只需使用 `aethersailor/rule-bot:latest`，现有 Compose 和更新方式不变。

Dependabot 更新按周处理，运行依赖和开发工具分别分组。PR 侧的只读工作流只提取并固化 Dependabot 元数据；默认分支上的独立特权工作流会复核 PR 作者、签名、head/base SHA、升级等级，以及 Docker 验证和 CodeQL 的全部必需检查。只有 minor/patch 会以 merge commit 串行自动合并，major 保持独立 PR 供人工审查。工作流摘要和互斥标签会明确标出“可自动合并”“等待 Dependabot 更新基线”或“需要人工审查”，不能把绿色但跳过合并的运行理解为已经合并。

自动合并后会显式触发并等待 `master` 的 Docker 发布和 CodeQL，避免 `GITHUB_TOKEN` 产生的合并事件不触发后续工作流。CodeQL 对 Python 和 GitHub Actions 同时执行扩展安全与质量查询，并在每次 push、PR、手动触发之外增加每周兜底扫描。

工作流引用的 Actions 全部固定到不可变 commit SHA，并保留主版本注释供 Dependabot 识别和更新，避免可移动标签带来的供应链风险。
