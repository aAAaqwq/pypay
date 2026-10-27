import json

import pytest
import requests

from pypay import (
    Channel,
    CreatePaymentRequest,
    NotificationEvent,
    PaymentClient,
    PaymentStatus,
    RefundRequest,
    RefundStatus,
    WeChatPayConfig,
)
from pypay.channels.wechat import WeChatChannel
from pypay.errors import (
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentSignatureError,
    PaymentValidationError,
)

from .conftest import Keys, WeChatTransport
from .keys import (
    verify_sha256,
    wechat_headers,
    wechat_notification_body,
    wechat_resource,
)

ORDER = "ORDER123"
REFUND = "REFUND123"


def make_channel(config: WeChatPayConfig) -> WeChatChannel:
    return WeChatChannel(config)


def query_payload(**overrides: object) -> dict[str, object]:
    payload = {
        "appid": "wxd678efh567hg6787",
        "mchid": "1900000109",
        "out_trade_no": ORDER,
        "transaction_id": "4200000000001",
        "trade_type": "NATIVE",
        "trade_state": "SUCCESS",
        "trade_state_desc": "支付成功",
        "success_time": "2026-10-27T10:00:00+08:00",
        "amount": {"total": 100, "payer_total": 100, "currency": "CNY"},
    }
    payload.update(overrides)
    return payload


def refund_payload(**overrides: object) -> dict[str, object]:
    payload = {
        "refund_id": "5030000000001",
        "out_refund_no": REFUND,
        "transaction_id": "4200000000001",
        "out_trade_no": ORDER,
        "status": "SUCCESS",
        "create_time": "2026-10-27T10:01:00+08:00",
        "success_time": "2026-10-27T10:01:05+08:00",
        "amount": {
            "total": 100,
            "refund": 100,
            "payer_total": 100,
            "payer_refund": 100,
            "currency": "CNY",
        },
    }
    payload.update(overrides)
    return payload


def parse_authorization(value: str) -> dict[str, str]:
    _, rest = value.split(" ", 1)
    return {key: raw.strip('"') for key, raw in (part.split("=", 1) for part in rest.split(","))}


def test_create_payment_returns_qr_and_signs_request(
    wechat_config: WeChatPayConfig,
    wechat_transport: WeChatTransport,
    keys: Keys,
) -> None:
    wechat_transport.queue(200, {"code_url": "weixin://wxpay/bizpayurl?pr=test"})
    channel = make_channel(wechat_config)
    result = channel.create_payment(
        CreatePaymentRequest(out_trade_no=ORDER, amount_fen=100, description="test order")
    )
    assert result.channel is Channel.WECHAT
    assert result.status is PaymentStatus.PENDING
    assert result.qr_code_url == "weixin://wxpay/bizpayurl?pr=test"

    call = wechat_transport.calls[0]
    assert call["url"].endswith("/v3/pay/transactions/native")
    assert call["payload"]["appid"] == wechat_config.appid
    assert call["payload"]["mchid"] == wechat_config.mchid
    assert call["payload"]["amount"] == {"total": 100, "currency": "CNY"}
    assert call["payload"]["notify_url"] == wechat_config.notify_url

    authorization = parse_authorization(call["headers"]["Authorization"])
    assert authorization["mchid"] == wechat_config.mchid
    assert authorization["serial_no"] == wechat_config.cert_serial_no
    sign_str = (
        f"POST\n/v3/pay/transactions/native\n{authorization['timestamp']}\n"
        f"{authorization['nonce_str']}\n{json.dumps(call['payload'])}\n"
    )
    verify_sha256(
        keys.merchant.private_key.public_key(),
        sign_str.encode(),
        authorization["signature"],
    )


def test_create_payment_rejects_bad_order_number(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    channel = make_channel(wechat_config)
    with pytest.raises(PaymentValidationError):
        channel.create_payment(
            CreatePaymentRequest(out_trade_no="ORDER/123", amount_fen=100, description="x")
        )
    assert wechat_transport.calls == []


def test_create_payment_rejects_plain_http_notify_url(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    channel = make_channel(wechat_config)
    with pytest.raises(PaymentValidationError):
        channel.create_payment(
            CreatePaymentRequest(
                out_trade_no=ORDER,
                amount_fen=100,
                description="x",
                notify_url="http://example.com/notify",
            )
        )


def test_query_payment_maps_paid(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, query_payload())
    result = make_channel(wechat_config).query_payment(ORDER)
    assert result.status is PaymentStatus.PAID
    assert result.amount_fen == 100
    assert result.transaction_id == "4200000000001"
    assert result.paid_at is not None
    assert result.paid_at.tzinfo is not None
    assert result.channel_status == "SUCCESS"


@pytest.mark.parametrize(
    ("trade_state", "expected"),
    [
        ("NOTPAY", PaymentStatus.PENDING),
        ("CLOSED", PaymentStatus.CLOSED),
        ("REFUND", PaymentStatus.REFUNDED),
        ("PAYERROR", PaymentStatus.FAILED),
        ("SOMETHING_NEW", PaymentStatus.UNKNOWN),
    ],
)
def test_query_payment_maps_trade_state(
    wechat_config: WeChatPayConfig,
    wechat_transport: WeChatTransport,
    trade_state: str,
    expected: PaymentStatus,
) -> None:
    wechat_transport.queue(200, query_payload(trade_state=trade_state))
    assert make_channel(wechat_config).query_payment(ORDER).status is expected


def test_query_payment_not_found(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(404, {"code": "ORDER_NOT_EXIST", "message": "not found"})
    with pytest.raises(PaymentNotFoundError) as exc_info:
        make_channel(wechat_config).query_payment(ORDER)
    assert exc_info.value.provider_code == "ORDER_NOT_EXIST"


def test_query_payment_rejects_mismatched_order(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, query_payload(out_trade_no="OTHER"))
    with pytest.raises(PaymentProviderError):
        make_channel(wechat_config).query_payment(ORDER)


def test_query_payment_rejects_non_cny(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, query_payload(amount={"total": 100, "currency": "USD"}))
    with pytest.raises(PaymentValidationError):
        make_channel(wechat_config).query_payment(ORDER)


def test_query_payment_timeout_is_ambiguous(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue_exception(requests.exceptions.Timeout("timeout"))
    with pytest.raises(PaymentNetworkError) as exc_info:
        make_channel(wechat_config).query_payment(ORDER)
    assert exc_info.value.may_have_succeeded is True


def test_response_with_bad_signature_is_rejected(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, query_payload(), sign=False)
    with pytest.raises(PaymentSignatureError):
        make_channel(wechat_config).query_payment(ORDER)


def test_close_payment(wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport) -> None:
    wechat_transport.queue(204)
    result = make_channel(wechat_config).close_payment(ORDER)
    assert result.status is PaymentStatus.CLOSED


def test_close_already_closed(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(400, {"code": "ORDER_CLOSED", "message": "already closed"})
    result = make_channel(wechat_config).close_payment(ORDER)
    assert result.status is PaymentStatus.CLOSED


def test_close_paid_order_is_provider_error(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(400, {"code": "ORDER_PAID", "message": "paid"})
    with pytest.raises(PaymentProviderError):
        make_channel(wechat_config).close_payment(ORDER)


def test_refund_and_query_refund(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, refund_payload())
    result = make_channel(wechat_config).refund(
        RefundRequest(
            out_refund_no=REFUND,
            out_trade_no=ORDER,
            amount_fen=100,
            total_amount_fen=100,
            reason="customer request",
        )
    )
    assert result.status is RefundStatus.SUCCEEDED
    assert result.amount_fen == 100
    assert result.refund_id == "5030000000001"
    call = wechat_transport.calls[0]
    assert call["payload"]["amount"] == {"refund": 100, "total": 100, "currency": "CNY"}
    assert call["payload"]["out_refund_no"] == REFUND

    wechat_transport.queue(200, refund_payload(status="PROCESSING"))
    query = make_channel(wechat_config).query_refund(REFUND, ORDER)
    assert query.status is RefundStatus.PENDING
    assert query.out_trade_no == ORDER


def test_query_refund_mismatched_order(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, refund_payload())
    with pytest.raises(PaymentProviderError):
        make_channel(wechat_config).query_refund(REFUND, "OTHER")


def test_query_refund_not_found(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(404, {"code": "RESOURCE_NOT_EXISTS", "message": "missing"})
    with pytest.raises(PaymentNotFoundError):
        make_channel(wechat_config).query_refund(REFUND)


def payment_resource(**overrides: object) -> dict[str, object]:
    resource = {
        "mchid": "1900000109",
        "appid": "wxd678efh567hg6787",
        "out_trade_no": ORDER,
        "transaction_id": "4200000000001",
        "trade_type": "NATIVE",
        "trade_state": "SUCCESS",
        "trade_state_desc": "支付成功",
        "bank_type": "OTHERS",
        "attach": "",
        "success_time": "2026-10-27T10:00:00+08:00",
        "payer": {"openid": "openid-test"},
        "amount": {"total": 100, "payer_total": 100, "currency": "CNY"},
    }
    resource.update(overrides)
    return resource


def refund_resource(**overrides: object) -> dict[str, object]:
    resource = {
        "mchid": "1900000109",
        "out_trade_no": ORDER,
        "transaction_id": "4200000000001",
        "out_refund_no": REFUND,
        "refund_id": "5030000000001",
        "refund_status": "SUCCESS",
        "success_time": "2026-10-27T10:01:05+08:00",
        "amount": {"total": 100, "refund": 100, "currency": "CNY"},
    }
    resource.update(overrides)
    return resource


def notification(
    keys: Keys,
    event_type: str,
    resource: dict[str, object],
    notification_id: str = "notify-1",
    apiv3_key: str = "a" * 32,
) -> tuple[dict[str, str], str]:
    encrypted = wechat_resource(
        apiv3_key,
        resource,
        associated_data="refund" if event_type.startswith("REFUND.") else "transaction",
    )
    body = wechat_notification_body(event_type, encrypted, notification_id)
    headers = wechat_headers(keys.platform.private_key, "PUB_KEY_ID_TEST", body)
    return headers, body


def test_parse_payment_notification(wechat_config: WeChatPayConfig, keys: Keys) -> None:
    headers, body = notification(keys, "TRANSACTION.SUCCESS", payment_resource())
    result = make_channel(wechat_config).parse_notification(headers, body)
    assert result.event_type is NotificationEvent.PAYMENT_SUCCEEDED
    assert result.status is PaymentStatus.PAID
    assert result.out_trade_no == ORDER
    assert result.amount_fen == 100
    assert result.currency == "CNY"
    assert result.transaction_id == "4200000000001"
    assert result.occurred_at is not None


def test_parse_refund_notification_full_refund(wechat_config: WeChatPayConfig, keys: Keys) -> None:
    headers, body = notification(keys, "REFUND.SUCCESS", refund_resource())
    result = make_channel(wechat_config).parse_notification(headers, body)
    assert result.event_type is NotificationEvent.REFUND_SUCCEEDED
    assert result.status is PaymentStatus.REFUNDED
    assert result.refund_amount_fen == 100
    assert result.out_refund_no == REFUND


def test_parse_refund_notification_partial_refund(
    wechat_config: WeChatPayConfig, keys: Keys
) -> None:
    resource = refund_resource(amount={"total": 100, "refund": 40, "currency": "CNY"})
    headers, body = notification(keys, "REFUND.SUCCESS", resource)
    result = make_channel(wechat_config).parse_notification(headers, body)
    assert result.status is PaymentStatus.PAID
    assert result.refund_amount_fen == 40


def test_parse_closed_notification(wechat_config: WeChatPayConfig, keys: Keys) -> None:
    headers, body = notification(keys, "TRANSACTION.CLOSED", payment_resource(trade_state="CLOSED"))
    result = make_channel(wechat_config).parse_notification(headers, body)
    assert result.event_type is NotificationEvent.PAYMENT_CLOSED
    assert result.status is PaymentStatus.CLOSED


def test_parse_notification_rejects_bad_signature(
    wechat_config: WeChatPayConfig, keys: Keys
) -> None:
    headers, body = notification(keys, "TRANSACTION.SUCCESS", payment_resource())
    headers["wechatpay-signature"] = "AAAA"
    with pytest.raises(PaymentSignatureError):
        make_channel(wechat_config).parse_notification(headers, body)


@pytest.mark.parametrize(
    ("field", "value"),
    [("mchid", "1900000000"), ("appid", "wxdifferent000000")],
)
def test_parse_notification_rejects_wrong_merchant_or_app(
    wechat_config: WeChatPayConfig,
    keys: Keys,
    field: str,
    value: str,
) -> None:
    headers, body = notification(keys, "TRANSACTION.SUCCESS", payment_resource(**{field: value}))
    with pytest.raises(PaymentValidationError):
        make_channel(wechat_config).parse_notification(headers, body)


def test_parse_notification_rejects_event_state_mismatch(
    wechat_config: WeChatPayConfig, keys: Keys
) -> None:
    headers, body = notification(
        keys, "TRANSACTION.SUCCESS", payment_resource(trade_state="NOTPAY")
    )
    with pytest.raises(PaymentValidationError):
        make_channel(wechat_config).parse_notification(headers, body)


def test_parse_notification_rejects_non_cny(wechat_config: WeChatPayConfig, keys: Keys) -> None:
    resource = payment_resource(amount={"total": 100, "currency": "USD"})
    headers, body = notification(keys, "TRANSACTION.SUCCESS", resource)
    with pytest.raises(PaymentValidationError):
        make_channel(wechat_config).parse_notification(headers, body)


def test_parse_notification_rejects_unknown_event(
    wechat_config: WeChatPayConfig, keys: Keys
) -> None:
    headers, body = notification(keys, "SOMETHING.NEW", payment_resource())
    with pytest.raises(PaymentValidationError):
        make_channel(wechat_config).parse_notification(headers, body)


def test_parse_notification_rejects_malformed_json(
    wechat_config: WeChatPayConfig, keys: Keys
) -> None:
    body = "{not-json"
    headers = wechat_headers(keys.platform.private_key, "PUB_KEY_ID_TEST", body)
    with pytest.raises(PaymentValidationError):
        make_channel(wechat_config).parse_notification(headers, body)


def test_payment_client_validates_expected_amount(
    wechat_config: WeChatPayConfig, keys: Keys
) -> None:
    headers, body = notification(keys, "TRANSACTION.SUCCESS", payment_resource())
    client = PaymentClient(Channel.WECHAT, wechat_config)
    result = client.parse_notification(
        headers, body, expected_out_trade_no=ORDER, expected_amount_fen=100
    )
    assert result.event_type is NotificationEvent.PAYMENT_SUCCEEDED
    with pytest.raises(PaymentValidationError):
        client.parse_notification(headers, body, expected_amount_fen=99)


@pytest.mark.parametrize(
    ("field", "value"),
    [("mchid", "1900000000"), ("appid", "wxdifferent000000")],
)
def test_query_payment_rejects_wrong_merchant_or_app(
    wechat_config: WeChatPayConfig,
    wechat_transport: WeChatTransport,
    field: str,
    value: str,
) -> None:
    wechat_transport.queue(200, query_payload(**{field: value}))
    with pytest.raises(PaymentProviderError):
        make_channel(wechat_config).query_payment(ORDER)


def test_unexpected_programming_error_propagates(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue_exception(TypeError("programming bug"))
    with pytest.raises(TypeError):
        make_channel(wechat_config).query_payment(ORDER)


def test_generic_sdk_error_is_provider_error(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue_exception(Exception("sdk failure"))
    with pytest.raises(PaymentProviderError):
        make_channel(wechat_config).query_payment(ORDER)


def test_pkcs1_keys_work_with_the_sdk(keys: Keys, wechat_transport: WeChatTransport) -> None:
    config = WeChatPayConfig(
        mchid="1900000109",
        appid="wxd678efh567hg6787",
        apiv3_key="a" * 32,
        cert_serial_no="MERCHANT_SERIAL",
        private_key=keys.merchant.private_pem_pkcs1,
        public_key=keys.platform.public_pem_pkcs1,
        public_key_id="PUB_KEY_ID_TEST",
        notify_url="https://example.com/notify/wechat",
        timeout=5,
    )
    wechat_transport.queue(200, {"code_url": "weixin://wxpay/bizpayurl?pr=pkcs1"})
    result = make_channel(config).create_payment(
        CreatePaymentRequest(out_trade_no=ORDER, amount_fen=100, description="test")
    )
    assert result.qr_code_url == "weixin://wxpay/bizpayurl?pr=pkcs1"


def test_create_payment_sends_expiry_and_attach(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    from datetime import datetime, timezone

    wechat_transport.queue(200, {"code_url": "weixin://test"})
    make_channel(wechat_config).create_payment(
        CreatePaymentRequest(
            out_trade_no=ORDER,
            amount_fen=100,
            description="test",
            expires_at=datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc),
            attach="meta",
        )
    )
    payload = wechat_transport.calls[0]["payload"]
    assert payload["time_expire"] == "2026-10-27T02:00:00+00:00"
    assert payload["attach"] == "meta"


def test_query_payment_rejects_invalid_json(
    wechat_config: WeChatPayConfig, wechat_transport: WeChatTransport
) -> None:
    wechat_transport.queue(200, "not-json")
    with pytest.raises(PaymentProviderError):
        make_channel(wechat_config).query_payment(ORDER)


def test_parse_refund_abnormal_keeps_order_paid(wechat_config: WeChatPayConfig, keys: Keys) -> None:
    headers, body = notification(keys, "REFUND.ABNORMAL", refund_resource(refund_status="ABNORMAL"))
    result = make_channel(wechat_config).parse_notification(headers, body)
    assert result.event_type is NotificationEvent.REFUND_FAILED
    assert result.status is PaymentStatus.PAID


def test_parse_refund_closed_keeps_order_paid(wechat_config: WeChatPayConfig, keys: Keys) -> None:
    headers, body = notification(keys, "REFUND.CLOSED", refund_resource(refund_status="CLOSED"))
    result = make_channel(wechat_config).parse_notification(headers, body)
    assert result.event_type is NotificationEvent.REFUND_FAILED
    assert result.status is PaymentStatus.PAID
