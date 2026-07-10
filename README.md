# Rule-Bot

一个用于管理 Mihomo 规则的 Telegram 机器人，支持域名查询与直连规则添加。

- ✅ 自动检查 GitHub 规则与 GEOSITE:CN
- ✅ DNS / NS 归属地判断，给出添加建议
- ✅ 群组验证与群组 @ 提及模式
- ✅ Docker 部署，支持 `linux/amd64` 与 `linux/arm64`

> 不提供 Windows 支持，仅建议 Docker 部署。

## 🧰 准备工作

### 获取 Telegram Bot Token

1) 打开 @BotFather  
2) 发送 `/newbot`，按提示设置机器人名称与用户名  
3) 复制并保存返回的 Token  

**BotFather 推荐设置（按需）**

- 群组模式必需：`/mybots` → 选择机器人 → `Bot Settings` → `Group Privacy` → `Turn off`  
- 允许加入群组：确认 `Allow Groups` 为启用（默认开启）  
- 可选：`/setcommands` 设置命令菜单  

建议的命令列表：

```text
start - 主菜单
help - 帮助信息
query - 查询域名
add - 添加规则入口
delete - 删除规则（暂不可用）
skip - 跳过说明
```

### 获取 GitHub Token

1) 打开 GitHub → Settings → Developer settings → Personal access tokens  
2) 二选一创建 Token（推荐 Classic 简单直观）  

**Classic Token**

- 选择 `Generate new token (classic)`  
- 勾选权限：`repo`  

**Fine-grained Token**

- 选择 `Generate new token`  
- 指定仓库  
- 权限：`Contents` 设为 `Read and write`  

> Token 只显示一次，请妥善保存。

### 获取群组 ID（群组模式 / 群组验证需要）

把 @userinfobot 加入群组，它会返回完整群组 ID（通常以 `-100` 开头）。  

## ⚡ 快速开始（Docker）

1) 创建工作目录并下载配置文件

```bash
mkdir -p /opt/Rule-Bot && cd /opt/Rule-Bot
wget https://raw.githubusercontent.com/Aethersailor/Rule-Bot/main/docker-compose.yml
```

1) 编辑配置文件

```bash
vim docker-compose.yml
```

修改以下必填参数（去掉 `#` 注释，填入你的实际值）：

- `TELEGRAM_BOT_TOKEN`
- `GITHUB_TOKEN`
- `GITHUB_REPO`
- `DIRECT_RULE_FILE`
- `GITHUB_BRANCH`（可选，不填则使用仓库默认分支）

1) 启动容器

```bash
docker compose up -d
```

1) 查看日志

```bash
docker compose logs -f rule-bot
```

## ⚙️ 配置一览

### 必需参数

- `TELEGRAM_BOT_TOKEN`：从 @BotFather 获取（见“准备工作”）
- `GITHUB_TOKEN`：需要 `repo` 权限（见“准备工作”）
- `GITHUB_REPO`：格式 `用户名/仓库名`
- `DIRECT_RULE_FILE`：规则文件路径（仓库内）
- `GITHUB_BRANCH`：目标分支（可选，不填则使用仓库默认分支）

### 可选参数（展开查看）

<details>
<summary>点击展开</summary>

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `PROXY_RULE_FILE` | 代理规则文件（暂不使用） | 空 |
| `GITHUB_BRANCH` | GitHub 目标分支（读写规则文件） | 空（默认分支） |
| `GITHUB_COMMIT_EMAIL` | 提交邮箱 | `noreply@users.noreply.github.com` |
| `LOG_LEVEL` | 日志级别：`DEBUG/INFO/WARNING/ERROR` | `INFO` |
| `LOG_FORMAT` | 日志格式：`compact/verbose` | `compact` |
| `DATA_UPDATE_INTERVAL` | 数据更新间隔（秒） | `21600`（6 小时） |
| `DATA_DIR` | 数据目录（容器内路径） | `/app/data` |
| `DOH_SERVERS` | A 记录 DoH 列表，逗号分隔 `name=url` | 内置默认 |
| `NS_DOH_SERVERS` | NS 记录 DoH 列表，逗号分隔 `name=url` | 内置默认 |
| `REQUIRED_GROUP_ID` | 群组验证 ID | 空 |
| `REQUIRED_GROUP_NAME` | 群组验证名称 | 空 |
| `REQUIRED_GROUP_LINK` | 群组验证链接 | 空 |
| `ALLOWED_GROUP_IDS` | 群组模式允许的群组 ID，逗号分隔 | 空 |
| `ADMIN_USER_IDS` | 管理员 Telegram 用户 ID，逗号分隔 | 空 |
| `TZ` | 时区 | `Asia/Shanghai` |
| `DNS_CACHE_TTL` | DNS A 记录缓存秒数 | `60` |
| `DNS_CACHE_SIZE` | DNS A 记录缓存上限 | `1024` |
| `NS_CACHE_TTL` | DNS NS 记录缓存秒数 | `300` |
| `NS_CACHE_SIZE` | DNS NS 记录缓存上限 | `512` |
| `DNS_MAX_CONCURRENCY` | DNS 并发限制 | `20` |
| `DNS_CONN_LIMIT` | DNS 全局连接池上限 | `30` |
| `DNS_CONN_LIMIT_PER_HOST` | DNS 单主机连接上限 | `10` |
| `DNS_TIMEOUT_TOTAL` | DNS 请求总超时 | `10` |
| `DNS_TIMEOUT_CONNECT` | DNS 连接超时 | `3` |
| `GEOSITE_CACHE_TTL` | GeoSite 查询缓存秒数 | `3600` |
| `GEOSITE_CACHE_SIZE` | GeoSite 查询缓存上限 | `2048` |
| `GEOIP_CACHE_TTL` | GeoIP 缓存秒数 | `21600` |
| `GEOIP_CACHE_SIZE` | GeoIP 缓存上限 | `4096` |
| `GITHUB_FILE_CACHE_TTL` | 规则文件缓存秒数 | `60` |
| `GITHUB_FILE_CACHE_SIZE` | 规则文件缓存上限 | `4` |
| `METRICS_ENABLED` | 开启 metrics 导出 | `false` |
| `METRICS_EXPORT_PATH` | metrics 输出路径 | `/tmp/rule-bot-metrics.json` |
| `METRICS_EXPORT_INTERVAL` | metrics 导出间隔秒数 | `30` |
| `METRICS_RESET_ON_EXPORT` | 导出后清零 | `false` |
| `MEMORY_SOFT_LIMIT_MB` | 可选进程软限制 MB | 空（交由容器/宿主机管理） |
| `MEMORY_HARD_LIMIT_MB` | 可选进程硬限制 MB | 空（交由容器/宿主机管理） |
| `MEMORY_TRIM_ENABLED` | 启用内存修剪 | `true` |

</details>

> 容器默认以非 root 用户运行，若挂载宿主机目录到 `/app/data`，请确保目录属主为 `UID/GID=1000`。

## 🧭 使用方式

### 私聊命令

- `/start`：主菜单
- `/help`：帮助信息
- `/query`：查询域名
- `/add`：添加规则入口
- `/delete`：暂不可用
- `/skip`：跳过域名说明

### 群组模式（ALLOWED_GROUP_IDS）

- 仅在白名单群组中响应
- 仅响应 **@ 机器人** 的消息
- 支持“回复包含域名的消息 + @ 机器人”

> 使用群组模式需要关闭机器人 Privacy Mode，并重新添加机器人到群组。

### 群组验证（REQUIRED_GROUP_*）

同时配置 `REQUIRED_GROUP_ID/NAME/LINK` 后生效，未通过或校验失败会拒绝访问（失败即拒绝）。

### 管理员模式（ADMIN_USER_IDS）

配置 `ADMIN_USER_IDS` 后，指定的管理员用户可以：

- 强制添加被系统检测拒绝的域名
- 获取调试辅助信息

> 通过 @userinfobot 获取你的 Telegram 用户 ID。

## 📌 规则逻辑（简版）

1) 解析域名并提取二级域名  
2) 检查 GitHub 规则与 GEOSITE:CN  
3) DNS / NS 归属地检测  
4) 符合条件则自动添加

> `.cn` 域名默认直连，不允许添加。

## 🐳 镜像与版本

- `latest`：唯一发布标签，对应 `master` 分支通过测试后的多架构镜像

镜像内置事件循环心跳健康检查；部署者不需要增加端口、环境变量或额外配置。

## 🧩 常见问题

**群组不响应消息**

1. 关闭 Privacy Mode  
2. 重新添加机器人到群组  
3. 仅在消息中 @ 机器人  

**挂载数据目录后权限报错**
把宿主机目录 `chown -R 1000:1000` 再启动容器。

## 💻 本地开发（可选）

- Python 3.12+
- `pip install -r requirements.txt`

## 🧪 1C1G 运行建议

建议对容器设置内存上限与 CPU 配额，避免全机抖动：

```yaml
services:
  rule-bot:
    mem_limit: 256m
    mem_reservation: 128m
    cpus: "0.5"
```

如果你使用的是 Swarm 模式，可改用 `deploy.resources` 语法。

**Swap/RSS 观测**

```bash
pid=$(pgrep -f "python -m src.main" | head -n 1)
grep -E "VmRSS|VmSize|VmSwap" /proc/$pid/status
```

**性能采样（需开启 metrics 导出）**

```bash
export METRICS_ENABLED=1
export METRICS_EXPORT_INTERVAL=30
python tools/profile_runtime.py --process-name "python -m src.main" --duration 300 --interval 5
```

**10 分钟压力模拟**

```bash
python tools/stress_sim.py --duration 600 --concurrency 4 --pause 0.5
```

## 📄 许可证

GPLv3
