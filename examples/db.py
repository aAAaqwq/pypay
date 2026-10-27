import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pypay import NotificationEvent, PaymentNotification

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    out_trade_no TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    amount_fen INTEGER NOT NULL CHECK (amount_fen > 0),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    transaction_id TEXT,
    refunded_amount_fen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    fulfilled_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    notification_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    out_trade_no TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (channel, notification_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
"""


class OrderExists(Exception):
    pass


class OrderNotFound(Exception):
    pass


class AmountMismatch(Exception):
    pass


class ChannelMismatch(Exception):
    pass


@dataclass(frozen=True)
class Order:
    out_trade_no: str
    channel: str
    amount_fen: int
    description: str
    status: str
    transaction_id: str | None
    refunded_amount_fen: int
    created_at: str
    paid_at: str | None
    fulfilled_at: str | None


@dataclass(frozen=True)
class ApplyResult:
    duplicate: bool
    newly_paid: bool
    ignored: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_order(
        self, out_trade_no: str, channel: str, amount_fen: int, description: str
    ) -> None:
        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orders
                        (out_trade_no, channel, amount_fen, description, status, created_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (out_trade_no, channel, amount_fen, description, _now()),
                )
        except sqlite3.IntegrityError as exc:
            raise OrderExists(out_trade_no) from exc

    def get_order(self, out_trade_no: str) -> Order | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE out_trade_no = ?", (out_trade_no,)
            ).fetchone()
        return _row_to_order(row) if row else None

    def pending_orders(self) -> list[Order]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [_row_to_order(row) for row in rows]

    def apply_notification(self, notification: PaymentNotification) -> ApplyResult:
        with self._connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO notifications
                        (channel, notification_id, event_type, out_trade_no, received_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        notification.channel.value,
                        notification.notification_id,
                        notification.event_type.value,
                        notification.out_trade_no,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError:
                return ApplyResult(duplicate=True, newly_paid=False)

            row = conn.execute(
                "SELECT channel, amount_fen, status FROM orders WHERE out_trade_no = ?",
                (notification.out_trade_no,),
            ).fetchone()
            if row is None:
                raise OrderNotFound(notification.out_trade_no)
            if notification.channel.value != row["channel"]:
                raise ChannelMismatch(notification.out_trade_no)
            if notification.amount_fen != row["amount_fen"]:
                raise AmountMismatch(notification.out_trade_no)

            if notification.event_type is NotificationEvent.PAYMENT_SUCCEEDED:
                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET status = 'paid',
                        transaction_id = ?,
                        paid_at = ?,
                        fulfilled_at = COALESCE(fulfilled_at, ?)
                    WHERE out_trade_no = ?
                      AND status NOT IN ('paid', 'refunded')
                    """,
                    (
                        notification.transaction_id,
                        notification.occurred_at.isoformat()
                        if notification.occurred_at
                        else _now(),
                        _now(),
                        notification.out_trade_no,
                    ),
                )
                return ApplyResult(duplicate=False, newly_paid=cursor.rowcount == 1)

            if notification.event_type is NotificationEvent.PAYMENT_CLOSED:
                conn.execute(
                    "UPDATE orders SET status = 'closed' WHERE out_trade_no = ? AND status = 'pending'",
                    (notification.out_trade_no,),
                )
                return ApplyResult(duplicate=False, newly_paid=False)

            if notification.event_type is NotificationEvent.PAYMENT_FAILED:
                conn.execute(
                    "UPDATE orders SET status = 'failed' WHERE out_trade_no = ? AND status = 'pending'",
                    (notification.out_trade_no,),
                )
                return ApplyResult(duplicate=False, newly_paid=False)

            if (
                notification.event_type is NotificationEvent.REFUND_SUCCEEDED
                and notification.refund_amount_fen
            ):
                conn.execute(
                    """
                    UPDATE orders
                    SET refunded_amount_fen = refunded_amount_fen + ?,
                        status = CASE
                            WHEN refunded_amount_fen + ? >= amount_fen THEN 'refunded'
                            ELSE status
                        END
                    WHERE out_trade_no = ?
                      AND status IN ('paid', 'refunded')
                    """,
                    (
                        notification.refund_amount_fen,
                        notification.refund_amount_fen,
                        notification.out_trade_no,
                    ),
                )
                return ApplyResult(duplicate=False, newly_paid=False)

            return ApplyResult(duplicate=False, newly_paid=False, ignored=True)

    def mark_paid_from_query(
        self, out_trade_no: str, amount_fen: int, transaction_id: str | None, paid_at: str | None
    ) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT amount_fen FROM orders WHERE out_trade_no = ?", (out_trade_no,)
            ).fetchone()
            if row is None:
                raise OrderNotFound(out_trade_no)
            if amount_fen != row["amount_fen"]:
                raise AmountMismatch(out_trade_no)
            cursor = conn.execute(
                """
                UPDATE orders
                SET status = 'paid',
                    transaction_id = COALESCE(?, transaction_id),
                    paid_at = COALESCE(?, ?),
                    fulfilled_at = COALESCE(fulfilled_at, ?)
                WHERE out_trade_no = ?
                  AND status NOT IN ('paid', 'refunded')
                """,
                (transaction_id, paid_at, _now(), _now(), out_trade_no),
            )
            return cursor.rowcount == 1

    def mark_closed_from_query(self, out_trade_no: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE orders SET status = 'closed' WHERE out_trade_no = ? AND status = 'pending'",
                (out_trade_no,),
            )
            return cursor.rowcount == 1

    def mark_failed(self, out_trade_no: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE orders SET status = 'failed' WHERE out_trade_no = ? AND status = 'pending'",
                (out_trade_no,),
            )
            return cursor.rowcount == 1

    def mark_refunded_from_query(self, out_trade_no: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE orders
                SET status = 'refunded', refunded_amount_fen = amount_fen
                WHERE out_trade_no = ? AND status IN ('paid', 'refunded')
                """,
                (out_trade_no,),
            )
            return cursor.rowcount == 1


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        out_trade_no=row["out_trade_no"],
        channel=row["channel"],
        amount_fen=row["amount_fen"],
        description=row["description"],
        status=row["status"],
        transaction_id=row["transaction_id"],
        refunded_amount_fen=row["refunded_amount_fen"],
        created_at=row["created_at"],
        paid_at=row["paid_at"],
        fulfilled_at=row["fulfilled_at"],
    )
