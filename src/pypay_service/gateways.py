from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pypay import (
    AlipayConfig,
    AsyncPaymentClient,
    Channel,
    PaymentClient,
    PaymentConfigError,
    WeChatPayConfig,
)
from pypay.base import AsyncPaymentGateway

from .config import ServiceConfig
from .errors import ProviderError
from .models import ChannelAccount


class GatewayRegistry:
    """Lazily builds one channel adapter per configured merchant account."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        factories: Mapping[str, AsyncPaymentGateway | Callable[[], AsyncPaymentGateway]]
        | None = None,
    ) -> None:
        self._config = config
        self._factories = dict(factories or {})
        self._cache: dict[str, AsyncPaymentGateway] = {}

    def get(self, account: ChannelAccount) -> AsyncPaymentGateway:
        cached = self._cache.get(account.id)
        if cached is not None:
            return cached
        factory = self._factories.get(account.id)
        if factory is not None:
            gateway = factory() if callable(factory) else factory
        else:
            gateway = self._build(account)
        self._cache[account.id] = gateway
        return gateway

    def _build(self, account: ChannelAccount) -> AsyncPaymentGateway:
        prefix = (
            account.env_prefix if account.env_prefix.endswith("_") else account.env_prefix + "_"
        )
        try:
            if account.channel is Channel.WECHAT:
                config: Any = WeChatPayConfig.from_env(prefix=prefix)
            else:
                config = AlipayConfig.from_env(prefix=prefix)
        except PaymentConfigError as exc:
            raise ProviderError(f"channel account {account.id} is not configured") from exc
        return AsyncPaymentClient(PaymentClient(account.channel, config))
