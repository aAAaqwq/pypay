from __future__ import annotations

import re
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pypay import Channel

from .config import ChannelAccountSeed
from .deps import get_ctx, read_form, require_admin, require_admin_write, validate_csrf
from .errors import ServiceError
from .payments_service import (
    close_order,
    get_order_detail,
    refresh_order,
)
from .projects_service import (
    create_project,
    rotate_project_credentials,
    set_channel_account_state,
    set_project_status,
    update_project,
)
from .refunds_service import RefundInput, request_refund
from .repo_audit import insert_audit, list_audit
from .repo_notifications import list_notifications_for_order
from .repo_orders import count_orders_by_status, get_order, list_orders
from .repo_outbox import (
    count_events_by_status,
    get_event,
    list_deliveries,
    list_events_for_order,
    list_problem_events,
    manual_resend,
)
from .repo_projects import (
    get_channel_account,
    get_project,
    list_channel_accounts,
    list_project_channels,
    list_projects,
    upsert_channel_account,
)
from .security import AdminPrincipal
from .web_admin import (
    render_audit,
    render_channels,
    render_dashboard,
    render_order_detail,
    render_orders,
    render_project_created,
    render_project_detail,
    render_project_new,
    render_projects,
)

router = APIRouter(prefix="/admin", tags=["admin"])
_ACCOUNT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")


def _nonce(request: Request) -> str:
    return getattr(request.state, "csp_nonce", "")


def _csrf(request: Request) -> str:
    return request.cookies.get("pypay_admin_csrf") or getattr(request.state, "csrf_token", "")


def _audit(
    request: Request,
    principal: AdminPrincipal,
    action: str,
    *,
    project_id: str | None = None,
    order_id: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    context = get_ctx(request)
    with context.db.transaction() as conn:
        insert_audit(
            conn,
            actor=principal.username,
            role=principal.role,
            action=action,
            project_id=project_id,
            order_id=order_id,
            details=details,
            now=context.clock(),
        )


def _return_urls(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _channel_map(form) -> dict[Channel, str]:
    channels: dict[Channel, str] = {}
    for channel in (Channel.WECHAT, Channel.ALIPAY):
        value = str(form.get(f"account_{channel.value}") or "").strip()
        if value:
            channels[channel] = value
    return channels


def _load_order(request: Request, payment_id: str):
    context = get_ctx(request)
    with context.db.read() as conn:
        order = get_order(conn, payment_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return context, order


def _render_order_page(
    request: Request,
    principal: AdminPrincipal,
    payment_id: str,
    *,
    flash: str | None = None,
    error: str | None = None,
    refund_key: str | None = None,
) -> HTMLResponse:
    context, order = _load_order(request, payment_id)
    detail = get_order_detail(context.db, order)
    with context.db.read() as conn:
        notifications = list_notifications_for_order(conn, order.id)
        events = list_events_for_order(conn, order.id)
        deliveries = {event.event_id: list_deliveries(conn, event.event_id) for event in events}
        audit = list_audit(conn, order_id=order.id, limit=50)
    event_rows = [(event, deliveries.get(event.event_id, [])) for event in events]
    html = render_order_detail(
        principal=principal,
        nonce=_nonce(request),
        csrf=_csrf(request),
        order=order,
        project=detail.project,
        attempts=detail.attempts,
        refunds=detail.refunds,
        event_rows=event_rows,
        notifications=notifications,
        audit=audit,
        refund_key=refund_key or secrets.token_hex(8),
        flash=flash,
        error=error,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _render_project_page(
    request: Request,
    principal: AdminPrincipal,
    project_id: str,
    *,
    rotated=None,
    flash: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    context = get_ctx(request)
    with context.db.read() as conn:
        project = get_project(conn, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        accounts = list_channel_accounts(conn)
        selected_accounts = list_project_channels(conn, project.id)
        orders = list_orders(conn, project_id=project.id, limit=20)
    selected = {account.channel.value: account.id for account in selected_accounts}
    html = render_project_detail(
        principal=principal,
        nonce=_nonce(request),
        csrf=_csrf(request),
        project=project,
        accounts=accounts,
        selected=selected,
        orders=orders,
        rotated=rotated,
        flash=flash,
        error=error,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    context = get_ctx(request)
    with context.db.read() as conn:
        projects = list_projects(conn)
        orders_by_status = count_orders_by_status(conn)
        events_by_status = count_events_by_status(conn)
        recent_orders = list_orders(conn, limit=10)
        problem_events = list_problem_events(conn, limit=10)
    stats = {
        "project_count": len(projects),
        "pending_orders": orders_by_status.get("pending", 0),
        "paid_orders": orders_by_status.get("paid", 0),
        "dead_events": events_by_status.get("dead", 0),
    }
    return HTMLResponse(
        render_dashboard(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            stats=stats,
            recent_orders=recent_orders,
            problem_events=problem_events,
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/projects", response_class=HTMLResponse)
async def projects_page(
    request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    context = get_ctx(request)
    with context.db.read() as conn:
        projects = list_projects(conn)
    return HTMLResponse(
        render_projects(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            projects=projects,
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/projects/new", response_class=HTMLResponse)
async def project_new_page(
    request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    context = get_ctx(request)
    with context.db.read() as conn:
        accounts = list_channel_accounts(conn)
    return HTMLResponse(
        render_project_new(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            accounts=accounts,
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/projects")
async def project_create(
    request: Request, principal: AdminPrincipal = Depends(require_admin_write)
) -> Response:
    context = get_ctx(request)
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    form_values = {
        "project_id": str(form.get("project_id") or ""),
        "name": str(form.get("name") or ""),
        "notify_url": str(form.get("notify_url") or ""),
        "return_urls": str(form.get("return_urls") or ""),
    }
    try:
        created = create_project(
            context.db,
            context.box,
            context.config,
            project_id=form_values["project_id"],
            name=form_values["name"],
            notify_url=form_values["notify_url"],
            return_urls=_return_urls(form_values["return_urls"]),
            channels=_channel_map(form),
            now=context.clock(),
        )
    except ServiceError as exc:
        with context.db.read() as conn:
            accounts = list_channel_accounts(conn)
        return HTMLResponse(
            render_project_new(
                principal=principal,
                nonce=_nonce(request),
                csrf=_csrf(request),
                accounts=accounts,
                error=exc.message,
                form=form_values,
            ),
            status_code=exc.http_status,
            headers={"Cache-Control": "no-store"},
        )
    _audit(
        request,
        principal,
        "project.create",
        project_id=created.project.id,
        details={"name": created.project.name},
    )
    return HTMLResponse(
        render_project_created(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            created=created,
            base_url=context.config.public_base_url,
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(
    project_id: str, request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    return _render_project_page(request, principal, project_id)


@router.post("/projects/{project_id}/settings")
async def project_update(
    project_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context = get_ctx(request)
    try:
        update_project(
            context.db,
            context.config,
            project_id,
            name=str(form.get("name") or ""),
            notify_url=str(form.get("notify_url") or ""),
            return_urls=_return_urls(str(form.get("return_urls") or "")),
            channels=_channel_map(form),
            now=context.clock(),
        )
    except ServiceError as exc:
        return _render_project_page(request, principal, project_id, error=exc.message)
    _audit(request, principal, "project.update", project_id=project_id)
    return RedirectResponse(url=f"/admin/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/status")
async def project_status(
    project_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context = get_ctx(request)
    enabled = str(form.get("enabled") or "") == "1"
    try:
        set_project_status(context.db, project_id, enabled, now=context.clock())
    except ServiceError as exc:
        return _render_project_page(request, principal, project_id, error=exc.message)
    _audit(
        request,
        principal,
        "project.enable" if enabled else "project.disable",
        project_id=project_id,
    )
    return RedirectResponse(url=f"/admin/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/rotate")
async def project_rotate(
    project_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context = get_ctx(request)
    target = str(form.get("target") or "both")
    try:
        rotated = rotate_project_credentials(
            context.db,
            context.box,
            project_id,
            rotate_api=target in {"api", "both"},
            rotate_notify=target in {"notify", "both"},
            now=context.clock(),
        )
    except ServiceError as exc:
        return _render_project_page(request, principal, project_id, error=exc.message)
    _audit(
        request,
        principal,
        "project.rotate",
        project_id=project_id,
        details={"target": target},
    )
    return _render_project_page(request, principal, project_id, rotated=rotated)


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(
    request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    context = get_ctx(request)
    with context.db.read() as conn:
        accounts = list_channel_accounts(conn)
    return HTMLResponse(
        render_channels(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            accounts=accounts,
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/channels")
async def channel_create(
    request: Request, principal: AdminPrincipal = Depends(require_admin_write)
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    account_id = str(form.get("account_id") or "")
    try:
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ValueError("账户 ID 必须是小写字母开头的 3-32 位字符")
        channel = Channel(str(form.get("channel") or ""))
        name = str(form.get("name") or "").strip()
        env_prefix = str(form.get("env_prefix") or "").strip()
        if not name or not env_prefix:
            raise ValueError("显示名称和环境变量前缀不能为空")
        seed = ChannelAccountSeed(
            id=account_id,
            channel=channel,
            name=name,
            env_prefix=env_prefix,
            enabled=True,
        )
        context = get_ctx(request)
        with context.db.transaction() as conn:
            upsert_channel_account(conn, seed, context.clock())
        _audit(
            request,
            principal,
            "channel.upsert",
            details={"account_id": account_id, "channel": channel.value},
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        context = get_ctx(request)
        with context.db.read() as conn:
            accounts = list_channel_accounts(conn)
        return HTMLResponse(
            render_channels(
                principal=principal,
                nonce=_nonce(request),
                csrf=_csrf(request),
                accounts=accounts,
                error=str(exc),
            ),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.post("/channels/{account_id}/toggle")
async def channel_toggle(
    account_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context = get_ctx(request)
    with context.db.read() as conn:
        account = get_channel_account(conn, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="channel account not found")
    set_channel_account_state(context.db, account_id, not account.enabled, now=context.clock())
    _audit(
        request,
        principal,
        "channel.enable" if not account.enabled else "channel.disable",
        details={"account_id": account_id},
    )
    return RedirectResponse(url="/admin/channels", status_code=303)


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(
    request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    context = get_ctx(request)
    filters = {
        "project_id": request.query_params.get("project_id", ""),
        "status": request.query_params.get("status", ""),
        "business_order_no": request.query_params.get("business_order_no", ""),
    }
    with context.db.read() as conn:
        orders = list_orders(
            conn,
            project_id=filters["project_id"] or None,
            status=filters["status"] or None,
            business_order_no=filters["business_order_no"] or None,
            limit=100,
        )
        projects = list_projects(conn)
    return HTMLResponse(
        render_orders(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            orders=orders,
            projects=projects,
            filters=filters,
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/orders/{payment_id}", response_class=HTMLResponse)
async def order_detail_page(
    payment_id: str, request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    return _render_order_page(request, principal, payment_id)


@router.post("/orders/{payment_id}/refresh")
async def order_refresh(
    payment_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context, order = _load_order(request, payment_id)
    try:
        report = await refresh_order(
            context.db,
            context.config,
            context.gateways,
            project_id=order.project_id,
            payment_id=payment_id,
            now=context.clock(),
        )
    except ServiceError as exc:
        return _render_order_page(request, principal, payment_id, error=exc.message)
    _audit(
        request,
        principal,
        "order.refresh",
        project_id=order.project_id,
        order_id=payment_id,
        details={"errors": list(report.errors)},
    )
    return RedirectResponse(url=f"/admin/orders/{payment_id}", status_code=303)


@router.post("/orders/{payment_id}/close")
async def order_close(
    payment_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context, order = _load_order(request, payment_id)
    try:
        report = await close_order(
            context.db,
            context.config,
            context.gateways,
            project_id=order.project_id,
            payment_id=payment_id,
            now=context.clock(),
        )
    except ServiceError as exc:
        return _render_order_page(request, principal, payment_id, error=exc.message)
    _audit(
        request,
        principal,
        "order.close",
        project_id=order.project_id,
        order_id=payment_id,
        details={"errors": list(report.errors)},
    )
    return RedirectResponse(url=f"/admin/orders/{payment_id}", status_code=303)


@router.post("/orders/{payment_id}/resend")
async def order_resend(
    payment_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context, order = _load_order(request, payment_id)
    event_id = str(form.get("event_id") or "")
    with context.db.read() as conn:
        event = get_event(conn, event_id)
    if event is None or event.order_id != order.id:
        return _render_order_page(request, principal, payment_id, error="事件不存在")
    with context.db.transaction() as conn:
        manual_resend(conn, event_id, context.clock())
    _audit(
        request,
        principal,
        "outbox.resend",
        project_id=order.project_id,
        order_id=payment_id,
        details={"event_id": event_id},
    )
    return RedirectResponse(url=f"/admin/orders/{payment_id}", status_code=303)


@router.post("/orders/{payment_id}/refund")
async def order_refund(
    payment_id: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_write),
) -> Response:
    form = await read_form(request)
    validate_csrf(request, form.get("csrf_token"))
    context, order = _load_order(request, payment_id)
    submitted_key = str(form.get("idempotency_key") or "") or None
    try:
        amount_fen = int(str(form.get("amount_fen") or ""))
    except ValueError:
        return _render_order_page(
            request,
            principal,
            payment_id,
            error="退款金额必须是整数分",
            refund_key=submitted_key,
        )
    try:
        outcome = await request_refund(
            context.db,
            context.config,
            context.gateways,
            project_id=order.project_id,
            actor=f"admin:{principal.username}",
            payload=RefundInput(
                payment_id=payment_id,
                amount_fen=amount_fen,
                reason=str(form.get("reason") or "") or None,
                idempotency_key=submitted_key,
            ),
            now=context.clock(),
        )
    except ServiceError as exc:
        return _render_order_page(
            request,
            principal,
            payment_id,
            error=exc.message,
            refund_key=submitted_key,
        )
    _audit(
        request,
        principal,
        "refund.create",
        project_id=order.project_id,
        order_id=payment_id,
        details={
            "refund_id": outcome.refund.id,
            "amount_fen": outcome.refund.amount_fen,
            "ambiguous": outcome.ambiguous,
        },
    )
    return RedirectResponse(url=f"/admin/orders/{payment_id}", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request, principal: AdminPrincipal = Depends(require_admin)
) -> Response:
    context = get_ctx(request)
    with context.db.read() as conn:
        entries = list_audit(conn, limit=200)
    return HTMLResponse(
        render_audit(
            principal=principal,
            nonce=_nonce(request),
            csrf=_csrf(request),
            entries=entries,
        ),
        headers={"Cache-Control": "no-store"},
    )
