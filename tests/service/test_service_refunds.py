from __future__ import annotations

import pytest

from pypay import (
    Channel,
    PaymentNetworkError,
    PaymentProviderError,
    QueryRefundResult,
    RefundResult,
    RefundStatus,
)
from tests.service.helpers import (
    attempt_out_trade_no,
    build_env,
    cashier_token,
    create_payment,
    get_json,
    notify,
    payment_response,
    post_json,
    signed_headers,
    start_attempt,
)


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def paid_order(
    env,
    *,
    amount_fen: int = 100,
    project: str = "project-a",
    business_order_no: str = "A-REF",
    channel: str = "wechat",
):
    client = env.clients["anon"]
    auth = env.auth(project)
    created = create_payment(
        client, auth, business_order_no=business_order_no, amount_fen=amount_fen
    )
    token = cashier_token(created.json()["cashier_url"])
    start_attempt(env, client, token, channel)
    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    response = notify(
        client,
        channel,
        f"{channel}-default",
        out_trade_no=out_trade_no,
        amount_fen=amount_fen,
    )
    assert response.status_code == 200
    return client, auth, created.json(), out_trade_no


def refund_payload(payment_id: str, amount_fen: int = 40, **overrides):
    payload = {"payment_id": payment_id, "amount_fen": amount_fen}
    payload.update(overrides)
    return payload


def events(env) -> list[str]:
    with env.db.read() as conn:
        rows = conn.execute("SELECT event_type FROM outbox_events ORDER BY created_at").fetchall()
    return [str(row["event_type"]) for row in rows]


def test_refund_success_updates_order_and_enqueues_event(env) -> None:
    client, auth, order, _otn = paid_order(env)
    response = post_json(
        client,
        auth,
        "/v1/refunds",
        refund_payload(order["payment_id"], 40, reason="用户申请", idempotency_key="refund-1"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["amount_fen"] == 40
    assert body["created"]
    assert body["ambiguous"] is False

    payment = payment_response(client, auth, order["payment_id"])
    assert payment["status"] == "paid"
    assert payment["refunded_amount_fen"] == 40
    assert "refund.succeeded" in events(env)
    env.run_outbox()
    refund_event = next(c for c in env.notifier.calls if c["event_type"] == "refund.succeeded")
    assert refund_event["payload"]["refund_amount_fen"] == 40
    assert refund_event["payload"]["refunded_total_fen"] == 40


def test_refund_idempotency_key_replays_the_same_refund(env) -> None:
    client, auth, order, _otn = paid_order(env)
    first = post_json(
        client,
        auth,
        "/v1/refunds",
        refund_payload(order["payment_id"], 40, idempotency_key="stable-key"),
    )
    assert first.status_code == 201
    second = post_json(
        client,
        auth,
        "/v1/refunds",
        refund_payload(order["payment_id"], 40, idempotency_key="stable-key"),
    )
    assert second.status_code == 200
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert second.json()["refund_id"] == first.json()["refund_id"]
    with env.db.read() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()["n"]
    assert count == 1

    conflict = post_json(
        client,
        auth,
        "/v1/refunds",
        refund_payload(order["payment_id"], 41, idempotency_key="stable-key"),
    )
    assert conflict.status_code == 409


def test_refund_idempotency_key_header_is_supported(env) -> None:
    client, auth, order, _otn = paid_order(env)
    payload = refund_payload(order["payment_id"], 40)
    from tests.service.helpers import json_body

    body = json_body(payload)
    headers = signed_headers(auth, "POST", "/v1/refunds", body)
    headers["Content-Type"] = "application/json"
    headers["Idempotency-Key"] = "header-key"
    response = client.post("/v1/refunds", content=body, headers=headers)
    assert response.status_code == 201
    replay_headers = signed_headers(auth, "POST", "/v1/refunds", body)
    replay_headers["Content-Type"] = "application/json"
    replay_headers["Idempotency-Key"] = "header-key"
    replay = client.post("/v1/refunds", content=body, headers=replay_headers)
    assert replay.status_code == 200
    assert replay.json()["refund_id"] == response.json()["refund_id"]


def test_refund_requires_a_paid_order(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-UNPAID")
    response = post_json(
        client,
        auth,
        "/v1/refunds",
        refund_payload(created.json()["payment_id"], 40),
    )
    assert response.status_code == 409


def test_refund_exceeding_remaining_amount_is_rejected(env) -> None:
    client, auth, order, _otn = paid_order(env)
    first = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 60))
    assert first.status_code == 201
    second = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 50))
    assert second.status_code == 409


def test_pending_refund_reserves_the_remaining_amount(env) -> None:
    client, auth, order, out_trade_no = paid_order(env)
    env.gateway("wechat-default").refund_result = RefundResult(
        channel=Channel.WECHAT,
        out_refund_no="pending-refund",
        out_trade_no=out_trade_no,
        amount_fen=60,
        total_amount_fen=100,
        status=RefundStatus.PENDING,
    )
    first = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 60))
    assert first.status_code == 202
    assert first.json()["ambiguous"] is True
    second = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 50))
    assert second.status_code == 409


def test_full_refund_marks_order_refunded(env) -> None:
    client, auth, order, _otn = paid_order(env)
    response = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 100))
    assert response.status_code == 201
    payment = payment_response(client, auth, order["payment_id"])
    assert payment["status"] == "refunded"
    assert payment["refunded_amount_fen"] == 100


def test_refund_timeout_then_query_confirms(env) -> None:
    client, auth, order, out_trade_no = paid_order(env)
    env.gateway("wechat-default").refund_error = PaymentNetworkError("timeout")
    response = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 100))
    assert response.status_code == 202, response.text
    refund = response.json()
    assert refund["status"] == "pending"

    env.gateway("wechat-default").query_refund_result = QueryRefundResult(
        channel=Channel.WECHAT,
        out_refund_no=refund["out_refund_no"],
        out_trade_no=out_trade_no,
        amount_fen=100,
        total_amount_fen=100,
        status=RefundStatus.SUCCEEDED,
        refund_id="wx-refund-1",
    )
    refreshed = post_json(client, auth, f"/v1/refunds/{refund['refund_id']}/refresh", {})
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "succeeded"
    payment = payment_response(client, auth, order["payment_id"])
    assert payment["status"] == "refunded"
    assert "refund.succeeded" in events(env)


def test_definite_provider_error_marks_refund_failed(env) -> None:
    client, auth, order, _otn = paid_order(env)
    env.gateway("wechat-default").refund_error = PaymentProviderError(
        "bad request", http_status=400
    )
    response = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 100))
    assert response.status_code == 502
    with env.db.read() as conn:
        row = conn.execute("SELECT status FROM refunds").fetchone()
    assert row["status"] == "failed"
    assert "refund.failed" in events(env)
    assert payment_response(client, auth, order["payment_id"])["status"] == "paid"


def test_refund_query_cross_project_is_rejected(env) -> None:
    client, auth, order, _otn = paid_order(env)
    refund = post_json(client, auth, "/v1/refunds", refund_payload(order["payment_id"], 40))
    refund_id = refund.json()["refund_id"]
    assert get_json(client, env.auth("project-b"), f"/v1/refunds/{refund_id}").status_code == 404
