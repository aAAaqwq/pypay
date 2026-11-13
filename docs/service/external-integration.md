# pypay 支付中台 · 外部站点接入说明

本文面向外部站点（token 中转站、充值站等）的后端开发者，说明如何把站内充值/购买接到 pypay 支付中台。

- 中台 API 地址：`https://pypay.kunjieai.com`
- 协议与本文同源；如需更细的字段说明，可一并索取 `api.md`。
- 所有金额单位为**人民币分（整数）**，币种仅支持 `CNY`。

> **对接交接清单（SOP）**
>
> 中台交付给对方：`API 地址`、`key_id`、`api_secret`、`notify_secret`。两个密钥只在创建或轮换时展示一次，必须走安全渠道，不进仓库、前端、日志。
>
> 对方必须提供：**固定公网出口 IP**（写入 `/v1` 白名单；经 NAT 或代理时给出真实出口地址）和 **`notify_url`**（公网 HTTPS、证书有效、能验签并在事务提交后返回 2xx）。返回地址同样需要逐字登记，见第 2 节。
>
> 以上任一项未就绪时，不要创建真实订单或开启真实支付。

## 1. 职责边界

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| 你的站点后端 | 计算价格、创建业务订单、调用中台下单、接收通知、发放额度/权益 | 不持有商户密钥，不解析微信/支付宝原始回调 |
| pypay 中台 | 支付订单、拉起渠道、收银台、回调验签、退款、通知投递 | 不保存你的用户/额度，不写你的数据库 |
| 浏览器 | 打开收银台付款 | 不决定金额，不修改订单 |

两条硬规则：

1. **金额只能由你的服务端决定**，浏览器传金额的请求会被拒绝。
2. **同步返回页（return_url）不是支付成功证据**，一切以服务端通知或主动查单为准。

## 2. 接入前需要你提供

| 项 | 说明 |
| --- | --- |
| 站点名称与用途 | 用于创建项目、收银台展示 |
| 服务器出口 IP | **公网固定 IP**，会加入 API 访问白名单；不在白名单内调用 `/v1` 会得到 nginx 403 |
| `notify_url` | 你是公网 HTTPS 地址、证书有效，收到事件后返回 2xx 的接收端点 |
| 返回地址 | 支付完成后允许跳回的页面，**逐字登记**（含协议、域名、路径；精确匹配，不做前缀匹配） |
| 是否使用退款 | 默认不开放，需要时单独申请 |

## 3. 你会收到

| 项 | 说明 |
| --- | --- |
| `API 地址` | `https://pypay.kunjieai.com`（只有该域名代理 `/v1`，且带 IP 白名单） |
| `key_id` | 形如 `pk_...`，放在请求头 |
| `api_secret` | 业务 API 签名密钥，**仅服务端持有** |
| `notify_secret` | 通知验签密钥，**仅服务端持有** |
| 项目 ID | 用于对账与工单沟通 |

密钥只在创建或轮换时展示一次。不要写进前端代码、浏览器存储、URL、日志或代码仓库。

## 4. 下单流程

```
浏览器 ──选套餐──► 你的后端
                    1. 创建业务订单（待支付，保存 business_order_no）
                    2. 服务端 POST /v1/payments（HMAC 签名）
                    3. 302 到返回的 cashier_url
浏览器 ──打开 cashier_url──► pypay 收银台 ──► 微信扫码 / 支付宝
pypay worker ──签名事件──► 你的 notify_url
                    4. 验签、按 event_id 幂等、事务内发放额度
                    5. 事务提交后再返回 2xx
```

### 4.1 `POST /v1/payments`

请求体：

```json
{
  "business_order_no": "TUC-20261110-8f3c2a",
  "subject": "Token 充值 100 万",
  "amount_fen": 9900,
  "currency": "CNY",
  "expires_in_seconds": 1800,
  "return_url": "https://your-site.example/topup",
  "metadata": {"user_ref": "u_123", "plan": "pro"}
}
```

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `business_order_no` | 是 | 1–64 位 `[0-9A-Za-z_.:@-]`，项目内唯一 |
| `subject` | 是 | 1–127 字符，会显示在收银台 |
| `amount_fen` | 是 | 正整数（分） |
| `currency` | 否 | 默认 `CNY`，其他值拒绝 |
| `expires_in_seconds` | 否 | 60–86400，默认 1800 |
| `return_url` | 否 | 必须已在白名单登记 |
| `metadata` | 否 | JSON 对象，≤4 KB，原样进入通知事件 |

成功响应（首次 201，重复请求 200）：

```json
{
  "payment_id": "pay_...",
  "business_order_no": "TUC-20261110-8f3c2a",
  "project_id": "your-project",
  "amount_fen": 9900,
  "currency": "CNY",
  "status": "pending",
  "service_ack_status": null,
  "cashier_url": "https://<中台配置的收银台域名>/pay/<token>",
  "expires_at": "2026-11-10T12:30:00.123456+00:00",
  "return_url": "https://your-site.example/topup",
  "created": true
}
```

- **`cashier_url` 由中台生成，不要自己拼接**，也不要写死域名；直接 302 给浏览器。
- 收银台页面不允许被 iframe 嵌入（`X-Frame-Options: DENY`），必须顶层跳转。
- 渠道能力：微信 Native 扫码、支付宝电脑网站支付。用户进入收银台后自行选择渠道。

### 4.2 幂等

同一项目 + 同一 `business_order_no`：

- 金额、币种、`subject`、`metadata`、`return_url` 完全一致 → 返回已有订单，`created: false`，不重复收银台。
- 任一不一致 → `409 conflict`。这说明业务侧单号复用，应告警排查，**不要换单号重试**。
- 已 `closed/expired` 的订单重复创建仍返回原订单；需要重新支付请用新的业务单号。

### 4.3 请求签名（所有 `/v1/*`）

请求头：

| 请求头 | 说明 |
| --- | --- |
| `X-Pypay-Key-Id` | 项目 key id |
| `X-Pypay-Timestamp` | Unix 秒，允许 ±300 秒偏差 |
| `X-Pypay-Nonce` | 8–128 位 `[A-Za-z0-9_-]`，一次性随机串 |
| `X-Pypay-Signature` | 小写 hex 的 HMAC-SHA256 |

签名原文（`\n` 为真实换行）：

```text
METHOD
PATH
TIMESTAMP
NONCE
SHA256_HEX(BODY)
```

- `PATH` 只含路径（如 `/v1/payments`），不含协议、域名、query string。
- `BODY` 是**实际发送的原始字节**；空 body 的 SHA256 为
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
- 签名 = `HMAC-SHA256(api_secret, 上述原文)`。

Python 示例（标准库 + `requests`）：

```python
import hashlib, hmac, json, secrets, time
import requests

API = "https://pypay.kunjieai.com"
KEY_ID = "pk_xxx"
API_SECRET = "***"

def signed_headers(method: str, path: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{body_hash}"
    signature = hmac.new(API_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Pypay-Key-Id": KEY_ID,
        "X-Pypay-Timestamp": ts,
        "X-Pypay-Nonce": nonce,
        "X-Pypay-Signature": signature,
        "Content-Type": "application/json",
    }

def create_payment(order) -> dict:
    path = "/v1/payments"
    body = json.dumps({
        "business_order_no": order.no,
        "subject": order.subject,
        "amount_fen": order.amount_fen,
        "return_url": order.return_url,
        "metadata": {"user_ref": order.user_ref},
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    resp = requests.post(API + path, data=body,
                         headers=signed_headers("POST", path, body), timeout=15)
    resp.raise_for_status()
    return resp.json()
```

其他语言按同一原文实现即可；注意 JSON 序列化后的字节必须与签名时一致。

## 5. 支付结果通知（服务端 webhook）

中台 worker 向你的 `notify_url` 发送 `POST application/json`：

| 请求头 | 说明 |
| --- | --- |
| `X-Pypay-Event-Id` | 稳定事件 ID，重试不变 |
| `X-Pypay-Event-Type` | 事件类型 |
| `X-Pypay-Timestamp` | Unix 秒 |
| `X-Pypay-Signature` | `sha256=` + `HMAC-SHA256(notify_secret, timestamp + "." + 原始body)` |

验签必须基于**原始 body 字节**，不能先解析再序列化。

```python
import hashlib, hmac, json, time

def verify_webhook(headers, raw_body: bytes, notify_secret: str) -> dict:
    ts = headers["X-Pypay-Timestamp"]
    sig = headers["X-Pypay-Signature"]
    sig = sig[len("sha256="):] if sig.startswith("sha256=") else sig
    if abs(time.time() - int(ts)) > 300:
        raise ValueError("stale timestamp")
    expected = hmac.new(notify_secret.encode(), f"{ts}.".encode() + raw_body,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("bad signature")
    return json.loads(raw_body)
```

### 5.1 事件类型

| 类型 | 触发 |
| --- | --- |
| `payment.succeeded` | 订单首次支付成功（每单最多一次） |
| `payment.closed` | 订单关闭 |
| `payment.expired` | 订单过期 |
| `payment.failed` | 所有渠道尝试失败 |
| `refund.succeeded` / `refund.failed` | 退款结果（如已开通） |

`payment.succeeded` 示例：

```json
{
  "event_id": "evt_...",
  "event_type": "payment.succeeded",
  "project_id": "your-project",
  "payment_id": "pay_...",
  "business_order_no": "TUC-20261110-8f3c2a",
  "subject": "Token 充值 100 万",
  "amount_fen": 9900,
  "currency": "CNY",
  "status": "paid",
  "refunded_amount_fen": 0,
  "metadata": {"user_ref": "u_123"},
  "occurred_at": "2026-11-10T12:05:00.000000+00:00",
  "channel": "wechat",
  "payment_order_no": "PP...",
  "transaction_id": "420000..."
}
```

### 5.2 处理要求

1. 先验签，失败返回 401，不处理业务。
2. 按 `event_id` 幂等；权益发放建议再以 `business_order_no` 为唯一键兜底。
3. 在自己的数据库事务内完成发放，**提交成功后**再返回 2xx。
4. 返回 2xx 的 body 不参与判断；4xx 视为永久拒绝；5xx/超时/3xx 会按指数退避重试（默认最多 8 次，单次超时 10 秒），耗尽后标记为 `dead`，可由中台后台手动重发（同一 `event_id`）。
5. 事件可能乱序到达。按事件类型推进状态，不要因为"还没收到成功事件"就认定不会支付。

## 6. 查单与关单

| 方法与路径 | 说明 |
| --- | --- |
| `GET /v1/payments/{payment_id}` | 查询订单快照、渠道尝试、事件 |
| `GET /v1/payments/by-business-order/{business_order_no}` | 按业务单号查（推荐恢复场景） |
| `POST /v1/payments/{payment_id}/refresh` | 主动向渠道查单并应用结果 |
| `POST /v1/payments/{payment_id}/close` | 关闭未支付订单 |

- 查单/关单只能看到本项目订单，跨项目返回 404。
- `refresh` 渠道失败时仍返回订单，并带 `refresh_errors`。
- `close` 遇到渠道错误返回 202 + `close_errors`，订单可能仍是 `pending`，需再查。
- 已支付订单不能通过 `close` 改状态。

## 7. 退款（如已开通）

`POST /v1/refunds`

```json
{"payment_id": "pay_...", "amount_fen": 3000, "reason": "部分退款", "idempotency_key": "refund-001"}
```

- `idempotency_key` 也可放 `Idempotency-Key` 请求头，两处不一致返回 400。
- `201` 新建；`200` 幂等重放；`202` 渠道超时、结果未知（必须 `refresh` 确认，不能直接重发）。
- 可退金额 = 订单金额 − 已成功退款 − 处理中退款（处理中会先占用额度）。

## 8. 错误码

| HTTP | code | 含义 | 处理建议 |
| --- | --- | --- | --- |
| 400 | `validation_error` | 参数非法（金额、`return_url` 未登记等） | 修参数，不要原样重试 |
| 401 | `authentication_error` | 签名/时间戳/nonce/项目停用 | 检查时钟与密钥 |
| 404 | `not_found` | 订单不存在或不属于本项目 | 检查 `payment_id`/项目 |
| 409 | `conflict` | 单号冲突、状态不允许、退款超额、无可用渠道 | 告警排查，不要换号重试 |
| 502 | `provider_error` | 渠道配置缺失或明确拒绝 | 联系中台 |
| 502 | `provider_network_error` | 渠道结果未知（带 `may_have_succeeded`） | **先查原单**，不要新建 |
| 500 | `internal_error` | 未预期错误 | 联系中台并提供时间与单号 |

边缘层返回的 **纯 HTML** `403`/`404`（`Server: nginx`）表示请求没到达应用：通常是对端 IP 不在白名单，或用了错误的域名（API 必须走 `https://pypay.kunjieai.com`）。

## 9. 联调步骤

1. 你方提供：站点名称、服务器公网出口 IP、`notify_url`、返回地址列表。
2. 中台创建项目、登记白名单与返回地址，安全渠道发送 `key_id`/`api_secret`/`notify_secret`。
3. 用 **1 分钱**测试单走通全链路：下单 → 打开 `cashier_url` → 支付 → 收到并验签 `payment.succeeded` → 只在事务内加一次额度 → 重复投递不重复加。
4. 验证异常路径：重复下单幂等、金额改大被 409、过期后不能支付、浏览器刷新/返回页面不触发发放。
5. 通过后再放真实金额；上线后保留查单补偿任务（定时 `by-business-order` 查询待支付订单）。

## 10. 安全要求

- `api_secret` / `notify_secret` 只放服务端环境变量或密钥管理，不进前端、不进 URL、不进日志。
- 不要依赖 `return_url` 判断支付成功；不要信任浏览器传来的金额、订单状态。
- `notify_url` 必须校验签名与时间戳，并做 `event_id` 幂等。
- 如密钥可能泄露，立即联系中台轮换；轮换后旧密钥即刻失效。

## 11. 需要中台配合的事项

- 新增/变更服务器出口 IP、`notify_url`、返回地址，均需中台登记后生效。
- 通知投递失败进入 `dead` 后，可在中台后台手动重发。
- 对账或疑难单，提供 `business_order_no` 与时间，中台可导出订单、事件与投递记录。

## 附录 A：如果贵站只支持"易支付（epay）网关地址"

部分中转站系统（new-api / one-api 及其分支）的在线充值只接受一组 epay 配置：网关地址、PID、商户密钥、通知地址、返回地址，并由站点拼出 `{网关}/submit.php` 之类的请求。

**pypay 中台不实现易支付协议**，没有 `submit.php`、没有 MD5 签名、没有 `pid/key` 商户模型。因此不能只填一条网关地址接入。可行方式二选一：

1. **贵站侧加一层适配**（推荐）：新建一个很小的服务，对上是 epay 协议（给 new-api 填网关地址），对下调用本文第 4 节的 `/v1/payments` 下单、第 5 节接收签名事件，再转换成 new-api 的 epay 通知与返回地址。本站已有的 `newapi` 项目就是这种形态，通知地址形如 `https://<贵站域名>/epay/notify/pypay`。
2. **统一网关**：由中台侧提供一个 epay 兼容入口，多个中转站共用。此方案需要单独评估与排期，接入前请先确认。

无论哪种方式，到账依据始终是中台发给你方服务端的签名事件；epay 的同步返回只用于跳转。
