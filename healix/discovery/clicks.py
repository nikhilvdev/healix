"""Which elements click-through discovery may click.

Some navigation is only script: a button that calls ``history.pushState``, a ``<div role="button">``
that sets ``location``. There is no ``<a href>`` to follow, so link discovery cannot see it. Click
discovery clicks such elements and notes where the page ends up.

Clicking things on a live site can do harm, so this module decides what is *not* clicked, and errs
towards skipping. It has no browser dependency: it works on the ``Element`` list a driver returns.

Never clicked:

* anything that would submit a form (``<button>`` in a form without ``type="button"``, and
  ``input`` of type submit, image or reset, or with a ``formaction``);
* anything whose label, ``aria-label``, ``title``, ``id``, classes or ``data-testid`` suggests it
  deletes, pays, signs out, saves, sends or otherwise commits something (``DENY_WORDS``, plus any
  the run adds). The match is on the start of a word, so it skips more than it strictly must: a
  "Payments" menu is skipped along with "Pay now". That is intended;
* anything hidden or disabled, and downloads.

Labels are read from the element and everything inside it, so
``<button><span>Delete</span></button>`` is caught even though the button itself has no text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from healix.driver.base import Element

# Words that mean "this changes something", matched at the start of a word in the element's label
# and attributes. Deliberately broad; see the module docstring.
DENY_WORDS: tuple[str, ...] = (
    "delet", "remov", "destr", "eras", "discard", "purg", "wipe", "archiv", "trash",
    "pay", "purchas", "buy", "checkout", "check out", "donat", "upgrad",
    "place order", "order now", "complete order", "book now", "add to cart", "add to bag",
    "add to basket",
    "sign out", "signout", "sign off", "log out", "logout", "log off", "logoff",
    "submit", "send", "save", "confirm", "approv", "reject", "declin", "accept", "agree", "apply",
    "publish", "transfer", "withdraw", "invit",
    "cancel", "unsubscrib", "subscrib", "deactivat", "terminat", "revok", "reset", "clear",
    "disabl", "danger", "destructiv",
)  # fmt: skip

# Whole words only: as prefixes they would also match "bank" and "blocks".
_WHOLE_WORDS: tuple[str, ...] = ("ban", "block", "kick")

_FORM_SUBMITTING_INPUTS = frozenset({"submit", "image", "reset"})
_CLICKABLE_ROLES = frozenset({"button", "link", "tab", "menuitem"})
_NOT_CLICKABLE_TAGS = frozenset({"html", "body", "form", "head"})
_LABEL_ATTRIBUTES = ("aria-label", "title", "value", "alt", "name", "data-testid", "data-test")

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> str:
    """``text`` as lowercase words separated by single spaces (``deleteBtn`` -> ``delete btn``)."""
    return _NON_ALNUM.sub(" ", _CAMEL.sub(r"\1 \2", text).lower()).strip()


def deny_pattern(extra: Iterable[str] = ()) -> re.Pattern[str]:
    """One pattern for every denied word, matched against ``_words`` output."""
    start, end = "(?<![a-z0-9])", "(?![a-z0-9])"
    prefixes = [re.escape(_words(w)) for w in (*DENY_WORDS, *extra) if _words(w)]
    parts = [f"{start}(?:{'|'.join(prefixes)})"] if prefixes else []
    parts.append(f"{start}(?:{'|'.join(_WHOLE_WORDS)}){end}")
    return re.compile("|".join(parts))


@dataclass(frozen=True)
class Candidate:
    """An element worth clicking, with the label to show for it."""

    element: Element
    label: str


@dataclass(frozen=True)
class Selection:
    """What ``select_candidates`` decided about a page."""

    candidates: list[Candidate]
    skipped_unsafe: int  # would have been clicked, but might commit something
    over_limit: int  # safe, but beyond ``limit``


def select_candidates(
    elements: Sequence[Element], *, limit: int, extra_deny: Iterable[str] = ()
) -> Selection:
    """The safe, distinct, clickable elements of a page in document order, at most ``limit``."""
    pattern = deny_pattern(extra_deny)
    candidates: list[Candidate] = []
    seen: set[tuple[object, ...]] = set()
    skipped = over = 0
    for index, element in enumerate(elements):
        if not _looks_clickable(element):
            continue
        if _would_commit(element) or pattern.search(_signals(elements, index)):
            skipped += 1
            continue
        label = _label(elements, index)
        key = (
            tuple(element.iframe_path),
            element.tag,
            element.attributes.get("role"),
            _words(label) or "class:" + " ".join(element.classes),
        )
        if key in seen:
            continue
        seen.add(key)
        if len(candidates) >= limit:
            over += 1
            continue
        candidates.append(Candidate(element, label))
    return Selection(candidates, skipped, over)


# -- what is clickable ---------------------------------------------------------------------- #


def _looks_clickable(element: Element) -> bool:
    if element.tag in _NOT_CLICKABLE_TAGS:
        return False
    if not element.computed.get("visible") or element.computed.get("enabled") is False:
        return False
    if not element.css_selector:
        return False
    attributes = element.attributes
    if element.tag == "button":
        return True
    if element.tag == "input":
        return (attributes.get("type") or "").lower() == "button"
    if element.tag == "a":
        # A real link is followed by ordinary discovery; only a script-driven one is clicked.
        href = (attributes.get("href") or "").strip().lower()
        return href in ("", "#") or href.startswith("javascript:")
    if (attributes.get("role") or "").lower() in _CLICKABLE_ROLES:
        return True
    return "onclick" in attributes


def _would_commit(element: Element) -> bool:
    """Would clicking this submit a form or download something, whatever it is called?"""
    attributes = element.attributes
    if "formaction" in attributes or "download" in attributes:
        return True
    kind = (attributes.get("type") or "").lower()
    if element.tag == "input" and kind in _FORM_SUBMITTING_INPUTS:
        return True
    # A button in a form submits it unless it says otherwise.
    return element.tag == "button" and _inside_form(element) and kind in ("", "submit", "reset")


def _inside_form(element: Element) -> bool:
    return "form" in (element.dom_context.get("tag_path") or [])[:-1]


# -- what it is called ---------------------------------------------------------------------- #


def _descendants(elements: Sequence[Element], index: int) -> list[Element]:
    """The elements inside ``elements[index]``: they follow it in document order."""
    parent = elements[index]
    prefix = (parent.css_selector or "") + " "
    frame = parent.iframe_path
    found: list[Element] = []
    for other in elements[index + 1 :]:
        if other.iframe_path != frame or not (other.css_selector or "").startswith(prefix):
            break
        found.append(other)
    return found


def _label(elements: Sequence[Element], index: int) -> str:
    """What a person would call this element: its text, else its accessible name."""
    element = elements[index]
    texts = [element.text_content, *(d.text_content for d in _descendants(elements, index))]
    text = " ".join(t for t in texts if t).strip()
    if text:
        return text[:60]
    for name in _LABEL_ATTRIBUTES:
        value = (element.attributes.get(name) or "").strip()
        if value:
            return value[:60]
    return element.id or ""


def _signals(elements: Sequence[Element], index: int) -> str:
    """Everything about an element that could say what it does, as words."""
    element = elements[index]
    pieces = [_label(elements, index), element.id or "", " ".join(element.classes)]
    pieces += [element.attributes.get(name) or "" for name in _LABEL_ATTRIBUTES]
    pieces.append(element.attributes.get("href") or "")
    pieces.append(element.attributes.get("onclick") or "")
    for descendant in _descendants(elements, index):
        pieces += [descendant.id or "", " ".join(descendant.classes)]
        pieces += [descendant.attributes.get(name) or "" for name in ("aria-label", "title")]
    return " ".join(_words(p) for p in pieces if p)
