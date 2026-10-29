from __future__ import annotations

import sqlite3
from datetime import datetime

from .timeutil import to_db


def insert_channel_notification(
    conn: sqlite3.Connection,
    *,
    channel: str,
    notification_id: str,
    event_type: str,
    out_trade_no: str,
    out_refund_no: str | None,
    payload_hash: str | None,
    now: datetime,
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO channel_notifications
            (channel, notification_id, event_type, out_trade_no, out_refund_no,
             payload_hash, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel,
            notification_id,
            event_type,
            out_trade_no,
            out_refund_no,
            payload_hash,
            to_db(now),
        ),
    )
    return cursor.rowcount == 1


def list_notifications_for_order(conn: sqlite3.Connection, order_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT n.* FROM channel_notifications n
        WHERE n.out_trade_no IN (
            SELECT out_trade_no FROM payment_attempts WHERE order_id = ?
        )
        ORDER BY n.id
        """,
        (order_id,),
    ).fetchall()


def insert_nonce(conn: sqlite3.Connection, key_id: str, nonce: str, now: datetime) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO api_nonces (key_id, nonce, created_at) VALUES (?, ?, ?)",
        (key_id, nonce, to_db(now)),
    )
    return cursor.rowcount == 1


def cleanup_nonces(conn: sqlite3.Connection, before: datetime) -> int:
    cursor = conn.execute("DELETE FROM api_nonces WHERE created_at < ?", (to_db(before),))
    return cursor.rowcount
