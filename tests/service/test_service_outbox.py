from __future__ import annotations

from datetime import timedelta

import pytest

from pypay_service.notifier import DeliveryResult
from pypay_service.timeutil import from_db, to_db
from tests.service.helpers import (
    attempt_out_trade_no,
    build_env,
    cashier_token,
    create_payment,
    notify,
    start_attempt,
)


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def paid_order(env, *, project: str = "project-a", business_order_no: str = "A-OUTBOX"):
    client = env.clients["anon"]
    auth = env.auth(project)
    created = create_payment(client, auth, business_order_no=business_order_no, amount_fen=100)
    token = cashier_token(created.json()["cashier_url"])
    start_attempt(env, client, token, "wechat")
    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    response = notify(
        client,
        "wechat",
        "wechat-default",
        out_trade_no=out_trade_no,
        transaction_id="TX-OUTBOX",
    )
    assert response.status_code == 200
    return client, auth, created.json(), out_trade_no


def only_event(env):
    with env.db.read() as conn:
        return conn.execute("SELECT * FROM outbox_events").fetchone()


def deliveries(env, event_id: str):
    with env.db.read() as conn:
        return conn.execute(
            "SELECT * FROM outbox_deliveries WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()


def test_event_is_sent_only_to_the_owning_project(env) -> None:
    paid_order(env)
    report = env.run_outbox()
    assert report.delivered == 1
    assert len(env.notifier.calls) == 1
    call = env.notifier.calls[0]
    assert call["notify_url"] == "https://business-a.test/pypay/notify"
    assert call["notify_secret"] == env.projects["project-a"].notify_secret
    payload = call["payload"]
    assert payload["project_id"] == "project-a"
    assert payload["business_order_no"] == "A-OUTBOX"
    assert payload["event_type"] == "payment.succeeded"
    assert payload["amount_fen"] == 100
    assert payload["currency"] == "CNY"
    assert payload["channel"] == "wechat"
    assert payload["occurred_at"]
    assert payload["event_id"] == call["event_id"]

    event = only_event(env)
    assert event["status"] == "delivered"
    assert event["ack_status"] == "acked"


def test_retry_backoff_and_worker_restart(env) -> None:
    paid_order(env)
    env.notifier.queue(DeliveryResult(status_code=500, body_excerpt="server error"))
    env.notifier.queue(DeliveryResult(status_code=None, error="timeout"))
    env.notifier.queue(DeliveryResult(status_code=200, body_excerpt="ok"))

    first = env.run_outbox()
    assert first.retried == 1
    event = only_event(env)
    event_id = event["event_id"]
    assert event["attempts"] == 1
    assert event["status"] == "pending"
    assert from_db(event["next_attempt_at"]) > env.clock()

    env.clock.advance(10)
    second = env.run_outbox()
    assert second.retried == 1
    event = only_event(env)
    assert event["event_id"] == event_id
    assert event["attempts"] == 2

    env.clock.advance(20)
    third = env.run_outbox()
    assert third.delivered == 1
    event = only_event(env)
    assert event["event_id"] == event_id
    assert event["status"] == "delivered"
    assert event["ack_status"] == "acked"
    assert [row["status"] for row in deliveries(env, event_id)] == [
        "retry",
        "retry",
        "delivered",
    ]


def test_stale_sending_lease_is_recovered_after_restart(env) -> None:
    paid_order(env)
    event = only_event(env)
    with env.db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET status = 'sending', lease_until = ? WHERE event_id = ?",
            (to_db(env.clock.now - timedelta(seconds=1)), event["event_id"]),
        )
    report = env.run_outbox()
    assert report.delivered == 1
    assert only_event(env)["status"] == "delivered"


def test_client_4xx_is_dead_and_manual_resend_recovers(env) -> None:
    _client, _auth, _order, _otn = paid_order(env)
    env.notifier.queue(DeliveryResult(status_code=400, body_excerpt="invalid signature"))
    first = env.run_outbox()
    assert first.dead == 1
    event = only_event(env)
    event_id = event["event_id"]
    assert event["status"] == "dead"
    assert event["ack_status"] == "rejected"
    assert deliveries(env, event_id)[0]["status"] == "dead"

    with env.db.transaction() as conn:
        from pypay_service.repo_outbox import manual_resend

        manual_resend(conn, event_id, env.clock())
    env.notifier.queue(DeliveryResult(status_code=200))
    second = env.run_outbox()
    assert second.delivered == 1
    event = only_event(env)
    assert event["event_id"] == event_id
    assert event["manual_resends"] == 1
    assert event["ack_status"] == "acked"


def test_retry_exhaustion_marks_dead_without_business_ack(env) -> None:
    paid_order(env)
    for _ in range(3):
        env.notifier.queue(DeliveryResult(status_code=503, body_excerpt="unavailable"))
    for delay in (0, 10, 20):
        env.clock.advance(delay)
        env.run_outbox()
    event = only_event(env)
    assert event["status"] == "dead"
    assert event["ack_status"] == "pending"
    assert event["attempts"] == 3
    assert len(deliveries(env, event["event_id"])) == 3


def test_disabled_project_still_drains_existing_events(env) -> None:
    from pypay_service.projects_service import set_project_status

    paid_order(env)
    set_project_status(env.db, "project-a", False, now=env.clock())
    report = env.run_outbox()
    assert report.delivered == 1


def test_event_id_is_stable_across_retries(env) -> None:
    paid_order(env)
    env.notifier.queue(DeliveryResult(status_code=500))
    env.run_outbox()
    first_id = only_event(env)["event_id"]
    env.clock.advance(10)
    env.notifier.queue(DeliveryResult(status_code=200))
    env.run_outbox()
    assert only_event(env)["event_id"] == first_id
    assert env.notifier.calls[0]["event_id"] == env.notifier.calls[-1]["event_id"]
