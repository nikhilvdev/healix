"""The self-healing resolver: find an element again, and repair its fingerprint if it moved.

Resolution goes in three steps:

1. **Primary locators**, in priority order (stable attributes → id → name → aria-label → css →
   xpath → normalized id → text). The first one to match a *unique* element wins.
   A strategy that is specific enough to trust (test id, id, name, aria-label) is accepted as it
   is when it is the first one tried. Any other match — a later strategy, or a positional or text
   one — is **verified** against the fingerprint with the scorer and rejected if it does not clear
   the confidence threshold: a locator that matches *something* is not the same as matching *the
   element*.
2. **Scoring.** If no locator survives, every same-tag element on the page is scored against the
   fingerprint (``healix.healing.scorer``) — weighted, so stable signals count for more than
   volatile ones.
3. **The gate.** The best candidate must reach the confidence threshold (default 0.5), and must
   not be a near-tie with the runner-up. A best-but-weak or ambiguous match is **rejected**, never
   silently used.

An accepted heal replaces the stored fingerprint with the healed element's, and is written to the
healing history in the same transaction, so the fix survives future runs and can be audited.

Anything that can't be resolved raises: ``ElementNotHealedError``, or — when the page is
canvas-rendered and so has no DOM elements to find — ``UnsupportedRenderingError``. It never
returns a guess.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from healix.driver.base import Driver, Element, ElementNotFoundError
from healix.healing.fingerprint import Fingerprint, describe_locator
from healix.healing.history import KIND_FALLBACK, KIND_SCORED, HealRecord, diff_fingerprints
from healix.healing.scorer import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_THRESHOLD,
    Score,
    is_trusted_strategy,
    locator_hit_holds,
    rank_candidates,
    score_candidate,
)
from healix.healing.store import FingerprintStore
from healix.log import get_logger
from healix.timeutil import iso_utc, utc_now

logger = get_logger(__name__)

EXACT = "exact"
HEALED = "healed"
SCORED_STRATEGY = "weighted_score"

# A canvas (or plugin object) this much of the page, with next to nothing else on it, means the UI
# is drawn rather than built from DOM elements.
_CANVAS_PAGE_SHARE = 0.4
_CANVAS_FALLBACK_AREA = 300 * 300
_CANVAS_MAX_DOM_CONTROLS = 3


class HealingError(Exception):
    """Something went wrong resolving or healing an element."""


class FingerprintNotFoundError(HealingError, LookupError):
    """There is no stored fingerprint for the requested page and role."""


class ElementNotHealedError(HealingError, ElementNotFoundError):
    """The element could not be found, and no candidate was a confident enough match."""


class UnsupportedRenderingError(HealingError):
    """The page draws its UI (canvas, plugin) instead of using DOM elements.

    No locator strategy — generic or platform-specific — can find elements that are not in the
    DOM, so healing refuses rather than guess. This is a documented boundary, not a bug.
    """


@dataclass(frozen=True)
class HealResult:
    """The outcome of resolving one fingerprint.

    ``outcome`` is ``"exact"`` (a locator matched; nothing changed) or ``"healed"`` (the
    fingerprint was repaired and ``record`` says how). ``strategy`` is the locator strategy that
    matched, or ``"weighted_score"`` for a scored heal. ``confidence`` is ``None`` for a trusted
    exact match and the score otherwise. ``fingerprint`` is what is now stored.
    """

    element: Element
    outcome: str
    strategy: str
    confidence: float | None
    fingerprint: Fingerprint
    record: HealRecord | None = None

    @property
    def healed(self) -> bool:
        return self.outcome == HEALED


def _area(element: Element) -> float:
    box = element.computed.get("bounding_box") or {}
    return float(box.get("width", 0)) * float(box.get("height", 0))


def looks_canvas_rendered(elements: Sequence[Element]) -> bool:
    """Whether the page is (almost) a single canvas/plugin surface with hardly any DOM controls."""
    visible = [e for e in elements if e.computed.get("visible", True)]
    page_area = max(
        (_area(e) for e in visible if e.iframe_path == ["main"] and e.tag in ("body", "html")),
        default=0.0,
    )
    minimum = page_area * _CANVAS_PAGE_SHARE if page_area else _CANVAS_FALLBACK_AREA
    surfaces = [
        e
        for e in visible
        if e.tag in ("canvas", "object", "embed", "applet") and _area(e) >= minimum
    ]
    if not surfaces:
        return False
    inside = [(s.iframe_path, f"{s.css_selector} ") for s in surfaces if s.css_selector]
    controls = [
        e
        for e in visible
        if (
            e.tag in ("input", "button", "select", "textarea")
            or (e.tag == "a" and e.computed.get("href"))
        )
        and e.attributes.get("type") != "hidden"
        and not any(
            e.iframe_path == path and (e.css_selector or "").startswith(prefix)
            for path, prefix in inside
        )
    ]
    return len(controls) <= _CANVAS_MAX_DOM_CONTROLS


class Resolver:
    """Resolve fingerprints against a live page, healing them when they have drifted.

    ``threshold`` is the minimum confidence for accepting a match (default 0.5).
    ``ambiguity_margin`` rejects a best candidate that is within that margin of a runner-up which
    also clears the threshold — two look-alikes are not a match, they are a coin toss.
    ``on_heal`` receives each stored ``HealRecord``.
    """

    def __init__(
        self,
        store: FingerprintStore,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
        run_id: str | None = None,
        on_heal: Callable[[HealRecord], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if ambiguity_margin < 0:
            raise ValueError("ambiguity_margin cannot be negative")
        self.store = store
        self.threshold = threshold
        self.ambiguity_margin = ambiguity_margin
        self.run_id = run_id
        self.on_heal = on_heal
        self.clock = clock

    def resolve(self, driver: Driver, page_url: str, element_role: str) -> HealResult:
        """Find the element ``(page_url, element_role)`` on the current page, healing if needed."""
        fingerprint = self.store.get(page_url, element_role)
        if fingerprint is None:
            raise FingerprintNotFoundError(
                f"no fingerprint stored for role {element_role!r} on {page_url}; "
                "record one first (Healer.learn, or extract with a fingerprint store)"
            )

        attempts: list[str] = []
        for index, spec in enumerate(fingerprint.locators()):
            found = driver.locate(spec, fingerprint)
            if found is None:
                attempts.append(f"{spec.strategy}: no unique match")
                continue
            if index == 0 and is_trusted_strategy(spec.strategy):
                logger.debug("resolved", role=element_role, strategy=spec.strategy)
                return HealResult(found, EXACT, spec.strategy, None, fingerprint)
            score = score_candidate(fingerprint, found)
            if score.confidence < self.threshold and not locator_hit_holds(
                fingerprint, score, self.threshold
            ):
                attempts.append(
                    f"{spec.strategy}: matched, but only {score.confidence:.2f} "
                    "like the stored element"
                )
                logger.warn(
                    "a locator matched something that does not resemble the stored element",
                    role=element_role,
                    page_url=page_url,
                    strategy=spec.strategy,
                    confidence=score.confidence,
                    threshold=self.threshold,
                )
                continue
            if index == 0:
                return HealResult(found, EXACT, spec.strategy, score.confidence, fingerprint)
            return self._heal(fingerprint, found, KIND_FALLBACK, spec.strategy, score.confidence)

        elements = driver.get_elements()
        ranked = rank_candidates(fingerprint, elements)
        best = ranked[0] if ranked else None
        if best is None or best.confidence < self.threshold:
            self._fail("below_threshold", fingerprint, elements, ranked, attempts)
        assert best is not None
        rival = ranked[1] if len(ranked) > 1 else None
        if (
            rival is not None
            and rival.confidence >= self.threshold
            and best.confidence - rival.confidence < self.ambiguity_margin
        ):
            self._fail("ambiguous", fingerprint, elements, ranked, attempts)
        return self._heal(
            fingerprint, best.element, KIND_SCORED, SCORED_STRATEGY, best.confidence, best
        )

    # -- healing ------------------------------------------------------------------- #

    def _heal(
        self,
        old: Fingerprint,
        element: Element,
        kind: str,
        strategy: str,
        confidence: float,
        score: Score | None = None,
    ) -> HealResult:
        new = Fingerprint.from_element(element, old.page_url, old.element_role)
        changed, change_kind = diff_fingerprints(old, new)
        record = HealRecord(
            page_url=old.page_url,
            element_role=old.element_role,
            at=iso_utc(self.clock()),
            kind=kind,
            strategy=strategy,
            confidence=round(confidence, 3),
            old_locator=describe_locator(old),
            new_locator=describe_locator(new),
            change_kind=change_kind,
            changed_fields=tuple(changed),
            old_fingerprint=old.to_dict(),
            new_fingerprint=new.to_dict(),
            run_id=self.run_id,
        )
        stored = self.store.apply_heal(new, record)  # fingerprint + history, atomically
        logger.info(
            "healed element",
            role=old.element_role,
            page_url=old.page_url,
            kind=kind,
            strategy=strategy,
            confidence=stored.confidence,
            change_kind=change_kind,
            changed=changed,
            signals=score.explain() if score else None,
        )
        if self.on_heal is not None:
            try:
                self.on_heal(stored)
            except Exception as exc:
                logger.warn("on_heal callback raised; continuing", error_type=type(exc).__name__)
        return HealResult(element, HEALED, strategy, stored.confidence, new, stored)

    def _fail(
        self,
        reason: str,
        fingerprint: Fingerprint,
        elements: Sequence[Element],
        ranked: Sequence[Score],
        attempts: Sequence[str],
    ) -> None:
        """Raise the right error. Nothing is changed in the store."""
        where = f"{fingerprint.element_role!r} on {fingerprint.page_url}"
        if looks_canvas_rendered(elements):
            logger.warn("page is canvas-rendered; cannot heal", role=fingerprint.element_role)
            raise UnsupportedRenderingError(
                f"cannot find {where}: the page draws its UI on a canvas (or plugin) instead of "
                "using DOM elements, and no locator can reach elements that are not in the DOM. "
                "This is a documented limit of Healix."
            )
        best = ranked[0] if ranked else None
        if reason == "ambiguous" and best is not None:
            detail = (
                f"the two best candidates scored {best.confidence:.2f} and "
                f"{ranked[1].confidence:.2f}, closer than the {self.ambiguity_margin:.2f} margin, "
                "so neither can be trusted to be the same element"
            )
        elif best is not None:
            detail = (
                f"the best candidate ({best.element.css_selector}) scored {best.confidence:.2f}, "
                f"below the {self.threshold:.2f} threshold"
            )
        else:
            detail = f"the page has no <{fingerprint.tag}> elements to compare"
        logger.warn(
            "could not heal element",
            role=fingerprint.element_role,
            page_url=fingerprint.page_url,
            reason=reason,
            best_confidence=best.confidence if best else None,
            threshold=self.threshold,
        )
        tried = f" (locators tried: {'; '.join(attempts)})" if attempts else ""
        raise ElementNotHealedError(f"cannot find {where}: {detail}{tried}")
