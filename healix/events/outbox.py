"""Webhook delivery that survives an outage and a restart.

``DurableWebhookSender`` writes every event to a SQLite file, an *outbox*, before it tries to send
it, and only removes it once the receiver has accepted it. So an event is never lost to a receiver
that is down, a crash, or a run that ends before the receiver is back: what is still in the outbox
is sent the next time a sender is opened on the same file (a later run, or ``healix flush-events``).

Guarantees, and their limits:

* **In order.** Events are sent oldest first, and a failed event is retried before any later one is
  sent, so the receiver sees them in the order they were emitted. A receiver that is down holds
  everything back until it returns.
* **At least once.** If the receiver processes an event but the answer never arrives, the event is
  sent again. Every attempt at one event carries the same ``X-Healix-Delivery`` header, so a
  receiver that must not act twice can drop repeats by that id. Exactly-once is not possible over
  HTTP, and Healix does not claim it.
* **A receiver that says no does not block the rest.** A 4xx answer other than 408 and 429 means
  the receiver rejected that event; it is set aside as *dead* (kept in the file, never resent
  unless asked) and the next event is sent.
* **One sender per outbox.** Two processes draining the same file at once would send events twice.

The outbox holds event payloads (page URLs, output paths), so the file is created readable by its
owner only. The signing secret and the webhook URL are never stored.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from healix.events.webhook import (
    DELIVERED,
    REJECTED,
    WebhookSender,
    encode_payload,
    post_with_retries,
    validate_webhook_url,
)
from healix.log import get_logger
from healix.timeutil import iso_utc, utc_now

logger = get_logger(__name__)

PENDING = "pending"
DEAD = "dead"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS healix_outbox (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT    NOT NULL UNIQUE,
    event       TEXT    NOT NULL,
    run_id      TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT    NOT NULL
)
"""


class OutboxError(RuntimeError):
    """The outbox file could not be opened or is not an outbox."""


@dataclass(frozen=True)
class OutboxEntry:
    """One event waiting to be delivered."""

    seq: int
    delivery_id: str
    event: str
    body: bytes
    attempts: int


class Outbox:
    """The SQLite file behind ``DurableWebhookSender``. Safe to use from several threads."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.path.exists()
            # Autocommit: every statement is durable as soon as it returns.
            self._db = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None, timeout=30
            )
            self._db.execute(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            raise OutboxError(f"cannot use {self.path} as a webhook outbox: {exc}") from exc
        if fresh:
            with contextlib.suppress(OSError):  # not every filesystem has permissions
                self.path.chmod(0o600)

    def add(self, event: str, run_id: str, body: bytes, *, now: datetime | None = None) -> str:
        """Store an event for delivery; return its delivery id."""
        delivery_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO healix_outbox (delivery_id, event, run_id, body, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (delivery_id, event, run_id, body.decode(), iso_utc(now or utc_now())),
            )
        return delivery_id

    def next_pending(self) -> OutboxEntry | None:
        """The oldest event still to be delivered."""
        with self._lock:
            row = self._db.execute(
                "SELECT seq, delivery_id, event, body, attempts FROM healix_outbox"
                " WHERE status = ? ORDER BY seq LIMIT 1",
                (PENDING,),
            ).fetchone()
        return None if row is None else OutboxEntry(row[0], row[1], row[2], row[3].encode(), row[4])

    def delivered(self, seq: int) -> None:
        """The receiver accepted it: it is no longer needed."""
        with self._lock:
            self._db.execute("DELETE FROM healix_outbox WHERE seq = ?", (seq,))

    def failed_attempt(self, seq: int, attempts: int, error: str | None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE healix_outbox SET attempts = attempts + ?, last_error = ? WHERE seq = ?",
                (attempts, error, seq),
            )

    def dead(self, seq: int, attempts: int, error: str | None) -> None:
        """The receiver rejected it: keep it for inspection, and stop sending it."""
        with self._lock:
            self._db.execute(
                "UPDATE healix_outbox SET status = ?, attempts = attempts + ?, last_error = ?"
                " WHERE seq = ?",
                (DEAD, attempts, error, seq),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) FROM healix_outbox GROUP BY status"
            ).fetchall()
        found = dict(rows)
        return {PENDING: found.get(PENDING, 0), DEAD: found.get(DEAD, 0)}

    def retry_dead(self) -> int:
        """Put rejected events back in line to be sent again; how many there were."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE healix_outbox SET status = ? WHERE status = ?", (PENDING, DEAD)
            )
            return max(cursor.rowcount, 0)

    def close(self) -> None:
        with self._lock:
            self._db.close()


class DurableWebhookSender:
    """The same ``send`` / ``close`` surface as ``WebhookSender``, backed by an ``Outbox``.

    ``send`` stores the event and returns; a background thread delivers in order. When the
    receiver cannot be reached the thread waits ``retry_interval`` seconds (doubling up to
    ``max_retry_interval``) and tries the same event again, so a receiver that comes back mid-run
    is caught up without the crawl noticing. ``close`` makes one last attempt to deliver what is
    left, waits up to ``timeout`` seconds, and leaves the rest in the outbox.
    """

    def __init__(
        self,
        url: str,
        outbox: Outbox | str | os.PathLike[str],
        *,
        secret: str | None = None,
        timeout: float = 5.0,
        max_attempts: int = 3,
        backoff: float = 0.5,
        retry_interval: float = 2.0,
        max_retry_interval: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = urlsplit(validate_webhook_url(url))
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if retry_interval <= 0 or max_retry_interval < retry_interval:
            raise ValueError("retry_interval must be positive and at most max_retry_interval")
        self._url = url
        self._host = parts.netloc.rsplit("@", 1)[-1]
        self._secret = secret
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._retry_interval = retry_interval
        self._max_retry_interval = max_retry_interval
        self._sleep = sleep
        self._owns_outbox = not isinstance(outbox, Outbox)
        self._outbox = outbox if isinstance(outbox, Outbox) else Outbox(outbox)
        self.delivered = 0
        self.failed = 0  # rejected by the receiver, and set aside
        self.dropped = 0  # could not even be stored
        self._left_behind: int | None = None  # what was still pending when the outbox was closed
        self._closing = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="healix-webhook", daemon=True)
        leftover = self._outbox.counts()
        if leftover[PENDING]:
            logger.info(
                "webhook outbox has undelivered events; sending them first",
                host=self._host,
                pending=leftover[PENDING],
            )
        self._thread.start()

    # -- public ------------------------------------------------------------- #

    @property
    def pending(self) -> int:
        """Events stored and not yet accepted by the receiver."""
        if self._left_behind is not None:
            return self._left_behind
        return self._outbox.counts()[PENDING]

    def send(self, payload: dict[str, Any]) -> None:
        """Store ``payload`` for delivery. Never raises: an event that cannot be stored is counted
        in ``dropped`` and logged, like the best-effort sender."""
        if self._closing.is_set():
            self.dropped += 1
            logger.warn(
                "webhook is closed; event dropped", host=self._host, event=payload.get("event")
            )
            return
        try:
            self._outbox.add(
                str(payload.get("event", "")),
                str(payload.get("run_id", "")),
                encode_payload(payload),
            )
        except Exception as exc:  # e.g. a full disk; the run must go on
            self.dropped += 1
            logger.warn(
                "could not store event in the outbox; event dropped",
                host=self._host,
                event=payload.get("event"),
                error=str(exc),
            )
            return
        self._wake.set()

    def wait_until_delivered(self, timeout: float) -> bool:
        """Block until nothing is left to send, or ``timeout`` seconds; whether it emptied."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending == 0:
                return True
            time.sleep(0.05)
        return self.pending == 0

    def close(self, timeout: float = 10.0) -> None:
        """Try once more to deliver what is queued (up to ``timeout`` seconds), stop the worker,
        and leave whatever is left in the outbox for the next sender."""
        if self._closing.is_set():
            return
        self._closing.set()
        self._wake.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warn("webhook did not finish delivering before the timeout", host=self._host)
        counts = self._outbox.counts()
        logger.info(
            "webhook closed",
            host=self._host,
            delivered=self.delivered,
            rejected=self.failed,
            dropped=self.dropped,
            pending=counts[PENDING],
        )
        if counts[PENDING]:
            logger.warn(
                "webhook events are still in the outbox; they are sent next time it is opened",
                host=self._host,
                pending=counts[PENDING],
                outbox=str(self._outbox.path),
            )
        if self._owns_outbox and not self._thread.is_alive():
            self._left_behind = counts[PENDING]
            self._outbox.close()

    def __enter__(self) -> DurableWebhookSender:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- worker ------------------------------------------------------------- #

    def _run(self) -> None:
        wait = self._retry_interval
        while True:
            try:
                entry = self._outbox.next_pending()
                if entry is None:
                    if self._closing.is_set():
                        return
                    self._wake.wait()
                    self._wake.clear()
                    continue
                if self._deliver(entry):
                    wait = self._retry_interval
                    continue
            except Exception as exc:  # the worker must outlive any single failure
                logger.error("unexpected webhook error", host=self._host, error=str(exc))
            if self._closing.is_set():
                return  # one last attempt has been made; the rest waits in the outbox
            self._closing.wait(wait)  # a receiver that is down: try the same event again shortly
            wait = min(wait * 2, self._max_retry_interval)

    def _deliver(self, entry: OutboxEntry) -> bool:
        """Try one event. ``True`` if the queue can move on to the next one."""
        outcome = post_with_retries(
            self._url,
            entry.body,
            entry.event,
            delivery_id=entry.delivery_id,
            secret=self._secret,
            timeout=self._timeout,
            max_attempts=self._max_attempts,
            backoff=self._backoff,
            sleep=self._sleep,
        )
        if outcome.status == DELIVERED:
            self._outbox.delivered(entry.seq)
            self.delivered += 1
            return True
        if outcome.status == REJECTED:
            self._outbox.dead(entry.seq, outcome.attempts, outcome.error)
            self.failed += 1
            logger.warn(
                "webhook receiver rejected an event; it is set aside and the next one is sent",
                host=self._host,
                event=entry.event,
                error=outcome.error,
            )
            return True
        self._outbox.failed_attempt(entry.seq, outcome.attempts, outcome.error)
        logger.warn(
            "webhook receiver is not accepting events; will try again",
            host=self._host,
            event=entry.event,
            attempts=outcome.attempts,
            error=outcome.error,
        )
        return False


def open_sender(
    url: str, *, secret: str | None = None, outbox: str | os.PathLike[str] | None = None
) -> WebhookSender | DurableWebhookSender:
    """The sender for ``url``: durable when an ``outbox`` file is given, best-effort otherwise."""
    if outbox is None:
        return WebhookSender(url, secret=secret)
    return DurableWebhookSender(url, outbox, secret=secret)


def validate_outbox(webhook_url: str | None, outbox: str | os.PathLike[str] | None) -> Path | None:
    """``outbox`` as a path, or ``None``; a ``ValueError`` if it is given without a webhook URL."""
    if outbox is None:
        return None
    if not webhook_url:
        raise ValueError("webhook_outbox needs a webhook_url: there is nowhere to deliver to")
    return Path(outbox)
