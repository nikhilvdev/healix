"""Generic iframe traversal and shadow DOM piercing.

Nothing here imports a browser library or knows about any vendor:

* ``COLLECT_ALL_JS`` / ``DESCRIBE_ONE_JS`` are in-page scripts that walk the DOM,
  descending into every *open* shadow root, and describe elements in the raw
  extraction schema. Any adapter can run them (Playwright ``evaluate``,
  Selenium ``execute_script``). ``build_collect_js`` / ``build_describe_js`` make the same
  scripts with platform adapters' hooks added (``healix.platform_adapters``).
* ``QUERY_JS`` / ``TEXT_JS`` / ``IDS_JS`` / ``CHILD_FRAMES_JS`` are in-page queries that see
  into open shadow roots, for backends whose native selectors do not (Selenium).
* ``walk_frames`` builds the same-origin frame tree from adapter callbacks.
* ``collect_elements`` runs the collector in each same-origin frame and merges
  the results, tagging every element with its ``iframe_path``.

Closed shadow roots and cross-origin frames are not reachable and are skipped; the frames are
reported (``SkippedFrame``) so a page's output says what it is missing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar
from urllib.parse import urlsplit

from healix.driver.base import (
    CROSS_ORIGIN,
    INSIDE_CROSS_ORIGIN,
    MAIN_FRAME,
    UNREADABLE,
    Element,
    Frame,
    SkippedFrame,
)
from healix.log import get_logger
from healix.platform_adapters.base import PlatformAdapter

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

// Ancestor tag names from the root (document or shadow root) down to `el`. Unlike an xpath or css
// selector it is not shortened by an id anchor, so it still describes where the element sits.
function tagPath(el, root) {
  const path = [];
  let node = el;
  while (node && node.nodeType === 1) {
    path.unshift(node.localName);
    const parent = node.parentNode;
    if (!parent || parent === root) break;
    node = parent;
  }
  return path;
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

// Platform adapters (healix.platform_adapters) register here, only in frames where their platform
// is detected. A hook that throws is ignored: extraction never depends on an adapter.
const platformHooks = [];
function platformSignal(el) {
  for (const hook of platformHooks) {
    try { const signal = hook(el); if (signal) return signal; } catch (e) { /* ignored */ }
  }
  return null;
}
/*PLATFORM_HOOKS*/

// Raw extraction schema for one element (iframe_path and id_normalized are added Python-side).
function describe(el) {
  const root = el.getRootNode();
  const chain = selectorChain(el);
  const rect = el.getBoundingClientRect();
  const attributes = {};
  const isPassword = el.localName === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'password';
  for (const a of el.attributes) {
    if (a.name === 'id' || a.name === 'class' || a.name === 'name') continue;
    // A password field's markup value is a secret; record that it existed, never what it was.
    attributes[a.name] = (isPassword && a.name === 'value') ? '[redacted]' : a.value;
  }
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
    platform_signal: platformSignal(el),
    dom_context: {
      parent_tag: parentEl ? parentEl.localName : null,
      parent_id: parentEl ? (parentEl.getAttribute('id') || null) : null,
      sibling_index: siblings.indexOf(el),
      nearby_label_text: nearbyLabelText(el, root),
      tag_path: tagPath(el, root),
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


def _with_hooks(adapters: Sequence[PlatformAdapter]) -> str:
    hooks = "".join(_hook_js(adapter) for adapter in adapters)
    return _JS_HELPERS.replace("/*PLATFORM_HOOKS*/", hooks)


def _hook_js(adapter: PlatformAdapter) -> str:
    """Register ``adapter``'s signal function, but only where its detection expression is true."""
    return (
        f"try {{ if ({adapter.detect_js}) {{ const signalFor = {adapter.signal_js}; "
        "platformHooks.push((el) => { const signal = signalFor(el); "
        f"return signal ? Object.assign({{ platform: {json.dumps(adapter.name)} }}, signal) : null; }}); }} "
        "} catch (e) { /* ignored */ }\n"
    )


def build_collect_js(adapters: Sequence[PlatformAdapter] = ()) -> str:
    """Function expression: describes every element in the current frame's document."""
    return "() => {" + _with_hooks(adapters) + "const out = []; walk(document, out); return out; }"


def build_describe_js(adapters: Sequence[PlatformAdapter] = ()) -> str:
    """Function expression taking one DOM element and describing it."""
    return "(el) => {" + _with_hooks(adapters) + "return describe(el); }"


COLLECT_ALL_JS = build_collect_js()
"""Describes every element in the current frame's document (no platform adapters)."""

DESCRIBE_ONE_JS = build_describe_js()
"""Takes one DOM element and describes it (no platform adapters)."""


# --------------------------------------------------------------------------- #
# In-page queries that see into open shadow roots
# --------------------------------------------------------------------------- #
#
# A backend's native selectors may stop at a shadow boundary (Selenium's do). These give it the
# same view Playwright has: descendant combinators cross shadow roots, and every match is
# returned in document order. Closed shadow roots are, as everywhere, out of reach.

_QUERY_HELPERS = r"""
const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();

// Every element, depth first in document order; a host's shadow tree comes before its light children.
function everyElement(node, out) {
  for (const el of node.children) {
    out.push(el);
    if (el.shadowRoot) everyElement(el.shadowRoot, out);
    everyElement(el, out);
  }
  return out;
}

// The parent, hopping from a shadow root to its host.
function flatParent(el) {
  const parent = el.parentNode;
  if (!parent) return null;
  if (parent.nodeType === 11) return parent.host || null;
  return parent.nodeType === 1 ? parent : null;
}

// Splits at descendant combinators (whitespace) that are outside brackets, parentheses and quotes.
// `>`, `+` and `~` stay glued to their neighbours. A selector list (a top-level comma) is one chunk.
function splitDescendant(selector) {
  const chunks = [];
  let current = '', depth = 0, quote = null;
  for (let i = 0; i < selector.length; i++) {
    const c = selector[i];
    if (c === '\\') { current += c + (selector[i + 1] || ''); i++; continue; }
    if (quote) { current += c; if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; current += c; continue; }
    if (c === '[' || c === '(') depth++;
    else if (c === ']' || c === ')') depth--;
    else if (c === ',' && depth === 0) return [selector];
    if (depth === 0 && /\s/.test(c)) {
      let j = i;
      while (j < selector.length && /\s/.test(selector[j])) j++;
      const before = current.trim().slice(-1), after = selector[j];
      if (current.trim() && j < selector.length && !'>+~'.includes(before) && !'>+~'.includes(after)) {
        chunks.push(current.trim());
        current = '';
      } else {
        current += ' ';
      }
      i = j - 1;
      continue;
    }
    current += c;
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks;
}

// Every element matching `selector`, whichever shadow root it is in. `a b` finds a `b` inside any
// descendant of an `a`, shadow trees included. Throws on a selector the browser rejects.
function pierceAll(selector) {
  const chunks = splitDescendant(selector);
  const all = everyElement(document, []);
  let current = all.filter((el) => el.matches(chunks[0]));
  for (let i = 1; i < chunks.length; i++) {
    const ancestors = new Set(current);
    current = all.filter((el) => {
      if (!el.matches(chunks[i])) return false;
      for (let p = flatParent(el); p; p = flatParent(p)) if (ancestors.has(p)) return true;
      return false;
    });
  }
  return current;
}

// The text runs directly inside an element (broken up by child elements) — what an exact text match
// compares against — plus a button-type input's value.
function ownTextRuns(el) {
  const runs = [];
  let run = '';
  for (const node of el.childNodes) {
    if (node.nodeType === 3) run += node.nodeValue;
    else if (node.nodeType === 1) { if (run) runs.push(run); run = ''; }
  }
  if (run) runs.push(run);
  if (el.localName === 'input' && /^(button|submit|reset)$/i.test(el.type)) runs.push(el.value);
  return runs.map(clean);
}
"""

QUERY_JS = "(selector) => {" + _QUERY_HELPERS + "return pierceAll(selector); }"
"""Every element matching a css selector, piercing open shadow roots."""

TEXT_JS = (
    "(tag, text) => {" + _QUERY_HELPERS + "const wanted = clean(text);"
    "return everyElement(document, []).filter((el) => (tag === '*' || el.localName === tag)"
    " && !/^(script|style|noscript)$/.test(el.localName) && ownTextRuns(el).includes(wanted)); }"
)
"""Every ``tag`` element whose own text is exactly ``text`` (whitespace-normalised)."""

IDS_JS = (
    "() => {"
    + _QUERY_HELPERS
    + "return everyElement(document, []).filter((el) => el.hasAttribute('id'))"
    ".map((el) => el.id); }"
)
"""The id of every element that has one, in document order (piercing shadow roots)."""

ID_AT_JS = (
    "(index) => {" + _QUERY_HELPERS + "return everyElement(document, [])"
    ".filter((el) => el.hasAttribute('id'))[index] || null; }"
)
"""The element at ``index`` in the ``IDS_JS`` listing."""

CHILD_FRAMES_JS = (
    "() => {const out = []; (function walk(node) { for (const el of node.children) {"
    "if (el.localName === 'iframe' || el.localName === 'frame') out.push(el);"
    "if (el.shadowRoot) walk(el.shadowRoot); walk(el); } })(document); return out; }"
)
"""The ``<iframe>``/``<frame>`` elements of the current document, piercing shadow roots."""


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
                Frame(
                    path=path,
                    url=url,
                    name=name,
                    same_origin=same_origin,
                    handle=child,
                    skip_reason=None
                    if same_origin
                    else (CROSS_ORIGIN if parent_same_origin else INSIDE_CROSS_ORIGIN),
                )
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
    frames: Iterable[Frame],
    run_collector: Callable[[Frame], list[dict[str, Any]]],
    *,
    skipped: list[SkippedFrame] | None = None,
) -> list[Element]:
    """Run ``run_collector`` in every same-origin frame and merge the results.

    A frame that fails mid-collection (e.g. it navigated or detached) is logged
    and skipped rather than failing the whole page. Every skipped frame, unreachable or
    failed, is appended to ``skipped`` (when given) with the reason.
    """
    elements: list[Element] = []
    for frame in frames:
        if not frame.same_origin:
            reason = frame.skip_reason or CROSS_ORIGIN
            logger.debug("skipping frame", frame=frame.path, url=frame.url, reason=reason)
            if skipped is not None:
                skipped.append(SkippedFrame(list(frame.path), frame.url, reason))
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
            if skipped is not None:
                skipped.append(SkippedFrame(list(frame.path), frame.url, UNREADABLE))
            continue
        elements.extend(Element.from_dict(raw, iframe_path=frame.path) for raw in raw_elements)
    return elements
