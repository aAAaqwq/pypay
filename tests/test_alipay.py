import json
from urllib.parse import urlencode

import pytest

from pypay import (
    AlipayConfig,
    Channel,
    CreatePaymentRequest,
    NotificationEvent,
    PaymentClient,
    PaymentStatus,
    RefundRequest,
    RefundStatus,
)
from pypay.channels.alipay import AlipayChannel
from pypay.errors import (
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentSignatureError,
    PaymentValidationError,
)

from .conftest import AlipayTransport, Keys
from .keys import (
    alipay_notification_body,
    alipay_request_sign_content,
    alipay_response,
    alipay_sign_content,
    parse_alipay_form,
    sign_sha256,
    verify_sha256,
)

ORDER = "ORDER123"
REFUND = "REFUND123"
APP_ID = "2021000000000000"


def make_channel(config: AlipayConfig) -> AlipayChannel:
    return AlipayChannel(config)


def test_create_payment_returns_signed_form(alipay_config: AlipayConfig, keys: Keys) -> None:
    result = make_channel(alipay_config).create_payment(
        CreatePaymentRequest(out_trade_no=ORDER, amount_fen=100, description="test order")
    )
    assert result.channel is Channel.ALIPAY
    assert result.form_html is not None
    fields = parse_alipay_form(result.form_html)
    signature = fields.pop("sign")
    verify_sha256(
        keys.app.private_key.public_key(),
        alipay_request_sign_content(fields).encode(),
        signature,
    )
    assert fields["method"] == "alipay.trade.page.pay"
    assert fields["app_id"] == APP_ID
    assert fields["notify_url"] == alipay_config.notify_url
    assert fields["return_url"] == alipay_config.return_url
    biz = json.loads(fields["biz_content"])
    assert biz["out_trade_no"] == ORDER
    assert biz["total_amount"] == "1.00"
    assert biz["subject"] == "test order"
    assert biz["product_code"] == "FAST_INSTANT_TRADE_PAY"


def test_create_payment_formats_expiry(alipay_config: AlipayConfig, keys: Keys) -> None:
    from datetime import datetime, timezone

    result = make_channel(alipay_config).create_payment(
        CreatePaymentRequest(
            out_trade_no=ORDER,
            amount_fen=1,
            description="test",
            expires_at=datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc),
        )
    )
    biz = json.loads(parse_alipay_form(result.form_html)["biz_content"])
    assert biz["time_expire"] == "2026-10-27 10:00:00"


def test_create_payment_rejects_bad_urls(
    alipay_config: AlipayConfig,
) -> None:
    with pytest.raises(PaymentValidationError):
        make_channel(alipay_config).create_payment(
            CreatePaymentRequest(
                out_trade_no=ORDER,
                amount_fen=100,
                description="test",
                notify_url="ftp://example.com/notify",
            )
        )


def query_payload(**overrides: object) -> dict[str, object]:
    payload = {
        "code": "10000",
        "msg": "Success",
        "out_trade_no": ORDER,
        "trade_no": "2026102722001",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "1.00",
        "send_pay_date": "2026-10-27 10:00:00",
    }
    payload.update(overrides)
    return payload


def test_query_payment_maps_paid(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    alipay_transport.queue(
        alipay_response("alipay_trade_query_response", query_payload(), keys.alipay.private_key)
    )
    result = make_channel(alipay_config).query_payment(ORDER)
    assert result.status is PaymentStatus.PAID
    assert result.amount_fen == 100
    assert result.transaction_id == "2026102722001"
    assert result.paid_at is not None and result.paid_at.tzinfo is not None
    call = alipay_transport.calls[0]
    biz = json.loads(call["params"]["biz_content"])
    assert biz["out_trade_no"] == ORDER


def test_query_payment_not_found(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {
        "code": "40004",
        "msg": "Business Failed",
        "sub_code": "ACQ.TRADE_NOT_EXIST",
        "sub_msg": "交易不存在",
    }
    alipay_transport.queue(
        alipay_response("alipay_trade_query_response", payload, keys.alipay.private_key)
    )
    with pytest.raises(PaymentNotFoundError) as exc_info:
        make_channel(alipay_config).query_payment(ORDER)
    assert exc_info.value.provider_code == "ACQ.TRADE_NOT_EXIST"


def test_query_payment_provider_error(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {
        "code": "40004",
        "msg": "Business Failed",
        "sub_code": "ACQ.SYSTEM_ERROR",
        "sub_msg": "系统错误",
    }
    alipay_transport.queue(
        alipay_response("alipay_trade_query_response", payload, keys.alipay.private_key)
    )
    with pytest.raises(PaymentProviderError):
        make_channel(alipay_config).query_payment(ORDER)


def test_query_payment_rejects_tampered_response(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    signed = alipay_response(
        "alipay_trade_query_response", query_payload(), keys.alipay.private_key
    )
    alipay_transport.queue(signed.replace(b'"1.00"', b'"9.99"'))
    with pytest.raises(PaymentSignatureError):
        make_channel(alipay_config).query_payment(ORDER)


def test_query_payment_timeout_is_ambiguous(
    alipay_config: AlipayConfig, alipay_transport: AlipayTransport
) -> None:
    alipay_transport.queue_exception(TimeoutError("timeout"))
    with pytest.raises(PaymentNetworkError) as exc_info:
        make_channel(alipay_config).query_payment(ORDER)
    assert exc_info.value.may_have_succeeded is True


def test_query_payment_request_exception_is_ambiguous(
    alipay_config: AlipayConfig, alipay_transport: AlipayTransport
) -> None:
    from alipay.aop.api.exception.Exception import RequestException

    alipay_transport.queue_exception(RequestException("connection reset"))
    with pytest.raises(PaymentNetworkError):
        make_channel(alipay_config).query_payment(ORDER)


def test_close_payment(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {"code": "10000", "msg": "Success", "out_trade_no": ORDER}
    alipay_transport.queue(
        alipay_response("alipay_trade_close_response", payload, keys.alipay.private_key)
    )
    result = make_channel(alipay_config).close_payment(ORDER)
    assert result.status is PaymentStatus.CLOSED


def test_refund_success(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {
        "code": "10000",
        "msg": "Success",
        "out_trade_no": ORDER,
        "trade_no": "2026102722001",
        "refund_fee": "1.00",
        "fund_change": "Y",
        "gmt_refund_pay": "2026-10-27 10:01:00",
    }
    alipay_transport.queue(
        alipay_response("alipay_trade_refund_response", payload, keys.alipay.private_key)
    )
    result = make_channel(alipay_config).refund(
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
    call = alipay_transport.calls[0]
    biz = json.loads(call["params"]["biz_content"])
    assert biz == {
        "out_trade_no": ORDER,
        "out_request_no": REFUND,
        "refund_amount": "1.00",
        "refund_reason": "customer request",
    }


def test_refund_fund_change_n_is_pending(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {
        "code": "10000",
        "msg": "Success",
        "out_trade_no": ORDER,
        "refund_fee": "1.00",
        "fund_change": "N",
    }
    alipay_transport.queue(
        alipay_response("alipay_trade_refund_response", payload, keys.alipay.private_key)
    )
    result = make_channel(alipay_config).refund(
        RefundRequest(
            out_refund_no=REFUND,
            out_trade_no=ORDER,
            amount_fen=100,
            total_amount_fen=100,
        )
    )
    assert result.status is RefundStatus.PENDING


def test_query_refund_success(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {
        "code": "10000",
        "msg": "Success",
        "out_trade_no": ORDER,
        "refund_amount": "1.00",
        "total_amount": "1.00",
        "refund_status": "REFUND_SUCCESS",
        "gmt_refund_pay": "2026-10-27 10:01:00",
    }
    alipay_transport.queue(
        alipay_response(
            "alipay_trade_fastpay_refund_query_response", payload, keys.alipay.private_key
        )
    )
    result = make_channel(alipay_config).query_refund(REFUND, ORDER)
    assert result.status is RefundStatus.SUCCEEDED
    assert result.amount_fen == 100
    call = alipay_transport.calls[0]
    biz = json.loads(call["params"]["biz_content"])
    assert biz == {"out_trade_no": ORDER, "out_request_no": REFUND}


def test_query_refund_requires_out_trade_no(
    alipay_config: AlipayConfig, alipay_transport: AlipayTransport
) -> None:
    with pytest.raises(PaymentValidationError):
        make_channel(alipay_config).query_refund(REFUND)


def test_query_refund_missing_record(
    alipay_config: AlipayConfig,
    alipay_transport: AlipayTransport,
    keys: Keys,
) -> None:
    payload = {"code": "10000", "msg": "Success", "out_trade_no": ORDER}
    alipay_transport.queue(
        alipay_response(
            "alipay_trade_fastpay_refund_query_response", payload, keys.alipay.private_key
        )
    )
    with pytest.raises(PaymentNotFoundError):
        make_channel(alipay_config).query_refund(REFUND, ORDER)


def notification_fields(**overrides: str) -> dict[str, str]:
    fields = {
        "app_id": APP_ID,
        "notify_id": "notify-1",
        "notify_time": "2026-10-27 10:00:05",
        "charset": "utf-8",
        "version": "1.0",
        "sign_type": "RSA2",
        "out_trade_no": ORDER,
        "trade_no": "2026102722001",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "1.00",
        "gmt_payment": "2026-10-27 10:00:00",
        "seller_id": "2088000000000000",
    }
    fields.update(overrides)
    return fields


def test_parse_notification(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(notification_fields(), keys.alipay.private_key)
    result = make_channel(alipay_config).parse_notification({}, body)
    assert result.event_type is NotificationEvent.PAYMENT_SUCCEEDED
    assert result.status is PaymentStatus.PAID
    assert result.amount_fen == 100
    assert result.transaction_id == "2026102722001"
    assert result.currency == "CNY"
    assert result.occurred_at is not None
    assert "sign" not in result.provider_data


@pytest.mark.parametrize(
    ("trade_status", "event"),
    [
        ("TRADE_FINISHED", NotificationEvent.PAYMENT_SUCCEEDED),
        ("TRADE_CLOSED", NotificationEvent.PAYMENT_CLOSED),
        ("WAIT_BUYER_PAY", NotificationEvent.PAYMENT_PENDING),
        ("SOMETHING_NEW", NotificationEvent.UNKNOWN),
    ],
)
def test_parse_notification_maps_trade_status(
    alipay_config: AlipayConfig,
    keys: Keys,
    trade_status: str,
    event: NotificationEvent,
) -> None:
    body = alipay_notification_body(
        notification_fields(trade_status=trade_status), keys.alipay.private_key
    )
    result = make_channel(alipay_config).parse_notification({}, body)
    assert result.event_type is event


def test_parse_notification_rejects_tampering(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(notification_fields(), keys.alipay.private_key)
    tampered = body.replace(b"1.00", b"9.99")
    with pytest.raises(PaymentSignatureError):
        make_channel(alipay_config).parse_notification({}, tampered)


def test_parse_notification_rejects_wrong_app_id(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(
        notification_fields(app_id="2021000000000001"), keys.alipay.private_key
    )
    with pytest.raises(PaymentValidationError):
        make_channel(alipay_config).parse_notification({}, body)


def test_parse_notification_rejects_wrong_seller_id(
    alipay_config: AlipayConfig, keys: Keys
) -> None:
    from dataclasses import replace

    config = replace(alipay_config, seller_id="2088000000000000")
    body = alipay_notification_body(
        notification_fields(seller_id="2088000000000001"), keys.alipay.private_key
    )
    with pytest.raises(PaymentValidationError):
        make_channel(config).parse_notification({}, body)


def test_parse_notification_rejects_non_cny(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(notification_fields(currency="USD"), keys.alipay.private_key)
    with pytest.raises(PaymentValidationError):
        make_channel(alipay_config).parse_notification({}, body)


def test_parse_notification_rejects_missing_notify_id(
    alipay_config: AlipayConfig, keys: Keys
) -> None:
    fields = notification_fields()
    fields.pop("notify_id")
    body = alipay_notification_body(fields, keys.alipay.private_key)
    with pytest.raises(PaymentValidationError):
        make_channel(alipay_config).parse_notification({}, body)


def test_parse_notification_rejects_missing_sign(
    alipay_config: AlipayConfig,
) -> None:
    body = b"app_id=2021000000000000&out_trade_no=ORDER123"
    with pytest.raises(PaymentSignatureError):
        make_channel(alipay_config).parse_notification({}, body)


def test_parse_notification_rejects_rsa_sign_type(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(notification_fields(sign_type="RSA"), keys.alipay.private_key)
    with pytest.raises(PaymentSignatureError):
        make_channel(alipay_config).parse_notification({}, body)


def test_parse_notification_rejects_duplicate_fields(
    alipay_config: AlipayConfig,
) -> None:
    with pytest.raises(PaymentValidationError):
        make_channel(alipay_config).parse_notification({}, b"a=1&a=2")


def test_notification_refund_fee(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(notification_fields(refund_fee="0.40"), keys.alipay.private_key)
    result = make_channel(alipay_config).parse_notification({}, body)
    assert result.refund_amount_fen == 40


def test_payment_client_validates_expected_amount(alipay_config: AlipayConfig, keys: Keys) -> None:
    body = alipay_notification_body(notification_fields(), keys.alipay.private_key)
    client = PaymentClient(Channel.ALIPAY, alipay_config)
    result = client.parse_notification(
        {}, body, expected_out_trade_no=ORDER, expected_amount_fen=100
    )
    assert result.event_type is NotificationEvent.PAYMENT_SUCCEEDED
    with pytest.raises(PaymentValidationError):
        client.parse_notification({}, body, expected_amount_fen=99)


def test_parse_notification_rejects_missing_sign_type(
    alipay_config: AlipayConfig, keys: Keys
) -> None:
    fields = notification_fields()
    fields.pop("sign_type")
    signature = sign_sha256(keys.alipay.private_key, alipay_sign_content(fields).encode())
    body = urlencode({**fields, "sign": signature}).encode()
    with pytest.raises(PaymentSignatureError):
        make_channel(alipay_config).parse_notification({}, body)
