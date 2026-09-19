"""Rule-based page classification (no LLM calls)."""

from healix.classification.rules import (
    PAGE_TYPES,
    UNKNOWN,
    Classification,
    classify,
    classify_page,
)

__all__ = ["PAGE_TYPES", "UNKNOWN", "Classification", "classify", "classify_page"]
