from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from pypay import (
    Channel,
    CreatePaymentRequest,
    PaymentConfigError,
    PaymentError,
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentSignatureError,
    PaymentStatus,
    PaymentValidationError,
)
from pypay.models import MAX_DESCRIPTION_LENGTH
from pypay.money import validate_fen

from .config import ServiceConfig
from .db import Database
from .enums import AttemptStatus, EventType, OrderStatus
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ProviderError,
    ValidationError,
)
from .gateways import GatewayRegistry
from .ids import new_attempt_id, new_cashier_token, new_out_trade_no, new_payment_id
from .models import PaymentAttempt, PaymentOrder, Project
from .outbox_service import enqueue_payment_event
from .payment_state import (
    StatusChange,
    latest_attempt,
    mark_attempt_closed,
    mark_attempt_failed,
    mark_paid,
    sync_order_status,
)
from .repo_orders import (
    expire_order,
    get_attempt,
    get_attempt_for_order_channel,
    get_order,
    get_order_by_business_no,
    get_order_by_token,
    get_order_for_project,
    insert_attempt,
    insert_order,
    list_attempts,
    list_orders_due_for_expiry,
    update_attempt_created,
)
from .repo_projects import (
    get_channel_account,
    get_project,
    get_project_channel_account,
    has_enabled_project_channel,
)
from .timeutil import to_db, utcnow

_BUSINESS_NO_RE = re.compile(r"^[0-9A-Za-z_.:@-]{1,64}$")
_MAX_EXPIRY_SECONDS = 24 * 60 * 60
_MIN_EXPIRY_SECONDS = 60
_MAX_METADATA_BYTES = 4096


@dataclass(frozen=True)
class CreateOrderInput:
    business_order_no: str
    subject: str
    amount_fen: int
    currency: str = "CNY"
    expires_in_seconds: int = 1800
    return_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CreateOrderResult:
    order: PaymentOrder
    created: bool


@dataclass(frozen=True)
class OrderDetail:
    order: PaymentOrder
    attempts: list[PaymentAttempt]
    refunds: list[Any]
    events: list[Any]
    project: Project | None = None


@dataclass(frozen=True)
class AttemptStartResult:
    order: PaymentOrder
    attempt: PaymentAttempt
    ambiguous: bool = False


@dataclass(frozen=True)
class RefreshReport:
    order: PaymentOrder
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CloseReport:
    order: PaymentOrder
    errors: tuple[str, ...] = ()


def cashier_url(config: ServiceConfig, order: PaymentOrder) -> str:
    return f"{config.public_base_url}/pay/{order.cashier_token}"


def _validate_business_order_no(value: str) -> str:
    if not isinstance(value, str) or not _BUSINESS_NO_RE.fullmatch(value):
        raise ValidationError(
            "business_order_no must be 1-64 characters from letters, digits, _ . : @ -"
        )
    return value


def _validate_subject(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("subject is required")
    if value != value.strip():
        raise ValidationError("subject must not have leading or trailing whitespace")
    if len(value) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError(f"subject must be at most {MAX_DESCRIPTION_LENGTH} characters")
    return value


def _validate_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("metadata must be a JSON object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValidationError(f"metadata must be at most {_MAX_METADATA_BYTES} bytes")
    return dict(value)


def _validate_order_input(project: Project, payload: CreateOrderInput) -> None:
    _validate_business_order_no(payload.business_order_no)
    _validate_subject(payload.subject)
    validate_fen(payload.amount_fen)
    if payload.currency != "CNY":
        raise ValidationError("currency must be CNY")
    if (
        isinstance(payload.expires_in_seconds, bool)
        or not isinstance(payload.expires_in_seconds, int)
        or not _MIN_EXPIRY_SECONDS <= payload.expires_in_seconds <= _MAX_EXPIRY_SECONDS
    ):
        raise ValidationError(
            f"expires_in_seconds must be between {_MIN_EXPIRY_SECONDS} and {_MAX_EXPIRY_SECONDS}"
        )
    if payload.return_url is not None and payload.return_url not in project.allowed_return_urls:
        raise ValidationError("return_url is not registered for this project")


def _assert_idempotent(existing: PaymentOrder, payload: CreateOrderInput) -> None:
    if existing.amount_fen != payload.amount_fen:
        raise ConflictError("business_order_no already exists with a different amount")
    if existing.currency != payload.currency:
        raise ConflictError("business_order_no already exists with a different currency")
    if existing.subject != payload.subject:
        raise ConflictError("business_order_no already exists with a different subject")
    if existing.metadata != payload.metadata:
        raise ConflictError("business_order_no already exists with different metadata")
    if payload.return_url is not None and existing.return_url != payload.return_url:
        raise ConflictError("business_order_no already exists with a different return_url")


def create_order(
    db: Database,
    config: ServiceConfig,
    project: Project,
    payload: CreateOrderInput,
    *,
    now: datetime | None = None,
) -> CreateOrderResult:
    current = now or utcnow()
    _validate_order_input(project, payload)
    with db.read() as conn:
        if not has_enabled_project_channel(conn, project.id):
            raise ConflictError("project has no enabled payment channel")
    expires_at = current + timedelta(seconds=payload.expires_in_seconds)
    order = PaymentOrder(
        id=new_payment_id(),
        project_id=project.id,
        business_order_no=payload.business_order_no,
        subject=payload.subject,
        amount_fen=payload.amount_fen,
        currency=payload.currency,
        status=OrderStatus.PENDING,
        cashier_token=new_cashier_token(),
        return_url=payload.return_url,
        metadata=_validate_metadata(payload.metadata),
        expires_at=expires_at,
        created_at=current,
        updated_at=current,
        paid_at=None,
        closed_at=None,
        refunded_amount_fen=0,
    )
    try:
        with db.transaction() as conn:
            insert_order(conn, order)
        return CreateOrderResult(order=order, created=True)
    except sqlite3.IntegrityError:
        with db.read() as conn:
            existing = get_order_by_business_no(conn, project.id, payload.business_order_no)
        if existing is None:
            raise
        _assert_idempotent(existing, payload)
        return CreateOrderResult(order=existing, created=False)


def get_order_for_request(db: Database, project_id: str, payment_id: str) -> PaymentOrder:
    with db.read() as conn:
        order = get_order_for_project(conn, payment_id, project_id)
    if order is None:
        raise NotFoundError("payment not found")
    return order


def get_order_by_business_order(
    db: Database, project_id: str, business_order_no: str
) -> PaymentOrder:
    with db.read() as conn:
        order = get_order_by_business_no(conn, project_id, business_order_no)
    if order is None:
        raise NotFoundError("payment not found")
    return order


def get_order_detail(db: Database, order: PaymentOrder) -> OrderDetail:
    from .repo_outbox import list_events_for_order
    from .repo_projects import get_project as _get_project
    from .repo_refunds import list_refunds_for_order

    with db.read() as conn:
        attempts = list_attempts(conn, order.id)
        refunds = list_refunds_for_order(conn, order.id)
        events = list_events_for_order(conn, order.id)
        project = _get_project(conn, order.project_id)
    return OrderDetail(
        order=order,
        attempts=attempts,
        refunds=list(refunds),
        events=list(events),
        project=project,
    )


def serialize_payment(db: Database, config: ServiceConfig, order: PaymentOrder) -> dict[str, Any]:
    detail = get_order_detail(db, order)
    ack_status = None
    for event in detail.events:
        if event.event_type is EventType.PAYMENT_SUCCEEDED:
            ack_status = event.ack_status.value
            break
    return {
        "payment_id": order.id,
        "business_order_no": order.business_order_no,
        "project_id": order.project_id,
        "subject": order.subject,
        "amount_fen": order.amount_fen,
        "currency": order.currency,
        "status": order.status.value,
        "service_ack_status": ack_status,
        "refunded_amount_fen": order.refunded_amount_fen,
        "cashier_url": cashier_url(config, order),
        "expires_at": to_db(order.expires_at),
        "created_at": to_db(order.created_at),
        "paid_at": to_db(order.paid_at) if order.paid_at else None,
        "closed_at": to_db(order.closed_at) if order.closed_at else None,
        "return_url": order.return_url,
        "attempts": [
            {
                "channel": attempt.channel.value,
                "status": attempt.status.value,
                "payment_order_no": attempt.out_trade_no,
                "transaction_id": attempt.transaction_id,
                "channel_status": attempt.channel_status,
                "paid_at": to_db(attempt.paid_at) if attempt.paid_at else None,
            }
            for attempt in detail.attempts
        ],
        "refunds": [
            {
                "refund_id": refund.id,
                "out_refund_no": refund.out_refund_no,
                "amount_fen": refund.amount_fen,
                "status": refund.status.value,
                "succeeded_at": to_db(refund.succeeded_at) if refund.succeeded_at else None,
            }
            for refund in detail.refunds
        ],
        "events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "delivery_status": event.status.value,
                "ack_status": event.ack_status.value,
                "attempts": event.attempts,
                "last_error": event.last_error,
            }
            for event in detail.events
        ],
    }


def expire_if_needed(db: Database, order: PaymentOrder, now: datetime) -> PaymentOrder:
    if order.expires_at > now or order.paid_at is not None:
        return order
    with db.transaction() as conn:
        current = get_order(conn, order.id)
        if current is None:
            raise NotFoundError("payment not found")
        sync_order_status(conn, current, now)
        refreshed = get_order(conn, order.id)
    return refreshed or order


async def start_cashier_attempt(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    cashier_token: str,
    channel: Channel,
    now: datetime | None = None,
) -> AttemptStartResult:
    current = now or utcnow()
    with db.read() as conn:
        order = get_order_by_token(conn, cashier_token)
        if order is None:
            raise NotFoundError("cashier link not found")
        project = get_project(conn, order.project_id)
        if project is None:
            raise NotFoundError("project not found")
        account = get_project_channel_account(conn, project.id, channel)
        attempt = get_attempt_for_order_channel(conn, order.id, channel)
    if account is None:
        raise ForbiddenError("payment channel is not enabled for this project")
    order = expire_if_needed(db, order, current)
    if order.status is not OrderStatus.PENDING:
        if order.status in {OrderStatus.PAID, OrderStatus.REFUNDED} and attempt is not None:
            return AttemptStartResult(order=order, attempt=attempt)
        raise ConflictError(f"payment order is {order.status.value}")
    if order.expires_at <= current:
        raise ConflictError("payment order has expired")
    if attempt is not None:
        if attempt.status in {AttemptStatus.PAID, AttemptStatus.REFUNDED}:
            return AttemptStartResult(order=order, attempt=attempt)
        if attempt.status in {AttemptStatus.CLOSED, AttemptStatus.FAILED}:
            raise ConflictError("this payment attempt is closed; choose another channel")

    notify_url = f"{config.public_base_url}/notify/{channel.value}/{account.id}"
    if channel is Channel.WECHAT and urlsplit(notify_url).scheme != "https":
        raise ProviderError("WeChat Pay requires an https public_base_url")

    is_retry = attempt is not None
    if attempt is None:
        candidate = PaymentAttempt(
            id=new_attempt_id(),
            order_id=order.id,
            channel=channel,
            account_id=account.id,
            out_trade_no=new_out_trade_no(),
            status=AttemptStatus.PENDING,
            amount_fen=order.amount_fen,
            currency=order.currency,
            qr_code_url=None,
            form_html=None,
            redirect_url=None,
            transaction_id=None,
            channel_status=None,
            created_at=current,
            updated_at=current,
            paid_at=None,
            closed_at=None,
        )
        try:
            with db.transaction() as conn:
                insert_attempt(conn, candidate)
            attempt = candidate
        except sqlite3.IntegrityError:
            with db.read() as conn:
                attempt = get_attempt_for_order_channel(conn, order.id, channel)
            if attempt is None:
                raise
            is_retry = True

    if attempt.qr_code_url or attempt.form_html or attempt.redirect_url:
        return AttemptStartResult(order=order, attempt=attempt)

    request = CreatePaymentRequest(
        out_trade_no=attempt.out_trade_no,
        amount_fen=order.amount_fen,
        description=order.subject,
        notify_url=notify_url,
        return_url=(
            f"{config.public_base_url}/pay/{order.cashier_token}/return/alipay"
            if channel is Channel.ALIPAY
            else None
        ),
        expires_at=order.expires_at,
    )
    try:
        gateway = gateways.get(account)
        result = await gateway.create_payment(request)
    except (PaymentNetworkError, PaymentSignatureError):
        _set_attempt_channel_status(db, attempt.id, "create_unknown", current)
        return AttemptStartResult(
            order=order, attempt=_reload_attempt(db, attempt.id), ambiguous=True
        )
    except PaymentValidationError as exc:
        _set_attempt_channel_status(db, attempt.id, "create_rejected", current)
        raise ProviderError("payment provider rejected the payment request") from exc
    except PaymentConfigError as exc:
        _set_attempt_channel_status(db, attempt.id, "config_error", current)
        raise ProviderError(f"channel account {account.id} is not configured") from exc
    except PaymentProviderError as exc:
        if is_retry or (exc.http_status is not None and exc.http_status >= 500):
            _set_attempt_channel_status(db, attempt.id, "create_unknown", current)
            return AttemptStartResult(
                order=order, attempt=_reload_attempt(db, attempt.id), ambiguous=True
            )
        _fail_attempt(db, attempt.id, current)
        raise ProviderError("payment provider rejected the request") from exc
    except PaymentError:
        _set_attempt_channel_status(db, attempt.id, "create_unknown", current)
        return AttemptStartResult(
            order=order, attempt=_reload_attempt(db, attempt.id), ambiguous=True
        )

    if result.amount_fen != order.amount_fen or result.out_trade_no != attempt.out_trade_no:
        raise ProviderError("payment provider returned a mismatched order")

    with db.transaction() as conn:
        refreshed_order = get_order(conn, order.id)
        refreshed_attempt = get_attempt(conn, attempt.id)
        if refreshed_order is None or refreshed_attempt is None:
            raise NotFoundError("payment attempt not found")
        if result.status is PaymentStatus.PAID:
            refreshed_order, refreshed_attempt, _ = mark_paid(
                conn,
                refreshed_order,
                refreshed_attempt,
                transaction_id=None,
                paid_at=current,
                channel_status="create_paid",
                now=current,
            )
        else:
            update_attempt_created(
                conn,
                attempt.id,
                qr_code_url=result.qr_code_url,
                form_html=result.form_html,
                redirect_url=result.redirect_url,
                channel_status="created",
                now=current,
            )
            refreshed_attempt = get_attempt(conn, attempt.id)
    if refreshed_attempt is None:
        raise NotFoundError("payment attempt not found")
    return AttemptStartResult(order=refreshed_order, attempt=refreshed_attempt)


def _reload_attempt(db: Database, attempt_id: str) -> PaymentAttempt:
    with db.read() as conn:
        attempt = get_attempt(conn, attempt_id)
    if attempt is None:
        raise NotFoundError("payment attempt not found")
    return attempt


def _set_attempt_channel_status(db: Database, attempt_id: str, status: str, now: datetime) -> None:
    with db.transaction() as conn:
        update_attempt_created(
            conn,
            attempt_id,
            qr_code_url=None,
            form_html=None,
            redirect_url=None,
            channel_status=status,
            now=now,
        )


def _fail_attempt(db: Database, attempt_id: str, now: datetime) -> None:
    from .repo_orders import update_attempt_state

    with db.transaction() as conn:
        update_attempt_state(
            conn,
            attempt_id,
            status=AttemptStatus.FAILED,
            channel_status="create_rejected",
            now=now,
        )


async def refresh_order(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    project_id: str,
    payment_id: str,
    now: datetime | None = None,
) -> RefreshReport:
    current = now or utcnow()
    order = get_order_for_request(db, project_id, payment_id)
    errors: list[str] = []
    with db.read() as conn:
        attempts = [a for a in list_attempts(conn, order.id) if a.status is AttemptStatus.PENDING]
    for attempt in attempts:
        with db.read() as conn:
            account = get_channel_account(conn, attempt.account_id)
        if account is None:
            errors.append(f"{attempt.channel.value}: channel account is missing")
            continue
        try:
            gateway = gateways.get(account)
            result = await gateway.query_payment(attempt.out_trade_no)
        except PaymentNotFoundError:
            continue
        except PaymentError as exc:
            errors.append(f"{attempt.channel.value}: {type(exc).__name__}")
            continue
        try:
            if result.amount_fen != order.amount_fen:
                errors.append(f"{attempt.channel.value}: provider amount mismatch")
                continue
            with db.transaction() as conn:
                current_order = get_order(conn, order.id)
                current_attempt = get_attempt(conn, attempt.id)
                if current_order is None or current_attempt is None:
                    continue
                if result.status is PaymentStatus.PAID:
                    mark_paid(
                        conn,
                        current_order,
                        current_attempt,
                        transaction_id=result.transaction_id,
                        paid_at=result.paid_at or current,
                        channel_status=result.channel_status,
                        now=current,
                    )
                elif result.status is PaymentStatus.CLOSED:
                    mark_attempt_closed(
                        conn, current_attempt, channel_status=result.channel_status, now=current
                    )
                    sync_order_status(conn, current_order, current)
                elif result.status is PaymentStatus.FAILED:
                    mark_attempt_failed(
                        conn, current_attempt, channel_status=result.channel_status, now=current
                    )
                    sync_order_status(conn, current_order, current)
                else:
                    update_attempt_created(
                        conn,
                        current_attempt.id,
                        qr_code_url=None,
                        form_html=None,
                        redirect_url=None,
                        channel_status=result.channel_status,
                        now=current,
                    )
        except PaymentError as exc:
            errors.append(f"{attempt.channel.value}: {type(exc).__name__}")
    with db.read() as conn:
        refreshed = get_order(conn, order.id)
    if refreshed is None:
        raise NotFoundError("payment not found")
    return RefreshReport(order=refreshed, errors=tuple(errors))


async def close_order(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    project_id: str,
    payment_id: str,
    now: datetime | None = None,
) -> CloseReport:
    current = now or utcnow()
    order = get_order_for_request(db, project_id, payment_id)
    if order.paid_at is not None:
        raise ConflictError("paid orders cannot be closed; create a refund instead")
    if order.status in {OrderStatus.CLOSED, OrderStatus.EXPIRED}:
        return CloseReport(order=order)
    errors: list[str] = []
    with db.read() as conn:
        attempts = [a for a in list_attempts(conn, order.id) if a.status is AttemptStatus.PENDING]
    for attempt in attempts:
        with db.read() as conn:
            account = get_channel_account(conn, attempt.account_id)
        if account is None:
            errors.append(f"{attempt.channel.value}: channel account is missing")
            continue
        try:
            gateway = gateways.get(account)
            await gateway.close_payment(attempt.out_trade_no)
        except PaymentNotFoundError:
            pass
        except PaymentError as exc:
            errors.append(f"{attempt.channel.value}: {type(exc).__name__}")
            continue
        with db.transaction() as conn:
            latest = get_attempt(conn, attempt.id)
            if latest is not None:
                mark_attempt_closed(conn, latest, channel_status="closed", now=current)
    with db.transaction() as conn:
        current_order = get_order(conn, order.id)
        if current_order is None:
            raise NotFoundError("payment not found")
        remaining = [a for a in list_attempts(conn, order.id) if a.status is AttemptStatus.PENDING]
        if not remaining:
            from .repo_orders import mark_order_closed

            if mark_order_closed(conn, order.id, current):
                refreshed = get_order(conn, order.id) or current_order
                enqueue_payment_event(
                    conn,
                    order=refreshed,
                    attempt=latest_attempt(conn, order.id),
                    event_type=EventType.PAYMENT_CLOSED,
                    occurred_at=current,
                    now=current,
                )
        refreshed = get_order(conn, order.id)
    if refreshed is None:
        raise NotFoundError("payment not found")
    return CloseReport(order=refreshed, errors=tuple(errors))


def expire_orders(
    db: Database, config: ServiceConfig, *, now: datetime | None = None, limit: int = 100
) -> int:
    current = now or utcnow()
    expired = 0
    with db.read() as conn:
        due = list_orders_due_for_expiry(conn, current, limit)
    for order in due:
        with db.transaction() as conn:
            if not expire_order(conn, order.id, current):
                continue
            refreshed = get_order(conn, order.id)
            if refreshed is None:
                continue
            enqueue_payment_event(
                conn,
                order=refreshed,
                attempt=latest_attempt(conn, order.id),
                event_type=EventType.PAYMENT_EXPIRED,
                occurred_at=current,
                now=current,
            )
            expired += 1
    return expired


def status_change_for_admin(
    db: Database, order: PaymentOrder, now: datetime | None = None
) -> StatusChange:
    current = now or utcnow()
    with db.transaction() as conn:
        return sync_order_status(conn, order, current)


def order_cashier_project(db: Database, order: PaymentOrder) -> Project | None:
    with db.read() as conn:
        return get_project(conn, order.project_id)


def payment_ack_status(db: Database, order_id: str) -> str | None:
    from .repo_outbox import list_events_for_order

    with db.read() as conn:
        for event in list_events_for_order(conn, order_id):
            if event.event_type is EventType.PAYMENT_SUCCEEDED:
                return event.ack_status.value
    return None


def order_for_cashier(
    db: Database, cashier_token: str
) -> tuple[PaymentOrder, Project, list[PaymentAttempt]]:
    with db.read() as conn:
        order = get_order_by_token(conn, cashier_token)
        if order is None:
            raise NotFoundError("cashier link not found")
        project = get_project(conn, order.project_id)
        if project is None:
            raise NotFoundError("project not found")
        attempts = list_attempts(conn, order.id)
    return order, project, attempts


def attempt_urls(config: ServiceConfig, token: str, attempt: PaymentAttempt) -> dict[str, str]:
    base = f"{config.public_base_url}/pay/{token}"
    return {
        "qr_svg": f"{base}/qr.svg",
        "alipay_form": f"{base}/alipay-form",
        "status": f"{base}/status",
    }
