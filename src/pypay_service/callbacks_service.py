from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from pypay import NotificationEvent, PaymentNotification, PaymentStatus

from .db import Database
from .enums import OrderStatus, RefundState
from .errors import NotFoundError, ValidationError
from .ids import new_refund_id
from .models import PaymentOrder, Refund
from .payment_state import (
    apply_refund_failure,
    apply_refund_success,
    mark_attempt_closed,
    mark_attempt_failed,
    mark_paid,
    sync_order_status,
)
from .repo_notifications import insert_channel_notification
from .repo_orders import (
    get_attempt_by_out_trade_no,
    get_order,
    update_attempt_created,
)
from .repo_refunds import get_refund_by_out_refund_no, insert_refund
from .timeutil import utcnow


@dataclass(frozen=True)
class ApplyNotificationResult:
    order: PaymentOrder | None
    duplicate: bool
    event_enqueued: bool
    ignored: bool = False


def _payload_hash(notification: PaymentNotification) -> str:
    try:
        encoded = json.dumps(
            dict(notification.provider_data),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(dict(notification.provider_data)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate(notification: PaymentNotification, order: PaymentOrder, attempt) -> None:
    if notification.channel is not attempt.channel:
        raise ValidationError("notification channel does not match the payment attempt")
    if notification.amount_fen != order.amount_fen or notification.amount_fen != attempt.amount_fen:
        raise ValidationError("notification amount does not match the payment order")
    if notification.currency != "CNY":
        raise ValidationError("notification currency is not CNY")
    if notification.event_type in {
        NotificationEvent.REFUND_SUCCEEDED,
        NotificationEvent.REFUND_FAILED,
    }:
        if notification.refund_amount_fen is None:
            raise ValidationError("refund notification is missing the refund amount")
        if notification.refund_amount_fen > order.amount_fen:
            raise ValidationError("refund amount exceeds the payment amount")


def apply_channel_notification(
    db: Database,
    notification: PaymentNotification,
    *,
    now: datetime | None = None,
) -> ApplyNotificationResult:
    current = now or utcnow()
    event_enqueued = False
    ignored = False
    with db.transaction() as conn:
        inserted = insert_channel_notification(
            conn,
            channel=notification.channel.value,
            notification_id=notification.notification_id,
            event_type=notification.event_type.value,
            out_trade_no=notification.out_trade_no,
            out_refund_no=notification.out_refund_no,
            payload_hash=_payload_hash(notification),
            now=current,
        )
        if not inserted:
            return ApplyNotificationResult(order=None, duplicate=True, event_enqueued=False)

        attempt = get_attempt_by_out_trade_no(conn, notification.out_trade_no)
        if attempt is None:
            raise NotFoundError("notification references an unknown payment order")
        order = get_order(conn, attempt.order_id)
        if order is None:
            raise NotFoundError("notification references an unknown payment order")
        _validate(notification, order, attempt)

        occurred_at = notification.occurred_at or current
        if notification.event_type is NotificationEvent.PAYMENT_SUCCEEDED:
            if notification.status is not PaymentStatus.PAID:
                raise ValidationError("payment success notification has an inconsistent status")
            order, attempt, first = mark_paid(
                conn,
                order,
                attempt,
                transaction_id=notification.transaction_id,
                paid_at=occurred_at,
                channel_status=notification.status.value,
                now=current,
            )
            event_enqueued = first
        elif notification.event_type is NotificationEvent.PAYMENT_CLOSED:
            changed_attempt = mark_attempt_closed(
                conn, attempt, channel_status=notification.status.value, now=current
            )
            change = sync_order_status(conn, order, current)
            order = change.order
            event_enqueued = change.changed and change.current in {
                OrderStatus.CLOSED,
                OrderStatus.FAILED,
                OrderStatus.EXPIRED,
            }
            ignored = not changed_attempt and not change.changed
        elif notification.event_type is NotificationEvent.PAYMENT_FAILED:
            changed_attempt = mark_attempt_failed(
                conn, attempt, channel_status=notification.status.value, now=current
            )
            change = sync_order_status(conn, order, current)
            order = change.order
            event_enqueued = change.changed and change.current in {
                OrderStatus.CLOSED,
                OrderStatus.FAILED,
                OrderStatus.EXPIRED,
            }
            ignored = not changed_attempt and not change.changed
        elif notification.event_type is NotificationEvent.PAYMENT_PENDING:
            update_attempt_created(
                conn,
                attempt.id,
                qr_code_url=None,
                form_html=None,
                redirect_url=None,
                channel_status=notification.status.value,
                now=current,
            )
            ignored = True
        elif notification.event_type is NotificationEvent.REFUND_SUCCEEDED:
            if notification.out_refund_no:
                refund = get_refund_by_out_refund_no(conn, notification.out_refund_no)
                if refund is None:
                    refund = Refund(
                        id=new_refund_id(),
                        order_id=order.id,
                        attempt_id=attempt.id,
                        project_id=order.project_id,
                        out_refund_no=notification.out_refund_no,
                        amount_fen=notification.refund_amount_fen or 0,
                        total_amount_fen=order.amount_fen,
                        reason="channel notification",
                        status=RefundState.PENDING,
                        provider_refund_id=None,
                        idempotency_key=None,
                        requested_by="channel",
                        created_at=current,
                        updated_at=current,
                        succeeded_at=None,
                    )
                    insert_refund(conn, refund)
                if refund.order_id != order.id:
                    raise ValidationError("refund notification references a different order")
                refund, order, changed = apply_refund_success(
                    conn,
                    order,
                    attempt,
                    refund,
                    provider_refund_id=None,
                    occurred_at=occurred_at,
                    now=current,
                )
                event_enqueued = changed
                ignored = not changed
            else:
                # Alipay's trade notification reports a cumulative refund_fee and
                # has no out_request_no. Per-refund state is reconciled through
                # refund/query_refund so this cumulative value is not booked here.
                ignored = True
        elif notification.event_type is NotificationEvent.REFUND_FAILED:
            if notification.out_refund_no:
                refund = get_refund_by_out_refund_no(conn, notification.out_refund_no)
                if refund is None:
                    ignored = True
                else:
                    _, changed = apply_refund_failure(
                        conn,
                        order,
                        attempt,
                        refund,
                        status=RefundState.FAILED,
                        provider_refund_id=None,
                        occurred_at=occurred_at,
                        now=current,
                    )
                    event_enqueued = changed
                    ignored = not changed
            else:
                ignored = True
        else:
            ignored = True
    return ApplyNotificationResult(
        order=order, duplicate=False, event_enqueued=event_enqueued, ignored=ignored
    )
