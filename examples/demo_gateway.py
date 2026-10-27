from html import escape

from pypay import (
    Channel,
    ClosePaymentResult,
    CreatePaymentRequest,
    CreatePaymentResult,
    PaymentNotFoundError,
    PaymentNotification,
    PaymentStatus,
    PaymentValidationError,
    QueryPaymentResult,
    QueryRefundResult,
    RefundRequest,
    RefundResult,
)


class DemoGateway:
    """In-memory gateway used only by the local verification page."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self._amounts: dict[str, int] = {}

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        self._amounts[request.out_trade_no] = request.amount_fen
        if self.channel is Channel.WECHAT:
            return CreatePaymentResult(
                channel=self.channel,
                out_trade_no=request.out_trade_no,
                amount_fen=request.amount_fen,
                status=PaymentStatus.PENDING,
                qr_code_url=f"weixin://wxpay/bizpayurl?pr=demo-{request.out_trade_no}",
            )
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            form_html=self._alipay_form(request),
        )

    async def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        amount = self._amounts.get(out_trade_no)
        if amount is None:
            raise PaymentNotFoundError("demo order not found")
        return QueryPaymentResult(
            channel=self.channel,
            out_trade_no=out_trade_no,
            amount_fen=amount,
            status=PaymentStatus.PENDING,
            channel_status="DEMO_PENDING",
        )

    async def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        return ClosePaymentResult(
            channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
        )

    async def refund(self, request: RefundRequest) -> RefundResult:
        raise PaymentValidationError("demo gateway does not support refunds")

    async def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        raise PaymentValidationError("demo gateway does not support refunds")

    async def parse_notification(
        self, headers: object, body: bytes | str, **kwargs: object
    ) -> PaymentNotification:
        raise PaymentValidationError("demo gateway does not accept real notifications")

    @staticmethod
    def _alipay_form(request: CreatePaymentRequest) -> str:
        order = escape(request.out_trade_no)
        description = escape(request.description)
        amount = f"{request.amount_fen // 100}.{request.amount_fen % 100:02d}"
        return f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>支付宝收银台（演示）</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #f3f6ff; color: #10203a;
         font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  main {{ width: min(420px, 86vw); padding: 32px; border: 1px solid #c9d6f2;
          border-radius: 20px 20px 20px 4px; background: #fff;
          box-shadow: 8px 8px 0 #dbe5fb; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
          background: #1677ff; color: #fff; font-size: 12px; letter-spacing: .08em; }}
  h1 {{ margin: 16px 0 4px; font-size: 24px; }}
  p {{ margin: 8px 0; color: #45536b; }}
  code {{ color: #10203a; }}
</style>
<main>
  <span class="tag">DEMO</span>
  <h1>支付宝收银台</h1>
  <p>订单：<code>{order}</code></p>
  <p>商品：{description}</p>
  <p>金额：<code>¥{amount}</code></p>
  <p>这是演示页面，不会发起真实支付。</p>
</main>
</html>"""


def demo_gateways() -> dict[Channel, DemoGateway]:
    return {
        Channel.WECHAT: DemoGateway(Channel.WECHAT),
        Channel.ALIPAY: DemoGateway(Channel.ALIPAY),
    }
