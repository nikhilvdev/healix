"""Self-healing: fingerprints, weighted scoring, a persistent store, and the resolver.

The public names are exported lazily. ``healix.driver.base`` imports ``healix.healing.fingerprint``
and the resolver imports ``healix.driver.base``; importing this package eagerly would turn that
into an import cycle.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

_EXPORTS: dict[str, tuple[str, ...]] = {
    "healix.healing.baseline": ("KEEP", "REFRESH", "RecordSummary", "record_page"),
    "healix.healing.fingerprint": ("Fingerprint", "LocatorSpec", "describe_locator"),
    "healix.healing.history": (
        "CHURN",
        "REGRESSION",
        "HealRecord",
        "churn_report",
        "diff_fingerprints",
    ),
    "healix.healing.resolver": (
        "EXACT",
        "HEALED",
        "ElementNotHealedError",
        "FingerprintNotFoundError",
        "HealingError",
        "HealResult",
        "Resolver",
        "UnsupportedRenderingError",
        "looks_canvas_rendered",
    ),
    "healix.healing.roles": ("assign_roles", "derive_role", "is_healable"),
    "healix.healing.scorer": (
        "DEFAULT_AMBIGUITY_MARGIN",
        "DEFAULT_THRESHOLD",
        "LOCATOR_PRIORITY",
        "WEIGHTS",
        "Score",
        "rank_candidates",
        "score_candidate",
    ),
    "healix.healing.store": (
        "DEFAULT_DB_PATH",
        "FingerprintStore",
        "SQLiteFingerprintStore",
        "StoreError",
        "open_store",
    ),
    "healix.healing.postgres_store": ("PostgresFingerprintStore",),
}
_MODULE_OF = {name: module for module, names in _EXPORTS.items() for name in names}

__all__ = [
    "CHURN",
    "DEFAULT_AMBIGUITY_MARGIN",
    "DEFAULT_DB_PATH",
    "DEFAULT_THRESHOLD",
    "EXACT",
    "ElementNotHealedError",
    "Fingerprint",
    "FingerprintNotFoundError",
    "FingerprintStore",
    "HEALED",
    "HealRecord",
    "HealResult",
    "HealingError",
    "KEEP",
    "LOCATOR_PRIORITY",
    "LocatorSpec",
    "PostgresFingerprintStore",
    "REFRESH",
    "REGRESSION",
    "RecordSummary",
    "Resolver",
    "SQLiteFingerprintStore",
    "Score",
    "StoreError",
    "UnsupportedRenderingError",
    "WEIGHTS",
    "assign_roles",
    "churn_report",
    "derive_role",
    "describe_locator",
    "diff_fingerprints",
    "is_healable",
    "looks_canvas_rendered",
    "open_store",
    "rank_candidates",
    "record_page",
    "score_candidate",
]


def __getattr__(name: str) -> Any:
    module = _MODULE_OF.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # cache: later lookups skip __getattr__
    return value


if TYPE_CHECKING:
    from healix.healing.baseline import KEEP, REFRESH, RecordSummary, record_page
    from healix.healing.fingerprint import Fingerprint, LocatorSpec, describe_locator
    from healix.healing.history import (
        CHURN,
        REGRESSION,
        HealRecord,
        churn_report,
        diff_fingerprints,
    )
    from healix.healing.postgres_store import PostgresFingerprintStore
    from healix.healing.resolver import (
        EXACT,
        HEALED,
        ElementNotHealedError,
        FingerprintNotFoundError,
        HealingError,
        HealResult,
        Resolver,
        UnsupportedRenderingError,
        looks_canvas_rendered,
    )
    from healix.healing.roles import assign_roles, derive_role, is_healable
    from healix.healing.scorer import (
        DEFAULT_AMBIGUITY_MARGIN,
        DEFAULT_THRESHOLD,
        LOCATOR_PRIORITY,
        WEIGHTS,
        Score,
        rank_candidates,
        score_candidate,
    )
    from healix.healing.store import (
        DEFAULT_DB_PATH,
        FingerprintStore,
        SQLiteFingerprintStore,
        StoreError,
        open_store,
    )
