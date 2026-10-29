from __future__ import annotations

import asyncio

import pytest

from pypay import Channel, PaymentNetworkError, PaymentStatus, QueryPaymentResult
from pypay_service.gateways import GatewayRegistry
from pypay_service.worker import MaintenanceWorker, PaymentWorker
from tests.service.helpers import (
    attempt_out_trade_no,
    build_env,
    cashier_token,
    create_payment,
    payment_response,
    start_attempt,
)


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def event_types(env) -> list[str]:
    with env.db.read() as conn:
        rows = conn.execute("SELECT event_type FROM outbox_events ORDER BY created_at").fetchall()
    return [str(row["event_type"]) for row in rows]


def test_payment_create_timeout_then_query_confirms(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    env.gateway("wechat-default").create_error = PaymentNetworkError("timeout")
    created = create_payment(client, auth, business_order_no="A-TIMEOUT")
    token = cashier_token(created.json()["cashier_url"])
    response = start_attempt(env, client, token, "wechat")
    assert response.status_code == 303
    page = client.get(f"/pay/{token}")
    assert "支付信息尚未就绪" in page.text
    with env.db.read() as conn:
        attempt = conn.execute(
            "SELECT out_trade_no, channel_status, status FROM payment_attempts"
        ).fetchone()
    assert attempt["status"] == "pending"
    assert attempt["channel_status"] == "create_unknown"

    env.gateway("wechat-default").query_result = QueryPaymentResult(
        channel=Channel.WECHAT,
        out_trade_no=attempt["out_trade_no"],
        amount_fen=100,
        status=PaymentStatus.PAID,
        transaction_id="TX-QUERY",
    )
    env.clock.advance(31)
    registry = GatewayRegistry(env.config, factories=env.gateways)
    report = asyncio.run(
        MaintenanceWorker(env.db, env.config, registry, clock=env.clock).run_once(now=env.clock())
    )
    assert report.queried == 1
    payment = payment_response(client, auth, created.json()["payment_id"])
    assert payment["status"] == "paid"
    assert payment["attempts"][0]["transaction_id"] == "TX-QUERY"
    assert event_types(env) == ["payment.succeeded"]


def test_payment_worker_delivers_after_reconciliation(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-WORKER")
    token = cashier_token(created.json()["cashier_url"])
    start_attempt(env, client, token, "wechat")
    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    from tests.service.helpers import notify

    notify(client, "wechat", "wechat-default", out_trade_no=out_trade_no)
    registry = GatewayRegistry(env.config, factories=env.gateways)
    worker = PaymentWorker(
        env.db,
        env.config,
        env.box,
        env.notifier,
        registry,
        clock=env.clock,
    )
    outbox_report, _maintenance_report = asyncio.run(worker.run_once(now=env.clock()))
    assert outbox_report.delivered == 1
    assert env.notifier.calls
    assert (
        payment_response(client, auth, created.json()["payment_id"])["service_ack_status"]
        == "acked"
    )


def test_expiry_sweep_enqueues_one_event(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-SWEEP", expires_in_seconds=60)
    env.clock.advance(61)
    registry = GatewayRegistry(env.config, factories=env.gateways)
    worker = MaintenanceWorker(env.db, env.config, registry, clock=env.clock)
    first = asyncio.run(worker.run_once(now=env.clock()))
    second = asyncio.run(worker.run_once(now=env.clock()))
    assert first.expired == 1
    assert second.expired == 0
    assert payment_response(client, auth, created.json()["payment_id"])["status"] == "expired"
    assert event_types(env).count("payment.expired") == 1


def test_close_pending_order_without_attempts(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-CLOSE-NOATTEMPT")
    payment_id = created.json()["payment_id"]
    from tests.service.helpers import post_json

    response = post_json(client, auth, f"/v1/payments/{payment_id}/close", {})
    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    assert event_types(env) == ["payment.closed"]


def test_close_paid_order_is_rejected(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-CLOSE-PAID")
    token = cashier_token(created.json()["cashier_url"])
    start_attempt(env, client, token, "wechat")
    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    from tests.service.helpers import notify

    notify(client, "wechat", "wechat-default", out_trade_no=out_trade_no)
    from tests.service.helpers import post_json

    response = post_json(client, auth, f"/v1/payments/{created.json()['payment_id']}/close", {})
    assert response.status_code == 409
