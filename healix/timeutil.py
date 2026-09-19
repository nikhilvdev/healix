"""UTC timestamp helpers shared by events and extraction output."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """``2026-09-19T12:30:45.123Z`` — ISO 8601 in UTC, millisecond precision."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
