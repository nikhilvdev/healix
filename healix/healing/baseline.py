"""Recording fingerprints from extracted elements.

This is how the store gets its baseline: after a page is extracted, every element a script would
act on (``roles.is_healable``) is fingerprinted under a stable role name. Recording is ordered and
happens inside the sequential extraction walk, which is why extraction is not parallelised.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from healix.driver.base import Element
from healix.healing.fingerprint import Fingerprint
from healix.healing.roles import assign_roles
from healix.healing.store import FingerprintStore

KEEP = "keep"
REFRESH = "refresh"
MODES = (KEEP, REFRESH)


@dataclass(frozen=True)
class RecordSummary:
    """What ``record_page`` did."""

    added: int = 0
    refreshed: int = 0
    kept: int = 0

    @property
    def total(self) -> int:
        return self.added + self.refreshed + self.kept


def record_page(
    store: FingerprintStore,
    page_url: str,
    elements: Sequence[Element],
    *,
    mode: str = KEEP,
) -> RecordSummary:
    """Fingerprint the healable elements of one page.

    ``mode="keep"`` (the default) only adds roles that are new: an existing fingerprint is the
    known-good baseline, and a page that has since changed must be *healed* (which is audited),
    not silently re-baselined. ``mode="refresh"`` overwrites — use it deliberately, after an
    intended redesign.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    added = refreshed = kept = 0
    for role, element in assign_roles(elements):
        fingerprint = Fingerprint.from_element(element, page_url, role)
        if mode == REFRESH:
            existed = store.get(page_url, role) is not None
            store.put(fingerprint)
            refreshed, added = (refreshed + 1, added) if existed else (refreshed, added + 1)
        elif store.put_if_absent(fingerprint):
            added += 1
        else:
            kept += 1
    return RecordSummary(added=added, refreshed=refreshed, kept=kept)
