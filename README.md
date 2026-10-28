# pypay

面向 Python 后端的统一支付模块，首期支持：

- **微信支付 APIv3 Native 扫码支付**，使用**微信支付公钥模式**（不下载平台证书）。
- **支付宝电脑网站支付**（`alipay.trade.page.pay`，产品码 `FAST_INSTANT_TRADE_PAY`）。
- 网站单次购买、充值、手动续费场景。

核心模块不依赖具体网站、用户表、会员或额度业务，也不依赖 FastAPI 或数据库。支付 SDK 的同步调用在异步客户端中通过 `asyncio.to_thread()` 放到工作线程，避免阻塞事件循环。

## 目录

```text
src/pypay/
├── client.py          # PaymentClient / AsyncPaymentClient
├── config.py          # 配置校验与环境变量读取
├── models.py          # 统一请求、结果、通知模型
├── money.py           # 分与元转换
├── enums.py           # 渠道 / 支付状态 / 退款状态 / 事件
├── errors.py          # 统一异常
└── channels/
    ├── wechat.py      # 微信 APIv3 Native（公钥模式）
    └── alipay.py      # 支付宝电脑网站支付
examples/
├── fastapi_app.py     # 下单、回调、查单补偿、前端验证台
├── demo_gateway.py    # 无商户凭据时的内存演示网关
├── static/index.html  # 无构建步骤的本地验证页面
├── db.py              # SQLite + 唯一约束 + 事务幂等
├── reconcile.py       # 扫描待支付订单并主动查单
└── minimal.py         # 最小同步调用
tests/                 # 使用临时测试密钥与模拟网络的测试
```

## 安装

```bash
# 推荐使用 uv
uv venv
uv pip install -e ".[examples,dev]"

# 或 pip
python -m venv .venv
.venv/bin/pip install -e ".[examples,dev]"
```

运行要求：Python 3.10+。

依赖的支付 SDK：

- 微信：[`wechatpayv3`](https://github.com/minibear2021/wechatpayv3)（同步，支持微信支付公钥模式）
- 支付宝：[`alipay-sdk-python`](https://github.com/alipay/alipay-sdk-python-all)（官方 SDK，同步）

## 配置

复制 `.env.example` 为 `.env`，只填自己的值。**仓库、日志、示例中不要出现真实凭据。**

```bash
cp .env.example .env
```

密钥优先使用文件：

- 微信：`PYPAY_WECHAT_PRIVATE_KEY_PATH`、`PYPAY_WECHAT_PUBLIC_KEY_PATH`
- 支付宝：`PYPAY_ALIPAY_PRIVATE_KEY_PATH`、`PYPAY_ALIPAY_PUBLIC_KEY_PATH`

也支持通过 `*_KEY` 环境变量传完整 PEM。密钥可以是 PKCS#1、PKCS#8，或支付宝工具导出的无头 base64；配置会统一规范化为 PKCS#8 私钥和 SPKI 公钥，避免 SDK 因 PEM 头不匹配而验签失败。同一组密钥的“文件路径”和“内联 PEM”只能设置一个，配置校验会明确报错。

### 微信必填项

| 变量 | 说明 |
| --- | --- |
| `PYPAY_WECHAT_MCHID` | 直连商户号 |
| `PYPAY_WECHAT_APPID` | 已与商户号绑定的 appid，Native 下单必填 |
| `PYPAY_WECHAT_APIV3_KEY` | APIv3 密钥，必须为 32 字节 |
| `PYPAY_WECHAT_CERT_SERIAL_NO` | 商户 API 证书序列号 |
| `PYPAY_WECHAT_PRIVATE_KEY(_PATH)` | 商户 API 私钥 PEM |
| `PYPAY_WECHAT_PUBLIC_KEY(_PATH)` | 微信支付公钥 PEM |
| `PYPAY_WECHAT_PUBLIC_KEY_ID` | 微信支付公钥 ID，例如 `PUB_KEY_ID_...` |
| `PYPAY_WECHAT_NOTIFY_URL` | 回调地址，必须是 HTTPS |

### 支付宝必填项

| 变量 | 说明 |
| --- | --- |
| `PYPAY_ALIPAY_APPID` | 支付宝应用 appid |
| `PYPAY_ALIPAY_PRIVATE_KEY(_PATH)` | 应用私钥，PKCS#1 或 PKCS#8 均可 |
| `PYPAY_ALIPAY_PUBLIC_KEY(_PATH)` | 支付宝公钥，支持 PKCS#1/SPKI 或无头 base64 |
| `PYPAY_ALIPAY_NOTIFY_URL` | 异步通知地址 |
| `PYPAY_ALIPAY_RETURN_URL` | 浏览器同步跳转地址，不作为支付依据 |
| `PYPAY_ALIPAY_GATEWAY` | 可选，默认生产网关；必须是 HTTPS；沙箱请改为沙箱网关 |
| `PYPAY_ALIPAY_SELLER_ID` | 可选；设置后会校验通知中的 `seller_id` |

注意：

- 官方支付宝 SDK 的 RSA2 签名实现只接受 **PKCS#1** 私钥，验签只接受 **SPKI** 公钥。配置层会先规范化密钥格式，适配层再把私钥转为 PKCS#1；不会重写签名算法。
- 首期只实现**支付宝公钥模式**。证书模式（`app_cert_sn` / `alipay_root_cert_sn`）尚未接入，接入前需要扩展适配层并完成真实联调。
- 支付宝扫码支付（`alipay.trade.precreate`）不在首期范围。若后续接入，需要开通**当面付**产品权限。
- 微信 Native 使用微信支付公钥模式；配置中必须同时提供公钥和公钥 ID，模块不会调用 `/v3/certificates` 下载平台证书。

### 密钥安全

- 密钥只放在部署机的只读文件或 Secret Manager，不要写进镜像、仓库、日志或前端。
- 本机文件权限：密钥目录 `700`，私钥、APIv3 密钥和证书 `600`；不要放在同步盘或公开目录。
- 测试与生产使用不同商户号/appid 和不同密钥；演示模式不要配置真实密钥。
- APIv3 密钥、商户证书私钥、支付宝应用私钥都要有轮换预案；怀疑泄露时先轮换再排查。
- 服务日志只记录渠道错误码和订单号，不记录签名原文、私钥、APIv3 密钥或完整回调体。
- 生产环境通过 KMS/Vault/云 Secret Manager 注入；容器只挂载需要的文件。
- 不要把 `.env`、`*.pem`、`*.p12` 放进 Web 根目录或可下载路径。

## 快速开始

```python
from pypay import CreatePaymentRequest, PaymentClient

client = PaymentClient.from_env()  # 读取 PYPAY_CHANNEL
result = client.create_payment(
    CreatePaymentRequest(
        out_trade_no="ORDER-20261027-001",
        amount_fen=1,  # 单位：分
        description="单次购买",
        expires_at=None,  # 可选，必须带时区
    )
)

if result.qr_code_url:
    print("微信二维码链接:", result.qr_code_url)
if result.form_html:
    print("支付宝收银台表单:", result.form_html)
```

异步应用使用 `AsyncPaymentClient`，实际 SDK 调用在工作线程执行：

```python
from pypay import AsyncPaymentClient, CreatePaymentRequest

client = AsyncPaymentClient.from_env()
result = await client.create_payment(
    CreatePaymentRequest(out_trade_no="ORDER-001", amount_fen=100, description="充值")
)
```

也可以显式绑定渠道和配置：

```python
from pypay import Channel, PaymentClient, WeChatPayConfig

client = PaymentClient(Channel.WECHAT, WeChatPayConfig.from_env())
```

## 统一接口

所有金额参数和返回金额都是**人民币分的整数**。与渠道的元字符串转换由适配层负责。

```python
class PaymentClient:
    channel: Channel

    def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult: ...
    def query_payment(self, out_trade_no: str) -> QueryPaymentResult: ...
    def close_payment(self, out_trade_no: str) -> ClosePaymentResult: ...
    def refund(self, request: RefundRequest) -> RefundResult: ...
    def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult: ...
    def parse_notification(
        self,
        headers: Mapping[str, str],
        body: bytes | str,
        *,
        expected_out_trade_no: str | None = None,
        expected_amount_fen: int | None = None,
        expected_currency: str = "CNY",
    ) -> PaymentNotification: ...
```

`AsyncPaymentClient` 暴露同名协程方法。

`query_refund` 的 `out_trade_no` 对微信可选（查询响应里会返回并校验），对支付宝必填，因为支付宝退款查询接口同时需要商户订单号和退款单号。

### 创建支付返回

| 字段 | 说明 |
| --- | --- |
| `qr_code_url` | 微信 Native 二维码内容，交给前端生成二维码 |
| `form_html` | 支付宝自动提交的收银台表单 |
| `redirect_url` | 预留字段；当前适配器默认返回表单 |
| `status` | 新订单为 `pending` |

### 支付状态

| 统一状态 | 微信 `trade_state` | 支付宝 `trade_status` |
| --- | --- | --- |
| `pending` | `NOTPAY`、`USERPAYING` | `WAIT_BUYER_PAY` |
| `paid` | `SUCCESS` | `TRADE_SUCCESS`、`TRADE_FINISHED` |
| `closed` | `CLOSED`、`REVOKED` | `TRADE_CLOSED` |
| `refunded` | `REFUND` | — |
| `failed` | `PAYERROR` | — |
| `unknown` | 其他值 | 其他值 |

### 退款状态

| 统一状态 | 微信 | 支付宝 |
| --- | --- | --- |
| `pending` | `PROCESSING` | `fund_change != Y` 或未返回 `REFUND_SUCCESS` |
| `succeeded` | `SUCCESS` | `REFUND_SUCCESS` / `fund_change == Y` |
| `failed` | `ABNORMAL` | — |
| `closed` | `CLOSED` | — |
| `unknown` | 其他值 | — |

### 异常

| 异常 | 含义 |
| --- | --- |
| `PaymentConfigError` | 配置缺失、密钥无法加载、内联与文件同时设置 |
| `PaymentValidationError` | 本地参数或通知内容不满足业务校验 |
| `PaymentSignatureError` | 响应或回调验签失败 |
| `PaymentProviderError` | 渠道返回错误，带 `provider_code`、`http_status` |
| `PaymentNotFoundError` | 渠道查不到订单或退款 |
| `PaymentNetworkError` | 网络失败或超时，带 `may_have_succeeded` |

**超时语义**：`PaymentNetworkError` 不代表支付或退款失败。请求可能已被渠道接受，必须先按原订单号/退款单号查询，再决定是否重试或标记失败。

## 回调

微信和支付宝使用不同的回调入口：

- 微信：JSON 原文 + `Wechatpay-*` 请求头
- 支付宝：`application/x-www-form-urlencoded` 原文

`parse_notification` 会完成：

1. 验签；微信还会解密 `resource`。
2. 校验商户号 / appid；支付宝还会在配置了 `seller_id` 时校验。
3. 校验订单号、金额、币种、支付/退款状态。
4. 转换为统一的 `PaymentNotification`。

微信退款请求默认复用同一个 `notify_url`，因此微信回调入口需要同时处理 `TRANSACTION.*` 和 `REFUND.*` 事件。示例中的 `/notify/wechat` 已按此实现。

调用方必须把**原始请求体**传给 `parse_notification`，不要先反序列化再重新编码。支付宝签名覆盖原始表单字段，重新编码会改变签名内容。

```python
notification = await client.parse_notification(request.headers, raw_body)
notification.assert_matches(
    expected_out_trade_no=order.out_trade_no,
    expected_amount_fen=order.amount_fen,
)
```

### 统一通知字段

`parse_notification` 返回 `PaymentNotification`：

| 字段 | 说明 | 微信来源 | 支付宝来源 |
| --- | --- | --- | --- |
| `channel` | `wechat` / `alipay` | — | — |
| `event_type` | `payment.succeeded`、`payment.pending`、`payment.closed`、`payment.failed`、`refund.succeeded`、`refund.failed`、`unknown` | `TRANSACTION.*`、`REFUND.*` | `trade_status` |
| `notification_id` | 渠道通知唯一 ID，用于去重 | 通知 `id` | `notify_id` |
| `out_trade_no` | 商户订单号，与业务订单绑定 | `out_trade_no` | `out_trade_no` |
| `amount_fen` | 订单总金额（分） | `amount.total` | `total_amount` |
| `currency` | 币种，当前只支持 `CNY` | `amount.currency` | `currency` |
| `status` | 统一支付状态 | `trade_state` | `trade_status` |
| `transaction_id` | 渠道交易号 | `transaction_id` | `trade_no` |
| `out_refund_no` | 退款单号，仅退款通知 | `out_refund_no` | 无（用 `out_request_no` 对账） |
| `refund_amount_fen` | 退款金额（分），仅退款通知 | 单笔退款金额 | `refund_fee` 累计值 |
| `occurred_at` | 渠道完成时间 | `success_time` | `gmt_payment` / `notify_time` |
| `provider_data` | 渠道原始字段（只读），仅用于排查 | 解密后的 `resource` | 表单字段 |

注意：

- 退款通知的 `status` 只有在**全额退款**时才是 `refunded`；部分退款仍是 `paid`，业务应使用 `refund_amount_fen` 判断退款金额。
- 微信退款金额是**单笔**退款金额，支付宝 `refund_fee` 是**累计**退款金额。支付宝退款应以 `refund()` 同步响应和 `query_refund` 对账为准，不能仅凭异步通知里的 `refund_fee` 判断单笔退款完成。
- 支付宝 `WAIT_BUYER_PAY` 会映射为 `payment.pending`；微信默认只在支付成功/关闭时推送。
- 业务逻辑只依赖统一字段；`provider_data` 不建议参与发货判断，渠道字段可能变化。

### 渠道期望的响应

- 微信：成功返回 HTTP 200 + `{"code":"SUCCESS","message":"成功"}`；失败返回 `FAIL` 和非 2xx（示例为 401/400/404/500）。
- 支付宝：成功返回纯文本 `success`；返回其他内容会被视为失败并重试。
- 重复回调：已处理过的通知仍返回成功，避免渠道无限重试；去重靠 `notification_id` 唯一约束。

业务系统之间的 webhook 事件格式、签名和重试策略见「方式 B」。

浏览器跳转（支付宝 `return_url`）只用于展示结果，**不能作为支付成功依据**。支付成功以可信回调或主动查单为准。

## FastAPI 示例

`examples/fastapi_app.py` 演示：

- 下单并保存本地订单。
- 微信 / 支付宝回调入口。
- SQLite 事务 + 唯一约束实现幂等：重复回调不会重复发货。
- 回调金额、渠道、订单号校验。
- 主动查单补偿。
- 退款先到/支付后到、退款去重与累计。
- 自带一个无构建步骤的前端验证台。

运行（无商户凭据的 UI 验证）：

```bash
PYPAY_DEMO_MODE=1 .venv/bin/uvicorn --factory examples.fastapi_app:create_app --reload
# 打开 http://127.0.0.1:8000/
```

运行（真实商户配置）：

```bash
set -a; source .env; set +a
.venv/bin/uvicorn --factory examples.fastapi_app:create_app --reload
```

或使用 uv：

```bash
uv run --env-file .env uvicorn --factory examples.fastapi_app:create_app --reload
```

主要端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 本地验证页面 |
| `GET` | `/qr.svg?data=...` | 服务端生成二维码 SVG |
| `POST` | `/orders` | 创建订单并下单 |
| `GET` | `/orders/{out_trade_no}` | 查询本地订单 |
| `POST` | `/orders/{out_trade_no}/sync` | 主动查单补偿 |
| `POST` | `/notify/wechat` | 微信回调 |
| `POST` | `/notify/alipay` | 支付宝回调 |
| `POST` | `/demo/pay/{out_trade_no}` | 仅演示模式：模拟支付成功 |
| `GET` | `/return/alipay` | 支付宝同步跳转展示页 |
| `GET` | `/health` | 健康检查 |

`PYPAY_CHANNELS` 指定示例启用的渠道，例如 `wechat,alipay`。只启用已配置好凭据的渠道。

### 幂等实现

`examples/db.py` 使用两张表：

- `orders.out_trade_no` 主键。
- `notifications(channel, notification_id)` 唯一约束。
- `notifications(channel, out_refund_no)` 部分唯一索引，防止同一退款换通知 ID 重复累计。

处理回调时在同一个事务中：

1. 插入通知记录；唯一约束冲突说明是重复回调，直接返回成功。
2. 按 `out_trade_no` 读取订单，校验渠道和金额。
3. 支付成功通知将订单更新为 `paid`；如果此前已收到全额退款则保持 `refunded` 且不发货，部分退款则正常记 `fulfilled_at`。
4. 微信退款通知按单笔增量累计；支付宝异步通知中的 `refund_fee` 是累计值，退款最终以 `refund()` 同步响应和 `query_refund` 对账为准。
5. 任何校验失败都会回滚，包括通知记录的插入。

这样即使渠道重复投递或并发投递，发货动作只会发生一次。

### 漏通知补偿

`examples/reconcile.py` 扫描 `pending` 订单并主动查单：

```bash
.venv/bin/python -m examples.reconcile
```

单笔补偿也可以直接调用 `POST /orders/{out_trade_no}/sync`。查单成功且金额一致时才更新本地订单。

## 前端验证台

访问 `http://127.0.0.1:8000/` 可以看到一个无构建步骤的本地验证页面：

- 选择微信支付或支付宝，输入元金额和商品描述。
- 微信返回 Native 二维码，页面通过 `/qr.svg` 在本地生成二维码。
- 支付宝返回电脑网站收银台表单，可点击“打开支付宝收银台”在新窗口提交。
- 展示订单状态、渠道单号、退款金额和状态时间线。
- “主动查单”调用 `POST /orders/{out_trade_no}/sync`。
- 演示模式下额外提供“模拟支付成功”，用于验证订单更新和发货链路。

**演示模式**通过 `PYPAY_DEMO_MODE=1` 启用，使用内存网关，不访问微信/支付宝，也不发起真实支付。该模式会绕过回调验签，**只能用于本地验证，生产环境必须关闭**。

**真实模式**需要配置 `.env` 中的商户凭据。微信 Native 二维码可以被微信扫码；支付宝会打开真实收银台。注意：

- 微信/支付宝的回调地址必须是渠道可访问的 **公网 HTTPS 地址**；`127.0.0.1` 不能被渠道回调，真实联调请使用隧道或测试域名。
- 真实模式下页面只负责创建订单和展示；支付结果仍以可信回调或主动查单为准。

二维码由 `segno` 在服务端生成，不依赖外部 CDN；安装 `.[examples]` 即可。

## 接入业务系统（把 demo 升级为可接入的支付服务）

### 先给结论

**不要在生产环境让业务系统把用户直接跳转到本项目的验证页（`examples/static/index.html`）来收款。**

验证页是本地联调工具，不是收银台产品：

- 没有登录、权限和限流，任何人都能调用 `/orders`。
- 金额和商品描述由浏览器提交，服务端没有和业务系统的订单校验。
- 订单、回调和发货都落在示例 SQLite 里，业务系统不知道支付结果，无法据此开通额度。
- 演示模式会绕过回调验签，`/demo/pay` 可以直接把订单改成已支付。

推荐的接入方式：

| 方式 | 适用场景 | 做法 |
| --- | --- | --- |
| **A. 后端嵌入（推荐）** | 业务系统自己收款、自己发货 | 业务系统后端依赖 `pypay`，自己建单、存单、收回调、发货；前端只复用二维码展示逻辑 |
| **B. 独立收银台** | 支付需要独立部署、多个业务共用 | 把示例升级成有鉴权、订单绑定和回调转发的独立服务；业务系统创建订单后跳转，支付结果回传业务系统 |
| C. 直接跳转示例页 | 只用于本地演示 | **不可用于生产** |

页面本身可以保留并演进成生产收银台，但**订单、金额、回调和发货必须回到业务系统自己的后端**，不能只做一个跳转。

### 方式 A：在业务系统后端嵌入 pypay

1. 安装依赖（同一仓库内可直接用 path 依赖）：

   ```bash
   uv add --editable ../pypay
   # 或 pip install -e ../pypay
   ```

2. 在业务系统的部署环境配置 `PYPAY_CHANNELS`、`PYPAY_WECHAT_*`、`PYPAY_ALIPAY_*`。`PYPAY_WECHAT_NOTIFY_URL` 必须指向业务系统自己的公网 HTTPS 接口，例如：

   ```text
   https://api.example.com/pay/notify/wechat
   ```

3. 下单时先写自己的订单，再调用 pypay；金额以服务端订单为准，绝不接受前端传入的金额：

   ```python
   from pypay import AsyncPaymentClient, CreatePaymentRequest

   pay = AsyncPaymentClient.from_env()


   async def start_recharge(user_id: int, plan_id: int) -> str:
       order = await db.create_order(user_id=user_id, plan_id=plan_id)  # 金额由套餐决定
       result = await pay.create_payment(
           CreatePaymentRequest(
               out_trade_no=order.out_trade_no,
               amount_fen=order.amount_fen,
               description=f"业务系统充值 {order.out_trade_no}",
           )
       )
       await db.save_payment_payload(order.out_trade_no, result.qr_code_url, result.form_html)
       return order.out_trade_no
   ```

4. 前端（或业务系统的收银页）只负责展示：

   - 微信：把 `qr_code_url` 渲染成二维码，例如复用示例的 `/qr.svg?data=...`，或用前端二维码库。
   - 支付宝：把 `form_html` 放到新窗口/iframe 提交，或跳转到表单页。
   - 轮询业务系统自己的 `GET /orders/{out_trade_no}` 展示状态；不要用浏览器跳转判定成功。

5. 回调入口必须读取原始请求体，验签后在**同一个数据库事务**里去重、更新订单、发货：

   ```python
   from pypay import NotificationEvent


   @app.post("/pay/notify/wechat")
   async def wechat_notify(request: Request):
       raw = await request.body()
       notification = await pay.parse_notification(request.headers, raw)

       async with db.transaction():
           if not await db.insert_notification_once(notification):
               return {"code": "SUCCESS", "message": "成功"}  # 重复回调直接成功
           order = await db.get_order_for_update(notification.out_trade_no)
           if order is None:
               raise HTTPException(status_code=404)
           notification.assert_matches(order.out_trade_no, order.amount_fen)
           if notification.event_type is NotificationEvent.PAYMENT_SUCCEEDED:
               await db.mark_paid_and_fulfill(order, notification)  # 只执行一次
       return {"code": "SUCCESS", "message": "成功"}
   ```

   支付宝回调返回 `success` / `failure`，入口逻辑相同。

6. 定时任务扫描 `pending` 订单，调用 `query_payment` 补偿漏通知；网络超时不要直接判失败，先按原订单号查单。浏览器跳转（`return_url`）只展示结果，不修改订单状态。

### 方式 B：把示例升级成可接入的独立支付服务

目标：接入方只通过稳定的 HTTP API 和 webhook 对接，不接触 pypay、商户密钥或微信/支付宝回调。收银页只是这套服务的一个前端。

#### 1. 角色与边界

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| 业务后端 | 用户、商品、业务订单、额度/权益 | 不保存商户密钥，不直接解析渠道回调 |
| 支付服务 | 支付订单、渠道适配、回调验签、退款、对账 | 不保存业务用户和额度 |
| 收银页 | 展示二维码/收银台表单、轮询状态 | 不决定金额，不直接修改订单状态 |

关键原则：**支付服务只认“支付订单”，业务系统只认“业务订单”**，两者用唯一的 `merchant_order_id` 绑定；金额由业务后端决定，收银页和浏览器不能传金额。

#### 2. 对外接口

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/v1/payments` | 服务间 HMAC | 创建支付，必须带 `Idempotency-Key` |
| `GET` | `/v1/payments/{payment_id}` | 服务间 HMAC | 查询支付状态 |
| `POST` | `/v1/payments/{payment_id}/close` | 服务间 HMAC | 关单 |
| `POST` | `/v1/refunds` | 服务间 HMAC | 退款，必须带 `Idempotency-Key` |
| `GET` | `/pay/{token}` | 短时签名 token | 收银页，token 绑定 `payment_id` |
| `POST` | `/notify/wechat`、`/notify/alipay` | 渠道验签 | 只暴露给渠道，公网 HTTPS |
| `POST` | 业务方 webhook | HMAC 签名 | 支付/退款事件，失败重试 |

创建支付请求示例：

```json
{
  "merchant_order_id": "ORDER-20261028-001",
  "amount_fen": 100,
  "channel": "wechat",
  "description": "单次购买",
  "user_ref": "user-123",
  "expires_at": "2026-10-28T12:00:00+08:00"
}
```

返回：

```json
{
  "payment_id": "pay_01J...",
  "status": "pending",
  "qr_code_url": "weixin://wxpay/bizpayurl?pr=...",
  "form_html": null,
  "expires_at": "2026-10-28T12:00:00+08:00"
}
```

支付成功 webhook：

```json
{
  "event_id": "evt_01J...",
  "event_type": "payment.succeeded",
  "payment_id": "pay_01J...",
  "merchant_order_id": "ORDER-20261028-001",
  "amount_fen": 100,
  "transaction_id": "420000...",
  "occurred_at": "2026-10-28T11:05:00+08:00"
}
```

请求头带 `X-Signature`、`X-Timestamp`；业务方按 `event_id` 去重，成功返回 `2xx`。

#### 3. 订单、状态与幂等

- `payment_orders` 对 `(merchant_id, merchant_order_id)` 建唯一约束，重复创建返回同一笔支付。
- 创建和退款都以 `Idempotency-Key` 建唯一记录；同 key 重放返回第一次的结果。
- `notifications` 对 `(channel, notification_id)` 唯一；退款再加 `(channel, out_refund_no)` 部分唯一索引。
- 回调处理在**同一个事务**内：插入通知唯一键 → 校验订单/金额/渠道 → 更新状态 → 触发发货/权益。
- 状态机：`pending → paid / closed / failed`；`paid → refunded`（支持部分退款）。不要允许逆向跳转。
- webhook 用 outbox 表记录，按 `event_id` 去重，指数退避重试；业务方也要幂等。
- 浏览器跳转只展示结果；支付成功以渠道回调或主动查单为准。

#### 4. 安全

- **服务间鉴权**：HMAC-SHA256（timestamp + nonce + body），允许 5 分钟时钟偏差，nonce 缓存防重放；密钥定期轮换。
- **收银页鉴权**：短时签名 token（建议 ≤15 分钟），绑定 `payment_id` 和业务单号；不要用可猜测的自增 ID。
- **金额完整性**：金额只来自业务后端的签名请求；支付服务校验金额与业务订单一致；二维码/表单接口不回传可篡改的金额。
- **渠道回调**：读取原始请求体，先验签再解析；校验商户号、appid、订单号、金额、币种；重复/伪造回调不能改变订单。
- **webhook 签名**：支付服务用独立密钥签名，业务方验签；不要把商户私钥或 APIv3 密钥传给业务方。
- **密钥管理**：KMS/Secret Manager 或只读文件挂载，文件权限 `600`；测试与生产使用不同商户号/appid；定期轮换 APIv3 密钥和商户证书。
- **网络与日志**：只暴露 `/notify/*` 和收银页；管理接口走内网或 IP 白名单；关闭生产 `/docs`；日志脱敏，不记录私钥、APIv3 密钥、完整回调原文。
- **限流与请求体**：按 token/IP/接口限流；创建支付、回调、查询分别限额；限制请求体大小（示例为 256 KiB）。

#### 5. 告警与对账

至少监控：

- 回调验签失败率、回调 5xx 率、重复回调比例。
- `pending` 超过 5 分钟的订单数、支付成功率、渠道错误码分布。
- webhook 重试耗尽、退款长时间未完成、对账金额不一致。
- 渠道证书/公钥轮换到期时间。

定时任务扫描 `pending` 订单调用 `query_payment` 补偿；退款以 `query_refund` 对账。对账差异要能定位到 `payment_id` 和业务单号。

#### 6. 上线顺序

1. 拆分配置：删除演示网关和 `/demo/pay`，生产环境硬性禁止演示模式。
2. 建订单表、通知唯一键、幂等表和迁移脚本。
3. 实现创建支付、查询、关单和收银页。
4. 接入渠道回调、webhook outbox 和重试。
5. 接入主动查单、对账和退款。
6. 加服务间鉴权、收银页 token、限流和告警。
7. 补测试：伪造回调、重复回调、金额不符、并发、超时、webhook 重放。
8. 小流量灰度，确认回调可达和对账一致后再放量。

#### 7. 接入方需要做什么

1. 申请 `merchant_id` / HMAC 密钥，配置自己的 webhook 地址。
2. 创建支付时传业务单号、金额和 `Idempotency-Key`。
3. 把收银页嵌入或跳转给用户；不要在前端计算或修改金额。
4. 验签 webhook，按 `event_id` 去重，成功后返回 `2xx`。
5. 提供查询兜底：webhook 丢失时按 `payment_id` 或业务单号主动查单。
6. 发货/开额度必须幂等，不能因为重复 webhook 重复发放。

#### 8. 从当前 demo 改动的清单

- [ ] 关闭 `PYPAY_DEMO_MODE`，删除或禁用 `/demo/pay`。
- [ ] `/orders` 增加鉴权；金额、商品和订单号由服务端决定，不能由浏览器提交。
- [ ] 支付订单与业务订单一一绑定，回调后通过直接写库或签名 webhook 通知业务系统发货。
- [ ] 订单表、通知唯一键、事务和发货幂等（参考 `examples/db.py`）。
- [ ] 公网 HTTPS 回调地址、验签、金额/渠道/订单校验、请求体大小限制。
- [ ] 主动查单补偿、对账任务、退款流程。
- [ ] 服务间 HMAC、收银页短时 token、限流、告警、日志脱敏。
- [ ] 密钥管理和轮换；测试与生产商户隔离。
- [ ] 不暴露 `/docs`、调试端点或示例数据库。

### 一句话选择

- 业务系统自己发货：**方式 A**，pypay 当库用，页面 UI 只作为前端参考。
- 多个业务共用一个支付服务：**方式 B**，先把示例改造成有鉴权和订单绑定的独立服务。
- 只是本地看二维码/走通流程：可以用验证页，但不要对公网开放，也不要接真实收款。

## 安全清单

- [x] 密钥只从环境变量或外部文件读取。
- [x] 配置对象 `repr` 不包含私钥和 APIv3 密钥。
- [x] `.gitignore` 排除 `.env`、`*.pem`、`*.key`、`*.crt`、`*.p12`、`*.db`。
- [x] 回调先验签再解析业务字段。
- [x] 校验商户号 / appid / seller_id、订单号、金额、币种、状态。
- [x] 回调处理使用数据库事务和唯一约束；退款通知按 `out_refund_no` 去重。
- [x] 超时不判定为失败，要求按原单号查询。
- [x] 示例限制通知请求体为 256 KiB。
- [x] 演示模式默认关闭；生产环境必须保持 `PYPAY_DEMO_MODE=0`。
- [ ] 生产环境仍需配置 HTTPS、限流、告警、密钥轮换和反向代理层的请求体限制。
- [ ] `notify_url` / `return_url` 必须来自服务端配置，不要接受用户输入；多租户场景应使用域名白名单，避免把签名订单数据发到第三方或形成支付后开放跳转。

## 测试

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest --cov=src/pypay --cov-report=term-missing
.venv/bin/ruff check src examples tests
.venv/bin/ruff format --check src examples tests
```

测试使用**运行时生成的临时 RSA 密钥**和**模拟网络**，不访问真实支付网关，不使用真实商户凭据。

已覆盖：

- 微信 Native 下单请求签名、二维码返回、查单、关单、退款、退款查询。
- 微信回调验签、AES-GCM 解密、商户/appid 校验、金额/币种校验、事件与状态一致性。
- 支付宝电脑网站支付表单签名、金额分转元、查单、关单、退款、退款查询。
- 支付宝回调验签、app_id/seller_id 校验、金额/币种校验、状态映射。
- 伪造回调、重复回调、金额不符、渠道不符、网络超时、渠道错误码。
- 异步客户端在工作线程执行同步 SDK 调用。
- FastAPI 示例的下单、回调幂等、主动查单补偿、退款先到/支付后到、退款去重。
- PKCS#1/PKCS#8/无头 base64 密钥规范化，以及支付宝 SPKI 公钥验签。
- 真实签名/加密链路的端到端测试（微信与支付宝各一条）。
- 前端验证台页面、二维码 SVG、演示模式下单/模拟支付。

**待真实商户联调项**：

- 微信商户号与 appid 绑定关系、公钥 ID、APIv3 密钥、回调地址可达性。
- 微信 Native 二维码实际扫码支付、关单与退款到账。
- 支付宝应用的产品权限（电脑网站支付）、应用公钥/支付宝公钥配置、网关与沙箱。
- 支付宝证书模式（尚未实现）。
- 真实网络下的超时、重试、限流和渠道风控。
- 对账、退款异常处理、密钥轮换与证书更新流程。

## 已知限制

- 首期只覆盖微信 Native 与支付宝电脑网站支付；未实现 JSAPI、小程序、App、H5、订阅/自动续费。
- 支付宝证书模式未实现。
- 支付宝退款查询必须同时提供 `out_trade_no`。
- 支付宝退款以同步响应和 `query_refund` 为准；`RefundRequest.notify_url` 当前只对微信生效。
- 示例对微信退款通知按单笔增量累计，并按 `(channel, out_refund_no)` 去重；对支付宝交易通知中的 `refund_fee` 按累计值处理。多笔部分退款的最终金额仍应以 `query_refund` 对账为准。
- 示例的 `reconcile.py` 只扫描 `pending` 订单；已支付订单上的退款漏通知需要通过退款单记录和 `query_refund` 单独对账。
- `PaymentClient` 绑定单一渠道和单一商户账户；多渠道请分别创建客户端。
- 示例使用 SQLite，生产环境应替换为项目自己的数据库和事务方案。
- 示例 `/orders` 和前端验证台没有登录、权限和限流；生产环境必须在业务层补齐，或不要暴露该页面（见「接入业务系统」）。
- 演示模式绕过验签，仅用于本地 UI/流程验证，不能用于任何真实收款场景。
- 示例允许客户端传入 `out_trade_no`；生产环境建议由服务端生成，避免订单号被猜测或复用。

## 常见问题

### 本地和生产分别怎么存密钥？

本地开发：

- 使用独立测试商户号/appid，不要复用生产密钥。
- 放在仓库外的专用目录，例如 `~/.config/pypay/keys/`；目录 `700`，私钥/证书/APIv3 密钥 `600`。
- 通过环境变量指向文件（`*_PATH`），不要写进 `.env.example`、代码、日志或截图。
- `.env` 如果使用，必须被 `.gitignore` 排除；更推荐 shell/direnv 注入。
- 不要放在桌面、下载目录或云同步盘。

生产：

- 使用 KMS / Secrets Manager / Vault / 云厂商密钥管理，运行时只读挂载或注入。
- 支付服务独占密钥；业务服务、前端、运维面板都不应拿到商户私钥。
- 测试/预发/生产使用不同商户和不同密钥；最小权限、访问审计。
- 容器镜像、CI 日志、构建缓存、监控标签里不能出现密钥。
- 轮换：先在商户平台生成新证书/密钥，灰度切换 `CERT_SERIAL_NO`、`PUBLIC_KEY_ID` 和密钥文件，再吊销旧密钥；APIv3 密钥轮换要同步更新回调解密。
- 怀疑泄露：先轮换，再查日志和访问记录；不要只删日志。

### 企业级支付对接一定要用 RocketMQ 吗？

不一定。支付正确性来自数据库事务、唯一约束、幂等和主动查单/对账，不来自消息队列；队列只能解耦和缓冲，不能替代这些。

不需要 MQ 的场景：单一支付服务 + 单一业务库，回调后直接写库/发 webhook，定时任务补偿，已经足够。

适合引入 MQ 的场景：

- 支付事件要分发给多个下游（发货、额度、CRM、通知、分析）。
- 下游处理慢，需要削峰/异步，不阻塞渠道回调响应。
- 已有一套 RocketMQ/Kafka 运维体系，且需要重试、死信、顺序或延迟消息。

推荐链路：

1. 渠道回调 → 验签 → 同一事务写通知唯一键、更新支付订单、写 outbox 事件。
2. outbox 投递到 RocketMQ（key 用 `payment_id`/订单号，消息带 `event_id`）。
3. 下游按 `event_id` 幂等消费；失败重试，超过次数进死信并告警。
4. 外部租户仍然用签名 webhook；内部服务可以用 MQ。

注意：

- RocketMQ 是至少一次投递，可能重复；不要假设 exactly-once，消费端必须幂等。
- 不要“回调先丢进 MQ，再写库”；MQ 故障或消息丢失会丢支付状态。先持久化，再投递。
- 事务消息可以减少“本地事务成功但消息没发”的问题，但 DB outbox 更简单、可移植；两者都要幂等。
- 监控消费堆积、重试、死信、回调失败率；对账任务不能省。

## License

MIT
