"""Page fixtures and a page-simulating driver for the healing tests (no browser)."""

from __future__ import annotations

import re

from healix.driver.base import Driver, Element
from healix.healing.fingerprint import Fingerprint, LocatorSpec
from healix.ids import normalize_id
from tests.elements import el

URL = "https://shop.example.com/login"


def username(**overrides):
    base = {
        "id": "user-4471",
        "name": "username",
        "classes": ["form-control", "input-lg"],
        "type": "text",
        "data_testid": "login-username",
        "aria_label": "Username",
        "sel": "html > body > div > form > input:nth-of-type(1)",
        "xpath": "/html[1]/body[1]/div[1]/form[1]/input[1]",
        "dom": {
            "parent_tag": "form",
            "parent_id": "login-form",
            "sibling_index": 1,
            "nearby_label_text": "Username",
        },
    }
    base.update(overrides)
    return el("input", **base)


def password(**overrides):
    base = {
        "id": "pw-1",
        "name": "password",
        "classes": ["form-control", "input-lg"],
        "type": "password",
        "data_testid": "login-password",
        "aria_label": "Password",
        "sel": "html > body > div > form > input:nth-of-type(2)",
        "xpath": "/html[1]/body[1]/div[1]/form[1]/input[2]",
        "dom": {
            "parent_tag": "form",
            "parent_id": "login-form",
            "sibling_index": 3,
            "nearby_label_text": "Password",
        },
    }
    base.update(overrides)
    return el("input", **base)


def submit(**overrides):
    base = {
        "id": "submit-btn",
        "classes": ["btn", "btn-primary"],
        "type": "submit",
        "text": "Sign in",
        "sel": "html > body > div > form > button",
        "xpath": "/html[1]/body[1]/div[1]/form[1]/button[1]",
        "dom": {"parent_tag": "form", "parent_id": "login-form", "sibling_index": 5},
    }
    base.update(overrides)
    return el("button", **base)


def v1():
    """The baseline login page."""
    return [username(), password(), submit()]


def fingerprint_of(element: Element, role: str, url: str = URL) -> Fingerprint:
    return Fingerprint.from_element(element, url, role)


class PageDriver(Driver):
    """A driver over a fixed list of elements. ``locate`` mimics the real locator semantics:
    a locator matches only if it selects exactly one element."""

    def __init__(self, elements):
        self.elements = list(elements)
        self.locates: list[str] = []
        self.reads = 0
        self.navigations: list[str] = []
        self.clicked: list[Element] = []
        self.written: list[tuple[str, Element]] = []
        self.started = self.closed = 0

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    @property
    def current_url(self):
        return URL

    def get_elements(self, *, iframe_traversal=True):
        self.reads += 1
        return list(self.elements)

    def locate(self, spec: LocatorSpec, fingerprint: Fingerprint):
        self.locates.append(spec.strategy)
        matches = self._matches(spec, fingerprint)
        return matches[0] if len(matches) == 1 else None

    def _matches(self, spec, fingerprint):
        if spec.kind == "css":
            attr = re.fullmatch(r'\[([\w-]+)="(.*)"\]', spec.value)
            if attr:
                key, value = attr.groups()
                return [e for e in self.elements if _attribute(e, key) == value]
            return [e for e in self.elements if e.css_selector == spec.value]
        if spec.kind == "xpath":
            return [e for e in self.elements if e.xpath == spec.value]
        if spec.kind == "id_pattern":
            return [e for e in self.elements if e.id and normalize_id(e.id) == spec.value]
        if spec.kind == "text":
            return [
                e
                for e in self.elements
                if e.text_content == spec.value
                and (not fingerprint.tag or e.tag == fingerprint.tag)
            ]
        return []

    def navigate(self, url):
        self.navigations.append(url)

    def find(self, fingerprint):
        raise NotImplementedError

    def click(self, target):
        self.clicked.append(target)

    def write(self, text, into):
        self.written.append((text, into))

    def get_frames(self):
        return []

    def screenshot(self):
        return b""


def _attribute(element: Element, key: str):
    if key == "id":
        return element.id
    if key == "name":
        return element.name
    return element.attributes.get(key)
