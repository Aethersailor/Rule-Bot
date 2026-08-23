<div align="center">

# 🤖 Rule-Bot

**用 Telegram 查询域名、判断是否适合直连，并按入口策略维护 GitHub 规则。**

一个面向 Mihomo / Clash 直连规则仓库的自托管 Telegram 机器人。

[![Build and Push Docker Image](https://github.com/Aethersailor/Rule-Bot/actions/workflows/docker-build.yml/badge.svg)](https://github.com/Aethersailor/Rule-Bot/actions/workflows/docker-build.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/aethersailor/rule-bot)](https://hub.docker.com/r/aethersailor/rule-bot)
[![License](https://img.shields.io/github/license/Aethersailor/Rule-Bot)](LICENSE)

[🚀 直接使用](https://t.me/asailor_rulebot) · [🔌 客户端](https://github.com/Aethersailor/Rule-Bot-Client) · [📚 使用文档](https://github.com/Aethersailor/Rule-Bot/wiki) · [🐳 Docker Hub](https://hub.docker.com/r/aethersailor/rule-bot) · [📦 GHCR](https://github.com/Aethersailor/Rule-Bot/pkgs/container/rule-bot) · [🔐 隐私说明](PRIVACY.md) · [🛡️ 安全策略](SECURITY.md)

</div>

---

Rule-Bot 把域名查询、网络信息检查、私聊确认、自动入口处理和 GitHub 提交放进一个清晰的流程，减少重复规则和误提交。

> [!IMPORTANT]
> **Rule-Bot 不是网页应用。** 项目维护的公共实例 [@asailor_rulebot](https://t.me/asailor_rulebot) 面向 [Custom_OpenClash_Rules](https://github.com/Aethersailor/Custom_OpenClash_Rules)，无需自行部署。
>
> 公共实例负责查询和维护补充直连规则文件 [`rule/Custom_Direct.list`](https://github.com/Aethersailor/Custom_OpenClash_Rules/blob/main/rule/Custom_Direct.list)。符合条件的域名会在 Telegram 私聊确认后，或由已授权的自动入口按策略处理后，以 `DOMAIN-SUFFIX` 规则写入该文件。维护其他规则仓库或需要独立服务时，再部署自己的实例。

## 🧭 选择使用方式

| | 直接使用公共实例 | 部署自己的实例 |
| --- | --- | --- |
| **适合场景** | 查询或维护 `Custom_OpenClash_Rules` 的补充直连规则 | 维护其他规则仓库，或需要独立配置与权限 |
| **需要准备** | Telegram | Docker 主机、Telegram Bot Token、GitHub Token |
| **开始方式** | 打开 [@asailor_rulebot](https://t.me/asailor_rulebot)，发送 `/start` | 阅读 [快速部署](#quick-deploy) 或 [Wiki 部署指南](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查) |

## ✨ 能做什么

| 能力 | 使用体验 |
| --- | --- |
| 🔎 **查询与去重** | 检查域名是否已存在于目标规则文件或 `GEOSITE:CN` |
| 🌐 **网络信息判断** | 结合 DNS、NS 和 GeoIP 结果，给出是否适合直连的建议 |
| ✅ **按入口策略提交** | 私聊需要确认；受信任群聊和已授权的 Rule-Bot Client 入口按各自策略自动处理 |
| 👥 **群组协作** | 在指定群组中响应 `@机器人` 的消息，自动检查并处理域名 |
| 📣 **可选管理能力** | 验证群成员身份、设置管理员、发送规则更新播报 |
| 🔌 **客户端接入** | 可选接收 [Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) 捕获的域名 |
| 🐳 **多架构镜像** | 提供 `linux/amd64` 和 `linux/arm64` Docker 镜像 |

> [!NOTE]
> 当前只管理直连规则。代理规则添加和规则删除尚未开放。

## 🔌 使用 Rule-Bot Client 自动发现域名

[Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) 是可选的配套客户端。它连接一个或多个 Mihomo 控制接口，读取日志和当前连接，筛选最终由兜底规则 `MATCH` 处理的域名，再把去重结果保存到本地清单。需要时，还可以把新域名发送给 Rule-Bot，由服务端检查重复规则、GeoSite 覆盖和直连策略，并把通过检查的域名写入目标 GitHub 规则仓库。

> [!IMPORTANT]
> Rule-Bot Client 不是代理客户端，不会修改 Mihomo 配置。Rule-Bot Client 的发送功能默认关闭；只使用本地收集时，域名不会发送给 Rule-Bot。

| 使用环境 | 客户端提供的方式 |
| --- | --- |
| Linux 或 NAS | Docker Compose、Debian 或 Ubuntu 软件包、原生二进制 |
| OpenWrt | 带 LuCI 管理页面的 IPK 或 APK 软件包，可自动发现本机 OpenClash 或 Nikki |
| 多个 Mihomo 实例 | 一个客户端可以连接多个控制接口并统一去重，无需为每个实例分别部署 |

开始使用：

- [下载最新版本](https://github.com/Aethersailor/Rule-Bot-Client/releases/latest)
- [阅读 Rule-Bot Client 用户文档](https://github.com/Aethersailor/Rule-Bot-Client/wiki)
- [配置 Rule-Bot 服务端接入](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入)
- [了解客户端隐私边界](https://github.com/Aethersailor/Rule-Bot-Client/blob/master/PRIVACY.md)

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
- 能够读写目标规则仓库内容的 GitHub Token；建议使用细粒度 Token，只授予目标仓库的 Contents 读写权限；
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
| `GITHUB_TOKEN` | `github_pat_replace_me` | 建议使用细粒度 Token，只授予目标仓库的 Contents 读写权限 |
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

## 🐳 镜像与版本渠道

仓库提供两类镜像标签：

| 标签 | 含义 | 适合用途 |
| --- | --- | --- |
| `latest` | `master` 每次通过构建后更新的滚动标签，可能领先最新 GitHub Release | 默认 Compose 部署和持续更新 |
| `X.Y.Z` | 对应 `vX.Y.Z` Git 标签和 GitHub Release 的版本标签 | 版本审计、问题复现和回滚 |

项目的 Compose 模板有意使用 `aethersailor/rule-bot:latest`。更新时执行 `docker compose pull && docker compose up -d`。需要确认正在运行的确切构建时，记录镜像 digest，并查看 OCI 标签 `org.opencontainers.image.revision`；digest 用于标识具体镜像，不要求把常规 Compose 改成固定版本。

- [查看 GitHub Releases](https://github.com/Aethersailor/Rule-Bot/releases)
- [查看 Docker Hub 标签](https://hub.docker.com/r/aethersailor/rule-bot/tags)

## 🗂️ 目标规则文件

`DIRECT_RULE_FILE` 指向一个已经存在的 UTF-8、逐行文本规则文件。文件可以混合保存注释、`DOMAIN-SUFFIX`、IP、端口和其他规则；Rule-Bot 只检查和新增 `DOMAIN-SUFFIX` 行，不会把其他规则类型当作重复项，也不会主动改写其他行。

新增规则时，Rule-Bot 同时写入来源注释和 `DOMAIN-SUFFIX,example.com`。如果文件中存在标记 `# 以下域名待提交 PR`，新内容插入在第一个标记之后；如果不存在，Rule-Bot 会在文件末尾创建该标记，再追加新内容。

## 📚 文档导航

| 文档 | 适合什么时候阅读 |
| --- | --- |
| 🏠 [Wiki 首页](https://github.com/Aethersailor/Rule-Bot/wiki) | 浏览全部用户文档并选择使用路径 |
| 🛠️ [部署与故障排查](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查) | 第一次部署、更新容器或处理启动问题 |
| ⚙️ [配置说明](https://github.com/Aethersailor/Rule-Bot/wiki/配置说明) | 启用群组验证、群聊、管理员、播报或调整数据源 |
| 🔌 [Rule-Bot Client 接入](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入) | 接入私用或社区客户端入口 |
| 🔐 [隐私说明](PRIVACY.md) | 了解 Telegram、Client API、网络元数据、公开提交和数据保留 |
| 🛡️ [安全策略](SECURITY.md) | 私密报告安全或隐私边界问题 |
| 📦 [GitHub Releases](https://github.com/Aethersailor/Rule-Bot/releases) | 核对稳定版本、发布时间和变更说明 |

## ⚠️ 使用限制

- 规则文件必须已经存在；Rule-Bot 管理其中的 `DOMAIN-SUFFIX,example.com` 行，其他行可以继续由原有维护流程管理。
- `.cn` 域名按现有策略默认直连，不会重复写入公开规则库。
- 每个 Telegram 账号每小时最多添加 50 个域名。
- 管理员可以绕过归属地策略，但不能绕过无效域名、重复规则和 `.cn` 处理。
- Rule-Bot 直接写入目标分支，不创建 Pull Request。建议为机器人使用独立 GitHub 凭据，并只授予目标仓库所需权限。

## 🔐 安全与隐私

Rule-Bot 会把成功添加的域名写入目标 GitHub 仓库及其提交历史；目标仓库公开时，这些内容也会公开。DNS、NS 和 GeoIP 检查会把待检查域名交给相应服务提供方。

Rule-Bot Client 入口默认关闭。启用前应阅读 [Wiki 接入说明](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入) 和 [隐私说明](PRIVACY.md)，使用 HTTPS 与 Bearer Token，并把监听端口限制在宿主机回环地址。隐藏 API 路径只能减少扫描噪声，不能代替鉴权。

## 🔗 相关项目与反馈入口

四个项目可以独立使用，也可以组成从规则维护到客户端反馈的流程：

| 项目 | 在流程中的职责 | 与 Rule-Bot 的关系 |
| --- | --- | --- |
| [Custom_OpenClash_Rules](https://github.com/Aethersailor/Custom_OpenClash_Rules) | 提供 OpenClash / Mihomo 配置、规则及其使用文档 | 项目公共实例的固定目标仓库 |
| [SubConverter-Extended](https://github.com/Aethersailor/SubConverter-Extended) | 按需转换订阅和生成客户端配置 | 可选的配置转换工具，不是 Rule-Bot 依赖 |
| [Rule-Bot](https://github.com/Aethersailor/Rule-Bot) | 检查域名并维护目标 GitHub 规则文件 | 当前项目 |
| [Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) | 本地收集 Mihomo `MATCH` 域名，并可选发送给 Rule-Bot | 可选输入端；本地收集不要求部署 Rule-Bot |

请选择与问题对象对应的入口：

| 问题或需求 | 入口 |
| --- | --- |
| 查询或添加少量公共直连域名 | [@asailor_rulebot](https://t.me/asailor_rulebot) |
| Rule-Bot 部署、Telegram 流程或 Client API 缺陷 | [提交 Rule-Bot Bug](https://github.com/Aethersailor/Rule-Bot/issues/new?template=bug_report.yml) |
| 本地收集、Mihomo 连接或客户端投递问题 | [Rule-Bot Client Issues](https://github.com/Aethersailor/Rule-Bot-Client/issues) |
| 规则内容、派生文件、CDN 或 OpenClash 使用问题 | [Custom_OpenClash_Rules Issues](https://github.com/Aethersailor/Custom_OpenClash_Rules/issues) |
| 订阅转换或生成配置问题 | [SubConverter-Extended Issues](https://github.com/Aethersailor/SubConverter-Extended/issues) |
| 安全漏洞或隐私边界问题 | [私密报告安全漏洞](https://github.com/Aethersailor/Rule-Bot/security/advisories/new) |

## 🌱 社区与许可

| 入口 | 链接 |
| --- | --- |
| 🤖 公共实例 | [@asailor_rulebot](https://t.me/asailor_rulebot) |
| 💬 交流群 | [Custom OpenClash Rules 交流群](https://t.me/custom_openclash_rules_group) |
| 📄 开源许可 | [GNU Affero General Public License v3.0](LICENSE) |

---

<div align="center">

如果 Rule-Bot 对你有帮助，欢迎通过对应项目的 Issue 提交建议，或将它分享给同样维护规则仓库的人。

</div>
