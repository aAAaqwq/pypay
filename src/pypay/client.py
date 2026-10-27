import asyncio
import os
from collections.abc import Mapping
from functools import partial
from typing import Any

from .channels.alipay import AlipayChannel
from .channels.wechat import WeChatChannel
from .config import AlipayConfig, PaymentConfig, WeChatPayConfig, config_from_env
from .enums import Channel
from .errors import PaymentConfigError
from .models import (
    ClosePaymentResult,
    CreatePaymentRequest,
    CreatePaymentResult,
    PaymentNotification,
    QueryPaymentResult,
    QueryRefundResult,
    RefundRequest,
    RefundResult,
)


class PaymentClient:
    """Synchronous facade bound to one channel and one merchant account.

    The payment SDKs are synchronous. In an async application use
    ``AsyncPaymentClient`` so calls run in a worker thread.
    """

    def __init__(self, channel: Channel | str, config: PaymentConfig) -> None:
        try:
            self._channel = Channel(channel)
        except ValueError as exc:
            raise PaymentConfigError(f"unknown channel: {channel}") from exc
        if self._channel is Channel.WECHAT:
            if not isinstance(config, WeChatPayConfig):
                raise PaymentConfigError("wechat channel requires WeChatPayConfig")
            self._adapter: WeChatChannel | AlipayChannel = WeChatChannel(config)
        else:
            if not isinstance(config, AlipayConfig):
                raise PaymentConfigError("alipay channel requires AlipayConfig")
            self._adapter = AlipayChannel(config)

    @property
    def channel(self) -> Channel:
        return self._channel

    @classmethod
    def from_env(cls, channel: Channel | str | None = None) -> "PaymentClient":
        selected = channel or os.environ.get("PYPAY_CHANNEL")
        if not selected:
            raise PaymentConfigError("PYPAY_CHANNEL is required when no channel is passed")
        return cls(selected, config_from_env(selected))

    def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        return self._adapter.create_payment(request)

    def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        return self._adapter.query_payment(out_trade_no)

    def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        return self._adapter.close_payment(out_trade_no)

    def refund(self, request: RefundRequest) -> RefundResult:
        return self._adapter.refund(request)

    def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        return self._adapter.query_refund(out_refund_no, out_trade_no)

    def parse_notification(
        self,
        headers: Mapping[str, str],
        body: bytes | str,
        *,
        expected_out_trade_no: str | None = None,
        expected_amount_fen: int | None = None,
        expected_currency: str = "CNY",
    ) -> PaymentNotification:
        notification = self._adapter.parse_notification(headers, body)
        notification.assert_matches(
            expected_out_trade_no=expected_out_trade_no,
            expected_amount_fen=expected_amount_fen,
            expected_currency=expected_currency,
        )
        return notification


class AsyncPaymentClient:
    """Async facade that runs the synchronous SDK calls in a worker thread."""

    def __init__(self, client: PaymentClient) -> None:
        self._client = client

    @property
    def channel(self) -> Channel:
        return self._client.channel

    @property
    def sync(self) -> PaymentClient:
        return self._client

    @classmethod
    def from_env(cls, channel: Channel | str | None = None) -> "AsyncPaymentClient":
        return cls(PaymentClient.from_env(channel))

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        return await asyncio.to_thread(self._client.create_payment, request)

    async def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        return await asyncio.to_thread(self._client.query_payment, out_trade_no)

    async def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        return await asyncio.to_thread(self._client.close_payment, out_trade_no)

    async def refund(self, request: RefundRequest) -> RefundResult:
        return await asyncio.to_thread(self._client.refund, request)

    async def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        return await asyncio.to_thread(self._client.query_refund, out_refund_no, out_trade_no)

    async def parse_notification(
        self,
        headers: Mapping[str, Any],
        body: bytes | str,
        *,
        expected_out_trade_no: str | None = None,
        expected_amount_fen: int | None = None,
        expected_currency: str = "CNY",
    ) -> PaymentNotification:
        call = partial(
            self._client.parse_notification,
            headers,
            body,
            expected_out_trade_no=expected_out_trade_no,
            expected_amount_fen=expected_amount_fen,
            expected_currency=expected_currency,
        )
        return await asyncio.to_thread(call)
