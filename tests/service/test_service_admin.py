from __future__ import annotations

import pytest

from pypay_service.notifier import DeliveryResult
from tests.service.helpers import (
    attempt_out_trade_no,
    build_env,
    cashier_token,
    create_payment,
    notify,
    start_attempt,
)

ADMIN = ("admin", "admin-password")
VIEWER = ("viewer", "viewer-password")


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def admin_csrf(client) -> str:
    response = client.get("/admin", auth=ADMIN)
    assert response.status_code == 200, response.text
    return client.cookies["pypay_admin_csrf"]


def paid_order(env, *, business_order_no: str = "A-ADMIN"):
    client = env.clients["anon"]
    auth = env.auth("project-a")
    created = create_payment(client, auth, business_order_no=business_order_no, amount_fen=100)
    token = cashier_token(created.json()["cashier_url"])
    start_attempt(env, client, token, "wechat")
    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    assert notify(client, "wechat", "wechat-default", out_trade_no=out_trade_no).status_code == 200
    return client, auth, created.json(), out_trade_no


def test_admin_requires_basic_auth(env) -> None:
    client = env.clients["admin"]
    response = client.get("/admin")
    assert response.status_code == 401
    assert "Basic" in response.headers["www-authenticate"]


def test_viewer_can_read_but_not_write(env) -> None:
    client = env.clients["admin"]
    page = client.get("/admin", auth=VIEWER)
    assert page.status_code == 200
    assert "总览" in page.text
    csrf = client.cookies["pypay_admin_csrf"]
    response = client.post(
        "/admin/channels",
        data={
            "csrf_token": csrf,
            "account_id": "wechat-extra",
            "channel": "wechat",
            "name": "微信备用",
            "env_prefix": "PYPAY_WECHAT_EXTRA_",
        },
        auth=VIEWER,
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_admin_post_requires_csrf(env) -> None:
    client = env.clients["admin"]
    admin_csrf(client)
    response = client.post(
        "/admin/channels",
        data={
            "account_id": "wechat-no-csrf",
            "channel": "wechat",
            "name": "无 CSRF",
            "env_prefix": "PYPAY_WECHAT_X_",
        },
        auth=ADMIN,
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_admin_can_create_update_rotate_and_disable_a_project(env) -> None:
    client = env.clients["admin"]
    csrf = admin_csrf(client)
    created = client.post(
        "/admin/projects",
        data={
            "csrf_token": csrf,
            "project_id": "project-c",
            "name": "Project C",
            "notify_url": "https://business-c.test/pypay/notify",
            "return_urls": "https://business-c.test/return\nhttps://business-c.test/pay/ok",
            "account_wechat": "wechat-default",
            "account_alipay": "alipay-default",
        },
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert created.status_code == 200, created.text
    assert "接入凭据" in created.text
    assert "PYPAY_API_SECRET=" in created.text
    with env.db.read() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = 'project-c'").fetchone()
    assert row is not None
    old_key = row["api_key_id"]

    updated = client.post(
        "/admin/projects/project-c/settings",
        data={
            "csrf_token": csrf,
            "name": "Project C2",
            "notify_url": "https://business-c.test/pypay/notify2",
            "return_urls": "https://business-c.test/return",
            "account_wechat": "wechat-default",
        },
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with env.db.read() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = 'project-c'").fetchone()
        channels = conn.execute(
            "SELECT channel FROM project_channels WHERE project_id = 'project-c'"
        ).fetchall()
    assert row["name"] == "Project C2"
    assert [c["channel"] for c in channels] == ["wechat"]

    rotated = client.post(
        "/admin/projects/project-c/rotate",
        data={"csrf_token": csrf, "target": "api"},
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert rotated.status_code == 200
    assert "新的 API Secret" in rotated.text
    with env.db.read() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = 'project-c'").fetchone()
    assert row["api_key_id"] != old_key

    disabled = client.post(
        "/admin/projects/project-c/status",
        data={"csrf_token": csrf, "enabled": "0"},
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    with env.db.read() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id = 'project-c'").fetchone()
    assert row["status"] == "disabled"


def test_admin_can_manage_channel_accounts(env) -> None:
    client = env.clients["admin"]
    csrf = admin_csrf(client)
    response = client.post(
        "/admin/channels",
        data={
            "csrf_token": csrf,
            "account_id": "wechat-backup",
            "channel": "wechat",
            "name": "微信备用",
            "env_prefix": "PYPAY_WECHAT_BACKUP_",
        },
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with env.db.read() as conn:
        row = conn.execute("SELECT * FROM channel_accounts WHERE id = 'wechat-backup'").fetchone()
    assert row is not None
    assert row["enabled"] == 1

    toggled = client.post(
        "/admin/channels/wechat-backup/toggle",
        data={"csrf_token": csrf},
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert toggled.status_code == 303
    with env.db.read() as conn:
        row = conn.execute(
            "SELECT enabled FROM channel_accounts WHERE id = 'wechat-backup'"
        ).fetchone()
    assert row["enabled"] == 0


def test_admin_order_pages_and_manual_actions(env) -> None:
    _client, _auth, order, out_trade_no = paid_order(env)
    client = env.clients["admin"]
    csrf = admin_csrf(client)

    listing = client.get("/admin/orders", auth=ADMIN)
    assert listing.status_code == 200
    assert "A-ADMIN" in listing.text

    detail = client.get(f"/admin/orders/{order['payment_id']}", auth=ADMIN)
    assert detail.status_code == 200
    assert out_trade_no in detail.text
    assert "payment.succeeded" in detail.text

    refresh = client.post(
        f"/admin/orders/{order['payment_id']}/refresh",
        data={"csrf_token": csrf},
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert refresh.status_code == 303

    with env.db.read() as conn:
        event_id = conn.execute("SELECT event_id FROM outbox_events").fetchone()["event_id"]
    resend = client.post(
        f"/admin/orders/{order['payment_id']}/resend",
        data={"csrf_token": csrf, "event_id": event_id},
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resend.status_code == 303
    with env.db.read() as conn:
        row = conn.execute(
            "SELECT manual_resends FROM outbox_events WHERE event_id = ?", (event_id,)
        ).fetchone()
    assert row["manual_resends"] == 1

    refund = client.post(
        f"/admin/orders/{order['payment_id']}/refund",
        data={
            "csrf_token": csrf,
            "amount_fen": "40",
            "reason": "管理员退款",
            "idempotency_key": "admin-refund-1",
        },
        auth=ADMIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert refund.status_code == 303
    with env.db.read() as conn:
        refund_row = conn.execute("SELECT * FROM refunds").fetchone()
        audit_row = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'refund.create'"
        ).fetchone()
    assert refund_row["amount_fen"] == 40
    assert refund_row["status"] == "succeeded"
    assert audit_row is not None
    assert audit_row["actor"] == "admin"

    with env.db.read() as conn:
        order_row = conn.execute(
            "SELECT refunded_amount_fen FROM payment_orders WHERE id = ?",
            (order["payment_id"],),
        ).fetchone()
    assert order_row["refunded_amount_fen"] == 40

    audit_page = client.get("/admin/audit", auth=ADMIN)
    assert audit_page.status_code == 200
    assert "refund.create" in audit_page.text


def test_viewer_cannot_refund(env) -> None:
    _client, _auth, order, _otn = paid_order(env, business_order_no="A-VIEWER")
    client = env.clients["admin"]
    csrf = admin_csrf(client)
    response = client.post(
        f"/admin/orders/{order['payment_id']}/refund",
        data={
            "csrf_token": csrf,
            "amount_fen": "100",
            "idempotency_key": "viewer-refund",
        },
        auth=VIEWER,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_dashboard_shows_dead_events(env) -> None:
    _client, _auth, order, _otn = paid_order(env, business_order_no="A-DEAD")
    env.notifier.queue(DeliveryResult(status_code=400, body_excerpt="bad signature"))
    env.run_outbox()
    client = env.clients["admin"]
    page = client.get("/admin", auth=ADMIN)
    assert page.status_code == 200
    assert "死信通知" in page.text
    assert "payment.succeeded" in page.text
    assert order["payment_id"]


def test_html_login_sets_session_and_logout_clears_it(env) -> None:
    client = env.clients["anon"]
    page = client.get("/admin/login")
    assert page.status_code == 200
    assert "登录管理台" in page.text
    csrf = client.cookies["pypay_admin_csrf"]

    rejected = client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrong-password", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert rejected.status_code == 401
    assert "账号或密码不正确" in rejected.text

    accepted = client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin-password", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/admin"
    assert client.cookies["pypay_admin_session"]

    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "admin · admin" in dashboard.text

    logout = client.post(
        "/admin/logout",
        data={"csrf_token": client.cookies["pypay_admin_csrf"]},
        follow_redirects=False,
    )
    assert logout.status_code == 303
    assert logout.headers["location"] == "/admin/login"
    assert "pypay_admin_session" not in client.cookies

    redirected = client.get("/admin", headers={"accept": "text/html"}, follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/admin/login"


def test_login_rejects_missing_csrf_and_tampered_session(env) -> None:
    client = env.clients["anon"]
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin-password"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    client.cookies.set("pypay_admin_session", "v1.not-a-real-session.invalid")
    redirect = client.get("/admin", headers={"accept": "text/html"}, follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/admin/login"


def test_docs_are_discovered_rendered_and_auto_extended(tmp_path) -> None:
    docs = tmp_path / "docs"
    (docs / "ops").mkdir(parents=True)
    (docs / "README.md").write_text(
        "# 文档首页\n\n见 [部署说明](deployment.md)。\n\n"
        "| 项目 | 说明 |\n| --- | --- |\n| pypay | 支付 |\n",
        encoding="utf-8",
    )
    (docs / "deployment.md").write_text("# 部署说明\n\n运行 `pytest`。\n", encoding="utf-8")
    (docs / "ops" / "runbook.md").write_text("# 运维手册\n\n排障步骤。\n", encoding="utf-8")
    env = build_env(tmp_path / "env", service_docs_dir=docs)
    client = env.clients["admin"]

    index = client.get("/admin/docs", auth=ADMIN)
    assert index.status_code == 200
    assert "文档首页" in index.text
    assert "运维手册" in index.text

    readme = client.get("/admin/docs/README", auth=ADMIN)
    assert readme.status_code == 200
    assert "<h1>文档首页</h1>" in readme.text
    assert 'href="/admin/docs/deployment"' in readme.text
    assert "<table>" in readme.text

    nested = client.get("/admin/docs/ops/runbook", auth=ADMIN)
    assert nested.status_code == 200
    assert "排障步骤" in nested.text

    (docs / "new-guide.md").write_text("# 新接入文档\n\n无需改代码。\n", encoding="utf-8")
    refreshed = client.get("/admin/docs", auth=ADMIN)
    assert "新接入文档" in refreshed.text

    traversal = client.get("/admin/docs/..%2Fpyproject.toml", auth=ADMIN)
    assert traversal.status_code == 404


def test_viewer_session_can_read_but_not_write(env) -> None:
    client = env.clients["anon"]
    client.get("/admin/login")
    csrf = client.cookies["pypay_admin_csrf"]
    login = client.post(
        "/admin/login",
        data={"username": "viewer", "password": "viewer-password", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = client.get("/admin")
    assert page.status_code == 200
    assert "viewer · viewer" in page.text

    write = client.post(
        "/admin/channels",
        data={
            "csrf_token": client.cookies["pypay_admin_csrf"],
            "account_id": "wechat-session-viewer",
            "channel": "wechat",
            "name": "只读测试",
            "env_prefix": "PYPAY_WECHAT_VIEWER_",
        },
        follow_redirects=False,
    )
    assert write.status_code == 403
