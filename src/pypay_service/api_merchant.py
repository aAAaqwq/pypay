from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .deps import get_ctx, require_project
from .errors import ValidationError
from .models import ProjectAuth
from .payments_service import (
    CreateOrderInput,
    close_order,
    create_order,
    get_order_by_business_order,
    get_order_for_request,
    refresh_order,
    serialize_payment,
)
from .refunds_service import (
    RefundInput,
    get_refund_for_request,
    refresh_refund,
    request_refund,
    serialize_refund,
)

router = APIRouter(prefix="/v1", tags=["merchant"])


class CreatePaymentIn(BaseModel):
    business_order_no: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=127)
    amount_fen: int = Field(gt=0, strict=True)
    currency: str = "CNY"
    expires_in_seconds: int = Field(default=1800, ge=60, le=86400)
    return_url: str | None = None
    metadata: dict[str, Any] | None = None


class RefundIn(BaseModel):
    payment_id: str = Field(min_length=1, max_length=64)
    amount_fen: int = Field(gt=0, strict=True)
    reason: str | None = Field(default=None, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=64)


@router.post("/payments", status_code=201)
async def create_payment(
    payload: CreatePaymentIn,
    request: Request,
    response: Response,
    project: ProjectAuth = Depends(require_project),
) -> dict[str, Any]:
    context = get_ctx(request)
    result = create_order(
        context.db,
        context.config,
        project.project,
        CreateOrderInput(
            business_order_no=payload.business_order_no,
            subject=payload.subject,
            amount_fen=payload.amount_fen,
            currency=payload.currency,
            expires_in_seconds=payload.expires_in_seconds,
            return_url=payload.return_url,
            metadata=payload.metadata or {},
        ),
        now=context.clock(),
    )
    response.status_code = 201 if result.created else 200
    body = serialize_payment(context.db, context.config, result.order)
    body["created"] = result.created
    return body


@router.get("/payments/by-business-order/{business_order_no}")
async def get_payment_by_business_order(
    business_order_no: str,
    request: Request,
    project: ProjectAuth = Depends(require_project),
) -> dict[str, Any]:
    context = get_ctx(request)
    order = get_order_by_business_order(context.db, project.project.id, business_order_no)
    return serialize_payment(context.db, context.config, order)


@router.get("/payments/{payment_id}")
async def get_payment(
    payment_id: str,
    request: Request,
    project: ProjectAuth = Depends(require_project),
) -> dict[str, Any]:
    context = get_ctx(request)
    order = get_order_for_request(context.db, project.project.id, payment_id)
    return serialize_payment(context.db, context.config, order)


@router.post("/payments/{payment_id}/refresh")
async def refresh_payment(
    payment_id: str,
    request: Request,
    project: ProjectAuth = Depends(require_project),
) -> dict[str, Any]:
    context = get_ctx(request)
    report = await refresh_order(
        context.db,
        context.config,
        context.gateways,
        project_id=project.project.id,
        payment_id=payment_id,
        now=context.clock(),
    )
    body = serialize_payment(context.db, context.config, report.order)
    if report.errors:
        body["refresh_errors"] = list(report.errors)
    return body


@router.post("/payments/{payment_id}/close")
async def close_payment(
    payment_id: str,
    request: Request,
    project: ProjectAuth = Depends(require_project),
) -> JSONResponse:
    context = get_ctx(request)
    report = await close_order(
        context.db,
        context.config,
        context.gateways,
        project_id=project.project.id,
        payment_id=payment_id,
        now=context.clock(),
    )
    body = serialize_payment(context.db, context.config, report.order)
    status = 202 if report.errors and report.order.status.value == "pending" else 200
    if report.errors:
        body["close_errors"] = list(report.errors)
    return JSONResponse(body, status_code=status)


@router.post("/refunds", status_code=201)
async def create_refund(
    payload: RefundIn,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    project: ProjectAuth = Depends(require_project),
) -> Response:
    context = get_ctx(request)
    idempotency_key = payload.idempotency_key or idempotency_key_header
    if (
        payload.idempotency_key
        and idempotency_key_header
        and payload.idempotency_key != idempotency_key_header
    ):
        raise ValidationError("body and header idempotency keys differ")
    outcome = await request_refund(
        context.db,
        context.config,
        context.gateways,
        project_id=project.project.id,
        actor=f"project:{project.project.id}",
        payload=RefundInput(
            payment_id=payload.payment_id,
            amount_fen=payload.amount_fen,
            reason=payload.reason,
            idempotency_key=idempotency_key,
        ),
        now=context.clock(),
    )
    body = serialize_refund(outcome.refund)
    body["created"] = outcome.created
    body["ambiguous"] = outcome.ambiguous
    status = 202 if outcome.ambiguous else (201 if outcome.created else 200)
    headers = {"X-Idempotent-Replay": "true"} if not outcome.created else None
    return JSONResponse(body, status_code=status, headers=headers)


@router.get("/refunds/{refund_id}")
async def get_refund(
    refund_id: str,
    request: Request,
    project: ProjectAuth = Depends(require_project),
) -> dict[str, Any]:
    context = get_ctx(request)
    refund = get_refund_for_request(context.db, project.project.id, refund_id)
    return serialize_refund(refund)


@router.post("/refunds/{refund_id}/refresh")
async def refresh_refund_route(
    refund_id: str,
    request: Request,
    project: ProjectAuth = Depends(require_project),
) -> dict[str, Any]:
    context = get_ctx(request)
    report = await refresh_refund(
        context.db,
        context.config,
        context.gateways,
        project_id=project.project.id,
        refund_id=refund_id,
        now=context.clock(),
    )
    body = serialize_refund(report.refund)
    if report.errors:
        body["refresh_errors"] = list(report.errors)
    return body
