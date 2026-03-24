# Email Relay Bridge Worker

跨 Cloudflare 账户的邮件中继方案，用于将**域名所在账户（账户 A）**收到的邮件，转发给**部署了 cloudflare_temp_email 的账户（账户 B）**。

## 背景

Cloudflare Email Routing 只能将邮件路由到**同一账户**下的 Worker。  
如果你的域名和临时邮件服务分布在不同的 Cloudflare 账户，需要通过本桥接方案打通。

```
账户 A（域名所在）                       账户 B（cloudflare_temp_email）
──────────────────────────────           ─────────────────────────────────────
  MX → Email Routing                       Worker: cloudflare_temp_email
       └─ Catch-all                              │
            └─ 本桥接 Worker  ──────────→  POST /external/api/relay_email
                                                 │
                                         存储 / Webhook / Telegram / AI 提取
```

---

## 目录结构

```
worker-bridge/
├── src/
│   └── worker.ts              # 桥接 Worker 源码
├── scripts/
│   ├── batch-deploy.py        # 多账户批量部署脚本
│   ├── accounts.example.yaml  # 配置文件模板
│   ├── accounts.yaml          # 真实配置（gitignored，勿提交）
│   └── requirements.txt       # Python 依赖
├── wrangler.toml              # 单账户手动部署配置
├── package.json
└── tsconfig.json
```

---

## 方式一：单账户手动部署

适合只有一个额外域名账户的场景。

### 前提

- Node.js 18+，已安装 `wrangler`
- 主 Worker（账户 B）已设置 `MAIL_RELAY_SECRET` 环境变量

### 步骤

```bash
cd worker-bridge

# 1. 安装依赖
npm install        # 或 pnpm install

# 2. 修改 wrangler.toml，填入主 Worker 的 URL
#    MAIN_WORKER_URL = "https://your-main-worker.workers.dev"

# 3. 使用 Wrangler 登录账户 A
npx wrangler login

# 4. 部署桥接 Worker
npx wrangler deploy

# 5. 设置共享密钥（与主 Worker 的 MAIL_RELAY_SECRET 保持一致）
npx wrangler secret put MAIL_RELAY_SECRET
```

然后在账户 A 的 Cloudflare 控制台中，进入域名的 **Email Routing → Catch-all**，将操作设为 **Send to Worker**，选择 `cloudflare-email-relay-bridge`。

---

## 方式二：多账户批量部署（推荐）

适合管理多个 Cloudflare 账户 / 域名的场景。脚本会自动：

1. 解析每个账户的 Account ID
2. 上传 / 更新桥接 Worker（含环境变量绑定）
3. 为每个域名启用 Email Routing
4. 配置 Catch-all 规则 → Worker

### 前提

- Python 3.9+
- 每个账户的 **Global API Key**（不是 API Token，需要有 Worker 和 Zone 写权限）
- 域名已托管在对应的 Cloudflare 账户（NS 已指向 CF）

### 快速开始

```bash
cd worker-bridge/scripts

# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 创建配置文件
cp accounts.example.yaml accounts.yaml
# 用编辑器填写真实的凭证和域名（见下方配置说明）

# 3. 空跑预览（不做任何修改）
python3 batch-deploy.py --dry-run

# 4. 确认无误后执行真实部署
python3 batch-deploy.py
```

### 配置文件 `accounts.yaml` 说明

```yaml
# 账户 B 中 cloudflare_temp_email Worker 的公开访问 URL
main_worker_url: "https://your-main-worker.workers.dev"

# 共享密钥，必须与主 Worker 的 MAIL_RELAY_SECRET 完全一致
# 生成方法：openssl rand -hex 32
mail_relay_secret: "your-strong-random-secret"

# 部署到每个账户的 Worker 名称（默认即可）
worker_name: "cloudflare-email-relay-bridge"

accounts:
  - email: "you@example.com"           # CF 登录邮箱
    api_key: "GLOBAL_API_KEY_HERE"     # Global API Key
    # account_id: "abc123"             # 可选；留空自动解析
    domains:
      - "domain1.com"
      - "domain2.com"

  - email: "other@example.com"
    api_key: "GLOBAL_API_KEY_HERE"
    domains:
      - "domain3.com"
```

> ⚠ **安全提示**：`accounts.yaml` 已加入 `.gitignore`，请勿将包含真实凭证的文件提交到版本控制。

### 命令行参数

| 参数 | 说明 |
|------|------|
| `--dry-run` | 预览所有操作，不发出任何真实 API 请求 |
| `--config <path>` | 指定配置文件路径（默认 `accounts.yaml`） |

---

## 主 Worker 侧配置

在账户 B 的 `cloudflare_temp_email` Worker 中，需设置一个环境变量：

```bash
# 推荐通过 Wrangler Secret 设置，避免明文出现在 wrangler.toml
cd worker
wrangler secret put MAIL_RELAY_SECRET
```

或在 `wrangler.toml` 中添加（不推荐明文存储）：

```toml
# MAIL_RELAY_SECRET = "your-strong-random-secret"
```

收到的邮件会经过完整的处理流水线：

- 存入 D1 数据库（`raw_mails` 表）
- Telegram Bot 通知
- Webhook 触发
- Another Worker（RPC）调用
- AI 邮件信息提取

---

## 安全说明

| 机制 | 说明 |
|------|------|
| `X-Relay-Secret` 请求头 | 桥接 Worker 每次转发都携带共享密钥，主 Worker 验证后才处理邮件 |
| `secret_text` binding | 批量部署时，密钥以 `secret_text` 类型写入 Worker binding，不可通过 API 读回 |
| HTTPS 传输 | 两端均为 Cloudflare Workers / HTTPS，传输全程加密 |
| 最小权限 | 桥接 Worker 没有任何 DB / KV / 存储绑定，仅做 HTTP 转发 |

---

## 常见问题

**Q: Email Routing 提示"需要先添加目标地址"**  
A: CF Email Routing 要求至少一个已验证的目标地址才能启用。在账户 A 的 Email Routing 页面添加并验证一个真实邮箱地址后，脚本重跑即可。

**Q: 批量部署提示 DNS 记录未生效**  
A: Email Routing 依赖特定的 MX / TXT DNS 记录，脚本会打印缺少的记录内容。CF 通常会自动创建这些记录，若未自动创建则需手动添加，等待 DNS 传播后重跑。

**Q: `account_id` 是否必须填写**  
A: 不必须。脚本会自动调用 `/accounts` API 解析。如果该 API Key 对应多个账户（如使用了组织账户），建议显式填写 `account_id` 以避免歧义。

**Q: 能否用 API Token 代替 Global API Key**  
A: 可以，但 API Token 需要同时具备以下权限：
- `Workers Scripts:Edit`（账户级）
- `Zone:Email Routing:Edit`（Zone 级）
- `Zone:Zone:Read`（Zone 级，用于解析 Zone ID）

将 `api_key` 字段替换为 Token 值，并在 `CloudflareClient` 请求头中将 `X-Auth-Key` 替换为 `Authorization: Bearer <token>` 即可（需修改 `batch-deploy.py`）。
