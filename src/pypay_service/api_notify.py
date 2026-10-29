from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from pypay import (
    Channel,
    PaymentError,
    PaymentSignatureError,
    PaymentValidationError,
)

from .callbacks_service import apply_channel_notification
from .deps import get_ctx, read_body
from .errors import NotFoundError, ServiceError, ValidationError
from .repo_projects import get_channel_account

logger = logging.getLogger("pypay_service.notify")

router = APIRouter(tags=["channel-callbacks"])


def _success(channel: Channel) -> Response:
    if channel is Channel.WECHAT:
        return JSONResponse({"code": "SUCCESS", "message": "成功"})
    return PlainTextResponse("success")


def _failure(channel: Channel, message: str, status_code: int) -> Response:
    if channel is Channel.WECHAT:
        return JSONResponse({"code": "FAIL", "message": message}, status_code=status_code)
    return PlainTextResponse("failure", status_code=status_code)


@router.post("/notify/{channel}/{account_id}")
async def channel_notification(channel: str, account_id: str, request: Request) -> Response:
    context = get_ctx(request)
    try:
        resolved = Channel(channel)
    except ValueError:
        return PlainTextResponse("unknown channel", status_code=404)
    with context.db.read() as conn:
        account = get_channel_account(conn, account_id)
    if account is None or account.channel is not resolved:
        logger.warning("notification for unknown channel account %s/%s", channel, account_id)
        return _failure(resolved, "unknown channel account", 404)
    body = await read_body(request)
    try:
        gateway = context.gateways.get(account)
        notification = await gateway.parse_notification(dict(request.headers), body)
    except PaymentSignatureError:
        logger.warning("rejected %s notification: signature", resolved.value)
        return _failure(resolved, "invalid signature", 401 if resolved is Channel.WECHAT else 400)
    except PaymentValidationError as exc:
        logger.warning("rejected %s notification: %s", resolved.value, type(exc).__name__)
        return _failure(resolved, "invalid notification", 400)
    except PaymentError as exc:
        logger.error("failed to parse %s notification: %s", resolved.value, type(exc).__name__)
        return _failure(resolved, "temporary failure", 500)
    except ServiceError as exc:
        logger.error("channel account %s is unavailable: %s", account_id, exc.code)
        return _failure(resolved, "temporary failure", 500)

    try:
        result = apply_channel_notification(context.db, notification, now=context.clock())
    except NotFoundError:
        logger.warning("notification for unknown order on %s/%s", channel, account_id)
        return _failure(resolved, "order not found", 404)
    except ValidationError:
        logger.warning("rejected %s notification: state validation", resolved.value)
        return _failure(resolved, "invalid notification", 400)
    except ServiceError as exc:
        logger.error("notification processing failed: %s", exc.code)
        return _failure(resolved, "temporary failure", 500)
    if result.duplicate:
        logger.info("duplicate %s notification %s", resolved.value, notification.notification_id)
    return _success(resolved)
