"""Element fingerprints.

For now this only holds enough of a fingerprint for ``Driver.find`` to resolve one:
the stored identity of an element plus the ordered list of primary locators
derived from it. The self-healing work will extend this module (weights, history,
persistence).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from healix.driver.base import Element

# Stable, test-oriented attributes tried first, in order.
STABLE_ATTRIBUTES = (
    "data-testid",
    "data-test-id",
    "data-test",
    "data-qa",
    "data-cy",
    "data-automation-id",
)


@dataclass(frozen=True)
class LocatorSpec:
    """One backend-neutral way to locate an element.

    ``kind`` is one of ``css``, ``xpath``, ``id_pattern`` (match by normalized
    id) or ``text`` (exact own-text match). Adapters translate a spec into their
    native locator.
    """

    strategy: str
    kind: str
    value: str


@dataclass
class Fingerprint:
    page_url: str
    element_role: str
    tag: str | None = None
    id: str | None = None
    id_normalized: str | None = None
    name: str | None = None
    classes: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    text_content: str | None = None
    xpath: str | None = None
    css_selector: str | None = None
    iframe_path: list[str] = field(default_factory=list)
    platform_signal: Any = None
    dom_context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_element(
        cls, element: Element, page_url: str, element_role: str | None = None
    ) -> Fingerprint:
        return cls(
            page_url=page_url,
            element_role=element_role or element.attributes.get("role") or element.tag,
            tag=element.tag,
            id=element.id,
            id_normalized=element.id_normalized,
            name=element.name,
            classes=list(element.classes),
            attributes=dict(element.attributes),
            text_content=element.text_content,
            xpath=element.xpath,
            css_selector=element.css_selector,
            iframe_path=list(element.iframe_path),
            platform_signal=element.platform_signal,
            dom_context=dict(element.dom_context),
        )

    def locators(self) -> Iterator[LocatorSpec]:
        """Primary locators in priority order.

        stable attrs -> id -> name -> aria-label -> css -> xpath ->
        normalized id -> text.
        """
        for attr in STABLE_ATTRIBUTES:
            value = self.attributes.get(attr)
            if value:
                yield LocatorSpec(f"stable_attr:{attr}", "css", _attr_selector(attr, value))
        if self.id:
            yield LocatorSpec("id", "css", _attr_selector("id", self.id))
        if self.name:
            yield LocatorSpec("name", "css", _attr_selector("name", self.name))
        aria_label = self.attributes.get("aria-label")
        if aria_label:
            yield LocatorSpec("aria_label", "css", _attr_selector("aria-label", aria_label))
        if self.css_selector:
            yield LocatorSpec("css", "css", self.css_selector)
        if self.xpath:
            yield LocatorSpec("xpath", "xpath", self.xpath)
        if self.id_normalized and self.id_normalized != self.id:
            yield LocatorSpec("normalized_id", "id_pattern", self.id_normalized)
        if self.text_content:
            yield LocatorSpec("text", "text", self.text_content)


def _attr_selector(attribute: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{attribute}="{escaped}"]'
