from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .enums import AckStatus, DeliveryStatus, EventType
from .models import DeliveryRecord, OutboxEvent
from .timeutil import from_db, to_db


def event_from_row(row: sqlite3.Row) -> OutboxEvent:
    return OutboxEvent(
        event_id=row["event_id"],
        project_id=row["project_id"],
        order_id=row["order_id"],
        event_type=EventType(row["event_type"]),
        dedupe_key=row["dedupe_key"],
        payload=json.loads(row["payload_json"]),
        status=DeliveryStatus(row["status"]),
        ack_status=AckStatus(row["ack_status"]),
        attempts=int(row["attempts"]),
        manual_resends=int(row["manual_resends"]),
        next_attempt_at=from_db(row["next_attempt_at"]),
        lease_until=from_db(row["lease_until"]) if row["lease_until"] else None,
        last_error=row["last_error"],
        delivered_at=from_db(row["delivered_at"]) if row["delivered_at"] else None,
        acked_at=from_db(row["acked_at"]) if row["acked_at"] else None,
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
    )


def delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=int(row["id"]),
        event_id=row["event_id"],
        attempt_no=int(row["attempt_no"]),
        status=row["status"],
        http_status=int(row["http_status"]) if row["http_status"] is not None else None,
        response_excerpt=row["response_excerpt"],
        error=row["error"],
        duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
        sent_at=from_db(row["sent_at"]),
    )


def insert_event(conn: sqlite3.Connection, event: OutboxEvent) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO outbox_events
            (event_id, project_id, order_id, event_type, dedupe_key, payload_json,
             status, ack_status, attempts, manual_resends, next_attempt_at,
             lease_until, last_error, delivered_at, acked_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.project_id,
            event.order_id,
            event.event_type.value,
            event.dedupe_key,
            json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
            event.status.value,
            event.ack_status.value,
            event.attempts,
            event.manual_resends,
            to_db(event.next_attempt_at),
            to_db(event.lease_until) if event.lease_until else None,
            event.last_error,
            to_db(event.delivered_at) if event.delivered_at else None,
            to_db(event.acked_at) if event.acked_at else None,
            to_db(event.created_at),
            to_db(event.updated_at),
        ),
    )
    return cursor.rowcount == 1


def get_event(conn: sqlite3.Connection, event_id: str) -> OutboxEvent | None:
    row = conn.execute("SELECT * FROM outbox_events WHERE event_id = ?", (event_id,)).fetchone()
    return event_from_row(row) if row else None


def list_events_for_order(conn: sqlite3.Connection, order_id: str) -> list[OutboxEvent]:
    rows = conn.execute(
        "SELECT * FROM outbox_events WHERE order_id = ? ORDER BY created_at", (order_id,)
    ).fetchall()
    return [event_from_row(row) for row in rows]


def list_due_event_ids(conn: sqlite3.Connection, now: datetime, limit: int = 20) -> list[str]:
    rows = conn.execute(
        """
        SELECT event_id FROM outbox_events
        WHERE status = 'pending'
          AND next_attempt_at <= ?
          AND (lease_until IS NULL OR lease_until <= ?)
        ORDER BY next_attempt_at, created_at
        LIMIT ?
        """,
        (to_db(now), to_db(now), limit),
    ).fetchall()
    return [str(row["event_id"]) for row in rows]


def claim_event(
    conn: sqlite3.Connection, event_id: str, lease_until: datetime, now: datetime
) -> bool:
    cursor = conn.execute(
        """
        UPDATE outbox_events
        SET status = 'sending', lease_until = ?, updated_at = ?
        WHERE event_id = ? AND status = 'pending'
        """,
        (to_db(lease_until), to_db(now), event_id),
    )
    return cursor.rowcount == 1


def reset_stale_leases(conn: sqlite3.Connection, now: datetime) -> int:
    cursor = conn.execute(
        """
        UPDATE outbox_events
        SET status = 'pending', lease_until = NULL, updated_at = ?
        WHERE status = 'sending' AND lease_until <= ?
        """,
        (to_db(now), to_db(now)),
    )
    return cursor.rowcount


def mark_delivered(conn: sqlite3.Connection, event_id: str, attempts: int, now: datetime) -> None:
    conn.execute(
        """
        UPDATE outbox_events
        SET status = 'delivered', ack_status = 'acked', attempts = ?,
            delivered_at = ?, acked_at = ?, lease_until = NULL,
            last_error = NULL, updated_at = ?
        WHERE event_id = ?
        """,
        (attempts, to_db(now), to_db(now), to_db(now), event_id),
    )


def mark_retry(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    attempts: int,
    next_attempt_at: datetime,
    last_error: str,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE outbox_events
        SET status = 'pending', attempts = ?, next_attempt_at = ?,
            lease_until = NULL, last_error = ?, updated_at = ?
        WHERE event_id = ?
        """,
        (attempts, to_db(next_attempt_at), last_error[:500], to_db(now), event_id),
    )


def mark_dead(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    attempts: int,
    last_error: str,
    ack_status: AckStatus,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE outbox_events
        SET status = 'dead', ack_status = ?, attempts = ?,
            lease_until = NULL, last_error = ?, updated_at = ?
        WHERE event_id = ?
        """,
        (ack_status.value, attempts, last_error[:500], to_db(now), event_id),
    )


def record_delivery(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    attempt_no: int,
    status: str,
    http_status: int | None,
    response_excerpt: str | None,
    error: str | None,
    duration_ms: int | None,
    sent_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO outbox_deliveries
            (event_id, attempt_no, status, http_status, response_excerpt,
             error, duration_ms, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            attempt_no,
            status,
            http_status,
            (response_excerpt or "")[:500] or None,
            (error or "")[:500] or None,
            duration_ms,
            to_db(sent_at),
        ),
    )


def list_deliveries(conn: sqlite3.Connection, event_id: str) -> list[DeliveryRecord]:
    rows = conn.execute(
        "SELECT * FROM outbox_deliveries WHERE event_id = ? ORDER BY id", (event_id,)
    ).fetchall()
    return [delivery_from_row(row) for row in rows]


def manual_resend(conn: sqlite3.Connection, event_id: str, now: datetime) -> None:
    conn.execute(
        """
        UPDATE outbox_events
        SET status = 'pending', ack_status = 'pending', next_attempt_at = ?,
            lease_until = NULL, manual_resends = manual_resends + 1, updated_at = ?
        WHERE event_id = ?
        """,
        (to_db(now), to_db(now), event_id),
    )


def count_events_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS total FROM outbox_events GROUP BY status"
    ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}


def list_problem_events(conn: sqlite3.Connection, limit: int = 20) -> list[OutboxEvent]:
    rows = conn.execute(
        """
        SELECT * FROM outbox_events
        WHERE status = 'dead' OR (status = 'pending' AND attempts > 0)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [event_from_row(row) for row in rows]
