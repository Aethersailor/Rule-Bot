# Rule-Bot

Rule-Bot 是一个面向 Mihomo / Clash 规则仓库的 Telegram 机器人。它把“查询域名、判断是否适合直连、写入 GitHub 规则文件”串成一套可审查的流程，也可以接收群聊消息和 [Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) 捕获的域名。

项目只管理直连域名规则。代理规则添加和规则删除目前尚未开放。

## 项目能力

- 查询域名是否已存在于目标 GitHub 规则文件或 `GEOSITE:CN`
- 结合域名、二级域名、DNS A 记录、NS 记录与 GeoIP 结果给出直连建议
- 经用户确认后，以 `DOMAIN-SUFFIX` 规则写入指定 GitHub 文件并返回提交链接
- 在指定 Telegram 群组中响应 `@机器人` 消息并自动处理域名
- 可选的群成员验证、管理员强制添加和私聊成功后的群组播报
- 通过彼此隔离的私用、社区 API 接收 Rule-Bot Client 域名
- 提供 `linux/amd64`、`linux/arm64` Docker 镜像

## 工作逻辑

无论域名来自 Telegram 私聊、群聊还是 Rule-Bot Client，最终都会进入同一套检查与写入逻辑：

```text
输入 URL 或域名
    ↓
规范化并提取用于规则的二级域名
    ↓
忽略默认直连的 .cn 域名
    ↓
检查目标 GitHub 规则与 GEOSITE:CN，避免重复
    ↓
查询域名/二级域名 IP、NS 与归属地
    ↓
符合直连策略且未触发频率限制
    ↓
写入规则文件并创建 GitHub commit
```

客户端不能指定规则文件、提交身份或绕过检查条件；这些内容始终由 Rule-Bot 的部署配置和服务端逻辑决定。

### Telegram 私聊

私聊提供菜单和以下命令：

| 命令 | 用途 |
| --- | --- |
| `/start` | 打开主菜单 |
| `/help` | 查看机器人内帮助 |
| `/query` | 查询域名状态、解析结果与添加建议 |
| `/add` | 检查域名并进入直连规则添加流程 |
| `/id` | 查看自己的 Telegram 用户 ID |
| `/skip` | 添加时跳过说明 |

查询不会修改仓库。添加流程会先检查重复项和归属地，符合条件时再让用户确认；写入成功后返回规则路径和 GitHub 提交链接。每个用户默认每小时最多添加 50 个域名。

配置 `REQUIRED_GROUP_ID`、`REQUIRED_GROUP_NAME`、`REQUIRED_GROUP_LINK` 后，私聊功能会要求用户先加入指定群组。验证失败时拒绝继续操作。

### Telegram 群聊

配置 `ALLOWED_GROUP_IDS` 后，机器人只在这些群组中工作，并且只响应明确 `@机器人` 的消息。支持两种方式：

- 在消息中同时写入域名并 `@机器人`
- 回复一条包含域名的消息，再 `@机器人`

群聊路径会自动执行查询和添加，不再要求私聊式二次确认；不符合条件、已经存在或属于 `.cn` 的域名不会写入。使用群聊模式前，需要在 @BotFather 中关闭机器人的 Privacy Mode，并将机器人重新加入群组。

`ANNOUNCEMENT_GROUP_ID` 是另一项独立功能：私聊成功添加规则后，机器人可以向指定群组发送一条静默播报。播报失败不会影响已经完成的 GitHub 提交，群聊内直接添加也不会重复播报。

### 管理员

`ADMIN_USER_IDS` 中的 Telegram 用户可以强制添加被归属地策略拒绝的域名。重复规则、无效域名和 `.cn` 默认直连逻辑仍由系统处理。

### Rule-Bot Client

Rule-Bot Client 可以把实际流量中捕获的域名提交给 Rule-Bot。API 只接受精确配置路径上的 `POST application/json`：

```json
{"version": 1, "domain": "example.com"}
```

请求使用 `Authorization: Bearer <token>`。收到域名后，Rule-Bot 会执行与 Telegram 相同的规范化、去重、`GEOSITE:CN`、DNS、NS、GeoIP、频率限制和 GitHub 写入流程。

两类入口默认关闭，且使用不同的信任模型：

| 入口 | 默认端口 | 适用场景 | 鉴权方式 |
| --- | ---: | --- | --- |
| 私用入口 | `8765` | 部署者自己的 Rule-Bot Client | 部署者保存的静态高强度 Token |
| 社区入口 | `7654` | 向群成员提供公共接入服务 | Rule-Bot 为每位用户独立签发、续签和吊销的 Token |

社区入口必须同时启用群成员验证。用户在机器人私聊主菜单的“Rule-Bot Client Community 接入”页面阅读并确认[隐私说明](PRIVACY.md)后申请；签发时会实时检查群成员身份，Token 只显示一次，重新签发会立即废止旧 Token。隐私说明版本更新后，现行社区 Token 会暂停，用户确认当前版本后才可继续使用；撤回同意会同时吊销 Token。

随机 API 路径用于减少扫描噪声，不能代替 Token。建议只把两个端口发布到宿主机回环地址，再通过反向代理或 Cloudflare Tunnel 提供 HTTPS；不要把私用和社区入口合并成同一个端口、路径或共享凭据。公共 Cloudflare 主机还应使用按主机名限定的 Request Header Transform Rule 移除 `CF-Connecting-IP`、`X-Forwarded-For` 和 `True-Client-IP`，避免客户端 IP 继续进入源站；这不会阻止 Cloudflare 自身看到网络元数据。

#### 隐私边界与必要警告

> [!WARNING]
> Rule-Bot Client 可能观察家庭、组织或共享网络中多个设备的连接域名。私用入口部署者和社区 Token 申请人必须确认自己有权收集并提交这些数据；Telegram 中的隐私同意只代表 Token 申请人，不代表其他网络用户或设备已经同意。

Rule-Bot 的服务端边界如下：

| 数据类别 | Rule-Bot 的处理 | 不会做的事 |
| --- | --- | --- |
| API 请求 | 只接受 `version` 和 `domain`，随后规范化并缩减为用于规则判断的域名 | 不接受客户端指定仓库、规则文件、提交身份、说明、来源或强制添加 |
| HTTP 与业务日志 | 关闭 Rule-Bot Client HTTP access log；业务事件使用每次进程启动时随机生成的短引用，不记录原始域名 | 不读取或保存客户端 IP，不把原始请求域名写入业务日志 |
| 限流状态 | 在进程内短期保存按私用入口或社区随机主体分组的请求时间，用于滚动限流 | 不把请求时间、次数、域名或 IP 持久化到数据库 |
| 社区 Token 数据库 | 保存 Telegram 用户 ID、随机 Token 主体、版本、签发/到期/启用状态，以及隐私说明版本和同意时间 | 不保存原始 Token、请求域名、客户端 IP 或最后使用时间 |
| GitHub 提交 | 成功添加后公开规则域名、Rule-Bot Client/Rule-Bot Client Community 来源标识和提交时间 | 不公开 Telegram 用户 ID、Token、客户端 IP 或 Rule-Bot Client 实例名 |

还必须理解以下限制：

- Rule-Bot Client 默认先将完整子域缩减为可注册域名，并在本地排除 `.cn`、用户排除项和重复主域；但协议允许客户端提交一个完整主机名，Rule-Bot 无法保证所有第三方客户端都执行了相同的本地隐私策略。Rule-Bot 会在内存中规范化后再处理，不会为失败、拒绝或重复请求建立域名数据库记录。
- 私用入口采用一个部署者静态 Token，没有社区入口的逐用户身份、同意、续签和吊销边界。任何获得该 Token 的人都可在轮换前使用私用入口，因此绝不能把它共享给社区用户。
- 社区 Token 是 Bearer 凭据：获得 Token 即可在过期或吊销前代表对应随机主体提交请求。Token 内只有不透明随机主体，不包含 Telegram 用户 ID；服务端数据库才保存二者映射。Token 只显示一次，应写入 Rule-Bot Client 的独立只读凭据文件；不要放入仓库、聊天记录、URL 查询参数或公开日志。
- 隐私说明升级后，既有社区 Token 会暂停；重新确认只恢复后续使用。撤回同意会吊销 Token，但不会删除 Rule-Bot Client 客户端本地保存的域名，也不能撤回已经进入 GitHub 历史的规则。
- HTTPS 保护传输，但不代替鉴权。隐藏路径不是密码。Cloudflare、反向代理或其他终止 TLS 的中间服务在技术上能够处理出口 IP、时间、请求路径、Authorization 头和域名正文；移除转发 IP 头只能避免源站继续收到这些头，不能让数据对 Cloudflare 不可见。
- Rule-Bot 的 DNS、NS 和 GeoIP 判断会把用于规则检查的域名交给配置的 DNS/DoH 服务；相应提供方可能看到查询域名。成功添加后，GitHub 仓库和 commit 历史是公开、持久的披露面，不能按临时私密数据处理。
- 私用和社区 Rule-Bot Client API 提交都不会触发 `ANNOUNCEMENT_GROUP_ID` 的 Telegram 群组播报；该播报只用于 Telegram 私聊成功提交。Rule-Bot Client 成功结果的公开面是目标 GitHub 规则及其提交历史。
- 如果不能接受第三方入口、DNS 提供方或公开 GitHub 历史的边界，应在 Rule-Bot Client 中保持 `rule_bot.enabled=false`，只在本地保存；也可以使用自管反向代理、直接 TLS 或受保护专网减少中间信任方。

完整服务端说明见 [PRIVACY.md](PRIVACY.md)，Rule-Bot Client 客户端的本地文件、主域缩减、排除项、代理和可靠投递边界见 [Rule-Bot Client 隐私说明](https://github.com/Aethersailor/Rule-Bot-Client/blob/master/PRIVACY.md)。

## Docker 部署

推荐使用 Docker Compose。开始前需要准备：

- Telegram Bot Token：通过 @BotFather 创建机器人后获取
- GitHub Token：对目标仓库的 Contents 具有读写权限
- 目标仓库路径：例如 `Aethersailor/Custom_OpenClash_Rules`
- 直连规则文件路径：相对于仓库根目录
- Docker 与 Docker Compose

### 1. 创建 Compose 文件

```bash
mkdir -p /opt/rule-bot
cd /opt/rule-bot
```

创建 `docker-compose.yml`：

```yaml
services:
  rule-bot:
    image: aethersailor/rule-bot:latest
    container_name: rule-bot
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: 1m
        max-file: "2"
    environment:
      TELEGRAM_BOT_TOKEN: "你的 Telegram Bot Token"
      GITHUB_TOKEN: "你的 GitHub Token"
      GITHUB_REPO: "用户名/仓库名"
      DIRECT_RULE_FILE: "规则文件路径"
      # GITHUB_BRANCH: "main" # 留空时使用仓库默认分支
      TZ: "Asia/Shanghai"
```

也可以直接下载仓库中的完整示例，其中包含群聊、管理员和 Rule-Bot Client 的可选配置：

```bash
wget -O docker-compose.yml https://raw.githubusercontent.com/Aethersailor/Rule-Bot/master/docker-compose.yml
```

不要把含有真实 Token 的 Compose 文件提交到 Git 仓库。

### 2. 启动并检查

```bash
docker compose pull
docker compose up -d
docker compose logs -f rule-bot
```

容器显示为 `healthy` 且日志中出现机器人启动成功后，即可在 Telegram 中发送 `/start`。

后续更新：

```bash
docker compose pull
docker compose up -d
```

### 3. 按需启用 Telegram 扩展功能

把需要的变量加入 `environment` 后重新执行 `docker compose up -d`：

| 场景 | 配置项 |
| --- | --- |
| 私聊前验证群成员身份 | `REQUIRED_GROUP_ID`、`REQUIRED_GROUP_NAME`、`REQUIRED_GROUP_LINK` |
| 在指定群聊中响应 | `ALLOWED_GROUP_IDS`，多个 ID 用逗号分隔 |
| 私聊添加成功后播报 | `ANNOUNCEMENT_GROUP_ID` |
| 管理员强制添加 | `ADMIN_USER_IDS`，多个用户 ID 用逗号分隔 |
| 指定目标分支 | `GITHUB_BRANCH` |

群组 ID 通常以 `-100` 开头，可以通过 @userinfobot 等方式获取。机器人需要已加入相关群组，并具备读取成员状态或发送消息所需的权限。

### 4. 按需启用 Rule-Bot Client

先创建持久化目录和密钥文件：

```bash
mkdir -p data secrets
openssl rand -hex 32 > secrets/private-token
openssl rand -hex 32 > secrets/signing-key
chown -R 1000:1000 data secrets
chmod 700 data secrets
chmod 400 secrets/private-token secrets/signing-key
```

然后为服务增加端口和数据卷：

```yaml
ports:
  - "127.0.0.1:8765:8765"
  - "127.0.0.1:7654:7654"
volumes:
  - ./data:/app/data
  - ./secrets:/run/secrets/rule-bot:ro
```

私用入口配置：

```yaml
environment:
  RULE_BOT_CLIENT_PRIVATE_API_ENABLED: "true"
  RULE_BOT_CLIENT_PRIVATE_API_PORT: "8765"
  RULE_BOT_CLIENT_PRIVATE_API_PATH: "/api/v1/rule-bot-client/replace-with-a-random-private-path"
  RULE_BOT_CLIENT_PRIVATE_API_TOKEN_FILE: "/run/secrets/rule-bot/private-token"
```

社区入口配置：

```yaml
environment:
  REQUIRED_GROUP_ID: "-1001234567890"
  REQUIRED_GROUP_NAME: "你的群组名称"
  REQUIRED_GROUP_LINK: "https://t.me/your_group"
  RULE_BOT_CLIENT_COMMUNITY_API_ENABLED: "true"
  RULE_BOT_CLIENT_COMMUNITY_API_PORT: "7654"
  RULE_BOT_CLIENT_COMMUNITY_API_PATH: "/api/v1/rule-bot-client/replace-with-a-different-random-path"
  RULE_BOT_CLIENT_COMMUNITY_API_BASE_URL: "https://rule-bot.example.com"
  RULE_BOT_CLIENT_COMMUNITY_TOKEN_SIGNING_KEY_FILE: "/run/secrets/rule-bot/signing-key"
  RULE_BOT_CLIENT_COMMUNITY_TOKEN_DATABASE: "/app/data/rule_bot_client_tokens.sqlite3"
```

如果只启用其中一个入口，只发布对应端口，并删除不需要的密钥文件和配置。社区入口必须持久化 `/app/data`，否则容器重建后签发、吊销与隐私同意状态会丢失。

反向代理应分别把两个 HTTPS 域名转发到 `127.0.0.1:8765` 和 `127.0.0.1:7654`。如果不使用反向代理，也可以通过对应的 `RULE_BOT_CLIENT_*_TLS_CERT_FILE` 和 `RULE_BOT_CLIENT_*_TLS_KEY_FILE` 让 Rule-Bot 直接提供 TLS。

## 许可证

[GNU General Public License v3.0](LICENSE)
