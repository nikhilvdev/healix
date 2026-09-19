import hashlib
import hmac
import json
import threading

import pytest

from healix import __version__
from healix.events import WebhookSender, sign_body, validate_webhook_url


def event(n=1, name="page_discovered"):
    return {"event": name, "run_id": "r", "timestamp": "2026-09-19T00:00:00.000Z", "data": {"n": n}}


def sender(url, **kwargs):
    kwargs.setdefault("backoff", 0.0)
    kwargs.setdefault("sleep", lambda s: None)
    return WebhookSender(url, **kwargs)


def test_posts_the_payload_as_json_with_identifying_headers(webhook_receiver):
    with sender(webhook_receiver.url) as hook:
        hook.send(event(1, "run_complete"))
    [request] = webhook_receiver.requests
    assert request["path"] == "/hook"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["X-Healix-Event"] == "run_complete"
    assert request["headers"]["User-Agent"] == f"healix/{__version__}"
    assert "X-Healix-Signature" not in request["headers"]
    assert json.loads(request["body"]) == event(1, "run_complete")


def test_signature_is_the_hmac_of_the_exact_body(webhook_receiver):
    with sender(webhook_receiver.url, secret="s3cret") as hook:
        hook.send(event())
    [request] = webhook_receiver.requests
    expected = "sha256=" + hmac.new(b"s3cret", request["body"], hashlib.sha256).hexdigest()
    assert request["headers"]["X-Healix-Signature"] == expected
    assert sign_body("s3cret", request["body"]) == expected


def test_events_are_delivered_in_order_and_close_flushes_the_queue(webhook_receiver):
    hook = sender(webhook_receiver.url)
    for n in range(20):
        hook.send(event(n))
    hook.close()
    assert [p["data"]["n"] for p in webhook_receiver.payloads] == list(range(20))
    assert hook.delivered == 20 and hook.failed == 0


def test_server_errors_are_retried_with_exponential_backoff_then_succeed(webhook_receiver):
    webhook_receiver.respond_with(500, 503, 200)
    sleeps = []
    with sender(webhook_receiver.url, backoff=0.5, sleep=sleeps.append) as hook:
        hook.send(event())
    assert len(webhook_receiver.requests) == 3
    assert sleeps == [0.5, 1.0]
    assert hook.delivered == 1 and hook.failed == 0


@pytest.mark.parametrize("status", [429, 408])
def test_throttling_statuses_are_retried(webhook_receiver, status):
    webhook_receiver.respond_with(status, 200)
    with sender(webhook_receiver.url) as hook:
        hook.send(event())
    assert len(webhook_receiver.requests) == 2 and hook.delivered == 1


def test_client_errors_are_not_retried(webhook_receiver):
    webhook_receiver.respond_with(400, 200)
    with sender(webhook_receiver.url) as hook:
        hook.send(event())
    assert len(webhook_receiver.requests) == 1
    assert hook.failed == 1 and hook.delivered == 0


def test_gives_up_after_max_attempts_and_the_worker_keeps_going(webhook_receiver):
    webhook_receiver.respond_with(500, 500, 500)
    with sender(webhook_receiver.url, max_attempts=3) as hook:
        hook.send(event(1))
        hook.send(event(2))
    assert len(webhook_receiver.requests) == 4  # 3 failed attempts, then the next event
    assert hook.failed == 1 and hook.delivered == 1


def test_an_unreachable_endpoint_never_raises():
    with sender("http://127.0.0.1:1/hook", max_attempts=2, timeout=0.5) as hook:
        hook.send(event())
    assert hook.failed == 1 and hook.delivered == 0


def test_failures_are_logged_without_the_url_or_its_embedded_token(webhook_receiver, captured_logs):
    webhook_receiver.respond_with(400)
    url = webhook_receiver.url + "?token=SUPER-SECRET-TOKEN"
    with sender(url) as hook:
        hook.send(event())
    failure = next(r for r in captured_logs if r["message"] == "webhook delivery failed")
    assert failure["meta"]["error"] == "HTTP 400"
    assert "SUPER-SECRET-TOKEN" not in json.dumps(captured_logs)
    assert failure["meta"]["host"].startswith("127.0.0.1:")


def test_a_full_queue_drops_events_and_counts_them():
    release = threading.Event()
    started = threading.Event()
    hook = sender("http://127.0.0.1:1/x", queue_size=1)

    def blocked(payload):
        started.set()
        release.wait(5)

    hook._deliver = blocked  # type: ignore[method-assign]
    hook.send(event(1))
    assert started.wait(5)  # the worker holds event 1
    hook.send(event(2))  # fills the queue
    hook.send(event(3))  # dropped
    assert hook.dropped == 1
    release.set()
    hook.close()


def test_sending_after_close_is_dropped_not_raised(webhook_receiver):
    hook = sender(webhook_receiver.url)
    hook.close()
    hook.send(event())
    assert hook.dropped == 1 and webhook_receiver.requests == []
    hook.close()  # idempotent


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://e.com/x", "http://", "/relative"])
def test_urls_must_be_absolute_http(bad):
    with pytest.raises(ValueError, match="http"):
        validate_webhook_url(bad)
    with pytest.raises(ValueError):
        WebhookSender(bad)


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError):
        WebhookSender("http://127.0.0.1:1/x", max_attempts=0)
