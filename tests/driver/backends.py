"""The browser backends under test, and the few things a test needs that differ between them."""

from __future__ import annotations

import os
from typing import Any

import pytest

BACKENDS = ["playwright", "selenium"]

# Set to run the Selenium tests on another browser (CI does this for Firefox). Asking for a
# browser by name means it has to be there: a launch failure then fails the test instead of
# quietly skipping it, so a broken job cannot look green.
SELENIUM_BROWSER_ENV = "HEALIX_TEST_SELENIUM_BROWSER"


def make_driver(backend: str, **kwargs: Any):
    """A started driver for ``backend``; the test is skipped when it cannot be launched."""
    pytest.importorskip(backend)
    requested = os.environ.get(SELENIUM_BROWSER_ENV) if backend == "selenium" else None
    if requested:
        kwargs.setdefault("browser", requested)
    if backend == "playwright":
        from healix.driver.playwright_adapter import PlaywrightDriverAdapter

        driver = PlaywrightDriverAdapter(**kwargs)
    else:
        from healix.driver.selenium_adapter import SeleniumDriverAdapter

        driver = SeleniumDriverAdapter(**kwargs)
    try:
        driver.start()
    except Exception as exc:  # browser (or its driver) not installed
        if requested:
            raise
        pytest.skip(f"cannot launch {backend}: {str(exc).splitlines()[0]}")
    return driver


def evaluate(driver, frame_path: list[str], expression: str) -> Any:
    """Evaluate a JavaScript expression inside the frame at ``frame_path``."""
    frame = next(f for f in driver.get_frames() if f.path == frame_path)
    if hasattr(driver, "page"):  # Playwright
        return frame.handle.evaluate(expression)
    with driver._frame(frame.handle):  # Selenium
        return driver.webdriver.execute_script(f"return ({expression});")


def force_backend(monkeypatch, backend: str) -> None:
    """Make ``create_driver`` build ``backend`` wherever the SDK and ``Healer`` ask for a driver."""
    from healix.driver.factory import create_driver

    def create(_requested: str, **kwargs: Any):
        return create_driver(backend, **kwargs)

    monkeypatch.setattr("healix.sdk.create_driver", create)
    monkeypatch.setattr("healix.healer.create_driver", create)
