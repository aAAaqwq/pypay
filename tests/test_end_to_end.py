from pathlib import Path

from fastapi.testclient import TestClient

from examples.fastapi_app import create_app
from pypay import (
    AlipayConfig,
    AsyncPaymentClient,
    Channel,
    PaymentClient,
    WeChatPayConfig,
)

from .conftest import AlipayTransport, Keys, WeChatTransport
from .keys import (
    alipay_notification_body,
    wechat_headers,
    wechat_notification_body,
    wechat_resource,
)


def test_wechat_end_to_end(
    tmp_path: Path,
    wechat_config: WeChatPayConfig,
    wechat_transport: WeChatTransport,
    keys: Keys,
) -> None:
    gateway = AsyncPaymentClient(PaymentClient(Channel.WECHAT, wechat_config))
    app = create_app({Channel.WECHAT: gateway}, db_path=tmp_path / "wechat.db")
    client = TestClient(app)

    wechat_transport.queue(200, {"code_url": "weixin://wxpay/bizpayurl?pr=e2e"})
    created = client.post(
        "/orders",
        json={"amount_fen": 100, "description": "e2e order", "channel": "wechat"},
    )
    assert created.status_code == 201
    order_no = created.json()["out_trade_no"]
    assert created.json()["qr_code_url"] == "weixin://wxpay/bizpayurl?pr=e2e"

    resource = {
        "mchid": wechat_config.mchid,
        "appid": wechat_config.appid,
        "out_trade_no": order_no,
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

    response = client.post("/notify/wechat", content=body.encode(), headers=headers)
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"
    order = client.get(f"/orders/{order_no}").json()
    assert order["status"] == "paid"
    assert order["transaction_id"] == "420000E2E"
    assert order["fulfilled_at"] is not None


def test_alipay_end_to_end(
    tmp_path: Path,
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    gateway = AsyncPaymentClient(PaymentClient(Channel.ALIPAY, alipay_config))
    app = create_app({Channel.ALIPAY: gateway}, db_path=tmp_path / "alipay.db")
    client = TestClient(app)

    created = client.post(
        "/orders",
        json={"amount_fen": 100, "description": "e2e order", "channel": "alipay"},
    )
    assert created.status_code == 201
    order_no = created.json()["out_trade_no"]
    assert created.json()["form_html"] is not None

    fields = {
        "app_id": alipay_config.appid,
        "notify_id": "notify-e2e",
        "notify_time": "2026-10-27 10:00:05",
        "charset": "utf-8",
        "version": "1.0",
        "sign_type": "RSA2",
        "out_trade_no": order_no,
        "trade_no": "2026102722001",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "1.00",
        "gmt_payment": "2026-10-27 10:00:00",
    }
    body = alipay_notification_body(fields, keys.alipay.private_key)
    response = client.post("/notify/alipay", content=body)
    assert response.status_code == 200
    assert response.text == "success"
    order = client.get(f"/orders/{order_no}").json()
    assert order["status"] == "paid"
    assert order["transaction_id"] == "2026102722001"
