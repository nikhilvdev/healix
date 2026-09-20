"""Selenium implementation of ``Driver``.

This is one of the only modules allowed to import ``selenium``.

Selenium's own selectors stop at a shadow root and its frames are entered by switching the whole
session into them, so this adapter leans on the same in-page scripts as every other backend
(``healix.driver.frames``): the collector and the shadow-piercing queries run inside each frame via
``execute_script``. That is one browser round trip per frame rather than one per shadow host, and
it sees exactly what the Playwright adapter sees — open shadow roots, same-origin frames — so the
same crawl, extraction and healing produce the same results on either backend.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver import Chrome, ChromeOptions, Edge, EdgeOptions, Firefox, FirefoxOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from healix.driver.base import Driver, Element, ElementNotFoundError, Frame, SkippedFrame, Target
from healix.driver.diagnose import BackendReport
from healix.driver.frames import (
    CHILD_FRAMES_JS,
    ID_AT_JS,
    IDS_JS,
    QUERY_JS,
    TEXT_JS,
    build_collect_js,
    build_describe_js,
    collect_elements,
    walk_frames,
)
from healix.driver.guard import BLOCKED_JS, GUARD_JS
from healix.driver.settle import ACTIVITY_EXPRESSION, POLL_SECONDS, wait_until_quiet
from healix.healing.fingerprint import Fingerprint, LocatorSpec
from healix.ids import normalize_id
from healix.log import get_logger
from healix.platform_adapters import ADAPTERS, PlatformAdapter

logger = get_logger(__name__)

BROWSERS = ("chrome", "firefox", "edge")

_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "{PROGRAMFILES}/Google/Chrome/Application/chrome.exe",
    "{PROGRAMFILES(X86)}/Google/Chrome/Application/chrome.exe",
    "{LOCALAPPDATA}/Google/Chrome/Application/chrome.exe",
)


def _find_chrome() -> str | None:
    import os
    import shutil
    from pathlib import Path

    for name in _CHROME_NAMES:
        if found := shutil.which(name):
            return found
    for template in _CHROME_PATHS:
        path = template
        for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            path = path.replace("{" + var + "}", os.environ.get(var, "\0"))
        if "\0" not in path and Path(path).exists():
            return path
    return None


def diagnose() -> BackendReport:
    """Whether Selenium and a Chrome/Chromium to drive are installed (``healix doctor``).

    Launches nothing. The matching chromedriver is fetched by Selenium Manager the first time a
    browser is launched, which needs network access; ``healix doctor --launch`` checks that.
    """
    from importlib.metadata import version

    chrome = _find_chrome()
    if chrome is None:
        return BackendReport(
            "selenium",
            True,
            version("selenium"),
            problem="Chrome or Chromium was not found",
            hint="install Google Chrome or Chromium",
        )
    return BackendReport("selenium", True, version("selenium"), browser=chrome)


DEFAULT_QUIET_MS = 500
_ACTIVITY_JS = f"return {ACTIVITY_EXPRESSION};"
# Chromedriver stamps this onto every <iframe> it switches into. It is not part of the page.
_DRIVER_ATTRIBUTES = ("cd_frame_id_",)
# The documents browsers show instead of a page they could not load.
_ERROR_PAGES = ("chrome-error://", "edge-error://", "about:neterror", "about:certerror")
_ERROR_PAGE_JS = (
    "return [document.URL, (document.querySelector('.error-code') || {}).textContent || null];"
)
_RETRYABLE = (
    ElementNotInteractableException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)


class _AmbiguousElementError(ElementNotFoundError):
    """A selector matched several elements: retrying will not make it unique."""


@dataclass(frozen=True)
class _FrameRef:
    """How to get into a frame: the chain of ``<iframe>`` elements from the top document."""

    chain: tuple[WebElement, ...]
    label: str | None = None


class SeleniumDriverAdapter(Driver):
    """Drive a Selenium ``WebDriver``.

    Pass an existing ``webdriver`` to embed in a caller-managed session (the caller keeps ownership
    and ``close()`` is a no-op), or omit it and the adapter launches and owns its own browser on
    ``start()`` / context-manager entry. ``browser`` is ``chrome`` (default), ``firefox`` or
    ``edge``; only Chrome is exercised by Healix's own tests. Selenium finds a matching browser
    driver itself (Selenium Manager), so nothing else needs installing.

    ``platform_adapters`` are the optional platform hooks (``healix.platform_adapters``); the
    default is all of them, ``()`` turns them off.

    Unlike Playwright, Selenium neither waits for elements to become actionable nor for the network
    to go idle. ``click`` and ``write`` therefore retry for up to ``action_timeout_ms`` while the
    element is missing, covered or not yet interactable, and ``settle`` waits (bounded by
    ``settle_timeout_ms``) for the page to finish loading and then stay quiet.

    "Quiet" is a heuristic: no new resources and no new elements for ``quiet_ms`` (default 500,
    like Playwright's network-idle window). WebDriver cannot see a request that is still in
    flight, so an API call that takes longer than ``quiet_ms`` to answer can be missed; raise
    ``quiet_ms`` for slow back ends.
    """

    def __init__(
        self,
        webdriver: WebDriver | None = None,
        *,
        browser: str = "chrome",
        headless: bool = True,
        settle_timeout_ms: int = 3000,
        quiet_ms: int = DEFAULT_QUIET_MS,
        action_timeout_ms: int = 5000,
        page_load_timeout_ms: int = 30000,
        window_size: tuple[int, int] = (1280, 720),
        platform_adapters: Sequence[PlatformAdapter] | None = None,
    ) -> None:
        if browser not in BROWSERS:
            raise ValueError(f"unknown browser {browser!r}; expected one of {BROWSERS}")
        adapters = ADAPTERS if platform_adapters is None else tuple(platform_adapters)
        self._collect_js = build_collect_js(adapters)
        self._describe_js = build_describe_js(adapters)
        self._webdriver = webdriver
        self._browser = browser
        self._headless = headless
        self._settle_timeout = settle_timeout_ms / 1000
        self._quiet = quiet_ms / 1000
        self._action_timeout = action_timeout_ms / 1000
        self._page_load_timeout = page_load_timeout_ms / 1000
        self._window_size = window_size
        self._owns_driver = False
        self._skipped: list[SkippedFrame] = []
        self._guarding = False

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._webdriver is not None:
            return
        self._webdriver = self._launch()
        self._owns_driver = True

    def _launch(self) -> WebDriver:
        driver: WebDriver
        if self._browser == "firefox":
            firefox = FirefoxOptions()
            if self._headless:
                firefox.add_argument("-headless")
            firefox.unhandled_prompt_behavior = "dismiss"
            driver = Firefox(options=firefox)
        elif self._browser == "edge":
            edge = EdgeOptions()
            if self._headless:
                edge.add_argument("--headless=new")
            edge.unhandled_prompt_behavior = "dismiss"
            driver = Edge(options=edge)
        else:
            chrome = ChromeOptions()
            if self._headless:
                chrome.add_argument("--headless=new")
            chrome.unhandled_prompt_behavior = "dismiss"  # like Playwright, dismiss alert()
            driver = Chrome(options=chrome)
        try:
            driver.set_window_size(*self._window_size)
            driver.set_page_load_timeout(self._page_load_timeout)
        except BaseException:
            driver.quit()
            raise
        return driver

    def close(self) -> None:
        if not self._owns_driver:
            return
        driver, self._webdriver, self._owns_driver = self._webdriver, None, False
        if driver is not None:
            driver.quit()

    @property
    def webdriver(self) -> WebDriver:
        if self._webdriver is None:
            raise RuntimeError("driver not started: call start() or use it as a context manager")
        return self._webdriver

    # -- Driver ------------------------------------------------------------- #

    @property
    def current_url(self) -> str:
        return self.webdriver.current_url

    def navigate(self, url: str) -> None:
        self.webdriver.get(url)
        self._raise_if_error_page(url)
        self.settle()
        if self._guarding:
            self._apply_guard()

    def _raise_if_error_page(self, url: str) -> None:
        """Playwright raises when a page cannot be loaded; some browsers show an error page and
        report success. Raise here too, so a failed load is never read as an ordinary page."""
        document_url, code = self.webdriver.execute_script(_ERROR_PAGE_JS)
        if str(document_url).startswith(_ERROR_PAGES):
            reason = f": {code}" if code else ""
            raise WebDriverException(f"could not load {url}{reason}")

    def settle(self) -> None:
        # A navigation in flight must finish before the page can be read.
        state = self._activity()
        deadline = time.monotonic() + max(self._settle_timeout, 1.0)
        while not state.startswith("complete|") and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            state = self._activity()
        if not state.startswith("complete|"):
            logger.debug("page did not finish loading while settling")
        # Client-rendered pages keep fetching after `load`. There is no "network idle" in
        # WebDriver, so watch the page's resources and elements instead, and give up after the
        # timeout: busy pages (polling, websockets) never go quiet.
        if self._settle_timeout <= 0:
            return
        if not wait_until_quiet(
            self._activity,
            quiet_s=self._quiet,
            deadline=time.monotonic() + self._settle_timeout,
            initial=state,
        ):
            logger.debug("page did not go quiet", timeout_ms=int(self._settle_timeout * 1000))

    def _activity(self) -> str:
        try:
            self.webdriver.switch_to.default_content()
            return str(self.webdriver.execute_script(_ACTIVITY_JS))
        except WebDriverException:  # mid-navigation
            return ""

    def get_frames(self) -> list[Frame]:
        return walk_frames(
            _FrameRef(()),
            children_of=self._child_frames,
            url_of=self._frame_url,
            name_of=lambda ref: ref.label,
        )

    def get_elements(self, *, iframe_traversal: bool = True) -> list[Element]:
        frames = self.get_frames()
        if not iframe_traversal:
            frames = frames[:1]  # the main frame is always first
        self._skipped = []
        return collect_elements(
            frames,
            lambda frame: [_scrub(raw) for raw in self._in_frame(frame, self._collect_js)],
            skipped=self._skipped,
        )

    def skipped_frames(self) -> list[SkippedFrame]:
        return list(self._skipped)

    def find(self, fingerprint: Fingerprint) -> Element:
        frames = self._search_frames(fingerprint)
        for spec in fingerprint.locators():
            found = self._locate_in(spec, fingerprint, frames)
            if found is not None:
                return found
        raise ElementNotFoundError(
            f"no unique element for {fingerprint.element_role!r} on {fingerprint.page_url} "
            f"(tried {[s.strategy for s in fingerprint.locators()]})"
        )

    def locate(self, spec: LocatorSpec, fingerprint: Fingerprint) -> Element | None:
        return self._locate_in(spec, fingerprint, self._search_frames(fingerprint))

    def click(self, target: Target) -> None:
        self._act(target, lambda element: element.click())

    def write(self, text: str, into: Target) -> None:
        def fill(element: WebElement) -> None:
            element.clear()
            element.send_keys(text)

        self._act(into, fill)

    def screenshot(self) -> bytes:
        return self.webdriver.get_screenshot_as_png()

    @contextmanager
    def guarded(self) -> Iterator[None]:
        self._guarding = True
        try:
            if self._webdriver is not None:
                self._apply_guard()
            yield
        finally:
            self._guarding = False

    def blocked_writes(self) -> int:
        total = 0
        for frame in self.get_frames():
            if not frame.same_origin:
                continue
            try:
                total += int(self._in_frame(frame, BLOCKED_JS))
            except WebDriverException:  # the frame navigated away while it was being read
                logger.debug("could not read the write guard", frame=frame.path)
        return total

    def _apply_guard(self) -> None:
        """Install the write guard in every same-origin frame of the current document."""
        for frame in self.get_frames():
            if not frame.same_origin:
                continue
            try:
                self._in_frame(frame, GUARD_JS)
            except WebDriverException:  # the frame navigated away while it was being read
                logger.debug("could not install the write guard", frame=frame.path)

    # -- frames ------------------------------------------------------------- #

    @contextmanager
    def _frame(self, ref: _FrameRef) -> Iterator[None]:
        """Run the body inside a frame, and leave the session back in the top document."""
        driver = self.webdriver
        try:
            driver.switch_to.default_content()
            for iframe in ref.chain:
                driver.switch_to.frame(iframe)
            yield
        finally:
            with contextlib.suppress(WebDriverException):  # the window may be gone
                driver.switch_to.default_content()

    def _run(self, function_expression: str, *args: Any) -> Any:
        return self.webdriver.execute_script(
            f"return ({function_expression}).apply(null, arguments);", *args
        )

    def _in_frame(self, frame: Frame, function_expression: str, *args: Any) -> Any:
        with self._frame(frame.handle):
            return self._run(function_expression, *args)

    def _child_frames(self, ref: _FrameRef) -> list[_FrameRef]:
        try:
            with self._frame(ref):
                iframes = self._run(CHILD_FRAMES_JS)
                # An iframe is only usable while its own document is current, so read its label now.
                labels = [_label(iframe) for iframe in iframes]
        except WebDriverException:  # the frame went away mid-walk
            return []
        return [
            _FrameRef((*ref.chain, iframe), label)
            for iframe, label in zip(iframes, labels, strict=True)
        ]

    def _frame_url(self, ref: _FrameRef) -> str:
        try:
            with self._frame(ref):
                return str(self._run("() => window.location.href"))
        except WebDriverException:
            return ""

    def _search_frames(self, fingerprint: Fingerprint) -> list[Frame]:
        frames = [f for f in self.get_frames() if f.same_origin]
        if fingerprint.iframe_path:
            frames = [f for f in frames if f.path == fingerprint.iframe_path]
        return frames

    # -- locating ----------------------------------------------------------- #

    def _locate_in(
        self, spec: LocatorSpec, fingerprint: Fingerprint, frames: list[Frame]
    ) -> Element | None:
        for frame in frames:
            with self._frame(frame.handle):
                matches = self._matches(spec, fingerprint)
                if len(matches) == 1:
                    raw = _scrub(self._run(self._describe_js, matches[0]))
                    return Element.from_dict(raw, iframe_path=frame.path)
        return None

    def _matches(self, spec: LocatorSpec, fingerprint: Fingerprint) -> list[WebElement]:
        """What ``spec`` matches in the current frame. More than one is ambiguous, so the caller
        treats it as a miss and the next strategy gets a turn."""
        try:
            if spec.kind == "css":
                found: list[WebElement] = self._run(QUERY_JS, spec.value)
            elif spec.kind == "xpath":
                found = self.webdriver.find_elements(By.XPATH, spec.value)
            elif spec.kind == "text":
                found = self._run(TEXT_JS, fingerprint.tag or "*", spec.value)
            elif spec.kind == "id_pattern":
                ids = self._run(IDS_JS)
                hits = [
                    i for i, candidate in enumerate(ids) if normalize_id(candidate) == spec.value
                ]
                found = [self._run(ID_AT_JS, hits[0])] if len(hits) == 1 else []
            else:
                raise ValueError(f"unknown locator kind {spec.kind!r}")
        except WebDriverException as exc:  # e.g. selector syntax the browser rejects
            logger.debug("locator failed", strategy=spec.strategy, value=spec.value, error=str(exc))
            return []
        return found

    # -- acting ------------------------------------------------------------- #

    def _act(self, target: Target, action: Callable[[WebElement], None]) -> None:
        """Find the element again and run ``action`` on it, retrying while it is not ready.

        Playwright waits for an element to be actionable; Selenium does not, so wait here, for up
        to ``action_timeout_ms``. An ambiguous selector is never retried: it will not get better.
        """
        element = self.find(target) if isinstance(target, Fingerprint) else target
        if not element.css_selector:
            raise ElementNotFoundError("element has no css_selector to locate it by")
        deadline = time.monotonic() + self._action_timeout
        while True:
            try:
                self._act_once(element, action)
                return
            except _AmbiguousElementError:
                raise
            except (ElementNotFoundError, *_RETRYABLE):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def _act_once(self, element: Element, action: Callable[[WebElement], None]) -> None:
        for frame in self.get_frames():
            if frame.path != element.iframe_path:
                continue
            with self._frame(frame.handle):
                found = self._run(QUERY_JS, element.css_selector)
                if len(found) > 1:
                    raise _AmbiguousElementError(
                        f"{element.css_selector!r} matches {len(found)} elements in "
                        f"frame {element.iframe_path}"
                    )
                if found:
                    action(found[0])
                    return
            raise ElementNotFoundError(
                f"{element.css_selector!r} not found in frame {element.iframe_path}"
            )
        raise ElementNotFoundError(f"frame {element.iframe_path} not found")


def _label(iframe: WebElement) -> str | None:
    """The iframe's ``name``, else its ``id``; ``None`` if it has neither."""
    return iframe.get_attribute("name") or iframe.get_attribute("id") or None


def _scrub(raw: dict[str, Any]) -> dict[str, Any]:
    """``raw`` without the attributes the browser driver itself adds to elements."""
    for name in _DRIVER_ATTRIBUTES:
        raw["attributes"].pop(name, None)
    return raw
