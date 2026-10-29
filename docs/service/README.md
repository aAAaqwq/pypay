# pypay 统一支付服务

`pypay_service` 是在原有 `src/pypay` 渠道适配层之上增加的多项目统一支付服务：

- 业务网站后端通过 HMAC 认证调用 `/v1/*` 创建支付、查询、关单、退款。
- pypay 生成不可篡改的订单快照和不可猜测的收银台链接 `/pay/{token}`。
- 收银台展示项目名、商品、金额、有效期、微信/支付宝入口和支付状态。
- 渠道回调（微信/支付宝 → pypay）先验签，再在同一事务内更新订单并写 outbox。
- 独立 worker 向订单所属项目投递签名 webhook，支持重试、退避、投递记录、手动重发和失败告警。
- 管理后台支持项目、渠道账户、订单、通知记录、人工补查/重发、权限受控退款和审计。

浏览器永远不决定金额；pypay 不直接修改任何业务网站数据库。

## 架构与边界

```text
业务网站后端
  │  1. HMAC 签名请求（服务间，不经浏览器）
  ▼
pypay_service  /v1/*  ───────────────►  payment_orders / payment_attempts
  │                                      refunds / outbox_events
  │  2. 返回 /pay/{token}
  ▼
浏览器收银台  /pay/{token}
  │  3. 选择微信/支付宝，服务端向渠道下单
  ▼
渠道适配层（src/pypay）──► 微信 APIv3 Native / 支付宝电脑网站支付
  ▲
  │  4. 渠道原始回调：/notify/{channel}/{account_id}
  │     验签 → 事务内去重/校验/更新 → outbox
  │
  └── 独立 worker ──► 5. 项目 notify_url（HMAC 签名事件）
                        业务网站验签、按 event_id 幂等、事务提交后返回 2xx
```

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| 业务网站后端 | 价格、业务订单、购买者、权益发放 | 不持有商户私钥，不直接解析渠道回调 |
| pypay_service | 支付订单、渠道下单、收银台、回调验签、退款、通知投递 | 不保存业务用户/额度，不直接写业务库 |
| 浏览器收银台 | 展示商品/金额/二维码/表单、轮询状态 | 不决定金额，不修改订单状态 |

## 目录

```text
src/pypay_service/
├── app.py                 # FastAPI 工厂、安全中间件、健康检查
├── config.py              # 服务配置、渠道账户种子
├── db.py                  # SQLite 连接、事务、迁移运行器
├── migrations/            # 可回滚 SQL 迁移
├── security.py            # SecretBox、HMAC、URL/SSRF、admin/CSRF
├── projects_service.py    # 项目创建、认证、凭证轮换
├── payments_service.py    # 下单、attempt、查单、关单、过期
├── callbacks_service.py   # 渠道回调状态应用
├── payment_state.py       # 订单/attempt/退款状态机（唯一事实源）
├── refunds_service.py     # 退款申请、查询、补偿
├── outbox_service.py      # 事件构造与落库
├── worker.py              # outbox 投递 + 过期/查单/退款补偿
├── notifier.py            # 项目 webhook HTTP 投递与签名
├── gateways.py            # 按渠道账户惰性构建 pypay 客户端
├── api_merchant.py        # /v1/*
├── api_cashier.py         # /pay/{token}/*
├── api_notify.py          # /notify/{channel}/{account}/*
├── api_admin.py           # /admin/*
├── web_*.py               # 收银台/管理台 HTML
├── client.py              # 业务侧同步/异步客户端 + webhook 验签
└── cli.py                 # migrate/create-project/worker/serve
```

## 快速开始（本地模拟）

```bash
uv venv
uv pip install -e ".[service,dev]"

# 运行服务测试
.venv/bin/python -m pytest tests/service -q

# 启动一个不接触真实渠道的本地演示服务
.venv/bin/python examples/service_demo.py
```

演示脚本会打印：

- 管理后台 `http://127.0.0.1:8099/admin`（`admin / demo-admin-password`）。
- 两个项目的 API 凭证。
- 两个收银台链接和“模拟支付”链接。

演示服务使用内存假渠道和假 notifier，不会发起真实支付，也不会使用真实商户凭据。

## 配置

复制 `.env.example` 为 `.env`，只填自己的值。

### 必填

| 变量 | 说明 |
| --- | --- |
| `PYPAY_SERVICE_DATABASE_PATH` | SQLite 文件路径，默认 `./var/pypay_service.db` |
| `PYPAY_SERVICE_PUBLIC_BASE_URL` | 服务公网根地址，例如 `https://pay.example.com`；不能带路径前缀；微信 Native 必须 HTTPS |
| `PYPAY_SERVICE_MASTER_KEY` | 32 字节密钥，用于加密项目 API/通知密钥；支持 `base64:`、`hex:` 或 64 位 hex |
| `PYPAY_SERVICE_ADMIN_USERNAME` / `PASSWORD` | 管理后台管理员账号 |
| `PYPAY_SERVICE_VIEWER_USERNAME` / `PASSWORD` | 可选，只读账号 |

### 渠道账户

服务支持多个商户账户。每个账户有一个 `env_prefix`，例如：

```json
[
  {"id": "wechat-default", "channel": "wechat", "name": "微信支付", "env_prefix": "PYPAY_WECHAT_"},
  {"id": "alipay-default", "channel": "alipay", "name": "支付宝", "env_prefix": "PYPAY_ALIPAY_"}
]
```

账户凭据仍然只从环境变量或外部密钥文件读取，不存数据库、不进 Git。`WeChatPayConfig` / `AlipayConfig` 会校验 mchid/appid、APIv3 密钥、证书序列号、私钥/公钥格式、公钥 ID 等必填项；缺失时下单返回 502，服务不会把失败当成支付失败。

未设置 `PYPAY_SERVICE_CHANNEL_ACCOUNTS` 时，服务会根据 `PYPAY_WECHAT_MCHID` / `PYPAY_ALIPAY_APPID` 自动生成默认账户。

> 微信回调地址是下单时按订单传入的。服务为每次 attempt 生成
> `https://你的域名/notify/wechat/{account_id}`，并覆盖配置里的默认 notify_url。
> 支付宝同理，并使用 `https://你的域名/pay/{token}/return/alipay` 作为浏览器返回地址。

### 本地开发开关

- `PYPAY_SERVICE_ALLOW_HTTP_URLS=1`：允许项目通知/返回地址使用 http。生产保持 0。
- `PYPAY_SERVICE_ALLOW_PRIVATE_URLS=1`：允许内网/localhost。生产保持 0；通知投递还会在发送时解析 DNS，拒绝私网地址。
- `PYPAY_SERVICE_ENABLE_DOCS=1`：本地开启 `/docs`。生产保持 0。

## 数据库与迁移

```bash
# 应用迁移
python -m pypay_service migrate up

# 回滚最近一步（会删表，先备份）
python -m pypay_service migrate down --steps 1
```

迁移文件在 `src/pypay_service/migrations/`，`schema_migrations` 记录版本。服务启动时默认也会执行 `migrate up`；生产多实例部署建议先单独执行迁移，再滚动启动。

## 运行

```bash
# 启动 HTTP 服务
python -m pypay_service serve --host 0.0.0.0 --port 8080

# 等价于
uvicorn pypay_service.app:create_app --factory --host 0.0.0.0 --port 8080

# 独立 worker（必须单独进程运行）
python -m pypay_service worker
```

worker 负责：

1. 投递 outbox 事件到项目 `notify_url`。
2. 扫描过期订单并发送 `payment.expired`。
3. 主动查单补偿漏回调。
4. 查询 pending 退款。

HTTP 进程不会在请求内发送业务通知；通知可靠性来自数据库 outbox + 独立 worker，而不是进程内临时任务。

## 状态机

订单状态：

```text
pending ──支付成功──► paid ──累计退款=全额──► refunded
   │                    │
   │                    └──部分退款保持 paid，refunded_amount_fen 增加
   ├──全部渠道关闭──► closed
   ├──全部渠道失败──► failed
   └──超过 expires_at──► expired
```

`paid/refunded` 是单调的：迟到的支付回调可以把 `expired/closed/failed` 提升为 `paid`；退款/关单/失败通知不会把已支付订单改回未支付。

退款状态：`pending → succeeded / failed / closed`。超时或网络错误只表示“待确认”，必须用原退款单号查询。

## 通知与投递

三层状态分开记录：

1. **支付状态**：`payment_orders.status` / `payment_attempts.status`。
2. **投递状态**：`outbox_events.status`（`pending/sending/delivered/dead`）。
3. **业务确认状态**：`outbox_events.ack_status`（`pending/acked/rejected`）。

“微信支付成功”不等于“会员/额度已经到账”。业务网站应在自己的事务提交后返回 2xx；pypay 收到 2xx 才标记 `acked`。收银台据此显示“服务开通中”或“服务已确认”。

投递规则：

- 2xx：投递成功，业务确认。
- 408/429/5xx/超时/网络错误：指数退避重试，超过 `OUTBOX_MAX_ATTEMPTS` 后进入 `dead`。
- 其他 4xx：永久拒绝，直接 `dead` 并标记 `ack_status=rejected`。
- 手动重发保持同一个 `event_id`。
- worker 崩溃后，过期的 `sending` 租约会被新 worker 回收。

## 安全

- 业务 API：HMAC-SHA256，签名覆盖 `METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)`；时间戳容差默认 300 秒；nonce 落库防重放。
- 收银台：只接受不可猜测的 token；金额/项目来自数据库快照；不读 URL 里的金额。
- 渠道回调：原始请求体验签/解密；校验商户、appid、订单号、金额、币种、状态；重复/并发/乱序不重复改状态或建事件。
- 业务 webhook：`HMAC-SHA256(notify_secret, timestamp + "." + raw_body)`，请求头带 `X-Pypay-Signature: sha256=...`。
- SSRF：项目通知/返回地址在配置时校验；投递时解析 DNS，拒绝私网/保留地址；不跟随重定向。
- 密钥：项目 API/通知密钥用 master key AES-GCM 加密存储；页面只在创建/轮换时显示一次；日志不记录密钥或完整回调体。
- 管理后台：HTTP Basic + admin/viewer 角色 + 双提交 CSRF + 写操作审计。生产建议放在 HTTPS、VPN 或现有 SSO 之后。
- 响应头：CSP（nonce 脚本）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、HSTS（HTTPS 时）。

## 管理后台

- 项目：创建、停用/启用、通知地址、返回地址白名单、渠道账户、API/通知密钥轮换。
- 渠道账户：添加/启停，显示环境变量前缀；凭据仍从环境读取。
- 订单：按项目/状态/业务单号筛选；订单快照、attempt、退款、渠道原始通知、事件与投递历史。
- 人工操作：主动查单、关闭未支付订单、手动重发事件、权限受控退款。
- 审计：项目/渠道/退款/重发/轮换等写操作。

## 测试

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/service -q
.venv/bin/ruff check src tests examples
.venv/bin/ruff format --check src tests examples
```

测试使用临时 RSA 密钥、模拟网络、假渠道和假 notifier，不访问真实支付网关，不使用真实商户凭据。验收矩阵见 [acceptance.md](acceptance.md)。

## 已知限制

- 首期数据库是 SQLite，适合单实例/低并发。多实例或高写入请迁移到 PostgreSQL（仓储层已集中，迁移点清晰）。
- 管理后台使用内置 Basic Auth，不是完整 SSO/权限系统；生产应接现有身份体系或放在受控网络后。
- 支付宝证书模式尚未接入；首期为支付宝公钥模式。
- 支付宝异步通知里的 `refund_fee` 是累计值且没有 `out_request_no`，本服务不以它记账；退款以 `refund/query_refund` 和退款记录为准。
- 同一订单如果出现第二个渠道也支付成功（异常场景），服务不会重复发 `payment.succeeded`；需要人工对账/退款。
- 真实商户联调项见 [acceptance.md](acceptance.md)。

## 文档索引

- [API 与事件契约](api.md)
- [网站接入指南](integration.md)
- [部署、运维与回滚](deployment.md)
- [旧回调迁移方案](migration.md)
- [验收与未完成联调](acceptance.md)
