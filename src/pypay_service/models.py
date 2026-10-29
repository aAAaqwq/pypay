from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from pypay import Channel

from .enums import (
    AckStatus,
    AttemptStatus,
    DeliveryStatus,
    EventType,
    OrderStatus,
    ProjectStatus,
    RefundState,
)


@dataclass(frozen=True)
class ChannelAccount:
    id: str
    channel: Channel
    name: str
    env_prefix: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    status: ProjectStatus
    api_key_id: str
    notify_url: str
    allowed_return_urls: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProjectAuth:
    project: Project
    api_secret: str = field(repr=False)
    notify_secret: str = field(repr=False)


@dataclass(frozen=True)
class CreatedProject:
    project: Project
    api_secret: str = field(repr=False)
    notify_secret: str = field(repr=False)


@dataclass(frozen=True)
class RotatedSecrets:
    api_key_id: str
    api_secret: str | None = field(repr=False)
    notify_secret: str | None = field(repr=False)


@dataclass(frozen=True)
class PaymentOrder:
    id: str
    project_id: str
    business_order_no: str
    subject: str
    amount_fen: int
    currency: str
    status: OrderStatus
    cashier_token: str
    return_url: str | None
    metadata: dict[str, Any]
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
    closed_at: datetime | None
    refunded_amount_fen: int


@dataclass(frozen=True)
class PaymentAttempt:
    id: str
    order_id: str
    channel: Channel
    account_id: str
    out_trade_no: str
    status: AttemptStatus
    amount_fen: int
    currency: str
    qr_code_url: str | None
    form_html: str | None
    redirect_url: str | None
    transaction_id: str | None
    channel_status: str | None
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
    closed_at: datetime | None


@dataclass(frozen=True)
class Refund:
    id: str
    order_id: str
    attempt_id: str
    project_id: str
    out_refund_no: str
    amount_fen: int
    total_amount_fen: int
    reason: str | None
    status: RefundState
    provider_refund_id: str | None
    idempotency_key: str | None
    requested_by: str
    created_at: datetime
    updated_at: datetime
    succeeded_at: datetime | None


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    project_id: str
    order_id: str
    event_type: EventType
    dedupe_key: str
    payload: dict[str, Any]
    status: DeliveryStatus
    ack_status: AckStatus
    attempts: int
    manual_resends: int
    next_attempt_at: datetime
    lease_until: datetime | None
    last_error: str | None
    delivered_at: datetime | None
    acked_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DeliveryRecord:
    id: int
    event_id: str
    attempt_no: int
    status: str
    http_status: int | None
    response_excerpt: str | None
    error: str | None
    duration_ms: int | None
    sent_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor: str
    role: str
    action: str
    project_id: str | None
    order_id: str | None
    details: Mapping[str, Any]
    created_at: datetime


def frozen_metadata(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))
