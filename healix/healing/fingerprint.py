"""Element fingerprints.

A ``Fingerprint`` is the stored identity of one element: everything extraction captured
about it, keyed by ``(page_url, element_role)``. It yields the ordered list of primary
locators used to find the element again (``locators``). When those fail, the healer
scores candidates against it (``healix.healing.scorer``) and, on a confident match,
replaces it with the healed element's fingerprint.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
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
    shadow_path: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """``(page_url, element_role)`` — how the store identifies a fingerprint."""
        return (self.page_url, self.element_role)

    @property
    def element_key(self) -> str:
        """The key as one string, as used in ``element_healed`` events."""
        return f"{self.page_url}#{self.element_role}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Fingerprint:
        """Rebuild from ``to_dict`` output; unknown keys are ignored so old stores keep loading."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

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
            shadow_path=list(element.shadow_path),
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


def describe_locator(fingerprint: Fingerprint) -> str:
    """The highest-priority locator of ``fingerprint`` as ``"<strategy>=<value>"`` (or ``""``)."""
    first = next(fingerprint.locators(), None)
    return f"{first.strategy}={first.value}" if first else ""
