import threading
from typing import Any

import pytest

from pypay import (
    AsyncPaymentClient,
    Channel,
    ClosePaymentResult,
    CreatePaymentRequest,
    CreatePaymentResult,
    NotificationEvent,
    PaymentNotification,
    PaymentStatus,
    QueryPaymentResult,
    QueryRefundResult,
    RefundRequest,
    RefundResult,
    RefundStatus,
)


class StubClient:
    channel = Channel.WECHAT

    def __init__(self) -> None:
        self.threads: list[int] = []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.threads.append(threading.get_ident())
        self.calls.append((name, args, kwargs))

    def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        self._record("create_payment", (request,), {})
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            qr_code_url="weixin://test",
        )

    def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        self._record("query_payment", (out_trade_no,), {})
        return QueryPaymentResult(
            channel=self.channel,
            out_trade_no=out_trade_no,
            amount_fen=100,
            status=PaymentStatus.PAID,
        )

    def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        self._record("close_payment", (out_trade_no,), {})
        return ClosePaymentResult(
            channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
        )

    def refund(self, request: RefundRequest) -> RefundResult:
        self._record("refund", (request,), {})
        return RefundResult(
            channel=self.channel,
            out_refund_no=request.out_refund_no,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            total_amount_fen=request.total_amount_fen,
            status=RefundStatus.SUCCEEDED,
        )

    def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        self._record("query_refund", (out_refund_no, out_trade_no), {})
        return QueryRefundResult(
            channel=self.channel,
            out_refund_no=out_refund_no,
            out_trade_no=out_trade_no or "ORDER1",
            amount_fen=100,
            status=RefundStatus.SUCCEEDED,
        )

    def parse_notification(
        self,
        headers: Any,
        body: bytes | str,
        **kwargs: Any,
    ) -> PaymentNotification:
        self._record("parse_notification", (headers, body), kwargs)
        return PaymentNotification(
            channel=self.channel,
            event_type=NotificationEvent.PAYMENT_SUCCEEDED,
            notification_id="notify-1",
            out_trade_no="ORDER1",
            amount_fen=100,
            currency="CNY",
            status=PaymentStatus.PAID,
        )


@pytest.mark.asyncio
async def test_async_client_runs_sdk_calls_in_worker_thread() -> None:
    stub = StubClient()
    client = AsyncPaymentClient(stub)  # type: ignore[arg-type]
    loop_thread = threading.get_ident()
    result = await client.create_payment(
        CreatePaymentRequest(out_trade_no="ORDER1", amount_fen=100, description="test")
    )
    assert result.status is PaymentStatus.PENDING
    assert stub.threads[0] != loop_thread


@pytest.mark.asyncio
async def test_async_client_delegates_every_method() -> None:
    stub = StubClient()
    client = AsyncPaymentClient(stub)  # type: ignore[arg-type]

    await client.query_payment("ORDER1")
    await client.close_payment("ORDER1")
    await client.refund(
        RefundRequest(
            out_refund_no="REFUND1",
            out_trade_no="ORDER1",
            amount_fen=100,
            total_amount_fen=100,
        )
    )
    await client.query_refund("REFUND1", "ORDER1")
    await client.parse_notification(
        {"wechatpay-signature": "x"},
        b"{}",
        expected_out_trade_no="ORDER1",
        expected_amount_fen=100,
    )

    names = [name for name, _, _ in stub.calls]
    assert names == [
        "query_payment",
        "close_payment",
        "refund",
        "query_refund",
        "parse_notification",
    ]
    assert stub.calls[3][1] == ("REFUND1", "ORDER1")
    assert stub.calls[4][2] == {
        "expected_out_trade_no": "ORDER1",
        "expected_amount_fen": 100,
        "expected_currency": "CNY",
    }


def test_async_client_exposes_channel_and_sync() -> None:
    stub = StubClient()
    client = AsyncPaymentClient(stub)  # type: ignore[arg-type]
    assert client.channel is Channel.WECHAT
    assert client.sync is stub
