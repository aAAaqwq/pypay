import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from .enums import Channel, NotificationEvent, PaymentStatus, RefundStatus
from .errors import PaymentValidationError
from .money import validate_fen

MAX_OUT_TRADE_NO_LENGTH = 32
MAX_DESCRIPTION_LENGTH = 127
MAX_OUT_REFUND_NO_LENGTH = 64
MAX_ATTACH_LENGTH = 128
MAX_REFUND_REASON_LENGTH = 80
_ORDER_NUMBER_RE = re.compile(r"^[0-9A-Za-z_\-|*@]+$")


def _require_text(value: object, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaymentValidationError(f"{name} is required")
    if value != value.strip():
        raise PaymentValidationError(f"{name} must not have leading or trailing whitespace")
    if len(value) > max_length:
        raise PaymentValidationError(f"{name} must be at most {max_length} characters")
    return value


def _optional_text(value: object, name: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, name, max_length)


def _require_order_number(value: object, name: str, max_length: int) -> str:
    text = _require_text(value, name, max_length)
    if not _ORDER_NUMBER_RE.fullmatch(text):
        raise PaymentValidationError(f"{name} must contain only digits, letters, _ - | * @")
    return text


def validate_out_trade_no(value: str) -> None:
    _require_order_number(value, "out_trade_no", MAX_OUT_TRADE_NO_LENGTH)


def validate_out_refund_no(value: str) -> None:
    _require_order_number(value, "out_refund_no", MAX_OUT_REFUND_NO_LENGTH)


def _require_http_url(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PaymentValidationError(f"{name} is required")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PaymentValidationError(f"{name} must be an absolute http(s) URL")
    return value


def _require_aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise PaymentValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PaymentValidationError(f"{name} must include a timezone")
    return value


@dataclass(frozen=True)
class CreatePaymentRequest:
    out_trade_no: str
    amount_fen: int
    description: str
    notify_url: str | None = None
    return_url: str | None = None
    expires_at: datetime | None = None
    attach: str | None = None

    def __post_init__(self) -> None:
        _require_order_number(self.out_trade_no, "out_trade_no", MAX_OUT_TRADE_NO_LENGTH)
        validate_fen(self.amount_fen)
        _require_text(self.description, "description", MAX_DESCRIPTION_LENGTH)
        if self.notify_url is not None:
            _require_http_url(self.notify_url, "notify_url")
        if self.return_url is not None:
            _require_http_url(self.return_url, "return_url")
        if self.expires_at is not None:
            _require_aware_datetime(self.expires_at, "expires_at")
        _optional_text(self.attach, "attach", MAX_ATTACH_LENGTH)


@dataclass(frozen=True)
class CreatePaymentResult:
    channel: Channel
    out_trade_no: str
    amount_fen: int
    status: PaymentStatus
    qr_code_url: str | None = None
    form_html: str | None = None
    redirect_url: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class QueryPaymentResult:
    channel: Channel
    out_trade_no: str
    amount_fen: int
    status: PaymentStatus
    transaction_id: str | None = None
    paid_at: datetime | None = None
    channel_status: str | None = None


@dataclass(frozen=True)
class ClosePaymentResult:
    channel: Channel
    out_trade_no: str
    status: PaymentStatus


@dataclass(frozen=True)
class RefundRequest:
    out_refund_no: str
    out_trade_no: str
    amount_fen: int
    total_amount_fen: int
    reason: str | None = None
    notify_url: str | None = None

    def __post_init__(self) -> None:
        _require_order_number(self.out_refund_no, "out_refund_no", MAX_OUT_REFUND_NO_LENGTH)
        _require_order_number(self.out_trade_no, "out_trade_no", MAX_OUT_TRADE_NO_LENGTH)
        validate_fen(self.amount_fen, "amount_fen")
        validate_fen(self.total_amount_fen, "total_amount_fen")
        if self.amount_fen > self.total_amount_fen:
            raise PaymentValidationError("amount_fen must not exceed total_amount_fen")
        _optional_text(self.reason, "reason", MAX_REFUND_REASON_LENGTH)
        if self.notify_url is not None:
            _require_http_url(self.notify_url, "notify_url")


@dataclass(frozen=True)
class RefundResult:
    channel: Channel
    out_refund_no: str
    out_trade_no: str
    amount_fen: int
    total_amount_fen: int
    status: RefundStatus
    refund_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class QueryRefundResult:
    channel: Channel
    out_refund_no: str
    out_trade_no: str
    amount_fen: int
    status: RefundStatus
    total_amount_fen: int | None = None
    refund_id: str | None = None
    succeeded_at: datetime | None = None


@dataclass(frozen=True)
class PaymentNotification:
    channel: Channel
    event_type: NotificationEvent
    notification_id: str
    out_trade_no: str
    amount_fen: int
    currency: str
    status: PaymentStatus
    transaction_id: str | None = None
    out_refund_no: str | None = None
    refund_amount_fen: int | None = None
    occurred_at: datetime | None = None
    provider_data: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.notification_id, "notification_id", 128)
        _require_order_number(self.out_trade_no, "out_trade_no", MAX_OUT_TRADE_NO_LENGTH)
        if self.out_refund_no is not None:
            _require_order_number(self.out_refund_no, "out_refund_no", MAX_OUT_REFUND_NO_LENGTH)
        if (
            isinstance(self.amount_fen, bool)
            or not isinstance(self.amount_fen, int)
            or self.amount_fen <= 0
        ):
            raise PaymentValidationError("amount_fen must be a positive integer")
        if self.refund_amount_fen is not None and (
            isinstance(self.refund_amount_fen, bool)
            or not isinstance(self.refund_amount_fen, int)
            or self.refund_amount_fen <= 0
        ):
            raise PaymentValidationError("refund_amount_fen must be a positive integer")
        object.__setattr__(self, "provider_data", MappingProxyType(dict(self.provider_data)))

    @property
    def is_payment_succeeded(self) -> bool:
        return self.event_type is NotificationEvent.PAYMENT_SUCCEEDED

    def assert_matches(
        self,
        *,
        expected_out_trade_no: str | None = None,
        expected_amount_fen: int | None = None,
        expected_currency: str = "CNY",
    ) -> None:
        if expected_out_trade_no is not None and self.out_trade_no != expected_out_trade_no:
            raise PaymentValidationError(
                "notification out_trade_no does not match the expected order"
            )
        if expected_amount_fen is not None and self.amount_fen != expected_amount_fen:
            raise PaymentValidationError("notification amount does not match the expected order")
        if expected_currency and self.currency != expected_currency:
            raise PaymentValidationError(
                "notification currency does not match the expected currency"
            )
