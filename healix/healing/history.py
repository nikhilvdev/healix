"""The healing history: an audit trail of every heal.

Each heal is recorded with the fingerprint before and after, which fields changed, and a
verdict on *what kind* of change it was, so a team can tell routine **selector churn**
(regenerated ids, hashed class names, a moved wrapper — the element is the same, only its
handles changed) from a real **UI regression** (the element's visible meaning changed: its
text, its label, its type, its tag). Churn is expected and harmless; a regression deserves
a human look, because a healed script would otherwise keep passing while the product
changed underneath it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from healix.healing.fingerprint import Fingerprint
from healix.healing.scorer import text_similarity

CHURN = "churn"
REGRESSION = "regression"

# How a heal was made.
KIND_SCORED = "scored"  # no locator matched; the best-scoring candidate was accepted
KIND_FALLBACK = "fallback"  # a lower-priority locator matched after higher ones failed

# Similarity below this for text/label means the visible meaning changed.
_MEANING_THRESHOLD = 0.6


@dataclass(frozen=True)
class HealRecord:
    """One heal, as stored in the history."""

    page_url: str
    element_role: str
    at: str
    kind: str
    strategy: str
    confidence: float
    old_locator: str
    new_locator: str
    change_kind: str
    changed_fields: tuple[str, ...]
    old_fingerprint: dict[str, Any] = field(default_factory=dict)
    new_fingerprint: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    id: int | None = None

    @property
    def element_key(self) -> str:
        return f"{self.page_url}#{self.element_role}"

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["changed_fields"] = list(self.changed_fields)
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HealRecord:
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if k in known}
        data["changed_fields"] = tuple(data.get("changed_fields", ()))
        return cls(**data)


def _similar(a: str | None, b: str | None) -> float:
    return text_similarity(a, b) if a and b else 1.0  # a missing side is not evidence of a change


def diff_fingerprints(old: Fingerprint, new: Fingerprint) -> tuple[list[str], str]:
    """The fields that differ between two fingerprints, and whether that is churn or regression.

    A **regression** is a change to what a user sees or what the element *is*: a different tag,
    input type, or role; text, accessible name, placeholder or label that no longer resembles
    the old. Everything else — ids, classes, test ids, name attributes, DOM position — is
    **churn**: the handles moved, the element did not.
    """
    changed: list[str] = []
    for name in ("tag", "id", "id_normalized", "name", "text_content", "xpath", "css_selector"):
        if getattr(old, name) != getattr(new, name):
            changed.append(name)
    if old.classes != new.classes:
        changed.append("classes")
    if old.iframe_path != new.iframe_path:
        changed.append("iframe_path")
    if old.shadow_path != new.shadow_path:
        changed.append("shadow_path")
    for attr in sorted(set(old.attributes) | set(new.attributes)):
        if old.attributes.get(attr) != new.attributes.get(attr):
            changed.append(f"attributes.{attr}")
    for key in sorted(set(old.dom_context) | set(new.dom_context)):
        if old.dom_context.get(key) != new.dom_context.get(key):
            changed.append(f"dom_context.{key}")

    regression = (
        old.tag != new.tag
        or _differs(old.attributes.get("type"), new.attributes.get("type"))
        or _differs(old.attributes.get("role"), new.attributes.get("role"))
        or _similar(old.text_content, new.text_content) < _MEANING_THRESHOLD
        or _similar(old.attributes.get("aria-label"), new.attributes.get("aria-label"))
        < _MEANING_THRESHOLD
        or _similar(old.attributes.get("placeholder"), new.attributes.get("placeholder"))
        < _MEANING_THRESHOLD
        or _similar(
            old.dom_context.get("nearby_label_text"), new.dom_context.get("nearby_label_text")
        )
        < _MEANING_THRESHOLD
    )
    return changed, REGRESSION if regression else CHURN


def _differs(a: str | None, b: str | None) -> bool:
    return bool(a and b and a != b)


def churn_report(records: Iterable[HealRecord]) -> list[dict[str, Any]]:
    """Per element: how often it healed and how many of those heals looked like regressions.

    Elements that heal constantly are the flaky locators worth stabilising; any with a
    non-zero ``regressions`` count deserve a look. Most-healed first.
    """
    heals: Counter[str] = Counter()
    regressions: Counter[str] = Counter()
    last: dict[str, str] = {}
    for record in records:
        key = record.element_key
        heals[key] += 1
        if record.change_kind == REGRESSION:
            regressions[key] += 1
        last[key] = max(last.get(key, ""), record.at)
    return [
        {
            "element_key": key,
            "heals": count,
            "regressions": regressions[key],
            "churn": count - regressions[key],
            "last_healed_at": last[key],
        }
        for key, count in heals.most_common()
    ]


__all__ = [
    "CHURN",
    "KIND_FALLBACK",
    "KIND_SCORED",
    "REGRESSION",
    "HealRecord",
    "churn_report",
    "diff_fingerprints",
]
