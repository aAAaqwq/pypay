from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from pypay import (
    AsyncPaymentClient,
    Channel,
    CreatePaymentRequest,
    PaymentConfigError,
    PaymentError,
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentNotification,
    PaymentSignatureError,
    PaymentStatus,
    PaymentValidationError,
)
from pypay.base import AsyncPaymentGateway

from .db import AmountMismatch, ChannelMismatch, Database, Order, OrderExists, OrderNotFound

logger = logging.getLogger("pypay.example")


class CreateOrderIn(BaseModel):
    amount_fen: int = Field(gt=0)
    description: str = Field(min_length=1, max_length=127)
    channel: Channel = Channel.WECHAT
    out_trade_no: str | None = Field(default=None, min_length=1, max_length=32)
    expires_in_minutes: int | None = Field(default=None, gt=0, le=1440)


class CreateOrderOut(BaseModel):
    out_trade_no: str
    channel: Channel
    amount_fen: int
    status: PaymentStatus
    qr_code_url: str | None = None
    form_html: str | None = None
    redirect_url: str | None = None


class OrderOut(BaseModel):
    out_trade_no: str
    channel: Channel
    amount_fen: int
    description: str
    status: PaymentStatus
    transaction_id: str | None = None
    refunded_amount_fen: int
    created_at: str
    paid_at: str | None = None
    fulfilled_at: str | None = None

    @classmethod
    def from_order(cls, order: Order) -> OrderOut:
        return cls(
            out_trade_no=order.out_trade_no,
            channel=Channel(order.channel),
            amount_fen=order.amount_fen,
            description=order.description,
            status=PaymentStatus(order.status),
            transaction_id=order.transaction_id,
            refunded_amount_fen=order.refunded_amount_fen,
            created_at=order.created_at,
            paid_at=order.paid_at,
            fulfilled_at=order.fulfilled_at,
        )


def gateways_from_env() -> dict[Channel, AsyncPaymentGateway]:
    raw = os.environ.get("PYPAY_CHANNELS") or os.environ.get("PYPAY_CHANNEL")
    if not raw:
        raise PaymentConfigError("set PYPAY_CHANNELS or PYPAY_CHANNEL")
    gateways: dict[Channel, AsyncPaymentGateway] = {}
    for name in (part.strip() for part in raw.split(",")):
        if not name:
            continue
        try:
            channel = Channel(name)
        except ValueError as exc:
            raise PaymentConfigError(f"unknown channel in PYPAY_CHANNELS: {name}") from exc
        gateways[channel] = AsyncPaymentClient.from_env(channel)
    if not gateways:
        raise PaymentConfigError("PYPAY_CHANNELS does not contain a usable channel")
    return gateways


def create_app(
    gateways: Mapping[Channel, AsyncPaymentGateway] | None = None,
    db_path: str | Path = "pypay_demo.db",
) -> FastAPI:
    resolved = dict(gateways) if gateways is not None else gateways_from_env()
    if not resolved:
        raise PaymentConfigError("at least one payment channel must be configured")

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    database = Database(db_file)

    app = FastAPI(title="pypay demo", version="0.1.0")
    app.state.gateways = resolved
    app.state.db = database

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "channels": sorted(c.value for c in resolved)}

    @app.post("/orders", response_model=CreateOrderOut, status_code=201)
    async def create_order(payload: CreateOrderIn, request: Request) -> CreateOrderOut:
        gateway = _gateway(request, payload.channel)
        out_trade_no = payload.out_trade_no or _new_order_no()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=payload.expires_in_minutes)
            if payload.expires_in_minutes
            else None
        )
        try:
            payment_request = CreatePaymentRequest(
                out_trade_no=out_trade_no,
                amount_fen=payload.amount_fen,
                description=payload.description,
                expires_at=expires_at,
            )
        except PaymentValidationError as exc:
            raise HTTPException(status_code=400, detail="invalid payment request") from exc

        try:
            database.create_order(
                out_trade_no=out_trade_no,
                channel=payload.channel.value,
                amount_fen=payload.amount_fen,
                description=payload.description,
            )
        except OrderExists as exc:
            raise HTTPException(status_code=409, detail="out_trade_no already exists") from exc

        try:
            result = await gateway.create_payment(payment_request)
        except PaymentValidationError as exc:
            logger.warning("create_payment validation failure for %s", out_trade_no)
            database.mark_failed(out_trade_no)
            raise HTTPException(status_code=400, detail="invalid payment request") from exc
        except PaymentNetworkError as exc:
            logger.warning("create_payment network failure for %s", out_trade_no)
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "payment state is unknown; query the order before retrying",
                    "out_trade_no": out_trade_no,
                    "may_have_succeeded": exc.may_have_succeeded,
                },
            ) from exc
        except PaymentError as exc:
            logger.warning("create_payment failed for %s: %s", out_trade_no, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={"message": "payment provider error", "out_trade_no": out_trade_no},
            ) from exc

        return CreateOrderOut(
            out_trade_no=result.out_trade_no,
            channel=result.channel,
            amount_fen=result.amount_fen,
            status=result.status,
            qr_code_url=result.qr_code_url,
            form_html=result.form_html,
            redirect_url=result.redirect_url,
        )

    @app.get("/orders/{out_trade_no}", response_model=OrderOut)
    async def get_order(out_trade_no: str, request: Request) -> OrderOut:
        order = _require_order(request, out_trade_no)
        return OrderOut.from_order(order)

    @app.post("/orders/{out_trade_no}/sync", response_model=OrderOut)
    async def sync_order(out_trade_no: str, request: Request) -> OrderOut:
        order = _require_order(request, out_trade_no)
        gateway = _gateway(request, Channel(order.channel))
        try:
            result = await gateway.query_payment(out_trade_no)
        except PaymentNotFoundError:
            return OrderOut.from_order(order)
        except PaymentNetworkError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "query failed; the order state is unchanged",
                    "out_trade_no": out_trade_no,
                    "may_have_succeeded": exc.may_have_succeeded,
                },
            ) from exc
        except PaymentError as exc:
            logger.warning("query_payment failed for %s: %s", out_trade_no, type(exc).__name__)
            raise HTTPException(status_code=502, detail="payment provider error") from exc

        try:
            if result.status is PaymentStatus.PAID:
                database.mark_paid_from_query(
                    out_trade_no=out_trade_no,
                    amount_fen=result.amount_fen,
                    transaction_id=result.transaction_id,
                    paid_at=result.paid_at.isoformat() if result.paid_at else None,
                )
            elif result.status is PaymentStatus.CLOSED:
                database.mark_closed_from_query(out_trade_no)
            elif result.status is PaymentStatus.REFUNDED:
                database.mark_refunded_from_query(out_trade_no)
        except AmountMismatch as exc:
            raise HTTPException(status_code=409, detail="provider amount mismatch") from exc

        refreshed = database.get_order(out_trade_no)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="order not found")
        return OrderOut.from_order(refreshed)

    @app.post("/notify/wechat")
    async def notify_wechat(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            gateway = _gateway(request, Channel.WECHAT)
            notification = await gateway.parse_notification(dict(request.headers), body)
            _apply_notification(request, notification)
        except PaymentSignatureError:
            logger.warning("rejected WeChat notification: signature")
            return JSONResponse({"code": "FAIL", "message": "invalid signature"}, status_code=401)
        except (
            PaymentValidationError,
            AmountMismatch,
            ChannelMismatch,
            PaymentNotFoundError,
        ) as exc:
            logger.warning("rejected WeChat notification: %s", type(exc).__name__)
            return JSONResponse(
                {"code": "FAIL", "message": "invalid notification"}, status_code=400
            )
        except OrderNotFound:
            logger.warning("WeChat notification for unknown order")
            return JSONResponse({"code": "FAIL", "message": "order not found"}, status_code=404)
        except PaymentError as exc:
            logger.warning("WeChat notification failed: %s", type(exc).__name__)
            return JSONResponse({"code": "FAIL", "message": "temporary failure"}, status_code=500)
        return JSONResponse({"code": "SUCCESS", "message": "成功"})

    @app.post("/notify/alipay")
    async def notify_alipay(request: Request) -> PlainTextResponse:
        body = await request.body()
        try:
            gateway = _gateway(request, Channel.ALIPAY)
            notification = await gateway.parse_notification(dict(request.headers), body)
            _apply_notification(request, notification)
        except PaymentSignatureError:
            logger.warning("rejected Alipay notification: signature")
            return PlainTextResponse("failure", status_code=400)
        except (
            PaymentValidationError,
            AmountMismatch,
            ChannelMismatch,
            PaymentNotFoundError,
        ) as exc:
            logger.warning("rejected Alipay notification: %s", type(exc).__name__)
            return PlainTextResponse("failure", status_code=400)
        except OrderNotFound:
            logger.warning("Alipay notification for unknown order")
            return PlainTextResponse("failure", status_code=404)
        except PaymentError as exc:
            logger.warning("Alipay notification failed: %s", type(exc).__name__)
            return PlainTextResponse("failure", status_code=500)
        return PlainTextResponse("success")

    @app.get("/return/alipay", response_class=HTMLResponse)
    async def alipay_return() -> str:
        return (
            "<!doctype html><meta charset='utf-8'><title>支付结果</title>"
            "<h1>支付结果待确认</h1>"
            "<p>请以支付宝异步通知或服务器主动查单结果为准，浏览器跳转不作为支付依据。</p>"
        )

    return app


def _gateway(request: Request, channel: Channel) -> AsyncPaymentGateway:
    gateway = request.app.state.gateways.get(channel)
    if gateway is None:
        raise HTTPException(status_code=503, detail=f"{channel.value} is not configured")
    return gateway


def _require_order(request: Request, out_trade_no: str) -> Order:
    order = request.app.state.db.get_order(out_trade_no)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


def _apply_notification(request: Request, notification: PaymentNotification) -> None:
    request.app.state.db.apply_notification(notification)


def _new_order_no() -> str:
    return "PYPAY" + secrets.token_hex(12).upper()
