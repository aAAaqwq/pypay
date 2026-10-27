from collections.abc import Mapping
from typing import Any, Protocol

from .enums import Channel
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


class PaymentGateway(Protocol):
    channel: Channel

    def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult: ...

    def query_payment(self, out_trade_no: str) -> QueryPaymentResult: ...

    def close_payment(self, out_trade_no: str) -> ClosePaymentResult: ...

    def refund(self, request: RefundRequest) -> RefundResult: ...

    def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult: ...

    def parse_notification(
        self,
        headers: Mapping[str, str],
        body: bytes | str,
        *,
        expected_out_trade_no: str | None = None,
        expected_amount_fen: int | None = None,
        expected_currency: str = "CNY",
    ) -> PaymentNotification: ...


class AsyncPaymentGateway(Protocol):
    channel: Channel

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult: ...

    async def query_payment(self, out_trade_no: str) -> QueryPaymentResult: ...

    async def close_payment(self, out_trade_no: str) -> ClosePaymentResult: ...

    async def refund(self, request: RefundRequest) -> RefundResult: ...

    async def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult: ...

    async def parse_notification(
        self,
        headers: Mapping[str, Any],
        body: bytes | str,
        *,
        expected_out_trade_no: str | None = None,
        expected_amount_fen: int | None = None,
        expected_currency: str = "CNY",
    ) -> PaymentNotification: ...
