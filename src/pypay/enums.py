from enum import Enum


class Channel(str, Enum):
    WECHAT = "wechat"
    ALIPAY = "alipay"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CLOSED = "closed"
    REFUNDED = "refunded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RefundStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class NotificationEvent(str, Enum):
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_CLOSED = "payment.closed"
    PAYMENT_FAILED = "payment.failed"
    REFUND_SUCCEEDED = "refund.succeeded"
    REFUND_FAILED = "refund.failed"
    UNKNOWN = "unknown"
