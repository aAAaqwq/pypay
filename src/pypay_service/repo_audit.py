from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .models import AuditEntry
from .timeutil import from_db, to_db


def insert_audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    role: str,
    action: str,
    project_id: str | None,
    order_id: str | None,
    details: dict[str, object] | None,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs
            (actor, role, action, project_id, order_id, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor,
            role,
            action,
            project_id,
            order_id,
            json.dumps(details or {}, ensure_ascii=False),
            to_db(now),
        ),
    )


def audit_from_row(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=int(row["id"]),
        actor=row["actor"],
        role=row["role"],
        action=row["action"],
        project_id=row["project_id"],
        order_id=row["order_id"],
        details=json.loads(row["details_json"] or "{}"),
        created_at=from_db(row["created_at"]),
    )


def list_audit(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    project_id: str | None = None,
    order_id: str | None = None,
) -> list[AuditEntry]:
    clauses: list[str] = []
    values: list[object] = []
    if project_id:
        clauses.append("project_id = ?")
        values.append(project_id)
    if order_id:
        clauses.append("order_id = ?")
        values.append(order_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values.append(limit)
    rows = conn.execute(
        f"SELECT * FROM audit_logs{where} ORDER BY id DESC LIMIT ?", values
    ).fetchall()
    return [audit_from_row(row) for row in rows]
