"""Fan a run's events out to the ``on_event`` callback and a webhook."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from healix.events.schema import make_event
from healix.log import get_logger
from healix.timeutil import utc_now

logger = get_logger(__name__)

EventCallback = Callable[[dict[str, Any]], None]


class EventSender(Protocol):
    """Where an emitter's webhook events go: ``WebhookSender`` or ``DurableWebhookSender``."""

    def send(self, payload: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class EventEmitter:
    """Builds validated events for one run and delivers each to every sink.

    A failing sink never disturbs the run: an exception from ``on_event`` is logged
    and swallowed, and webhook delivery failures are handled inside the sender.
    Both sinks receive the same payload dict shape (``Event.to_dict()``).
    """

    def __init__(
        self,
        run_id: str,
        *,
        on_event: EventCallback | None = None,
        sender: EventSender | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.run_id = run_id
        self.on_event = on_event
        self.sender = sender
        self.clock = clock

    def emit(self, event: str, **data: Any) -> dict[str, Any]:
        """Validate, then deliver. Returns the payload. Raises ``EventError`` on a bad event."""
        payload = make_event(event, self.run_id, data, timestamp=self.clock()).to_dict()
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception as exc:
                logger.warn(
                    "on_event callback raised; continuing",
                    event=event,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        if self.sender is not None:
            self.sender.send(payload)
        return payload
