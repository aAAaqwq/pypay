"""Copyable FastAPI receiver for pypay business notifications.

Required environment variables:
  PROJECT_NOTIFY_SECRET   the project's notify secret from pypay
  DATABASE_PATH           optional, defaults to ./var/business_receiver.db

The receiver acknowledges an event only after its own transaction commits.
It deduplicates by event_id and by business order number, so retries and
duplicate deliveries do not grant an entitlement twice.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from pypay_service.client import WebhookSignatureError, verify_webhook

MAX_BODY_BYTES = 256 * 1024
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "./var/business_receiver.db"))
NOTIFY_SECRET = os.environ.get("PROJECT_NOTIFY_SECRET", "")
if not NOTIFY_SECRET:
    raise RuntimeError("set PROJECT_NOTIFY_SECRET before starting the receiver")

SCHEMA = """
CREATE TABLE IF NOT EXISTS received_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    business_order_no TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entitlements (
    business_order_no TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL,
    amount_fen INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    granted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds_seen (
    refund_id TEXT PRIMARY KEY,
    business_order_no TEXT NOT NULL,
    amount_fen INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DATABASE_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


_init_db()
app = FastAPI(title="pypay business receiver example", docs_url=None, redoc_url=None)


@app.post("/pypay/notify")
async def receive_notification(request: Request) -> JSONResponse:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    try:
        event = verify_webhook(NOTIFY_SECRET, request.headers, body)
    except WebhookSignatureError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)

    event_id = str(event.get("event_id") or "")
    event_type = str(event.get("event_type") or "")
    business_order_no = str(event.get("business_order_no") or "")
    payment_id = str(event.get("payment_id") or "")
    amount_fen = event.get("amount_fen")
    if not event_id or not event_type or not business_order_no or not payment_id:
        return JSONResponse({"ok": False, "error": "missing event fields"}, status_code=400)
    if not isinstance(amount_fen, int) or isinstance(amount_fen, bool) or amount_fen <= 0:
        return JSONResponse({"ok": False, "error": "invalid amount"}, status_code=400)

    with connection() as conn:
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO received_events
                (event_id, event_type, business_order_no, payment_id, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, event_type, business_order_no, payment_id, _now()),
        )
        if inserted.rowcount == 0:
            return JSONResponse({"ok": True, "duplicate": True})

        if event_type == "payment.succeeded":
            # Replace this with the real grant: add quota, activate a plan,
            # create a digital-human service record, etc. The unique key keeps
            # repeated deliveries from granting twice.
            conn.execute(
                """
                INSERT OR IGNORE INTO entitlements
                    (business_order_no, payment_id, amount_fen, event_id, granted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (business_order_no, payment_id, amount_fen, event_id, _now()),
            )
        elif event_type == "refund.succeeded":
            refund_id = str(event.get("refund_id") or event_id)
            refund_amount = event.get("refund_amount_fen")
            if isinstance(refund_amount, int) and not isinstance(refund_amount, bool):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO refunds_seen
                        (refund_id, business_order_no, amount_fen, event_id, seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (refund_id, business_order_no, refund_amount, event_id, _now()),
                )
            # Revoke or reduce the entitlement in this same transaction if your
            # business policy requires it.
        # Unknown event types are acknowledged after being recorded so the
        # service does not retry them forever.
    return JSONResponse({"ok": True})


@app.get("/entitlements/{business_order_no}")
async def get_entitlement(business_order_no: str) -> dict[str, object]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM entitlements WHERE business_order_no = ?", (business_order_no,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="entitlement not found")
    return dict(row)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8098)
