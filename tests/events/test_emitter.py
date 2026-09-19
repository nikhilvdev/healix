import json

import pytest

from healix.events import EventEmitter, EventError, WebhookSender

COMPLETE = {
    "pages_discovered": 2,
    "pages_extracted": 2,
    "platform_detected": None,
    "manifest_path": "output/manifest.json",
}


def test_emit_returns_the_validated_payload_and_calls_on_event():
    seen = []
    emitter = EventEmitter("run-1", on_event=seen.append)
    payload = emitter.emit("run_complete", **COMPLETE)
    assert seen == [payload]
    assert payload["event"] == "run_complete" and payload["run_id"] == "run-1"
    assert payload["data"] == COMPLETE


def test_callback_and_webhook_receive_the_identical_payload(webhook_receiver):
    seen = []
    sender = WebhookSender(webhook_receiver.url)
    EventEmitter("run-1", on_event=seen.append, sender=sender).emit("run_complete", **COMPLETE)
    sender.close()
    assert webhook_receiver.payloads == seen  # including the timestamp


def test_a_raising_callback_is_logged_and_swallowed_and_the_webhook_still_gets_it(
    webhook_receiver, captured_logs
):
    def boom(payload):
        raise RuntimeError("callback bug")

    sender = WebhookSender(webhook_receiver.url)
    emitter = EventEmitter("r", on_event=boom, sender=sender)
    emitter.emit("run_complete", **COMPLETE)  # must not raise
    sender.close()
    assert len(webhook_receiver.requests) == 1
    logged = next(
        r for r in captured_logs if r["message"] == "on_event callback raised; continuing"
    )
    assert logged["meta"]["error"] == "callback bug"


def test_an_invalid_event_raises_and_delivers_nothing(webhook_receiver):
    seen = []
    sender = WebhookSender(webhook_receiver.url)
    emitter = EventEmitter("r", on_event=seen.append, sender=sender)
    with pytest.raises(EventError):
        emitter.emit("run_complete", pages_discovered=1)
    sender.close()
    assert seen == [] and webhook_receiver.requests == []


def test_no_sinks_is_fine():
    assert EventEmitter("r").emit("run_complete", **COMPLETE)["event"] == "run_complete"


def test_the_clock_is_injectable():
    from datetime import datetime, timezone

    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    payload = EventEmitter("r", clock=lambda: fixed).emit("run_complete", **COMPLETE)
    assert payload["timestamp"] == "2026-01-02T03:04:05.000Z"
    assert json.dumps(payload)  # JSON-serializable
