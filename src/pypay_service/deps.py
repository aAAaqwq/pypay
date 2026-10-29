from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

from .context import AppContext
from .errors import AuthenticationError
from .models import ProjectAuth
from .projects_service import authenticate_project
from .security import (
    AdminPrincipal,
    authenticate_admin,
    constant_time_matches,
)


def get_ctx(request: Request) -> AppContext:
    context = getattr(request.app.state, "ctx", None)
    if context is None:
        raise RuntimeError("pypay service context is not initialized")
    return context


async def read_body(request: Request) -> bytes:
    cached = getattr(request.state, "raw_body", None)
    if cached is not None:
        return cached
    context = get_ctx(request)
    maximum = context.config.request_max_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
        if length > maximum:
            raise HTTPException(status_code=413, detail="request body too large")
    body = await request.body()
    if len(body) > maximum:
        raise HTTPException(status_code=413, detail="request body too large")
    request.state.raw_body = body
    return body


async def read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/x-www-form-urlencoded", ""}:
        raise HTTPException(
            status_code=415,
            detail="form submissions must be application/x-www-form-urlencoded",
        )
    body = await read_body(request)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="form body is not UTF-8") from exc
    values: dict[str, str] = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        if key in values:
            raise HTTPException(status_code=400, detail="duplicate form field")
        values[key] = value
    return values


async def require_project(request: Request) -> ProjectAuth:
    context = get_ctx(request)
    body = await read_body(request)
    try:
        return authenticate_project(
            context.db,
            context.box,
            context.config,
            key_id=request.headers.get("x-pypay-key-id"),
            timestamp=request.headers.get("x-pypay-timestamp"),
            nonce=request.headers.get("x-pypay-nonce"),
            signature=request.headers.get("x-pypay-signature"),
            method=request.method,
            path=request.url.path,
            body=body,
            now=context.clock(),
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": 'PypayKey realm="pypay merchant api"'},
        ) from exc


async def require_admin(request: Request) -> AdminPrincipal:
    context = get_ctx(request)
    principal = authenticate_admin(
        request.headers.get("authorization"), context.config.admin_credentials
    )
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="admin authentication required",
            headers={"WWW-Authenticate": 'Basic realm="pypay admin"'},
        )
    return principal


async def require_admin_write(request: Request) -> AdminPrincipal:
    principal = await require_admin(request)
    if not principal.can_write:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


def csrf_token(request: Request) -> str:
    token = request.cookies.get("pypay_admin_csrf")
    if not token:
        raise HTTPException(status_code=403, detail="missing CSRF cookie; reload the admin page")
    return token


def validate_csrf(request: Request, submitted: str | None) -> None:
    cookie = request.cookies.get("pypay_admin_csrf")
    if not constant_time_matches(cookie, submitted):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
