# GitHub Actions

项目使用 `master` 分支，并将同一份 amd64/arm64 镜像发布到 Docker Hub 的 `aethersailor/rule-bot` 和 GitHub Container Registry 的 `ghcr.io/aethersailor/rule-bot`。

`Build and Push Docker Image` 在 `master` 更新、推送 `vMAJOR.MINOR.PATCH` 标签或手动触发时构建并发布镜像。`master` 发布 `latest`，版本标签发布去掉 `v` 前缀的版本号，例如 `v0.2.0` 对应 `0.2.0`。发布前确认分支或标签仍指向本次提交，避免旧任务覆盖新镜像；发布后清理 Docker Hub 的非标准标签和 GHCR 中未被有效镜像引用的旧对象。

项目由维护者人工验证，不设置单元测试、静态检查、安全扫描或镜像体积门禁。运行时的输入校验、数据保护和容器健康检查继续由应用提供。

`Auto-merge Dependabot PRs` 在 Dependabot PR 新建、更新、重新打开或转为待合并状态时运行，也支持手动触发，并每 15 分钟扫描一次开放 PR。自动合并包含 patch、minor 和 major 更新，不要求人工批准或等待测试、CodeQL 等检查。

合并前只确认 PR 来自同仓库的 `dependabot[bot]` 分支、不是草稿、改动属于依赖文件，且全部提交均由 Dependabot 签名。使用精确 head SHA 执行 squash 合并，避免检查后分支变化导致误合并。单个 PR 冲突或 API 请求失败时继续处理其他 PR，后续扫描会重试；Dependabot 按 `rebase-strategy: auto` 更新分支。真实依赖冲突或不兼容升级仍可能需要维护者处理，自动合并不保证兼容性。

Dependabot 按周检查 GitHub Actions、Python 运行依赖和 Docker 基础镜像。每个生态的更新分别分组，包含主版本升级。合并完成后显式触发 Docker 发布，因为使用 `GITHUB_TOKEN` 合并产生的 push 事件不会自动启动普通 push 工作流。[GitHub 工作流触发说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

`Sync Docker Hub Description` 在 `master` 的 `.github/DOCKERHUB_DESCRIPTION.md` 变化后同步 Docker Hub 说明，也支持手动触发。详细部署说明以 README 和 Wiki 为准。

工作流引用的 Actions 固定到 commit SHA，并保留主版本注释供 Dependabot 识别和更新。
