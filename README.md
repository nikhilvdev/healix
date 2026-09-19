# healix

[![CI](https://github.com/nikhilvdev/healix/actions/workflows/ci.yml/badge.svg)](https://github.com/nikhilvdev/healix/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License](https://img.shields.io/github/license/nikhilvdev/healix)](LICENSE)

A pure-Python library that crawls a website, recognizes and classifies every page it
finds, extracts every element with maximum raw detail into JSON, and generates
self-healing Selenium/Playwright automation scripts. It is built to plug into
external orchestration platforms through an SDK, a CLI, and webhooks.

**Status: pre-release, under active development.** The driver abstraction (Playwright),
iframe/shadow-DOM traversal, stable-ID normalization, page discovery with its manifest,
rule-based page classification, and extraction to per-page JSON are
implemented and tested. Login handling, the healing scorer and store, the Selenium adapter, script generation,
and the SDK/CLI/event surface are **not built yet** — see the [Roadmap](#roadmap) and
`CHANGELOG.md`. Nothing is published to PyPI yet.

## Features

Available now:

- **One driver interface** — everything is written against a `Driver` ABC; only the
  adapter module imports `playwright`. Selenium comes later against the same interface
- **Maximum-detail element extraction** — tag, id, normalized id, name, classes, every
  attribute, computed state, bounding box, css selector, xpath, iframe path, shadow path,
  and DOM context, captured up front and never deferred to a second pass
- **Recursive same-origin iframe traversal** — every frame's elements are merged into one
  flat list, each tagged with an `iframe_path` such as `["main", "workspace_panel", "form_frame"]`
- **Open shadow DOM piercing** — nested open shadow roots are walked recursively; no
  vendor-specific code
- **Stable-ID normalization** — `user-4471` → `user-{n}`, `pt1:r1:0:soc1::content` →
  `pt{n}:r{n}:{n}:soc{n}::content`, UUIDs and long hex fragments too
- **Fingerprint resolution** — `driver.find(fingerprint)` tries stable attributes → id →
  name → aria-label → css → xpath → normalized id → text, and rejects ambiguous matches
- **Two-stage crawl, stage one: discovery** — finds every reachable in-scope page with **no
  depth cutoff**, bounded only by a `max_pages` safety ceiling
- **Deduplication by normalized URL *and* structural hash** — `/product/123` and
  `/product/456` on one template become one manifest entry
- **Resumable manifest** — per-page `pending | extracted | failed` status, atomic writes,
  and a `remaining_pages()` resume set
- **Rule-based page classification** — labels each page `login`, `dashboard`, `list`,
  `detail`, `form`, `search`, `checkout`, `nav_shell`, `modal`, or `unknown` from element
  and URL heuristics. Deterministic, no LLM, no network; every decision comes with the
  scores and signals behind it — see [Page classification](#page-classification)
- **Structured logging via [logquill](https://pypi.org/project/logquill/)** — JSON-line
  records with metadata instead of formatted strings; quiet by default, one call to
  reconfigure — see [Logging](#logging)
- **One small runtime dependency** — `logquill`, which itself has none. Playwright is an
  optional extra
- **Typed throughout** — `mypy --strict` clean

- **Sequential, resumable extraction (stage two)** — walks the manifest one page at a time and
  writes one full-detail JSON file per page, saving progress after every page so an
  interrupted run resumes where it stopped — see [Extraction and output](#extraction-and-output)

Planned (see [Roadmap](#roadmap)): auto-detected login with `.env` credentials, weighted and threshold-gated
self-healing with a persistent fingerprint store, a Selenium adapter, script generation,
and the SDK / CLI / webhook surface.

## Install

Healix is not on PyPI yet. From a checkout:

```bash
git clone https://github.com/nikhilvdev/healix.git
cd healix
python -m venv .venv
source .venv/bin/activate
pip install -e ".[playwright]"
playwright install chromium
```

Requires Python 3.10+.

## Quickstart

### Extract every element on a page

```python
from healix.driver.playwright_adapter import PlaywrightDriverAdapter

with PlaywrightDriverAdapter() as driver:
    driver.navigate("https://example.com")
    for el in driver.get_elements():
        print(el.tag, el.css_selector, el.text_content)
```

```text
html html None
body html > body None
div html > body > div None
h1 html > body > div > h1 Example Domain
p html > body > div > p:nth-of-type(1) This domain is for use in documentation examples ...
p html > body > div > p:nth-of-type(2) None
a html > body > div > p:nth-of-type(2) > a Learn more
```

`get_elements()` returns `Element` objects — see [Element schema](#element-schema). Pass
your own `Page` (`PlaywrightDriverAdapter(page)`) to embed Healix in a Playwright session
you already manage; the adapter then leaves the browser lifecycle to you.

### Find, click, and write

```python
import json

from healix.driver.playwright_adapter import PlaywrightDriverAdapter
from healix.healing.fingerprint import Fingerprint

with PlaywrightDriverAdapter() as driver:
    driver.navigate("https://example.com")

    link = driver.find(
        Fingerprint(
            page_url="https://example.com", element_role="link", tag="a", text_content="Learn more"
        )
    )
    print(json.dumps(link.to_dict(), indent=2))

    driver.click(link)  # also accepts a Fingerprint directly
    print(driver.current_url)  # https://www.iana.org/help/example-domains
    # driver.write("hello", into=field)
```

`find` raises `ElementNotFoundError` when no locator resolves to exactly one element. It
does not guess — score-based healing on top of it is [planned](#roadmap).

### Discover every page on a site

```python
from healix.discovery.crawler import DiscoveryConfig, DiscoveryCrawler
from healix.driver.playwright_adapter import PlaywrightDriverAdapter

with PlaywrightDriverAdapter() as driver:
    crawler = DiscoveryCrawler(driver, DiscoveryConfig(max_pages=50))
    manifest = crawler.discover(
        ["https://example.com/"], run_id="demo", manifest_path="output/manifest.json"
    )

print(manifest.pages_discovered, manifest.discovery_status)
for page in manifest.pages:
    print(page.status, page.page_type, page.url)
```

```text
1 complete
pending unknown https://example.com/
```

(`example.com` links only to another domain, so there is nothing else in scope.)

### Classify a page

```python
from healix.classification import classify, classify_page
from healix.driver.playwright_adapter import PlaywrightDriverAdapter

with PlaywrightDriverAdapter() as driver:
    driver.navigate("https://github.com/login")
    elements = driver.get_elements()

    print(classify(elements, driver.current_url))  # login

    result = classify_page(elements, driver.current_url)
    print(result.page_type, result.confidence)  # login 1.0
    print(result.signals["login"])  # ['password_input', 'submit_control', 'few_fields', 'login_cue']
```

`DiscoveryCrawler` runs this on every page it visits, so each manifest entry already has
a `page_type`.

### Extract every discovered page to JSON

```python
from healix.discovery.crawler import DiscoveryConfig, DiscoveryCrawler
from healix.driver.playwright_adapter import PlaywrightDriverAdapter
from healix.extraction import ElementExtractor, ExtractionConfig

with PlaywrightDriverAdapter() as driver:
    # Discovery: find the pages (writes output/manifest.json)
    DiscoveryCrawler(driver, DiscoveryConfig(max_pages=5)).discover(
        ["https://quotes.toscrape.com/"], run_id="demo", manifest_path="output/manifest.json"
    )

    # Extraction: extract each one, one at a time, into output/pages/
    manifest = ElementExtractor(driver, ExtractionConfig(output_path="output")).extract(
        "output/manifest.json"
    )

print(manifest.pages_extracted, "of", manifest.pages_discovered, "extracted")
for page in manifest.pages:
    print(page.status, page.page_type, page.output_file)
```

```text
4 of 4 extracted
extracted list pages/0001-quotes.toscrape.com.json
extracted login pages/0002-quotes.toscrape.com-login.json
extracted unknown pages/0003-quotes.toscrape.com-author-albert-einstein.json
extracted detail pages/0004-quotes.toscrape.com-tag-change-page-1.json
```

### Resume an interrupted run

Progress is saved after every page, so just run the extractor again over the same manifest:

```python
with PlaywrightDriverAdapter() as driver:
    ElementExtractor(driver, ExtractionConfig(output_path="output")).extract("output/manifest.json")
```

Pages already `extracted` are skipped, `failed` pages are retried, and a page marked
`extracted` whose output file has gone missing is extracted again. Low-level access is on
`Manifest`: `remaining_pages()`, `mark_extracted()`, `mark_failed()`, `save()`.

## How it works

Healix crawls in two stages rather than as a depth-limited breadth-first walk.

```text
Stage 1 — discovery                        Stage 2 — extraction
───────────────────                        ────────────────────
walk links, no depth cutoff                walk the manifest one page at a time
dedupe by URL + structural hash            navigate → wait → extract full element JSON
stop at max_pages or empty frontier        → write output → mark "extracted" → next
        │                                              ▲
        └────────────► manifest.json ──────────────────┘
                       (resume any run_id from the first non-extracted page)
```

Extraction is sequential by default so fingerprint-store writes stay ordered and resumability
stays simple.

### The `Driver` abstraction

```python
class Driver(ABC):
    current_url: str  # property

    def navigate(self, url: str) -> None: ...
    def find(self, fingerprint: Fingerprint) -> Element: ...
    def click(self, target: Element | Fingerprint) -> None: ...
    def write(self, text: str, into: Element | Fingerprint) -> None: ...
    def get_elements(self, *, iframe_traversal: bool = True) -> list[Element]: ...
    def get_frames(self) -> list[Frame]: ...  # recursive, same-origin
    def screenshot(self) -> bytes: ...
```

Extraction, discovery, healing, classification, and generation never import `playwright`
or `selenium`; they go through `Driver`. `PlaywrightDriverAdapter` is the only adapter
today. `navigate()` waits for the `load` event and then, best-effort, up to
`settle_timeout_ms` (default 3000) for the network to go idle so client-rendered pages
have content before it is read. Set it to `0` to skip that wait.

### Iframes, shadow DOM, and stable IDs

These three are generic core capabilities, not per-vendor code.

| Capability | Behavior |
|---|---|
| Iframes | The frame tree is walked recursively. Same-origin frames are merged into the page's elements, each tagged with its `iframe_path`. Cross-origin frames (and everything beneath them) are listed by `get_frames()` with `same_origin=False` and skipped |
| Shadow DOM | Open shadow roots are pierced recursively. `css_selector` crosses the boundary with a descendant combinator; `shadow_path` lists each host's selector, outermost first |
| Stable IDs | Volatile id segments are replaced with placeholders; both `id` and `id_normalized` are stored, and `find` falls back to the normalized form when the raw id no longer resolves |

| Raw id | Normalized |
|---|---|
| `user-4471` | `user-{n}` |
| `pt1:r1:0:soc1::content` | `pt{n}:r{n}:{n}:soc{n}::content` |
| `row-123e4567-e89b-12d3-a456-426614174000` | `row-{uuid}` |
| `btn_a3f9c2d81b` | `btn_{hex}` |
| `login-form` | `login-form` |

### Element schema

Every element captures maximum raw detail at extraction time. This is real output for the
"Learn more" link on `example.com`:

```json
{
  "tag": "a",
  "id": null,
  "id_normalized": null,
  "name": null,
  "classes": [],
  "attributes": { "href": "https://iana.org/domains/example" },
  "text_content": "Learn more",
  "computed": {
    "visible": true,
    "enabled": true,
    "bounding_box": { "x": 256, "y": 186.078125, "width": 82, "height": 18 },
    "checked": null,
    "selected": null,
    "readonly": null,
    "required": null,
    "focused": false,
    "position": "static",
    "z_index": null,
    "href": "https://iana.org/domains/example"
  },
  "xpath": "/html[1]/body[1]/div[1]/p[2]/a[1]",
  "css_selector": "html > body > div > p:nth-of-type(2) > a",
  "iframe_path": ["main"],
  "platform_signal": null,
  "dom_context": {
    "parent_tag": "p",
    "parent_id": null,
    "sibling_index": 0,
    "nearby_label_text": null
  },
  "shadow_path": []
}
```

Notes on individual fields:

- `attributes` holds every attribute except `id`, `class`, and `name`, which have their own fields.
- `text_content` is the element's **own** text nodes only, so parents don't repeat their children's text.
- `xpath` is relative to the element's nearest root — the document, or its shadow root.
- `bounding_box` is relative to the element's own frame viewport, not the page.
- `computed.position` and `computed.z_index` (the CSS values, `null` for `auto`) are what the
  `modal` classifier reads to spot overlays.
- `computed.href` is the browser-resolved absolute URL (it honors `<base href>`) on links, `null` elsewhere.
- Input `value` (what a user has typed) is deliberately **not** captured, and a password
  field's markup `value` *attribute* is recorded as `"[redacted]"`, so password values never reach
  output JSON.
- Non-rendered tags (`script`, `style`, `head`, `title`, `meta`, `link`, `template`, `noscript`, `base`) are skipped; everything else is captured.

## Discovery and the manifest

`DiscoveryCrawler(driver, config).discover(start_urls, run_id=None, manifest_path=None)`
visits every distinct in-scope page reachable from the start URLs and returns a `Manifest`.
Links are read from `Driver.get_elements()`, so anchors inside same-origin iframes and open
shadow roots are found too.

### Config

`DiscoveryConfig` is the `crawl.discovery` block of the run config:

| Key | Default | Meaning |
|---|---|---|
| `domain_scope` | `"same_domain"` | `same_domain` (host match, ignoring a leading `www.`) or `same_origin` (scheme + host + port) |
| `max_pages` | `50` | Safety ceiling on page **visits** (navigations), not manifest entries |
| `dedupe_by` | `"url_normalized_and_structural_hash"` | Or `"url_normalized"` to keep every distinct URL |
| `template_sample_size` | `3` | Once this many URLs of one path template (`/product/1`, `/product/2`, …) have been visited, further ones are **deferred** until all other pages are visited — never dropped. `0` disables |

`template_sample_size` exists so a family of look-alike pages can't use up `max_pages` ahead
of distinct pages. With 200 product links and `max_pages=50`, `/about` still gets visited.

### How pages are deduplicated

- **Normalized URL** — scheme and host lowercased; default ports, userinfo, and the fragment
  dropped; session/tracking parameters (`utm_*`, `gclid`, `fbclid`, `jsessionid`, `sid`, …)
  stripped; query sorted; trailing slash removed. A `#/route` or `#!/route` fragment is kept,
  because on hash-routed apps it *is* the page.
- **Structural hash** — a fingerprint of the page's DOM *structure* built from the set of
  distinct element signatures (tag, parent tag, normalized id, name, `type`, `role`, frame
  depth, shadow-ness). Text, list length, volatile ids, and CSS classes are ignored, so two
  pages on one template hash the same. The second page is recorded under the first as a
  `variant_urls` entry, and its links are still followed.

### Manifest format

```json
{
  "run_id": "demo",
  "start_urls": ["https://example.com/"],
  "discovery_status": "complete",
  "pages_discovered": 1,
  "pages_extracted": 0,
  "platform_detected": null,
  "pages": [
    {
      "url": "https://example.com/",
      "page_type": "unknown",
      "structural_hash": "c260c62ac032aa1d",
      "status": "pending",
      "output_file": null,
      "variant_urls": [],
      "error": null
    }
  ]
}
```

- `discovery_status` is `complete`, `max_pages_reached`, or `interrupted`. Anything but
  `complete` means pages may be missing. `manifest_path` is written even on interruption.
- `status` is `pending`, `extracted`, or `failed`. A page that fails to load, or whose
  elements can't be read, is recorded as `failed` with an `error`, and the crawl continues.
- `page_type` comes from [`classify`](#page-classification) by default. It is provisional:
  extraction re-classifies each page from its fresh elements and writes the final type back.
   Pass your own
  `classifier=(elements, url) -> str`, or `classifier=None` to skip classification (every
  page is then `"unknown"`).
- `on_page_discovered=` is called once per new manifest entry as it is recorded.

Manifest writes are atomic (temp file + rename), so a crash never leaves a truncated file.

## Extraction and output

`ElementExtractor(driver, config).extract(manifest)` is the second stage. For each page in the manifest,
in order, it navigates, waits for load, extracts every element at full detail, writes the
page's JSON, marks the manifest entry `extracted`, and moves on. It is **sequential by design**
— it keeps fingerprint-store writes ordered and makes resume simple — so don't parallelize it
without revisiting that.

`manifest` is a `Manifest` or a path to one. Progress is saved to `<output_path>/manifest.json`
(or `manifest_path=`) after every page. The call returns the updated `Manifest`.

### Config

`ExtractionConfig` is the `crawl.extraction` block of the run config:

| Key | Default | Meaning |
|---|---|---|
| `sequence` | `"one_by_one"` | The only supported value today |
| `output_format` | `"json"` | The only supported value today |
| `output_path` | `"./output/"` | Where `manifest.json` and `pages/` go |
| `iframe_traversal` | `true` | Merge same-origin frames' elements in; `false` reads the main frame only (open shadow roots are still pierced) |
| `platform_detection` | `"auto"` | `auto` or `off`. Validated, but acts only once the platform adapters land; `platform_detected` stays `null` until then |

### Output layout

```text
output/
  manifest.json
  pages/
    0001-example.com.json
    0002-example.com-orders-42.json
```

A page's `output_file` in the manifest is relative to `output_path`. File names use the page's
1-based position in the manifest (unique and stable) plus a readable slug of its URL. Files are
written atomically.

### Page JSON

```json
{
  "schema_version": 1,
  "run_id": "demo",
  "url": "https://quotes.toscrape.com/login",
  "page_type": "login",
  "structural_hash": "3fa4f7fde11f3d63",
  "captured_at": "2026-09-19T17:38:50.626Z",
  "element_counts": {
    "total": 28,
    "visible": 27,
    "in_shadow_root": 0,
    "by_tag": { "a": 4, "body": 1, "div": 9, "footer": 1, "form": 1, "h1": 1, "html": 1, "input": 4, "label": 2, "p": 3, "span": 1 },
    "by_frame": { "main": 28 }
  },
  "elements": [ "…each one in the raw element schema above…" ]
}
```

`final_url` is added only when the page redirected. `page_type` and `structural_hash` are from
the fresh extraction — if the structure changed since discovery, that is logged at `info`.
`by_frame` keys are the `iframe_path` joined with `/`. Only the structural representative of a
group of same-template pages is extracted; the others are listed under its `variant_urls`.

### Failures, resume, and events

- A page that fails to load or read is marked `failed` with its `error`, and the run continues.
- The output file is written **before** the manifest records it, so a crash in between just
  means the page is extracted again — the manifest never points at a file that isn't there.
- `classifier=` re-classifies each page (default `healix.classification.classify`; `None`
  keeps the discovery-time type). `on_page_extracted=` receives an `ExtractedPage` (`url`,
  `page_type`, `element_count`, `output_file`) — the fields of the planned `page_extracted`
  event.

> **Output files are sensitive.** They record page markup as found: attribute values, link
> URLs (which can carry tokens), and hidden-input values such as CSRF tokens. Only a password
> field's markup `value` is redacted. `output/` is git-ignored by default; keep it that way.

## Page classification

`classify(elements, url="")` labels a page with one of nine types, or `"unknown"`. It is
pure element-count and attribute heuristics over the `Element` list and the URL — no LLM
calls, no network, no randomness — so the same page always gets the same answer. Only
*visible* elements vote, so hidden templates and collapsed panels don't skew a result.

| Type | Detection heuristic |
|---|---|
| `login` | A single password input plus a submit control and few other fields, with a sign-in cue in the URL or a heading; or an OAuth/SSO redirect URL with "Sign in with…" buttons (no password field needed). Sign-up cues count against it |
| `dashboard` | Several KPI/summary widgets and charts (`canvas`, chart-library markup), few inputs, an overview/dashboard cue |
| `list` | Repeating row structures (table rows, cards) outside navigation, plus pagination controls |
| `detail` | Single-entity display: one `h1`, key/value pairs (`dl`, read-only fields), text content, no repeating rows, an entity-shaped URL such as `/product/123` |
| `form` | High input-field-to-text ratio, several fields, and a submit/save control |
| `search` | A search input, filter controls (selects, checkboxes), and a results area — a header search box alone is not enough |
| `checkout` | Payment-field patterns (`cc-number`, CVC, expiry…), multi-step indicators, checkout cues |
| `nav_shell` | Mostly navigation links, low input density, little prose |
| `modal` | A dialog (`role="dialog"`, `aria-modal`, `<dialog open>`) or a large high-`z-index` fixed overlay that *dominates* the page |

### How a type is chosen

Each type has a rule that awards weighted **signals** (weights sum to 1.0) and a few
penalties. A page gets a type when its score reaches **0.5**. When several types score
within **0.15** of the best, the most specific wins, in this order: `modal`, `login`,
`checkout`, `search`, `form`, `list`, `dashboard`, `detail`, `nav_shell`. That is how a
login form (structurally also a small form) is called a login, and a results page (also a
form and a list) is called a search. If nothing reaches 0.5 the answer is `"unknown"` — an
honest "no rule matched" rather than a weak guess.

`classify_page` returns the evidence — every type's score and the signals that fired — so a
surprising label is debuggable, and `confidence` lets callers ignore low-confidence labels:

```python
result = classify_page(elements, url)  # a product grid with a header search box
result.page_type   # "list"
result.confidence  # 0.9
result.scores      # {"login": 0.15, "dashboard": 0.15, "list": 0.9, "detail": 0.1, "form": 0.45,
                   #  "search": 0.0, "checkout": 0.0, "nav_shell": 0.7, "modal": 0.0}
result.signals["list"]       # ["repeating_rows", "pagination", "few_fields"]
result.signals["nav_shell"]  # [..., "-repeating_content_rows"]  (a "-" prefix is a penalty)
```

### Accuracy and limits

These are heuristics, not a guarantee. The rules are exercised against realistic rendered
pages for every type and for the look-alikes that trip naive rules (a registration form with
a password field, a header search box on a product grid, a cookie banner on an article), and
spot-checked against public sites — login pages on GitHub and two practice sites, list pages
on Hacker News and two scraping sandboxes, a DuckDuckGo results page. Expect misses on
unusual layouts.

- **Article and other long-form content has no type.** It typically comes back `unknown`, or
  `nav_shell` at the minimum 0.5 score on link-heavy pages such as a Wikipedia article.
- **A thin cookie banner or toast is deliberately not a `modal`** (a container must be at
  least 150px tall and cover or hold most of the page), so a page carrying one keeps its
  real type. A blocking, full-page overlay is a `modal`.
- A page can legitimately be two things (a search page is also a list). The priority order
  picks one; `result.scores` shows the runner-up.

## Logging

Healix logs through [logquill](https://pypi.org/project/logquill/): every record is a short
constant message plus structured metadata, rendered as one JSON line —

```json
{"timestamp":"2026-09-19T17:24:53.728Z","level":"INFO","logger":"healix.discovery.crawler","message":"discovery finished","meta":{"run_id":"demo","status":"complete","visits":1,"pages_discovered":1}}
```

By default only `WARN` and above are shown, on **stderr** (stdout stays free for program
output). Turn up the detail with the environment or in code:

```bash
HEALIX_LOG_LEVEL=debug python my_crawl.py
```

```python
from logquill import FileTransport
from healix.log import configure_logging

configure_logging(level="info")                                 # just change the level
configure_logging(transports=[FileTransport("healix.log")])     # send records elsewhere
configure_logging(transports=[])                                # silence Healix entirely
```

Levels: `info` records discovery start and finish, `debug` adds every discovered page, every
classification with its scores, redirects, and skipped frames; `warn` reports pages that
failed to load and frames that couldn't be read. Any logquill transport or plugin works —
see the [logquill docs](https://github.com/nikhilvdev/logquill-python).

In your own code around Healix, `from healix.log import get_logger` gives you a logger under
the same configuration. `configure_logging` updates every Healix logger, including ones
created before it was called.

## Design decisions

These are fixed unless explicitly reopened:

- **Pure Python.** No second implementation language and no compiled core. Cross-language
  consumers are served by the CLI and webhook boundary.
- **Rule-based classification, no LLM calls** anywhere in the crawl/classify/extract path —
  deterministic, reproducible runs with no external API cost.
- **Secrets live in `.env` only.** Never in the run config, which must always be safe to
  commit. Copy [`.env.example`](.env.example) to `.env` (git-ignored).
- **Login is a page classification**, not a separate config step. A page classified `login`
  is handed to a login handler that applies `.env` credentials.
- **Platform adapters are additive.** SAP UI5 and Salesforce LWC adapters, when they land,
  only add signal on top of the generic pipeline, which always runs on its own.
- **Healing must reject weak matches.** Attributes are weighted (stable signals over volatile
  ones), and a best-but-weak candidate below the confidence threshold is rejected, not used.

## Known limitations

- **Canvas-rendered UI is unsupported.** Some Dynamics 365 canvas apps and legacy Java-applet
  Oracle Forms aren't in the DOM at all, so no locator strategy can reach their elements.
  This is a hard boundary, and the healer is designed to fail cleanly on it.
- **Closed shadow roots** are not reachable.
- **Cross-origin iframes** are listed but not extracted, and a same-origin frame nested
  inside a cross-origin one is skipped too.
- **Extraction reads a page as a fresh, anonymous visit.** Client-side state, filled-in forms,
  and anything behind a login are not reproduced (login handling is planned). If a page
  redirects to a login page, that is what gets extracted — and classified.
- **Script-only navigation** — buttons and router pushes with no `<a href>` — is invisible
  to discovery.
- **Client-rendered sites that never go network-idle** may be read before they finish
  rendering. Pages with an identical (e.g. empty) structure would then collapse into one
  manifest entry.
- **Structural dedup is coarse by design.** Two different pages whose elements produce the
  same signatures are merged. Use `dedupe_by="url_normalized"` to turn it off.
- **Classification is heuristic** — see [Accuracy and limits](#accuracy-and-limits).
- Authenticated crawling arrives with the login handler; today discovery sees only what an
  anonymous browser sees.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| 1 | `Driver` ABC, Playwright adapter, iframe + shadow DOM traversal, ID normalization | ✅ Done |
| 2 | Discovery, manifest, dedup, per-page status | ✅ Done |
| 3 | Rule-based page classification (`login`, `dashboard`, `list`, `detail`, `form`, `search`, `checkout`, `nav_shell`, `modal`), and logquill-based logging | ✅ Done |
| 4 | Sequential extraction — one JSON file per page, resumable | ✅ Done |
| 5 | SDK (`Crawler`, `Extractor`, `Healer`, `ScriptGenerator`), CLI, event schema and webhooks | Planned |
| 6 | Auto-detected login with `.env` credentials, MFA abort, mid-crawl re-login | Planned |
| 7 | Self-healing: fingerprints, weighted scorer, confidence threshold, SQLite/Postgres store | Planned |
| 8 | Selenium adapter, SAP UI5 and Salesforce LWC platform adapters | Planned |
| 9 | Packaging, `healix doctor`, PyPI release | Planned |

### Planned integration surface

Not implemented yet — shown so the direction is clear. All three will emit the same event
payloads (`page_discovered`, `page_extracted`, `element_healed`, `script_generated`,
`run_complete`, `login_failed`).

```python
# SDK
from healix import Crawler, ScriptGenerator

run = Crawler(config="run_config.json").discover_and_extract()
script = ScriptGenerator(run.pages).to_playwright(style="pom")
```

```bash
# CLI
healix crawl --config run_config.json --output ./output/
healix generate --input ./output/manifest.json --backend playwright --style pom
```

## API reference

Every public class and function has a docstring; the reference is generated with
[pdoc](https://pdoc.dev) and published at
[nikhilvdev.github.io/healix](https://nikhilvdev.github.io/healix/) on every push to `main`.
To build it locally:

```bash
pip install -e ".[docs]"
pdoc --docformat google healix
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,hooks]"
playwright install chromium
pre-commit install

ruff check .
ruff format --check .
mypy
pytest
```

The browser tests run real headless Chromium against small fixture sites served over local
HTTP (including a second origin for the cross-origin cases); they skip themselves if
Playwright or its browsers aren't installed.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow, the
[Code of Conduct](CODE_OF_CONDUCT.md) for community standards, and
[SECURITY.md](.github/SECURITY.md) for how to report a vulnerability.

## License

[MIT](LICENSE)
