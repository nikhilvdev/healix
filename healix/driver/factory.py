"""Create the ``Driver`` for a configured backend."""

from __future__ import annotations

import importlib.util

from healix.driver.base import Driver
from healix.driver.diagnose import BackendReport
from healix.platform_adapters import ADAPTERS

BACKENDS = ("playwright", "selenium")


class BackendUnavailableError(RuntimeError):
    """The requested backend can't be used (its library is not installed)."""


def create_driver(
    backend: str = "playwright",
    *,
    headless: bool = True,
    platform_detection: str = "auto",
    quiet_ms: int | None = None,
) -> Driver:
    """A new, not-yet-started driver for ``backend``.

    ``platform_detection`` is ``"auto"`` (the platform adapters may add ``platform_signal``) or
    ``"off"`` (the generic pipeline alone). ``quiet_ms`` is how long a page must stay unchanged
    before it counts as loaded; ``None`` keeps each backend's default (Playwright: no such wait,
    Selenium: 500).
    """
    if platform_detection not in ("auto", "off"):
        raise ValueError(f"platform_detection must be 'auto' or 'off', got {platform_detection!r}")
    if quiet_ms is not None and quiet_ms < 0:
        raise ValueError(f"quiet_ms must be 0 or more, got {quiet_ms!r}")
    adapters = ADAPTERS if platform_detection == "auto" else ()
    if backend == "playwright":
        try:
            from healix.driver.playwright_adapter import (
                DEFAULT_QUIET_MS,
                PlaywrightDriverAdapter,
            )
        except ImportError as exc:
            raise BackendUnavailableError(
                "the playwright backend needs Playwright: "
                "pip install 'healix[playwright]' && playwright install chromium"
            ) from exc
        return PlaywrightDriverAdapter(
            headless=headless,
            platform_adapters=adapters,
            quiet_ms=DEFAULT_QUIET_MS if quiet_ms is None else quiet_ms,
        )
    if backend == "selenium":
        try:
            from healix.driver.selenium_adapter import (
                DEFAULT_QUIET_MS,
                SeleniumDriverAdapter,
            )
        except ImportError as exc:
            raise BackendUnavailableError(
                "the selenium backend needs Selenium: pip install 'healix[selenium]' "
                "(it also needs Chrome installed)"
            ) from exc
        return SeleniumDriverAdapter(
            headless=headless,
            platform_adapters=adapters,
            quiet_ms=DEFAULT_QUIET_MS if quiet_ms is None else quiet_ms,
        )
    raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")


_PACKAGES = {"playwright": "playwright", "selenium": "selenium"}
_INSTALL = {
    "playwright": "pip install 'healix[playwright]' && playwright install chromium",
    "selenium": "pip install 'healix[selenium]'",
}


def diagnose_backend(backend: str) -> BackendReport:
    """Whether ``backend`` is installed and has a browser to drive. Launches nothing."""
    if backend not in _PACKAGES:
        raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")
    if importlib.util.find_spec(_PACKAGES[backend]) is None:
        return BackendReport(
            backend,
            False,
            problem="the Python package is not installed",
            hint=_INSTALL[backend],
        )
    if backend == "playwright":
        from healix.driver.playwright_adapter import diagnose
    else:
        from healix.driver.selenium_adapter import diagnose
    return diagnose()
