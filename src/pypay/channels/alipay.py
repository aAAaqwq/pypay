import http.client
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo

from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
from alipay.aop.api.domain.AlipayTradeCloseModel import AlipayTradeCloseModel
from alipay.aop.api.domain.AlipayTradeFastpayRefundQueryModel import (
    AlipayTradeFastpayRefundQueryModel,
)
from alipay.aop.api.domain.AlipayTradePagePayModel import AlipayTradePagePayModel
from alipay.aop.api.domain.AlipayTradeQueryModel import AlipayTradeQueryModel
from alipay.aop.api.domain.AlipayTradeRefundModel import AlipayTradeRefundModel
from alipay.aop.api.exception.Exception import RequestException, ResponseException
from alipay.aop.api.request.AlipayTradeCloseRequest import AlipayTradeCloseRequest
from alipay.aop.api.request.AlipayTradeFastpayRefundQueryRequest import (
    AlipayTradeFastpayRefundQueryRequest,
)
from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest
from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest
from alipay.aop.api.request.AlipayTradeRefundRequest import AlipayTradeRefundRequest
from alipay.aop.api.util.SignatureUtils import verify_with_rsa
from cryptography.hazmat.primitives import serialization

from ..config import AlipayConfig
from ..enums import Channel, NotificationEvent, PaymentStatus, RefundStatus
from ..errors import (
    PaymentConfigError,
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
    validate_out_refund_no,
    validate_out_trade_no,
)
from ..money import fen_to_yuan, yuan_to_fen

_ALIPAY_TZ = ZoneInfo("Asia/Shanghai")
_TRADE_STATUS_MAP = {
    "WAIT_BUYER_PAY": PaymentStatus.PENDING,
    "TRADE_SUCCESS": PaymentStatus.PAID,
    "TRADE_FINISHED": PaymentStatus.PAID,
    "TRADE_CLOSED": PaymentStatus.CLOSED,
}
_NOT_FOUND_SUB_CODES = {"ACQ.TRADE_NOT_EXIST", "ACQ.REFUND_NOT_EXIST"}


def _normalize_private_key(pem: str) -> str:
    if "BEGIN RSA PRIVATE KEY" in pem:
        return pem
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode("utf-8")
    except Exception as exc:
        raise PaymentConfigError("Alipay private key could not be normalized to PKCS#1") from exc


def _format_alipay_time(value: datetime) -> str:
    return value.astimezone(_ALIPAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_alipay_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ALIPAY_TZ)
    except ValueError as exc:
        raise PaymentValidationError("provider returned an invalid timestamp") from exc


def _require_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PaymentValidationError(f"{name} must be an absolute http(s) URL")
    return value


def _provider_fen(value: object, field: str) -> int:
    try:
        return yuan_to_fen(value)
    except PaymentValidationError as exc:
        raise PaymentProviderError(f"Alipay response has an invalid {field}") from exc


class AlipayChannel:
    channel = Channel.ALIPAY

    def __init__(self, config: AlipayConfig) -> None:
        self._config = config
        client_config = AlipayClientConfig()
        client_config.app_id = config.appid
        client_config.sign_type = "RSA2"
        client_config.app_private_key = _normalize_private_key(config.private_key)
        client_config.alipay_public_key = config.public_key
        client_config.server_url = config.gateway
        client_config.timeout = config.timeout
        self._client = DefaultAlipayClient(client_config)

    def _execute(self, request: Any) -> dict[str, Any]:
        try:
            raw = self._client.execute(request)
        except RequestException as exc:
            raise PaymentNetworkError(
                "Alipay request failed in transit; query by out_trade_no before treating it as failed"
            ) from exc
        except ResponseException as exc:
            message = str(exc)
            if "sign verify failed" in message.lower():
                raise PaymentSignatureError(
                    "Alipay response signature verification failed"
                ) from exc
            status_match = re.search(r"invalid http status (\d+)", message)
            http_status = int(status_match.group(1)) if status_match else None
            raise PaymentProviderError(
                "Alipay returned an error response", http_status=http_status
            ) from exc
        except (TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            raise PaymentNetworkError(
                "Alipay request timed out or lost connection; query by out_trade_no before treating it as failed"
            ) from exc
        except Exception as exc:
            raise PaymentProviderError("Alipay request failed") from exc
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PaymentProviderError("Alipay returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise PaymentProviderError("Alipay returned an unexpected response shape")
        return dict(payload)

    def _check_code(self, payload: Mapping[str, Any]) -> None:
        code = str(payload.get("code") or "")
        if code == "10000":
            return
        sub_code = str(payload.get("sub_code") or "") or None
        message = (
            payload.get("sub_msg") or payload.get("msg") or "Alipay returned an error response"
        )
        if sub_code in _NOT_FOUND_SUB_CODES:
            raise PaymentNotFoundError(str(message), provider_code=sub_code or code or None)
        raise PaymentProviderError(str(message), provider_code=sub_code or code or None)

    def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResult:
        model = AlipayTradePagePayModel()
        model.out_trade_no = request.out_trade_no
        model.total_amount = fen_to_yuan(request.amount_fen)
        model.subject = request.description
        model.product_code = "FAST_INSTANT_TRADE_PAY"
        if request.expires_at is not None:
            model.time_expire = _format_alipay_time(request.expires_at)
        page_request = AlipayTradePagePayRequest(biz_model=model)
        page_request.notify_url = _require_url(
            request.notify_url or self._config.notify_url, "notify_url"
        )
        page_request.return_url = _require_url(
            request.return_url or self._config.return_url, "return_url"
        )
        try:
            form_html = self._client.page_execute(page_request, http_method="POST")
        except RequestException as exc:
            raise PaymentConfigError("Alipay request signing failed") from exc
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            form_html=form_html,
            expires_at=request.expires_at,
        )

    def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        validate_out_trade_no(out_trade_no)
        model = AlipayTradeQueryModel()
        model.out_trade_no = out_trade_no
        payload = self._execute(AlipayTradeQueryRequest(biz_model=model))
        self._check_code(payload)
        if payload.get("out_trade_no") != out_trade_no:
            raise PaymentProviderError("Alipay returned a mismatched out_trade_no")
        total_amount = payload.get("total_amount")
        if total_amount is None:
            raise PaymentProviderError("Alipay response is missing total_amount")
        currency = payload.get("currency", "CNY")
        if currency != "CNY":
            raise PaymentValidationError("Alipay returned a non-CNY currency")
        trade_status = str(payload.get("trade_status") or "")
        return QueryPaymentResult(
            channel=self.channel,
            out_trade_no=out_trade_no,
            amount_fen=_provider_fen(total_amount, "total_amount"),
            status=_TRADE_STATUS_MAP.get(trade_status, PaymentStatus.UNKNOWN),
            transaction_id=payload.get("trade_no"),
            paid_at=_parse_alipay_time(payload.get("send_pay_date")),
            channel_status=trade_status or None,
        )

    def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        validate_out_trade_no(out_trade_no)
        model = AlipayTradeCloseModel()
        model.out_trade_no = out_trade_no
        payload = self._execute(AlipayTradeCloseRequest(biz_model=model))
        self._check_code(payload)
        if payload.get("out_trade_no") != out_trade_no:
            raise PaymentProviderError("Alipay returned a mismatched out_trade_no")
        return ClosePaymentResult(
            channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
        )

    def refund(self, request: RefundRequest) -> RefundResult:
        model = AlipayTradeRefundModel()
        model.out_trade_no = request.out_trade_no
        model.refund_amount = fen_to_yuan(request.amount_fen)
        model.out_request_no = request.out_refund_no
        if request.reason is not None:
            model.refund_reason = request.reason
        payload = self._execute(AlipayTradeRefundRequest(biz_model=model))
        self._check_code(payload)
        if payload.get("out_trade_no") != request.out_trade_no:
            raise PaymentProviderError("Alipay returned a mismatched out_trade_no")
        refund_fee = payload.get("refund_fee")
        if refund_fee is None:
            raise PaymentProviderError("Alipay refund response is missing refund_fee")
        fund_change = payload.get("fund_change")
        return RefundResult(
            channel=self.channel,
            out_refund_no=request.out_refund_no,
            out_trade_no=request.out_trade_no,
            amount_fen=_provider_fen(refund_fee, "refund_fee"),
            total_amount_fen=request.total_amount_fen,
            status=RefundStatus.SUCCEEDED if fund_change == "Y" else RefundStatus.PENDING,
            created_at=_parse_alipay_time(payload.get("gmt_refund_pay")),
        )

    def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        if not out_trade_no:
            raise PaymentValidationError(
                "Alipay refund queries require out_trade_no as well as out_refund_no"
            )
        validate_out_trade_no(out_trade_no)
        validate_out_refund_no(out_refund_no)
        model = AlipayTradeFastpayRefundQueryModel()
        model.out_trade_no = out_trade_no
        model.out_request_no = out_refund_no
        payload = self._execute(AlipayTradeFastpayRefundQueryRequest(biz_model=model))
        self._check_code(payload)
        if payload.get("out_trade_no") != out_trade_no:
            raise PaymentProviderError("Alipay returned a mismatched out_trade_no")
        if payload.get("out_request_no") != out_refund_no:
            raise PaymentProviderError("Alipay returned a mismatched out_request_no")
        refund_amount = payload.get("refund_amount")
        if refund_amount is None:
            raise PaymentNotFoundError("Alipay has no refund record for this out_refund_no")
        total_amount = payload.get("total_amount")
        refund_status = payload.get("refund_status")
        return QueryRefundResult(
            channel=self.channel,
            out_refund_no=out_refund_no,
            out_trade_no=out_trade_no,
            amount_fen=_provider_fen(refund_amount, "refund_amount"),
            total_amount_fen=(
                _provider_fen(total_amount, "total_amount") if total_amount is not None else None
            ),
            status=(
                RefundStatus.SUCCEEDED
                if refund_status == "REFUND_SUCCESS"
                else RefundStatus.PENDING
            ),
            succeeded_at=_parse_alipay_time(payload.get("gmt_refund_pay")),
        )

    def parse_notification(
        self, headers: Mapping[str, str], body: bytes | str
    ) -> PaymentNotification:
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PaymentValidationError("Alipay notification is not UTF-8") from exc
        if not body:
            raise PaymentValidationError("Alipay notification body is empty")
        pairs = parse_qsl(body, keep_blank_values=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise PaymentValidationError("Alipay notification contains duplicate fields")
        signature = fields.pop("sign", None)
        if not signature:
            raise PaymentSignatureError("Alipay notification is missing sign")
        sign_type = fields.get("sign_type")
        if sign_type != "RSA2":
            raise PaymentSignatureError("Alipay notification sign_type is not RSA2")
        unsigned = {key: value for key, value in fields.items() if key != "sign_type"}
        content = "&".join(f"{key}={unsigned[key]}" for key in sorted(unsigned))
        try:
            verified = verify_with_rsa(self._config.public_key, content.encode("utf-8"), signature)
        except Exception as exc:
            raise PaymentSignatureError(
                "Alipay notification signature verification failed"
            ) from exc
        if not verified:
            raise PaymentSignatureError("Alipay notification signature verification failed")
        if fields.get("app_id") != self._config.appid:
            raise PaymentValidationError("Alipay notification app_id does not match")
        if self._config.seller_id is not None and fields.get("seller_id") != self._config.seller_id:
            raise PaymentValidationError("Alipay notification seller_id does not match")
        notification_id = fields.get("notify_id")
        if not notification_id:
            raise PaymentValidationError("Alipay notification is missing notify_id")
        out_trade_no = fields.get("out_trade_no")
        if not out_trade_no:
            raise PaymentValidationError("Alipay notification is missing out_trade_no")
        total_amount = fields.get("total_amount")
        if total_amount is None:
            raise PaymentValidationError("Alipay notification is missing total_amount")
        currency = fields.get("currency", "CNY")
        if currency != "CNY":
            raise PaymentValidationError("Alipay notification currency is not CNY")
        trade_status = str(fields.get("trade_status") or "")
        event = {
            "WAIT_BUYER_PAY": NotificationEvent.PAYMENT_PENDING,
            "TRADE_SUCCESS": NotificationEvent.PAYMENT_SUCCEEDED,
            "TRADE_FINISHED": NotificationEvent.PAYMENT_SUCCEEDED,
            "TRADE_CLOSED": NotificationEvent.PAYMENT_CLOSED,
        }.get(trade_status, NotificationEvent.UNKNOWN)
        refund_fee = fields.get("refund_fee")
        refund_amount_fen = _provider_fen(refund_fee, "refund_fee") if refund_fee else None
        return PaymentNotification(
            channel=self.channel,
            event_type=event,
            notification_id=notification_id,
            out_trade_no=out_trade_no,
            amount_fen=_provider_fen(total_amount, "total_amount"),
            currency=currency,
            status=_TRADE_STATUS_MAP.get(trade_status, PaymentStatus.UNKNOWN),
            transaction_id=fields.get("trade_no"),
            refund_amount_fen=refund_amount_fen,
            occurred_at=_parse_alipay_time(fields.get("gmt_payment") or fields.get("notify_time")),
            provider_data=fields,
        )
