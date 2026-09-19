"""Healix — crawl, classify, extract, and self-heal web automation."""

__version__ = "0.1.0.dev0"

from healix.auth import Credentials  # noqa: E402
from healix.config import ConfigError, RunConfig  # noqa: E402
from healix.healer import Healer  # noqa: E402
from healix.healing import (  # noqa: E402
    ElementNotHealedError,
    FingerprintStore,
    HealingError,
    HealResult,
    SQLiteFingerprintStore,
    UnsupportedRenderingError,
)
from healix.sdk import Crawler, Extractor, Run, RunConflictError  # noqa: E402

__all__ = [
    "ConfigError",
    "Crawler",
    "Credentials",
    "ElementNotHealedError",
    "Extractor",
    "FingerprintStore",
    "HealResult",
    "Healer",
    "HealingError",
    "Run",
    "RunConfig",
    "RunConflictError",
    "SQLiteFingerprintStore",
    "UnsupportedRenderingError",
    "__version__",
]
