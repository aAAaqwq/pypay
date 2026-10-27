from datetime import datetime, timezone

import pytest

from pypay import (
    Channel,
    CreatePaymentRequest,
    NotificationEvent,
    PaymentNotification,
    PaymentStatus,
    RefundRequest,
)
from pypay.errors import PaymentValidationError


def test_create_payment_request_accepts_valid_input() -> None:
    request = CreatePaymentRequest(
        out_trade_no="ORDER-1",
        amount_fen=100,
        description="test order",
        notify_url="https://example.com/notify",
        expires_at=datetime.now(timezone.utc),
        attach="meta",
    )
    assert request.amount_fen == 100


@pytest.mark.parametrize("out_trade_no", ["", " ORDER1", "ORDER1 ", "x" * 33])
def test_create_payment_request_rejects_bad_order_number(out_trade_no: str) -> None:
    with pytest.raises(PaymentValidationError):
        CreatePaymentRequest(out_trade_no=out_trade_no, amount_fen=1, description="test")


def test_create_payment_request_rejects_naive_expiry() -> None:
    with pytest.raises(PaymentValidationError):
        CreatePaymentRequest(
            out_trade_no="ORDER-1",
            amount_fen=1,
            description="test",
            expires_at=datetime.now(),
        )


def test_refund_request_rejects_amount_above_total() -> None:
    with pytest.raises(PaymentValidationError):
        RefundRequest(
            out_refund_no="REFUND-1",
            out_trade_no="ORDER-1",
            amount_fen=101,
            total_amount_fen=100,
        )


def test_notification_assert_matches() -> None:
    notification = PaymentNotification(
        channel=Channel.WECHAT,
        event_type=NotificationEvent.PAYMENT_SUCCEEDED,
        notification_id="notify-1",
        out_trade_no="ORDER-1",
        amount_fen=100,
        currency="CNY",
        status=PaymentStatus.PAID,
        transaction_id="TX-1",
    )
    notification.assert_matches(expected_out_trade_no="ORDER-1", expected_amount_fen=100)
    with pytest.raises(PaymentValidationError):
        notification.assert_matches(expected_out_trade_no="ORDER-2")
    with pytest.raises(PaymentValidationError):
        notification.assert_matches(expected_amount_fen=99)
    with pytest.raises(PaymentValidationError):
        notification.assert_matches(expected_currency="USD")


def test_notification_provider_data_is_read_only() -> None:
    notification = PaymentNotification(
        channel=Channel.ALIPAY,
        event_type=NotificationEvent.PAYMENT_SUCCEEDED,
        notification_id="notify-1",
        out_trade_no="ORDER-1",
        amount_fen=100,
        currency="CNY",
        status=PaymentStatus.PAID,
        provider_data={"a": 1},
    )
    assert notification.is_payment_succeeded
    assert dict(notification.provider_data) == {"a": 1}
    with pytest.raises(TypeError):
        notification.provider_data["b"] = 2  # type: ignore[index]
