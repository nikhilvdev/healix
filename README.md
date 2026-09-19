# healix

[![CI](https://github.com/nikhilvdev/healix/actions/workflows/ci.yml/badge.svg)](https://github.com/nikhilvdev/healix/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License](https://img.shields.io/github/license/nikhilvdev/healix)](LICENSE)

A pure-Python library that crawls a website, recognizes and classifies every page it
finds, extracts every element with maximum raw detail into JSON, and generates
self-healing Selenium/Playwright automation scripts. It is built to plug into
external orchestration platforms through an SDK, a CLI, and webhooks.

**Status: pre-release, under active development.** The driver abstraction (Playwright),
iframe/shadow-DOM traversal, stable-ID normalization, and Phase A page discovery with
its manifest are implemented and tested. Classification, the extraction writer,
login handling, the healing scorer and store, the Selenium adapter, script generation,
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
- **Two-phase crawl, phase A: discovery** — finds every reachable in-scope page with **no
  depth cutoff**, bounded only by a `max_pages` safety ceiling
- **Deduplication by normalized URL *and* structural hash** — `/product/123` and
  `/product/456` on one template become one manifest entry
- **Resumable manifest** — per-page `pending | extracted | failed` status, atomic writes,
  and a `remaining_pages()` resume set
- **Zero required runtime dependencies** — Playwright is an optional extra
- **Typed throughout** — `mypy --strict` clean

Planned (see [Roadmap](#roadmap)): rule-based page classification, sequential extraction to
per-page JSON, auto-detected login with `.env` credentials, weighted and threshold-gated
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

### Resume from a manifest

```python
from healix.discovery.manifest import Manifest

manifest = Manifest.load("output/manifest.json")
for page in manifest.remaining_pages():  # everything not yet "extracted"
    print(page.url)

manifest.mark_extracted("https://example.com/", "output/example.com.json")
manifest.save("output/manifest.json")
```

## How it works

Healix crawls in two phases rather than as a depth-limited breadth-first walk.

```text
Phase A — discovery                        Phase B — extraction (planned)
─────────────────────                      ──────────────────────────────
walk links, no depth cutoff                walk the manifest one page at a time
dedupe by URL + structural hash            navigate → wait → extract full element JSON
stop at max_pages or empty frontier        → write output → mark "extracted" → next
        │                                              ▲
        └────────────► manifest.json ──────────────────┘
                       (resume any run_id from the first non-extracted page)
```

Phase B is sequential by default so fingerprint-store writes stay ordered and resumability
stays simple.

### The `Driver` abstraction

```python
class Driver(ABC):
    current_url: str  # property

    def navigate(self, url: str) -> None: ...
    def find(self, fingerprint: Fingerprint) -> Element: ...
    def click(self, target: Element | Fingerprint) -> None: ...
    def write(self, text: str, into: Element | Fingerprint) -> None: ...
    def get_elements(self) -> list[Element]: ...
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
- `computed.href` is the browser-resolved absolute URL (it honors `<base href>`) on links, `null` elsewhere.
- Input `value` is deliberately **not** captured, so filled-in passwords never reach output JSON.
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
- `page_type` is `"unknown"` until [classification](#roadmap) lands; pass a
  `classifier=` callable to `DiscoveryCrawler` to set it in the meantime.
- `on_page_discovered=` is called once per new manifest entry as it is recorded.

Manifest writes are atomic (temp file + rename), so a crash never leaves a truncated file.

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
- **Script-only navigation** — buttons and router pushes with no `<a href>` — is invisible
  to discovery.
- **Client-rendered sites that never go network-idle** may be read before they finish
  rendering. Pages with an identical (e.g. empty) structure would then collapse into one
  manifest entry.
- **Structural dedup is coarse by design.** Two different pages whose elements produce the
  same signatures are merged. Use `dedupe_by="url_normalized"` to turn it off.
- Authenticated crawling arrives with the login handler; today discovery sees only what an
  anonymous browser sees.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | `Driver` ABC, Playwright adapter, iframe + shadow DOM traversal, ID normalization | ✅ Done |
| 2 | Phase A discovery, manifest, dedup, per-page status | ✅ Done |
| 3 | Rule-based page classification (`login`, `dashboard`, `list`, `detail`, `form`, `search`, `checkout`, `nav_shell`, `modal`) | Planned |
| 4 | Phase B sequential extraction — one JSON file per page | Planned |
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
