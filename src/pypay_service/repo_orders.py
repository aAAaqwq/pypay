from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from pypay import Channel

from .enums import AttemptStatus, OrderStatus
from .models import PaymentAttempt, PaymentOrder
from .timeutil import from_db, to_db


def order_from_row(row: sqlite3.Row) -> PaymentOrder:
    return PaymentOrder(
        id=row["id"],
        project_id=row["project_id"],
        business_order_no=row["business_order_no"],
        subject=row["subject"],
        amount_fen=int(row["amount_fen"]),
        currency=row["currency"],
        status=OrderStatus(row["status"]),
        cashier_token=row["cashier_token"],
        return_url=row["return_url"],
        metadata=json.loads(row["metadata_json"] or "{}"),
        expires_at=from_db(row["expires_at"]),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        paid_at=from_db(row["paid_at"]) if row["paid_at"] else None,
        closed_at=from_db(row["closed_at"]) if row["closed_at"] else None,
        refunded_amount_fen=int(row["refunded_amount_fen"]),
    )


def attempt_from_row(row: sqlite3.Row) -> PaymentAttempt:
    return PaymentAttempt(
        id=row["id"],
        order_id=row["order_id"],
        channel=Channel(row["channel"]),
        account_id=row["account_id"],
        out_trade_no=row["out_trade_no"],
        status=AttemptStatus(row["status"]),
        amount_fen=int(row["amount_fen"]),
        currency=row["currency"],
        qr_code_url=row["qr_code_url"],
        form_html=row["form_html"],
        redirect_url=row["redirect_url"],
        transaction_id=row["transaction_id"],
        channel_status=row["channel_status"],
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        paid_at=from_db(row["paid_at"]) if row["paid_at"] else None,
        closed_at=from_db(row["closed_at"]) if row["closed_at"] else None,
    )


def insert_order(conn: sqlite3.Connection, order: PaymentOrder) -> None:
    conn.execute(
        """
        INSERT INTO payment_orders
            (id, project_id, business_order_no, subject, amount_fen, currency,
             status, cashier_token, return_url, metadata_json, expires_at,
             created_at, updated_at, paid_at, closed_at, refunded_amount_fen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order.id,
            order.project_id,
            order.business_order_no,
            order.subject,
            order.amount_fen,
            order.currency,
            order.status.value,
            order.cashier_token,
            order.return_url,
            json.dumps(order.metadata, ensure_ascii=False),
            to_db(order.expires_at),
            to_db(order.created_at),
            to_db(order.updated_at),
            to_db(order.paid_at) if order.paid_at else None,
            to_db(order.closed_at) if order.closed_at else None,
            order.refunded_amount_fen,
        ),
    )


def get_order(conn: sqlite3.Connection, order_id: str) -> PaymentOrder | None:
    row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    return order_from_row(row) if row else None


def get_order_for_project(
    conn: sqlite3.Connection, order_id: str, project_id: str
) -> PaymentOrder | None:
    row = conn.execute(
        "SELECT * FROM payment_orders WHERE id = ? AND project_id = ?",
        (order_id, project_id),
    ).fetchone()
    return order_from_row(row) if row else None


def get_order_by_business_no(
    conn: sqlite3.Connection, project_id: str, business_order_no: str
) -> PaymentOrder | None:
    row = conn.execute(
        "SELECT * FROM payment_orders WHERE project_id = ? AND business_order_no = ?",
        (project_id, business_order_no),
    ).fetchone()
    return order_from_row(row) if row else None


def get_order_by_token(conn: sqlite3.Connection, cashier_token: str) -> PaymentOrder | None:
    row = conn.execute(
        "SELECT * FROM payment_orders WHERE cashier_token = ?", (cashier_token,)
    ).fetchone()
    return order_from_row(row) if row else None


def list_orders(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
    status: str | None = None,
    business_order_no: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PaymentOrder]:
    clauses: list[str] = []
    values: list[object] = []
    if project_id:
        clauses.append("project_id = ?")
        values.append(project_id)
    if status:
        clauses.append("status = ?")
        values.append(status)
    if business_order_no:
        clauses.append("business_order_no LIKE ?")
        values.append(f"%{business_order_no}%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM payment_orders{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        values,
    ).fetchall()
    return [order_from_row(row) for row in rows]


def update_order_paid(
    conn: sqlite3.Connection, order_id: str, paid_at: datetime, now: datetime
) -> bool:
    cursor = conn.execute(
        """
        UPDATE payment_orders
        SET status = CASE
                WHEN refunded_amount_fen >= amount_fen THEN 'refunded'
                ELSE 'paid'
            END,
            paid_at = COALESCE(paid_at, ?),
            updated_at = ?
        WHERE id = ? AND paid_at IS NULL
        """,
        (to_db(paid_at), to_db(now), order_id),
    )
    return cursor.rowcount == 1


def expire_order(conn: sqlite3.Connection, order_id: str, now: datetime) -> bool:
    cursor = conn.execute(
        """
        UPDATE payment_orders
        SET status = 'expired', updated_at = ?
        WHERE id = ? AND status = 'pending' AND paid_at IS NULL
        """,
        (to_db(now), order_id),
    )
    return cursor.rowcount == 1


def mark_order_closed(conn: sqlite3.Connection, order_id: str, now: datetime) -> bool:
    cursor = conn.execute(
        """
        UPDATE payment_orders
        SET status = 'closed', closed_at = COALESCE(closed_at, ?), updated_at = ?
        WHERE id = ? AND status = 'pending' AND paid_at IS NULL
        """,
        (to_db(now), to_db(now), order_id),
    )
    return cursor.rowcount == 1


def mark_order_failed(conn: sqlite3.Connection, order_id: str, now: datetime) -> bool:
    cursor = conn.execute(
        """
        UPDATE payment_orders
        SET status = 'failed', updated_at = ?
        WHERE id = ? AND status = 'pending' AND paid_at IS NULL
        """,
        (to_db(now), order_id),
    )
    return cursor.rowcount == 1


def add_refunded_amount(
    conn: sqlite3.Connection, order_id: str, amount_fen: int, now: datetime
) -> tuple[int, bool]:
    conn.execute(
        """
        UPDATE payment_orders
        SET refunded_amount_fen = MIN(amount_fen, refunded_amount_fen + ?),
            status = CASE
                WHEN MIN(amount_fen, refunded_amount_fen + ?) >= amount_fen
                    THEN 'refunded'
                ELSE status
            END,
            updated_at = ?
        WHERE id = ?
        """,
        (amount_fen, amount_fen, to_db(now), order_id),
    )
    row = conn.execute(
        "SELECT refunded_amount_fen, amount_fen FROM payment_orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        raise LookupError(order_id)
    refunded = int(row["refunded_amount_fen"])
    return refunded, refunded >= int(row["amount_fen"])


def insert_attempt(conn: sqlite3.Connection, attempt: PaymentAttempt) -> None:
    conn.execute(
        """
        INSERT INTO payment_attempts
            (id, order_id, channel, account_id, out_trade_no, status,
             amount_fen, currency, qr_code_url, form_html, redirect_url,
             transaction_id, channel_status, created_at, updated_at, paid_at, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt.id,
            attempt.order_id,
            attempt.channel.value,
            attempt.account_id,
            attempt.out_trade_no,
            attempt.status.value,
            attempt.amount_fen,
            attempt.currency,
            attempt.qr_code_url,
            attempt.form_html,
            attempt.redirect_url,
            attempt.transaction_id,
            attempt.channel_status,
            to_db(attempt.created_at),
            to_db(attempt.updated_at),
            to_db(attempt.paid_at) if attempt.paid_at else None,
            to_db(attempt.closed_at) if attempt.closed_at else None,
        ),
    )


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> PaymentAttempt | None:
    row = conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (attempt_id,)).fetchone()
    return attempt_from_row(row) if row else None


def get_attempt_by_out_trade_no(
    conn: sqlite3.Connection, out_trade_no: str
) -> PaymentAttempt | None:
    row = conn.execute(
        "SELECT * FROM payment_attempts WHERE out_trade_no = ?", (out_trade_no,)
    ).fetchone()
    return attempt_from_row(row) if row else None


def get_attempt_for_order_channel(
    conn: sqlite3.Connection, order_id: str, channel: Channel
) -> PaymentAttempt | None:
    row = conn.execute(
        "SELECT * FROM payment_attempts WHERE order_id = ? AND channel = ?",
        (order_id, channel.value),
    ).fetchone()
    return attempt_from_row(row) if row else None


def list_attempts(conn: sqlite3.Connection, order_id: str) -> list[PaymentAttempt]:
    rows = conn.execute(
        "SELECT * FROM payment_attempts WHERE order_id = ? ORDER BY created_at",
        (order_id,),
    ).fetchall()
    return [attempt_from_row(row) for row in rows]


def update_attempt_created(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    qr_code_url: str | None,
    form_html: str | None,
    redirect_url: str | None,
    channel_status: str | None,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE payment_attempts
        SET qr_code_url = COALESCE(?, qr_code_url),
            form_html = COALESCE(?, form_html),
            redirect_url = COALESCE(?, redirect_url),
            channel_status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (qr_code_url, form_html, redirect_url, channel_status, to_db(now), attempt_id),
    )


def update_attempt_paid(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    transaction_id: str | None,
    channel_status: str | None,
    paid_at: datetime,
    now: datetime,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE payment_attempts
        SET status = 'paid',
            transaction_id = COALESCE(?, transaction_id),
            channel_status = ?,
            paid_at = COALESCE(paid_at, ?),
            updated_at = ?
        WHERE id = ? AND status IN ('pending', 'closed', 'failed')
        """,
        (transaction_id, channel_status, to_db(paid_at), to_db(now), attempt_id),
    )
    return cursor.rowcount == 1


def update_attempt_state(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    status: AttemptStatus,
    channel_status: str | None,
    now: datetime,
    closed_at: datetime | None = None,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE payment_attempts
        SET status = ?, channel_status = ?, updated_at = ?,
            closed_at = COALESCE(closed_at, ?)
        WHERE id = ? AND status = 'pending'
        """,
        (
            status.value,
            channel_status,
            to_db(now),
            to_db(closed_at) if closed_at else None,
            attempt_id,
        ),
    )
    return cursor.rowcount == 1


def list_pending_attempts(conn: sqlite3.Connection) -> list[PaymentAttempt]:
    rows = conn.execute(
        "SELECT * FROM payment_attempts WHERE status = 'pending' ORDER BY created_at"
    ).fetchall()
    return [attempt_from_row(row) for row in rows]


def list_orders_due_for_expiry(
    conn: sqlite3.Connection, now: datetime, limit: int = 100
) -> list[PaymentOrder]:
    rows = conn.execute(
        """
        SELECT * FROM payment_orders
        WHERE status = 'pending' AND paid_at IS NULL AND expires_at <= ?
        ORDER BY expires_at LIMIT ?
        """,
        (to_db(now), limit),
    ).fetchall()
    return [order_from_row(row) for row in rows]


def list_orders_for_reconcile(
    conn: sqlite3.Connection,
    *,
    created_before: datetime,
    created_after: datetime,
    limit: int = 50,
) -> list[PaymentOrder]:
    rows = conn.execute(
        """
        SELECT * FROM payment_orders
        WHERE status IN ('pending', 'expired')
          AND paid_at IS NULL
          AND created_at <= ?
          AND created_at >= ?
        ORDER BY created_at LIMIT ?
        """,
        (to_db(created_before), to_db(created_after), limit),
    ).fetchall()
    return [order_from_row(row) for row in rows]


def count_orders_by_status(
    conn: sqlite3.Connection, project_id: str | None = None
) -> dict[str, int]:
    if project_id:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM payment_orders WHERE project_id = ? GROUP BY status",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM payment_orders GROUP BY status"
        ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}
