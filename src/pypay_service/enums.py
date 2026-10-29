from enum import Enum

from pypay import Channel

__all__ = [
    "AckStatus",
    "AttemptStatus",
    "Channel",
    "DeliveryStatus",
    "EventType",
    "OrderStatus",
    "ProjectStatus",
    "RefundState",
]


class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CLOSED = "closed"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    FAILED = "failed"


class AttemptStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CLOSED = "closed"
    FAILED = "failed"
    REFUNDED = "refunded"


class RefundState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CLOSED = "closed"


class EventType(str, Enum):
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_CLOSED = "payment.closed"
    PAYMENT_EXPIRED = "payment.expired"
    PAYMENT_FAILED = "payment.failed"
    REFUND_SUCCEEDED = "refund.succeeded"
    REFUND_FAILED = "refund.failed"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    DELIVERED = "delivered"
    DEAD = "dead"


class AckStatus(str, Enum):
    PENDING = "pending"
    ACKED = "acked"
    REJECTED = "rejected"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
