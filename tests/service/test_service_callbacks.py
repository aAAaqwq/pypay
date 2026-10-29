from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from pypay import (
    Channel,
    NotificationEvent,
    PaymentNotification,
    PaymentSignatureError,
    PaymentStatus,
)
from pypay_service.callbacks_service import apply_channel_notification
from tests.service.helpers import (
    attempt_out_trade_no,
    build_env,
    cashier_token,
    create_payment,
    notify,
    payment_response,
    start_attempt,
)


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def started(
    env,
    *,
    project: str = "project-a",
    channel: str = "wechat",
    amount_fen: int = 100,
    business_order_no: str = "A-PAY",
):
    client = env.clients["anon"]
    auth = env.auth(project)
    created = create_payment(
        client, auth, business_order_no=business_order_no, amount_fen=amount_fen
    )
    assert created.status_code == 201, created.text
    token = cashier_token(created.json()["cashier_url"])
    start = start_attempt(env, client, token, channel)
    assert start.status_code == 303, start.text
    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    return client, auth, created.json(), token, out_trade_no


def notification_count(env) -> int:
    with env.db.read() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM channel_notifications").fetchone()
    return int(row["n"])


def events(env) -> list[str]:
    with env.db.read() as conn:
        rows = conn.execute("SELECT event_type FROM outbox_events ORDER BY created_at").fetchall()
    return [str(row["event_type"]) for row in rows]


def test_payment_success_is_idempotent_across_notification_ids(env) -> None:
    client, auth, order, _token, otn = started(env)
    first = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        notification_id="notify-1",
        transaction_id="TX-1",
    )
    assert first.status_code == 200
    assert first.json()["code"] == "SUCCESS"
    assert payment_response(client, auth, order["payment_id"])["status"] == "paid"
    assert events(env) == ["payment.succeeded"]

    duplicate = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        notification_id="notify-1",
        transaction_id="TX-1",
    )
    assert duplicate.status_code == 200
    assert notification_count(env) == 1
    assert events(env) == ["payment.succeeded"]

    different_id = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        notification_id="notify-2",
        transaction_id="TX-1",
    )
    assert different_id.status_code == 200
    assert notification_count(env) == 2
    assert events(env) == ["payment.succeeded"]


def test_amount_mismatch_is_rejected_and_rolled_back(env) -> None:
    client, auth, order, _token, otn = started(env)
    response = notify(client, "wechat", "wechat-default", out_trade_no=otn, amount_fen=99)
    assert response.status_code == 400
    assert payment_response(client, auth, order["payment_id"])["status"] == "pending"
    assert notification_count(env) == 0
    assert events(env) == []


def test_signature_error_is_rejected(env) -> None:
    client, auth, order, _token, otn = started(env)
    env.gateway("wechat-default").parse_error = PaymentSignatureError("bad signature")
    response = notify(client, "wechat", "wechat-default", out_trade_no=otn)
    assert response.status_code == 401
    assert payment_response(client, auth, order["payment_id"])["status"] == "pending"
    assert notification_count(env) == 0


def test_unknown_order_is_rejected(env) -> None:
    response = notify(env.clients["anon"], "wechat", "wechat-default", out_trade_no="MISSING")
    assert response.status_code == 404
    assert notification_count(env) == 0


def test_channel_mismatch_is_rejected(env) -> None:
    client, auth, order, _token, otn = started(env, channel="wechat")
    response = notify(client, "alipay", "alipay-default", out_trade_no=otn)
    assert response.status_code == 400
    assert payment_response(client, auth, order["payment_id"])["status"] == "pending"


def test_alipay_notification_returns_plain_success(env) -> None:
    client, auth, order, _token, otn = started(env, channel="alipay", business_order_no="A-ALI")
    response = notify(client, "alipay", "alipay-default", out_trade_no=otn)
    assert response.status_code == 200
    assert response.text == "success"
    assert payment_response(client, auth, order["payment_id"])["status"] == "paid"


def test_closed_and_failed_notifications(env) -> None:
    client, auth, closed_order, _token, closed_otn = started(env, business_order_no="A-CLOSE")
    response = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=closed_otn,
        event_type="payment.closed",
        status="closed",
    )
    assert response.status_code == 200
    assert payment_response(client, auth, closed_order["payment_id"])["status"] == "closed"
    assert "payment.closed" in events(env)

    _client, _auth, failed_order, _token, failed_otn = started(env, business_order_no="A-FAIL")
    response = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=failed_otn,
        event_type="payment.failed",
        status="failed",
    )
    assert response.status_code == 200
    assert payment_response(client, auth, failed_order["payment_id"])["status"] == "failed"
    assert "payment.failed" in events(env)


def test_refund_before_payment_is_not_lost(env) -> None:
    client, auth, order, _token, otn = started(env, business_order_no="A-REFUND-FIRST")
    refund = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        event_type="refund.succeeded",
        status="paid",
        out_refund_no="REF-1",
        refund_amount_fen=40,
    )
    assert refund.status_code == 200
    pending = payment_response(client, auth, order["payment_id"])
    assert pending["status"] == "pending"
    assert pending["refunded_amount_fen"] == 40
    assert events(env) == ["refund.succeeded"]

    payment = notify(client, "wechat", "wechat-default", out_trade_no=otn)
    assert payment.status_code == 200
    paid = payment_response(client, auth, order["payment_id"])
    assert paid["status"] == "paid"
    assert paid["refunded_amount_fen"] == 40
    assert events(env) == ["refund.succeeded", "payment.succeeded"]


def test_full_refund_before_payment_keeps_refunded_status(env) -> None:
    client, auth, order, _token, otn = started(env, business_order_no="A-FULL-FIRST")
    refund = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        event_type="refund.succeeded",
        status="refunded",
        out_refund_no="REF-FULL",
        refund_amount_fen=100,
    )
    assert refund.status_code == 200
    assert payment_response(client, auth, order["payment_id"])["status"] == "refunded"

    payment = notify(client, "wechat", "wechat-default", out_trade_no=otn)
    assert payment.status_code == 200
    after = payment_response(client, auth, order["payment_id"])
    assert after["status"] == "refunded"
    assert after["paid_at"] is not None
    assert after["refunded_amount_fen"] == 100


def test_refund_duplicate_notification_does_not_double_count(env) -> None:
    client, auth, order, _token, otn = started(env, business_order_no="A-REFUND-DUP")
    notify(client, "wechat", "wechat-default", out_trade_no=otn)
    first = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        event_type="refund.succeeded",
        status="paid",
        out_refund_no="REF-DUP",
        refund_amount_fen=40,
        notification_id="refund-a",
    )
    assert first.status_code == 200
    second = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        event_type="refund.succeeded",
        status="paid",
        out_refund_no="REF-DUP",
        refund_amount_fen=40,
        notification_id="refund-b",
    )
    assert second.status_code == 200
    response = payment_response(client, auth, order["payment_id"])
    assert response["refunded_amount_fen"] == 40
    assert events(env).count("refund.succeeded") == 1


def test_refund_above_order_total_is_rejected(env) -> None:
    client, _auth, _order, _token, otn = started(env, business_order_no="A-REFUND-OVER")
    notify(client, "wechat", "wechat-default", out_trade_no=otn)
    response = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=otn,
        event_type="refund.succeeded",
        status="paid",
        out_refund_no="REF-OVER",
        refund_amount_fen=200,
    )
    assert response.status_code == 400
    with env.db.read() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()
    assert int(row["n"]) == 0


def test_concurrent_duplicate_callbacks_create_one_event(env) -> None:
    client, auth, order, _token, otn = started(env, business_order_no="A-CONC-DUP")
    notification = PaymentNotification(
        channel=Channel.WECHAT,
        event_type=NotificationEvent.PAYMENT_SUCCEEDED,
        notification_id="concurrent-same",
        out_trade_no=otn,
        amount_fen=100,
        currency="CNY",
        status=PaymentStatus.PAID,
        transaction_id="TX-C",
    )

    def apply() -> None:
        apply_channel_notification(env.db, notification, now=env.clock())

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: apply(), range(6)))
    assert events(env) == ["payment.succeeded"]
    assert payment_response(client, auth, order["payment_id"])["status"] == "paid"


def test_concurrent_different_notification_ids_still_create_one_event(env) -> None:
    _client, _auth, _order, _token, otn = started(env, business_order_no="A-CONC-IDS")

    def apply(index: int) -> None:
        apply_channel_notification(
            env.db,
            PaymentNotification(
                channel=Channel.WECHAT,
                event_type=NotificationEvent.PAYMENT_SUCCEEDED,
                notification_id=f"concurrent-{index}",
                out_trade_no=otn,
                amount_fen=100,
                currency="CNY",
                status=PaymentStatus.PAID,
                transaction_id="TX-C",
            ),
            now=env.clock(),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(apply, range(6)))
    assert events(env) == ["payment.succeeded"]


def test_cashier_shows_service_pending_then_acked(env) -> None:
    client, _auth, order, token, otn = started(env, business_order_no="A-ACK")
    notify(client, "wechat", "wechat-default", out_trade_no=otn)
    pending_page = client.get(f"/pay/{token}")
    assert "支付成功，服务开通中" in pending_page.text
    env.run_outbox()
    acked_page = client.get(f"/pay/{token}")
    assert "支付成功，服务已确认" in acked_page.text
    assert order["payment_id"]
