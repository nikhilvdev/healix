"""Deliver events to a webhook URL.

``WebhookSender`` POSTs each event's JSON payload to one URL from a background
thread, in order, so a slow or dead endpoint never stalls a crawl. Delivery
problems are logged and never raised.

Each request carries ``X-Healix-Event`` (the event type). If a ``secret`` is given,
it also carries ``X-Healix-Signature: sha256=<hex>``, the HMAC-SHA256 of the raw
request body, so the receiver can verify the sender. The secret belongs in the
environment (``HEALIX_WEBHOOK_SECRET``), never in the run config.

Retries: network errors, timeouts, HTTP 5xx, 408 and 429 are retried with
exponential backoff; other 4xx responses are not (the receiver has said no).
The URL is never logged — webhook URLs often embed tokens — only its host.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from healix import __version__
from healix.log import get_logger

logger = get_logger(__name__)

_STOP = object()
_RETRYABLE_STATUSES = frozenset({408, 429})


def sign_body(secret: str, body: bytes) -> str:
    """The ``X-Healix-Signature`` value for ``body``."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def validate_webhook_url(url: str) -> str:
    """Return ``url`` if it is an absolute http(s) URL, else raise ``ValueError``."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("webhook URL must be an absolute http(s) URL")
    return url


class WebhookSender:
    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        timeout: float = 5.0,
        max_attempts: int = 3,
        backoff: float = 0.5,
        queue_size: int = 1000,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = urlsplit(validate_webhook_url(url))
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._url = url
        self._host = parts.netloc.rsplit("@", 1)[-1]
        self._secret = secret
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._sleep = sleep
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self.delivered = 0
        self.failed = 0
        self.dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="healix-webhook", daemon=True)
        self._thread.start()

    # -- public ------------------------------------------------------------- #

    def send(self, payload: dict[str, Any]) -> None:
        """Queue ``payload`` for delivery. Never blocks and never raises."""
        if self._closed:
            self.dropped += 1
            logger.warn(
                "webhook is closed; event dropped", host=self._host, event=payload.get("event")
            )
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1
            logger.warn(
                "webhook queue is full; event dropped", host=self._host, event=payload.get("event")
            )

    def close(self, timeout: float = 10.0) -> None:
        """Deliver what is queued (up to ``timeout`` seconds), then stop the worker."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(queue.Full):
            self._queue.put(_STOP, timeout=timeout)
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warn("webhook did not finish delivering before the timeout", host=self._host)
        logger.info(
            "webhook closed",
            host=self._host,
            delivered=self.delivered,
            failed=self.failed,
            dropped=self.dropped,
        )

    def __enter__(self) -> WebhookSender:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- worker ------------------------------------------------------------- #

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._deliver(item)
            except Exception as exc:  # the worker must outlive any single bad delivery
                self.failed += 1
                logger.error("unexpected webhook error", host=self._host, error=str(exc))

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"healix/{__version__}",
            "X-Healix-Event": str(payload.get("event", "")),
        }
        if self._secret:
            headers["X-Healix-Signature"] = sign_body(self._secret, body)
        return urllib.request.Request(self._url, data=body, headers=headers, method="POST")

    def _deliver(self, payload: dict[str, Any]) -> None:
        error = "unknown"
        for attempt in range(1, self._max_attempts + 1):
            try:
                with urllib.request.urlopen(self._request(payload), timeout=self._timeout) as resp:
                    resp.read()
                self.delivered += 1
                return
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}"
                if exc.code < 500 and exc.code not in _RETRYABLE_STATUSES:
                    break  # the receiver rejected it; retrying won't change that
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
            if attempt < self._max_attempts:
                self._sleep(self._backoff * 2 ** (attempt - 1))
        self.failed += 1
        logger.warn(
            "webhook delivery failed",
            host=self._host,
            event=payload.get("event"),
            attempts=attempt,
            error=error,
        )
