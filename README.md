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
├── fastapi_app.py     # 下单、回调、查单补偿示例
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

也支持通过 `*_KEY` 环境变量传完整 PEM。同一组密钥的“文件路径”和“内联 PEM”只能设置一个，配置校验会明确报错。

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
| `PYPAY_ALIPAY_PUBLIC_KEY(_PATH)` | 支付宝公钥 PEM |
| `PYPAY_ALIPAY_NOTIFY_URL` | 异步通知地址 |
| `PYPAY_ALIPAY_RETURN_URL` | 浏览器同步跳转地址，不作为支付依据 |
| `PYPAY_ALIPAY_GATEWAY` | 可选，默认生产网关；沙箱请改为沙箱网关 |
| `PYPAY_ALIPAY_SELLER_ID` | 可选；设置后会校验通知中的 `seller_id` |

注意：

- 官方支付宝 SDK 的 RSA2 签名实现只接受 **PKCS#1** 私钥。适配层会用 `cryptography` 将 PKCS#8 转为 PKCS#1，不会重写签名算法。
- 首期只实现**支付宝公钥模式**。证书模式（`app_cert_sn` / `alipay_root_cert_sn`）尚未接入，接入前需要扩展适配层并完成真实联调。
- 支付宝扫码支付（`alipay.trade.precreate`）不在首期范围。若后续接入，需要开通**当面付**产品权限。
- 微信 Native 使用微信支付公钥模式；配置中必须同时提供公钥和公钥 ID，模块不会调用 `/v3/certificates` 下载平台证书。

## 快速开始

```python
from pypay import CreatePaymentRequest, PaymentClient

client = PaymentClient.from_env()  # 读取 PYPAY_CHANNEL
result = client.create_payment(
    CreatePaymentRequest(
        out_trade_no="ORDER-20261027-001",
        amount_fen=1,                 # 单位：分
        description="单次购买",
        expires_at=None,              # 可选，必须带时区
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

调用方必须把**原始请求体**传给 `parse_notification`，不要先反序列化再重新编码。支付宝签名覆盖原始表单字段，重新编码会改变签名内容。

```python
notification = await client.parse_notification(request.headers, raw_body)
notification.assert_matches(
    expected_out_trade_no=order.out_trade_no,
    expected_amount_fen=order.amount_fen,
)
```

浏览器跳转（支付宝 `return_url`）只用于展示结果，**不能作为支付成功依据**。支付成功以可信回调或主动查单为准。

## FastAPI 示例

`examples/fastapi_app.py` 演示：

- 下单并保存本地订单。
- 微信 / 支付宝回调入口。
- SQLite 事务 + 唯一约束实现幂等：重复回调不会重复发货。
- 回调金额、渠道、订单号校验。
- 主动查单补偿。
- 退款通知累计退款金额。

运行：

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
| `POST` | `/orders` | 创建订单并下单 |
| `GET` | `/orders/{out_trade_no}` | 查询本地订单 |
| `POST` | `/orders/{out_trade_no}/sync` | 主动查单补偿 |
| `POST` | `/notify/wechat` | 微信回调 |
| `POST` | `/notify/alipay` | 支付宝回调 |
| `GET` | `/return/alipay` | 支付宝同步跳转展示页 |
| `GET` | `/health` | 健康检查 |

`PYPAY_CHANNELS` 指定示例启用的渠道，例如 `wechat,alipay`。只启用已配置好凭据的渠道。

### 幂等实现

`examples/db.py` 使用两张表：

- `orders.out_trade_no` 主键。
- `notifications(channel, notification_id)` 唯一约束。

处理回调时在同一个事务中：

1. 插入通知记录；唯一约束冲突说明是重复回调，直接返回成功。
2. 按 `out_trade_no` 读取订单，校验渠道和金额。
3. 只有 `status NOT IN ('paid', 'refunded')` 时才更新为已支付并写 `fulfilled_at`。
4. 任何校验失败都会回滚，包括通知记录的插入。

这样即使渠道重复投递或并发投递，发货动作只会发生一次。

### 漏通知补偿

`examples/reconcile.py` 扫描 `pending` 订单并主动查单：

```bash
.venv/bin/python -m examples.reconcile
```

单笔补偿也可以直接调用 `POST /orders/{out_trade_no}/sync`。查单成功且金额一致时才更新本地订单。

## 接入其他项目

1. 只依赖 `src/pypay`，不要把 FastAPI、SQLite 或业务表带进核心模块。
2. 在自己的订单表中保存 `out_trade_no`、金额（分）、渠道和状态。
3. 回调入口读取原始请求体，调用 `parse_notification`。
4. 在自己的数据库事务中先写通知唯一键，再更新订单和发货状态。
5. 定时任务扫描待支付订单，调用 `query_payment` 补偿。
6. 浏览器跳转页只展示结果，不修改订单状态。

## 安全清单

- [x] 密钥只从环境变量或外部文件读取。
- [x] 配置对象 `repr` 不包含私钥和 APIv3 密钥。
- [x] `.gitignore` 排除 `.env`、`*.pem`、`*.key`、`*.crt`、`*.p12`、`*.db`。
- [x] 回调先验签再解析业务字段。
- [x] 校验商户号 / appid / seller_id、订单号、金额、币种、状态。
- [x] 回调处理使用数据库事务和唯一约束。
- [x] 超时不判定为失败，要求按原单号查询。
- [ ] 生产环境仍需配置 HTTPS、请求体大小限制、限流、告警和密钥轮换流程。

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
- FastAPI 示例的下单、回调幂等、主动查单补偿、退款累计。
- 真实签名/加密链路的端到端测试（微信与支付宝各一条）。

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
- `PaymentClient` 绑定单一渠道和单一商户账户；多渠道请分别创建客户端。
- 示例使用 SQLite，生产环境应替换为项目自己的数据库和事务方案。

## License

MIT
