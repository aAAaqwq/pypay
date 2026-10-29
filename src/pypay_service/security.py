from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SECRET_CONTEXT = b"pypay-service-secret-v1"
_ADMIN_SESSION_CONTEXT = b"pypay-admin-session-v1"
ADMIN_SESSION_COOKIE = "pypay_admin_session"
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class SecurityError(ValueError):
    """A credential, signature, or outbound URL failed a security check."""


def parse_master_key(value: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise SecurityError("master key is required")
    raw = value.strip()
    try:
        if raw.startswith("base64:"):
            key = base64.b64decode(raw[len("base64:") :], validate=True)
        elif raw.startswith("hex:"):
            key = bytes.fromhex(raw[len("hex:") :])
        elif len(raw) == 64:
            key = bytes.fromhex(raw)
        else:
            key = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SecurityError("master key must be hex, base64, or 32 raw bytes") from exc
    if len(key) != 32:
        raise SecurityError("master key must decode to exactly 32 bytes")
    return key


class SecretBox:
    """AES-GCM at-rest wrapper for project API and notification secrets."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise SecurityError("secret box key must be 32 bytes")
        self._key = key

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), _SECRET_CONTEXT)
        return "v1:" + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token.startswith("v1:"):
            raise SecurityError("unsupported secret encoding")
        try:
            raw = base64.urlsafe_b64decode(token[3:])
            nonce, ciphertext = raw[:12], raw[12:]
            return AESGCM(self._key).decrypt(nonce, ciphertext, _SECRET_CONTEXT).decode("utf-8")
        except Exception as exc:
            raise SecurityError("secret could not be decrypted") from exc


def request_signature(
    secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}"
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def normalize_signature_header(value: str) -> str:
    value = value.strip()
    if value.startswith("sha256="):
        return value[len("sha256=") :]
    return value


def verify_webhook_signature(
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        return False
    expected = webhook_signature(secret, timestamp, body)
    return secrets.compare_digest(expected, normalize_signature_header(signature))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_matches(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return secrets.compare_digest(left, right)


def cashier_form_token(master_key: bytes, cashier_token: str) -> str:
    message = b"cashier:" + cashier_token.encode("ascii")
    return hmac.new(master_key, message, hashlib.sha256).hexdigest()


def verify_cashier_form_token(master_key: bytes, cashier_token: str, submitted: str | None) -> bool:
    return constant_time_matches(cashier_form_token(master_key, cashier_token), submitted)


@dataclass(frozen=True)
class AdminPrincipal:
    username: str
    role: str

    @property
    def can_write(self) -> bool:
        return self.role == "admin"


def parse_basic_authorization(value: str | None) -> tuple[str, str] | None:
    if not value or not value.lower().startswith("basic "):
        return None
    encoded = value[6:].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


@dataclass(frozen=True)
class AdminCredentials:
    username: str
    password: str
    role: str


def authenticate_admin_credentials(
    username: str, password: str, credentials: Iterable[AdminCredentials]
) -> AdminPrincipal | None:
    for credential in credentials:
        if secrets.compare_digest(username, credential.username) and secrets.compare_digest(
            password, credential.password
        ):
            return AdminPrincipal(username=credential.username, role=credential.role)
    return None


def authenticate_admin(
    authorization: str | None, credentials: Iterable[AdminCredentials]
) -> AdminPrincipal | None:
    parsed = parse_basic_authorization(authorization)
    if parsed is None:
        return None
    username, password = parsed
    return authenticate_admin_credentials(username, password, credentials)


def _admin_session_key(master_key: bytes) -> bytes:
    return hmac.new(master_key, _ADMIN_SESSION_CONTEXT, hashlib.sha256).digest()


def _admin_credential_tag(master_key: bytes, credential: AdminCredentials) -> str:
    material = f"{credential.username}\0{credential.password}\0{credential.role}".encode()
    return hmac.new(_admin_session_key(master_key), material, hashlib.sha256).hexdigest()[:32]


def create_admin_session(
    master_key: bytes,
    credentials: Iterable[AdminCredentials],
    principal: AdminPrincipal,
    *,
    ttl_seconds: int,
    now: float | None = None,
) -> str:
    credential = next(
        (
            candidate
            for candidate in credentials
            if candidate.username == principal.username and candidate.role == principal.role
        ),
        None,
    )
    if credential is None:
        raise ValueError("admin principal has no matching credential")
    issued = int(time.time() if now is None else now)
    payload = {
        "u": principal.username,
        "r": principal.role,
        "t": _admin_credential_tag(master_key, credential),
        "iat": issued,
        "exp": issued + ttl_seconds,
        "n": secrets.token_hex(8),
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signature = hmac.new(
        _admin_session_key(master_key), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"v1.{encoded}.{signature}"


def verify_admin_session(
    master_key: bytes,
    credentials: Iterable[AdminCredentials],
    token: str | None,
    *,
    now: float | None = None,
) -> AdminPrincipal | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    encoded, signature = parts[1], parts[2]
    expected = hmac.new(
        _admin_session_key(master_key), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not constant_time_matches(expected, signature):
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("u")
    role = payload.get("r")
    tag = payload.get("t")
    expires = payload.get("exp")
    if (
        not isinstance(username, str)
        or not isinstance(role, str)
        or not isinstance(tag, str)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
    ):
        return None
    current = int(time.time() if now is None else now)
    if expires < current:
        return None
    for credential in credentials:
        if credential.username != username or credential.role != role:
            continue
        if secrets.compare_digest(_admin_credential_tag(master_key, credential), tag):
            return AdminPrincipal(username=username, role=role)
        return None
    return None


def _private_message(label: str, reason: str) -> SecurityError:
    return SecurityError(f"{label} {reason}")


def _check_ip(ip: _IPAddress, label: str, allow_private: bool) -> None:
    if not allow_private and not ip.is_global:
        raise _private_message(label, "must not resolve to a private or reserved address")


def validate_outbound_url(
    value: str,
    *,
    label: str,
    allow_http: bool,
    allow_private: bool,
) -> str:
    """Validate a URL that the service will later request or redirect a browser to."""
    if not isinstance(value, str) or not value.strip():
        raise SecurityError(f"{label} is required")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise SecurityError(f"{label} must be an absolute http(s) URL")
    if not allow_http and parts.scheme != "https":
        raise SecurityError(f"{label} must use https")
    if not parts.hostname:
        raise SecurityError(f"{label} must include a host")
    if parts.username or parts.password:
        raise SecurityError(f"{label} must not include credentials")
    if parts.fragment:
        raise SecurityError(f"{label} must not include a fragment")
    host = parts.hostname.strip().lower()
    if not allow_private:
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise SecurityError(f"{label} must not target a private host")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _check_ip(ip, label, allow_private)
    return value


def resolve_public_host(
    host: str,
    *,
    label: str,
    allow_private: bool,
    resolver: Callable[..., list[tuple]] | None = None,
) -> None:
    """Re-check a hostname at send time to defeat DNS rebinding."""
    if allow_private:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _check_ip(ip, label, allow_private)
        return
    lookup = resolver or socket.getaddrinfo
    try:
        addresses = lookup(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SecurityError(f"{label} host could not be resolved") from exc
    if not addresses:
        raise SecurityError(f"{label} host could not be resolved")
    for entry in addresses:
        sockaddr = entry[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        _check_ip(ip, label, allow_private)
