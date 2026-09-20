"""``Healer``: the public entry point to self-healing.

::

    from healix import Healer

    with Healer("healix.db") as healer:
        healer.learn("https://example.com/login")                 # record the baseline
        ...                                                        # the page changes
        healer.click("https://example.com/login", "button:sign-in")  # finds it anyway

A ``Healer`` owns a browser (unless you pass a ``driver``), a fingerprint store, and — if you ask —
an ``on_event``/webhook sink that receives an ``element_healed`` event for every heal. It is a
context manager: leaving the block closes what it opened.

Fingerprints are usually recorded during extraction (``Crawler(..., fingerprint_store=...)``,
``healix crawl --fingerprint-db``); ``learn`` records a single page directly. Roles are stable
names such as ``textbox:username`` or ``button:sign-in`` (see ``healix.healing.roles``).

Nothing here guesses. An element that cannot be found with confidence raises
``ElementNotHealedError``; a canvas-rendered page raises ``UnsupportedRenderingError``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Sequence
from typing import Any

from healix.discovery.manifest import normalize_url
from healix.driver.base import Driver, Element
from healix.driver.factory import create_driver
from healix.events import (
    ELEMENT_HEALED,
    EventCallback,
    EventEmitter,
    open_sender,
    validate_outbox,
    validate_webhook_url,
)
from healix.healing import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_DB_PATH,
    DEFAULT_THRESHOLD,
    KEEP,
    FingerprintStore,
    HealRecord,
    HealResult,
    RecordSummary,
    Resolver,
    churn_report,
    open_store,
    record_page,
)
from healix.log import get_logger

logger = get_logger(__name__)

WEBHOOK_SECRET_ENV = "HEALIX_WEBHOOK_SECRET"


class Healer:
    """Record element fingerprints, and find elements again after the page changes.

    ``store`` is a ``FingerprintStore``, a path to a SQLite file or a ``postgresql://`` URL (either
    opened and closed by the ``Healer``), or ``None`` for ``./healix.db``. ``threshold`` (default
    0.5) is the minimum confidence for accepting a healed match. ``ambiguity_margin`` (default
    0.05) rejects a best candidate that is a near-tie with another. A ``driver`` you pass is used
    as-is and stays yours to close.
    """

    def __init__(
        self,
        store: FingerprintStore | str | os.PathLike[str] | None = None,
        *,
        driver: Driver | None = None,
        backend: str = "playwright",
        headless: bool = True,
        threshold: float = DEFAULT_THRESHOLD,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
        run_id: str | None = None,
        on_event: EventCallback | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        webhook_outbox: str | os.PathLike[str] | None = None,
    ) -> None:
        self._store_arg = store if store is not None else DEFAULT_DB_PATH
        self._driver_arg = driver
        self.backend = backend
        self.headless = headless
        self.threshold = threshold
        self.ambiguity_margin = ambiguity_margin
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.on_event = on_event
        self.webhook_url = validate_webhook_url(webhook_url) if webhook_url else None
        self.webhook_secret = webhook_secret or os.environ.get(WEBHOOK_SECRET_ENV) or None
        self.webhook_outbox = validate_outbox(self.webhook_url, webhook_outbox)

        self._stack: contextlib.ExitStack | None = None
        self._driver: Driver | None = None
        self._store: FingerprintStore | None = None
        self._resolver: Resolver | None = None
        self._emitter: EventEmitter | None = None

    # -- lifecycle ------------------------------------------------------------------ #

    def start(self) -> Healer:
        """Open the store and browser. Called automatically on first use."""
        if self._stack is not None:
            return self
        stack = contextlib.ExitStack()
        try:
            store = self._store_arg
            if not isinstance(store, FingerprintStore):
                store = open_store(store)
                stack.callback(store.close)
            sender = None
            if self.webhook_url:
                sender = open_sender(
                    self.webhook_url, secret=self.webhook_secret, outbox=self.webhook_outbox
                )
                stack.callback(sender.close)
            driver = self._driver_arg
            if driver is None:
                driver = create_driver(self.backend, headless=self.headless)
                driver.start()
                stack.callback(driver.close)
            self._emitter = EventEmitter(self.run_id, on_event=self.on_event, sender=sender)
            self._resolver = Resolver(
                store,
                threshold=self.threshold,
                ambiguity_margin=self.ambiguity_margin,
                run_id=self.run_id,
                on_heal=self._emit_healed,
            )
        except BaseException:
            stack.close()
            raise
        self._stack, self._store, self._driver = stack, store, driver
        return self

    def close(self) -> None:
        """Release everything this ``Healer`` opened (a caller-supplied driver or store is left)."""
        if self._stack is not None:
            self._stack.close()
        self._stack = self._store = self._driver = self._resolver = self._emitter = None

    def __enter__(self) -> Healer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def driver(self) -> Driver:
        """The driver in use (started on first access) — for inspecting or acting on the page."""
        self.start()
        assert self._driver is not None
        return self._driver

    @property
    def store(self) -> FingerprintStore:
        self.start()
        assert self._store is not None
        return self._store

    # -- recording ------------------------------------------------------------------ #

    def learn(
        self,
        page_url: str,
        elements: Sequence[Element] | None = None,
        *,
        mode: str = KEEP,
    ) -> RecordSummary:
        """Record fingerprints for the healable elements of ``page_url``.

        With no ``elements`` the page is loaded and read. ``mode="keep"`` (default) adds only new
        roles and leaves an existing baseline alone; ``"refresh"`` overwrites it.
        """
        self.start()
        assert self._driver is not None
        if elements is None:
            self._driver.navigate(page_url)
            elements = self._driver.get_elements()
        return record_page(self.store, normalize_url(page_url), elements, mode=mode)

    # -- resolving ------------------------------------------------------------------- #

    def resolve(self, page_url: str, element_role: str, *, navigate: bool = True) -> HealResult:
        """Find the element, healing its fingerprint if the page has changed.

        ``navigate=False`` resolves against the page the browser is already on.
        """
        self.start()
        assert self._driver is not None and self._resolver is not None
        if navigate:
            self._driver.navigate(page_url)
        return self._resolver.resolve(self._driver, normalize_url(page_url), element_role)

    def click(self, page_url: str, element_role: str, *, navigate: bool = True) -> HealResult:
        """``resolve`` the element, then click it."""
        result = self.resolve(page_url, element_role, navigate=navigate)
        assert self._driver is not None
        self._driver.click(result.element)
        return result

    def write(
        self, text: str, page_url: str, element_role: str, *, navigate: bool = True
    ) -> HealResult:
        """``resolve`` the element, then type ``text`` into it."""
        result = self.resolve(page_url, element_role, navigate=navigate)
        assert self._driver is not None
        self._driver.write(text, into=result.element)
        return result

    # -- the audit trail --------------------------------------------------------------- #

    def history(
        self,
        page_url: str | None = None,
        element_role: str | None = None,
        *,
        change_kind: str | None = None,
        limit: int | None = None,
    ) -> list[HealRecord]:
        """Heal records, newest first; ``change_kind`` filters to ``"churn"`` / ``"regression"``."""
        return self.store.history(
            normalize_url(page_url) if page_url else None,
            element_role,
            change_kind=change_kind,
            limit=limit,
        )

    def report(self) -> list[dict[str, Any]]:
        """Per element: how often it healed, and how many of those looked like regressions."""
        return churn_report(self.store.history())

    # -- events ------------------------------------------------------------------------- #

    def _emit_healed(self, record: HealRecord) -> None:
        assert self._emitter is not None
        self._emitter.emit(
            ELEMENT_HEALED,
            element_key=record.element_key,
            old_locator=record.old_locator,
            new_locator=record.new_locator,
            strategy_used=record.strategy,
            confidence_score=record.confidence,
            page_url=record.page_url,
        )
