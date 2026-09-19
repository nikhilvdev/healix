"""Playwright implementation of ``Driver`` (sync API).

This is one of the only modules allowed to import ``playwright``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from playwright.sync_api import Frame as PlaywrightFrame
from playwright.sync_api import Locator, Page, Playwright, sync_playwright

from healix.driver.base import Driver, Element, ElementNotFoundError, Frame, Target
from healix.driver.frames import COLLECT_ALL_JS, DESCRIBE_ONE_JS, collect_elements, walk_frames
from healix.healing.fingerprint import Fingerprint, LocatorSpec
from healix.ids import normalize_id

logger = logging.getLogger(__name__)


class PlaywrightDriverAdapter(Driver):
    """Drive a Playwright ``Page``.

    Pass an existing ``page`` to embed in a caller-managed session (the caller
    keeps ownership and ``close()`` is a no-op), or omit it and the adapter
    launches and owns its own browser on ``start()`` / context-manager entry.
    """

    def __init__(
        self,
        page: Page | None = None,
        *,
        browser: str = "chromium",
        headless: bool = True,
    ) -> None:
        self._page = page
        self._browser_name = browser
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Any = None
        self._owns_browser = False

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = getattr(self._playwright, self._browser_name).launch(headless=self._headless)
        self._page = self._browser.new_context().new_page()
        self._owns_browser = True

    def close(self) -> None:
        if not self._owns_browser:
            return
        self._browser.close()
        self._playwright.stop()
        self._page = self._browser = self._playwright = None
        self._owns_browser = False

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("driver not started: call start() or use it as a context manager")
        return self._page

    # -- Driver ------------------------------------------------------------- #

    def navigate(self, url: str) -> None:
        self.page.goto(url, wait_until="load")

    def get_frames(self) -> list[Frame]:
        return walk_frames(
            self.page.main_frame,
            # child_frames can still list frames from a previous document after re-navigation
            children_of=lambda f: [c for c in f.child_frames if not c.is_detached()],
            url_of=lambda f: f.url,
            name_of=_frame_label,
        )

    def get_elements(self) -> list[Element]:
        return collect_elements(self.get_frames(), lambda frame: frame.handle.evaluate(COLLECT_ALL_JS))

    def find(self, fingerprint: Fingerprint) -> Element:
        frames = self._search_frames(fingerprint)
        for spec in fingerprint.locators():
            for frame in frames:
                locator = self._resolve(spec, frame.handle, fingerprint)
                if locator is not None:
                    return Element.from_dict(locator.evaluate(DESCRIBE_ONE_JS), iframe_path=frame.path)
        raise ElementNotFoundError(
            f"no unique element for {fingerprint.element_role!r} on {fingerprint.page_url} "
            f"(tried {[s.strategy for s in fingerprint.locators()]})"
        )

    def click(self, target: Target) -> None:
        self._locator_for(target).click()

    def write(self, text: str, into: Target) -> None:
        self._locator_for(into).fill(text)

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    # -- internals ---------------------------------------------------------- #

    def _search_frames(self, fingerprint: Fingerprint) -> list[Frame]:
        frames = [f for f in self.get_frames() if f.same_origin]
        if fingerprint.iframe_path:
            frames = [f for f in frames if f.path == fingerprint.iframe_path]
        return frames

    def _resolve(self, spec: LocatorSpec, frame: PlaywrightFrame, fingerprint: Fingerprint) -> Locator | None:
        """Turn ``spec`` into a locator matching exactly one element in ``frame``, else ``None``.

        A locator matching several elements is ambiguous, so it is treated as a
        miss and the next strategy gets a turn.
        """
        if spec.kind == "css":
            locator = frame.locator(spec.value)
        elif spec.kind == "xpath":
            locator = frame.locator(f"xpath={spec.value}")
        elif spec.kind == "text":
            locator = frame.locator(f"{fingerprint.tag or '*'}:text-is({json.dumps(spec.value)})")
        elif spec.kind == "id_pattern":
            with_ids = frame.locator("[id]")
            ids = with_ids.evaluate_all("els => els.map(e => e.id)")
            matches = [i for i, candidate in enumerate(ids) if normalize_id(candidate) == spec.value]
            locator = with_ids.nth(matches[0]) if len(matches) == 1 else None
        else:
            raise ValueError(f"unknown locator kind {spec.kind!r}")
        if locator is None:
            return None
        try:
            return locator if locator.count() == 1 else None
        except Exception as exc:  # e.g. selector syntax the browser rejects
            logger.debug("locator %s failed: %s", spec, exc)
            return None

    def _locator_for(self, target: Target) -> Locator:
        element = self.find(target) if isinstance(target, Fingerprint) else target
        if not element.css_selector:
            raise ElementNotFoundError("element has no css_selector to locate it by")
        for frame in self.get_frames():
            if frame.path == element.iframe_path:
                return frame.handle.locator(element.css_selector)
        raise ElementNotFoundError(f"frame {element.iframe_path} not found")


def _frame_label(frame: PlaywrightFrame) -> str | None:
    """Frame's ``name``, else the ``id`` of its <iframe> element; ``None`` if neither."""
    if frame.name:
        return frame.name
    try:
        return frame.frame_element().get_attribute("id")
    except Exception:  # detached frame
        return None
