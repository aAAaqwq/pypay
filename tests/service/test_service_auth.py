from __future__ import annotations

import time

import pytest

from tests.service.helpers import (
    build_env,
    create_payment,
    get_json,
    json_body,
    signed_headers,
)


@pytest.fixture
def env(tmp_path):
    return build_env(tmp_path)


def test_valid_signature_is_accepted(env) -> None:
    response = create_payment(env.clients["anon"], env.auth("project-a"), business_order_no="A-SIG")
    assert response.status_code == 201


def test_missing_headers_are_rejected(env) -> None:
    response = env.clients["anon"].post(
        "/v1/payments",
        content=json_body(
            {
                "business_order_no": "A-NOSIG",
                "subject": "x",
                "amount_fen": 100,
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_bad_signature_is_rejected(env) -> None:
    body = json_body({"business_order_no": "A-BAD", "subject": "x", "amount_fen": 100})
    headers = signed_headers(env.auth("project-a"), "POST", "/v1/payments", body)
    headers["X-Pypay-Signature"] = "0" * 64
    headers["Content-Type"] = "application/json"
    response = env.clients["anon"].post("/v1/payments", content=body, headers=headers)
    assert response.status_code == 401
    assert "signature" in response.text


def test_nonce_cannot_be_replayed(env) -> None:
    nonce = "a" * 32
    first = create_payment(
        env.clients["anon"], env.auth("project-a"), business_order_no="A-N1", nonce=nonce
    )
    assert first.status_code == 201
    second = create_payment(
        env.clients["anon"], env.auth("project-a"), business_order_no="A-N2", nonce=nonce
    )
    assert second.status_code == 401
    assert "nonce" in second.text


def test_timestamp_outside_skew_is_rejected(env) -> None:
    stale = int(time.time()) - 10_000
    response = create_payment(
        env.clients["anon"],
        env.auth("project-a"),
        business_order_no="A-OLD",
        timestamp=stale,
    )
    assert response.status_code == 401
    assert "timestamp" in response.text


def test_body_tampering_is_rejected(env) -> None:
    signed = json_body({"business_order_no": "A-T1", "subject": "x", "amount_fen": 100})
    sent = json_body({"business_order_no": "A-T1", "subject": "x", "amount_fen": 1})
    headers = signed_headers(env.auth("project-a"), "POST", "/v1/payments", signed)
    headers["Content-Type"] = "application/json"
    response = env.clients["anon"].post("/v1/payments", content=sent, headers=headers)
    assert response.status_code == 401


def test_unknown_key_id_is_rejected(env) -> None:
    body = json_body({"business_order_no": "A-K", "subject": "x", "amount_fen": 100})
    headers = signed_headers(env.auth("project-a"), "POST", "/v1/payments", body)
    headers["X-Pypay-Key-Id"] = "pk_unknown"
    headers["Content-Type"] = "application/json"
    response = env.clients["anon"].post("/v1/payments", content=body, headers=headers)
    assert response.status_code == 401


def test_get_requires_a_signature(env) -> None:
    created = create_payment(env.clients["anon"], env.auth("project-a"), business_order_no="A-GET")
    payment_id = created.json()["payment_id"]
    assert env.clients["anon"].get(f"/v1/payments/{payment_id}").status_code == 401
    signed = get_json(env.clients["anon"], env.auth("project-a"), f"/v1/payments/{payment_id}")
    assert signed.status_code == 200


def test_query_string_is_not_part_of_the_signature(env) -> None:
    # Merchant endpoints do not use security-sensitive query parameters. The
    # signature covers the path and body hash; this test documents that contract.
    created = create_payment(env.clients["anon"], env.auth("project-a"), business_order_no="A-Q")
    payment_id = created.json()["payment_id"]
    path = f"/v1/payments/{payment_id}"
    headers = signed_headers(env.auth("project-a"), "GET", path, b"")
    response = env.clients["anon"].get(path + "?unused=1", headers=headers)
    assert response.status_code == 200
