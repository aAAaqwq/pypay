from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .security import (
    normalize_signature_header,
    request_signature,
    verify_webhook_signature,
)


def _clean_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("base_url must be an absolute http(s) URL")
    if parts.path not in {"", "/"}:
        raise ValueError("base_url must not include a path prefix")
    return f"{parts.scheme}://{parts.netloc}"


class PayServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        response_text: str | None = None,
    ) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.response_text = response_text


class WebhookSignatureError(Exception):
    pass


def _error_from_response(response: httpx.Response) -> PayServiceError:
    code = "http_error"
    message = response.text[:300] if response.text else "request failed"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)
        elif "detail" in payload:
            message = str(payload["detail"])
    return PayServiceError(response.status_code, code, message, response_text=response.text)


def verify_webhook(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> dict[str, Any]:
    lowered = {key.lower(): value for key, value in headers.items()}
    timestamp = lowered.get("x-pypay-timestamp")
    signature = lowered.get("x-pypay-signature")
    if not timestamp or not signature:
        raise WebhookSignatureError("missing webhook signature headers")
    if not verify_webhook_signature(
        secret,
        timestamp,
        body,
        normalize_signature_header(signature),
        tolerance_seconds=tolerance_seconds,
        now=now,
    ):
        raise WebhookSignatureError("webhook signature verification failed")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookSignatureError("webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookSignatureError("webhook body must be a JSON object")
    return payload


class _Signer:
    def __init__(self, key_id: str, secret: str) -> None:
        if not key_id or not secret:
            raise ValueError("key_id and secret are required")
        self.key_id = key_id
        self.secret = secret

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = request_signature(self.secret, method, path, timestamp, nonce, body)
        headers = {
            "X-Pypay-Key-Id": self.key_id,
            "X-Pypay-Timestamp": timestamp,
            "X-Pypay-Nonce": nonce,
            "X-Pypay-Signature": signature,
        }
        if body:
            headers["Content-Type"] = "application/json"
        return headers


def _encode(payload: Mapping[str, Any] | None) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _create_payload(
    *,
    business_order_no: str,
    subject: str,
    amount_fen: int,
    currency: str,
    expires_in_seconds: int,
    return_url: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "business_order_no": business_order_no,
        "subject": subject,
        "amount_fen": amount_fen,
        "currency": currency,
        "expires_in_seconds": expires_in_seconds,
    }
    if return_url is not None:
        payload["return_url"] = return_url
    if metadata is not None:
        payload["metadata"] = dict(metadata)
    return payload


class PayServiceClient:
    def __init__(
        self,
        base_url: str,
        key_id: str,
        secret: str,
        *,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = _clean_base_url(base_url)
        self._signer = _Signer(key_id, secret)
        self._client = http_client or httpx.Client(
            base_url=self._base_url, timeout=timeout, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PayServiceClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = _encode(payload)
        headers = self._signer.headers(method, path, body)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.request(method, path, content=body, headers=headers)
        if response.is_error:
            raise _error_from_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise PayServiceError(
                response.status_code, "invalid_response", "response is not JSON"
            ) from exc
        if not isinstance(data, dict):
            raise PayServiceError(
                response.status_code, "invalid_response", "response must be a JSON object"
            )
        return data

    def create_payment(
        self,
        *,
        business_order_no: str,
        subject: str,
        amount_fen: int,
        currency: str = "CNY",
        expires_in_seconds: int = 1800,
        return_url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/payments",
            _create_payload(
                business_order_no=business_order_no,
                subject=subject,
                amount_fen=amount_fen,
                currency=currency,
                expires_in_seconds=expires_in_seconds,
                return_url=return_url,
                metadata=metadata,
            ),
        )

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/payments/{payment_id}")

    def get_payment_by_business_order(self, business_order_no: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/payments/by-business-order/{business_order_no}")

    def refresh_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/payments/{payment_id}/refresh")

    def close_payment(self, payment_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/payments/{payment_id}/close")

    def create_refund(
        self,
        *,
        payment_id: str,
        amount_fen: int,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"payment_id": payment_id, "amount_fen": amount_fen}
        if reason is not None:
            payload["reason"] = reason
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return self._request("POST", "/v1/refunds", payload, idempotency_key=idempotency_key)

    def get_refund(self, refund_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/refunds/{refund_id}")

    def refresh_refund(self, refund_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/refunds/{refund_id}/refresh")


class AsyncPayServiceClient:
    def __init__(
        self,
        base_url: str,
        key_id: str,
        secret: str,
        *,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = _clean_base_url(base_url)
        self._signer = _Signer(key_id, secret)
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, trust_env=False
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncPayServiceClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = _encode(payload)
        headers = self._signer.headers(method, path, body)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await self._client.request(method, path, content=body, headers=headers)
        if response.is_error:
            raise _error_from_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise PayServiceError(
                response.status_code, "invalid_response", "response is not JSON"
            ) from exc
        if not isinstance(data, dict):
            raise PayServiceError(
                response.status_code, "invalid_response", "response must be a JSON object"
            )
        return data

    async def create_payment(
        self,
        *,
        business_order_no: str,
        subject: str,
        amount_fen: int,
        currency: str = "CNY",
        expires_in_seconds: int = 1800,
        return_url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/payments",
            _create_payload(
                business_order_no=business_order_no,
                subject=subject,
                amount_fen=amount_fen,
                currency=currency,
                expires_in_seconds=expires_in_seconds,
                return_url=return_url,
                metadata=metadata,
            ),
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/payments/{payment_id}")

    async def get_payment_by_business_order(self, business_order_no: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/payments/by-business-order/{business_order_no}")

    async def refresh_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/payments/{payment_id}/refresh")

    async def close_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/payments/{payment_id}/close")

    async def create_refund(
        self,
        *,
        payment_id: str,
        amount_fen: int,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"payment_id": payment_id, "amount_fen": amount_fen}
        if reason is not None:
            payload["reason"] = reason
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/v1/refunds", payload, idempotency_key=idempotency_key)

    async def get_refund(self, refund_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/refunds/{refund_id}")

    async def refresh_refund(self, refund_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/refunds/{refund_id}/refresh")
