"""Healix — crawl, classify, extract, and self-heal web automation."""

__version__ = "1.1.0"

from healix.auth import Credentials  # noqa: E402
from healix.config import ConfigError, RunConfig  # noqa: E402
from healix.generation import GenerationError, ScriptGenerator  # noqa: E402
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
    "GenerationError",
    "HealResult",
    "Healer",
    "HealingError",
    "Run",
    "RunConfig",
    "RunConflictError",
    "ScriptGenerator",
    "SQLiteFingerprintStore",
    "UnsupportedRenderingError",
    "__version__",
]
