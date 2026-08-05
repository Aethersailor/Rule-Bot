# GitHub Actions

项目只保留一个长期分支和一个发布标签：

- 分支：`master`
- Docker Hub：`aethersailor/rule-bot:latest`

`Build and Push Docker Image` 会先执行编译检查、Ruff、单元测试、依赖完整性和漏洞审计。PR 只在 GitHub Actions 内构建多架构镜像，不登录或推送 Docker Hub；`master` 和手动触发会先发布不可变的 `sha-<commit>` 候选镜像。两个架构都通过容器内依赖导入 smoke，且构建前后都确认提交仍是当前 `master` 后，才会把同一镜像提升为 `latest`。

发布完成后会验证 amd64/arm64 manifest 中的 `org.opencontainers.image.revision` 都等于本次提交。不可变 SHA 标签保留作回滚证据；部署者仍只需使用 `aethersailor/rule-bot:latest`，现有 Compose 和更新方式不变。

Dependabot 更新按周分组。只有 PR 的完整验证工作流成功后才会自动合并，并显式触发一次 `master` 发布，避免 `GITHUB_TOKEN` 合并事件不触发后续工作流的问题。
