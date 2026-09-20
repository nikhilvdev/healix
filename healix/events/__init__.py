"""Events: the shared schema, the emitter, and webhook delivery (best-effort or durable)."""

from healix.events.emitter import EventCallback, EventEmitter, EventSender
from healix.events.outbox import (
    DurableWebhookSender,
    Outbox,
    OutboxError,
    open_sender,
    validate_outbox,
)
from healix.events.schema import (
    ELEMENT_HEALED,
    EVENT_DATA_FIELDS,
    EVENT_TYPES,
    LOGIN_FAILED,
    LOGIN_FAILURE_REASONS,
    PAGE_DISCOVERED,
    PAGE_EXTRACTED,
    RUN_COMPLETE,
    SCRIPT_GENERATED,
    Event,
    EventError,
    make_event,
)
from healix.events.webhook import WebhookSender, sign_body, validate_webhook_url

__all__ = [
    "ELEMENT_HEALED",
    "EVENT_DATA_FIELDS",
    "EVENT_TYPES",
    "LOGIN_FAILED",
    "LOGIN_FAILURE_REASONS",
    "PAGE_DISCOVERED",
    "PAGE_EXTRACTED",
    "RUN_COMPLETE",
    "SCRIPT_GENERATED",
    "DurableWebhookSender",
    "Event",
    "EventCallback",
    "EventEmitter",
    "EventError",
    "EventSender",
    "Outbox",
    "OutboxError",
    "WebhookSender",
    "make_event",
    "open_sender",
    "sign_body",
    "validate_outbox",
    "validate_webhook_url",
]
