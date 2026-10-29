"""Local-only demo of the unified payment service.

This example uses in-memory fake channel gateways and a fake notifier. It
never contacts WeChat Pay or Alipay and never uses real credentials.

Run:
    python examples/service_demo.py

Open the printed cashier URLs, then use the printed "simulate payment" URL
(or the Alipay demo form) to see the paid and acknowledged states.
"""

from __future__ import annotations

import asyncio
import html
import logging
import tempfile
import uuid
from pathlib import Path

import uvicorn
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from pypay import (
    Channel,
    ClosePaymentResult,
    CreatePaymentResult,
    NotificationEvent,
    PaymentNotFoundError,
    PaymentNotification,
    PaymentStatus,
    PaymentValidationError,
    QueryPaymentResult,
    QueryRefundResult,
    RefundResult,
    RefundStatus,
)
from pypay_service.app import create_app
from pypay_service.callbacks_service import apply_channel_notification
from pypay_service.config import ChannelAccountSeed, ServiceConfig
from pypay_service.db import Database
from pypay_service.enums import AttemptStatus
from pypay_service.notifier import DeliveryResult
from pypay_service.payments_service import CreateOrderInput, create_order
from pypay_service.projects_service import create_project, sync_channel_accounts
from pypay_service.repo_orders import get_attempt_by_out_trade_no, get_order, list_attempts
from pypay_service.security import SecretBox
from pypay_service.timeutil import utcnow
from pypay_service.worker import OutboxWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pypay.demo")

HOST = "127.0.0.1"
PORT = 8099
LOCAL_BASE = f"http://{HOST}:{PORT}"
DEMO_BASE = "https://pay.demo"


class DemoGateway:
    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    async def create_payment(self, request) -> CreatePaymentResult:
        if self.channel is Channel.WECHAT:
            return CreatePaymentResult(
                channel=self.channel,
                out_trade_no=request.out_trade_no,
                amount_fen=request.amount_fen,
                status=PaymentStatus.PENDING,
                qr_code_url=f"weixin://demo/{request.out_trade_no}",
            )
        order_no = html.escape(request.out_trade_no)
        form_html = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<title>支付宝演示收银台</title>
<body style="font-family:system-ui;padding:40px;background:#f4efe6;color:#1b1a17">
  <h1 style="font-family:Georgia,serif">支付宝演示收银台</h1>
  <p>这是本地演示表单，不会发起真实支付。</p>
  <form id="demo-pay" method="get" action="/demo/alipay/{order_no}">
    <button type="submit" style="padding:12px 20px;border-radius:10px;border:0;background:#0f6b5c;color:white;font-weight:700">
      模拟支付成功
    </button>
  </form>
  <script>document.getElementById('demo-pay').submit();</script>
</body></html>"""
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            form_html=form_html,
        )

    async def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        raise PaymentNotFoundError("demo gateway does not query")

    async def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        return ClosePaymentResult(
            channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
        )

    async def refund(self, request) -> RefundResult:
        return RefundResult(
            channel=self.channel,
            out_refund_no=request.out_refund_no,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            total_amount_fen=request.total_amount_fen,
            status=RefundStatus.SUCCEEDED,
        )

    async def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        raise PaymentNotFoundError("demo gateway does not query refunds")

    async def parse_notification(self, headers, body, **kwargs) -> PaymentNotification:
        raise PaymentValidationError("demo gateway does not parse real notifications")


class DemoNotifier:
    def send(
        self,
        *,
        notify_url: str,
        notify_secret: str,
        event_id: str,
        event_type: str,
        body: bytes,
    ) -> DeliveryResult:
        logger.info("demo notification %s -> %s", event_type, notify_url)
        return DeliveryResult(status_code=200, body_excerpt="demo acknowledged")


def _seed() -> tuple[ServiceConfig, Database, SecretBox, object, object]:
    tmp = Path(tempfile.mkdtemp(prefix="pypay-service-demo-"))
    config = ServiceConfig(
        database_path=tmp / "demo.db",
        public_base_url=DEMO_BASE,
        master_key=b"d" * 32,
        admin_username="admin",
        admin_password="demo-admin-password",
        viewer_username="viewer",
        viewer_password="demo-viewer-password",
        allow_http_urls=True,
        allow_private_urls=True,
        channel_accounts=(
            ChannelAccountSeed(
                id="wechat-demo",
                channel=Channel.WECHAT,
                name="微信支付（演示）",
                env_prefix="DEMO_WECHAT_",
            ),
            ChannelAccountSeed(
                id="alipay-demo",
                channel=Channel.ALIPAY,
                name="支付宝（演示）",
                env_prefix="DEMO_ALIPAY_",
            ),
        ),
    )
    db = Database(config.database_path)
    db.migrate_up()
    sync_channel_accounts(db, config, utcnow())
    box = SecretBox(config.master_key)
    project_a = create_project(
        db,
        box,
        config,
        project_id="super-aigc",
        name="Super-AIGC",
        notify_url=f"{LOCAL_BASE}/demo/notify/super-aigc",
        return_urls=[f"{LOCAL_BASE}/demo/return"],
        channels={Channel.WECHAT: "wechat-demo", Channel.ALIPAY: "alipay-demo"},
        now=utcnow(),
    )
    project_b = create_project(
        db,
        box,
        config,
        project_id="super-aigc-digitalhuman",
        name="Super-AIGC 数字人",
        notify_url=f"{LOCAL_BASE}/demo/notify/super-aigc-digitalhuman",
        return_urls=[f"{LOCAL_BASE}/demo/return"],
        channels={Channel.WECHAT: "wechat-demo", Channel.ALIPAY: "alipay-demo"},
        now=utcnow(),
    )
    order_a = create_order(
        db,
        config,
        project_a.project,
        CreateOrderInput(
            business_order_no="AIGC-20261029-001",
            subject="Super-AIGC 额度包",
            amount_fen=9900,
            return_url=f"{LOCAL_BASE}/demo/return",
        ),
        now=utcnow(),
    ).order
    order_b = create_order(
        db,
        config,
        project_b.project,
        CreateOrderInput(
            business_order_no="DH-20261029-001",
            subject="数字人形象定制",
            amount_fen=19900,
            return_url=f"{LOCAL_BASE}/demo/return",
        ),
        now=utcnow(),
    ).order
    return config, db, box, (project_a, order_a), (project_b, order_b)


def _mark_paid(db: Database, order, attempt) -> None:
    notification = PaymentNotification(
        channel=attempt.channel,
        event_type=NotificationEvent.PAYMENT_SUCCEEDED,
        notification_id=f"DEMO-{uuid.uuid4().hex}",
        out_trade_no=attempt.out_trade_no,
        amount_fen=order.amount_fen,
        currency="CNY",
        status=PaymentStatus.PAID,
        transaction_id=f"DEMO-{uuid.uuid4().hex[:12].upper()}",
        occurred_at=utcnow(),
    )
    apply_channel_notification(db, notification, now=utcnow())


def build_demo():
    config, db, box, (project_a, order_a), (project_b, order_b) = _seed()
    notifier = DemoNotifier()
    gateways = {
        "wechat-demo": DemoGateway(Channel.WECHAT),
        "alipay-demo": DemoGateway(Channel.ALIPAY),
    }
    app = create_app(
        config,
        gateways=gateways,
        notifier=notifier,
        db=db,
        migrate=False,
    )

    @app.get("/demo/return")
    async def demo_return() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>返回网站</title>"
            "<main style='font-family:system-ui;padding:48px'>"
            "<h1>这是演示业务网站</h1><p>支付结果请以业务系统收到的服务端通知为准。</p>"
            "<p><a href='/'>返回演示服务</a> · <a href='/admin'>管理后台</a></p></main>"
        )

    @app.get("/demo/notify/{project_id}")
    async def demo_notify(project_id: str) -> JSONResponse:
        return JSONResponse({"ok": True, "project_id": project_id, "demo": True})

    @app.get("/demo/pay/{payment_id}")
    async def demo_pay(payment_id: str) -> Response:
        with db.read() as conn:
            order = get_order(conn, payment_id)
            attempts = list_attempts(conn, order.id) if order else []
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        attempt = next((a for a in reversed(attempts) if a.status is AttemptStatus.PENDING), None)
        if attempt is not None:
            _mark_paid(db, order, attempt)
        return RedirectResponse(url=f"/pay/{order.cashier_token}", status_code=303)

    @app.get("/demo/alipay/{out_trade_no}")
    async def demo_alipay(out_trade_no: str) -> Response:
        with db.read() as conn:
            attempt = get_attempt_by_out_trade_no(conn, out_trade_no)
            order = get_order(conn, attempt.order_id) if attempt else None
        if attempt is None or order is None:
            raise HTTPException(status_code=404, detail="payment attempt not found")
        if attempt.status is AttemptStatus.PENDING:
            _mark_paid(db, order, attempt)
        return RedirectResponse(url=f"/pay/{order.cashier_token}", status_code=303)

    @app.on_event("startup")
    async def start_outbox() -> None:
        async def loop() -> None:
            worker = OutboxWorker(db, config, box, notifier)
            while True:
                try:
                    await asyncio.to_thread(worker.run_once)
                except Exception:
                    logger.exception("demo outbox pass failed")
                await asyncio.sleep(1)

        app.state.demo_task = asyncio.create_task(loop())

    @app.on_event("shutdown")
    async def stop_outbox() -> None:
        task = getattr(app.state, "demo_task", None)
        if task is not None:
            task.cancel()

    print("=" * 72)
    print("pypay 统一支付服务 · 本地演示（不会发起真实支付）")
    print(f"数据库: {config.database_path}")
    print(f"管理后台: {LOCAL_BASE}/admin")
    print("  管理员: admin / demo-admin-password")
    print("  只读:   viewer / demo-viewer-password")
    for label, project, order in (
        ("Super-AIGC", project_a, order_a),
        ("数字人", project_b, order_b),
    ):
        print("-" * 72)
        print(f"项目: {label} ({project.project.id})")
        print(f"  key_id={project.project.api_key_id}")
        print(f"  api_secret={project.api_secret}")
        print(f"  notify_secret={project.notify_secret}")
        print(f"  收银台: {LOCAL_BASE}/pay/{order.cashier_token}")
        print(f"  模拟支付: {LOCAL_BASE}/demo/pay/{order.id}")
    print("=" * 72)
    print("按 Ctrl+C 停止。")
    return app


if __name__ == "__main__":
    uvicorn.run(build_demo(), host=HOST, port=PORT, log_level="info")
