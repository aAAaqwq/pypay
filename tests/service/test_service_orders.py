from __future__ import annotations

import pytest

from tests.service.helpers import (
    attempt_out_trade_no,
    build_env,
    cashier_path,
    cashier_token,
    create_payment,
    get_json,
    notify,
    payment_response,
    post_json,
    start_attempt,
)


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def test_two_projects_create_orders_and_render_their_own_cashier(env) -> None:
    client = env.clients["anon"]
    a = create_payment(
        client, env.auth("project-a"), business_order_no="A-1", subject="A 套餐", amount_fen=100
    )
    assert a.status_code == 201, a.text
    b = create_payment(
        client, env.auth("project-b"), business_order_no="B-1", subject="B 数字人", amount_fen=250
    )
    assert b.status_code == 201, b.text
    assert a.json()["payment_id"] != b.json()["payment_id"]
    assert a.json()["cashier_url"] != b.json()["cashier_url"]

    page_a = client.get(cashier_path(a.json()["cashier_url"]))
    assert page_a.status_code == 200
    assert "Project A" in page_a.text
    assert "A 套餐" in page_a.text
    assert "¥1.00" in page_a.text
    assert "B 数字人" not in page_a.text

    page_b = client.get(cashier_path(b.json()["cashier_url"]))
    assert page_b.status_code == 200
    assert "Project B" in page_b.text
    assert "B 数字人" in page_b.text
    assert "¥2.50" in page_b.text
    assert "A 套餐" not in page_b.text

    token = cashier_token(a.json()["cashier_url"])
    assert "100" not in token
    assert "project-a" not in token


def test_duplicate_order_is_idempotent_and_conflicts_are_rejected(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    first = create_payment(client, auth, business_order_no="A-1", amount_fen=100)
    assert first.status_code == 201
    second = create_payment(client, auth, business_order_no="A-1", amount_fen=100)
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert second.json()["cashier_url"] == first.json()["cashier_url"]

    amount_conflict = create_payment(client, auth, business_order_no="A-1", amount_fen=101)
    assert amount_conflict.status_code == 409
    subject_conflict = create_payment(
        client, auth, business_order_no="A-1", amount_fen=100, subject="另一个商品"
    )
    assert subject_conflict.status_code == 409
    return_conflict = create_payment(
        client,
        auth,
        business_order_no="A-1",
        amount_fen=100,
        return_url="https://business-a.test/return",
    )
    assert return_conflict.status_code == 409
    metadata_conflict = create_payment(
        client,
        auth,
        business_order_no="A-1",
        amount_fen=100,
        metadata={"plan": "pro"},
    )
    assert metadata_conflict.status_code == 409


def test_return_url_must_be_registered(env) -> None:
    response = create_payment(
        env.clients["anon"],
        env.auth("project-a"),
        business_order_no="A-URL",
        return_url="https://evil.test/redirect",
    )
    assert response.status_code == 400


def test_cashier_rejects_unknown_token_and_bad_csrf(env) -> None:
    client = env.clients["anon"]
    assert client.get("/pay/not-a-real-token").status_code == 404
    created = create_payment(client, env.auth("project-a"), business_order_no="A-CSRF")
    token = cashier_token(created.json()["cashier_url"])
    response = client.post(
        f"/pay/{token}/start", data={"channel": "wechat"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_order_expiry_blocks_payment_and_emits_event(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-EXP", expires_in_seconds=60)
    assert created.status_code == 201
    token = cashier_token(created.json()["cashier_url"])
    env.clock.advance(61)
    page = client.get(cashier_path(created.json()["cashier_url"]))
    assert page.status_code == 200
    assert "订单已过期" in page.text
    start = start_attempt(env, client, token, "wechat")
    assert start.status_code == 303
    assert "expired" in client.get(cashier_path(created.json()["cashier_url"])).text

    env.run_outbox()
    event_types = [call["event_type"] for call in env.notifier.calls]
    assert "payment.expired" in event_types
    expired_call = next(c for c in env.notifier.calls if c["event_type"] == "payment.expired")
    assert expired_call["notify_url"] == "https://business-a.test/pypay/notify"
    assert expired_call["payload"]["project_id"] == "project-a"


def test_cross_project_queries_and_refunds_are_rejected(env) -> None:
    client = env.clients["anon"]
    created = create_payment(client, env.auth("project-a"), business_order_no="A-X")
    payment_id = created.json()["payment_id"]
    assert get_json(client, env.auth("project-b"), f"/v1/payments/{payment_id}").status_code == 404
    assert (
        post_json(
            client,
            env.auth("project-b"),
            "/v1/refunds",
            {"payment_id": payment_id, "amount_fen": 100},
        ).status_code
        == 404
    )


def test_disabled_project_cannot_call_the_api(env) -> None:
    from pypay_service.projects_service import set_project_status

    set_project_status(env.db, "project-a", False, now=env.clock())
    response = create_payment(env.clients["anon"], env.auth("project-a"), business_order_no="A-DIS")
    assert response.status_code == 401
    set_project_status(env.db, "project-a", True, now=env.clock())


def test_channel_disabled_for_project_is_rejected(env) -> None:
    client = env.clients["anon"]
    created = create_payment(client, env.auth("project-b"), business_order_no="B-CH")
    token = cashier_token(created.json()["cashier_url"])
    response = start_attempt(env, client, token, "alipay")
    assert response.status_code == 403
    assert "not enabled" in response.text


def test_wechat_and_alipay_attempts_are_both_reachable(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-BOTH")
    token = cashier_token(created.json()["cashier_url"])
    assert start_attempt(env, client, token, "wechat").status_code == 303
    page = client.get(f"/pay/{token}")
    assert "微信扫码支付" in page.text

    assert start_attempt(env, client, token, "alipay").status_code == 303
    page = client.get(f"/pay/{token}")
    assert "前往支付宝" in page.text
    assert "alipay-form?attempt=" in page.text
    form_url = page.text.split('href="/pay/' + token)[1].split('"')[0]
    form = client.get("/pay/" + token + form_url.replace("&amp;", "&"))
    assert form.status_code == 200
    assert "openapi.alipay.test" in form.text

    assert len(payment_response(client, auth, created.json()["payment_id"])["attempts"]) == 2


def test_start_twice_reuses_the_same_attempt(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-REUSE")
    token = cashier_token(created.json()["cashier_url"])
    first = start_attempt(env, client, token, "wechat")
    second = start_attempt(env, client, token, "wechat")
    assert first.status_code == 303
    assert second.status_code == 303
    attempts = payment_response(client, auth, created.json()["payment_id"])["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["channel"] == "wechat"


def test_cashier_never_accepts_an_amount_parameter(env) -> None:
    client = env.clients["anon"]
    created = create_payment(
        client, env.auth("project-a"), business_order_no="A-AMT", amount_fen=100
    )
    token = cashier_token(created.json()["cashier_url"])
    page = client.get(f"/pay/{token}?amount_fen=1&project_id=project-b")
    assert page.status_code == 200
    assert "¥1.00" in page.text
    assert "Project B" not in page.text


def test_paid_cashier_does_not_keep_a_live_countdown(env) -> None:
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no="A-PAID-COUNTDOWN")
    token = cashier_token(created.json()["cashier_url"])
    assert start_attempt(env, client, token, "wechat").status_code == 303
    otn = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    response = notify(client, "wechat", "wechat-default", out_trade_no=otn, amount_fen=100)
    assert response.status_code == 200
    page = client.get(f"/pay/{token}")
    assert "支付成功" in page.text
    assert 'id="countdown"' not in page.text
    assert 'data-expires="0"' in page.text


def test_notify_helpers_are_configured_for_the_right_project(env) -> None:
    client = env.clients["anon"]
    created = create_payment(client, env.auth("project-a"), business_order_no="A-N")
    token = cashier_token(created.json()["cashier_url"])
    start_attempt(env, client, token, "wechat")
    otn = payment_response(client, env.auth("project-a"), created.json()["payment_id"])["attempts"][
        0
    ]["payment_order_no"]
    response = notify(client, "wechat", "wechat-default", out_trade_no=otn, amount_fen=100)
    assert response.status_code == 200
    env.run_outbox()
    assert env.notifier.calls[-1]["notify_url"] == "https://business-a.test/pypay/notify"
