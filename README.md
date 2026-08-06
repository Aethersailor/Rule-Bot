# Rule-Bot

Rule-Bot 是一个面向 Mihomo / Clash 规则仓库的 Telegram 机器人。它把“查询域名、判断是否适合直连、写入 GitHub 规则文件”串成一套可审查的流程，也可以接收群聊消息和 [MatchScope](https://github.com/Aethersailor/MatchScope) 捕获的域名。

项目只管理直连域名规则。代理规则添加和规则删除目前尚未开放。

## 项目能力

- 查询域名是否已存在于目标 GitHub 规则文件或 `GEOSITE:CN`
- 结合域名、二级域名、DNS A 记录、NS 记录与 GeoIP 结果给出直连建议
- 经用户确认后，以 `DOMAIN-SUFFIX` 规则写入指定 GitHub 文件并返回提交链接
- 在指定 Telegram 群组中响应 `@机器人` 消息并自动处理域名
- 可选的群成员验证、管理员强制添加和私聊成功后的群组播报
- 通过彼此隔离的私用、社区 API 接收 MatchScope 域名
- 提供 `linux/amd64`、`linux/arm64` Docker 镜像

## 工作逻辑

无论域名来自 Telegram 私聊、群聊还是 MatchScope，最终都会进入同一套检查与写入逻辑：

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

### MatchScope

MatchScope 可以把实际流量中捕获的域名提交给 Rule-Bot。API 只接受精确配置路径上的 `POST application/json`：

```json
{"version": 1, "domain": "example.com"}
```

请求使用 `Authorization: Bearer <token>`。收到域名后，Rule-Bot 会执行与 Telegram 相同的规范化、去重、`GEOSITE:CN`、DNS、NS、GeoIP、频率限制和 GitHub 写入流程。

两类入口默认关闭，且使用不同的信任模型：

| 入口 | 默认端口 | 适用场景 | 鉴权方式 |
| --- | ---: | --- | --- |
| 私用入口 | `8765` | 部署者自己的 MatchScope | 部署者保存的静态高强度 Token |
| 社区入口 | `7654` | 向群成员提供公共接入服务 | Rule-Bot 为每位用户独立签发、续签和吊销的 Token |

社区入口必须同时启用群成员验证。用户在机器人私聊主菜单的“MatchScope 接入”页面阅读并确认[隐私说明](PRIVACY.md)后申请；签发时会实时检查群成员身份，Token 只显示一次，重新签发会立即废止旧 Token。升级后，既有社区 Token 会暂停，用户确认当前版本的隐私说明后可继续使用原 Token；撤回同意会同时吊销 Token。

随机 API 路径用于减少扫描噪声，不能代替 Token。建议只把两个端口发布到宿主机回环地址，再通过反向代理或 Cloudflare Tunnel 提供 HTTPS；不要把私用和社区入口合并成同一个端口、路径或共享凭据。公共 Cloudflare 主机还应使用按主机名限定的 Request Header Transform Rule 移除 `CF-Connecting-IP`、`X-Forwarded-For` 和 `True-Client-IP`，避免客户端 IP 继续进入源站；这不会阻止 Cloudflare 自身看到网络元数据。

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

也可以直接下载仓库中的完整示例，其中包含群聊、管理员和 MatchScope 的可选配置：

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

### 4. 按需启用 MatchScope

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
  MATCHSCOPE_PRIVATE_API_ENABLED: "true"
  MATCHSCOPE_PRIVATE_API_PORT: "8765"
  MATCHSCOPE_PRIVATE_API_PATH: "/api/v1/matchscope/replace-with-a-random-private-path"
  MATCHSCOPE_PRIVATE_API_TOKEN_FILE: "/run/secrets/rule-bot/private-token"
```

社区入口配置：

```yaml
environment:
  REQUIRED_GROUP_ID: "-1001234567890"
  REQUIRED_GROUP_NAME: "你的群组名称"
  REQUIRED_GROUP_LINK: "https://t.me/your_group"
  MATCHSCOPE_PUBLIC_API_ENABLED: "true"
  MATCHSCOPE_PUBLIC_API_PORT: "7654"
  MATCHSCOPE_PUBLIC_API_PATH: "/api/v1/matchscope/replace-with-a-different-random-path"
  MATCHSCOPE_PUBLIC_BASE_URL: "https://rule-bot.example.com"
  MATCHSCOPE_TOKEN_SIGNING_KEY_FILE: "/run/secrets/rule-bot/signing-key"
```

如果只启用其中一个入口，只发布对应端口，并删除不需要的密钥文件和配置。社区入口必须持久化 `/app/data`，否则容器重建后签发、吊销与隐私同意状态会丢失。

反向代理应分别把两个 HTTPS 域名转发到 `127.0.0.1:8765` 和 `127.0.0.1:7654`。如果不使用反向代理，也可以通过对应的 `MATCHSCOPE_*_TLS_CERT_FILE` 和 `MATCHSCOPE_*_TLS_KEY_FILE` 让 Rule-Bot 直接提供 TLS。

## 许可证

[GNU General Public License v3.0](LICENSE)
