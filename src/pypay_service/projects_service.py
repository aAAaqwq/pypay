from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from pypay import Channel

from .config import ServiceConfig
from .db import Database
from .errors import AuthenticationError, ConflictError, NotFoundError, ValidationError
from .ids import new_api_key_id, new_secret
from .models import CreatedProject, Project, ProjectAuth, RotatedSecrets
from .repo_notifications import cleanup_nonces, insert_nonce
from .repo_projects import (
    get_channel_account,
    get_project,
    get_project_by_key_id,
    insert_project,
    list_project_channels,
    list_projects,
    project_from_row,
    rotate_project_secrets,
    set_channel_account_enabled,
    set_project_channels,
    update_project_settings,
    update_project_status,
    upsert_channel_account,
)
from .security import (
    SecretBox,
    SecurityError,
    constant_time_matches,
    request_signature,
    validate_outbound_url,
)
from .timeutil import utcnow

_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_KEY_ID_HEADER = "x-pypay-key-id"
_TIMESTAMP_HEADER = "x-pypay-timestamp"
_NONCE_HEADER = "x-pypay-nonce"
_SIGNATURE_HEADER = "x-pypay-signature"


def sync_channel_accounts(db: Database, config: ServiceConfig, now: datetime) -> None:
    if not config.channel_accounts:
        return
    with db.transaction() as conn:
        for seed in config.channel_accounts:
            upsert_channel_account(conn, seed, now)


def list_project_views(db: Database) -> list[Project]:
    with db.read() as conn:
        return list_projects(conn)


def get_project_view(db: Database, project_id: str) -> Project:
    with db.read() as conn:
        project = get_project(conn, project_id)
    if project is None:
        raise NotFoundError("project not found")
    return project


def get_project_channels(db: Database, project_id: str):
    with db.read() as conn:
        return list_project_channels(conn, project_id)


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValidationError(
            "project_id must start with a letter and contain 3-32 lowercase letters, digits, _ or -"
        )
    return project_id


def _validate_text(value: str | None, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    if value != value.strip():
        raise ValidationError(f"{name} must not have leading or trailing whitespace")
    if len(value) > max_length:
        raise ValidationError(f"{name} must be at most {max_length} characters")
    return value


def _validate_project_urls(
    config: ServiceConfig, notify_url: str, return_urls: Sequence[str]
) -> tuple[str, list[str]]:
    try:
        notify = validate_outbound_url(
            notify_url,
            label="notify_url",
            allow_http=config.allow_http_urls,
            allow_private=config.allow_private_urls,
        )
    except SecurityError as exc:
        raise ValidationError(str(exc)) from exc
    cleaned: list[str] = []
    for value in return_urls:
        try:
            url = validate_outbound_url(
                value,
                label="return_url",
                allow_http=config.allow_http_urls,
                allow_private=config.allow_private_urls,
            )
        except SecurityError as exc:
            raise ValidationError(str(exc)) from exc
        if url not in cleaned:
            cleaned.append(url)
    if len(cleaned) > 20:
        raise ValidationError("at most 20 return URLs may be registered")
    return notify, cleaned


def _validate_channel_selection(
    conn: sqlite3.Connection, channels: Mapping[Channel, str]
) -> list[str]:
    if not channels:
        raise ValidationError("at least one payment channel is required")
    account_ids: list[str] = []
    for channel, account_id in channels.items():
        account = get_channel_account(conn, account_id)
        if account is None:
            raise ValidationError(f"unknown channel account: {account_id}")
        if account.channel is not channel:
            raise ValidationError(f"channel account {account_id} is not a {channel.value} account")
        if not account.enabled:
            raise ValidationError(f"channel account {account_id} is disabled")
        account_ids.append(account_id)
    return account_ids


def create_project(
    db: Database,
    box: SecretBox,
    config: ServiceConfig,
    *,
    project_id: str,
    name: str,
    notify_url: str,
    return_urls: Sequence[str],
    channels: Mapping[Channel, str],
    now: datetime | None = None,
) -> CreatedProject:
    current = now or utcnow()
    project_id = validate_project_id(project_id)
    name = _validate_text(name, "name", 100)
    notify, cleaned_returns = _validate_project_urls(config, notify_url, return_urls)
    api_key_id = new_api_key_id()
    api_secret = new_secret()
    notify_secret = new_secret()
    try:
        with db.transaction() as conn:
            account_ids = _validate_channel_selection(conn, channels)
            insert_project(
                conn,
                project_id=project_id,
                name=name,
                api_key_id=api_key_id,
                api_secret_enc=box.encrypt(api_secret),
                notify_secret_enc=box.encrypt(notify_secret),
                notify_url=notify,
                allowed_return_urls=cleaned_returns,
                now=current,
            )
            set_project_channels(conn, project_id, account_ids)
            project = get_project(conn, project_id)
    except sqlite3.IntegrityError as exc:
        raise ConflictError("project id or API key id already exists") from exc
    if project is None:
        raise ConflictError("project could not be created")
    return CreatedProject(project=project, api_secret=api_secret, notify_secret=notify_secret)


def update_project(
    db: Database,
    config: ServiceConfig,
    project_id: str,
    *,
    name: str,
    notify_url: str,
    return_urls: Sequence[str],
    channels: Mapping[Channel, str],
    now: datetime | None = None,
) -> Project:
    current = now or utcnow()
    name = _validate_text(name, "name", 100)
    notify, cleaned_returns = _validate_project_urls(config, notify_url, return_urls)
    with db.transaction() as conn:
        if get_project(conn, project_id) is None:
            raise NotFoundError("project not found")
        account_ids = _validate_channel_selection(conn, channels)
        update_project_settings(
            conn,
            project_id,
            name=name,
            notify_url=notify,
            allowed_return_urls=cleaned_returns,
            now=current,
        )
        set_project_channels(conn, project_id, account_ids)
        project = get_project(conn, project_id)
    if project is None:
        raise NotFoundError("project not found")
    return project


def set_project_status(
    db: Database, project_id: str, enabled: bool, now: datetime | None = None
) -> Project:
    current = now or utcnow()
    with db.transaction() as conn:
        if get_project(conn, project_id) is None:
            raise NotFoundError("project not found")
        update_project_status(conn, project_id, "active" if enabled else "disabled", current)
        project = get_project(conn, project_id)
    if project is None:
        raise NotFoundError("project not found")
    return project


def rotate_project_credentials(
    db: Database,
    box: SecretBox,
    project_id: str,
    *,
    rotate_api: bool = True,
    rotate_notify: bool = True,
    now: datetime | None = None,
) -> RotatedSecrets:
    if not rotate_api and not rotate_notify:
        raise ValidationError("choose at least one secret to rotate")
    current = now or utcnow()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError("project not found")
        api_key_id = row["api_key_id"]
        api_secret_enc = row["api_secret_enc"]
        notify_secret_enc = row["notify_secret_enc"]
        api_secret: str | None = None
        notify_secret: str | None = None
        if rotate_api:
            api_key_id = new_api_key_id()
            api_secret = new_secret()
            api_secret_enc = box.encrypt(api_secret)
        if rotate_notify:
            notify_secret = new_secret()
            notify_secret_enc = box.encrypt(notify_secret)
        rotate_project_secrets(
            conn,
            project_id,
            api_key_id=api_key_id,
            api_secret_enc=api_secret_enc,
            notify_secret_enc=notify_secret_enc,
            now=current,
        )
    return RotatedSecrets(api_key_id=api_key_id, api_secret=api_secret, notify_secret=notify_secret)


def set_channel_account_state(
    db: Database, account_id: str, enabled: bool, now: datetime | None = None
) -> None:
    current = now or utcnow()
    with db.transaction() as conn:
        if get_channel_account(conn, account_id) is None:
            raise NotFoundError("channel account not found")
        set_channel_account_enabled(conn, account_id, enabled, current)


def authenticate_project(
    db: Database,
    box: SecretBox,
    config: ServiceConfig,
    *,
    key_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    method: str,
    path: str,
    body: bytes,
    now: datetime | None = None,
) -> ProjectAuth:
    current = now or utcnow()
    if not key_id or not timestamp or not nonce or not signature:
        raise AuthenticationError("missing pypay authentication headers")
    if not _NONCE_RE.fullmatch(nonce):
        raise AuthenticationError("invalid nonce")
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise AuthenticationError("invalid timestamp") from exc
    if abs(int(current.timestamp()) - sent_at) > config.clock_skew_seconds:
        raise AuthenticationError("timestamp outside the allowed clock skew")

    with db.transaction() as conn:
        row = get_project_by_key_id(conn, key_id)
        if row is None:
            raise AuthenticationError("invalid project credentials")
        if row["status"] != "active":
            raise AuthenticationError("project is disabled")
        try:
            api_secret = box.decrypt(row["api_secret_enc"])
            notify_secret = box.decrypt(row["notify_secret_enc"])
        except SecurityError as exc:
            raise AuthenticationError("invalid project credentials") from exc
        expected = request_signature(api_secret, method, path, timestamp, nonce, body)
        if not constant_time_matches(expected, signature):
            raise AuthenticationError("signature verification failed")
        if not insert_nonce(conn, key_id, nonce, current):
            raise AuthenticationError("nonce has already been used")
        cleanup_nonces(conn, current - timedelta(seconds=config.nonce_ttl_seconds))
        project = project_from_row(row)
    return ProjectAuth(project=project, api_secret=api_secret, notify_secret=notify_secret)


def project_allows_return_url(project: Project, return_url: str | None) -> bool:
    if return_url is None:
        return True
    return return_url in project.allowed_return_urls


def random_project_suffix() -> str:
    return secrets.token_hex(4)
