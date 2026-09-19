"""Driver abstraction. Adapters are imported lazily so backends stay optional."""

from healix.driver.base import Driver, Element, ElementNotFoundError, Frame

__all__ = ["Driver", "Element", "ElementNotFoundError", "Frame", "PlaywrightDriverAdapter"]


def __getattr__(name: str) -> object:
    if name == "PlaywrightDriverAdapter":
        from healix.driver.playwright_adapter import PlaywrightDriverAdapter

        return PlaywrightDriverAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
