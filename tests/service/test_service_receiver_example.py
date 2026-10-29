from __future__ import annotations

import importlib
import json
import time

from fastapi.testclient import TestClient

from pypay_service.security import webhook_signature


def _headers(secret: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "X-Pypay-Timestamp": timestamp,
        "X-Pypay-Signature": "sha256=" + webhook_signature(secret, timestamp, body),
    }


def _payload(event_id: str = "evt_receiver_1") -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": "payment.succeeded",
            "project_id": "super-aigc",
            "payment_id": "pay_receiver",
            "business_order_no": "A-RECEIVER-1",
            "subject": "额度包",
            "amount_fen": 9900,
            "currency": "CNY",
            "status": "paid",
            "refunded_amount_fen": 0,
            "metadata": {"user_ref": "u_1"},
            "occurred_at": "2026-10-29T12:00:00.000000+00:00",
            "channel": "wechat",
        },
        separators=(",", ":"),
    ).encode()


def test_receiver_verifies_and_grants_exactly_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROJECT_NOTIFY_SECRET", "receiver-secret")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "receiver.db"))

    import examples.business_receiver as receiver

    importlib.reload(receiver)
    client = TestClient(receiver.app, base_url="https://testserver")
    body = _payload()

    rejected = client.post("/pypay/notify", content=body, headers=_headers("wrong", body))
    assert rejected.status_code == 401
    assert client.get("/entitlements/A-RECEIVER-1").status_code == 404

    accepted = client.post("/pypay/notify", content=body, headers=_headers("receiver-secret", body))
    assert accepted.status_code == 200
    duplicate = client.post(
        "/pypay/notify", content=body, headers=_headers("receiver-secret", body)
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    entitlement = client.get("/entitlements/A-RECEIVER-1")
    assert entitlement.status_code == 200
    assert entitlement.json()["amount_fen"] == 9900
    assert entitlement.json()["event_id"] == "evt_receiver_1"
