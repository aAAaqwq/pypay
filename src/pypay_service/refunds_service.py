from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pypay import (
    PaymentConfigError,
    PaymentError,
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentSignatureError,
    PaymentValidationError,
    RefundRequest,
    RefundStatus,
)

from .config import ServiceConfig
from .db import Database
from .enums import RefundState
from .errors import ConflictError, NotFoundError, ProviderError, ServiceError, ValidationError
from .gateways import GatewayRegistry
from .ids import new_out_refund_no, new_refund_id
from .models import PaymentAttempt, PaymentOrder, Refund
from .payment_state import apply_refund_failure, apply_refund_success, paid_attempt
from .repo_orders import get_attempt, get_order, get_order_for_project
from .repo_projects import get_channel_account
from .repo_refunds import (
    get_refund,
    get_refund_by_idempotency_key,
    get_refund_for_project,
    insert_refund,
    list_pending_refunds,
    list_refunds_for_order,
    sum_active_refunds,
)
from .timeutil import to_db, utcnow

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MAX_REASON_LENGTH = 80


@dataclass(frozen=True)
class RefundInput:
    payment_id: str
    amount_fen: int
    reason: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RefundOutcome:
    refund: Refund
    created: bool
    ambiguous: bool = False


@dataclass(frozen=True)
class RefundRefreshReport:
    refund: Refund
    errors: tuple[str, ...] = ()


def _validate_refund_input(payload: RefundInput) -> None:
    if isinstance(payload.amount_fen, bool) or not isinstance(payload.amount_fen, int):
        raise ValidationError("amount_fen must be an integer")
    if payload.amount_fen <= 0:
        raise ValidationError("amount_fen must be greater than zero")
    if payload.reason is not None:
        if not isinstance(payload.reason, str) or payload.reason != payload.reason.strip():
            raise ValidationError("reason must be plain text without edge whitespace")
        if len(payload.reason) > _MAX_REASON_LENGTH:
            raise ValidationError(f"reason must be at most {_MAX_REASON_LENGTH} characters")
    if payload.idempotency_key is not None and not _IDEMPOTENCY_RE.fullmatch(
        payload.idempotency_key
    ):
        raise ValidationError(
            "idempotency_key must be 1-64 characters from letters, digits, _ . : -"
        )


async def request_refund(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    project_id: str,
    actor: str,
    payload: RefundInput,
    now: datetime | None = None,
) -> RefundOutcome:
    current = now or utcnow()
    _validate_refund_input(payload)
    with db.transaction() as conn:
        order = get_order_for_project(conn, payload.payment_id, project_id)
        if order is None:
            raise NotFoundError("payment not found")
        if order.paid_at is None:
            raise ConflictError("payment has not succeeded yet")
        if payload.idempotency_key:
            existing = get_refund_by_idempotency_key(conn, project_id, payload.idempotency_key)
            if existing is not None:
                if existing.order_id != order.id or existing.amount_fen != payload.amount_fen:
                    raise ConflictError("idempotency_key was already used for another refund")
                return RefundOutcome(refund=existing, created=False)
        remaining = order.amount_fen - max(
            order.refunded_amount_fen, sum_active_refunds(conn, order.id)
        )
        if payload.amount_fen > remaining:
            raise ConflictError("refund amount exceeds the remaining refundable amount")
        attempt = paid_attempt(conn, order.id)
        if attempt is None:
            raise ConflictError("payment has no successful channel attempt")
        account = get_channel_account(conn, attempt.account_id)
        if account is None:
            raise ProviderError("channel account is missing")
        refund = Refund(
            id=new_refund_id(),
            order_id=order.id,
            attempt_id=attempt.id,
            project_id=project_id,
            out_refund_no=new_out_refund_no(),
            amount_fen=payload.amount_fen,
            total_amount_fen=order.amount_fen,
            reason=payload.reason,
            status=RefundState.PENDING,
            provider_refund_id=None,
            idempotency_key=payload.idempotency_key,
            requested_by=actor,
            created_at=current,
            updated_at=current,
            succeeded_at=None,
        )
        try:
            insert_refund(conn, refund)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("refund idempotency key is already in use") from exc

    return await _submit_refund(
        db,
        config,
        gateways,
        refund=refund,
        order=order,
        attempt=attempt,
        account=account,
        now=current,
    )


async def _submit_refund(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    refund: Refund,
    order: PaymentOrder,
    attempt: PaymentAttempt,
    account,
    now: datetime,
) -> RefundOutcome:
    notify_url = f"{config.public_base_url}/notify/{account.channel.value}/{account.id}"
    gateway = gateways.get(account)
    request = RefundRequest(
        out_refund_no=refund.out_refund_no,
        out_trade_no=attempt.out_trade_no,
        amount_fen=refund.amount_fen,
        total_amount_fen=refund.total_amount_fen,
        reason=refund.reason,
        notify_url=notify_url,
    )
    try:
        result = await gateway.refund(request)
    except (PaymentNetworkError, PaymentSignatureError):
        return RefundOutcome(refund=refund, created=True, ambiguous=True)
    except PaymentConfigError as exc:
        raise ProviderError("channel account is not configured") from exc
    except PaymentValidationError as exc:
        _mark_refund_failed(db, refund, order, attempt, now)
        raise ProviderError("payment provider rejected the refund request") from exc
    except PaymentProviderError as exc:
        if exc.http_status is not None and exc.http_status >= 500:
            return RefundOutcome(refund=refund, created=True, ambiguous=True)
        _mark_refund_failed(db, refund, order, attempt, now)
        raise ProviderError("payment provider rejected the refund request") from exc
    except PaymentError:
        return RefundOutcome(refund=refund, created=True, ambiguous=True)

    if result.amount_fen != refund.amount_fen or result.out_trade_no != attempt.out_trade_no:
        return RefundOutcome(refund=refund, created=True, ambiguous=True)

    if result.status is RefundStatus.SUCCEEDED:
        with db.transaction() as conn:
            current_order = get_order(conn, order.id)
            current_attempt = get_attempt(conn, attempt.id)
            current_refund = get_refund(conn, refund.id)
            if current_order and current_attempt and current_refund:
                current_refund, _, _ = apply_refund_success(
                    conn,
                    current_order,
                    current_attempt,
                    current_refund,
                    provider_refund_id=result.refund_id,
                    occurred_at=result.created_at or now,
                    now=now,
                )
                return RefundOutcome(refund=current_refund, created=True)
        return RefundOutcome(refund=refund, created=True, ambiguous=True)

    if result.status in {RefundStatus.FAILED, RefundStatus.CLOSED}:
        with db.transaction() as conn:
            current_order = get_order(conn, order.id)
            current_attempt = get_attempt(conn, attempt.id)
            current_refund = get_refund(conn, refund.id)
            if current_order and current_attempt and current_refund:
                current_refund, _ = apply_refund_failure(
                    conn,
                    current_order,
                    current_attempt,
                    current_refund,
                    status=(
                        RefundState.FAILED
                        if result.status is RefundStatus.FAILED
                        else RefundState.CLOSED
                    ),
                    provider_refund_id=result.refund_id,
                    occurred_at=now,
                    now=now,
                )
                return RefundOutcome(refund=current_refund, created=True)
        return RefundOutcome(refund=refund, created=True, ambiguous=True)

    return RefundOutcome(refund=refund, created=True, ambiguous=True)


def _mark_refund_failed(
    db: Database,
    refund: Refund,
    order: PaymentOrder,
    attempt: PaymentAttempt,
    now: datetime,
) -> None:
    with db.transaction() as conn:
        current_order = get_order(conn, order.id)
        current_attempt = get_attempt(conn, attempt.id)
        current_refund = get_refund(conn, refund.id)
        if current_order and current_attempt and current_refund:
            apply_refund_failure(
                conn,
                current_order,
                current_attempt,
                current_refund,
                status=RefundState.FAILED,
                provider_refund_id=None,
                occurred_at=now,
                now=now,
            )


def get_refund_for_request(db: Database, project_id: str, refund_id: str) -> Refund:
    with db.read() as conn:
        refund = get_refund_for_project(conn, refund_id, project_id)
    if refund is None:
        raise NotFoundError("refund not found")
    return refund


def serialize_refund(refund: Refund) -> dict[str, Any]:
    return {
        "refund_id": refund.id,
        "payment_id": refund.order_id,
        "out_refund_no": refund.out_refund_no,
        "amount_fen": refund.amount_fen,
        "total_amount_fen": refund.total_amount_fen,
        "reason": refund.reason,
        "status": refund.status.value,
        "provider_refund_id": refund.provider_refund_id,
        "created_at": to_db(refund.created_at),
        "succeeded_at": to_db(refund.succeeded_at) if refund.succeeded_at else None,
    }


async def refresh_refund(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    project_id: str,
    refund_id: str,
    now: datetime | None = None,
) -> RefundRefreshReport:
    current = now or utcnow()
    with db.read() as conn:
        refund = get_refund_for_project(conn, refund_id, project_id)
    if refund is None:
        raise NotFoundError("refund not found")
    if refund.status is not RefundState.PENDING:
        return RefundRefreshReport(refund=refund)
    with db.read() as conn:
        order = get_order(conn, refund.order_id)
        attempt = get_attempt(conn, refund.attempt_id)
        account = get_channel_account(conn, attempt.account_id) if attempt else None
    if order is None or attempt is None or account is None:
        return RefundRefreshReport(refund=refund, errors=("refund references missing records",))
    try:
        gateway = gateways.get(account)
        result = await gateway.query_refund(refund.out_refund_no, attempt.out_trade_no)
    except PaymentNotFoundError:
        return RefundRefreshReport(refund=refund)
    except PaymentError as exc:
        return RefundRefreshReport(refund=refund, errors=(type(exc).__name__,))

    if result.amount_fen != refund.amount_fen:
        return RefundRefreshReport(refund=refund, errors=("provider refund amount mismatch",))
    current_refund = refund
    with db.transaction() as conn:
        current_order = get_order(conn, refund.order_id)
        current_attempt = get_attempt(conn, refund.attempt_id)
        current_refund = get_refund(conn, refund_id)
        if not current_order or not current_attempt or not current_refund:
            return RefundRefreshReport(refund=refund, errors=("refund records disappeared",))
        if result.status is RefundStatus.SUCCEEDED:
            current_refund, _, _ = apply_refund_success(
                conn,
                current_order,
                current_attempt,
                current_refund,
                provider_refund_id=result.refund_id,
                occurred_at=result.succeeded_at or current,
                now=current,
            )
        elif result.status in {RefundStatus.FAILED, RefundStatus.CLOSED}:
            current_refund, _ = apply_refund_failure(
                conn,
                current_order,
                current_attempt,
                current_refund,
                status=(
                    RefundState.FAILED
                    if result.status is RefundStatus.FAILED
                    else RefundState.CLOSED
                ),
                provider_refund_id=result.refund_id,
                occurred_at=current,
                now=current,
            )
    return RefundRefreshReport(refund=current_refund)


async def reconcile_pending_refunds(
    db: Database,
    config: ServiceConfig,
    gateways: GatewayRegistry,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    current = now or utcnow()
    with db.read() as conn:
        pending = list_pending_refunds(conn)[:limit]
    reconciled = 0
    for refund in pending:
        try:
            report = await refresh_refund(
                db,
                config,
                gateways,
                project_id=refund.project_id,
                refund_id=refund.id,
                now=current,
            )
        except PaymentError:
            continue
        except ServiceError:
            continue
        if report.refund.status is not RefundState.PENDING:
            reconciled += 1
    return reconciled


def list_refunds_for_payment(db: Database, order_id: str) -> list[Refund]:
    with db.read() as conn:
        return list_refunds_for_order(conn, order_id)
