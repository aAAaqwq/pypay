import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .enums import Channel
from .errors import PaymentConfigError

DEFAULT_WECHAT_TIMEOUT = 10.0
DEFAULT_ALIPAY_TIMEOUT = 10.0
DEFAULT_ALIPAY_GATEWAY = "https://openapi.alipay.com/gateway.do"


def _pem_candidates(value: str, markers: tuple[str, ...]) -> list[str]:
    if "-----BEGIN" in value:
        return [value]
    body = value.strip()
    return [f"-----BEGIN {marker}-----\n{body}\n-----END {marker}-----" for marker in markers]


def _load_private_key(value: str, label: str) -> rsa.RSAPrivateKey:
    if not isinstance(value, str) or not value.strip():
        raise PaymentConfigError(f"{label} is required")
    last_error: Exception | None = None
    for candidate in _pem_candidates(value, ("PRIVATE KEY", "RSA PRIVATE KEY")):
        try:
            key = serialization.load_pem_private_key(candidate.encode("utf-8"), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            last_error = exc
            continue
        if not isinstance(key, rsa.RSAPrivateKey):
            raise PaymentConfigError(f"{label} must be an RSA private key")
        return key
    raise PaymentConfigError(f"{label} is not a valid unencrypted RSA private key") from last_error


def _load_public_key(value: str, label: str) -> rsa.RSAPublicKey:
    if not isinstance(value, str) or not value.strip():
        raise PaymentConfigError(f"{label} is required")
    last_error: Exception | None = None
    for candidate in _pem_candidates(value, ("PUBLIC KEY", "RSA PUBLIC KEY")):
        try:
            key = serialization.load_pem_public_key(candidate.encode("utf-8"))
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            last_error = exc
            continue
        if not isinstance(key, rsa.RSAPublicKey):
            raise PaymentConfigError(f"{label} must be an RSA public key")
        return key
    raise PaymentConfigError(f"{label} is not a valid RSA public key") from last_error


def _normalize_private_key(value: str, label: str) -> str:
    key = _load_private_key(value, label)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _normalize_public_key(value: str, label: str) -> str:
    key = _load_public_key(value, label)
    return key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def _require_text(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaymentConfigError(f"{label} is required")
    return value


def _require_http_url(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PaymentConfigError(f"{label} is required")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PaymentConfigError(f"{label} must be an absolute http(s) URL")
    return value


def _require_https_url(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PaymentConfigError(f"{label} is required")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise PaymentConfigError(f"{label} must be an absolute https URL")
    return value


def _require_positive_timeout(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PaymentConfigError(f"{label} must be a positive number of seconds")
    return float(value)


class _EnvReader:
    def __init__(self, environ: Mapping[str, str], prefix: str) -> None:
        self._environ = environ
        self._prefix = prefix
        self.missing: list[str] = []

    def required(self, suffix: str) -> str | None:
        name = self._prefix + suffix
        value = self._environ.get(name)
        if not value:
            self.missing.append(name)
            return None
        return value

    def optional(self, suffix: str, default: str | None = None) -> str | None:
        value = self._environ.get(self._prefix + suffix)
        return value if value else default

    def secret(self, inline_suffix: str, path_suffix: str) -> str | None:
        inline_name = self._prefix + inline_suffix
        path_name = self._prefix + path_suffix
        inline = self._environ.get(inline_name)
        path_value = self._environ.get(path_name)
        if inline and path_value:
            raise PaymentConfigError(f"set only one of {inline_name} and {path_name}")
        if path_value:
            path = Path(path_value).expanduser()
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PaymentConfigError(f"{path_name} does not point to a readable file") from exc
        if inline:
            return inline
        self.missing.append(f"{inline_name} or {path_name}")
        return None

    def raise_if_missing(self, provider: str) -> None:
        if self.missing:
            joined = ", ".join(self.missing)
            raise PaymentConfigError(f"missing required {provider} configuration: {joined}")


@dataclass(frozen=True)
class WeChatPayConfig:
    mchid: str
    appid: str
    apiv3_key: str = field(repr=False)
    cert_serial_no: str
    private_key: str = field(repr=False)
    public_key: str = field(repr=False)
    public_key_id: str
    notify_url: str
    timeout: float = DEFAULT_WECHAT_TIMEOUT

    def __post_init__(self) -> None:
        _require_text(self.mchid, "mchid")
        _require_text(self.appid, "appid")
        _require_text(self.apiv3_key, "apiv3_key")
        _require_text(self.cert_serial_no, "cert_serial_no")
        _require_text(self.public_key_id, "public_key_id")
        if len(self.apiv3_key.encode("utf-8")) != 32:
            raise PaymentConfigError("apiv3_key must be exactly 32 bytes")
        object.__setattr__(
            self, "private_key", _normalize_private_key(self.private_key, "private_key")
        )
        object.__setattr__(self, "public_key", _normalize_public_key(self.public_key, "public_key"))
        _require_https_url(self.notify_url, "notify_url")
        object.__setattr__(self, "timeout", _require_positive_timeout(self.timeout, "timeout"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "WeChatPayConfig":
        env = _EnvReader(os.environ if environ is None else environ, "PYPAY_WECHAT_")
        values = {
            "mchid": env.required("MCHID"),
            "appid": env.required("APPID"),
            "apiv3_key": env.required("APIV3_KEY"),
            "cert_serial_no": env.required("CERT_SERIAL_NO"),
            "public_key_id": env.required("PUBLIC_KEY_ID"),
            "notify_url": env.required("NOTIFY_URL"),
            "private_key": env.secret("PRIVATE_KEY", "PRIVATE_KEY_PATH"),
            "public_key": env.secret("PUBLIC_KEY", "PUBLIC_KEY_PATH"),
        }
        timeout_raw = env.optional("TIMEOUT", str(DEFAULT_WECHAT_TIMEOUT))
        env.raise_if_missing("WeChat Pay")
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise PaymentConfigError("PYPAY_WECHAT_TIMEOUT must be a number") from exc
        return cls(timeout=timeout, **values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class AlipayConfig:
    appid: str
    private_key: str = field(repr=False)
    public_key: str = field(repr=False)
    notify_url: str
    return_url: str
    gateway: str = DEFAULT_ALIPAY_GATEWAY
    seller_id: str | None = None
    timeout: float = DEFAULT_ALIPAY_TIMEOUT

    def __post_init__(self) -> None:
        _require_text(self.appid, "appid")
        object.__setattr__(
            self, "private_key", _normalize_private_key(self.private_key, "private_key")
        )
        object.__setattr__(self, "public_key", _normalize_public_key(self.public_key, "public_key"))
        _require_http_url(self.notify_url, "notify_url")
        _require_http_url(self.return_url, "return_url")
        _require_https_url(self.gateway, "gateway")
        if self.seller_id is not None:
            _require_text(self.seller_id, "seller_id")
        object.__setattr__(self, "timeout", _require_positive_timeout(self.timeout, "timeout"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AlipayConfig":
        env = _EnvReader(os.environ if environ is None else environ, "PYPAY_ALIPAY_")
        values = {
            "appid": env.required("APPID"),
            "notify_url": env.required("NOTIFY_URL"),
            "return_url": env.required("RETURN_URL"),
            "private_key": env.secret("PRIVATE_KEY", "PRIVATE_KEY_PATH"),
            "public_key": env.secret("PUBLIC_KEY", "PUBLIC_KEY_PATH"),
        }
        gateway = env.optional("GATEWAY", DEFAULT_ALIPAY_GATEWAY)
        seller_id = env.optional("SELLER_ID")
        timeout_raw = env.optional("TIMEOUT", str(DEFAULT_ALIPAY_TIMEOUT))
        env.raise_if_missing("Alipay")
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise PaymentConfigError("PYPAY_ALIPAY_TIMEOUT must be a number") from exc
        return cls(gateway=gateway, seller_id=seller_id, timeout=timeout, **values)  # type: ignore[arg-type]


PaymentConfig = WeChatPayConfig | AlipayConfig


def config_from_env(
    channel: Channel | str,
    environ: Mapping[str, str] | None = None,
) -> PaymentConfig:
    try:
        resolved = Channel(channel)
    except ValueError as exc:
        raise PaymentConfigError(f"unknown channel: {channel}") from exc
    if resolved is Channel.WECHAT:
        return WeChatPayConfig.from_env(environ)
    return AlipayConfig.from_env(environ)
