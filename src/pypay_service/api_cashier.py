from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from pypay import Channel, PaymentError

from .deps import get_ctx, read_form
from .errors import ConflictError, NotFoundError, ServiceError
from .payments_service import (
    expire_if_needed,
    order_for_cashier,
    payment_ack_status,
    start_cashier_attempt,
)
from .projects_service import get_project_channels
from .security import verify_cashier_form_token
from .timeutil import to_db
from .web_cashier import render_cashier, render_cashier_error, render_return_page

router = APIRouter(tags=["cashier"])


def _nonce(request: Request) -> str:
    return getattr(request.state, "csp_nonce", "")


def _form_token(master_key: bytes, token: str) -> str:
    from .security import cashier_form_token

    return cashier_form_token(master_key, token)


def _error_page(message: str, status_code: int, request: Request) -> HTMLResponse:
    return HTMLResponse(
        render_cashier_error(message, _nonce(request)),
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _load(token: str, request: Request):
    context = get_ctx(request)
    now = context.clock()
    order, project, attempts = order_for_cashier(context.db, token)
    order = expire_if_needed(context.db, order, now)
    channels = [
        account for account in get_project_channels(context.db, project.id) if account.enabled
    ]
    return context, now, order, project, attempts, channels


@router.get("/pay/{token}", response_class=HTMLResponse)
async def cashier(
    token: str,
    request: Request,
    attempt: str | None = Query(default=None),
) -> Response:
    try:
        context, now, order, project, attempts, channels = _load(token, request)
    except NotFoundError as exc:
        return _error_page(exc.message, 404, request)
    service_ack = payment_ack_status(context.db, order.id)
    html = render_cashier(
        order=order,
        project=project,
        attempts=attempts,
        channels=channels,
        service_ack=service_ack,
        form_token=_form_token(context.config.master_key, token),
        nonce=_nonce(request),
        now=now,
        selected_attempt_id=attempt,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/pay/{token}/start")
async def start_channel(token: str, request: Request) -> Response:
    context = get_ctx(request)
    form = await read_form(request)
    csrf = form.get("csrf_token")
    if not verify_cashier_form_token(
        context.config.master_key, token, str(csrf) if csrf is not None else None
    ):
        return _error_page("收银台表单校验失败，请重新打开页面。", 403, request)
    try:
        channel = Channel(str(form.get("channel") or ""))
    except ValueError:
        return _error_page("不支持的支付方式。", 400, request)
    try:
        result = await start_cashier_attempt(
            context.db,
            context.config,
            context.gateways,
            cashier_token=token,
            channel=channel,
            now=context.clock(),
        )
    except ConflictError:
        return RedirectResponse(url=f"/pay/{token}", status_code=303)
    except ServiceError as exc:
        return _error_page(exc.message, exc.http_status, request)
    except PaymentError:
        return _error_page("支付渠道暂时不可用，请稍后重试。", 502, request)
    return RedirectResponse(url=f"/pay/{token}?attempt={result.attempt.id}", status_code=303)


@router.get("/pay/{token}/status")
async def cashier_status(token: str, request: Request) -> JSONResponse:
    try:
        context, _now, order, _project, attempts, _channels = _load(token, request)
    except NotFoundError as exc:
        return JSONResponse({"error": exc.message}, status_code=404)
    return JSONResponse(
        {
            "status": order.status.value,
            "service_ack_status": payment_ack_status(context.db, order.id),
            "refunded_amount_fen": order.refunded_amount_fen,
            "expires_at": to_db(order.expires_at),
            "attempts": [
                {
                    "channel": attempt.channel.value,
                    "status": attempt.status.value,
                    "channel_status": attempt.channel_status,
                }
                for attempt in attempts
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pay/{token}/qr.svg")
async def cashier_qr(
    token: str,
    request: Request,
    attempt: str | None = Query(default=None),
) -> Response:
    try:
        _context, _now, _order, _project, attempts, _channels = _load(token, request)
    except NotFoundError as exc:
        return Response(exc.message, status_code=404, media_type="text/plain")
    selected = next((a for a in attempts if a.id == attempt), None)
    if selected is None or not selected.qr_code_url:
        return Response("payment QR code not found", status_code=404, media_type="text/plain")
    try:
        import segno
    except ImportError:
        return Response(
            "install pypay[service] for QR rendering", status_code=503, media_type="text/plain"
        )
    svg = segno.make(selected.qr_code_url, error="m").svg_inline(scale=4)
    if "xmlns=" not in svg:
        svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pay/{token}/alipay-form")
async def cashier_alipay_form(
    token: str,
    request: Request,
    attempt: str | None = Query(default=None),
) -> Response:
    try:
        _context, _now, _order, _project, attempts, _channels = _load(token, request)
    except NotFoundError as exc:
        return Response(exc.message, status_code=404, media_type="text/plain")
    selected = next((a for a in attempts if a.id == attempt), None)
    if selected is None or not selected.form_html:
        return Response("Alipay form not found", status_code=404, media_type="text/plain")
    return HTMLResponse(
        selected.form_html,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pay/{token}/return/alipay", response_class=HTMLResponse)
async def alipay_return(token: str, request: Request) -> Response:
    try:
        _context, _now, order, project, _attempts, _channels = _load(token, request)
    except NotFoundError as exc:
        return _error_page(exc.message, 404, request)
    return HTMLResponse(
        render_return_page(order, project, _nonce(request)),
        headers={"Cache-Control": "no-store"},
    )
