from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pypay import (
    PaymentError,
    PaymentNetworkError,
    PaymentSignatureError,
)
from pypay.base import AsyncPaymentGateway

from .api_admin import router as admin_router
from .api_cashier import router as cashier_router
from .api_merchant import router as merchant_router
from .api_notify import router as notify_router
from .config import ServiceConfig
from .context import AppContext
from .db import Database
from .errors import ServiceError
from .gateways import GatewayRegistry
from .notifier import HttpNotifier, Notifier
from .projects_service import sync_channel_accounts
from .security import SecretBox
from .timeutil import utcnow

logger = logging.getLogger("pypay_service")

_DEFAULT_CSP = (
    "default-src 'none'; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'nonce-{nonce}'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)
_ALIPAY_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "form-action 'self' https:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


def create_app(
    config: ServiceConfig | None = None,
    *,
    gateways: Mapping[str, AsyncPaymentGateway | Callable[[], AsyncPaymentGateway]] | None = None,
    notifier: Notifier | None = None,
    clock: Callable[[], datetime] | None = None,
    db: Database | None = None,
    migrate: bool = True,
) -> FastAPI:
    resolved_config = config or ServiceConfig.from_env()
    database = db or Database(resolved_config.database_file)
    if migrate:
        database.migrate_up()
    box = SecretBox(resolved_config.master_key)
    registry = GatewayRegistry(resolved_config, factories=gateways)
    http_notifier = notifier or HttpNotifier(resolved_config)
    clock_fn = clock or utcnow
    sync_channel_accounts(database, resolved_config, clock_fn())
    context = AppContext(
        config=resolved_config,
        db=database,
        box=box,
        gateways=registry,
        notifier=http_notifier,
        clock=clock_fn,
    )
    app = FastAPI(
        title="pypay unified payment service",
        version="0.2.0",
        docs_url="/docs" if resolved_config.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved_config.enable_docs else None,
    )
    app.state.ctx = context

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        nonce = secrets.token_urlsafe(18)
        request.state.csp_nonce = nonce
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "bad_request", "message": "invalid content-length"}},
                    status_code=400,
                )
            if length > resolved_config.request_max_bytes:
                return JSONResponse(
                    {"error": {"code": "payload_too_large", "message": "request body too large"}},
                    status_code=413,
                )
        csrf_cookie = request.cookies.get("pypay_admin_csrf")
        set_csrf = False
        if request.url.path.startswith("/admin") and request.method == "GET":
            if not csrf_cookie:
                csrf_cookie = secrets.token_urlsafe(32)
                set_csrf = True
            request.state.csrf_token = csrf_cookie
        response = await call_next(request)
        if set_csrf and csrf_cookie:
            response.set_cookie(
                "pypay_admin_csrf",
                csrf_cookie,
                max_age=8 * 3600,
                httponly=True,
                samesite="strict",
                secure=resolved_config.secure_cookies,
                path="/admin",
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if resolved_config.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith(("/pay", "/v1", "/admin", "/notify")):
            response.headers["Cache-Control"] = "no-store"
        csp = _ALIPAY_CSP if request.url.path.endswith("/alipay-form") else _DEFAULT_CSP
        response.headers["Content-Security-Policy"] = csp.format(nonce=nonce)
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.http_status,
        )

    @app.exception_handler(PaymentNetworkError)
    async def network_error_handler(request: Request, exc: PaymentNetworkError) -> JSONResponse:
        logger.warning("provider network error: %s", type(exc).__name__)
        return JSONResponse(
            {
                "error": {
                    "code": "provider_network_error",
                    "message": "payment state is unknown; query the original order before retrying",
                    "may_have_succeeded": exc.may_have_succeeded,
                }
            },
            status_code=502,
        )

    @app.exception_handler(PaymentError)
    async def payment_error_handler(request: Request, exc: PaymentError) -> JSONResponse:
        logger.warning("payment error: %s", type(exc).__name__)
        code = "provider_error"
        if isinstance(exc, PaymentSignatureError):
            code = "provider_signature_error"
        return JSONResponse(
            {"error": {"code": code, "message": "payment provider error"}},
            status_code=502,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled request error on %s", request.url.path)
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "internal server error"}},
            status_code=500,
        )

    app.include_router(merchant_router)
    app.include_router(cashier_router)
    app.include_router(notify_router)
    app.include_router(admin_router)

    @app.get("/health")
    async def health() -> dict[str, object]:
        with database.read() as conn:
            from .repo_projects import list_channel_accounts

            accounts = list_channel_accounts(conn)
        return {
            "status": "ok",
            "database": database.healthcheck(),
            "channels": [
                {"id": account.id, "channel": account.channel.value, "enabled": account.enabled}
                for account in accounts
            ],
        }

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        return {"status": "ready" if database.healthcheck() else "not_ready"}

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"service": "pypay", "status": "ok"}

    return app
