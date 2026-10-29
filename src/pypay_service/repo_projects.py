from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from pypay import Channel

from .config import ChannelAccountSeed
from .enums import ProjectStatus
from .models import ChannelAccount, Project
from .timeutil import from_db, to_db


def project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        status=ProjectStatus(row["status"]),
        api_key_id=row["api_key_id"],
        notify_url=row["notify_url"],
        allowed_return_urls=tuple(json.loads(row["allowed_return_urls"] or "[]")),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
    )


def channel_account_from_row(row: sqlite3.Row) -> ChannelAccount:
    return ChannelAccount(
        id=row["id"],
        channel=Channel(row["channel"]),
        name=row["name"],
        env_prefix=row["env_prefix"],
        enabled=bool(row["enabled"]),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
    )


def upsert_channel_account(
    conn: sqlite3.Connection, seed: ChannelAccountSeed, now: datetime
) -> None:
    conn.execute(
        """
        INSERT INTO channel_accounts
            (id, channel, name, env_prefix, enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            channel = excluded.channel,
            name = excluded.name,
            env_prefix = excluded.env_prefix,
            updated_at = excluded.updated_at
        """,
        (
            seed.id,
            seed.channel.value,
            seed.name,
            seed.env_prefix,
            int(seed.enabled),
            to_db(now),
            to_db(now),
        ),
    )


def get_channel_account(conn: sqlite3.Connection, account_id: str) -> ChannelAccount | None:
    row = conn.execute("SELECT * FROM channel_accounts WHERE id = ?", (account_id,)).fetchone()
    return channel_account_from_row(row) if row else None


def list_channel_accounts(conn: sqlite3.Connection) -> list[ChannelAccount]:
    rows = conn.execute("SELECT * FROM channel_accounts ORDER BY channel, name").fetchall()
    return [channel_account_from_row(row) for row in rows]


def set_channel_account_enabled(
    conn: sqlite3.Connection, account_id: str, enabled: bool, now: datetime
) -> None:
    conn.execute(
        "UPDATE channel_accounts SET enabled = ?, updated_at = ? WHERE id = ?",
        (int(enabled), to_db(now), account_id),
    )


def insert_project(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    name: str,
    api_key_id: str,
    api_secret_enc: str,
    notify_secret_enc: str,
    notify_url: str,
    allowed_return_urls: list[str],
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO projects
            (id, name, status, api_key_id, api_secret_enc, notify_secret_enc,
             notify_url, allowed_return_urls, created_at, updated_at)
        VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            name,
            api_key_id,
            api_secret_enc,
            notify_secret_enc,
            notify_url,
            json.dumps(allowed_return_urls),
            to_db(now),
            to_db(now),
        ),
    )


def get_project(conn: sqlite3.Connection, project_id: str) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return project_from_row(row) if row else None


def get_project_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def get_project_by_key_id(conn: sqlite3.Connection, api_key_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE api_key_id = ?", (api_key_id,)).fetchone()


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [project_from_row(row) for row in rows]


def update_project_status(
    conn: sqlite3.Connection, project_id: str, status: str, now: datetime
) -> None:
    conn.execute(
        "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
        (status, to_db(now), project_id),
    )


def update_project_settings(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    name: str,
    notify_url: str,
    allowed_return_urls: list[str],
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE projects
        SET name = ?, notify_url = ?, allowed_return_urls = ?, updated_at = ?
        WHERE id = ?
        """,
        (name, notify_url, json.dumps(allowed_return_urls), to_db(now), project_id),
    )


def rotate_project_secrets(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    api_key_id: str,
    api_secret_enc: str,
    notify_secret_enc: str,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE projects
        SET api_key_id = ?, api_secret_enc = ?, notify_secret_enc = ?, updated_at = ?
        WHERE id = ?
        """,
        (api_key_id, api_secret_enc, notify_secret_enc, to_db(now), project_id),
    )


def set_project_channels(conn: sqlite3.Connection, project_id: str, account_ids: list[str]) -> None:
    conn.execute("DELETE FROM project_channels WHERE project_id = ?", (project_id,))
    for account_id in account_ids:
        account_row = conn.execute(
            "SELECT channel FROM channel_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account_row is None:
            raise ValueError(f"unknown channel account: {account_id}")
        conn.execute(
            """
            INSERT INTO project_channels (project_id, channel, account_id, enabled)
            VALUES (?, ?, ?, 1)
            """,
            (project_id, account_row["channel"], account_id),
        )


def list_project_channels(conn: sqlite3.Connection, project_id: str) -> list[ChannelAccount]:
    rows = conn.execute(
        """
        SELECT a.*
        FROM project_channels pc
        JOIN channel_accounts a ON a.id = pc.account_id
        WHERE pc.project_id = ? AND pc.enabled = 1
        ORDER BY a.channel, a.name
        """,
        (project_id,),
    ).fetchall()
    return [channel_account_from_row(row) for row in rows]


def get_project_channel_account(
    conn: sqlite3.Connection, project_id: str, channel: Channel
) -> ChannelAccount | None:
    row = conn.execute(
        """
        SELECT a.*
        FROM project_channels pc
        JOIN channel_accounts a ON a.id = pc.account_id
        WHERE pc.project_id = ? AND pc.channel = ? AND pc.enabled = 1 AND a.enabled = 1
        """,
        (project_id, channel.value),
    ).fetchone()
    return channel_account_from_row(row) if row else None


def has_enabled_project_channel(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 AS present
        FROM project_channels pc
        JOIN channel_accounts a ON a.id = pc.account_id
        WHERE pc.project_id = ? AND pc.enabled = 1 AND a.enabled = 1
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return row is not None
