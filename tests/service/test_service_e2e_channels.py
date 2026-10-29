from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from pypay import (
    AlipayConfig,
    AsyncPaymentClient,
    Channel,
    PaymentClient,
    WeChatPayConfig,
)
from pypay_service.app import create_app
from pypay_service.config import ChannelAccountSeed, ServiceConfig
from pypay_service.db import Database
from pypay_service.models import ProjectAuth
from pypay_service.projects_service import create_project, sync_channel_accounts
from pypay_service.security import SecretBox, cashier_form_token
from tests.conftest import AlipayTransport, Keys, WeChatTransport
from tests.keys import (
    alipay_notification_body,
    parse_alipay_form,
    wechat_headers,
    wechat_notification_body,
    wechat_resource,
)
from tests.service.helpers import (
    MASTER_KEY,
    PUBLIC_BASE_URL,
    Clock,
    FakeNotifier,
    attempt_out_trade_no,
    cashier_token,
    create_payment,
    payment_response,
)


def real_env(tmp_path: Path, *, channel: Channel, gateway):
    seeds = (
        ChannelAccountSeed(
            id=f"{channel.value}-default",
            channel=channel,
            name=channel.value,
            env_prefix=f"PYPAY_{channel.value.upper()}_",
        ),
    )
    config = ServiceConfig(
        database_path=tmp_path / f"{channel.value}.db",
        public_base_url=PUBLIC_BASE_URL,
        master_key=MASTER_KEY,
        admin_username="admin",
        admin_password="admin-password",
        channel_accounts=seeds,
    )
    db = Database(config.database_path)
    db.migrate_up()
    clock = Clock()
    sync_channel_accounts(db, config, clock())
    box = SecretBox(config.master_key)
    created = create_project(
        db,
        box,
        config,
        project_id="project-real",
        name="Project Real",
        notify_url="https://business-real.test/pypay/notify",
        return_urls=["https://business-real.test/return"],
        channels={channel: f"{channel.value}-default"},
        now=clock(),
    )
    notifier = FakeNotifier()
    app = create_app(
        config,
        gateways={f"{channel.value}-default": gateway},
        notifier=notifier,
        clock=clock,
        db=db,
        migrate=False,
    )
    client = TestClient(app)
    auth = ProjectAuth(
        project=created.project,
        api_secret=created.api_secret,
        notify_secret=created.notify_secret,
    )
    return config, db, box, clock, notifier, client, auth


def start(env_config: ServiceConfig, client: TestClient, token: str, channel: str):
    return client.post(
        f"/pay/{token}/start",
        data={
            "csrf_token": cashier_form_token(env_config.master_key, token),
            "channel": channel,
        },
        follow_redirects=False,
    )


def test_real_wechat_native_callback_end_to_end(
    tmp_path: Path,
    wechat_config: WeChatPayConfig,
    wechat_transport: WeChatTransport,
    keys: Keys,
) -> None:
    gateway = AsyncPaymentClient(PaymentClient(Channel.WECHAT, wechat_config))
    config, _db, _box, _clock, _notifier, client, auth = real_env(
        tmp_path, channel=Channel.WECHAT, gateway=gateway
    )
    created = create_payment(
        client, auth, business_order_no="WX-E2E", subject="微信端到端", amount_fen=100
    )
    assert created.status_code == 201, created.text
    token = cashier_token(created.json()["cashier_url"])
    wechat_transport.queue(200, {"code_url": "weixin://wxpay/bizpayurl?pr=e2e"})
    response = start(config, client, token, "wechat")
    assert response.status_code == 303, response.text
    payload = wechat_transport.calls[0]["payload"]
    assert payload["notify_url"] == "https://pay.test/notify/wechat/wechat-default"
    assert payload["amount"] == {"total": 100, "currency": "CNY"}

    out_trade_no = attempt_out_trade_no(client, auth, created.json()["payment_id"])
    resource = {
        "mchid": wechat_config.mchid,
        "appid": wechat_config.appid,
        "out_trade_no": out_trade_no,
        "transaction_id": "420000E2E",
        "trade_type": "NATIVE",
        "trade_state": "SUCCESS",
        "trade_state_desc": "支付成功",
        "success_time": "2026-10-27T10:00:00+08:00",
        "amount": {"total": 100, "payer_total": 100, "currency": "CNY"},
    }
    encrypted = wechat_resource(wechat_config.apiv3_key, resource)
    body = wechat_notification_body("TRANSACTION.SUCCESS", encrypted, "notify-e2e")
    headers = wechat_headers(keys.platform.private_key, wechat_config.public_key_id, body)
    response = client.post("/notify/wechat/wechat-default", content=body.encode(), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["code"] == "SUCCESS"
    payment = payment_response(client, auth, created.json()["payment_id"])
    assert payment["status"] == "paid"
    assert payment["attempts"][0]["transaction_id"] == "420000E2E"


def test_real_alipay_page_pay_and_callback_end_to_end(
    tmp_path: Path,
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    gateway = AsyncPaymentClient(PaymentClient(Channel.ALIPAY, alipay_config))
    config, db, _box, _clock, _notifier, client, auth = real_env(
        tmp_path, channel=Channel.ALIPAY, gateway=gateway
    )
    created = create_payment(
        client, auth, business_order_no="ALI-E2E", subject="支付宝端到端", amount_fen=250
    )
    assert created.status_code == 201, created.text
    token = cashier_token(created.json()["cashier_url"])
    response = start(config, client, token, "alipay")
    assert response.status_code == 303, response.text
    page = client.get(f"/pay/{token}")
    assert "前往支付宝" in page.text

    with db.read() as conn:
        attempt_row = conn.execute(
            "SELECT id, form_html, out_trade_no FROM payment_attempts"
        ).fetchone()
    assert attempt_row is not None
    form = client.get(f"/pay/{token}/alipay-form?attempt={attempt_row['id']}")
    assert form.status_code == 200, form.text
    fields = parse_alipay_form(attempt_row["form_html"])
    assert fields["notify_url"] == "https://pay.test/notify/alipay/alipay-default"
    assert fields["return_url"].endswith(f"/pay/{token}/return/alipay")
    biz = json.loads(fields["biz_content"])
    assert biz["out_trade_no"] == attempt_row["out_trade_no"]
    assert biz["total_amount"] == "2.50"

    notification_fields = {
        "app_id": alipay_config.appid,
        "notify_id": "notify-ali-e2e",
        "notify_time": "2026-10-27 10:00:05",
        "charset": "utf-8",
        "version": "1.0",
        "sign_type": "RSA2",
        "out_trade_no": attempt_row["out_trade_no"],
        "trade_no": "2026102722001",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "2.50",
        "gmt_payment": "2026-10-27 10:00:00",
    }
    body = alipay_notification_body(notification_fields, keys.alipay.private_key)
    response = client.post("/notify/alipay/alipay-default", content=body)
    assert response.status_code == 200, response.text
    assert response.text == "success"
    payment = payment_response(client, auth, created.json()["payment_id"])
    assert payment["status"] == "paid"
    assert payment["attempts"][0]["transaction_id"] == "2026102722001"
    assert alipay_transport.calls == []
