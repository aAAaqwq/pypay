import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.db import Database
from examples.fastapi_app import create_app
from pypay import (
    Channel,
    ClosePaymentResult,
    CreatePaymentRequest,
    CreatePaymentResult,
    NotificationEvent,
    PaymentConfigError,
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentNotification,
    PaymentProviderError,
    PaymentSignatureError,
    PaymentStatus,
    PaymentValidationError,
    QueryPaymentResult,
    QueryRefundResult,
    RefundResult,
    RefundStatus,
)


class FakeGateway:
    def __init__(
        self,
        channel: Channel,
        *,
        notification: PaymentNotification | None = None,
        query_result: QueryPaymentResult | None = None,
        create_error: Exception | None = None,
        query_error: Exception | None = None,
        parse_error: Exception | None = None,
    ) -> None:
        self.channel = channel
        self.notification = notification
        self.query_result = query_result
        self.create_error = create_error
        self.query_error = query_error
        self.parse_error = parse_error

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        if self.create_error:
            raise self.create_error
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            qr_code_url="weixin://test" if self.channel is Channel.WECHAT else None,
            form_html="<form></form>" if self.channel is Channel.ALIPAY else None,
        )

    async def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        if self.query_error:
            raise self.query_error
        if self.query_result is None:
            raise AssertionError("query_result not configured")
        return self.query_result

    async def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        return ClosePaymentResult(
            channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
        )

    async def refund(self, request: Any) -> RefundResult:
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
        return QueryRefundResult(
            channel=self.channel,
            out_refund_no=out_refund_no,
            out_trade_no=out_trade_no or "ORDER1",
            amount_fen=100,
            status=RefundStatus.SUCCEEDED,
        )

    async def parse_notification(
        self, headers: Any, body: bytes | str, **kwargs: Any
    ) -> PaymentNotification:
        if self.parse_error:
            raise self.parse_error
        if self.notification is None:
            raise AssertionError("notification not configured")
        return self.notification


def notification(
    channel: Channel,
    notification_id: str,
    *,
    out_trade_no: str,
    amount_fen: int = 100,
    event_type: NotificationEvent = NotificationEvent.PAYMENT_SUCCEEDED,
    status: PaymentStatus = PaymentStatus.PAID,
    refund_amount_fen: int | None = None,
    out_refund_no: str | None = None,
) -> PaymentNotification:
    return PaymentNotification(
        channel=channel,
        event_type=event_type,
        notification_id=notification_id,
        out_trade_no=out_trade_no,
        amount_fen=amount_fen,
        currency="CNY",
        status=status,
        transaction_id=f"TX-{notification_id}",
        refund_amount_fen=refund_amount_fen,
        out_refund_no=out_refund_no,
    )


def new_client(tmp_path: Path, *gateways: FakeGateway) -> TestClient:
    app = create_app(
        {gateway.channel: gateway for gateway in gateways},
        db_path=tmp_path / "demo.db",
    )
    return TestClient(app)


def create_order(
    client: TestClient,
    *,
    channel: str = "wechat",
    amount_fen: int = 100,
    out_trade_no: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "amount_fen": amount_fen,
        "description": "test order",
        "channel": channel,
    }
    if out_trade_no is not None:
        payload["out_trade_no"] = out_trade_no
    response = client.post("/orders", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def notification_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0])
    finally:
        conn.close()


def test_create_order_and_get(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    created = create_order(client)
    assert created["channel"] == "wechat"
    assert created["status"] == "pending"
    assert created["qr_code_url"] == "weixin://test"

    response = client.get(f"/orders/{created['out_trade_no']}")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["fulfilled_at"] is None


def test_create_order_rejects_duplicate_out_trade_no(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    create_order(client, out_trade_no="ORDER-FIXED")
    response = client.post(
        "/orders",
        json={
            "amount_fen": 100,
            "description": "duplicate",
            "channel": "wechat",
            "out_trade_no": "ORDER-FIXED",
        },
    )
    assert response.status_code == 409


def test_create_order_provider_rejection_marks_order_failed(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT, create_error=PaymentProviderError("boom"))
    client = new_client(tmp_path, gateway)
    response = client.post(
        "/orders", json={"amount_fen": 100, "description": "test", "channel": "wechat"}
    )
    assert response.status_code == 502
    out_trade_no = response.json()["detail"]["out_trade_no"]
    assert client.get(f"/orders/{out_trade_no}").json()["status"] == "failed"


def test_create_order_provider_5xx_keeps_order_pending(tmp_path: Path) -> None:
    gateway = FakeGateway(
        Channel.WECHAT,
        create_error=PaymentProviderError("gateway error", http_status=502),
    )
    client = new_client(tmp_path, gateway)
    response = client.post(
        "/orders", json={"amount_fen": 100, "description": "test", "channel": "wechat"}
    )
    assert response.status_code == 502
    assert response.json()["detail"]["may_have_succeeded"] is True
    out_trade_no = response.json()["detail"]["out_trade_no"]
    assert client.get(f"/orders/{out_trade_no}").json()["status"] == "pending"


def test_create_order_signature_failure_keeps_order_pending(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT, create_error=PaymentSignatureError("bad signature"))
    client = new_client(tmp_path, gateway)
    response = client.post(
        "/orders", json={"amount_fen": 100, "description": "test", "channel": "wechat"}
    )
    assert response.status_code == 502
    assert response.json()["detail"]["may_have_succeeded"] is True
    out_trade_no = response.json()["detail"]["out_trade_no"]
    assert client.get(f"/orders/{out_trade_no}").json()["status"] == "pending"


def test_create_order_network_failure_is_reported_as_ambiguous(tmp_path: Path) -> None:
    gateway = FakeGateway(
        Channel.WECHAT, create_error=PaymentNetworkError("timeout", may_have_succeeded=True)
    )
    client = new_client(tmp_path, gateway)
    response = client.post(
        "/orders", json={"amount_fen": 100, "description": "test", "channel": "wechat"}
    )
    assert response.status_code == 502
    assert response.json()["detail"]["may_have_succeeded"] is True


def test_wechat_notification_is_idempotent(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.notification = notification(Channel.WECHAT, "notify-1", out_trade_no=order_no)

    first = client.post("/notify/wechat", content=b"{}")
    assert first.status_code == 200
    assert first.json()["code"] == "SUCCESS"
    paid = client.get(f"/orders/{order_no}").json()
    assert paid["status"] == "paid"
    fulfilled_at = paid["fulfilled_at"]
    assert fulfilled_at is not None

    second = client.post("/notify/wechat", content=b"{}")
    assert second.status_code == 200
    again = client.get(f"/orders/{order_no}").json()
    assert again["fulfilled_at"] == fulfilled_at
    assert notification_count(tmp_path / "demo.db") == 1


def test_wechat_notification_amount_mismatch_rolls_back(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.notification = notification(
        Channel.WECHAT, "notify-1", out_trade_no=order_no, amount_fen=99
    )
    response = client.post("/notify/wechat", content=b"{}")
    assert response.status_code == 400
    assert client.get(f"/orders/{order_no}").json()["status"] == "pending"
    assert notification_count(tmp_path / "demo.db") == 0


def test_wechat_notification_unknown_order_is_not_recorded(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    gateway.notification = notification(Channel.WECHAT, "notify-1", out_trade_no="MISSING")
    response = client.post("/notify/wechat", content=b"{}")
    assert response.status_code == 404
    assert notification_count(tmp_path / "demo.db") == 0


def test_wechat_notification_signature_error(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT, parse_error=PaymentSignatureError("bad"))
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    response = client.post("/notify/wechat", content=b"{}")
    assert response.status_code == 401
    assert client.get(f"/orders/{created['out_trade_no']}").json()["status"] == "pending"


def test_wechat_notification_channel_mismatch(tmp_path: Path) -> None:
    wechat = FakeGateway(Channel.WECHAT)
    alipay = FakeGateway(Channel.ALIPAY)
    client = new_client(tmp_path, wechat, alipay)
    created = create_order(client, channel="alipay")
    order_no = created["out_trade_no"]
    wechat.notification = notification(Channel.WECHAT, "notify-1", out_trade_no=order_no)
    response = client.post("/notify/wechat", content=b"{}")
    assert response.status_code == 400
    assert client.get(f"/orders/{order_no}").json()["status"] == "pending"


def test_alipay_notification_and_duplicate(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.ALIPAY)
    client = new_client(tmp_path, gateway)
    created = create_order(client, channel="alipay")
    order_no = created["out_trade_no"]
    assert created["form_html"] == "<form></form>"
    gateway.notification = notification(Channel.ALIPAY, "notify-1", out_trade_no=order_no)

    first = client.post("/notify/alipay", content=b"a=1")
    assert first.status_code == 200
    assert first.text == "success"
    assert client.get(f"/orders/{order_no}").json()["status"] == "paid"

    second = client.post("/notify/alipay", content=b"a=1")
    assert second.text == "success"
    assert notification_count(tmp_path / "demo.db") == 1


def test_alipay_notification_signature_error(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.ALIPAY, parse_error=PaymentSignatureError("bad"))
    client = new_client(tmp_path, gateway)
    created = create_order(client, channel="alipay")
    response = client.post("/notify/alipay", content=b"a=1")
    assert response.status_code == 400
    assert response.text == "failure"
    assert client.get(f"/orders/{created['out_trade_no']}").json()["status"] == "pending"


def test_sync_compensation_marks_paid(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.query_result = QueryPaymentResult(
        channel=Channel.WECHAT,
        out_trade_no=order_no,
        amount_fen=100,
        status=PaymentStatus.PAID,
        transaction_id="TX-QUERY",
    )
    response = client.post(f"/orders/{order_no}/sync")
    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert response.json()["transaction_id"] == "TX-QUERY"
    assert response.json()["fulfilled_at"] is not None

    second = client.post(f"/orders/{order_no}/sync")
    assert second.json()["fulfilled_at"] == response.json()["fulfilled_at"]


def test_sync_amount_mismatch(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    gateway.query_result = QueryPaymentResult(
        channel=Channel.WECHAT,
        out_trade_no=created["out_trade_no"],
        amount_fen=99,
        status=PaymentStatus.PAID,
    )
    response = client.post(f"/orders/{created['out_trade_no']}/sync")
    assert response.status_code == 409


def test_sync_not_found_leaves_order_unchanged(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT, query_error=PaymentNotFoundError("missing"))
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    response = client.post(f"/orders/{created['out_trade_no']}/sync")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_sync_network_error(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    gateway.query_error = PaymentNetworkError("timeout")
    response = client.post(f"/orders/{created['out_trade_no']}/sync")
    assert response.status_code == 502
    assert response.json()["detail"]["may_have_succeeded"] is True


def test_closed_notification(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    gateway.notification = notification(
        Channel.WECHAT,
        "notify-1",
        out_trade_no=created["out_trade_no"],
        event_type=NotificationEvent.PAYMENT_CLOSED,
        status=PaymentStatus.CLOSED,
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    assert client.get(f"/orders/{created['out_trade_no']}").json()["status"] == "closed"


def test_refund_notifications_update_refunded_amount(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.notification = notification(Channel.WECHAT, "notify-1", out_trade_no=order_no)
    assert client.post("/notify/wechat", content=b"{}").status_code == 200

    gateway.notification = notification(
        Channel.WECHAT,
        "refund-1",
        out_trade_no=order_no,
        event_type=NotificationEvent.REFUND_SUCCEEDED,
        refund_amount_fen=40,
        out_refund_no="REF-1",
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    partial = client.get(f"/orders/{order_no}").json()
    assert partial["refunded_amount_fen"] == 40
    assert partial["status"] == "paid"

    gateway.notification = notification(
        Channel.WECHAT,
        "refund-2",
        out_trade_no=order_no,
        event_type=NotificationEvent.REFUND_SUCCEEDED,
        refund_amount_fen=60,
        out_refund_no="REF-2",
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    full = client.get(f"/orders/{order_no}").json()
    assert full["refunded_amount_fen"] == 100
    assert full["status"] == "refunded"


def test_unconfigured_channel_returns_503(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    response = client.post(
        "/orders", json={"amount_fen": 100, "description": "test", "channel": "alipay"}
    )
    assert response.status_code == 503


def test_get_unknown_order_returns_404(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    assert client.get("/orders/MISSING").status_code == 404


def test_health_and_return_page(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "channels": ["wechat"], "demo": False}
    page = client.get("/return/alipay")
    assert page.status_code == 200
    assert "异步通知" in page.text


def test_empty_gateways_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(PaymentConfigError):
        create_app({}, db_path=tmp_path / "demo.db")


def test_pending_orders(tmp_path: Path) -> None:
    database = Database(tmp_path / "demo.db")
    database.create_order("ORDER1", "wechat", 100, "test")
    assert [order.out_trade_no for order in database.pending_orders()] == ["ORDER1"]


def test_create_order_rejects_whitespace_order_number_before_insert(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    response = client.post(
        "/orders",
        json={
            "amount_fen": 100,
            "description": "test",
            "channel": "wechat",
            "out_trade_no": " ORDER1 ",
        },
    )
    assert response.status_code == 400
    assert client.get("/orders/%20ORDER1%20").status_code == 404


def test_create_order_validation_failure_marks_order_failed(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT, create_error=PaymentValidationError("bad"))
    client = new_client(tmp_path, gateway)
    response = client.post(
        "/orders",
        json={
            "amount_fen": 100,
            "description": "test",
            "channel": "wechat",
            "out_trade_no": "ORDER1",
        },
    )
    assert response.status_code == 400
    assert client.get("/orders/ORDER1").json()["status"] == "failed"


def test_unknown_channel_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from examples.fastapi_app import gateways_from_env

    monkeypatch.setenv("PYPAY_CHANNELS", "stripe")
    with pytest.raises(PaymentConfigError):
        gateways_from_env()


def test_partial_refund_before_payment_is_not_lost(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]

    gateway.notification = notification(
        Channel.WECHAT,
        "refund-first",
        out_trade_no=order_no,
        event_type=NotificationEvent.REFUND_SUCCEEDED,
        refund_amount_fen=40,
        out_refund_no="REF-1",
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    pending = client.get(f"/orders/{order_no}").json()
    assert pending["status"] == "pending"
    assert pending["refunded_amount_fen"] == 40

    gateway.notification = notification(
        Channel.WECHAT, "payment-after-refund", out_trade_no=order_no
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    paid = client.get(f"/orders/{order_no}").json()
    assert paid["status"] == "paid"
    assert paid["refunded_amount_fen"] == 40
    assert paid["fulfilled_at"] is not None


def test_full_refund_before_payment_does_not_fulfill(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]

    gateway.notification = notification(
        Channel.WECHAT,
        "refund-first",
        out_trade_no=order_no,
        event_type=NotificationEvent.REFUND_SUCCEEDED,
        refund_amount_fen=100,
        out_refund_no="REF-1",
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    refunded = client.get(f"/orders/{order_no}").json()
    assert refunded["status"] == "refunded"
    assert refunded["refunded_amount_fen"] == 100
    assert refunded["fulfilled_at"] is None

    gateway.notification = notification(
        Channel.WECHAT, "payment-after-refund", out_trade_no=order_no
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    after = client.get(f"/orders/{order_no}").json()
    assert after["status"] == "refunded"
    assert after["transaction_id"] == "TX-payment-after-refund"
    assert after["fulfilled_at"] is None


def test_same_refund_with_different_notification_id_is_deduped(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.notification = notification(Channel.WECHAT, "payment", out_trade_no=order_no)
    assert client.post("/notify/wechat", content=b"{}").status_code == 200

    for notification_id in ("refund-a", "refund-b"):
        gateway.notification = notification(
            Channel.WECHAT,
            notification_id,
            out_trade_no=order_no,
            event_type=NotificationEvent.REFUND_SUCCEEDED,
            refund_amount_fen=40,
            out_refund_no="REF-1",
        )
        assert client.post("/notify/wechat", content=b"{}").status_code == 200

    order = client.get(f"/orders/{order_no}").json()
    assert order["refunded_amount_fen"] == 40


def test_refund_query_does_not_invent_a_full_refund(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.notification = notification(Channel.WECHAT, "payment", out_trade_no=order_no)
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    gateway.query_result = QueryPaymentResult(
        channel=Channel.WECHAT,
        out_trade_no=order_no,
        amount_fen=100,
        status=PaymentStatus.REFUNDED,
    )
    response = client.post(f"/orders/{order_no}/sync")
    assert response.status_code == 200
    assert response.json()["status"] == "refunded"
    assert response.json()["refunded_amount_fen"] == 0


@pytest.mark.parametrize("path", ["/notify/wechat", "/notify/alipay"])
def test_notification_body_size_is_limited(tmp_path: Path, path: str) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT), FakeGateway(Channel.ALIPAY))
    response = client.post(path, content=b"x" * (256 * 1024 + 1))
    assert response.status_code == 413


def test_payment_failed_notification(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    gateway.notification = notification(
        Channel.WECHAT,
        "notify-1",
        out_trade_no=created["out_trade_no"],
        event_type=NotificationEvent.PAYMENT_FAILED,
        status=PaymentStatus.FAILED,
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    assert client.get(f"/orders/{created['out_trade_no']}").json()["status"] == "failed"


def test_alipay_notification_unknown_order(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.ALIPAY)
    client = new_client(tmp_path, gateway)
    gateway.notification = notification(Channel.ALIPAY, "notify-1", out_trade_no="MISSING")
    response = client.post("/notify/alipay", content=b"a=1")
    assert response.status_code == 404
    assert response.text == "failure"


def test_wechat_refund_amount_is_clamped_to_order_total(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.WECHAT)
    client = new_client(tmp_path, gateway)
    created = create_order(client)
    order_no = created["out_trade_no"]
    gateway.notification = notification(Channel.WECHAT, "payment", out_trade_no=order_no)
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    gateway.notification = notification(
        Channel.WECHAT,
        "refund-over",
        out_trade_no=order_no,
        event_type=NotificationEvent.REFUND_SUCCEEDED,
        refund_amount_fen=200,
        out_refund_no="REF-OVER",
    )
    assert client.post("/notify/wechat", content=b"{}").status_code == 200
    order = client.get(f"/orders/{order_no}").json()
    assert order["refunded_amount_fen"] == 100
    assert order["status"] == "refunded"


def test_alipay_cumulative_refund_is_not_added_twice(tmp_path: Path) -> None:
    gateway = FakeGateway(Channel.ALIPAY)
    client = new_client(tmp_path, gateway)
    created = create_order(client, channel="alipay")
    order_no = created["out_trade_no"]
    gateway.notification = notification(Channel.ALIPAY, "payment", out_trade_no=order_no)
    assert client.post("/notify/alipay", content=b"a=1").status_code == 200

    for notification_id, refunded in (("refund-a", 40), ("refund-b", 60)):
        gateway.notification = notification(
            Channel.ALIPAY,
            notification_id,
            out_trade_no=order_no,
            event_type=NotificationEvent.REFUND_SUCCEEDED,
            refund_amount_fen=refunded,
        )
        assert client.post("/notify/alipay", content=b"a=1").status_code == 200

    order = client.get(f"/orders/{order_no}").json()
    assert order["refunded_amount_fen"] == 60
    assert order["status"] == "paid"


def test_frontend_page_and_qr(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    page = client.get("/")
    assert page.status_code == 200
    assert "pypay" in page.text
    assert "创建一笔测试支付" in page.text
    assert "/qr.svg" in page.text

    qr = client.get("/qr.svg", params={"data": "weixin://wxpay/bizpayurl?pr=test"})
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in qr.text
    assert 'xmlns="http://www.w3.org/2000/svg"' in qr.text

    too_long = client.get("/qr.svg", params={"data": "x" * 2049})
    assert too_long.status_code == 400


def test_demo_mode_end_to_end(tmp_path: Path) -> None:
    app = create_app(demo_mode=True, db_path=tmp_path / "demo.db")
    client = TestClient(app)

    health = client.get("/health").json()
    assert health == {"status": "ok", "channels": ["alipay", "wechat"], "demo": True}

    created = client.post(
        "/orders",
        json={"amount_fen": 100, "description": "demo wechat", "channel": "wechat"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["qr_code_url"].startswith("weixin://")

    paid = client.post(f"/demo/pay/{body['out_trade_no']}")
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"
    assert paid.json()["transaction_id"].startswith("DEMO-")
    assert paid.json()["fulfilled_at"] is not None

    alipay = client.post(
        "/orders",
        json={"amount_fen": 250, "description": "demo alipay", "channel": "alipay"},
    )
    assert alipay.status_code == 201
    assert "DEMO" in alipay.json()["form_html"]


def test_demo_pay_is_disabled_in_real_mode(tmp_path: Path) -> None:
    client = new_client(tmp_path, FakeGateway(Channel.WECHAT))
    response = client.post("/demo/pay/ORDER1")
    assert response.status_code == 404


def test_demo_mode_can_be_enabled_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYPAY_DEMO_MODE", "1")
    app = create_app(db_path=tmp_path / "demo.db")
    client = TestClient(app)
    assert client.get("/health").json()["demo"] is True
