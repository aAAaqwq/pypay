from __future__ import annotations

import sqlite3
from datetime import datetime

from .enums import RefundState
from .models import Refund
from .timeutil import from_db, to_db


def refund_from_row(row: sqlite3.Row) -> Refund:
    return Refund(
        id=row["id"],
        order_id=row["order_id"],
        attempt_id=row["attempt_id"],
        project_id=row["project_id"],
        out_refund_no=row["out_refund_no"],
        amount_fen=int(row["amount_fen"]),
        total_amount_fen=int(row["total_amount_fen"]),
        reason=row["reason"],
        status=RefundState(row["status"]),
        provider_refund_id=row["provider_refund_id"],
        idempotency_key=row["idempotency_key"],
        requested_by=row["requested_by"],
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        succeeded_at=from_db(row["succeeded_at"]) if row["succeeded_at"] else None,
    )


def insert_refund(conn: sqlite3.Connection, refund: Refund) -> None:
    conn.execute(
        """
        INSERT INTO refunds
            (id, order_id, attempt_id, project_id, out_refund_no, amount_fen,
             total_amount_fen, reason, status, provider_refund_id, idempotency_key,
             requested_by, created_at, updated_at, succeeded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            refund.id,
            refund.order_id,
            refund.attempt_id,
            refund.project_id,
            refund.out_refund_no,
            refund.amount_fen,
            refund.total_amount_fen,
            refund.reason,
            refund.status.value,
            refund.provider_refund_id,
            refund.idempotency_key,
            refund.requested_by,
            to_db(refund.created_at),
            to_db(refund.updated_at),
            to_db(refund.succeeded_at) if refund.succeeded_at else None,
        ),
    )


def get_refund(conn: sqlite3.Connection, refund_id: str) -> Refund | None:
    row = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
    return refund_from_row(row) if row else None


def get_refund_for_project(
    conn: sqlite3.Connection, refund_id: str, project_id: str
) -> Refund | None:
    row = conn.execute(
        "SELECT * FROM refunds WHERE id = ? AND project_id = ?", (refund_id, project_id)
    ).fetchone()
    return refund_from_row(row) if row else None


def get_refund_by_out_refund_no(conn: sqlite3.Connection, out_refund_no: str) -> Refund | None:
    row = conn.execute("SELECT * FROM refunds WHERE out_refund_no = ?", (out_refund_no,)).fetchone()
    return refund_from_row(row) if row else None


def get_refund_by_idempotency_key(
    conn: sqlite3.Connection, project_id: str, idempotency_key: str
) -> Refund | None:
    row = conn.execute(
        "SELECT * FROM refunds WHERE project_id = ? AND idempotency_key = ?",
        (project_id, idempotency_key),
    ).fetchone()
    return refund_from_row(row) if row else None


def list_refunds_for_order(conn: sqlite3.Connection, order_id: str) -> list[Refund]:
    rows = conn.execute(
        "SELECT * FROM refunds WHERE order_id = ? ORDER BY created_at", (order_id,)
    ).fetchall()
    return [refund_from_row(row) for row in rows]


def update_refund_state(
    conn: sqlite3.Connection,
    refund_id: str,
    *,
    status: RefundState,
    provider_refund_id: str | None,
    now: datetime,
    succeeded_at: datetime | None = None,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE refunds
        SET status = ?, provider_refund_id = COALESCE(?, provider_refund_id),
            updated_at = ?, succeeded_at = COALESCE(succeeded_at, ?)
        WHERE id = ? AND status != ?
        """,
        (
            status.value,
            provider_refund_id,
            to_db(now),
            to_db(succeeded_at) if succeeded_at else None,
            refund_id,
            status.value,
        ),
    )
    return cursor.rowcount == 1


def list_pending_refunds(conn: sqlite3.Connection) -> list[Refund]:
    rows = conn.execute(
        "SELECT * FROM refunds WHERE status = 'pending' ORDER BY created_at"
    ).fetchall()
    return [refund_from_row(row) for row in rows]


def sum_active_refunds(conn: sqlite3.Connection, order_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_fen), 0) AS total
        FROM refunds
        WHERE order_id = ? AND status IN ('pending', 'succeeded')
        """,
        (order_id,),
    ).fetchone()
    return int(row["total"]) if row else 0
