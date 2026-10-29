from __future__ import annotations

import base64
from pathlib import Path

import pytest

from pypay_service.config import ServiceConfig, ServiceConfigError
from pypay_service.security import (
    AdminCredentials,
    AdminPrincipal,
    SecretBox,
    SecurityError,
    authenticate_admin,
    cashier_form_token,
    create_admin_session,
    parse_basic_authorization,
    resolve_public_host,
    validate_outbound_url,
    verify_admin_session,
    verify_cashier_form_token,
)


def test_outbound_url_rejects_private_hosts_and_bad_schemes() -> None:
    with pytest.raises(SecurityError):
        validate_outbound_url(
            "https://127.0.0.1/notify", label="notify_url", allow_http=False, allow_private=False
        )
    with pytest.raises(SecurityError):
        validate_outbound_url(
            "https://192.168.1.20/notify",
            label="notify_url",
            allow_http=False,
            allow_private=False,
        )
    with pytest.raises(SecurityError):
        validate_outbound_url(
            "https://localhost/notify", label="notify_url", allow_http=False, allow_private=False
        )
    with pytest.raises(SecurityError):
        validate_outbound_url(
            "http://example.com/notify",
            label="notify_url",
            allow_http=False,
            allow_private=False,
        )
    with pytest.raises(SecurityError):
        validate_outbound_url(
            "https://user:pass@example.com/notify",
            label="notify_url",
            allow_http=False,
            allow_private=False,
        )
    with pytest.raises(SecurityError):
        validate_outbound_url(
            "https://example.com/notify#fragment",
            label="notify_url",
            allow_http=False,
            allow_private=False,
        )
    assert (
        validate_outbound_url(
            "https://example.com/notify",
            label="notify_url",
            allow_http=False,
            allow_private=False,
        )
        == "https://example.com/notify"
    )


def test_dns_resolution_rejects_private_answers() -> None:
    def private(_host, _port, type=None):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]

    def public(_host, _port, type=None):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    with pytest.raises(SecurityError):
        resolve_public_host(
            "business.test", label="notify_url", allow_private=False, resolver=private
        )
    resolve_public_host("business.test", label="notify_url", allow_private=False, resolver=public)


def test_secret_box_round_trip_and_wrong_key() -> None:
    box = SecretBox(b"k" * 32)
    token = box.encrypt("super-secret")
    assert token.startswith("v1:")
    assert box.decrypt(token) == "super-secret"
    with pytest.raises(SecurityError):
        SecretBox(b"x" * 32).decrypt(token)


def test_admin_auth_helpers() -> None:
    credentials = (
        AdminCredentials("admin", "admin-password", "admin"),
        AdminCredentials("viewer", "viewer-password", "viewer"),
    )
    header = "Basic " + base64.b64encode(b"admin:admin-password").decode()
    principal = authenticate_admin(header, credentials)
    assert principal is not None and principal.can_write
    viewer_header = "Basic " + base64.b64encode(b"viewer:viewer-password").decode()
    viewer = authenticate_admin(viewer_header, credentials)
    assert viewer is not None and not viewer.can_write
    assert authenticate_admin("Basic !!!", credentials) is None
    assert parse_basic_authorization("Bearer x") is None


def test_cashier_form_token_is_bound_to_the_token() -> None:
    key = b"m" * 32
    token = cashier_form_token(key, "cashier-token")
    assert verify_cashier_form_token(key, "cashier-token", token)
    assert not verify_cashier_form_token(key, "other-token", token)
    assert not verify_cashier_form_token(key, "cashier-token", None)


def test_service_base_url_must_be_root_https_or_http() -> None:
    with pytest.raises(ServiceConfigError):
        ServiceConfig(
            database_path=Path("/tmp/x.db"),
            public_base_url="https://pay.test/api",
            master_key=b"k" * 32,
            admin_username="admin",
            admin_password="admin-password",
        )


def test_admin_session_is_signed_expires_and_follows_credentials() -> None:
    key = b"k" * 32
    credentials = (
        AdminCredentials("admin", "password-one", "admin"),
        AdminCredentials("viewer", "viewer-password", "viewer"),
    )
    principal = AdminPrincipal("admin", "admin")
    token = create_admin_session(key, credentials, principal, ttl_seconds=60, now=1_000)
    assert verify_admin_session(key, credentials, token, now=1_000) == principal
    assert verify_admin_session(key, credentials, token, now=1_061) is None
    assert verify_admin_session(b"x" * 32, credentials, token, now=1_000) is None

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert verify_admin_session(key, credentials, tampered, now=1_000) is None

    changed = (AdminCredentials("admin", "password-two", "admin"),)
    assert verify_admin_session(key, changed, token, now=1_000) is None
