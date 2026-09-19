"""Backend-neutral driver interface and the data models it speaks in.

Everything else in Healix (extraction, healing, classification, generation) is
written once against ``Driver``. Only adapter modules may import ``playwright``
or ``selenium``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from healix.healing.fingerprint import Fingerprint, LocatorSpec
from healix.ids import normalize_id

MAIN_FRAME = "main"


class ElementNotFoundError(LookupError):
    """No element (or frame) matched the requested fingerprint/path."""


@dataclass
class Element:
    """One DOM element captured with maximum raw detail.

    Mirrors the raw element extraction schema. ``xpath`` is relative to the
    element's nearest root (the document, or its shadow root); ``css_selector``
    is complete and pierces shadow roots. ``shadow_path`` lists the css selector
    of each shadow host (outermost first) and is empty for light-DOM elements.
    """

    tag: str
    id: str | None = None
    id_normalized: str | None = None
    name: str | None = None
    classes: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    text_content: str | None = None
    computed: dict[str, Any] = field(default_factory=dict)
    xpath: str | None = None
    css_selector: str | None = None
    iframe_path: list[str] = field(default_factory=list)
    platform_signal: Any = None
    dom_context: dict[str, Any] = field(default_factory=dict)
    shadow_path: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, iframe_path: list[str] | None = None) -> Element:
        raw_id = raw.get("id") or None
        return cls(
            tag=raw["tag"],
            id=raw_id,
            id_normalized=normalize_id(raw_id),
            name=raw.get("name"),
            classes=list(raw.get("classes") or []),
            attributes=dict(raw.get("attributes") or {}),
            text_content=raw.get("text_content"),
            computed=dict(raw.get("computed") or {}),
            xpath=raw.get("xpath"),
            css_selector=raw.get("css_selector"),
            iframe_path=list(
                iframe_path if iframe_path is not None else raw.get("iframe_path") or []
            ),
            platform_signal=raw.get("platform_signal"),
            dom_context=dict(raw.get("dom_context") or {}),
            shadow_path=list(raw.get("shadow_path") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Frame:
    """A frame in the page's frame tree.

    ``path`` is the ``iframe_path`` of every element inside it, starting at
    ``"main"``. ``handle`` is the adapter's native frame object.
    """

    path: list[str]
    url: str
    name: str | None = None
    same_origin: bool = True
    handle: Any = field(default=None, repr=False, compare=False)


Target = Element | Fingerprint


class Driver(ABC):
    """Browser automation surface used by all of Healix."""

    def start(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Acquire browser resources. Adapters that lazily launch override this."""

    def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any browser resources this driver owns."""

    def __enter__(self) -> Driver:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    @abstractmethod
    def current_url(self) -> str:
        """URL of the main frame, after any redirects."""

    @abstractmethod
    def navigate(self, url: str) -> None: ...

    def settle(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Wait, best-effort and bounded, for in-flight navigation and network activity to quiet.

        Call it after an action that may navigate (a click, a form submit) before reading the
        page. Drivers with nothing to wait for leave the default no-op.
        """

    @abstractmethod
    def find(self, fingerprint: Fingerprint) -> Element:
        """Resolve ``fingerprint`` via its primary locators.

        Raises ``ElementNotFoundError`` when none resolve to exactly one element.
        Score-based healing on top of this is planned.
        """

    def locate(self, spec: LocatorSpec, fingerprint: Fingerprint) -> Element | None:
        """The one element that a single locator strategy matches, or ``None``.

        ``None`` means the locator matched nothing or was ambiguous (several elements). This is
        what lets the healer see *which* strategy resolved a fingerprint; ``find`` is
        ``locate`` tried down the priority list. Backends that support healing implement it.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot locate by a single strategy")

    @abstractmethod
    def click(self, target: Target) -> None: ...

    @abstractmethod
    def write(self, text: str, into: Target) -> None: ...

    @abstractmethod
    def get_elements(self, *, iframe_traversal: bool = True) -> list[Element]:
        """Every element on the page, including those in open shadow roots.

        With ``iframe_traversal`` (the default) elements of every same-origin frame
        are merged in, each tagged with its ``iframe_path``; without it only the main
        frame is read.
        """

    @abstractmethod
    def get_frames(self) -> list[Frame]:
        """The frame tree, flattened depth-first, main frame first."""

    @abstractmethod
    def screenshot(self) -> bytes: ...
