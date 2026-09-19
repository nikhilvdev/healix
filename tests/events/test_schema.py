import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from healix.events import (
    EVENT_DATA_FIELDS,
    EVENT_TYPES,
    LOGIN_FAILURE_REASONS,
    Event,
    EventError,
    make_event,
)

VALID = {
    "page_discovered": {"url": "https://e.com/", "page_type": "list", "structural_hash": "abc"},
    "page_extracted": {
        "url": "https://e.com/",
        "page_type": "list",
        "element_count": 12,
        "output_file": "pages/0001-e.com.json",
    },
    "element_healed": {
        "element_key": "login/username",
        "old_locator": "#user-4471",
        "new_locator": "[data-testid=u]",
        "strategy_used": "stable_attr",
        "confidence_score": 0.82,
        "page_url": "https://e.com/login",
    },
    "script_generated": {
        "backend": "playwright",
        "style": "pom",
        "file_path": "out/login.py",
        "element_count": 3,
    },
    "run_complete": {
        "pages_discovered": 4,
        "pages_extracted": 4,
        "platform_detected": None,
        "manifest_path": "output/manifest.json",
    },
    "login_failed": {
        "url": "https://e.com/login",
        "reason": "mfa_required",
        "screenshot_ref": None,
    },
}


def test_the_six_documented_event_types():
    assert set(EVENT_TYPES) == set(VALID) == set(EVENT_DATA_FIELDS)
    assert len(EVENT_TYPES) == 6


@pytest.mark.parametrize("event", EVENT_TYPES)
def test_valid_events_build_and_serialize_to_the_envelope(event):
    built = make_event(event, "run-1", VALID[event])
    payload = built.to_dict()
    assert list(payload) == ["event", "run_id", "timestamp", "data"]
    assert payload["event"] == event and payload["run_id"] == "run-1"
    assert payload["data"] == VALID[event]


@pytest.mark.parametrize("event", EVENT_TYPES)
def test_every_data_field_is_required(event):
    for field in EVENT_DATA_FIELDS[event]:
        data = {k: v for k, v in VALID[event].items() if k != field}
        with pytest.raises(EventError, match=field):
            make_event(event, "r", data)


@pytest.mark.parametrize("event", EVENT_TYPES)
def test_unknown_data_fields_are_rejected(event):
    with pytest.raises(EventError, match="unknown data fields"):
        make_event(event, "r", {**VALID[event], "surprise": 1})


def test_unknown_event_type_and_bad_run_id_are_rejected():
    with pytest.raises(EventError, match="unknown event type"):
        make_event("page_deleted", "r", {})
    for bad in ("", None, 7):
        with pytest.raises(EventError, match="run_id"):
            make_event("run_complete", bad, VALID["run_complete"])


@pytest.mark.parametrize("reason", LOGIN_FAILURE_REASONS)
def test_login_failed_accepts_each_documented_reason(reason):
    make_event("login_failed", "r", {**VALID["login_failed"], "reason": reason})


def test_login_failed_rejects_other_reasons():
    with pytest.raises(EventError, match="reason"):
        make_event("login_failed", "r", {**VALID["login_failed"], "reason": "bad_vibes"})
    assert LOGIN_FAILURE_REASONS == (
        "mfa_required",
        "timeout",
        "selector_not_found",
        "auth_rejected",
    )


def test_timestamp_is_utc_iso_with_milliseconds_and_can_be_supplied():
    stamped = make_event(
        "run_complete",
        "r",
        VALID["run_complete"],
        timestamp=datetime(2026, 9, 19, 1, 2, 3, 456000, tzinfo=timezone.utc),
    )
    assert stamped.timestamp == "2026-09-19T01:02:03.456Z"
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z",
        make_event("run_complete", "r", VALID["run_complete"]).timestamp,
    )


def test_to_dict_returns_a_copy_of_data():
    event = make_event("run_complete", "r", VALID["run_complete"])
    event.to_dict()["data"]["pages_extracted"] = 99
    assert event.data["pages_extracted"] == 4


def test_event_is_immutable():
    event = make_event("run_complete", "r", VALID["run_complete"])
    with pytest.raises(AttributeError):
        event.run_id = "other"  # type: ignore[misc]
    assert isinstance(event, Event)


def test_readme_event_table_matches_the_schema():
    readme = (Path(__file__).parents[2] / "README.md").read_text()
    section = readme.split("## Events", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \| (.+?) \|$", section, flags=re.MULTILINE)
    documented = {name: re.findall(r"`(\w+)`", fields) for name, fields in rows}
    assert set(documented) == set(EVENT_TYPES)
    for event, fields in documented.items():
        assert fields == list(EVENT_DATA_FIELDS[event]), event
