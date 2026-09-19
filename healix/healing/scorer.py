"""Weighted similarity between a stored fingerprint and a live element.

When none of a fingerprint's locators find its element, the healer scores every same-tag
element on the page against it and accepts the best one **only if it is confident enough**.

The score is deliberately *not* a flat comparison. Signals are weighted by how stable they
are — a ``data-testid`` or ``aria-label`` says far more about identity than an
auto-generated ``id`` or a DOM index, both of which change on every build:

    stable test attribute  0.25     visible text           0.12     other attributes  0.08
    name                   0.12     nearby label text      0.12     DOM path          0.08
    aria-label             0.10     id (raw/normalized)    0.04     classes           0.04
    parent                 0.03     sibling index          0.02

Three rules decide how the signals combine:

* A signal that is **absent on either side is neutral**, not a mismatch: a dev stripping a
  test id does not make the element a different one. A **different value** is a mismatch.
* The score is the weighted mean of the *comparable* signals, then **damped by how much
  evidence there was** (comparable weight against ``EVIDENCE_TARGET``). A candidate that
  can only be compared on one or two weak signals cannot score high, however well they match.
* Candidates must have the fingerprint's tag.

``LOCATOR_PRIORITY`` is the order primary locators are tried in before any scoring happens
(``Fingerprint.locators`` yields them in this order; a test keeps the two in step).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from healix.driver.base import Element
from healix.healing.fingerprint import STABLE_ATTRIBUTES, Fingerprint
from healix.ids import normalize_id

# Order in which a fingerprint's primary locators are tried.
LOCATOR_PRIORITY = (
    "stable_attr",
    "id",
    "name",
    "aria_label",
    "css",
    "xpath",
    "normalized_id",
    "text",
)
# Strategies specific enough that a unique hit is trusted as the same element when it is the
# first one tried. Positional and text strategies are always verified against the score.
TRUSTED_STRATEGIES = ("stable_attr", "id", "name", "aria_label")

DEFAULT_THRESHOLD = 0.5
DEFAULT_AMBIGUITY_MARGIN = 0.05
EVIDENCE_TARGET = 0.5

WEIGHTS: dict[str, float] = {
    "stable_attr": 0.25,
    "name": 0.12,
    "text": 0.12,
    "aria_label": 0.10,
    "label": 0.12,
    "attributes": 0.08,
    "dom_path": 0.08,
    "id": 0.04,
    "classes": 0.04,
    "parent": 0.03,
    "sibling_index": 0.02,
}

# Attributes that change with every render or carry no identity; never compared.
_VOLATILE_ATTRIBUTES = frozenset(
    {
        "style",
        "tabindex",
        "data-reactid",
        "data-react-checksum",
        "aria-describedby",
        "aria-labelledby",
        "aria-controls",
        "aria-owns",
        "aria-activedescendant",
        "aria-expanded",
        "aria-selected",
        "aria-checked",
        "aria-pressed",
        "aria-hidden",
        "jsname",
        "jscontroller",
        "jsaction",
    }
)
_COVERED_ATTRIBUTES = frozenset({*STABLE_ATTRIBUTES, "aria-label", "class", "id", "name"})
_VOLATILE_PREFIXES = ("data-v-", "_ngcontent", "_nghost", "ng-", "data-react", "data-emotion")


def is_trusted_strategy(strategy: str) -> bool:
    """Whether a locator strategy (e.g. ``stable_attr:data-testid``) is trusted on its own."""
    return strategy.split(":", 1)[0] in TRUSTED_STRATEGIES


@dataclass(frozen=True)
class Signal:
    """One comparison. ``similarity`` is ``None`` when it could not be made (neutral)."""

    name: str
    weight: float
    similarity: float | None


@dataclass(frozen=True)
class Score:
    """How well one candidate matches a fingerprint, and why."""

    element: Element
    confidence: float
    raw: float
    evidence: float
    signals: tuple[Signal, ...]

    def explain(self) -> dict[str, float | None]:
        """Each signal's similarity (``None`` = not comparable), for logs and audits."""
        return {
            s.name: None if s.similarity is None else round(s.similarity, 3) for s in self.signals
        }


# --------------------------------------------------------------------------- #
# Similarity helpers
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def text_similarity(a: str, b: str) -> float:
    """Text similarity in [0, 1]: 1 for equal, and 0 for anything that is merely *not unlike*.

    Raw sequence ratios give unrelated words a comfortable 0.3–0.4; rescaling so a ratio of
    0.5 or less is 0 stops near-noise from adding up.
    """
    a, b = _norm(a), _norm(b)
    if a == b:
        return 1.0
    return max(0.0, (SequenceMatcher(None, a, b).ratio() - 0.5) * 2)


def _tags(xpath: str | None) -> list[str]:
    """Tag names along an xpath, ignoring indexes, predicates, and id-anchor wildcards."""
    if not xpath:
        return []
    tags = []
    for segment in xpath.split("/"):
        tag = re.sub(r"\[.*$", "", segment).strip()
        if tag and tag != "*":
            tags.append(tag)
    return tags


def _stable_attr(fp: Fingerprint, el: Element) -> float | None:
    shared = [
        attr for attr in STABLE_ATTRIBUTES if fp.attributes.get(attr) and el.attributes.get(attr)
    ]
    if not shared:
        return None
    return 1.0 if any(fp.attributes[a] == el.attributes[a] for a in shared) else 0.0


def _aria_label(fp: Fingerprint, el: Element) -> float | None:
    a, b = fp.attributes.get("aria-label"), el.attributes.get("aria-label")
    return text_similarity(a, b) if a and b else None


def _name(fp: Fingerprint, el: Element) -> float | None:
    return text_similarity(fp.name, el.name) if fp.name and el.name else None


def _id(fp: Fingerprint, el: Element) -> float | None:
    if not fp.id or not el.id:
        return None
    if fp.id == el.id:
        return 1.0
    stored = fp.id_normalized or normalize_id(fp.id)
    return 0.8 if stored and stored == (el.id_normalized or normalize_id(el.id)) else 0.0


def _text(fp: Fingerprint, el: Element) -> float | None:
    return (
        text_similarity(fp.text_content, el.text_content)
        if fp.text_content and el.text_content
        else None
    )


def _comparable_attributes(attributes: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in attributes.items()
        if k not in _COVERED_ATTRIBUTES
        and k not in _VOLATILE_ATTRIBUTES
        and not k.startswith(_VOLATILE_PREFIXES)
    }


def _attributes(fp: Fingerprint, el: Element) -> float | None:
    stored, live = _comparable_attributes(fp.attributes), _comparable_attributes(el.attributes)
    shared = [k for k in stored if k in live]
    if not shared:
        return None
    return sum(stored[k] == live[k] for k in shared) / len(shared)


def _classes(fp: Fingerprint, el: Element) -> float | None:
    if not fp.classes or not el.classes:
        return None
    a, b = set(fp.classes), set(el.classes)
    return len(a & b) / len(a | b)


def _path_of(dom_context: dict[str, Any], xpath: str | None) -> list[str]:
    """The tag path: ``dom_context.tag_path`` when captured, else derived from the xpath.

    The xpath of an element with a unique id is anchored at the id and so carries no path.
    """
    recorded = dom_context.get("tag_path")
    if isinstance(recorded, list) and recorded:
        return [str(tag) for tag in recorded]
    return _tags(xpath)


def _dom_path(fp: Fingerprint, el: Element) -> float | None:
    stored, live = _path_of(fp.dom_context, fp.xpath), _path_of(el.dom_context, el.xpath)
    if not stored or not live:
        return None
    similarity = SequenceMatcher(None, stored, live).ratio()
    if fp.iframe_path != el.iframe_path:
        similarity *= 0.5  # a different frame is a different place
    if fp.shadow_path != el.shadow_path:
        similarity *= 0.5
    return similarity


def _label(fp: Fingerprint, el: Element) -> float | None:
    a = fp.dom_context.get("nearby_label_text")
    b = el.dom_context.get("nearby_label_text")
    return text_similarity(a, b) if a and b else None


def _parent(fp: Fingerprint, el: Element) -> float | None:
    parts: list[float] = []
    stored_tag, live_tag = fp.dom_context.get("parent_tag"), el.dom_context.get("parent_tag")
    if stored_tag and live_tag:
        parts.append(1.0 if stored_tag == live_tag else 0.0)
    stored_id, live_id = fp.dom_context.get("parent_id"), el.dom_context.get("parent_id")
    if stored_id and live_id:
        parts.append(1.0 if normalize_id(stored_id) == normalize_id(live_id) else 0.0)
    return sum(parts) / len(parts) if parts else None


def _sibling_index(fp: Fingerprint, el: Element) -> float | None:
    a, b = fp.dom_context.get("sibling_index"), el.dom_context.get("sibling_index")
    if not isinstance(a, int) or not isinstance(b, int):
        return None
    return 1.0 / (1 + abs(a - b))


_COMPARATORS = {
    "stable_attr": _stable_attr,
    "name": _name,
    "text": _text,
    "aria_label": _aria_label,
    "label": _label,
    "attributes": _attributes,
    "dom_path": _dom_path,
    "id": _id,
    "classes": _classes,
    "parent": _parent,
    "sibling_index": _sibling_index,
}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def score_candidate(fingerprint: Fingerprint, element: Element) -> Score:
    """Score ``element`` against ``fingerprint``. A different tag scores zero."""
    if fingerprint.tag and element.tag != fingerprint.tag:
        return Score(element, 0.0, 0.0, 0.0, ())
    signals = tuple(
        Signal(name, WEIGHTS[name], compare(fingerprint, element))
        for name, compare in _COMPARATORS.items()
    )
    comparable = [s for s in signals if s.similarity is not None]
    weight = sum(s.weight for s in comparable)
    if weight == 0:
        return Score(element, 0.0, 0.0, 0.0, signals)
    raw = sum(s.weight * (s.similarity or 0.0) for s in comparable) / weight
    evidence = min(1.0, weight / EVIDENCE_TARGET)
    return Score(element, round(raw * evidence, 4), round(raw, 4), round(evidence, 4), signals)


def rank_candidates(fingerprint: Fingerprint, elements: Sequence[Element]) -> list[Score]:
    """Score every same-tag element, best first (ties keep document order)."""
    scored = [
        score_candidate(fingerprint, e)
        for e in elements
        if not fingerprint.tag or e.tag == fingerprint.tag
    ]
    return sorted(scored, key=lambda s: -s.confidence)
