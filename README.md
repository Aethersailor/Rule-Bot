<div align="center">

# 🤖 Rule-Bot

**Look up domains in Telegram, assess whether they are suitable for direct routing, and maintain GitHub rules according to the policy for each submission method.**

A self-hosted Telegram bot for Mihomo / Clash direct-routing rule repositories.

English · [简体中文](README-zh-CN.md)

[![Build and Push Docker Image](https://github.com/Aethersailor/Rule-Bot/actions/workflows/docker-build.yml/badge.svg)](https://github.com/Aethersailor/Rule-Bot/actions/workflows/docker-build.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/aethersailor/rule-bot)](https://hub.docker.com/r/aethersailor/rule-bot)
[![License](https://img.shields.io/github/license/Aethersailor/Rule-Bot)](LICENSE)

[🚀 Use the Public Bot](https://t.me/asailor_rulebot) · [🔌 Client](https://github.com/Aethersailor/Rule-Bot-Client) · [📚 User Documentation](https://github.com/Aethersailor/Rule-Bot/wiki) · [🐳 Docker Hub](https://hub.docker.com/r/aethersailor/rule-bot) · [📦 GHCR](https://github.com/Aethersailor/Rule-Bot/pkgs/container/rule-bot) · [🔐 Privacy Notice](PRIVACY.md) · [🛡️ Security Policy](SECURITY.md)

</div>

---

> [!WARNING]
> This software is intended solely for study and research outside the People's Republic of China.
>
> **Organizations and individuals are prohibited from accessing, downloading, deploying, or using any part of this project in mainland China. If you are in mainland China, close this page immediately.**
>
> Organizations and individuals must not use any part of this project for activities that violate applicable laws or regulations. Users are individually responsible for the legal consequences of their use.

Rule-Bot combines domain lookups, network checks, private-chat confirmation, automated submission handling, and GitHub commits in one workflow to reduce duplicate rules and unintended submissions.

> [!IMPORTANT]
> **Rule-Bot is a Telegram bot. It has no web interface.** The project maintains [@asailor_rulebot](https://t.me/asailor_rulebot) as a public instance for [Custom_OpenClash_Rules](https://github.com/Aethersailor/Custom_OpenClash_Rules). You can use it without deploying your own instance.
>
> The public instance uses the supplemental direct-routing rule file [`rule/Custom_Direct.list`](https://github.com/Aethersailor/Custom_OpenClash_Rules/blob/main/rule/Custom_Direct.list) for lookups and maintains its entries. Eligible domains are added to this file as `DOMAIN-SUFFIX` rules after confirmation in a private Telegram chat or after an authorized automated submission method processes them according to its policy. Deploy your own instance if you need to maintain another rules repository or run a separate service.
>
> The public instance requires no group membership for standard private chats or for requesting or reissuing a community token. Users must still agree to the Privacy Notice before requesting a token.

> [!IMPORTANT]
> **Project scope**
>
> This project provides no proxy servers, proxy nodes, or node subscriptions for users. It operates no commercial or other proxy services and provides no internet access services. Neither the project nor its maintainers partner with, represent, or act as guarantors for such providers.

## 🧭 Choose How to Use Rule-Bot

| | Use the Public Instance | Deploy Your Own Instance |
| --- | --- | --- |
| **Use case** | Look up or maintain supplemental direct-routing rules for `Custom_OpenClash_Rules` | Maintain a different rules repository or use separate configuration and permissions |
| **Requirements** | Telegram | A Docker host, a Telegram bot token, and a GitHub token |
| **Get started** | Open [@asailor_rulebot](https://t.me/asailor_rulebot) and send `/start` | Read [Quick Deployment](#quick-deploy) or the [Wiki deployment guide](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查) |

## ✨ Features

| Feature | What it does |
| --- | --- |
| 🔎 **Lookup and deduplication** | Checks whether a domain is already in the target rule file or covered by `GEOSITE:CN` |
| 🌐 **Network checks** | Uses DNS, NS, and GeoIP results to recommend whether a domain is suitable for direct routing |
| ✅ **Submission policies** | Private chats require confirmation. Trusted group chats and authorized Rule-Bot Client submissions are handled automatically according to their respective policies |
| 👥 **Group collaboration** | Responds when mentioned in designated groups and automatically checks and processes domains |
| 📣 **Optional administration** | Verifies group membership, lets operators configure administrators, and announces rule updates |
| 🔌 **Client integration** | Can receive domains collected by [Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) |
| 🐳 **Multi-architecture images** | Provides Docker images for `linux/amd64` and `linux/arm64` |

> [!NOTE]
> Rule-Bot currently manages direct-routing rules only. Adding proxy rules and deleting rules are currently unavailable.

## 🔌 Discover Domains Automatically with Rule-Bot Client

[Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) is an optional companion client. It connects to one or more Mihomo controllers, reads logs and active connections, and selects domains from connections ultimately handled by the fallback rule `MATCH`. It deduplicates the results and saves them to a local list. It can also send new domains to Rule-Bot. The server checks for duplicate rules and GeoSite coverage, applies its direct-routing policy, and writes domains that pass the checks to the target GitHub rules repository.

> [!IMPORTANT]
> Rule-Bot Client does not proxy traffic or modify the Mihomo configuration. Sending is disabled by default. With local collection only, the client never sends domains to Rule-Bot.

| Environment | Client options |
| --- | --- |
| Linux or NAS | Docker Compose, packages for Debian or Ubuntu, or a native binary |
| OpenWrt | IPK or APK packages with a LuCI administration page and automatic discovery of OpenClash or Nikki on the same device |
| Multiple Mihomo instances | One client can connect to multiple controllers and deduplicate across them. A separate deployment for each Mihomo instance is unnecessary |

To get started, see the following resources.

- [Download the latest release](https://github.com/Aethersailor/Rule-Bot-Client/releases/latest)
- [Read the Rule-Bot Client documentation](https://github.com/Aethersailor/Rule-Bot-Client/wiki)
- [Configure the Rule-Bot server integration](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入)
- [Review the client's privacy boundaries](https://github.com/Aethersailor/Rule-Bot-Client/blob/master/PRIVACY.md)

## 💬 Use the Bot

### Private Chats

Send `/start` to open the main menu. You can also use the following commands directly.

| Command | Purpose |
| --- | --- |
| `/query` | Look up the domain's status and check results without changing rules |
| `/add` | Check a domain and start the workflow for adding a direct-routing rule |
| `/help` | View the bot's built-in help |
| `/id` | View your Telegram user ID |
| `/skip` | Skip the explanation when adding a rule and continue with the submission |

The add workflow normalizes the domain first, then checks `.cn` handling, the target rule file, `GEOSITE:CN`, DNS, NS, and GeoIP. The workflow can continue if the target domain's IP address is in mainland China. When the mainland China signal comes only from NS records, the result must pass checks using multiple resolvers and two GeoIP datasets, and meet the name-server quorum requirement. A match on a single NS IP address does not trigger an addition. Only eligible domains reach the confirmation step. After a successful submission, the bot returns the rule file path and a link to the GitHub commit.

### Group Membership Requirements

Operators can configure standard private chats and community token issuance independently. Both settings accept `true` and `false`. Omitting a setting preserves the behavior of earlier versions.

| Environment variable | Applies to | Behavior when omitted |
| --- | --- | --- |
| `TELEGRAM_PRIVATE_CHAT_REQUIRE_GROUP` | Standard Telegram private chats | Requires group membership when `REQUIRED_GROUP_ID`, `REQUIRED_GROUP_NAME`, and `REQUIRED_GROUP_LINK` are all valid |
| `RULE_BOT_CLIENT_COMMUNITY_TOKEN_REQUIRE_GROUP` | Community token issuance and reissuance | Requires group membership when the Community API is enabled |

Features that require membership share the three group settings above. Set the relevant switch to `false` to waive the group membership requirement for that feature. Community tokens are still issued through Telegram accounts, and users must confirm the Privacy Notice first.

### Group Chats

With group chat mode enabled, you can submit a domain in either of the following ways.

- Include a domain in a message and mention the bot with @.
- Reply to a message that contains a domain and mention the bot with @.

> [!WARNING]
> **Group chat mode checks and adds domains automatically, without the extra confirmation step used in private chats.** Add the bot only to trusted groups and restrict which groups can use it with `ALLOWED_GROUP_IDS`.

Group chat mode requires turning off the bot's Privacy Mode in [@BotFather](https://t.me/BotFather) and re-adding the bot to the group afterward. See the [Wiki configuration guide](https://github.com/Aethersailor/Rule-Bot/wiki/配置说明#telegram-groups) for the complete setup.

## 🔄 How Rule-Bot Processes a Domain

| Step | Action | Result |
| :---: | --- | --- |
| **1** | Extract the registrable domain from the supplied URL or domain | Normalize the domain to check |
| **2** | Skip `.cn` domains, which already use direct routing by default, and check the target rule file and `GEOSITE:CN` | Avoid duplicate entries |
| **3** | Query DNS, NS, and GeoIP information. For results based on NS alone, check evidence from multiple resolvers and verify that a quorum of name servers supports the result | Determine whether there is reliable evidence for direct routing based on a mainland China signal |
| **4** | Follow the private-chat confirmation flow or the applicable automated processing policy | Add a `DOMAIN-SUFFIX` rule and create a GitHub commit if the domain qualifies |

Lookup operations leave the repository untouched. Additions in private chats require user confirmation. Group chats and Rule-Bot Client submissions use their own automated processing policies.

<a id="quick-deploy"></a>

## 🚀 Quick Deployment

### 1. Prepare the Host and Credentials

- A host with Docker and Docker Compose installed.
- A Telegram bot token created with [@BotFather](https://t.me/BotFather).
- A GitHub token with read and write access to the contents of the target rules repository. A fine-grained token with **Contents** set to **Read and write** for only the target repository is recommended.
- An existing target repository and direct-routing rule file.

### 2. Download the Compose Template

```bash
mkdir rule-bot && cd rule-bot
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/Aethersailor/Rule-Bot/master/docker-compose.yml
```

### 3. Fill In the Required Settings

Edit `docker-compose.yml` and fill in at least the following four settings.

| Setting | Example | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | `123456:replace-me` | The bot token from [@BotFather](https://t.me/BotFather) |
| `GITHUB_TOKEN` | `github_pat_replace_me` | A fine-grained token with **Contents** set to **Read and write** for only the target repository is recommended |
| `GITHUB_REPO` | `owner/repository` | The target GitHub repository |
| `DIRECT_RULE_FILE` | `rule/Direct.list` | The rule file path relative to the repository root |

### 4. Check the Configuration and Start the Bot

Make sure you have replaced all four required values, then start the container.

```bash
grep -nE '^[[:space:]]*-[[:space:]]+(TELEGRAM_BOT_TOKEN|GITHUB_TOKEN|GITHUB_REPO|DIRECT_RULE_FILE)=your_' docker-compose.yml
chmod 600 docker-compose.yml
docker compose pull
docker compose up -d
docker compose logs -f rule-bot
```

> [!TIP]
> **Successful startup**. The container status is `healthy`, the log contains “机器人启动成功，开始轮询” (“Bot started successfully and is polling”), and sending `/start` in Telegram opens the main menu.

If startup fails, see [Troubleshooting in the Wiki](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查#troubleshooting).

> [!CAUTION]
> `docker-compose.yml` contains Telegram and GitHub credentials. Never commit a file that contains real tokens to Git or post the full configuration or logs directly to an issue.

## 🐳 Images and Version Channels

The repository provides two types of image tags.

| Tag | Meaning | Use |
| --- | --- | --- |
| `latest` | Rolling tag updated after each successful build on `master`. It may be ahead of the latest GitHub Release | Default Compose deployments and ongoing updates |
| `X.Y.Z` | Version tag corresponding to the `vX.Y.Z` Git tag and GitHub Release | Version auditing, reproducing issues, and rolling back |

The project's Compose template intentionally uses `aethersailor/rule-bot:latest`. To update, run `docker compose pull && docker compose up -d`. To identify the exact build in use, record the image digest and check the OCI label `org.opencontainers.image.revision`. The digest identifies a specific image, and routine Compose deployments can continue using the rolling tag.

- [View GitHub Releases](https://github.com/Aethersailor/Rule-Bot/releases)
- [View Docker Hub tags](https://hub.docker.com/r/aethersailor/rule-bot/tags)

## 🗂️ Target Rule File

`DIRECT_RULE_FILE` points to an existing UTF-8 plain-text file with one entry per line. The file can contain a mix of comments, `DOMAIN-SUFFIX` rules, IP addresses, ports, and other rules. Rule-Bot checks and adds only `DOMAIN-SUFFIX` lines. It leaves other lines as they are and does not count other rule types as duplicates.

When adding a rule, Rule-Bot writes a source comment along with `DOMAIN-SUFFIX,example.com`. If the file contains the marker `# 以下域名待提交 PR`, the bot inserts the new content after the first occurrence. If the marker is absent, the bot adds it to the end of the file and appends the new content after it.

## 📚 Documentation

| Document | When to use it |
| --- | --- |
| 🏠 [Wiki Home](https://github.com/Aethersailor/Rule-Bot/wiki) | Browse the full user documentation and choose how to use Rule-Bot |
| 🛠️ [Deployment and Troubleshooting](https://github.com/Aethersailor/Rule-Bot/wiki/部署与故障排查) | Deploy for the first time, update the container, or troubleshoot startup |
| ⚙️ [Configuration](https://github.com/Aethersailor/Rule-Bot/wiki/配置说明) | Enable group verification, group chats, administrators, or announcements, or adjust data sources |
| 🔌 [Rule-Bot Client Integration](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入) | Connect clients through private or community submission endpoints |
| 🔐 [Privacy Notice](PRIVACY.md) | Understand Telegram, the Client API, network metadata, public commits, and data retention |
| 🛡️ [Security Policy](SECURITY.md) | Report security or privacy issues privately |
| 📦 [GitHub Releases](https://github.com/Aethersailor/Rule-Bot/releases) | Check stable versions, release dates, and release notes |

## ⚠️ Usage Limits

See the [performance notes](docs/performance.md) for concurrency, upgrade boundaries, and data validation. Updates continue to use the existing deployment configuration and client protocol.

- The rule file must already exist. Rule-Bot manages `DOMAIN-SUFFIX,example.com` lines in that file. Existing maintenance workflows can continue to manage the other lines.
- Under the current policy, `.cn` domains use direct routing by default and are not added redundantly to the public rules repository.
- Each Telegram account can add up to 50 domains per hour.
- Administrators can bypass the geolocation policy. Invalid domains, duplicate rules, and `.cn` handling still follow the standard checks.
- Rule-Bot writes directly to the target branch without opening a pull request. Use separate GitHub credentials for the bot and grant only the permissions needed for the target repository.

## 🔐 Security and Privacy

Rule-Bot writes domains it successfully adds to the target GitHub repository and its commit history. Those domains are public if the target repository is public. DNS, NS, and GeoIP checks disclose the domains being checked to the respective service providers.

The Rule-Bot Client endpoint is disabled by default. Before enabling it, read the [Wiki integration guide](https://github.com/Aethersailor/Rule-Bot/wiki/Rule-Bot-Client-接入) and the [Privacy Notice](PRIVACY.md). Use HTTPS and bearer tokens, and bind the listening port only to the host's loopback address. Hiding the API path reduces scan noise. Authentication is still required.

If DNS results are temporarily inconclusive, the Client API persists the domain and retries it in the background. Users can continue using their current client. For compatibility with older clients, the response to that request uses `HTTP 200` and `status=rejected_policy` with `deferred=true`. The rule is omitted for now, and the server handles subsequent checks. Older clients continue to the next record. GitHub failures, queue write failures, and rate limiting still return retryable errors. Persist the entire data directory to retain the background queue. See the [Privacy Notice](PRIVACY.md) for what data is stored.

## 🔗 Related Projects and Where to Get Help

The four projects can be used independently or together in a workflow that covers rule maintenance and feedback from clients.

| Project | Role in the workflow | Relationship to Rule-Bot |
| --- | --- | --- |
| [Custom_OpenClash_Rules](https://github.com/Aethersailor/Custom_OpenClash_Rules) | Provides OpenClash / Mihomo configurations, rules, and usage documentation | The fixed target repository of the project's public instance |
| [SubConverter-Extended](https://github.com/Aethersailor/SubConverter-Extended) | Converts subscriptions and generates client configurations on demand | An optional configuration conversion tool, independent of Rule-Bot |
| [Rule-Bot](https://github.com/Aethersailor/Rule-Bot) | Checks domains and maintains the target GitHub rule file | This project |
| [Rule-Bot Client](https://github.com/Aethersailor/Rule-Bot-Client) | Collects Mihomo `MATCH` domains locally and can optionally send them to Rule-Bot | An optional source of domains. Local collection works without a Rule-Bot deployment |

Use the contact point that matches the issue or request.

| Issue or request | Where to go |
| --- | --- |
| Look up or add a small number of public direct-routing domains | [@asailor_rulebot](https://t.me/asailor_rulebot) |
| Rule-Bot deployment, Telegram workflows, or Client API bugs | [Report a Rule-Bot bug](https://github.com/Aethersailor/Rule-Bot/issues/new?template=bug_report.yml) |
| Local collection, Mihomo connections, or client delivery issues | [Rule-Bot Client Issues](https://github.com/Aethersailor/Rule-Bot-Client/issues) |
| Rule contents, derived files, CDN, or OpenClash usage | [Custom_OpenClash_Rules Issues](https://github.com/Aethersailor/Custom_OpenClash_Rules/issues) |
| Subscription conversion or configuration generation | [SubConverter-Extended Issues](https://github.com/Aethersailor/SubConverter-Extended/issues) |
| Security vulnerabilities or privacy issues | [Private vulnerability report](https://github.com/Aethersailor/Rule-Bot/security/advisories/new) |

## 🌱 Community and License

| Resource | Link |
| --- | --- |
| 🤖 Public instance | [@asailor_rulebot](https://t.me/asailor_rulebot) |
| 📄 Open-source license | [GNU Affero General Public License v3.0](LICENSE) |

---

<div align="center">

If Rule-Bot is useful to you, post suggestions in the relevant project's issues or share Rule-Bot with others who maintain rules repositories.

</div>
