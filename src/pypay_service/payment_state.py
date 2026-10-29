from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .enums import AttemptStatus, EventType, OrderStatus, RefundState
from .models import PaymentAttempt, PaymentOrder, Refund
from .outbox_service import enqueue_payment_event, enqueue_refund_event
from .repo_orders import (
    add_refunded_amount,
    expire_order,
    get_order,
    list_attempts,
    mark_order_closed,
    mark_order_failed,
    update_attempt_paid,
    update_attempt_state,
    update_order_paid,
)
from .repo_refunds import list_refunds_for_order, update_refund_state
from .timeutil import to_db


@dataclass(frozen=True)
class StatusChange:
    order: PaymentOrder
    previous: OrderStatus
    current: OrderStatus
    changed: bool
    attempt: PaymentAttempt | None


def latest_attempt(conn: sqlite3.Connection, order_id: str) -> PaymentAttempt | None:
    attempts = list_attempts(conn, order_id)
    return attempts[-1] if attempts else None


def paid_attempt(conn: sqlite3.Connection, order_id: str) -> PaymentAttempt | None:
    for attempt in list_attempts(conn, order_id):
        if attempt.status is AttemptStatus.PAID:
            return attempt
    return None


def mark_paid(
    conn: sqlite3.Connection,
    order: PaymentOrder,
    attempt: PaymentAttempt,
    *,
    transaction_id: str | None,
    paid_at: datetime,
    channel_status: str | None,
    now: datetime,
) -> tuple[PaymentOrder, PaymentAttempt, bool]:
    update_attempt_paid(
        conn,
        attempt.id,
        transaction_id=transaction_id,
        channel_status=channel_status,
        paid_at=paid_at,
        now=now,
    )
    first_payment = update_order_paid(conn, order.id, paid_at, now)
    refreshed_order = get_order(conn, order.id)
    refreshed_attempt = None
    for candidate in list_attempts(conn, order.id):
        if candidate.id == attempt.id:
            refreshed_attempt = candidate
            break
    if refreshed_order is None or refreshed_attempt is None:
        raise LookupError(order.id)
    if first_payment:
        enqueue_payment_event(
            conn,
            order=refreshed_order,
            attempt=refreshed_attempt,
            event_type=EventType.PAYMENT_SUCCEEDED,
            occurred_at=paid_at,
            now=now,
        )
    return refreshed_order, refreshed_attempt, first_payment


def mark_attempt_closed(
    conn: sqlite3.Connection,
    attempt: PaymentAttempt,
    *,
    channel_status: str | None,
    now: datetime,
) -> bool:
    return update_attempt_state(
        conn,
        attempt.id,
        status=AttemptStatus.CLOSED,
        channel_status=channel_status,
        now=now,
        closed_at=now,
    )


def mark_attempt_failed(
    conn: sqlite3.Connection,
    attempt: PaymentAttempt,
    *,
    channel_status: str | None,
    now: datetime,
) -> bool:
    return update_attempt_state(
        conn,
        attempt.id,
        status=AttemptStatus.FAILED,
        channel_status=channel_status,
        now=now,
    )


def sync_order_status(conn: sqlite3.Connection, order: PaymentOrder, now: datetime) -> StatusChange:
    previous = order.status
    attempts = list_attempts(conn, order.id)

    if order.paid_at is not None:
        target = (
            OrderStatus.REFUNDED
            if order.refunded_amount_fen >= order.amount_fen or previous is OrderStatus.REFUNDED
            else OrderStatus.PAID
        )
        changed = False
        if target is not previous:
            conn.execute(
                "UPDATE payment_orders SET status = ?, updated_at = ? WHERE id = ?",
                (target.value, to_db(now), order.id),
            )
            changed = True
        refreshed = get_order(conn, order.id) or order
        return StatusChange(
            refreshed, previous, refreshed.status, changed, latest_attempt(conn, order.id)
        )

    paid = next((attempt for attempt in attempts if attempt.status is AttemptStatus.PAID), None)
    if paid is not None:
        first = update_order_paid(conn, order.id, paid.paid_at or now, now)
        refreshed = get_order(conn, order.id) or order
        if first:
            enqueue_payment_event(
                conn,
                order=refreshed,
                attempt=paid,
                event_type=EventType.PAYMENT_SUCCEEDED,
                occurred_at=paid.paid_at or now,
                now=now,
            )
        return StatusChange(refreshed, previous, refreshed.status, first, paid)

    has_pending = any(attempt.status is AttemptStatus.PENDING for attempt in attempts)
    if order.expires_at <= now:
        target = OrderStatus.EXPIRED
    elif has_pending:
        target = OrderStatus.PENDING
    elif attempts and all(attempt.status is AttemptStatus.CLOSED for attempt in attempts):
        target = OrderStatus.CLOSED
    elif any(attempt.status is AttemptStatus.FAILED for attempt in attempts):
        target = OrderStatus.FAILED
    elif attempts:
        target = OrderStatus.CLOSED
    else:
        target = OrderStatus.PENDING if order.expires_at > now else OrderStatus.EXPIRED

    changed = False
    if target is OrderStatus.EXPIRED:
        changed = expire_order(conn, order.id, now)
    elif target is OrderStatus.CLOSED:
        changed = mark_order_closed(conn, order.id, now)
    elif target is OrderStatus.FAILED:
        changed = mark_order_failed(conn, order.id, now)
    # Never regress a terminal order back to pending; leave it unchanged.

    refreshed = get_order(conn, order.id) or order
    representative = paid or latest_attempt(conn, order.id)
    if changed and refreshed.status is OrderStatus.EXPIRED:
        enqueue_payment_event(
            conn,
            order=refreshed,
            attempt=representative,
            event_type=EventType.PAYMENT_EXPIRED,
            occurred_at=now,
            now=now,
        )
    elif changed and refreshed.status is OrderStatus.CLOSED:
        enqueue_payment_event(
            conn,
            order=refreshed,
            attempt=representative,
            event_type=EventType.PAYMENT_CLOSED,
            occurred_at=now,
            now=now,
        )
    elif changed and refreshed.status is OrderStatus.FAILED:
        enqueue_payment_event(
            conn,
            order=refreshed,
            attempt=representative,
            event_type=EventType.PAYMENT_FAILED,
            occurred_at=now,
            now=now,
        )
    return StatusChange(refreshed, previous, refreshed.status, changed, representative)


def apply_refund_success(
    conn: sqlite3.Connection,
    order: PaymentOrder,
    attempt: PaymentAttempt,
    refund: Refund,
    *,
    provider_refund_id: str | None,
    occurred_at: datetime,
    now: datetime,
) -> tuple[Refund, PaymentOrder, bool]:
    changed = update_refund_state(
        conn,
        refund.id,
        status=RefundState.SUCCEEDED,
        provider_refund_id=provider_refund_id,
        now=now,
        succeeded_at=occurred_at,
    )
    refreshed_refund = None
    for candidate in list_refunds_for_order(conn, order.id):
        if candidate.id == refund.id:
            refreshed_refund = candidate
            break
    if refreshed_refund is None:
        raise LookupError(refund.id)
    if not changed:
        return refreshed_refund, order, False
    add_refunded_amount(conn, order.id, refund.amount_fen, now)
    refreshed_order = get_order(conn, order.id) or order
    enqueue_refund_event(
        conn,
        order=refreshed_order,
        attempt=attempt,
        refund=refreshed_refund,
        event_type=EventType.REFUND_SUCCEEDED,
        occurred_at=occurred_at,
        now=now,
    )
    return refreshed_refund, refreshed_order, True


def apply_refund_failure(
    conn: sqlite3.Connection,
    order: PaymentOrder,
    attempt: PaymentAttempt,
    refund: Refund,
    *,
    status: RefundState,
    provider_refund_id: str | None,
    occurred_at: datetime,
    now: datetime,
) -> tuple[Refund, bool]:
    changed = update_refund_state(
        conn,
        refund.id,
        status=status,
        provider_refund_id=provider_refund_id,
        now=now,
    )
    refreshed_refund = None
    for candidate in list_refunds_for_order(conn, order.id):
        if candidate.id == refund.id:
            refreshed_refund = candidate
            break
    if refreshed_refund is None:
        raise LookupError(refund.id)
    if changed and status in {RefundState.FAILED, RefundState.CLOSED}:
        enqueue_refund_event(
            conn,
            order=order,
            attempt=attempt,
            refund=refreshed_refund,
            event_type=EventType.REFUND_FAILED,
            occurred_at=occurred_at,
            now=now,
        )
    return refreshed_refund, changed
