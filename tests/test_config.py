from pathlib import Path

import pytest

from pypay import (
    AlipayConfig,
    Channel,
    PaymentClient,
    WeChatPayConfig,
)
from pypay.errors import PaymentConfigError

from .conftest import Keys


def wechat_env(keys: Keys, **overrides: str) -> dict[str, str]:
    env = {
        "PYPAY_WECHAT_MCHID": "1900000109",
        "PYPAY_WECHAT_APPID": "wxd678efh567hg6787",
        "PYPAY_WECHAT_APIV3_KEY": "a" * 32,
        "PYPAY_WECHAT_CERT_SERIAL_NO": "MERCHANT_SERIAL",
        "PYPAY_WECHAT_PUBLIC_KEY_ID": "PUB_KEY_ID_TEST",
        "PYPAY_WECHAT_NOTIFY_URL": "https://example.com/notify/wechat",
        "PYPAY_WECHAT_PRIVATE_KEY": keys.merchant.private_pem_pkcs8,
        "PYPAY_WECHAT_PUBLIC_KEY": keys.platform.public_pem,
    }
    env.update(overrides)
    return env


def alipay_env(keys: Keys, **overrides: str) -> dict[str, str]:
    env = {
        "PYPAY_ALIPAY_APPID": "2021000000000000",
        "PYPAY_ALIPAY_NOTIFY_URL": "https://example.com/notify/alipay",
        "PYPAY_ALIPAY_RETURN_URL": "https://example.com/return/alipay",
        "PYPAY_ALIPAY_PRIVATE_KEY": keys.app.private_pem_pkcs8,
        "PYPAY_ALIPAY_PUBLIC_KEY": keys.alipay.public_pem,
    }
    env.update(overrides)
    return env


def test_wechat_config_from_env(keys: Keys) -> None:
    config = WeChatPayConfig.from_env(wechat_env(keys))
    assert config.appid == "wxd678efh567hg6787"
    assert config.public_key_id == "PUB_KEY_ID_TEST"


def test_wechat_config_lists_every_missing_field(keys: Keys) -> None:
    env = wechat_env(keys)
    env.pop("PYPAY_WECHAT_APPID")
    env.pop("PYPAY_WECHAT_PUBLIC_KEY_ID")
    with pytest.raises(PaymentConfigError) as exc_info:
        WeChatPayConfig.from_env(env)
    message = str(exc_info.value)
    assert "PYPAY_WECHAT_APPID" in message
    assert "PYPAY_WECHAT_PUBLIC_KEY_ID" in message
    assert "BEGIN" not in message


def test_wechat_config_reads_key_files(keys: Keys, tmp_path: Path) -> None:
    private_path = tmp_path / "apiclient_key.pem"
    public_path = tmp_path / "wechatpay_public_key.pem"
    private_path.write_text(keys.merchant.private_pem_pkcs8)
    public_path.write_text(keys.platform.public_pem)
    env = wechat_env(keys)
    env.pop("PYPAY_WECHAT_PRIVATE_KEY")
    env.pop("PYPAY_WECHAT_PUBLIC_KEY")
    env["PYPAY_WECHAT_PRIVATE_KEY_PATH"] = str(private_path)
    env["PYPAY_WECHAT_PUBLIC_KEY_PATH"] = str(public_path)
    config = WeChatPayConfig.from_env(env)
    assert "BEGIN PRIVATE KEY" in config.private_key


def test_wechat_config_rejects_inline_and_path(keys: Keys, tmp_path: Path) -> None:
    path = tmp_path / "key.pem"
    path.write_text(keys.merchant.private_pem_pkcs8)
    env = wechat_env(keys)
    env["PYPAY_WECHAT_PRIVATE_KEY_PATH"] = str(path)
    with pytest.raises(PaymentConfigError) as exc_info:
        WeChatPayConfig.from_env(env)
    assert "PRIVATE_KEY" in str(exc_info.value)


def test_wechat_config_rejects_missing_key_file(keys: Keys, tmp_path: Path) -> None:
    env = wechat_env(keys)
    env.pop("PYPAY_WECHAT_PRIVATE_KEY")
    env["PYPAY_WECHAT_PRIVATE_KEY_PATH"] = str(tmp_path / "missing.pem")
    with pytest.raises(PaymentConfigError):
        WeChatPayConfig.from_env(env)


def test_wechat_config_rejects_short_apiv3_key(keys: Keys) -> None:
    with pytest.raises(PaymentConfigError):
        WeChatPayConfig.from_env(wechat_env(keys, PYPAY_WECHAT_APIV3_KEY="too-short"))


def test_wechat_config_requires_https_notify(keys: Keys) -> None:
    with pytest.raises(PaymentConfigError):
        WeChatPayConfig.from_env(
            wechat_env(keys, PYPAY_WECHAT_NOTIFY_URL="http://example.com/notify")
        )


def test_wechat_config_requires_appid(keys: Keys) -> None:
    env = wechat_env(keys)
    env.pop("PYPAY_WECHAT_APPID")
    with pytest.raises(PaymentConfigError) as exc_info:
        WeChatPayConfig.from_env(env)
    assert "PYPAY_WECHAT_APPID" in str(exc_info.value)


def test_wechat_config_requires_public_key_id(keys: Keys) -> None:
    env = wechat_env(keys)
    env.pop("PYPAY_WECHAT_PUBLIC_KEY_ID")
    with pytest.raises(PaymentConfigError) as exc_info:
        WeChatPayConfig.from_env(env)
    assert "PYPAY_WECHAT_PUBLIC_KEY_ID" in str(exc_info.value)


def test_wechat_config_hides_secrets_in_repr(keys: Keys) -> None:
    config = WeChatPayConfig.from_env(wechat_env(keys))
    rendered = repr(config)
    assert keys.merchant.private_pem_pkcs8[:40] not in rendered
    assert "a" * 32 not in rendered


def test_alipay_config_from_env_accepts_pkcs8(keys: Keys) -> None:
    config = AlipayConfig.from_env(alipay_env(keys))
    assert config.appid == "2021000000000000"
    assert "PRIVATE KEY" in config.private_key
    assert keys.app.private_pem_pkcs8[:40] not in repr(config)


def test_alipay_config_requires_return_url(keys: Keys) -> None:
    env = alipay_env(keys)
    env.pop("PYPAY_ALIPAY_RETURN_URL")
    with pytest.raises(PaymentConfigError) as exc_info:
        AlipayConfig.from_env(env)
    assert "PYPAY_ALIPAY_RETURN_URL" in str(exc_info.value)


def test_client_from_env_selects_channel(keys: Keys, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in wechat_env(keys).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PYPAY_CHANNEL", "wechat")
    client = PaymentClient.from_env()
    assert client.channel is Channel.WECHAT


def test_client_from_env_requires_channel(keys: Keys, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in wechat_env(keys).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PYPAY_CHANNEL", raising=False)
    with pytest.raises(PaymentConfigError):
        PaymentClient.from_env()


def test_client_rejects_mismatched_config(alipay_config: AlipayConfig) -> None:
    with pytest.raises(PaymentConfigError):
        PaymentClient(Channel.WECHAT, alipay_config)
    with pytest.raises(PaymentConfigError):
        PaymentClient(Channel.ALIPAY, config=None)  # type: ignore[arg-type]


def test_unknown_channel_is_a_config_error(alipay_config: AlipayConfig) -> None:
    from pypay.config import config_from_env

    with pytest.raises(PaymentConfigError):
        PaymentClient("stripe", alipay_config)
    with pytest.raises(PaymentConfigError):
        config_from_env("stripe")


def test_wechat_config_rejects_missing_key_value() -> None:
    with pytest.raises(PaymentConfigError):
        WeChatPayConfig(
            mchid="1900000109",
            appid="wxd678efh567hg6787",
            apiv3_key="a" * 32,
            cert_serial_no="MERCHANT_SERIAL",
            private_key="",  # type: ignore[arg-type]
            public_key="",  # type: ignore[arg-type]
            public_key_id="PUB_KEY_ID_TEST",
            notify_url="https://example.com/notify/wechat",
        )
