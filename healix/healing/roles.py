"""Element roles: the stable names under which fingerprints are stored.

A fingerprint is keyed by ``(page_url, element_role)``, so a role must name an element
in a way that survives the page changing — ``textbox:username``, ``button:sign-in`` —
rather than describe where it sits. ``derive_role`` builds one from the most stable
identity an element has (test id, then accessible name, name attribute, label, text,
placeholder, …); ``assign_roles`` numbers duplicates on a page (``button:save#2``) in
document order.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from healix.driver.base import Element
from healix.healing.fingerprint import STABLE_ATTRIBUTES

_INTERACTIVE_TAGS = frozenset({"input", "button", "select", "textarea"})
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "menuitem",
        "tab",
        "switch",
        "slider",
        "option",
    }
)
_INPUT_KINDS = {
    "": "textbox",
    "text": "textbox",
    "email": "textbox",
    "password": "textbox",
    "tel": "textbox",
    "url": "textbox",
    "number": "textbox",
    "search": "searchbox",
    "checkbox": "checkbox",
    "radio": "radio",
    "submit": "button",
    "button": "button",
    "reset": "button",
    "image": "button",
    "file": "file",
    "range": "slider",
}
_MAX_SLUG = 40


def _type(element: Element) -> str:
    return element.attributes.get("type", "").lower()


def is_healable(element: Element) -> bool:
    """Whether an element is worth fingerprinting: something a script would act on."""
    if not element.computed.get("visible", True):
        return False
    if element.tag == "input" and _type(element) == "hidden":
        return False
    if element.tag == "a":
        return bool(element.attributes.get("href") or element.computed.get("href"))
    if element.tag in _INTERACTIVE_TAGS:
        return True
    if element.attributes.get("role", "").lower() in _INTERACTIVE_ROLES:
        return True
    return any(element.attributes.get(attr) for attr in STABLE_ATTRIBUTES)


def element_kind(element: Element) -> str:
    """A coarse, human-readable kind: ``textbox``, ``button``, ``link``, ``checkbox``, …"""
    role = element.attributes.get("role", "").lower()
    if role:
        return role
    if element.tag == "input":
        return _INPUT_KINDS.get(_type(element), "textbox")
    return {"button": "button", "a": "link", "select": "combobox", "textarea": "textbox"}.get(
        element.tag, element.tag
    )


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:_MAX_SLUG].rstrip("-")


def _identity(element: Element) -> str:
    """The most stable human-meaningful label an element has."""
    attrs = element.attributes
    for attr in STABLE_ATTRIBUTES:
        if attrs.get(attr):
            return attrs[attr]
    candidates = (
        attrs.get("aria-label"),
        element.name,
        element.dom_context.get("nearby_label_text"),
        element.text_content,
        attrs.get("placeholder"),
        attrs.get("value") if _type(element) in ("submit", "button") else None,
        attrs.get("title"),
        element.id_normalized,
        _href_tail(attrs.get("href") or element.computed.get("href")),
    )
    return next((c for c in candidates if c and _slug(c)), "")


def _href_tail(href: str | None) -> str | None:
    if not href:
        return None
    path = re.sub(r"[?#].*$", "", href).rstrip("/")
    return path.rsplit("/", 1)[-1] or None


def derive_role(element: Element) -> str:
    """``kind:slug`` for one element (``kind`` alone when it has no usable identity)."""
    slug = _slug(_identity(element))
    kind = element_kind(element)
    return f"{kind}:{slug}" if slug else kind


def assign_roles(elements: Sequence[Element]) -> list[tuple[str, Element]]:
    """Roles for every healable element on a page; repeats get ``#2``, ``#3`` in document order."""
    seen: dict[str, int] = {}
    assigned: list[tuple[str, Element]] = []
    for element in elements:
        if not is_healable(element):
            continue
        role = derive_role(element)
        seen[role] = seen.get(role, 0) + 1
        assigned.append((role if seen[role] == 1 else f"{role}#{seen[role]}", element))
    return assigned
