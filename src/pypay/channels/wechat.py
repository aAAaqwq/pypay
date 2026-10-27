import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from wechatpayv3 import WeChatPay, WeChatPayType

from ..config import WeChatPayConfig
from ..enums import Channel, NotificationEvent, PaymentStatus, RefundStatus
from ..errors import (
    PaymentNetworkError,
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentSignatureError,
    PaymentValidationError,
)
from ..models import (
    ClosePaymentResult,
    CreatePaymentRequest,
    CreatePaymentResult,
    PaymentNotification,
    QueryPaymentResult,
    QueryRefundResult,
    RefundRequest,
    RefundResult,
)

_OUT_TRADE_NO_RE = re.compile(r"^[0-9A-Za-z_\-|*@]{1,32}$")
_OUT_REFUND_NO_RE = re.compile(r"^[0-9A-Za-z_\-|*@]{1,64}$")
_NOT_FOUND_CODES = {"ORDER_NOT_EXIST", "RESOURCE_NOT_EXIST", "RESOURCE_NOT_EXISTS"}
_TRADE_STATE_MAP = {
    "SUCCESS": PaymentStatus.PAID,
    "REFUND": PaymentStatus.REFUNDED,
    "NOTPAY": PaymentStatus.PENDING,
    "USERPAYING": PaymentStatus.PENDING,
    "CLOSED": PaymentStatus.CLOSED,
    "REVOKED": PaymentStatus.CLOSED,
    "PAYERROR": PaymentStatus.FAILED,
}
_REFUND_STATUS_MAP = {
    "SUCCESS": RefundStatus.SUCCEEDED,
    "PROCESSING": RefundStatus.PENDING,
    "CLOSED": RefundStatus.CLOSED,
    "ABNORMAL": RefundStatus.FAILED,
}


def _validate_out_trade_no(value: str) -> None:
    if not _OUT_TRADE_NO_RE.fullmatch(value):
        raise PaymentValidationError(
            "out_trade_no must contain only digits, letters, _ - | * @ and be at most 32 characters"
        )


def _validate_out_refund_no(value: str) -> None:
    if not _OUT_REFUND_NO_RE.fullmatch(value):
        raise PaymentValidationError(
            "out_refund_no must contain only digits, letters, _ - | * @ and be at most 64 characters"
        )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaymentValidationError("provider returned an invalid timestamp") from exc


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaymentProviderError(f"provider response is missing a valid {field}")
    return value


class WeChatChannel:
    channel = Channel.WECHAT

    def __init__(self, config: WeChatPayConfig) -> None:
        self._config = config
        self._sdk = WeChatPay(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=config.mchid,
            private_key=config.private_key,
            cert_serial_no=config.cert_serial_no,
            appid=config.appid,
            apiv3_key=config.apiv3_key,
            notify_url=config.notify_url,
            public_key=config.public_key,
            public_key_id=config.public_key_id,
            timeout=(config.timeout, config.timeout),
        )

    def _notify_url(self, override: str | None) -> str:
        url = override or self._config.notify_url
        if urlparse(url).scheme != "https":
            raise PaymentValidationError("WeChat Pay notify_url must use https")
        return url

    def _call(self, action: Any, **kwargs: Any) -> tuple[int, Any]:
        try:
            return action(**kwargs)
        except requests.exceptions.Timeout as exc:
            raise PaymentNetworkError(
                "WeChat Pay request timed out; query by out_trade_no before treating it as failed"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise PaymentNetworkError(
                "WeChat Pay network error; query by out_trade_no before treating it as failed"
            ) from exc
        except Exception as exc:
            message = str(exc)
            if (
                "failed to verify the signature" in message
                or "does not support this algorithm" in message
            ):
                raise PaymentSignatureError(
                    "WeChat Pay response signature verification failed"
                ) from exc
            raise PaymentProviderError(message or "WeChat Pay request failed") from exc

    def _decode(self, text: Any) -> dict[str, Any]:
        if not text:
            return {}
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PaymentProviderError("WeChat Pay returned non-UTF-8 data") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PaymentProviderError("WeChat Pay returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise PaymentProviderError("WeChat Pay returned an unexpected response shape")
        return data

    def _raise_for_status(self, status: int, data: Mapping[str, Any]) -> None:
        if 200 <= status < 300:
            return
        code = data.get("code")
        message = data.get("message") or f"WeChat Pay returned HTTP {status}"
        if status == 404 or code in _NOT_FOUND_CODES:
            raise PaymentNotFoundError(
                str(message), provider_code=str(code) if code else None, http_status=status
            )
        raise PaymentProviderError(
            str(message), provider_code=str(code) if code else None, http_status=status
        )

    def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        _validate_out_trade_no(request.out_trade_no)
        kwargs: dict[str, Any] = {
            "description": request.description,
            "out_trade_no": request.out_trade_no,
            "amount": {"total": request.amount_fen, "currency": "CNY"},
            "notify_url": self._notify_url(request.notify_url),
            "pay_type": WeChatPayType.NATIVE,
        }
        if request.expires_at is not None:
            kwargs["time_expire"] = request.expires_at.isoformat(timespec="seconds")
        if request.attach is not None:
            kwargs["attach"] = request.attach
        status, text = self._call(self._sdk.pay, **kwargs)
        data = self._decode(text)
        self._raise_for_status(status, data)
        qr_code_url = data.get("code_url")
        if not isinstance(qr_code_url, str) or not qr_code_url:
            raise PaymentProviderError("WeChat Pay response is missing code_url")
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            qr_code_url=qr_code_url,
            expires_at=request.expires_at,
        )

    def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        _validate_out_trade_no(out_trade_no)
        status, text = self._call(self._sdk.query, out_trade_no=out_trade_no)
        data = self._decode(text)
        self._raise_for_status(status, data)
        return self._query_result(data, out_trade_no)

    def _query_result(
        self, data: Mapping[str, Any], expected_out_trade_no: str
    ) -> QueryPaymentResult:
        returned_order = data.get("out_trade_no")
        if returned_order != expected_out_trade_no:
            raise PaymentProviderError("WeChat Pay returned a mismatched out_trade_no")
        amount = data.get("amount")
        if not isinstance(amount, Mapping):
            raise PaymentProviderError("WeChat Pay response is missing amount")
        total = _positive_int(amount.get("total"), "amount.total")
        currency = amount.get("currency", "CNY")
        if currency != "CNY":
            raise PaymentValidationError("WeChat Pay returned a non-CNY currency")
        state = str(data.get("trade_state") or "")
        return QueryPaymentResult(
            channel=self.channel,
            out_trade_no=expected_out_trade_no,
            amount_fen=total,
            status=_TRADE_STATE_MAP.get(state, PaymentStatus.UNKNOWN),
            transaction_id=data.get("transaction_id"),
            paid_at=_parse_time(data.get("success_time")),
            channel_status=state or None,
        )

    def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        _validate_out_trade_no(out_trade_no)
        status, text = self._call(self._sdk.close, out_trade_no=out_trade_no)
        data = self._decode(text)
        if 200 <= status < 300:
            return ClosePaymentResult(
                channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
            )
        if data.get("code") == "ORDER_CLOSED":
            return ClosePaymentResult(
                channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
            )
        self._raise_for_status(status, data)
        raise PaymentProviderError("WeChat Pay close request failed")

    def refund(self, request: RefundRequest) -> RefundResult:
        _validate_out_trade_no(request.out_trade_no)
        _validate_out_refund_no(request.out_refund_no)
        kwargs: dict[str, Any] = {
            "out_refund_no": request.out_refund_no,
            "out_trade_no": request.out_trade_no,
            "amount": {
                "refund": request.amount_fen,
                "total": request.total_amount_fen,
                "currency": "CNY",
            },
            "notify_url": self._notify_url(request.notify_url),
        }
        if request.reason is not None:
            kwargs["reason"] = request.reason
        status, text = self._call(self._sdk.refund, **kwargs)
        data = self._decode(text)
        self._raise_for_status(status, data)
        if data.get("out_refund_no") != request.out_refund_no:
            raise PaymentProviderError("WeChat Pay returned a mismatched out_refund_no")
        if data.get("out_trade_no") != request.out_trade_no:
            raise PaymentProviderError("WeChat Pay returned a mismatched out_trade_no")
        amount = data.get("amount")
        if not isinstance(amount, Mapping):
            raise PaymentProviderError("WeChat Pay refund response is missing amount")
        refund_amount = _positive_int(amount.get("refund"), "amount.refund")
        total_amount = _positive_int(amount.get("total"), "amount.total")
        currency = amount.get("currency", "CNY")
        if currency != "CNY":
            raise PaymentValidationError("WeChat Pay returned a non-CNY currency")
        refund_status = str(data.get("status") or "")
        return RefundResult(
            channel=self.channel,
            out_refund_no=request.out_refund_no,
            out_trade_no=request.out_trade_no,
            amount_fen=refund_amount,
            total_amount_fen=total_amount,
            status=_REFUND_STATUS_MAP.get(refund_status, RefundStatus.UNKNOWN),
            refund_id=data.get("refund_id"),
            created_at=_parse_time(data.get("create_time")),
        )

    def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        _validate_out_refund_no(out_refund_no)
        status, text = self._call(self._sdk.query_refund, out_refund_no=out_refund_no)
        data = self._decode(text)
        self._raise_for_status(status, data)
        if data.get("out_refund_no") != out_refund_no:
            raise PaymentProviderError("WeChat Pay returned a mismatched out_refund_no")
        returned_order = data.get("out_trade_no")
        if not isinstance(returned_order, str) or not returned_order:
            raise PaymentProviderError("WeChat Pay response is missing out_trade_no")
        if out_trade_no is not None and returned_order != out_trade_no:
            raise PaymentProviderError("WeChat Pay returned a mismatched out_trade_no")
        amount = data.get("amount")
        if not isinstance(amount, Mapping):
            raise PaymentProviderError("WeChat Pay refund response is missing amount")
        refund_amount = _positive_int(amount.get("refund"), "amount.refund")
        total_amount = amount.get("total")
        refund_status = str(data.get("status") or "")
        return QueryRefundResult(
            channel=self.channel,
            out_refund_no=out_refund_no,
            out_trade_no=returned_order,
            amount_fen=refund_amount,
            total_amount_fen=total_amount if isinstance(total_amount, int) else None,
            status=_REFUND_STATUS_MAP.get(refund_status, RefundStatus.UNKNOWN),
            refund_id=data.get("refund_id"),
            succeeded_at=_parse_time(data.get("success_time")),
        )

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes | str
    ) -> PaymentNotification:
        try:
            data = self._sdk.callback(headers, body)
        except Exception as exc:
            raise PaymentValidationError("WeChat Pay notification could not be parsed") from exc
        if not data or not isinstance(data, Mapping):
            raise PaymentSignatureError(
                "WeChat Pay notification signature verification failed; also check the APIv3 key"
            )
        notification_id = data.get("id")
        if not isinstance(notification_id, str) or not notification_id:
            raise PaymentValidationError("WeChat Pay notification is missing id")
        event_type = str(data.get("event_type") or "")
        resource = data.get("resource")
        if not isinstance(resource, Mapping):
            raise PaymentValidationError("WeChat Pay notification is missing resource")
        if resource.get("mchid") != self._config.mchid:
            raise PaymentValidationError("WeChat Pay notification merchant does not match")
        if event_type.startswith("TRANSACTION."):
            return self._payment_notification(notification_id, event_type, resource)
        if event_type.startswith("REFUND."):
            return self._refund_notification(notification_id, event_type, resource)
        raise PaymentValidationError(f"unsupported WeChat Pay notification event: {event_type}")

    def _payment_notification(
        self, notification_id: str, event_type: str, resource: Mapping[str, Any]
    ) -> PaymentNotification:
        if resource.get("appid") != self._config.appid:
            raise PaymentValidationError("WeChat Pay notification appid does not match")
        amount = resource.get("amount")
        if not isinstance(amount, Mapping):
            raise PaymentValidationError("WeChat Pay notification is missing amount")
        total = _positive_int(amount.get("total"), "amount.total")
        if amount.get("currency") != "CNY":
            raise PaymentValidationError("WeChat Pay notification currency is not CNY")
        trade_state = str(resource.get("trade_state") or "")
        if event_type == "TRANSACTION.SUCCESS" and trade_state != "SUCCESS":
            raise PaymentValidationError("WeChat Pay notification event and trade_state disagree")
        if event_type == "TRANSACTION.CLOSED" and trade_state != "CLOSED":
            raise PaymentValidationError("WeChat Pay notification event and trade_state disagree")
        event = {
            "TRANSACTION.SUCCESS": NotificationEvent.PAYMENT_SUCCEEDED,
            "TRANSACTION.CLOSED": NotificationEvent.PAYMENT_CLOSED,
        }.get(event_type, NotificationEvent.UNKNOWN)
        out_trade_no = resource.get("out_trade_no")
        if not isinstance(out_trade_no, str) or not out_trade_no:
            raise PaymentValidationError("WeChat Pay notification is missing out_trade_no")
        return PaymentNotification(
            channel=self.channel,
            event_type=event,
            notification_id=notification_id,
            out_trade_no=out_trade_no,
            amount_fen=total,
            currency="CNY",
            status=_TRADE_STATE_MAP.get(trade_state, PaymentStatus.UNKNOWN),
            transaction_id=resource.get("transaction_id"),
            occurred_at=_parse_time(resource.get("success_time")),
            provider_data=dict(resource),
        )

    def _refund_notification(
        self, notification_id: str, event_type: str, resource: Mapping[str, Any]
    ) -> PaymentNotification:
        amount = resource.get("amount")
        if not isinstance(amount, Mapping):
            raise PaymentValidationError("WeChat Pay refund notification is missing amount")
        total = _positive_int(amount.get("total"), "amount.total")
        refund = _positive_int(amount.get("refund"), "amount.refund")
        if amount.get("currency") != "CNY":
            raise PaymentValidationError("WeChat Pay refund notification currency is not CNY")
        refund_status = str(resource.get("refund_status") or "")
        if event_type == "REFUND.SUCCESS" and refund_status != "SUCCESS":
            raise PaymentValidationError("WeChat Pay refund event and refund_status disagree")
        if event_type in {"REFUND.ABNORMAL", "REFUND.CLOSED"} and refund_status not in {
            "ABNORMAL",
            "CLOSED",
        }:
            raise PaymentValidationError("WeChat Pay refund event and refund_status disagree")
        event = {
            "REFUND.SUCCESS": NotificationEvent.REFUND_SUCCEEDED,
            "REFUND.ABNORMAL": NotificationEvent.REFUND_FAILED,
            "REFUND.CLOSED": NotificationEvent.REFUND_FAILED,
        }.get(event_type, NotificationEvent.UNKNOWN)
        out_trade_no = resource.get("out_trade_no")
        out_refund_no = resource.get("out_refund_no")
        if not isinstance(out_trade_no, str) or not out_trade_no:
            raise PaymentValidationError("WeChat Pay refund notification is missing out_trade_no")
        if not isinstance(out_refund_no, str) or not out_refund_no:
            raise PaymentValidationError("WeChat Pay refund notification is missing out_refund_no")
        status = PaymentStatus.REFUNDED if refund == total else PaymentStatus.PAID
        return PaymentNotification(
            channel=self.channel,
            event_type=event,
            notification_id=notification_id,
            out_trade_no=out_trade_no,
            amount_fen=total,
            currency="CNY",
            status=status,
            transaction_id=resource.get("transaction_id"),
            out_refund_no=out_refund_no,
            refund_amount_fen=refund,
            occurred_at=_parse_time(resource.get("success_time")),
            provider_data=dict(resource),
        )
