from __future__ import annotations

import json
import time

import httpx
import pytest

from pypay_service.client import (
    AsyncPayServiceClient,
    PayServiceClient,
    PayServiceError,
    WebhookSignatureError,
    verify_webhook,
)
from pypay_service.security import request_signature, webhook_signature

SECRET = "client-secret"


def test_sync_client_signs_every_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        timestamp = request.headers["x-pypay-timestamp"]
        nonce = request.headers["x-pypay-nonce"]
        expected = request_signature(
            SECRET, request.method, request.url.path, timestamp, nonce, request.content
        )
        assert request.headers["x-pypay-signature"] == expected
        assert request.headers["x-pypay-key-id"] == "pk_test"
        return httpx.Response(
            201,
            json={
                "payment_id": "pay_1",
                "cashier_url": "https://pay.test/pay/token",
                "status": "pending",
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://pay.test")
    client = PayServiceClient("https://pay.test", "pk_test", SECRET, http_client=http_client)
    result = client.create_payment(business_order_no="B-1", subject="测试", amount_fen=100)
    assert result["payment_id"] == "pay_1"
    assert str(seen[0].url) == "https://pay.test/v1/payments"
    payload = json.loads(seen[0].content)
    assert payload["business_order_no"] == "B-1"
    assert payload["amount_fen"] == 100


def test_client_raises_structured_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "conflict", "message": "amount mismatch"}},
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://pay.test")
    client = PayServiceClient("https://pay.test", "pk_test", SECRET, http_client=http_client)
    with pytest.raises(PayServiceError) as exc_info:
        client.create_payment(business_order_no="B-1", subject="测试", amount_fen=100)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "conflict"
    assert "amount mismatch" in exc_info.value.message


def test_async_client_signs_requests() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        timestamp = request.headers["x-pypay-timestamp"]
        nonce = request.headers["x-pypay-nonce"]
        expected = request_signature(
            SECRET, request.method, request.url.path, timestamp, nonce, request.content
        )
        assert request.headers["x-pypay-signature"] == expected
        return httpx.Response(200, json={"payment_id": "pay_1", "status": "paid"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://pay.test")
    client = AsyncPayServiceClient("https://pay.test", "pk_test", SECRET, http_client=http_client)

    async def run() -> None:
        result = await client.get_payment("pay_1")
        assert result["status"] == "paid"

    import asyncio

    asyncio.run(run())


def test_verify_webhook_accepts_a_valid_signature() -> None:
    body = json.dumps({"event_id": "evt_1", "event_type": "payment.succeeded"}).encode()
    timestamp = str(int(time.time()))
    signature = webhook_signature(SECRET, timestamp, body)
    payload = verify_webhook(
        SECRET,
        {
            "X-Pypay-Timestamp": timestamp,
            "X-Pypay-Signature": f"sha256={signature}",
        },
        body,
    )
    assert payload["event_id"] == "evt_1"


def test_verify_webhook_rejects_tampering_and_stale_timestamps() -> None:
    body = b'{"event_id":"evt_1"}'
    timestamp = str(int(time.time()))
    signature = webhook_signature(SECRET, timestamp, body)
    headers = {"X-Pypay-Timestamp": timestamp, "X-Pypay-Signature": signature}
    with pytest.raises(WebhookSignatureError):
        verify_webhook(SECRET, headers, b'{"event_id":"evt_2"}')
    old_timestamp = str(int(time.time()) - 10_000)
    old_signature = webhook_signature(SECRET, old_timestamp, body)
    with pytest.raises(WebhookSignatureError):
        verify_webhook(
            SECRET,
            {"X-Pypay-Timestamp": old_timestamp, "X-Pypay-Signature": old_signature},
            body,
        )


def test_client_rejects_base_url_with_path() -> None:
    with pytest.raises(ValueError):
        PayServiceClient("https://pay.test/api", "pk_test", SECRET)
