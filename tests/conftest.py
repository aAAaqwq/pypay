import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import pytest
import requests

from pypay import AlipayConfig, WeChatPayConfig

from .keys import KeyPair, generate_keypair, wechat_headers


@dataclass(frozen=True)
class Keys:
    merchant: KeyPair
    platform: KeyPair
    app: KeyPair
    alipay: KeyPair


@pytest.fixture(scope="session")
def keys() -> Keys:
    return Keys(
        merchant=generate_keypair(),
        platform=generate_keypair(),
        app=generate_keypair(),
        alipay=generate_keypair(),
    )


@pytest.fixture
def wechat_config(keys: Keys) -> WeChatPayConfig:
    return WeChatPayConfig(
        mchid="1900000109",
        appid="wxd678efh567hg6787",
        apiv3_key="a" * 32,
        cert_serial_no="MERCHANT_SERIAL",
        private_key=keys.merchant.private_pem_pkcs8,
        public_key=keys.platform.public_pem,
        public_key_id="PUB_KEY_ID_TEST",
        notify_url="https://example.com/notify/wechat",
        timeout=5,
    )


@pytest.fixture
def alipay_config(keys: Keys) -> AlipayConfig:
    return AlipayConfig(
        appid="2021000000000000",
        private_key=keys.app.private_pem_pkcs8,
        public_key=keys.alipay.public_pem,
        notify_url="https://example.com/notify/alipay",
        return_url="https://example.com/return/alipay",
        gateway="https://openapi.alipay.com/gateway.do",
        timeout=5,
    )


class FakeResponse:
    def __init__(self, status_code: int, body: Any, headers: dict[str, str]) -> None:
        if body is None:
            text = ""
        elif isinstance(body, bytes):
            text = body.decode("utf-8")
        elif isinstance(body, str):
            text = body
        else:
            text = json.dumps(body)
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers


class WeChatTransport:
    def __init__(self, platform_key: Any, platform_key_id: str) -> None:
        self._platform_key = platform_key
        self._platform_key_id = platform_key_id
        self._queue: deque[Any] = deque()
        self.calls: list[dict[str, Any]] = []

    def queue(
        self,
        status_code: int,
        body: Any = None,
        *,
        sign: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        text = "" if body is None else body if isinstance(body, str) else json.dumps(body)
        response_headers = dict(headers or {})
        if sign:
            response_headers.update(
                wechat_headers(
                    self._platform_key,
                    self._platform_key_id,
                    text,
                    lowercase=False,
                )
            )
        if text:
            response_headers.setdefault("Content-Type", "application/json")
        self._queue.append(FakeResponse(status_code, body, response_headers))

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    def _next(self, method: str, url: str, headers: Any, payload: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        if not self._queue:
            raise AssertionError(f"unexpected {method} request to {url}")
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, headers: Any = None, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, headers, None)

    def post(
        self,
        url: str,
        headers: Any = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        **kwargs: Any,
    ) -> FakeResponse:
        return self._next("POST", url, headers, json if json is not None else data)


@pytest.fixture
def wechat_transport(monkeypatch: pytest.MonkeyPatch, keys: Keys) -> WeChatTransport:
    transport = WeChatTransport(keys.platform.private_key, "PUB_KEY_ID_TEST")
    monkeypatch.setattr("wechatpayv3.core.requests", transport)
    return transport


class AlipayTransport:
    def __init__(self) -> None:
        self._queue: deque[Any] = deque()
        self.calls: list[dict[str, Any]] = []

    def queue(self, response: bytes) -> None:
        self._queue.append(response)

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    def __call__(
        self,
        url: str,
        query_string: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        charset: str = "utf-8",
        timeout: int = 15,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "query_string": query_string,
                "headers": headers,
                "params": params,
                "charset": charset,
                "timeout": timeout,
            }
        )
        if not self._queue:
            raise AssertionError(f"unexpected Alipay request to {url}")
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def alipay_transport(monkeypatch: pytest.MonkeyPatch) -> AlipayTransport:
    transport = AlipayTransport()
    monkeypatch.setattr("alipay.aop.api.DefaultAlipayClient.do_post", transport)
    return transport


@pytest.fixture
def timeout_exception() -> requests.exceptions.Timeout:
    return requests.exceptions.Timeout("simulated timeout")
