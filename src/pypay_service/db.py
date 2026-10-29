from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .timeutil import to_db, utcnow


class MigrationError(RuntimeError):
    pass


def _run_sql(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a migration without triggering sqlite3's implicit script commit."""
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                conn.execute(statement)
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        conn.execute(remainder)


class Database:
    def __init__(self, path: str | Path, migrations_dir: str | Path | None = None) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations_dir = (
            Path(migrations_dir).expanduser()
            if migrations_dir is not None
            else Path(__file__).with_name("migrations")
        )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _ensure_migration_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

    def applied_versions(self) -> list[str]:
        with self.read() as conn:
            self._ensure_migration_table(conn)
            rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        return [str(row["version"]) for row in rows]

    def _migration_files(self, direction: str) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for path in sorted(self.migrations_dir.glob(f"*.{direction}.sql")):
            version = path.name.split(".", 1)[0]
            files[version] = path
        return files

    def migrate_up(self) -> list[str]:
        up_files = self._migration_files("up")
        applied_now: list[str] = []
        with self.transaction() as conn:
            self._ensure_migration_table(conn)
            applied = {
                str(row["version"])
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version in sorted(up_files):
                if version in applied:
                    continue
                _run_sql(conn, up_files[version].read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, to_db(utcnow())),
                )
                applied_now.append(version)
        return applied_now

    def migrate_down(self, steps: int = 1) -> list[str]:
        if steps < 1:
            raise MigrationError("steps must be at least 1")
        down_files = self._migration_files("down")
        reverted: list[str] = []
        with self.transaction() as conn:
            self._ensure_migration_table(conn)
            applied = [
                str(row["version"])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version DESC"
                ).fetchall()
            ]
            for version in applied[:steps]:
                path = down_files.get(version)
                if path is None:
                    raise MigrationError(f"no down migration for version {version}")
                _run_sql(conn, path.read_text(encoding="utf-8"))
                conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
                reverted.append(version)
        return reverted

    def healthcheck(self) -> bool:
        with self.read() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)
