"""Create the ``Driver`` for a configured backend."""

from __future__ import annotations

from healix.driver.base import Driver
from healix.platform_adapters import ADAPTERS

BACKENDS = ("playwright", "selenium")


class BackendUnavailableError(RuntimeError):
    """The requested backend can't be used (its library is not installed)."""


def create_driver(
    backend: str = "playwright", *, headless: bool = True, platform_detection: str = "auto"
) -> Driver:
    """A new, not-yet-started driver for ``backend``.

    ``platform_detection`` is ``"auto"`` (the platform adapters may add ``platform_signal``) or
    ``"off"`` (the generic pipeline alone).
    """
    if platform_detection not in ("auto", "off"):
        raise ValueError(f"platform_detection must be 'auto' or 'off', got {platform_detection!r}")
    adapters = ADAPTERS if platform_detection == "auto" else ()
    if backend == "playwright":
        try:
            from healix.driver.playwright_adapter import PlaywrightDriverAdapter
        except ImportError as exc:
            raise BackendUnavailableError(
                "the playwright backend needs Playwright: "
                "pip install 'healix[playwright]' && playwright install chromium"
            ) from exc
        return PlaywrightDriverAdapter(headless=headless, platform_adapters=adapters)
    if backend == "selenium":
        try:
            from healix.driver.selenium_adapter import SeleniumDriverAdapter
        except ImportError as exc:
            raise BackendUnavailableError(
                "the selenium backend needs Selenium: pip install 'healix[selenium]' "
                "(it also needs Chrome installed)"
            ) from exc
        return SeleniumDriverAdapter(headless=headless, platform_adapters=adapters)
    raise ValueError(f"unknown backend {backend!r}; expected one of {BACKENDS}")
