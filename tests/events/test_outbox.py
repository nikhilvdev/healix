"""Durable webhook delivery: what survives a down receiver, a restart, and a rejected event."""

import json
import os
import sqlite3
import time

import pytest

from healix.events import (
    DurableWebhookSender,
    Outbox,
    OutboxError,
    WebhookSender,
    open_sender,
    validate_outbox,
)
from healix.events.webhook import post_with_retries
from tests.conftest import WebhookReceiver

DEAD_URL = "http://127.0.0.1:1/hook"  # nothing listens here


def event(n=1, name="page_discovered", run_id="r"):
    return {
        "event": name,
        "run_id": run_id,
        "timestamp": "2026-09-20T00:00:00.000Z",
        "data": {"n": n},
    }


def sender(url, outbox, **kwargs):
    kwargs.setdefault("backoff", 0.0)
    kwargs.setdefault("sleep", lambda s: None)
    kwargs.setdefault("retry_interval", 0.01)
    kwargs.setdefault("max_retry_interval", 0.05)
    kwargs.setdefault("timeout", 1.0)
    return DurableWebhookSender(url, outbox, **kwargs)


def numbers(receiver):
    return [p["data"]["n"] for p in receiver.payloads]


def delivery_ids(receiver):
    return [r["headers"]["X-Healix-Delivery"] for r in receiver.requests]


@pytest.fixture
def outbox_path(tmp_path):
    return tmp_path / "events.db"


# --- the outbox file ------------------------------------------------------------ #


def test_events_come_back_oldest_first_and_only_until_they_are_delivered(outbox_path):
    outbox = Outbox(outbox_path)
    for n in range(3):
        outbox.add("page_discovered", "r", json.dumps({"n": n}).encode())
    first = outbox.next_pending()
    assert json.loads(first.body) == {"n": 0}
    outbox.delivered(first.seq)
    assert json.loads(outbox.next_pending().body) == {"n": 1}
    assert outbox.counts() == {"pending": 2, "dead": 0}
    outbox.close()


def test_a_stored_event_survives_reopening_the_file_with_the_same_id(outbox_path):
    first = Outbox(outbox_path)
    delivery_id = first.add("run_complete", "r", b'{"a":"\xc3\xa9"}')
    first.close()
    entry = Outbox(outbox_path).next_pending()
    assert entry.delivery_id == delivery_id
    assert entry.body == b'{"a":"\xc3\xa9"}'  # the exact bytes, so a signature still matches


def test_a_rejected_event_is_kept_but_no_longer_pending_until_asked_for(outbox_path):
    outbox = Outbox(outbox_path)
    outbox.add("page_discovered", "r", b"{}")
    entry = outbox.next_pending()
    outbox.dead(entry.seq, 1, "HTTP 400")
    assert outbox.next_pending() is None
    assert outbox.counts() == {"pending": 0, "dead": 1}
    assert outbox.retry_dead() == 1
    assert outbox.next_pending().seq == entry.seq
    outbox.close()


@pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX idea")
def test_the_outbox_file_is_readable_by_its_owner_only(outbox_path):
    Outbox(outbox_path).close()
    assert outbox_path.stat().st_mode & 0o777 == 0o600


def test_the_folder_is_created_when_it_does_not_exist(tmp_path):
    Outbox(tmp_path / "deep" / "er" / "events.db").close()
    assert (tmp_path / "deep" / "er" / "events.db").is_file()


def test_a_file_that_is_not_an_outbox_is_refused_with_a_clear_error(outbox_path):
    outbox_path.write_text("this is not a database")
    with pytest.raises(OutboxError, match="events.db"):
        Outbox(outbox_path)


def test_an_outbox_needs_somewhere_to_deliver_to(outbox_path):
    with pytest.raises(ValueError, match="webhook_url"):
        validate_outbox(None, outbox_path)
    assert validate_outbox("https://h.example/x", None) is None
    assert validate_outbox("https://h.example/x", outbox_path) == outbox_path


def test_open_sender_is_best_effort_unless_an_outbox_is_given(outbox_path, webhook_receiver):
    plain = open_sender(webhook_receiver.url)
    durable = open_sender(webhook_receiver.url, outbox=outbox_path)
    try:
        assert type(plain) is WebhookSender
        assert type(durable) is DurableWebhookSender
    finally:
        plain.close()
        durable.close()


# --- delivery ------------------------------------------------------------------- #


def test_delivers_the_same_payload_the_best_effort_sender_would(outbox_path, webhook_receiver):
    with sender(webhook_receiver.url, outbox_path, secret="s3cret") as hook:
        hook.send(event(1, "run_complete"))
    [request] = webhook_receiver.requests
    assert json.loads(request["body"]) == event(1, "run_complete")
    assert request["headers"]["X-Healix-Event"] == "run_complete"
    assert request["headers"]["X-Healix-Signature"].startswith("sha256=")
    assert hook.delivered == 1 and hook.pending == 0


def test_events_arrive_in_order_and_leave_nothing_behind(outbox_path, webhook_receiver):
    with sender(webhook_receiver.url, outbox_path) as hook:
        for n in range(25):
            hook.send(event(n))
    assert numbers(webhook_receiver) == list(range(25))
    assert Outbox(outbox_path).counts() == {"pending": 0, "dead": 0}


def test_every_event_has_its_own_delivery_id(outbox_path, webhook_receiver):
    with sender(webhook_receiver.url, outbox_path) as hook:
        for n in range(5):
            hook.send(event(n))
    assert len(set(delivery_ids(webhook_receiver))) == 5


def test_a_failing_receiver_is_retried_until_it_answers_and_the_id_does_not_change(
    outbox_path, webhook_receiver
):
    webhook_receiver.respond_with(503, 503, 503, 503, 503, 503)  # two full rounds of attempts
    with sender(webhook_receiver.url, outbox_path, max_attempts=3) as hook:
        hook.send(event(1))
        hook.send(event(2))
        assert hook.wait_until_delivered(10)
    assert numbers(webhook_receiver) == [1] * 7 + [2]  # six refusals, then the one that worked
    ids = delivery_ids(webhook_receiver)
    assert len(set(ids[:7])) == 1  # every attempt at event 1 carries one id
    assert ids[7] != ids[0]
    assert hook.delivered == 2


def test_a_later_event_is_never_sent_ahead_of_one_that_is_still_failing(
    outbox_path, webhook_receiver
):
    webhook_receiver.respond_with(500, 500, 500)
    with sender(
        webhook_receiver.url,
        outbox_path,
        max_attempts=3,
        retry_interval=0.2,
        max_retry_interval=0.2,
    ) as hook:
        hook.send(event(1))
        hook.send(event(2))
        assert hook.wait_until_delivered(10)
    assert numbers(webhook_receiver) == [1, 1, 1, 1, 2]


def test_the_receiver_can_be_stopped_and_brought_back_mid_run(outbox_path):
    first = WebhookReceiver()
    hook = sender(first.url, outbox_path, max_attempts=1, timeout=0.5)
    try:
        hook.send(event(1))
        hook.send(event(2))
        assert hook.wait_until_delivered(10)
        first.close()  # the receiver dies mid-run
        for n in (3, 4, 5):
            hook.send(event(n))
        time.sleep(0.3)
        assert hook.pending == 3  # held, not lost, and the run was not disturbed
        second = WebhookReceiver(port=first.port)  # and comes back
        try:
            assert hook.wait_until_delivered(10)
            assert numbers(second) == [3, 4, 5]  # in order, each once
        finally:
            second.close()
    finally:
        hook.close()
    assert numbers(first) == [1, 2]


def test_events_left_behind_by_a_run_are_sent_first_by_the_next_one(outbox_path, webhook_receiver):
    down = sender(DEAD_URL, outbox_path, max_attempts=1, timeout=0.3)
    for n in (1, 2, 3):
        down.send(event(n, run_id="old-run"))
    down.close(timeout=3)
    assert Outbox(outbox_path).counts()["pending"] == 3  # the run ended with the receiver down

    with sender(webhook_receiver.url, outbox_path) as later:
        later.send(event(4, run_id="new-run"))
    assert numbers(webhook_receiver) == [1, 2, 3, 4]
    assert [p["run_id"] for p in webhook_receiver.payloads] == ["old-run"] * 3 + ["new-run"]


def test_a_receiver_that_saw_an_event_but_was_never_told_gets_it_again_with_the_same_id(
    outbox_path,
):
    """At least once: a crash between "accepted" and "forgotten" means a second delivery."""
    first, second = WebhookReceiver(), WebhookReceiver()
    try:
        outbox = Outbox(outbox_path)
        outbox.add("page_discovered", "r", json.dumps(event(1)).encode())
        entry = outbox.next_pending()
        # What a sender that crashed did: the receiver accepted it, then nothing was recorded.
        outcome = post_with_retries(
            first.url, entry.body, entry.event, delivery_id=entry.delivery_id
        )
        assert outcome.status == "delivered"
        outbox.close()

        with sender(second.url, outbox_path) as again:
            assert again.wait_until_delivered(5)
        assert numbers(first) == numbers(second) == [1]
        assert delivery_ids(first) == delivery_ids(second)  # the receiver can tell it is a repeat
    finally:
        first.close()
        second.close()


def test_a_rejected_event_is_set_aside_and_the_ones_after_it_still_go(
    outbox_path, webhook_receiver
):
    webhook_receiver.respond_with(200, 400, 200)
    with sender(webhook_receiver.url, outbox_path) as hook:
        for n in (1, 2, 3):
            hook.send(event(n))
    assert numbers(webhook_receiver) == [1, 2, 3]
    assert hook.delivered == 2 and hook.failed == 1
    assert Outbox(outbox_path).counts() == {"pending": 0, "dead": 1}


def test_rejected_events_can_be_sent_again_on_request(outbox_path, webhook_receiver):
    webhook_receiver.respond_with(400)
    with sender(webhook_receiver.url, outbox_path) as hook:
        hook.send(event(1))
    outbox = Outbox(outbox_path)
    assert outbox.retry_dead() == 1
    with sender(webhook_receiver.url, outbox) as hook:
        assert hook.wait_until_delivered(5)
    assert numbers(webhook_receiver) == [1, 1]
    assert outbox.counts() == {"pending": 0, "dead": 0}
    outbox.close()


def test_close_leaves_the_undelivered_in_the_file_and_says_so(outbox_path, captured_logs):
    hook = sender(DEAD_URL, outbox_path, max_attempts=1, timeout=0.3)
    hook.send(event(1))
    hook.send(event(2))
    hook.close(timeout=3)
    assert Outbox(outbox_path).counts()["pending"] == 2
    warning = next(r for r in captured_logs if "still in the outbox" in r["message"])
    assert warning["meta"]["pending"] == 2


def test_close_makes_a_last_attempt_at_what_is_left(outbox_path, webhook_receiver):
    hook = sender(webhook_receiver.url, outbox_path, retry_interval=30, max_retry_interval=30)
    webhook_receiver.respond_with(503, 503, 503)  # the first round fails; the worker will now sleep
    hook.send(event(1))
    deadline = time.monotonic() + 5
    while len(webhook_receiver.requests) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    hook.close(timeout=5)  # must not wait out the 30 s interval
    assert numbers(webhook_receiver) == [1, 1, 1, 1]
    assert Outbox(outbox_path).counts()["pending"] == 0


def test_sending_after_close_is_dropped_not_raised(outbox_path, webhook_receiver):
    hook = sender(webhook_receiver.url, outbox_path)
    hook.close()
    hook.send(event())
    assert hook.dropped == 1 and webhook_receiver.requests == []
    hook.close()  # idempotent


def test_an_event_that_cannot_be_stored_is_dropped_without_stopping_the_run(
    outbox_path, webhook_receiver
):
    hook = sender(webhook_receiver.url, outbox_path)

    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    hook._outbox.add = disk_full
    hook.send(event())
    hook.close()
    assert hook.dropped == 1 and webhook_receiver.requests == []


def test_neither_the_url_nor_the_secret_is_written_to_the_file(outbox_path, webhook_receiver):
    url = webhook_receiver.url + "?token=SUPER-SECRET-TOKEN"
    with sender(url, outbox_path, secret="hmac-secret-value") as hook:
        hook.send(event())
    raw = outbox_path.read_bytes()
    assert b"SUPER-SECRET-TOKEN" not in raw and b"hmac-secret-value" not in raw

    down = sender(DEAD_URL + "?token=SUPER-SECRET-TOKEN", outbox_path, secret="hmac-secret-value")
    down.send(event())
    down.close(timeout=3)
    rows = sqlite3.connect(outbox_path).execute("SELECT * FROM healix_outbox").fetchall()
    assert rows and "SUPER-SECRET-TOKEN" not in json.dumps(rows, default=str)
    assert "hmac-secret-value" not in json.dumps(rows, default=str)


def test_failures_are_logged_without_the_url(outbox_path, captured_logs):
    down = sender(DEAD_URL + "?token=SUPER-SECRET-TOKEN", outbox_path, max_attempts=1, timeout=0.3)
    down.send(event())
    down.close(timeout=3)
    assert "SUPER-SECRET-TOKEN" not in json.dumps(captured_logs)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"retry_interval": 0}, {"retry_interval": 10, "max_retry_interval": 1}],
)
def test_bad_settings_are_rejected(outbox_path, kwargs):
    with pytest.raises(ValueError):
        DurableWebhookSender("http://127.0.0.1:1/x", outbox_path, **kwargs)


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://e.com/x", "/relative"])
def test_the_url_must_be_absolute_http(outbox_path, bad):
    with pytest.raises(ValueError, match="http"):
        DurableWebhookSender(bad, outbox_path)
