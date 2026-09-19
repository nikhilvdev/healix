"""Generic iframe traversal and shadow DOM piercing.

Nothing here imports a browser library or knows about any vendor:

* ``COLLECT_ALL_JS`` / ``DESCRIBE_ONE_JS`` are in-page scripts that walk the DOM,
  descending into every *open* shadow root, and describe elements in the raw
  extraction schema. Any adapter can run them (Playwright ``evaluate``,
  Selenium ``execute_script``).
* ``walk_frames`` builds the same-origin frame tree from adapter callbacks.
* ``collect_elements`` runs the collector in each same-origin frame and merges
  the results, tagging every element with its ``iframe_path``.

Closed shadow roots and cross-origin frames are not reachable and are skipped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from healix.driver.base import MAIN_FRAME, Element, Frame
from healix.log import get_logger

logger = get_logger(__name__)

H = TypeVar("H")

# --------------------------------------------------------------------------- #
# In-page scripts
# --------------------------------------------------------------------------- #

_JS_HELPERS = r"""
const SKIP = new Set(['script', 'style', 'noscript', 'template', 'meta', 'link', 'head', 'title', 'base']);
const XHTML = 'http://www.w3.org/1999/xhtml';

const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c);
const isShadowRoot = (root) => root.nodeType === 11 && !!root.host;

function uniqueIn(root, selector) {
  try { return root.querySelectorAll(selector).length === 1; } catch (e) { return false; }
}

// Selector unique within `root` (a Document or ShadowRoot).
function localSelector(el, root) {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1) {
    if (node.id) {
      const byId = '#' + cssEscape(node.id);
      if (uniqueIn(root, byId)) { parts.unshift(byId); break; }
    }
    let segment = cssEscape(node.localName);
    const parent = node.parentNode;
    if (parent && parent.children) {
      const same = Array.from(parent.children).filter((c) => c.localName === node.localName);
      if (same.length > 1) segment += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
    }
    parts.unshift(segment);
    if (!parent || parent === root) break;
    node = parent;
  }
  return parts.join(' > ');
}

// Selectors from the outermost document down to `el`, one entry per shadow boundary crossed.
function selectorChain(el) {
  const chain = [];
  let node = el;
  for (;;) {
    const root = node.getRootNode();
    chain.unshift(localSelector(node, root));
    if (isShadowRoot(root)) node = root.host; else break;
  }
  return chain;
}

function xpathQuote(s) {
  if (s.indexOf('"') === -1) return '"' + s + '"';
  if (s.indexOf("'") === -1) return "'" + s + "'";
  return null;
}

// XPath relative to the element's nearest root (document or shadow root).
function xpathOf(el, root) {
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1) {
    if (node.id && root.nodeType === 9) {
      const q = xpathQuote(node.id);
      if (q && uniqueIn(root, '#' + cssEscape(node.id))) { parts.unshift('/*[@id=' + q + ']'); return '/' + parts.join('/'); }
    }
    const name = node.localName;
    const test = node.namespaceURI === XHTML ? name : "*[local-name()='" + name + "']";
    let index = 1;
    for (let s = node.previousElementSibling; s; s = s.previousElementSibling) if (s.localName === name) index++;
    parts.unshift(test + '[' + index + ']');
    const parent = node.parentNode;
    if (!parent || parent === root) break;
    node = parent;
  }
  return '/' + parts.join('/');
}

function ownText(el) {
  let text = '';
  for (const n of el.childNodes) if (n.nodeType === 3) text += n.nodeValue;
  text = clean(text);
  return text || null;
}

function nearbyLabelText(el, root) {
  if (el.labels && el.labels.length) {
    const t = clean(Array.from(el.labels).map((l) => l.textContent).join(' '));
    if (t) return t.slice(0, 200);
  }
  const labelledBy = el.getAttribute('aria-labelledby');
  if (labelledBy && root.getElementById) {
    const t = clean(labelledBy.split(/\s+/).map((id) => { const n = root.getElementById(id); return n ? n.textContent : ''; }).join(' '));
    if (t) return t.slice(0, 200);
  }
  const prev = el.previousElementSibling;
  if (prev && /^(label|span|legend|strong|b|p|h[1-6])$/.test(prev.localName)) {
    const t = clean(prev.textContent);
    if (t && t.length <= 80) return t;
  }
  return null;
}

function computedState(el, root, rect) {
  const style = getComputedStyle(el);
  const visible = style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse'
    && rect.width > 0 && rect.height > 0;
  const isControl = /^(input|textarea|select|button|option|optgroup|fieldset)$/.test(el.localName);
  const isTextField = el.localName === 'input' || el.localName === 'textarea';
  return {
    visible: visible,
    enabled: !(isControl && el.matches(':disabled')) && el.getAttribute('aria-disabled') !== 'true',
    bounding_box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    checked: (el.localName === 'input' && (el.type === 'checkbox' || el.type === 'radio')) ? el.checked : null,
    selected: el.localName === 'option' ? el.selected : null,
    readonly: isTextField ? el.readOnly : null,
    required: (isTextField || el.localName === 'select') ? el.required : null,
    focused: root.activeElement === el,
    position: style.position,
    z_index: Number.isFinite(parseInt(style.zIndex, 10)) ? parseInt(style.zIndex, 10) : null,
    // Absolute URL as the browser resolves it (honours <base href>); null for non-links.
    href: ((el.localName === 'a' || el.localName === 'area') && typeof el.href === 'string' && el.href) ? el.href : null,
  };
}

// Raw extraction schema for one element (iframe_path and id_normalized are added Python-side).
function describe(el) {
  const root = el.getRootNode();
  const chain = selectorChain(el);
  const rect = el.getBoundingClientRect();
  const attributes = {};
  for (const a of el.attributes) if (a.name !== 'id' && a.name !== 'class' && a.name !== 'name') attributes[a.name] = a.value;
  const parentNode = el.parentNode;
  const parentEl = el.parentElement || (parentNode && parentNode.host) || null;
  const siblings = parentNode && parentNode.children ? Array.from(parentNode.children) : [el];
  return {
    tag: el.localName,
    id: el.getAttribute('id') || null,
    name: el.getAttribute('name'),
    classes: Array.from(el.classList),
    attributes: attributes,
    text_content: ownText(el),
    computed: computedState(el, root, rect),
    xpath: xpathOf(el, root),
    css_selector: chain.join(' '),
    shadow_path: chain.slice(0, -1),
    platform_signal: null,
    dom_context: {
      parent_tag: parentEl ? parentEl.localName : null,
      parent_id: parentEl ? (parentEl.getAttribute('id') || null) : null,
      sibling_index: siblings.indexOf(el),
      nearby_label_text: nearbyLabelText(el, root),
    },
  };
}

// Depth-first, document order; a shadow host's shadow tree comes before its light children.
function walk(node, out) {
  for (const el of node.children) {
    if (SKIP.has(el.localName)) continue;
    out.push(describe(el));
    if (el.shadowRoot) walk(el.shadowRoot, out);
    walk(el, out);
  }
}
"""

COLLECT_ALL_JS = "() => {" + _JS_HELPERS + "const out = []; walk(document, out); return out; }"
"""Function expression: describes every element in the current frame's document."""

DESCRIBE_ONE_JS = "(el) => {" + _JS_HELPERS + "return describe(el); }"
"""Function expression taking one DOM element and describing it."""


# --------------------------------------------------------------------------- #
# Frame tree
# --------------------------------------------------------------------------- #

_INHERITING_URLS = ("", "about:blank", "about:srcdoc")


def origin_of(url: str) -> str | None:
    """Origin of ``url``; ``None`` if the frame inherits its parent's origin.

    Opaque origins (``data:`` etc.) get a unique per-URL string so they never
    equal a real origin.
    """
    if url in _INHERITING_URLS:
        return None
    parts = urlsplit(url)
    if parts.scheme in ("http", "https", "ws", "wss") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}".lower()
    if parts.scheme == "file":
        return "file://"
    return f"opaque:{url}"


def walk_frames(
    root: H,
    *,
    children_of: Callable[[H], Iterable[H]],
    url_of: Callable[[H], str],
    name_of: Callable[[H], str | None],
) -> list[Frame]:
    """Flatten a frame tree depth-first, main frame first.

    Adapter callbacks supply the native tree. Every frame is reported, but only
    frames reachable through an unbroken chain of same-origin frames have
    ``same_origin=True`` — everything beneath a cross-origin frame is flagged
    cross-origin too. Sibling labels are made unique (``name``, ``name#2``, ...)
    so ``path`` identifies a frame unambiguously.
    """
    root_url = url_of(root)
    root_origin = origin_of(root_url)
    frames = [Frame(path=[MAIN_FRAME], url=root_url, name=None, same_origin=True, handle=root)]

    def visit(
        parent: H, parent_path: list[str], parent_same_origin: bool, parent_origin: str | None
    ) -> None:
        used: set[str] = set()
        for index, child in enumerate(children_of(parent)):
            name = name_of(child) or None
            label = _unique_label(name or f"iframe[{index}]", used)
            url = url_of(child)
            origin = origin_of(url) or parent_origin
            same_origin = parent_same_origin and origin == root_origin
            path = [*parent_path, label]
            frames.append(
                Frame(path=path, url=url, name=name, same_origin=same_origin, handle=child)
            )
            visit(child, path, same_origin, origin)

    visit(root, [MAIN_FRAME], True, root_origin)
    return frames


def _unique_label(label: str, used: set[str]) -> str:
    candidate, n = label, 1
    while candidate in used:
        n += 1
        candidate = f"{label}#{n}"
    used.add(candidate)
    return candidate


def collect_elements(
    frames: Iterable[Frame], run_collector: Callable[[Frame], list[dict[str, Any]]]
) -> list[Element]:
    """Run ``run_collector`` in every same-origin frame and merge the results.

    A frame that fails mid-collection (e.g. it navigated or detached) is logged
    and skipped rather than failing the whole page.
    """
    elements: list[Element] = []
    for frame in frames:
        if not frame.same_origin:
            logger.debug("skipping cross-origin frame", frame=frame.path, url=frame.url)
            continue
        try:
            raw_elements = run_collector(frame)
        except Exception as exc:
            logger.warn(
                "could not collect elements from frame",
                frame=frame.path,
                url=frame.url,
                error=str(exc),
            )
            continue
        elements.extend(Element.from_dict(raw, iframe_path=frame.path) for raw in raw_elements)
    return elements
