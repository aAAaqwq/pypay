from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .enums import AckStatus, DeliveryStatus, EventType
from .ids import new_event_id
from .models import OutboxEvent, PaymentAttempt, PaymentOrder, Refund
from .repo_outbox import insert_event
from .timeutil import to_db


def _base_payload(
    event_id: str,
    event_type: EventType,
    order: PaymentOrder,
    occurred_at: datetime,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type.value,
        "project_id": order.project_id,
        "payment_id": order.id,
        "business_order_no": order.business_order_no,
        "subject": order.subject,
        "amount_fen": order.amount_fen,
        "currency": order.currency,
        "status": order.status.value,
        "refunded_amount_fen": order.refunded_amount_fen,
        "metadata": dict(order.metadata),
        "occurred_at": to_db(occurred_at),
    }


def enqueue_payment_event(
    conn: sqlite3.Connection,
    *,
    order: PaymentOrder,
    attempt: PaymentAttempt | None,
    event_type: EventType,
    occurred_at: datetime,
    now: datetime,
) -> bool:
    event_id = new_event_id()
    payload = _base_payload(event_id, event_type, order, occurred_at)
    payload.update(
        {
            "channel": attempt.channel.value if attempt else None,
            "payment_order_no": attempt.out_trade_no if attempt else None,
            "transaction_id": attempt.transaction_id if attempt else None,
        }
    )
    event = OutboxEvent(
        event_id=event_id,
        project_id=order.project_id,
        order_id=order.id,
        event_type=event_type,
        dedupe_key=f"{event_type.value}:{order.id}",
        payload=payload,
        status=DeliveryStatus.PENDING,
        ack_status=AckStatus.PENDING,
        attempts=0,
        manual_resends=0,
        next_attempt_at=now,
        lease_until=None,
        last_error=None,
        delivered_at=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )
    return insert_event(conn, event)


def enqueue_refund_event(
    conn: sqlite3.Connection,
    *,
    order: PaymentOrder,
    attempt: PaymentAttempt,
    refund: Refund,
    event_type: EventType,
    occurred_at: datetime,
    now: datetime,
) -> bool:
    event_id = new_event_id()
    payload = _base_payload(event_id, event_type, order, occurred_at)
    payload.update(
        {
            "channel": attempt.channel.value,
            "payment_order_no": attempt.out_trade_no,
            "transaction_id": attempt.transaction_id,
            "refund_id": refund.id,
            "out_refund_no": refund.out_refund_no,
            "refund_amount_fen": refund.amount_fen,
            "refunded_total_fen": order.refunded_amount_fen,
            "reason": refund.reason,
        }
    )
    event = OutboxEvent(
        event_id=event_id,
        project_id=order.project_id,
        order_id=order.id,
        event_type=event_type,
        dedupe_key=f"{event_type.value}:{refund.id}",
        payload=payload,
        status=DeliveryStatus.PENDING,
        ack_status=AckStatus.PENDING,
        attempts=0,
        manual_resends=0,
        next_attempt_at=now,
        lease_until=None,
        last_error=None,
        delivered_at=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )
    return insert_event(conn, event)
