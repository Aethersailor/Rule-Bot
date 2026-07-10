# GitHub Actions

项目只保留一个长期分支和一个发布标签：

- 分支：`master`
- Docker Hub：`aethersailor/rule-bot:latest`

`Build and Push Docker Image` 会先执行编译检查、Ruff、单元测试、依赖完整性和漏洞审计。PR 只在 GitHub Actions 内构建多架构镜像，不登录或推送 Docker Hub；`master` 和手动触发才会发布 `latest`。发布完成后会验证 amd64/arm64 manifest，并清理 Docker Hub 上所有非 `latest` 标签。

Dependabot 更新按周分组。只有 PR 的完整验证工作流成功后才会自动合并，并显式触发一次 `master` 发布，避免 `GITHUB_TOKEN` 合并事件不触发后续工作流的问题。
