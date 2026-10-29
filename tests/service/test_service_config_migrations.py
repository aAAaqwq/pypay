from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from pypay_service.config import ServiceConfig, ServiceConfigError
from pypay_service.db import Database
from pypay_service.security import SecurityError, parse_master_key

MASTER = base64.b64encode(b"k" * 32).decode()


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "PYPAY_SERVICE_DATABASE_PATH": "./var/test.db",
        "PYPAY_SERVICE_PUBLIC_BASE_URL": "https://pay.test",
        "PYPAY_SERVICE_MASTER_KEY": "base64:" + MASTER,
        "PYPAY_SERVICE_ADMIN_USERNAME": "admin",
        "PYPAY_SERVICE_ADMIN_PASSWORD": "admin-password",
    }
    env.update(overrides)
    return env


def test_migrations_apply_and_roll_back(tmp_path) -> None:
    database = Database(tmp_path / "migrate.db")
    assert database.migrate_up() == ["0001_initial"]
    assert database.migrate_up() == []
    assert database.applied_versions() == ["0001_initial"]

    with database.read() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"projects", "payment_orders", "outbox_events", "audit_logs"} <= tables

    assert database.migrate_down(1) == ["0001_initial"]
    with database.read() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "projects" not in tables
    assert "payment_orders" not in tables
    assert "schema_migrations" in tables

    assert database.migrate_up() == ["0001_initial"]


def test_config_from_env_parses_channel_accounts() -> None:
    env = base_env(
        PYPAY_SERVICE_CHANNEL_ACCOUNTS=json.dumps(
            [
                {
                    "id": "wechat-default",
                    "channel": "wechat",
                    "name": "微信支付",
                    "env_prefix": "PYPAY_WECHAT_",
                },
                {
                    "id": "alipay-default",
                    "channel": "alipay",
                    "name": "支付宝",
                    "env_prefix": "PYPAY_ALIPAY_",
                },
            ]
        )
    )
    config = ServiceConfig.from_env(env)
    assert config.public_base_url == "https://pay.test"
    assert [seed.id for seed in config.channel_accounts] == [
        "wechat-default",
        "alipay-default",
    ]
    assert config.admin_credentials[0].role == "admin"


def test_config_auto_detects_default_accounts() -> None:
    env = base_env(PYPAY_WECHAT_MCHID="1900000109")
    config = ServiceConfig.from_env(env)
    assert [seed.id for seed in config.channel_accounts] == ["wechat-default"]


def test_config_requires_master_key_and_admin_password() -> None:
    env = base_env()
    env.pop("PYPAY_SERVICE_MASTER_KEY")
    with pytest.raises(ServiceConfigError):
        ServiceConfig.from_env(env)
    env = base_env(PYPAY_SERVICE_ADMIN_PASSWORD="short")
    with pytest.raises(ServiceConfigError):
        ServiceConfig.from_env(env)


def test_config_rejects_invalid_accounts_json() -> None:
    with pytest.raises(ServiceConfigError):
        ServiceConfig.from_env(base_env(PYPAY_SERVICE_CHANNEL_ACCOUNTS="{not-json"))


def test_parse_master_key_accepts_hex_and_base64() -> None:
    assert parse_master_key("hex:" + ("ab" * 32)) == bytes.fromhex("ab" * 32)
    assert parse_master_key("base64:" + MASTER) == b"k" * 32
    with pytest.raises(SecurityError):
        parse_master_key("too-short")


def test_down_migration_is_reversible_with_data_loss(tmp_path) -> None:
    database = Database(tmp_path / "reversible.db")
    database.migrate_up()
    with database.transaction() as conn:
        conn.execute(
            """
            INSERT INTO projects
                (id, name, status, api_key_id, api_secret_enc, notify_secret_enc,
                 notify_url, allowed_return_urls, created_at, updated_at)
            VALUES ('p1', 'P1', 'active', 'pk_1', 'x', 'y',
                    'https://example.test/notify', '[]', '2026-01-01T00:00:00.000000+00:00',
                    '2026-01-01T00:00:00.000000+00:00')
            """
        )
    database.migrate_down(1)
    with pytest.raises(sqlite3.OperationalError), database.read() as conn:
        conn.execute("SELECT * FROM projects").fetchall()


def test_config_parses_admin_session_and_docs_dir(tmp_path) -> None:
    docs = tmp_path / "docs"
    config = ServiceConfig.from_env(
        base_env(
            PYPAY_SERVICE_ADMIN_SESSION_SECONDS="1200",
            PYPAY_SERVICE_DOCS_DIR=str(docs),
        )
    )
    assert config.admin_session_seconds == 1200
    assert config.service_docs_dir == docs

    with pytest.raises(ServiceConfigError):
        ServiceConfig.from_env(base_env(PYPAY_SERVICE_ADMIN_SESSION_SECONDS="30"))


def test_secure_cookie_flag_follows_public_url_and_can_be_overridden(tmp_path) -> None:
    base = {
        "database_path": tmp_path / "cookies.db",
        "public_base_url": "https://pay.test",
        "master_key": b"k" * 32,
        "admin_username": "admin",
        "admin_password": "password123",
    }
    assert ServiceConfig(**base).secure_cookies is True
    assert ServiceConfig(**{**base, "public_base_url": "http://pay.test"}).secure_cookies is False
    assert ServiceConfig(**{**base, "secure_cookies": False}).secure_cookies is False
