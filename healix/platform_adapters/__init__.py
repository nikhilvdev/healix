"""Thin, optional platform adapters: extra ``platform_signal`` on top of the generic pipeline.

Adapters are additive only. The generic pipeline (iframe traversal, shadow DOM piercing, stable-id
normalization) always runs whatever platform is detected, and never depends on an adapter firing.
"""

from __future__ import annotations

from collections.abc import Iterable

from healix.platform_adapters.base import PlatformAdapter
from healix.platform_adapters.salesforce_lwc import ADAPTER as SALESFORCE_LWC
from healix.platform_adapters.sap_ui5 import ADAPTER as SAP_UI5

# Every adapter, in the order they are tried. The first one that has a signal for an element wins.
ADAPTERS: tuple[PlatformAdapter, ...] = (SAP_UI5, SALESFORCE_LWC)

__all__ = ["ADAPTERS", "SALESFORCE_LWC", "SAP_UI5", "PlatformAdapter", "detected_platform"]


def detected_platform(elements: Iterable[object]) -> str | None:
    """The platform named by the first element that carries a ``platform_signal``, else ``None``."""
    for element in elements:
        signal = getattr(element, "platform_signal", None)
        if isinstance(signal, dict) and signal.get("platform"):
            return str(signal["platform"])
    return None
