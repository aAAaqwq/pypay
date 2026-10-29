# API 与事件契约

本文描述 `pypay_service` 对业务网站暴露的接口、签名方式、状态模型和 webhook 事件。收银台和管理后台是 HTML 页面，不要求业务网站直接调用。

所有金额单位为**人民币分（整数）**，币种首期仅支持 `CNY`。

## 1. 业务 API 认证

业务网站后端持有项目级 `key_id` 和 `api_secret`。浏览器不得持有或使用这两个值。

每个请求带以下请求头：

| 请求头 | 说明 |
| --- | --- |
| `X-Pypay-Key-Id` | 项目 API key id，例如 `pk_...` |
| `X-Pypay-Timestamp` | Unix 秒级时间戳，默认允许 ±300 秒偏差 |
| `X-Pypay-Nonce` | 一次性随机串，8–128 位 `[A-Za-z0-9_-]` |
| `X-Pypay-Signature` | 小写 hex HMAC-SHA256 |

签名原文（`\n` 为真实换行）：

```text
METHOD
PATH
TIMESTAMP
NONCE
SHA256_HEX(BODY)
```

- `METHOD` 大写，例如 `POST`、`GET`。
- `PATH` 只包含路径，例如 `/v1/payments`，**不包含**协议、域名、query string。
- `BODY` 是请求原始字节；GET 或空 body 使用空字节，其 SHA256 为
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
- `SIGNATURE = HMAC-SHA256(api_secret, canonical_string)`，输出小写 hex。

```python
import hashlib, hmac

body_hash = hashlib.sha256(body).hexdigest()
canonical = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}"
signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
```

nonce 按项目落库，重放会被拒绝；服务端会清理超过 `NONCE_TTL_SECONDS` 的 nonce。项目停用后所有 API 调用返回 401。

### 认证错误

认证失败返回 401：

```json
{
  "detail": {
    "code": "authentication_error",
    "message": "signature verification failed"
  }
}
```

并带 `WWW-Authenticate: PypayKey realm="pypay merchant api"`。

## 2. 通用错误格式

业务逻辑错误：

```json
{
  "error": {
    "code": "conflict",
    "message": "business_order_no already exists with a different amount"
  }
}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `validation_error` | 参数不合法（金额、币种、URL 未登记等） |
| 401 | `authentication_error` | 认证失败/项目停用 |
| 404 | `not_found` | 订单或退款不存在，或不属于当前项目 |
| 409 | `conflict` | 业务单号冲突、状态不允许、退款超额、无可用渠道 |
| 502 | `provider_error` | 渠道配置缺失或渠道拒绝 |
| 502 | `provider_network_error` | 渠道网络异常，结果未知，先查原单再重试 |
| 500 | `internal_error` | 未预期错误（不含敏感细节） |

Pydantic 请求体校验失败返回 FastAPI 默认 422 `{"detail": [...]}`。

`provider_network_error` 额外带 `may_have_succeeded`：

```json
{
  "error": {
    "code": "provider_network_error",
    "message": "payment state is unknown; query the original order before retrying",
    "may_have_succeeded": true
  }
}
```

## 3. 创建支付

`POST /v1/payments`

请求：

```json
{
  "business_order_no": "AIGC-20261029-001",
  "subject": "Super-AIGC 额度包",
  "amount_fen": 9900,
  "currency": "CNY",
  "expires_in_seconds": 1800,
  "return_url": "https://www.super-aigc.example/pay/success",
  "metadata": {"user_ref": "u_123", "plan": "pro"}
}
```

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `business_order_no` | 是 | 1–64 位 `[0-9A-Za-z_.:@-]`，项目内唯一 |
| `subject` | 是 | 1–127 字符，不允许首尾空白 |
| `amount_fen` | 是 | 正整数（JSON 整数，布尔值会被拒绝） |
| `currency` | 否 | 默认 `CNY`，其他值拒绝 |
| `expires_in_seconds` | 否 | 60–86400，默认 1800 |
| `return_url` | 否 | 必须已登记在项目返回地址白名单 |
| `metadata` | 否 | JSON 对象，≤4 KB，原样进入事件 |

成功：首次创建 201，重复请求 200，body 均为：

```json
{
  "payment_id": "pay_...",
  "business_order_no": "AIGC-20261029-001",
  "project_id": "super-aigc",
  "subject": "Super-AIGC 额度包",
  "amount_fen": 9900,
  "currency": "CNY",
  "status": "pending",
  "service_ack_status": null,
  "refunded_amount_fen": 0,
  "cashier_url": "https://pay.example.com/pay/...",
  "expires_at": "2026-10-29T12:30:00.123456+00:00",
  "created_at": "2026-10-29T12:00:00.123456+00:00",
  "paid_at": null,
  "closed_at": null,
  "return_url": "https://www.super-aigc.example/pay/success",
  "attempts": [],
  "refunds": [],
  "events": [],
  "created": true
}
```

幂等规则（同一项目 + `business_order_no`）：

- 金额、币种、`subject`、`metadata` 或（请求带 `return_url` 时）返回地址不一致 → 409。
- 完全一致 → 返回已有订单，`created: false`，不新建订单、不重复收银台。
- 状态已经是 `closed/expired` 时重复创建仍返回已有订单；需要重新支付请使用新的业务单号。

`service_ack_status` 是 `payment.succeeded` 事件的业务确认状态：`null`（还没有事件）、`pending`、`acked`、`rejected`。收银台用它区分“已支付”和“业务已确认”。

## 4. 查询与补偿

| 方法与路径 | 说明 |
| --- | --- |
| `GET /v1/payments/{payment_id}` | 查询订单快照、attempt、退款、事件 |
| `GET /v1/payments/by-business-order/{business_order_no}` | 按业务单号查询当前项目订单 |
| `POST /v1/payments/{payment_id}/refresh` | 主动向渠道查单，应用结果后返回订单 |
| `POST /v1/payments/{payment_id}/close` | 关闭未支付订单（会尝试关闭渠道订单） |

所有查询都按认证项目过滤，跨项目返回 404。

`refresh` 在渠道查询失败时仍返回当前订单，并附加：

```json
{"refresh_errors": ["wechat: PaymentNetworkError"]}
```

`close` 遇到渠道错误时返回 202 + `close_errors`，订单可能仍是 `pending`。已支付订单不能通过 close 改状态。

## 5. 退款

`POST /v1/refunds`

```json
{
  "payment_id": "pay_...",
  "amount_fen": 3000,
  "reason": "部分退款",
  "idempotency_key": "refund-20261029-001"
}
```

`idempotency_key` 也可放在 `Idempotency-Key` 请求头；两处同时提供且不一致返回 400。字符范围 `[A-Za-z0-9_.:-]{1,64}`。

响应状态：

| HTTP | 条件 | body |
| --- | --- | --- |
| 201 | 新建退款 | `created: true`，`ambiguous: false` |
| 200 | 幂等重放 | 原退款，`created: false`；带 `X-Idempotent-Replay: true` |
| 202 | 渠道超时/网络错误，结果未知 | `ambiguous: true` |
| 400 | 参数非法 | `validation_error` |
| 404 | 订单不存在或不属于当前项目 | `not_found` |
| 409 | 未支付、超额、幂等键冲突 | `conflict` |
| 502 | 渠道配置缺失或明确拒绝 | `provider_error` |

退款响应：

```json
{
  "refund_id": "ref_...",
  "payment_id": "pay_...",
  "out_refund_no": "RF...",
  "amount_fen": 3000,
  "total_amount_fen": 9900,
  "reason": "部分退款",
  "status": "pending",
  "provider_refund_id": null,
  "created_at": "2026-10-29T12:10:00.000000+00:00",
  "succeeded_at": null,
  "created": true,
  "ambiguous": false
}
```

可退金额 = 订单金额 − max(已成功退款累计, 处理中退款累计)。处理中退款会先占用额度，避免并发超额。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /v1/refunds/{refund_id}` | 查询退款 |
| `POST /v1/refunds/{refund_id}/refresh` | 向渠道查询退款，应用结果后返回 |

202/超时只表示“待确认”，必须调用 `refresh` 或等待 worker 补偿，不能据此重复发起退款。重复使用同一 `idempotency_key` 是安全的。

## 6. 收银台路由（浏览器）

这些路由不要求业务 API 签名，只使用不可猜测的收银台 token。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/pay/{token}` | 收银台页面；可选 `?attempt={id}` 指定展示的 attempt |
| POST | `/pay/{token}/start` | 表单提交渠道选择；校验 CSRF 后 303 跳回 |
| GET | `/pay/{token}/status` | JSON 状态，供页面轮询 |
| GET | `/pay/{token}/qr.svg?attempt={id}` | 微信二维码 SVG |
| GET | `/pay/{token}/alipay-form?attempt={id}` | 支付宝电脑网站支付表单 |
| GET | `/pay/{token}/return/alipay` | 支付宝同步返回落地页，仅用于交互 |

`/pay/{token}/status` 示例：

```json
{
  "status": "paid",
  "service_ack_status": "acked",
  "refunded_amount_fen": 0,
  "expires_at": "2026-10-29T12:30:00.000000+00:00",
  "attempts": [
    {"channel": "wechat", "status": "paid", "channel_status": "SUCCESS"}
  ]
}
```

同步返回页不构成支付成功证据；业务网站必须以服务端通知或主动查询为准。

## 7. 渠道回调

`POST /notify/{channel}/{account_id}`，由微信/支付宝直接调用，业务网站不调用。

- 原始 body 验签/解密，校验商户号、appid、订单号、金额、币种、交易状态。
- 重复、并发、乱序通知不会重复改变状态或重复创建事件。
- 处理成功按渠道约定响应：
  - 微信：`{"code":"SUCCESS","message":"成功"}`
  - 支付宝：纯文本 `success`
- 验签失败、金额/订单不匹配等返回该渠道的失败响应，促使渠道重试或暴露问题。
- 微信失败响应为 `{"code":"FAIL","message":"..."}`；支付宝为纯文本 `failure`。

## 8. 业务 webhook 事件

worker 向项目 `notify_url` 发送 `POST application/json`，请求头：

| 请求头 | 说明 |
| --- | --- |
| `X-Pypay-Event-Id` | 稳定事件 ID，重试不变 |
| `X-Pypay-Event-Type` | 事件类型 |
| `X-Pypay-Timestamp` | Unix 秒 |
| `X-Pypay-Signature` | `sha256=` + HMAC-SHA256(notify_secret, timestamp + "." + raw_body) |
| `User-Agent` | `pypay-service/0.2` |

签名必须基于**原始 body 字节**计算，不能先解析再序列化。默认时间戳容差 300 秒。

### 事件类型

| 类型 | 触发 |
| --- | --- |
| `payment.succeeded` | 订单首次支付成功（每订单最多一次） |
| `payment.closed` | 订单关闭 |
| `payment.expired` | 订单过期 |
| `payment.failed` | 所有渠道尝试失败 |
| `refund.succeeded` | 退款成功 |
| `refund.failed` | 退款明确失败 |

### 支付事件 payload

```json
{
  "event_id": "evt_...",
  "event_type": "payment.succeeded",
  "project_id": "super-aigc",
  "payment_id": "pay_...",
  "business_order_no": "AIGC-20261029-001",
  "subject": "Super-AIGC 额度包",
  "amount_fen": 9900,
  "currency": "CNY",
  "status": "paid",
  "refunded_amount_fen": 0,
  "metadata": {"user_ref": "u_123", "plan": "pro"},
  "occurred_at": "2026-10-29T12:05:00.000000+00:00",
  "channel": "wechat",
  "payment_order_no": "PP...",
  "transaction_id": "420000..."
}
```

`status` 是事件发生时的订单状态。`payment.closed/expired/failed` 的 `channel` 等字段可能为 `null`。

### 退款事件 payload

在支付事件字段基础上增加：

```json
{
  "event_type": "refund.succeeded",
  "channel": "wechat",
  "payment_order_no": "PP...",
  "transaction_id": "420000...",
  "refund_id": "ref_...",
  "out_refund_no": "RF...",
  "refund_amount_fen": 3000,
  "refunded_total_fen": 3000,
  "reason": "部分退款"
}
```

`refunded_total_fen` 是订单累计已退款金额。

### 业务侧处理要求

1. 读取**原始请求体**再验签，不能使用框架自动解析后的对象。
2. 校验 `X-Pypay-Timestamp` 与签名；失败返回 401，不处理业务。
3. 按 `event_id` 幂等；建议同时以 `business_order_no` 作为权益唯一键。
4. 在自己的数据库事务中完成权益发放/退款记录，**提交成功后**再返回 2xx。
5. 返回 2xx 的 body 不参与状态判断；4xx 视为永久拒绝，5xx/超时触发重试，只有 2xx 才算业务确认。

事件可能因重试而乱序到达（例如 `refund.succeeded` 早于 `payment.succeeded`，或 `payment.expired` 之后又收到迟到的 `payment.succeeded`）。业务侧应按事件类型推进状态，不要把“未收到成功事件”当成“不会支付”，也不要因为顺序异常丢弃事件。

### 重试与确认

- 2xx：`delivered` + `acked`。
- 408/429/5xx/超时/网络错误：`pending`，指数退避重试，达到上限后 `dead`、`ack_status=pending`。
- 其他 4xx：`dead`、`ack_status=rejected`，不自动重试。
- 3xx：不跟随重定向，按可重试失败处理，最终可能进入 `dead`（`ack_status=pending`）。
- 管理后台可手动重发，保持同一 `event_id`。

## 9. 健康检查

| 路径 | 用途 |
| --- | --- |
| `GET /health` | 进程与数据库状态、已配置渠道 |
| `GET /ready` | 数据库可读写则返回 ready |

健康检查不返回密钥。

## 10. 状态枚举

订单：`pending`、`paid`、`closed`、`expired`、`refunded`、`failed`。

attempt：`pending`、`paid`、`closed`、`failed`、`refunded`。

退款：`pending`、`succeeded`、`failed`、`closed`。

投递：`pending`、`sending`、`delivered`、`dead`。

业务确认：`pending`、`acked`、`rejected`。
