from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db(value: datetime) -> str:
    """Serialize in a fixed UTC form so SQLite string ordering is chronological."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_db(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("database timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_iso(value: str) -> datetime:
    return from_db(value)


def plus_seconds(value: datetime, seconds: float) -> datetime:
    return value + timedelta(seconds=seconds)


def seconds_until(value: datetime, now: datetime) -> int:
    return max(0, int((value - now).total_seconds()))
