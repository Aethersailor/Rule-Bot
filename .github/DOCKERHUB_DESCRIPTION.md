# Rule-Bot

Rule-Bot 是一个面向 Mihomo / Clash 直连规则仓库的自托管 Telegram 机器人。它查询域名、检查重复规则与网络信息，并按 Telegram 私聊确认、受信任群聊或 Rule-Bot Client 入口策略，把通过检查的域名写入目标 GitHub 规则文件。

只想为 `Custom_OpenClash_Rules` 查询或补充直连域名时，可以直接使用公共实例 [@asailor_rulebot](https://t.me/asailor_rulebot)，无需自行部署。

## 镜像标签

- `latest`：随 `master` 更新的滚动标签，也是项目 Compose 模板的默认值；它可能领先最新 GitHub Release。
- `X.Y.Z`：对应 `vX.Y.Z` GitHub Release 的版本标签，用于版本审计、问题复现和回滚。

项目不发布 `dev` 标签。需要确认具体构建时，请记录镜像 digest，并查看 OCI 标签 `org.opencontainers.image.revision`。

- [GitHub Releases](https://github.com/Aethersailor/Rule-Bot/releases)
- [全部镜像标签](https://hub.docker.com/r/aethersailor/rule-bot/tags)

## 快速部署

```bash
mkdir rule-bot && cd rule-bot
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/Aethersailor/Rule-Bot/master/docker-compose.yml
```

编辑 Compose 文件并填写：

- `TELEGRAM_BOT_TOKEN`；
- `GITHUB_TOKEN`；
- `GITHUB_REPO`；
- `DIRECT_RULE_FILE`。

GitHub Token 建议使用 Fine-grained PAT，只授予目标仓库的 Contents 读写权限。不要提交包含真实 Token 的 Compose 文件。

```bash
chmod 600 docker-compose.yml
docker compose pull
docker compose up -d
docker compose ps
```

容器以 UID/GID `1000:1000` 运行。挂载的数据目录必须允许该用户写入，密钥文件必须允许该用户读取。

## 数据与网络入口

基础 Telegram 模式下，`/app/data` 主要保存可重新下载的数据文件。启用 Rule-Bot Client Community 后，`/app/data` 还会保存社区 Token、吊销和隐私同意状态，必须挂载持久化目录并备份数据库与签名密钥。

Rule-Bot Client 私用和社区入口默认关闭。启用后，应把宿主机端口绑定到 `127.0.0.1`，再通过 HTTPS 反向代理或 Tunnel 暴露，并始终使用 Bearer Token 鉴权。

## 文档与许可

- [项目 README](https://github.com/Aethersailor/Rule-Bot#readme)
- [用户 Wiki](https://github.com/Aethersailor/Rule-Bot/wiki)
- [部署与故障排查](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查)
- [Rule-Bot Client 接入](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入)
- [隐私说明](https://github.com/Aethersailor/Rule-Bot/blob/master/PRIVACY.md)
- [安全策略](https://github.com/Aethersailor/Rule-Bot/blob/master/SECURITY.md)

Rule-Bot 采用 [GNU Affero General Public License v3.0](https://github.com/Aethersailor/Rule-Bot/blob/master/LICENSE)，OCI 许可标识为 `AGPL-3.0-only`。
