"""Rule-based page classification.

Deterministic element-count and attribute heuristics — no LLM calls, no network,
no randomness: the same elements and URL always give the same answer.

Each page type has a rule that awards weighted *signals* (weights sum to 1.0), so
a score is explainable: ``classify_page`` returns which signals fired. A type is
chosen when its score reaches ``MIN_SCORE``. If several types score within
``TIE_MARGIN`` of the best, the most specific one wins, by ``PRIORITY`` order —
that is how a login form (which is also, structurally, a small form) is called a
login, and a search-results page (which also repeats rows) is called a search.
Below ``MIN_SCORE`` the page is ``"unknown"``: an honest "no rule matched" beats a
weak guess.

Only *visible* elements are considered, so templates and hidden panels don't vote.
Everything is computed from ``Element`` data (and the URL), never from the
browser directly, so this module works the same on any ``Driver`` backend.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from urllib.parse import urlsplit

from healix.driver.base import Element
from healix.ids import normalize_id
from healix.log import get_logger

logger = get_logger(__name__)

LOGIN = "login"
DASHBOARD = "dashboard"
LIST = "list"
DETAIL = "detail"
FORM = "form"
SEARCH = "search"
CHECKOUT = "checkout"
NAV_SHELL = "nav_shell"
MODAL = "modal"
UNKNOWN = "unknown"

PAGE_TYPES = (LOGIN, DASHBOARD, LIST, DETAIL, FORM, SEARCH, CHECKOUT, NAV_SHELL, MODAL)

# Most specific first; breaks ties between types scoring within TIE_MARGIN of each other.
PRIORITY = (MODAL, LOGIN, CHECKOUT, SEARCH, FORM, LIST, DASHBOARD, DETAIL, NAV_SHELL)

MIN_SCORE = 0.5
TIE_MARGIN = 0.15

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

_LOGIN_URL = re.compile(r"/(log-?in|sign-?in|signin|auth|sso|session)(/|$)")
_LOGIN_TEXT = re.compile(r"\b(sign|log)[ -]?in\b|\bwelcome back\b")
_SIGNUP_URL = re.compile(r"/(register|sign-?up|join|create-?account)(/|$)")
_SIGNUP_TEXT = re.compile(r"\b(sign ?up|register|create (an )?account|join)\b")
_OAUTH_URL = re.compile(
    r"/oauth2?/|/authorize|/saml|/sso|/openid|/adfs/|login\.microsoftonline\.com|"
    r"accounts\.google\.com|okta\.com|auth0\.com|[?&](response_type|client_id|redirect_uri)="
)
_SSO_BUTTON = re.compile(
    r"(sign|log) ?in with|continue with|single sign-?on|\bsso\b|"
    r"use (your )?(google|microsoft|okta|github|apple)"
)

_SUBMIT_TEXT = re.compile(
    r"\b(sign ?in|log ?in|submit|save|send|continue|next|create|register|sign ?up|update|apply|"
    r"pay|place order|search|join)\b"
)

_PAYMENT_FIELD = re.compile(
    r"cc-(number|exp|csc|name|type)|card[-_ ]?(number|num|holder|no)\b|cardnumber|\bcv[vc]\b|"
    r"\bcsc\b|security[-_ ]?code|expir(y|ation)|exp[-_ ]?(date|month|year)|\biban\b|"
    r"routing[-_ ]?number"
)
_STEPPER = re.compile(
    r"stepper|step[-_ ]?(indicator|nav|list|progress|\d)|checkout[-_ ]?(steps?|progress)|wizard|"
    r"progress[-_ ]?(bar|tracker|steps)"
)
_STEP_TEXT = re.compile(r"\bstep \d+ (of|/) \d+\b")
_CHECKOUT_URL = re.compile(r"/(checkout|cart|payment|billing|order|pay|purchase)(/|$)")
_CHECKOUT_TEXT = re.compile(
    r"place order|pay now|complete (purchase|order)|order summary|proceed to (checkout|payment)|"
    r"payment (method|details)"
)

_SEARCH_INPUT = re.compile(r"\b(search|query|keyword|q)\b")
_FILTER = re.compile(r"filter|facet|refine|\bsort\b")
_RESULTS = re.compile(
    r"search[-_ ]?results?|results?[-_ ]?(list|grid|container|area|count|wrapper)|\bresults\b|serp"
)
_SEARCH_URL = re.compile(r"/search\b|[?&](q|query|s|keyword|keywords)=")

_KPI = re.compile(r"\b(kpi|metric|metrics|scorecard|stat|stats|statistic|tile|widget|summary)\b")
_CHART = re.compile(r"chart|graph|\bplot|sparkline|highcharts|recharts|apexcharts|echarts|plotly")
_DASHBOARD_URL = re.compile(r"/(dashboard|overview|analytics|insights|summary|reports?)(/|$)")
_DASHBOARD_TEXT = re.compile(r"\b(dashboard|overview|analytics|insights)\b")

_PAGINATION = re.compile(r"paginat|\bpager\b")
_PAGE_DIRECTION = re.compile(r"^(?:(next|older)|(prev|previous|newer))(?: page)?$")
_NEXT_SYMBOLS = frozenset("›»→")
_PREV_SYMBOLS = frozenset("‹«←")
_PAGE_NUMBER = re.compile(r"^\d{1,3}$")

_KEY_VALUE = re.compile(
    r"\b(field|detail|property|attribute|info|meta)[-_](label|value|name|key)\b"
)
_DETAIL_URL = re.compile(r"/(details?|view|profile)(/|$)")

_NON_FIELD_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
_TEXT_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "figcaption",
        "dd",
        "dt",
        "li",
        "td",
        "th",
    }
)
_PROSE_TAGS = frozenset({"span", "div"})  # counted as text only when they hold real prose
_PROSE_MIN_CHARS = 20
_ROW_TAGS = frozenset({"tr", "li", "article", "div", "section", "dd"})
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})
_CHROME_ROLES = frozenset(
    {"navigation", "banner", "contentinfo", "menu", "menubar", "complementary"}
)
_DIALOG_ROLES = frozenset({"dialog", "alertdialog"})

_MIN_REPEATED_ROWS = 5
_MIN_FORM_INPUT_RATIO = 0.3
_MIN_OVERLAY_Z_INDEX = 100
_MIN_DOMINANT_AREA = 0.4
_MIN_DOMINANT_NODE_SHARE = 0.6
_MIN_MODAL_HEIGHT = 150  # px; thinner containers are banners/toasts, not dialogs


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Classification:
    """The outcome of classifying one page, with the evidence behind it."""

    page_type: str
    scores: dict[str, float]
    signals: dict[str, list[str]] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        """The chosen type's score (0.0 for ``unknown``)."""
        return self.scores.get(self.page_type, 0.0)


class _Score:
    """Accumulates weighted signals for one rule."""

    __slots__ = ("fired", "total")

    def __init__(self) -> None:
        self.total = 0.0
        self.fired: list[str] = []

    def add(self, name: str, weight: float, met: bool) -> None:
        if met:
            self.total += weight
            self.fired.append(name)

    def penalize(self, name: str, weight: float, applies: bool) -> None:
        if applies:
            self.total = max(0.0, self.total - weight)
            self.fired.append(f"-{name}")


# --------------------------------------------------------------------------- #
# Page features
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Node:
    """A visible element, pre-digested for matching."""

    el: Element
    tag: str
    type: str
    role: str
    blob: str  # lowercased id/name/classes/identifying attributes, for keyword matching
    text: str  # lowercased own text (or button value)
    frame: tuple[str, ...]
    sel: str

    @property
    def height(self) -> float:
        box = self.el.computed.get("bounding_box") or {}
        return float(box.get("height", 0))

    @property
    def area(self) -> float:
        box = self.el.computed.get("bounding_box") or {}
        return float(box.get("width", 0)) * float(box.get("height", 0))


def _make_node(el: Element) -> _Node:
    attrs = el.attributes
    node_type = attrs.get("type", "").lower()
    text = el.text_content or ""
    if not text and el.tag == "input" and node_type in _BUTTON_INPUT_TYPES:
        text = attrs.get("value", "")
    blob = " ".join(
        part
        for part in (
            el.id,
            el.name,
            *el.classes,
            attrs.get("placeholder"),
            attrs.get("aria-label"),
            attrs.get("autocomplete"),
            attrs.get("title"),
            attrs.get("data-testid"),
            attrs.get("role"),
            node_type,
        )
        if part
    ).lower()
    return _Node(
        el=el,
        tag=el.tag,
        type=node_type,
        role=attrs.get("role", "").lower(),
        blob=blob,
        text=" ".join(text.lower().split()),
        frame=tuple(el.iframe_path),
        sel=el.css_selector or "",
    )


def _parent_selectors(sel: str) -> Iterator[str]:
    """Yield the selector of each ancestor, nearest first (``a > b > c`` -> ``a > b``, ``a``)."""
    while " " in sel:
        i = sel.rfind(" ")
        sel = sel[: i - 2] if i >= 2 and sel[i - 1] == ">" else sel[:i]
        yield sel


def _is_inside(node: _Node, containers: Sequence[_Node]) -> bool:
    return any(
        node.frame == c.frame and node.sel.startswith(c.sel + " ") for c in containers if c.sel
    )


class _Features:
    """Everything the rules ask about a page, computed once and shared."""

    def __init__(self, elements: Sequence[Element], url: str) -> None:
        self.url = url.lower()
        self.path = urlsplit(url).path.lower()
        self.nodes = [_make_node(e) for e in elements if e.computed.get("visible", True)]

    # -- element groups ------------------------------------------------------ #

    @cached_property
    def fields(self) -> list[_Node]:
        return [
            n
            for n in self.nodes
            if (n.tag == "input" and n.type not in _NON_FIELD_INPUT_TYPES)
            or n.tag in ("textarea", "select")
        ]

    @cached_property
    def editable_fields(self) -> list[_Node]:
        return [
            n
            for n in self.fields
            if not n.el.computed.get("readonly") and n.el.computed.get("enabled", True)
        ]

    @cached_property
    def readonly_fields(self) -> list[_Node]:
        return [n for n in self.fields if n.el.computed.get("readonly")]

    @cached_property
    def passwords(self) -> list[_Node]:
        return [n for n in self.nodes if n.tag == "input" and n.type == "password"]

    @cached_property
    def buttons(self) -> list[_Node]:
        return [
            n
            for n in self.nodes
            if n.tag == "button"
            or n.role == "button"
            or (n.tag == "input" and n.type in _BUTTON_INPUT_TYPES)
        ]

    @cached_property
    def submits(self) -> list[_Node]:
        return [
            n
            for n in self.buttons
            if (n.tag == "input" and n.type in ("submit", "image"))
            or (
                n.tag == "button"
                and (n.type == "submit" or (not n.type and _SUBMIT_TEXT.search(n.text)))
            )
        ]

    @cached_property
    def links(self) -> list[_Node]:
        return [
            n
            for n in self.nodes
            if n.tag == "a" and (n.el.computed.get("href") or n.el.attributes.get("href"))
        ]

    @cached_property
    def nav_links(self) -> list[_Node]:
        """Links that navigate, i.e. not the inline links inside a paragraph of prose."""
        paragraphs = [n for n in self.nodes if n.tag in ("p", "blockquote", "figcaption")]
        return [n for n in self.links if not _is_inside(n, paragraphs)]

    @cached_property
    def interactive(self) -> list[_Node]:
        return [*self.fields, *self.buttons, *self.links]

    @cached_property
    def headings(self) -> list[_Node]:
        return [n for n in self.nodes if n.tag in ("h1", "h2", "h3", "legend") and n.text]

    @cached_property
    def action_texts(self) -> list[str]:
        """Text of headings and submit controls — the places a page states what it *is*."""
        return [n.text for n in (*self.headings, *self.submits) if n.text]

    @cached_property
    def text_blocks(self) -> list[_Node]:
        return [
            n
            for n in self.nodes
            if n.text
            and (n.tag in _TEXT_TAGS or (n.tag in _PROSE_TAGS and len(n.text) >= _PROSE_MIN_CHARS))
        ]

    @cached_property
    def chrome(self) -> list[_Node]:
        """Navigation/header/footer containers, whose repeated children aren't page content."""
        return [n for n in self.nodes if n.tag in _CHROME_TAGS or n.role in _CHROME_ROLES]

    @cached_property
    def navs(self) -> list[_Node]:
        return [n for n in self.nodes if n.tag == "nav" or n.role == "navigation"]

    # -- structure ------------------------------------------------------------ #

    @cached_property
    def descendant_counts(self) -> Counter[tuple[tuple[str, ...], str]]:
        counts: Counter[tuple[tuple[str, ...], str]] = Counter()
        for n in self.nodes:
            for parent in _parent_selectors(n.sel):
                counts[(n.frame, parent)] += 1
        return counts

    @cached_property
    def repeated_rows(self) -> int:
        """Size of the largest run of same-shaped siblings that looks like data rows."""
        groups: Counter[tuple[tuple[str, ...], str, str, tuple[str, ...]]] = Counter()
        for n in self.nodes:
            if n.tag not in _ROW_TAGS or not n.sel or _is_inside(n, self.chrome):
                continue
            # A row has parts; a bare bullet or column div does not. <tr> always qualifies.
            if n.tag != "tr" and self.descendant_counts[(n.frame, n.sel)] < 2:
                continue
            parent = next(_parent_selectors(n.sel), "")
            groups[(n.frame, parent, n.tag, tuple(sorted(n.el.classes)))] += 1
        return max(groups.values(), default=0)

    def outermost(self, matches: Sequence[_Node]) -> list[_Node]:
        """Drop matches nested inside another match (``kpi-card > kpi-value`` counts once)."""
        selectors = {(n.frame, n.sel) for n in matches}
        return [
            n
            for n in matches
            if not any((n.frame, p) in selectors for p in _parent_selectors(n.sel))
        ]

    @cached_property
    def page_area(self) -> float:
        main = [n for n in self.nodes if n.frame == ("main",) and n.tag in ("body", "html")]
        return max((n.area for n in main), default=0.0)

    # -- cues ----------------------------------------------------------------- #

    def any_text(self, pattern: re.Pattern[str]) -> bool:
        return any(pattern.search(t) for t in self.action_texts)


# --------------------------------------------------------------------------- #
# Rules — one per page type. Keep in sync with the README's page-type table.
# --------------------------------------------------------------------------- #


def _login(f: _Features) -> _Score:
    # Route A: a credentials form.
    a = _Score()
    a.add("password_input", 0.45, len(f.passwords) == 1)
    a.add("submit_control", 0.20, bool(f.submits))
    a.add("few_fields", 0.10, 0 < len(f.fields) <= 3)
    a.add("login_cue", 0.25, bool(_LOGIN_URL.search(f.path)) or f.any_text(_LOGIN_TEXT))
    a.penalize("signup_cue", 0.40, bool(_SIGNUP_URL.search(f.path)) or f.any_text(_SIGNUP_TEXT))

    # Route B: an OAuth / SSO redirect page, which may have no password field at all.
    b = _Score()
    b.add("oauth_redirect_url", 0.60, bool(_OAUTH_URL.search(f.url)))
    b.add(
        "sso_buttons",
        0.25,
        any(
            _SSO_BUTTON.search(n.text) or _SSO_BUTTON.search(n.blob) for n in (*f.buttons, *f.links)
        ),
    )
    b.add("few_fields", 0.15, len(f.fields) <= 3)
    return a if a.total >= b.total else b


def _dashboard(f: _Features) -> _Score:
    s = _Score()
    kpis = f.outermost([n for n in f.nodes if _KPI.search(n.blob)])
    charts = f.outermost([n for n in f.nodes if n.tag == "canvas" or _CHART.search(n.blob)])
    s.add("kpi_widgets", 0.30, len(kpis) >= 3)
    s.add("chart", 0.15, len(charts) == 1)
    s.add("charts", 0.35, len(charts) >= 2)
    s.add("minimal_inputs", 0.15, len(f.fields) <= 3)
    s.add(
        "dashboard_cue",
        0.15,
        bool(_DASHBOARD_URL.search(f.path))
        or any(_DASHBOARD_TEXT.search(n.text) for n in f.headings),
    )
    return s


def _list(f: _Features) -> _Score:
    s = _Score()
    rows = f.repeated_rows
    s.add("repeating_rows", 0.45, rows >= _MIN_REPEATED_ROWS)
    s.add("some_repeating_rows", 0.25, 3 <= rows < _MIN_REPEATED_ROWS)
    s.add("pagination", 0.35, _has_pagination(f))
    s.add("table_header", 0.10, sum(n.tag == "th" for n in f.nodes) >= 2)
    s.add("few_fields", 0.10, len(f.fields) <= 3)
    return s


def _has_pagination(f: _Features) -> bool:
    if any(_PAGINATION.search(n.blob) for n in f.nodes):
        return True
    if any(n.el.attributes.get("rel", "").lower() in ("next", "prev") for n in f.links):
        return True
    controls = [
        n.text or n.el.attributes.get("aria-label", "").lower() for n in (*f.links, *f.buttons)
    ]
    directions = {_page_direction(t) for t in controls}
    numbered = sum(bool(_PAGE_NUMBER.match(t)) for t in controls)
    return "next" in directions and ("prev" in directions or numbered >= 2)


def _page_direction(text: str) -> str | None:
    """``"next"``/``"prev"`` for a pager control's label, else ``None``.

    Handles ``Next``, ``Next »``, ``« Previous``, ``›``, ``Next page`` and the like.
    """
    stripped = text.translate(str.maketrans("", "", "›»→‹«←")).strip()
    if not stripped:
        if any(c in _NEXT_SYMBOLS for c in text):
            return "next"
        if any(c in _PREV_SYMBOLS for c in text):
            return "prev"
        return None
    match = _PAGE_DIRECTION.match(stripped)
    if match is None:
        return None
    return "next" if match.group(1) else "prev"


def _detail(f: _Features) -> _Score:
    s = _Score()
    h1_count = sum(n.tag == "h1" for n in f.nodes)
    dt_count = sum(n.tag == "dt" for n in f.nodes)
    kv_markers = sum(bool(_KEY_VALUE.search(n.blob)) for n in f.nodes)
    entity_url = any(normalize_id(seg) != seg for seg in f.path.split("/")[2:] if seg) or bool(
        _DETAIL_URL.search(f.path)
    )
    s.add("single_heading", 0.15, h1_count == 1)
    s.add("read_only", 0.10, len(f.editable_fields) <= 1)
    s.add("text_content", 0.10, len(f.text_blocks) >= 3)
    s.add("not_repeating", 0.10, f.repeated_rows < _MIN_REPEATED_ROWS)
    s.add("key_value_pairs", 0.30, dt_count >= 2 or kv_markers >= 2 or len(f.readonly_fields) >= 2)
    s.add("entity_url", 0.25, entity_url)
    return s


def _form(f: _Features) -> _Score:
    s = _Score()
    fields = len(f.fields)
    ratio = fields / (fields + len(f.text_blocks)) if fields else 0.0
    credentials_only = bool(f.passwords) and fields <= 3
    s.add("many_fields", 0.25, fields >= 3)
    s.add("high_input_to_text_ratio", 0.25, ratio >= _MIN_FORM_INPUT_RATIO)
    s.add("submit_control", 0.20, bool(f.submits))
    s.add("form_element", 0.10, any(n.tag == "form" for n in f.nodes))
    s.add("not_credentials_only", 0.20, not credentials_only)
    # The defining trait of a form is that inputs dominate the text. A content page that merely
    # has a few controls (a settings menu, a newsletter box) must not clear the bar without it.
    s.penalize("low_input_to_text_ratio", 0.30, ratio < _MIN_FORM_INPUT_RATIO)
    return s


def _search(f: _Features) -> _Score:
    s = _Score()
    search_inputs = [
        n
        for n in f.fields
        if n.tag in ("input", "textarea")
        and (n.type == "search" or n.role == "searchbox" or _SEARCH_INPUT.search(n.blob))
    ]
    filter_controls = sum(
        1 for n in f.fields if n.tag == "select" or n.type in ("checkbox", "radio", "range")
    ) + int(any(_FILTER.search(n.blob) for n in f.nodes))
    s.add("search_input", 0.30, bool(search_inputs))
    s.add("filter_controls", 0.25, filter_controls >= 2)
    s.add("results_area", 0.30, any(_RESULTS.search(n.blob) for n in f.nodes))
    s.add("search_url", 0.15, bool(_SEARCH_URL.search(f.url)))
    # A header search box plus some controls exists on almost every site; without a results
    # area (or a search URL) there is nothing to say this page is *showing* search results.
    s.penalize(
        "no_results_evidence",
        0.30,
        not (any(_RESULTS.search(n.blob) for n in f.nodes) or _SEARCH_URL.search(f.url)),
    )
    return s


def _checkout(f: _Features) -> _Score:
    s = _Score()
    payment_fields = sum(bool(_PAYMENT_FIELD.search(n.blob)) for n in f.fields)
    stepper = any(
        _STEPPER.search(n.blob) or n.el.attributes.get("aria-current") == "step" for n in f.nodes
    )
    step_text = any(_STEP_TEXT.search(n.text) for n in f.nodes)
    s.add("payment_fields", 0.50, payment_fields >= 2)
    s.add("some_payment_field", 0.25, payment_fields == 1)
    s.add("multi_step_indicator", 0.25, stepper or step_text)
    s.add(
        "checkout_cue",
        0.15,
        bool(_CHECKOUT_URL.search(f.path)) or any(_CHECKOUT_TEXT.search(n.text) for n in f.nodes),
    )
    s.add("submit_control", 0.10, bool(f.submits))
    return s


def _nav_shell(f: _Features) -> _Score:
    s = _Score()
    links = len(f.nav_links)
    non_prose_interactive = len(f.fields) + len(f.buttons) + links
    s.add("mostly_links", 0.40, links >= 5 and links / max(non_prose_interactive, 1) >= 0.8)
    s.add("navigation_landmark", 0.20, bool(f.navs))
    s.add("low_input_density", 0.20, len(f.fields) <= 1)
    s.add("little_prose", 0.20, links >= 2 * max(len(f.text_blocks), 1))
    # Repeated rows outside nav chrome are content (a product grid), not navigation.
    s.penalize("repeating_content_rows", 0.30, f.repeated_rows >= _MIN_REPEATED_ROWS)
    return s


def _modal(f: _Features) -> _Score:
    s = _Score()
    containers = _modal_containers(f)
    s.add("dialog_or_overlay", 0.40, bool(containers))
    s.add("dominates_page", 0.60, _dominates_page(f, containers))
    return s


def _dominates_page(f: _Features, containers: list[_Node]) -> bool:
    """Whether a dialog/overlay is what the page *is*, not just something laid over it.

    A cookie banner or toast is a dialog too, but a thin one; it must not turn every
    page it sits on into a "modal". So a container has to be tall enough to be a real
    dialog, and then either cover most of the page or hold most of its content.
    """
    tall = [c for c in containers if c.height >= _MIN_MODAL_HEIGHT]
    if not tall or f.page_area <= 0:
        return False
    if max(c.area for c in tall) / f.page_area >= _MIN_DOMINANT_AREA:
        return True
    content = [n for n in f.nodes if n.tag not in ("html", "body")]
    tall_ids = {id(c) for c in tall}
    held = sum(1 for n in content if id(n) in tall_ids or _is_inside(n, tall))
    return held / max(len(content), 1) >= _MIN_DOMINANT_NODE_SHARE


def _modal_containers(f: _Features) -> list[_Node]:
    """Dialogs, plus large high-z-index fixed/absolute overlays."""
    found = []
    for n in f.nodes:
        attrs = n.el.attributes
        is_dialog = (
            n.role in _DIALOG_ROLES
            or attrs.get("aria-modal", "").lower() == "true"
            or (n.tag == "dialog" and "open" in attrs)
        )
        z_index = n.el.computed.get("z_index")
        is_overlay = (
            n.tag not in ("html", "body")
            and n.el.computed.get("position") in ("fixed", "absolute")
            and isinstance(z_index, int)
            and z_index >= _MIN_OVERLAY_Z_INDEX
            and f.page_area > 0
            and n.frame == ("main",)
            and n.area / f.page_area >= _MIN_DOMINANT_AREA
        )
        if is_dialog or is_overlay:
            found.append(n)
    return found


RULES: dict[str, Callable[[_Features], _Score]] = {
    LOGIN: _login,
    DASHBOARD: _dashboard,
    LIST: _list,
    DETAIL: _detail,
    FORM: _form,
    SEARCH: _search,
    CHECKOUT: _checkout,
    NAV_SHELL: _nav_shell,
    MODAL: _modal,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def classify_page(elements: Sequence[Element], url: str = "") -> Classification:
    """Classify a page from its elements (and URL), with the evidence for the decision."""
    features = _Features(elements, url)
    results = {page_type: RULES[page_type](features) for page_type in PAGE_TYPES}
    scores = {t: round(min(r.total, 1.0), 2) for t, r in results.items()}
    signals = {t: r.fired for t, r in results.items() if r.fired}

    best = max(scores.values())
    if best < MIN_SCORE:
        page_type = UNKNOWN
    else:
        page_type = next(
            t for t in PRIORITY if scores[t] >= MIN_SCORE and scores[t] >= best - TIE_MARGIN - 1e-9
        )
    logger.debug("classified page", url=url, page_type=page_type, scores=scores)
    return Classification(page_type=page_type, scores=scores, signals=signals)


def classify(elements: Sequence[Element], url: str = "") -> str:
    """The page type: one of ``PAGE_TYPES``, or ``"unknown"`` if no rule is confident enough."""
    return classify_page(elements, url).page_type
