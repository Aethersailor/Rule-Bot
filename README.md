<div align="center">

# 🤖 Rule-Bot

**用 Telegram 查询域名、判断是否适合直连，并把确认后的规则写入 GitHub。**

一个面向 Mihomo / Clash 直连规则仓库的自托管 Telegram 机器人。

[![Build and Push Docker Image](https://github.com/Aethersailor/Rule-Bot/actions/workflows/docker-build.yml/badge.svg)](https://github.com/Aethersailor/Rule-Bot/actions/workflows/docker-build.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/aethersailor/rule-bot)](https://hub.docker.com/r/aethersailor/rule-bot)
[![License](https://img.shields.io/github/license/Aethersailor/Rule-Bot)](LICENSE)

[🚀 直接使用](https://t.me/asailor_rulebot) · [📚 使用文档](https://github.com/Aethersailor/Rule-Bot/wiki) · [🐳 Docker Hub](https://hub.docker.com/r/aethersailor/rule-bot) · [🔐 隐私说明](PRIVACY.md)

</div>

---

Rule-Bot 把域名查询、网络信息检查、人工确认和 GitHub 提交放进一个清晰的聊天流程，减少重复规则和误提交。

> [!IMPORTANT]
> **Rule-Bot 不是网页应用。** [Custom_OpenClash_Rules](https://github.com/Aethersailor/Custom_OpenClash_Rules) 用户可以直接打开配套机器人 [@asailor_rulebot](https://t.me/asailor_rulebot)；维护其他规则仓库或需要独立服务时，再部署自己的实例。

## 🧭 选择使用方式

| | 直接使用配套机器人 | 部署自己的实例 |
| --- | --- | --- |
| **适合场景** | 为 `Custom_OpenClash_Rules` 查询或添加直连域名 | 维护其他规则仓库，或需要独立配置与权限 |
| **需要准备** | Telegram | Docker 主机、Telegram Bot Token、GitHub Token |
| **开始方式** | 打开 [@asailor_rulebot](https://t.me/asailor_rulebot)，发送 `/start` | 阅读 [快速部署](#quick-deploy) 或 [Wiki 部署指南](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查) |

## ✨ 能做什么

| 能力 | 使用体验 |
| --- | --- |
| 🔎 **查询与去重** | 检查域名是否已存在于目标规则文件或 `GEOSITE:CN` |
| 🌐 **网络信息判断** | 结合 DNS、NS 和 GeoIP 结果，给出是否适合直连的建议 |
| ✅ **确认后提交** | 私聊确认后写入 `DOMAIN-SUFFIX` 规则，并返回 GitHub 提交链接 |
| 👥 **群组协作** | 在指定群组中响应 `@机器人` 的消息，自动检查并处理域名 |
| 📣 **可选管理能力** | 验证群成员身份、设置管理员、发送规则更新播报 |
| 🔌 **客户端接入** | 可选接收 [Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) 捕获的域名 |
| 🐳 **多架构镜像** | 提供 `linux/amd64` 和 `linux/arm64` Docker 镜像 |

> [!NOTE]
> 当前只管理直连规则。代理规则添加和规则删除尚未开放。

## 💬 使用机器人

### 私聊

发送 `/start` 打开主菜单，也可以直接使用以下命令：

| 命令 | 用途 |
| --- | --- |
| `/query` | 查询域名状态和检查结果，不修改规则 |
| `/add` | 检查域名并进入直连规则添加流程 |
| `/help` | 查看机器人内的使用说明 |
| `/id` | 查看自己的 Telegram 用户 ID |
| `/skip` | 添加规则时跳过说明并继续提交 |

添加流程会先规范化域名，再检查 `.cn`、目标规则文件、`GEOSITE:CN`、DNS、NS 和 GeoIP。只有符合条件的域名才会进入确认步骤。提交成功后，机器人返回规则文件路径和 GitHub 提交链接。

### 群聊

启用群聊模式后，可以：

- 在消息中写入域名并 `@机器人`；
- 回复一条包含域名的消息，再 `@机器人`。

> [!WARNING]
> **群聊模式会自动完成检查和添加，不会再次要求私聊式确认。** 机器人只应加入受信任的群组，并通过 `ALLOWED_GROUP_IDS` 限定可用群组。

群聊模式需要在 [@BotFather](https://t.me/BotFather) 中关闭机器人的 Privacy Mode，并在修改后把机器人重新加入群组。完整设置见 [Wiki 配置说明](https://github.com/Aethersailor/Rule-Bot/wiki/配置说明#telegram-groups)。

## 🔄 Rule-Bot 如何处理一个域名

| 步骤 | 处理动作 | 结果 |
| :---: | --- | --- |
| **1** | 从输入的 URL 或域名中提取可注册域名 | 统一待检查对象 |
| **2** | 跳过默认直连的 `.cn` 域名，并检查目标规则文件与 `GEOSITE:CN` | 避免重复写入 |
| **3** | 查询 DNS、NS 和 GeoIP 信息 | 判断是否存在中国大陆直连信号 |
| **4** | 按私聊确认或自动处理策略执行 | 符合条件时写入 `DOMAIN-SUFFIX` 规则并创建 GitHub 提交 |

查询操作不会修改仓库。私聊添加需要用户确认；群聊和 Rule-Bot Client 使用各自的自动处理策略。

<a id="quick-deploy"></a>

## 🚀 快速部署

### 1. 准备环境与凭据

- 一台已经安装 Docker 和 Docker Compose 的主机；
- 通过 [@BotFather](https://t.me/BotFather) 创建的 Telegram Bot Token；
- 能够读写目标规则仓库内容的 GitHub Token；
- 已经存在的目标仓库和直连规则文件。

### 2. 下载 Compose 模板

```bash
mkdir rule-bot && cd rule-bot
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/Aethersailor/Rule-Bot/master/docker-compose.yml
```

### 3. 填写必需配置

编辑 `docker-compose.yml`，至少填写以下四项：

| 配置项 | 示例 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | `123456:replace-me` | [@BotFather](https://t.me/BotFather) 提供的 Bot Token |
| `GITHUB_TOKEN` | `github_pat_replace_me` | 需要能够读写目标仓库内容 |
| `GITHUB_REPO` | `owner/repository` | 目标 GitHub 仓库 |
| `DIRECT_RULE_FILE` | `rule/Direct.list` | 相对于仓库根目录的规则文件路径 |

### 4. 检查并启动

确认四项必需配置已经替换，然后启动容器：

```bash
grep -nE '^[[:space:]]*-[[:space:]]+(TELEGRAM_BOT_TOKEN|GITHUB_TOKEN|GITHUB_REPO|DIRECT_RULE_FILE)=your_' docker-compose.yml
chmod 600 docker-compose.yml
docker compose pull
docker compose up -d
docker compose logs -f rule-bot
```

> [!TIP]
> **成功标志：** 容器状态为 `healthy`，日志出现“机器人启动成功，开始轮询”，并且在 Telegram 中发送 `/start` 后能够打开主菜单。

如果启动失败，先查看 [Wiki 中的故障排查](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查#troubleshooting)。

> [!CAUTION]
> `docker-compose.yml` 中包含 Telegram 和 GitHub 凭据。不要把填写真实 Token 的文件提交到 Git，也不要把完整配置或日志直接发布到 Issue。

## 📚 文档导航

| 文档 | 适合什么时候阅读 |
| --- | --- |
| 🏠 [Wiki 首页](https://github.com/Aethersailor/Rule-Bot/wiki) | 浏览全部用户文档并选择使用路径 |
| 🛠️ [部署与故障排查](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查) | 第一次部署、更新容器或处理启动问题 |
| ⚙️ [配置说明](https://github.com/Aethersailor/Rule-Bot/wiki/配置说明) | 启用群组验证、群聊、管理员、播报或调整数据源 |
| 🔌 [Rule-Bot Client 接入](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入) | 接入私用或社区客户端入口 |
| 🔐 [隐私说明](PRIVACY.md) | 了解社区 Token、网络元数据、数据保留和用户控制 |

## ⚠️ 使用限制

- 规则文件必须已经存在，并使用每行一条 `DOMAIN-SUFFIX,example.com` 的文本格式。
- `.cn` 域名按现有策略默认直连，不会重复写入公开规则库。
- 每个 Telegram 账号每小时最多添加 50 个域名。
- 管理员可以绕过归属地策略，但不能绕过无效域名、重复规则和 `.cn` 处理。
- Rule-Bot 直接写入目标分支，不创建 Pull Request。建议为机器人使用独立 GitHub 凭据，并只授予目标仓库所需权限。

## 🔐 安全与隐私

Rule-Bot 会把成功添加的域名公开写入目标 GitHub 仓库及其提交历史。DNS、NS 和 GeoIP 检查也会把待检查域名交给相应服务提供方。

Rule-Bot Client 入口默认关闭。启用前应阅读 [Wiki 接入说明](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入) 和 [隐私说明](PRIVACY.md)，使用 HTTPS 与 Bearer Token，并把监听端口限制在宿主机回环地址。隐藏 API 路径只能减少扫描噪声，不能代替鉴权。

## 🌱 社区与许可

| 入口 | 链接 |
| --- | --- |
| 🤖 配套机器人 | [@asailor_rulebot](https://t.me/asailor_rulebot) |
| 💬 交流群 | [Custom OpenClash Rules 交流群](https://t.me/custom_openclash_rules_group) |
| 📄 开源许可 | [GNU General Public License v3.0](LICENSE) |

---

<div align="center">

如果 Rule-Bot 对你有帮助，欢迎通过 Issue 提交建议，或将它分享给同样维护规则仓库的人。

</div>
