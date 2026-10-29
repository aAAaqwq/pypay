from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .config import ServiceConfig
from .security import SecurityError, resolve_public_host, webhook_signature


@dataclass(frozen=True)
class DeliveryResult:
    status_code: int | None
    body_excerpt: str | None = None
    error: str | None = None
    duration_ms: int | None = None

    @property
    def delivered(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def permanent_failure(self) -> bool:
        if self.status_code is None:
            return False
        if self.status_code in {408, 429}:
            return False
        return 400 <= self.status_code < 500


class Notifier(Protocol):
    def send(
        self,
        *,
        notify_url: str,
        notify_secret: str,
        event_id: str,
        event_type: str,
        body: bytes,
    ) -> DeliveryResult: ...


class HttpNotifier:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    def send(
        self,
        *,
        notify_url: str,
        notify_secret: str,
        event_id: str,
        event_type: str,
        body: bytes,
    ) -> DeliveryResult:
        host = urlsplit(notify_url).hostname
        if not host:
            return DeliveryResult(status_code=None, error="notify_url has no host")
        try:
            resolve_public_host(
                host,
                label="notify_url",
                allow_private=self._config.allow_private_urls,
            )
        except SecurityError as exc:
            return DeliveryResult(status_code=None, error=str(exc))
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "pypay-service/0.2",
            "X-Pypay-Event-Id": event_id,
            "X-Pypay-Event-Type": event_type,
            "X-Pypay-Timestamp": timestamp,
            "X-Pypay-Signature": "sha256=" + webhook_signature(notify_secret, timestamp, body),
        }
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=self._config.outbox_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(notify_url, content=body, headers=headers)
        except httpx.TimeoutException:
            return DeliveryResult(
                status_code=None,
                error="notification timed out",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except httpx.RequestError as exc:
            return DeliveryResult(
                status_code=None,
                error=f"notification transport error: {type(exc).__name__}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration = int((time.monotonic() - started) * 1000)
        excerpt = response.text[:500] if response.text else None
        if 300 <= response.status_code < 400:
            return DeliveryResult(
                status_code=response.status_code,
                body_excerpt=excerpt,
                error="notification endpoint returned a redirect; redirects are not followed",
                duration_ms=duration,
            )
        return DeliveryResult(
            status_code=response.status_code,
            body_excerpt=excerpt,
            duration_ms=duration,
        )
