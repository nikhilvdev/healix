"""Driver abstraction. Adapters are imported lazily so backends stay optional."""

from healix.driver.base import Driver, Element, ElementNotFoundError, Frame, SkippedFrame

__all__ = [
    "Driver",
    "Element",
    "ElementNotFoundError",
    "Frame",
    "SkippedFrame",
    "PlaywrightDriverAdapter",
    "SeleniumDriverAdapter",
]


def __getattr__(name: str) -> object:
    if name == "PlaywrightDriverAdapter":
        from healix.driver.playwright_adapter import PlaywrightDriverAdapter

        return PlaywrightDriverAdapter
    if name == "SeleniumDriverAdapter":
        from healix.driver.selenium_adapter import SeleniumDriverAdapter

        return SeleniumDriverAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
