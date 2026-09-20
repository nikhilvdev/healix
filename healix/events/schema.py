"""The event schema shared by the SDK, the CLI, and webhooks.

Every event has the same envelope::

    {"event": "...", "run_id": "...", "timestamp": "2026-09-19T12:30:45.123Z", "data": {...}}

and a ``data`` shape fixed by the event type (``EVENT_DATA_FIELDS``). In a multi-role run every
event also carries ``"role"`` (the user role it happened under); it is left out of a run that has
no roles, so a single-credential run's payloads are exactly what they always were. The SDK's
``on_event`` callback and the CLI's ``--webhook-url`` both emit exactly this
payload. ``make_event`` validates it — required keys present, no unknown keys — so
the contract can't drift silently. Adding an event type means adding it here *and*
to the event table in the README (a test keeps the two in sync).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from healix.timeutil import iso_utc, utc_now

PAGE_DISCOVERED = "page_discovered"
PAGE_EXTRACTED = "page_extracted"
ELEMENT_HEALED = "element_healed"
SCRIPT_GENERATED = "script_generated"
RUN_COMPLETE = "run_complete"
LOGIN_FAILED = "login_failed"
ROLES_COMPARED = "roles_compared"

EVENT_TYPES = (
    PAGE_DISCOVERED,
    PAGE_EXTRACTED,
    ELEMENT_HEALED,
    SCRIPT_GENERATED,
    RUN_COMPLETE,
    LOGIN_FAILED,
    ROLES_COMPARED,
)

# The keys of ``data`` for each event type — exactly these, no more and no fewer.
EVENT_DATA_FIELDS: dict[str, tuple[str, ...]] = {
    PAGE_DISCOVERED: ("url", "page_type", "structural_hash"),
    PAGE_EXTRACTED: ("url", "page_type", "element_count", "output_file"),
    ELEMENT_HEALED: (
        "element_key",
        "old_locator",
        "new_locator",
        "strategy_used",
        "confidence_score",
        "page_url",
    ),
    SCRIPT_GENERATED: ("backend", "style", "file_path", "element_count"),
    RUN_COMPLETE: ("pages_discovered", "pages_extracted", "platform_detected", "manifest_path"),
    LOGIN_FAILED: ("url", "reason", "screenshot_ref"),
    ROLES_COMPARED: ("roles", "diff_path", "differences"),
}

LOGIN_FAILURE_REASONS = ("mfa_required", "timeout", "selector_not_found", "auth_rejected")


class EventError(ValueError):
    """An event that doesn't match the schema."""


@dataclass(frozen=True)
class Event:
    event: str
    run_id: str
    timestamp: str
    data: dict[str, Any]
    role: str | None = None

    def __post_init__(self) -> None:
        if self.event not in EVENT_DATA_FIELDS:
            raise EventError(f"unknown event type {self.event!r}; expected one of {EVENT_TYPES}")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise EventError("run_id must be a non-empty string")
        expected = set(EVENT_DATA_FIELDS[self.event])
        actual = set(self.data)
        if missing := expected - actual:
            raise EventError(f"{self.event} is missing data fields: {sorted(missing)}")
        if extra := actual - expected:
            raise EventError(f"{self.event} has unknown data fields: {sorted(extra)}")
        if self.event == LOGIN_FAILED and self.data["reason"] not in LOGIN_FAILURE_REASONS:
            raise EventError(
                f"login_failed reason must be one of {LOGIN_FAILURE_REASONS}, "
                f"got {self.data['reason']!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """The wire payload, identical for callbacks and webhooks."""
        payload: dict[str, Any] = {
            "event": self.event,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "data": dict(self.data),
        }
        if self.role:
            payload["role"] = self.role
        return payload


def make_event(
    event: str,
    run_id: str,
    data: dict[str, Any],
    *,
    timestamp: datetime | None = None,
    role: str | None = None,
) -> Event:
    """Build a validated ``Event`` stamped with ``timestamp`` (default: now, UTC)."""
    return Event(event, run_id, iso_utc(timestamp or utc_now()), dict(data), role)
