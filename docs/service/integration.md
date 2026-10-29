# 网站接入指南

本文面向 `Super-AIGC`、`super-aigc-digitalhuman` 等业务网站的后端开发者，说明如何接入 `pypay_service`。

## 1. 接入前准备

在 pypay 管理后台或 CLI 为每个网站创建一个项目，得到：

| 值 | 用途 | 保存位置 |
| --- | --- | --- |
| `project_id` | 项目标识，pypay 内部使用 | 配置文件 |
| `key_id` | 业务 API 认证标识 | 服务端环境变量/密钥管理 |
| `api_secret` | 业务 API HMAC 密钥 | 服务端密钥管理 |
| `notify_secret` | 业务 webhook 验签密钥 | 服务端密钥管理 |

同时登记：

- `notify_url`：pypay worker 投递事件的公网 HTTPS 地址。
- 返回地址白名单：支付完成后允许跳转的站点页面。
- 可用渠道：例如 `wechat=wechat-default`、`alipay=alipay-default`。

CLI 示例：

```bash
export PYPAY_SERVICE_PUBLIC_BASE_URL=https://pay.example.com
export PYPAY_SERVICE_MASTER_KEY='base64:...'
export PYPAY_SERVICE_DATABASE_PATH=./var/pypay_service.db
export PYPAY_SERVICE_ADMIN_USERNAME=admin
export PYPAY_SERVICE_ADMIN_PASSWORD='...'
export PYPAY_WECHAT_MCHID=...        # 渠道账户凭据仍从环境读取
export PYPAY_ALIPAY_APPID=...

python -m pypay_service create-project \
  --id super-aigc \
  --name "Super-AIGC" \
  --notify-url https://www.super-aigc.example/pypay/notify \
  --return-url https://www.super-aigc.example/pay/success \
  --return-url https://www.super-aigc.example/pay/cancel \
  --channel wechat=wechat-default \
  --channel alipay=alipay-default
```

密钥只在创建/轮换时显示一次。**不要**写进前端代码、浏览器 localStorage、URL、日志或 Git。

## 2. 职责边界

```text
浏览器 ──下单/选套餐──► 业务网站后端
                          │ 1. 计算价格，创建业务订单（状态待支付）
                          │ 2. 服务端调用 pypay 创建支付单
                          │ 3. 把 cashier_url 返回给浏览器
浏览器 ──打开 cashier_url──► pypay 收银台 ──► 微信/支付宝
                          │
pypay worker ──签名事件──► 业务网站 notify_url
                          │ 4. 验签、幂等、事务内发放权益
                          └─ 5. 提交后返回 2xx
```

规则：

- 价格只由业务网站后端计算；pypay 不接受浏览器传入金额。
- 浏览器只需要 `cashier_url`，不需要 `key_id`/`api_secret`/`notify_secret`。
- `return_url` 页面只是交互，不能作为支付成功依据。
- pypay 不直接写业务数据库；权益由业务网站在收到事件后自己发放。

## 3. 下单流程

### 3.1 创建业务订单

在业务库中创建订单，状态为“待支付”，保存 `business_order_no`、用户、商品、金额（分）、套餐/额度信息。

`business_order_no` 建议包含站点前缀和日期，例如：

```text
AIGC-20261029-8f3c2a
DH-20261029-91ab77
```

### 3.2 创建支付单

使用同步客户端：

```python
from pypay_service.client import PayServiceClient, PayServiceError

with PayServiceClient(base_url, key_id, api_secret) as client:
    payment = client.create_payment(
        business_order_no=order.no,
        subject="Super-AIGC 额度包",
        amount_fen=order.amount_fen,
        expires_in_seconds=1800,
        return_url="https://www.super-aigc.example/pay/success",
        metadata={"user_ref": user.id, "plan": order.plan},
    )

order.payment_id = payment["payment_id"]
order.save()
redirect(payment["cashier_url"])
```

异步框架（FastAPI/Starlette）使用 `AsyncPayServiceClient`，接口相同。

重复调用是安全的：

- 同一 `business_order_no`、相同金额/商品 → 返回已有支付单，`created: false`。
- 金额或商品不同 → 409，说明业务侧出现了单号复用，应告警而不是换号重试。

### 3.3 原始 HTTP 示例

```http
POST /v1/payments HTTP/1.1
Host: pay.example.com
X-Pypay-Key-Id: pk_...
X-Pypay-Timestamp: 1793000000
X-Pypay-Nonce: 5f0c2b1e9a7d4c3b
X-Pypay-Signature: 9d2f...
Content-Type: application/json

{"business_order_no":"AIGC-20261029-001","subject":"Super-AIGC 额度包","amount_fen":9900}
```

签名原文：

```text
POST
/v1/payments
1793000000
5f0c2b1e9a7d4c3b
<sha256 of the exact JSON bytes>
```

### 3.4 把用户送到收银台

- 后端返回 `cashier_url`，前端整页跳转。收银台设置了 `X-Frame-Options: DENY` 和 `frame-ancestors 'none'`，不要用 iframe 嵌入。
- 收银台自己展示项目名、商品、金额、倒计时和支付方式。
- 不要自行拼接金额、项目或订单参数到收银台 URL。

## 4. 接收支付结果

### 4.1 以服务端通知为准

`payment.succeeded` 事件到达后：

1. 读取原始 body。
2. 用 `notify_secret` 验证 `X-Pypay-Timestamp` 和 `X-Pypay-Signature`。
3. 校验 `project_id` 是本项目、`amount_fen` 与业务订单一致。
4. 按 `event_id` 去重；以 `business_order_no` 作为权益发放的唯一键。
5. 在同一个数据库事务中：标记业务订单已支付、发放额度/套餐/服务。
6. 事务提交后返回 2xx。

可复制示例见 [`examples/business_receiver.py`](../../examples/business_receiver.py)。核心验签：

```python
from pypay_service.client import WebhookSignatureError, verify_webhook

try:
    event = verify_webhook(notify_secret, request.headers, raw_body)
except WebhookSignatureError:
    return Response(status_code=401)
```

`verify_webhook` 默认允许 300 秒时间偏差，可传 `tolerance_seconds` 调整。

### 4.2 幂等

pypay 会重试，也可能因 worker 恢复而重复投递。业务侧必须：

```sql
INSERT INTO received_events(event_id, ...) VALUES (?, ...);
-- 唯一键冲突 → 已处理，直接返回 2xx
```

或使用 `INSERT ... ON CONFLICT DO NOTHING`。只按“业务订单是否已支付”判断也可以，但保留 `event_id` 便于排查。

权益表对 `business_order_no` 建唯一约束，确保重复事件不会重复加额度。

### 4.3 状态映射

| 事件 | 业务动作 |
| --- | --- |
| `payment.succeeded` | 标记已支付、发放权益 |
| `payment.closed` | 标记已关闭，允许用户重新下单 |
| `payment.expired` | 标记已过期，提示重新下单 |
| `payment.failed` | 标记失败，可提示换支付方式 |
| `refund.succeeded` | 记录退款；按业务规则扣减/回收权益 |
| `refund.failed` | 记录失败，人工跟进 |

“支付成功”与“业务已确认”是两件事。收银台的 `service_ack_status` 在业务返回 2xx 后才变成 `acked`。业务处理失败时不要返回 2xx，让 pypay 重试。

### 4.4 主动查询兜底

通知可能延迟。用户回到业务网站时，可以：

```python
payment = client.get_payment_by_business_order(order.no)
if payment["status"] in {"paid", "refunded"} and not order.paid:
    grant_from_payment(payment)  # 同样按 business_order_no 幂等
```

或调用 `refresh_payment(payment_id)` 让 pypay 主动向渠道查单。不要仅凭浏览器返回页发放权益。

## 5. 退款

```python
refund = client.create_refund(
    payment_id=order.payment_id,
    amount_fen=3000,
    reason="用户申请部分退款",
    idempotency_key=f"refund-{order.no}-1",
)
```

- 支持全额/部分退款；累计不能超过订单金额。
- 使用稳定的 `idempotency_key`；网络超时后重试同一键是安全的。
- 返回 202/`ambiguous: true` 表示结果未知：调用 `client.refresh_refund(refund_id)`，不要新建退款。
- 退款成功会发送 `refund.succeeded` 事件；权益回收由业务侧决定。
- 支付宝退款以 `refund/query_refund` 结果为准，不以异步通知里的累计 `refund_fee` 记账。

## 6. 多站点

每个站点独立项目、独立凭证、独立通知地址：

| 站点 | project_id | notify_url |
| --- | --- | --- |
| Super-AIGC | `super-aigc` | `https://www.super-aigc.example/pypay/notify` |
| 数字人 | `super-aigc-digitalhuman` | `https://digitalhuman.example/pypay/notify` |

- 一个站点的凭证不能查询/退款另一个站点的订单（跨项目返回 404）。
- 事件只会投递到订单所属项目。
- 新增站点只需创建项目，不需要改 pypay 代码。

## 7. 本地联调

不接触真实渠道时，可以：

1. 运行 `python examples/service_demo.py`。
2. 使用演示输出的 `key_id`/`api_secret` 调用 `/v1/payments`。
3. 打开收银台，用演示“模拟支付”链接触发成功。
4. 观察业务通知被假 notifier 记录。

真实渠道联调需要在测试商户环境配置渠道凭据、公网 HTTPS 回调地址，并完成真实支付/退款。本次交付**未进行**这些操作，清单见 [acceptance.md](acceptance.md)。

## 8. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 401 `signature verification failed` | 签名路径带 query、body 被重新序列化、密钥不对 | 用原始字节和 path-only 重新签 |
| 401 `nonce has already been used` | nonce 复用 | 每次请求新 nonce |
| 409 `different amount` | 业务单号复用但金额变了 | 检查业务订单生成逻辑 |
| 400 `return_url is not registered` | 返回地址未登记 | 在项目配置中加入白名单 |
| 409 `project has no enabled payment channel` | 项目未配置/渠道被停用 | 检查项目渠道与账户状态 |
| 收银台一直“服务开通中” | 业务通知未返回 2xx | 查管理后台事件投递记录和业务日志 |
| 跨项目 404 | 用错项目凭证 | 每个站点使用自己的凭证 |

更多接口细节见 [api.md](api.md)，迁移旧回调见 [migration.md](migration.md)。
