from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pypay import Channel

from .security import AdminCredentials, SecurityError, parse_master_key

DEFAULT_DATABASE_PATH = "./var/pypay_service.db"
DEFAULT_REQUEST_MAX_BYTES = 256 * 1024


class ServiceConfigError(ValueError):
    """Service configuration is missing or inconsistent."""


@dataclass(frozen=True)
class ChannelAccountSeed:
    id: str
    channel: Channel
    name: str
    env_prefix: str
    enabled: bool = True


def _require_text(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceConfigError(f"{name} is required")
    return value.strip()


def _require_bool(value: str | None, name: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_number(value: str | None, name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise ServiceConfigError(f"{name} must be a number") from exc
    if number <= 0:
        raise ServiceConfigError(f"{name} must be positive")
    return number


def _require_int(value: str | None, name: str, default: int, minimum: int = 1) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ServiceConfigError(f"{name} must be an integer") from exc
    if number < minimum:
        raise ServiceConfigError(f"{name} must be at least {minimum}")
    return number


def _parse_base_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ServiceConfigError("PYPAY_SERVICE_PUBLIC_BASE_URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ServiceConfigError("PYPAY_SERVICE_PUBLIC_BASE_URL must not include credentials")
    if parts.path not in {"", "/"}:
        raise ServiceConfigError("PYPAY_SERVICE_PUBLIC_BASE_URL must not include a path prefix")
    if parts.query or parts.fragment:
        raise ServiceConfigError("PYPAY_SERVICE_PUBLIC_BASE_URL must not include query or fragment")
    return f"{parts.scheme}://{parts.netloc}"


def _parse_channel_accounts(
    raw: str | None, environ: Mapping[str, str]
) -> tuple[ChannelAccountSeed, ...]:
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServiceConfigError("PYPAY_SERVICE_CHANNEL_ACCOUNTS must be valid JSON") from exc
        if not isinstance(payload, list):
            raise ServiceConfigError("PYPAY_SERVICE_CHANNEL_ACCOUNTS must be a JSON list")
        seeds: list[ChannelAccountSeed] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ServiceConfigError("each channel account must be an object")
            try:
                channel = Channel(str(item["channel"]))
            except (KeyError, ValueError) as exc:
                raise ServiceConfigError("channel account has an unknown channel") from exc
            enabled_raw = item.get("enabled", True)
            enabled = (
                enabled_raw
                if isinstance(enabled_raw, bool)
                else str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
            )
            seeds.append(
                ChannelAccountSeed(
                    id=_require_text(item.get("id"), "channel account id"),
                    channel=channel,
                    name=_require_text(item.get("name"), "channel account name"),
                    env_prefix=_require_text(item.get("env_prefix"), "channel account env_prefix"),
                    enabled=enabled,
                )
            )
        return tuple(seeds)

    seeds = []
    if environ.get("PYPAY_WECHAT_MCHID"):
        seeds.append(
            ChannelAccountSeed(
                id="wechat-default",
                channel=Channel.WECHAT,
                name="微信支付",
                env_prefix="PYPAY_WECHAT_",
            )
        )
    if environ.get("PYPAY_ALIPAY_APPID"):
        seeds.append(
            ChannelAccountSeed(
                id="alipay-default",
                channel=Channel.ALIPAY,
                name="支付宝",
                env_prefix="PYPAY_ALIPAY_",
            )
        )
    return tuple(seeds)


@dataclass(frozen=True)
class ServiceConfig:
    database_path: Path
    public_base_url: str
    master_key: bytes = field(repr=False)
    admin_username: str
    admin_password: str = field(repr=False)
    viewer_username: str | None = None
    viewer_password: str | None = field(default=None, repr=False)
    allow_http_urls: bool = False
    allow_private_urls: bool = False
    enable_docs: bool = False
    request_max_bytes: int = DEFAULT_REQUEST_MAX_BYTES
    clock_skew_seconds: int = 300
    nonce_ttl_seconds: int = 600
    outbox_max_attempts: int = 8
    outbox_timeout_seconds: float = 10.0
    outbox_batch_size: int = 20
    outbox_backoff_base_seconds: float = 5.0
    outbox_lease_seconds: float = 60.0
    reconcile_grace_seconds: float = 30.0
    worker_poll_interval_seconds: float = 2.0
    maintenance_interval_seconds: float = 30.0
    channel_accounts: tuple[ChannelAccountSeed, ...] = ()

    def __post_init__(self) -> None:
        if not self.public_base_url or self.public_base_url != _parse_base_url(
            self.public_base_url
        ):
            raise ServiceConfigError("public_base_url must be a normalized absolute URL")
        if not self.admin_username or not self.admin_password:
            raise ServiceConfigError("admin credentials are required")
        if len(self.admin_password) < 8:
            raise ServiceConfigError("admin password must be at least 8 characters")
        if bool(self.viewer_username) != bool(self.viewer_password):
            raise ServiceConfigError("set both viewer username and password, or neither")
        if self.viewer_password is not None and len(self.viewer_password) < 8:
            raise ServiceConfigError("viewer password must be at least 8 characters")
        if len(self.master_key) != 32:
            raise ServiceConfigError("master key must be 32 bytes")

    @property
    def admin_credentials(self) -> tuple[AdminCredentials, ...]:
        credentials = [
            AdminCredentials(self.admin_username, self.admin_password, "admin"),
        ]
        if self.viewer_username and self.viewer_password:
            credentials.append(
                AdminCredentials(self.viewer_username, self.viewer_password, "viewer")
            )
        return tuple(credentials)

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_base_url).scheme == "https"

    @property
    def database_file(self) -> Path:
        return Path(self.database_path).expanduser()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ServiceConfig:
        env = os.environ if environ is None else environ
        database_raw = env.get("PYPAY_SERVICE_DATABASE_PATH", DEFAULT_DATABASE_PATH)
        try:
            master_key = parse_master_key(
                _require_text(env.get("PYPAY_SERVICE_MASTER_KEY"), "PYPAY_SERVICE_MASTER_KEY")
            )
        except SecurityError as exc:
            raise ServiceConfigError(str(exc)) from exc
        viewer_username = env.get("PYPAY_SERVICE_VIEWER_USERNAME") or None
        viewer_password = env.get("PYPAY_SERVICE_VIEWER_PASSWORD") or None
        return cls(
            database_path=Path(database_raw),
            public_base_url=_parse_base_url(
                _require_text(
                    env.get("PYPAY_SERVICE_PUBLIC_BASE_URL"),
                    "PYPAY_SERVICE_PUBLIC_BASE_URL",
                )
            ),
            master_key=master_key,
            admin_username=_require_text(
                env.get("PYPAY_SERVICE_ADMIN_USERNAME"), "PYPAY_SERVICE_ADMIN_USERNAME"
            ),
            admin_password=_require_text(
                env.get("PYPAY_SERVICE_ADMIN_PASSWORD"), "PYPAY_SERVICE_ADMIN_PASSWORD"
            ),
            viewer_username=viewer_username,
            viewer_password=viewer_password,
            allow_http_urls=_require_bool(
                env.get("PYPAY_SERVICE_ALLOW_HTTP_URLS"), "PYPAY_SERVICE_ALLOW_HTTP_URLS"
            ),
            allow_private_urls=_require_bool(
                env.get("PYPAY_SERVICE_ALLOW_PRIVATE_URLS"), "PYPAY_SERVICE_ALLOW_PRIVATE_URLS"
            ),
            enable_docs=_require_bool(
                env.get("PYPAY_SERVICE_ENABLE_DOCS"), "PYPAY_SERVICE_ENABLE_DOCS"
            ),
            request_max_bytes=_require_int(
                env.get("PYPAY_SERVICE_REQUEST_MAX_BYTES"),
                "PYPAY_SERVICE_REQUEST_MAX_BYTES",
                DEFAULT_REQUEST_MAX_BYTES,
            ),
            clock_skew_seconds=_require_int(
                env.get("PYPAY_SERVICE_CLOCK_SKEW_SECONDS"),
                "PYPAY_SERVICE_CLOCK_SKEW_SECONDS",
                300,
            ),
            nonce_ttl_seconds=_require_int(
                env.get("PYPAY_SERVICE_NONCE_TTL_SECONDS"),
                "PYPAY_SERVICE_NONCE_TTL_SECONDS",
                600,
            ),
            outbox_max_attempts=_require_int(
                env.get("PYPAY_SERVICE_OUTBOX_MAX_ATTEMPTS"),
                "PYPAY_SERVICE_OUTBOX_MAX_ATTEMPTS",
                8,
            ),
            outbox_timeout_seconds=_require_number(
                env.get("PYPAY_SERVICE_OUTBOX_TIMEOUT_SECONDS"),
                "PYPAY_SERVICE_OUTBOX_TIMEOUT_SECONDS",
                10.0,
            ),
            outbox_batch_size=_require_int(
                env.get("PYPAY_SERVICE_OUTBOX_BATCH_SIZE"),
                "PYPAY_SERVICE_OUTBOX_BATCH_SIZE",
                20,
            ),
            outbox_backoff_base_seconds=_require_number(
                env.get("PYPAY_SERVICE_OUTBOX_BACKOFF_BASE_SECONDS"),
                "PYPAY_SERVICE_OUTBOX_BACKOFF_BASE_SECONDS",
                5.0,
            ),
            outbox_lease_seconds=_require_number(
                env.get("PYPAY_SERVICE_OUTBOX_LEASE_SECONDS"),
                "PYPAY_SERVICE_OUTBOX_LEASE_SECONDS",
                60.0,
            ),
            reconcile_grace_seconds=_require_number(
                env.get("PYPAY_SERVICE_RECONCILE_GRACE_SECONDS"),
                "PYPAY_SERVICE_RECONCILE_GRACE_SECONDS",
                30.0,
            ),
            worker_poll_interval_seconds=_require_number(
                env.get("PYPAY_SERVICE_WORKER_POLL_INTERVAL_SECONDS"),
                "PYPAY_SERVICE_WORKER_POLL_INTERVAL_SECONDS",
                2.0,
            ),
            maintenance_interval_seconds=_require_number(
                env.get("PYPAY_SERVICE_MAINTENANCE_INTERVAL_SECONDS"),
                "PYPAY_SERVICE_MAINTENANCE_INTERVAL_SECONDS",
                30.0,
            ),
            channel_accounts=_parse_channel_accounts(
                env.get("PYPAY_SERVICE_CHANNEL_ACCOUNTS"), env
            ),
        )
