# healix

[![CI](https://github.com/nikhilvdev/healix/actions/workflows/ci.yml/badge.svg)](https://github.com/nikhilvdev/healix/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License](https://img.shields.io/github/license/nikhilvdev/healix)](LICENSE)

A pure-Python library that crawls a website, recognizes and classifies every page it
finds, extracts every element with maximum raw detail into JSON, and generates
self-healing Selenium/Playwright automation scripts. It is built to plug into
external orchestration platforms through an SDK, a CLI, and webhooks.

**Status: 2.0.** The driver abstraction (Playwright and
Selenium), iframe/shadow-DOM traversal, stable-ID normalization, page discovery with its manifest,
rule-based page classification, extraction to per-page JSON, and the SDK, CLI, and event/webhook
surface are implemented and tested, and so are automatic login (username/password, SSO, session
expiry), self-healing (weighted, threshold-gated, persisted, audited) and the optional SAP UI5 and
Salesforce platform adapters. Script generation (`ScriptGenerator`, `healix generate`) and
`healix doctor` are built too. See [Known limitations](#known-limitations) for what it does not do,
and `CHANGELOG.md` for the release notes.

## Features

Available now:

- **One driver interface, two backends** — everything is written against a `Driver` ABC; only the
  adapter modules import `playwright` or `selenium`. The same crawl, extraction and healing run
  unmodified on either, chosen with the run config's `backend` — see
  [The `Driver` abstraction](#the-driver-abstraction)
- **Optional platform adapters** — SAP UI5 control ids and Salesforce Lightning component tags are
  added to elements as `platform_signal`, only ever *on top of* the generic pipeline — see
  [Platform adapters](#platform-adapters)
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
- **One small runtime dependency** — `logquill`, which itself has none. Playwright, Selenium and
  the PostgreSQL driver are optional extras
- **Typed throughout** — `mypy --strict` clean

- **Sequential, resumable extraction (stage two)** — walks the manifest one page at a time and
  writes one full-detail JSON file per page, saving progress after every page so an
  interrupted run resumes where it stopped — see [Extraction and output](#extraction-and-output)

- **SDK, CLI, and webhooks on one event schema** — `Crawler` and `Extractor` in Python,
  `healix crawl` / `healix extract` on the command line. `on_event` and `--webhook-url` emit the
  *same* payloads (`page_discovered`, `page_extracted`, `run_complete`, …), signed if you set a
  secret — see [SDK](#sdk), [CLI](#cli), and [Events](#events)
- **A run config that is safe to commit** — credential-looking keys are rejected, pointing you at
  `.env` — see [Run configuration](#run-configuration)

- **Automatic login** — a page classified `login` is filled in with credentials from `.env`,
  including a single-sign-on redirect and POST-back. If the session expires mid-crawl it logs in
  again and resumes. It aborts cleanly on MFA, never submits a password twice, and never types
  credentials where it shouldn't — see [Authentication](#authentication)

- **Self-healing locators** — when a stored locator stops matching, every same-tag element is scored
  against the element's fingerprint with *weighted* signals (test id and aria-label count for far
  more than a generated id), and the best is accepted only above a confidence threshold. The fix is
  persisted and every heal is audited as routine churn or a possible regression. It refuses rather
  than guess — see [Self-healing](#self-healing)

- **Script generation** — `ScriptGenerator` and `healix generate` turn a crawl into a page-object
  module, a `pytest` file, or a plain fill-and-click script, for Playwright or Selenium. Every step
  goes through `Healer`, so the script keeps working when the page changes — see
  [Script generation](#script-generation)
- **`healix doctor`** — checks the Python version, the dependencies, each backend's package and
  browser, and (with `--launch`) that a browser really opens and reads a page — see [CLI](#cli)

## Install

```bash
pip install "healix[all]"        # or healix[playwright], healix[selenium]
playwright install chromium      # for the Playwright backend
healix doctor                    # check that this machine is ready
```

From a checkout, for development:

```bash
git clone https://github.com/nikhilvdev/healix.git
cd healix
python -m venv .venv
source .venv/bin/activate
pip install -e ".[playwright]"
playwright install chromium
```

Requires Python 3.10+. This also installs the `healix` command. Runtime dependencies are
`logquill` (logging), `python-dotenv` (the CLI loads `.env`) and `jinja2` (script generation). The browser libraries are optional
extras: `healix[playwright]` (the default backend; then `playwright install chromium`) and
`healix[selenium]` (needs Chrome installed; Selenium finds the matching driver itself).
`healix[all]` installs both and `healix[postgres]`, the `psycopg` driver only needed to keep
fingerprints in PostgreSQL.

## Quickstart

### Crawl a site with the SDK

Write a run config (safe to commit — see [Run configuration](#run-configuration)):

```json
{
  "base_url": "https://quotes.toscrape.com/",
  "crawl": {
    "discovery": { "max_pages": 4 },
    "extraction": { "output_path": "./output/" }
  }
}
```

```python
from healix import Crawler

run = Crawler("run_config.json", on_event=print).discover_and_extract()
print(run.pages_extracted, "of", run.pages_discovered, "pages ->", run.manifest_path)
```

`on_event` is called with one dict per event as the crawl progresses (see [Events](#events)).
`run.pages` are the manifest entries; the JSON for each is under `output/pages/`.

### Crawl a site with the CLI

```bash
healix crawl --config run_config.json --webhook-url https://hooks.example.com/healix
```

```text
run bebd30b63d35: 4 of 4 pages extracted (discovery max_pages_reached)
manifest: output/manifest.json
```

The webhook received the same events `on_event` would have — here, four `page_discovered`, four
`page_extracted`, and one `run_complete`:

```json
{"event": "run_complete", "run_id": "bebd30b63d35", "timestamp": "2026-09-19T18:00:14.862Z", "data": {"pages_discovered": 4, "pages_extracted": 4, "platform_detected": null, "manifest_path": "output/manifest.json"}}
```

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
does not guess; repairing a fingerprint that has drifted is the job of the
[healer](#self-healing).

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
    print(
        result.signals["login"]
    )  # ['password_input', 'submit_control', 'few_fields', 'login_cue']
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

Progress is saved after every page. With the SDK or CLI, re-run with the same `run_id`
(`Crawler(config, run_id="…")`, `healix crawl --run-id …`); see [Resuming](#resuming-a-run).
Working at the lower level, just run the extractor again over the same manifest:

```python
with PlaywrightDriverAdapter() as driver:
    ElementExtractor(driver, ExtractionConfig(output_path="output")).extract("output/manifest.json")
```

Pages already `extracted` are skipped, `failed` pages are retried, and a page marked
`extracted` whose output file has gone missing is extracted again. Low-level access is on
`Manifest`: `remaining_pages()`, `mark_extracted()`, `mark_failed()`, `save()`.

## Run configuration

One JSON file describes a crawl. It is safe to commit: it never holds secrets.

```json
{
  "mode": "guided | autonomous",
  "base_url": "https://example.com",
  "start_url": "https://example.com/login",
  "crawl": {
    "discovery": {
      "domain_scope": "same_domain",
      "max_pages": 50,
      "dedupe_by": "url_normalized_and_structural_hash",
      "template_sample_size": 3,
      "click_discovery": false
    },
    "extraction": {
      "sequence": "one_by_one",
      "output_format": "json",
      "output_path": "./output/",
      "iframe_traversal": true,
      "platform_detection": "auto"
    }
  },
  "backend": "playwright | selenium",
  "roles": ["admin", "standard"]
}
```

| Key | Meaning |
|---|---|
| `mode` | `guided`: crawl starts at `start_url` (and `base_url`, if given). `autonomous`: only `base_url`; the crawl discovers from scratch. Optional — inferred as `guided` when `start_url` is present |
| `base_url`, `start_url` | Absolute http(s) URLs. Guided mode needs `start_url`; autonomous needs `base_url` |
| `crawl.discovery` | See [Discovery config](#config) |
| `crawl.extraction` | See [Extraction config](#config-1) |
| `roles` | Optional list of user role **names** (never credentials): crawl once per role and compare. See [Multi-role runs](#multi-role-runs) |
| `backend` | `playwright` (default) or `selenium`. Everything else in the config means the same on both. A backend whose library is not installed raises `BackendUnavailableError` saying which extra to install |

Everything is optional except the start point. Unknown keys are errors, not silently ignored, so a
typo can't quietly change a crawl.

**Secrets never go here.** Any key that looks like a credential (`password`, `secret`, `token`,
`api_key`, `credential…`, `username`) is rejected at any depth, with a message pointing at `.env`.
Put credentials and the webhook secret in the environment or a git-ignored `.env`
(see [`.env.example`](.env.example)).

## SDK

```python
from healix import Crawler, Extractor
```

| | |
|---|---|
| `Crawler(config, *, run_id=None, on_event=None, webhook_url=None, webhook_secret=None, webhook_outbox=None, driver=None, headless=True, credentials=None, auto_login=True, fingerprint_store=None, fingerprint_mode="keep")` | `.discover()` finds pages and writes the manifest. `.discover_and_extract()` then extracts each one |
| `RoleCrawler(config, *, run_id=None, only=None, on_event=None, webhook_url=None, webhook_secret=None, webhook_outbox=None, headless=True, credentials=None, auto_login=True, fingerprint_store=None, fingerprint_mode="keep")` | For a config with `roles`: `.discover()` / `.discover_and_extract()` crawl once per role and return a `MultiRoleRun` (`.runs`, `.diff`, `.diff_path`) — see [Multi-role runs](#multi-role-runs). `Crawler` refuses such a config |
| `Extractor(config=None, *, on_event=None, webhook_url=None, webhook_secret=None, webhook_outbox=None, driver=None, headless=True, credentials=None, auto_login=True, fingerprint_store=None, fingerprint_mode="keep", role=None)` | `.extract(manifest)` extracts a `Manifest` or a path to one, resumably. `role` picks whose credentials to use; it defaults to the manifest's own |
| `ScriptGenerator(source, *, base_dir=None, fingerprint_db=None, record_fingerprints=True, max_elements_per_page=200, on_event=None, webhook_url=None, ...)` | `.to_playwright(style="pom")` / `.to_selenium(style="pom")` / `.generate(backend, style)` build a script and return a `GeneratedScript` — see [Script generation](#script-generation) |
| `Healer(store=None, *, driver=None, threshold=0.5, ambiguity_margin=0.05, run_id=None, on_event=None, webhook_url=None, ...)` | Records fingerprints and finds elements again after the page changes — see [Self-healing](#self-healing) |

`config` is a path to a run-config JSON, a dict, or a `RunConfig`. Both calls return a `Run`:
`run.run_id`, `run.pages` (manifest entries), `run.pages_discovered`, `run.pages_extracted`,
`run.pages_failed`, `run.platform_detected`, `run.discovery_status`, `run.blocked_on_auth`,
`run.manifest_path`, `run.summary()`.

- **The browser.** Unless you pass a `driver`, the SDK launches and closes its own (the config's
  `backend`, headless by default). A `driver` you pass is used as-is and its lifecycle stays yours.
- **`.env`.** The SDK does not read `.env`. If you keep the login credentials or
  `HEALIX_WEBHOOK_SECRET` there, call `dotenv.load_dotenv()` first, or pass `credentials=` /
  `webhook_secret=`.
- **Login.** Automatic and on by default; `credentials=Credentials(username, password)` overrides the
  environment and `auto_login=False` turns it off. See [Authentication](#authentication).
- **`Extractor` output location.** Given a manifest *path* and no `config`, output goes beside the
  manifest. With a `config`, it goes to `crawl.extraction.output_path`.
- **Errors.** `ConfigError` (bad config), `RunConflictError` (see below),
  `BackendUnavailableError` (the backend's library is not installed). All are ordinary exceptions; the
  SDK never calls `sys.exit`.

### Resuming a run

`run_id` names a run. Re-running with the **same `run_id`** over the same output directory
resumes it: finished discovery is reused, and extraction continues from the first page that is not
`extracted` (failed pages are retried). The events you receive describe the work done by *that
call* — a resume does not re-announce pages discovered earlier.

Without a `run_id`, a new one is generated and kept on `crawler.run_id`. If the output directory
already holds a run, that is a `RunConflictError` rather than a silent overwrite — pass the run's id
to resume it, or choose another output path.

## CLI

```bash
healix crawl   --config run_config.json [--output DIR] [--run-id ID] [--discover-only] [--role NAME]...
               [--webhook-url URL [--webhook-outbox PATH]] [--headed] [--no-login]
               [--fingerprint-db PATH] [--json] [--log-level LEVEL]
healix extract --manifest output/manifest.json [--config run_config.json] [--role NAME]
               [--webhook-url URL [--webhook-outbox PATH]] [--headed] [--no-login]
               [--fingerprint-db PATH] [--json] [--log-level LEVEL]
healix generate --input output/manifest.json [--backend playwright|selenium]
               [--style pom|test|action] [--output DIR] [--fingerprint-db PATH] [--no-record]
               [--webhook-url URL [--webhook-outbox PATH]] [--json] [--log-level LEVEL]
healix flush-events --outbox PATH --webhook-url URL [--timeout SECONDS] [--retry-rejected] [--json]
healix doctor [--launch] [--json]
healix --version          # also: python -m healix
```

- `crawl` runs discovery then extraction (`--discover-only` stops after discovery). `--output`
  overrides `crawl.extraction.output_path`.
- `extract` runs extraction over an existing manifest — a second step after `--discover-only`, or
  the way to retry failed pages. Output goes beside the manifest unless `--config` says otherwise.
- `--webhook-url` posts every event to that URL; see [Webhooks](#webhooks). The CLI goes through
  the SDK, so the events are exactly what `on_event` receives.
- `--role NAME` (on `crawl`, repeatable) runs only those roles of a [multi-role run](#multi-role-runs);
  on `extract` it says which role's credentials to log in with, and defaults to the manifest's own.
- `--webhook-outbox PATH` makes webhook delivery durable and needs `--webhook-url`;
  `flush-events` sends what a run left in it. Both are described under [Durable delivery](#durable-delivery).
- `--json` prints the run summary as one JSON line on stdout instead of the human text. Logs go to
  stderr (JSON lines, `WARN` and above by default; `--log-level` changes it).
- `--no-login` turns automatic login off.
- `--fingerprint-db PATH` records an element fingerprint for every actionable element to a SQLite file as pages are extracted — the baseline for [self-healing](#self-healing).
- `generate` writes one script from the extracted pages of a manifest (default `--backend
  playwright --style pom`, into `scripts/` beside the manifest) and records the fingerprints it heals
  against — see [Script generation](#script-generation).
- `doctor` checks that this machine can run Healix and exits `0` if it can, `1` if it cannot:

  ```text
  healix 2.0.0 doctor

    ok    python         3.12.14
    ok    logquill       1.0.0
    ok    python-dotenv  1.2.3
    ok    jinja2         3.1.6
    ok    playwright     1.63.0, browser at …/Google Chrome for Testing
    ok    selenium       4.49.0, browser at /Applications/Google Chrome.app/…
    ok    postgres       psycopg 3.3.6 (optional)
    info  credentials    login credentials are not set; a run that reaches a login page will stop there
                         -> set WEBLIB_LOGIN_USERNAME and WEBLIB_LOGIN_PASSWORD in the environment or a .env file

  Ready. Usable backends: playwright, selenium.
  ```

  A backend that is not installed is reported (`--`) and is not an error: one usable backend is
  enough. Without `--launch` nothing is started, so a browser that is installed but broken looks fine;
  `--launch` opens each usable browser, loads a page and reads its elements, which proves it. For
  Selenium that is also what downloads the matching chromedriver the first time (Selenium Manager),
  so it needs network access. Credentials are reported as set or not, never shown.
- The CLI loads a `.env` from the current directory (that is where the login credentials can live).

| Exit status | Meaning |
|---|---|
| `0` | Success |
| `1` | Runtime error (browser failure, backend unavailable, …); for `doctor`, no usable backend |
| `2` | Usage or configuration error, including a run conflict |
| `3` | The run finished, but some pages failed (`--run-id` the same id to retry them) |
| `4` | Blocked on authentication: a login page was reached but no credentials are set |
| `130` | Interrupted. Progress is saved; the message says how to resume |

## Events

Every event has the same envelope, whether it arrives through `on_event` or a webhook:

```json
{"event": "page_extracted", "run_id": "bebd30b63d35", "timestamp": "2026-09-19T18:00:09.658Z", "data": {"url": "…", "page_type": "list", "element_count": 136, "output_file": "pages/0001-quotes.toscrape.com.json"}}
```

`data` has exactly these fields for each event type — no more, no fewer. `make_event` validates it,
so the contract can't drift.

| Event | `data` contains |
|---|---|
| `page_discovered` | `url`, `page_type`, `structural_hash` |
| `page_extracted` | `url`, `page_type`, `element_count`, `output_file` |
| `element_healed` | `element_key`, `old_locator`, `new_locator`, `strategy_used`, `confidence_score`, `page_url` |
| `script_generated` | `backend`, `style`, `file_path`, `element_count` |
| `run_complete` | `pages_discovered`, `pages_extracted`, `platform_detected`, `manifest_path` |
| `login_failed` | `url`, `reason`, `screenshot_ref` |
| `roles_compared` | `roles`, `diff_path`, `differences` |

In a [multi-role run](#multi-role-runs) every event also carries `"role"` in the envelope, next to
`event` and `run_id`, saying which role it happened under. A run without roles has no such key, so
its payloads are exactly what they always were. `roles_compared` is emitted once, after the last
role, and its `differences` is the number of page and element differences in `roles-diff.json`.

`page_type` in `page_discovered` is provisional (classified during discovery); in `page_extracted`
it is final. A `login_failed` `reason` is one of `mfa_required`, `timeout`, `selector_not_found`,
`auth_rejected`.

**Emitted today:** `page_discovered` (once per new manifest entry), `page_extracted` (once per page
written), `login_failed` (once per failed login — see [Authentication](#authentication)), and
`run_complete` (last). `element_healed` is emitted by `Healer` (see [Self-healing](#self-healing)),
and `script_generated` by `ScriptGenerator` and `healix generate` (once per script; `file_path` is
`null` when the script was returned but not written to disk).

A `login_failed` `url` never carries a query string (SSO redirect URLs hold `state` and `code`),
and `screenshot_ref` is a path relative to the output directory, or `null`.

### Webhooks

`--webhook-url` (or `webhook_url=`) POSTs each event's JSON, in order, from a background thread — a
slow endpoint never stalls the crawl.

- Headers: `Content-Type: application/json`, `X-Healix-Event: <event type>`, `X-Healix-Delivery: <id>`, `User-Agent: healix/<version>`. The delivery id is the same for every attempt at one event, so a receiver that sees an event twice can tell. It is a header and not part of the payload, which stays identical to what `on_event` receives.
- **Signing.** If `HEALIX_WEBHOOK_SECRET` is set (or `webhook_secret=` passed), each request also
  carries `X-Healix-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw request body. Verify it
  before trusting a payload:

  ```python
  import hashlib, hmac


  def verify(secret: str, body: bytes, header: str) -> bool:
      expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
      return hmac.compare_digest(expected, header)
  ```

- **Retries.** Network errors, timeouts, HTTP 5xx, 408 and 429 are retried up to 3 times with
  exponential backoff; other 4xx responses are not (the receiver said no). A delivery that finally
  fails is logged and skipped — it never fails the crawl.
- **Not logged.** The URL (webhook URLs often embed tokens) is never logged, only its host.
- **By default delivery is best-effort:** events are queued in memory (up to 1000) and flushed when
  the run ends. An event that cannot be delivered is logged and dropped. If you can't afford to
  miss one, turn on durable delivery below, or treat the manifest as the source of truth.

#### Durable delivery

Add `--webhook-outbox PATH` (or `webhook_outbox=` on `Crawler`, `Extractor`, `Healer` and
`ScriptGenerator`) and every event is written to a SQLite file before it is sent, and removed only
once the receiver has accepted it:

```bash
healix crawl --config run_config.json --webhook-url https://hooks.example.com/healix \
             --webhook-outbox events.db
```

- **A receiver that goes down mid-run does not lose anything.** The events wait in the file, the
  sender keeps retrying (2 s, then longer, up to 60 s between rounds), and when the receiver
  returns they are delivered, oldest first. The crawl is not slowed down or failed by the outage.
- **In order.** A failing event is retried before any later one is sent, so the receiver sees events
  in the order they were emitted.
- **What is left when the run ends stays in the file.** At exit, Healix makes one last attempt, then
  says on stderr how many events are still waiting. They are sent first by the next run that opens
  the same outbox, or on demand:

  ```bash
  healix flush-events --outbox events.db --webhook-url https://hooks.example.com/healix
  ```

  `flush-events` exits `0` when the outbox is empty, `1` when events are still waiting (the receiver
  could not be reached within `--timeout`, default 60 s) and `3` when the receiver rejected some.
  It signs with `HEALIX_WEBHOOK_SECRET`, like a run.
- **At least once, not exactly once.** If the receiver processes an event but its answer never
  arrives, the event is sent again. Every attempt carries the same `X-Healix-Delivery` id: drop a
  delivery whose id you have already handled. Exactly-once is not possible over HTTP.
- **A receiver that says no does not block the rest.** A 4xx answer other than 408 and 429 means
  the receiver rejected that event. It is set aside in the file (never sent again unless you pass
  `--retry-rejected` to `flush-events`) and the next event goes out.
- **One sender per outbox file.** Two processes draining the same file at once would send events
  twice. Use one outbox per receiver.
- **The file is private to you** (mode `0600`): it holds page URLs and output paths. The signing
  secret and the webhook URL are never stored in it.

## Authentication

Login is not a configuration step. When discovery or extraction lands on a page the classifier
calls `login`, Healix fills it in and carries on — one credential per crawl. To crawl as several
users and compare what each can reach, see [Multi-role runs](#multi-role-runs).

**Credentials** come from the environment, never from the run config:

```bash
# .env  (git-ignored; see .env.example)
WEBLIB_LOGIN_USERNAME=alice@example.com
WEBLIB_LOGIN_PASSWORD=...
```

The CLI loads `.env` from the current directory. In the SDK, call `dotenv.load_dotenv()` or pass
`Crawler(config, credentials=Credentials(username, password))`.

### What it handles

- **A username + password form**, including one inside an iframe or shadow root.
- **Single sign-on.** A page offering only "Sign in with SSO" is clicked through to the identity
  provider and back, whether it returns by redirect or by an auto-submitted POST form, including
  providers that ask for the username and the password on separate screens.
- **Session expiry.** A page that redirects to the login after you have logged in triggers a new
  login, then the page is loaded again and the crawl resumes where it stopped. This works during
  discovery and during extraction.
- **Guided and autonomous modes.** A guided run that starts at the login page logs in there and
  continues past it; an autonomous run logs in the first time a page bounces to a login.

### What it will not do

The failure modes are fixed, and none of them can hang or loop:

- **MFA cannot be completed automatically.** The attempt is aborted with `login_failed`
  (`mfa_required`) — the one-time-code form is never touched.
- **A password is never submitted twice in one attempt.** If the site sends you back to the same
  form, that is `auth_rejected`; retrying risks locking the account.
- **A failure ends login for the rest of the run.** Pages that needed it are recorded as `failed`
  (`requires authentication, and the login did not succeed`); nothing is retried.
- **A session that keeps expiring is bounded** — at most three logins per run, then `auth_rejected`.
- **Every login has a deadline** (30 seconds) and a step limit; a form that goes nowhere is
  `timeout`.
- CAPTCHAs, passkeys/WebAuthn, and hardware keys are not supported; they end as `timeout` or
  `auth_rejected`.

| `login_failed` reason | When |
|---|---|
| `mfa_required` | A one-time-code / second-factor prompt appeared |
| `auth_rejected` | The site refused the credentials, sent the form back, or the session keeps ending |
| `timeout` | The form was submitted but nothing changed before the deadline |
| `selector_not_found` | No usable login form: no password field, no submit control, or the page was not one it is willing to fill in (see below) |

A failure also saves a screenshot to `<output>/screenshots/login-failed-N.png` and reports it in the
event. It may show the username you typed (never the password, which the browser masks) — treat
`output/` as sensitive.

### Blocked on auth

With **no credentials set**, reaching a login page does not fail the login — the run is flagged
`blocked_on_auth` (in the manifest, on `Run`, in `--json` output; the CLI exits `4`), one warning is
logged, and the pages behind the login are recorded as `failed` with
`requires authentication, and no credentials are set`. No `login_failed` event is raised: the
event's reasons describe a login that was attempted. Set the two variables and re-run with the same
`run_id`; a blocked run is *discovered again*, since what is behind the login was never seen.

### Where credentials are typed

Credentials are typed only on **in-scope pages**, or on pages whose URL host or path looks like an
OAuth/SSO endpoint (`/oauth/…`, `/authorize`, `/saml`, `/sso`, `okta.com`, `accounts.google.com`,
and so on). The query string never counts — anyone can append `?client_id=` to a URL. An unexpected
redirect to some other login page is refused, and nothing is typed.

Before typing, the handler also checks the form itself: exactly one password field. A registration
page (two password fields) that was misclassified as a login is left alone.

> **Limit of that guard.** It stops *accidental* credential entry. It is not a defence against a
> hostile site: an application chooses its own identity provider, so a site you point Healix at and
> give credentials to can send them anywhere a real SSO flow could. Only use credentials for sites
> you trust with them — the same trust you place in typing them into the site's own login form.

### Secrets stay out of everything

The credentials are never logged, never put in an event, and never written to disk; a
`Credentials` object prints as `Credentials(username='***', password='***')`. URLs in logs and in
`login_failed` events drop their query string. A test crawls with a distinctive password and checks
that neither it nor the username appears in any log record, event, summary, manifest, or output
file.

## Multi-role runs

A site shows different things to different people. List **role names** in the run config, give each
its own credentials in the environment, and Healix crawls the site once as each role and tells you
what differs:

```json
{ "base_url": "https://app.example.com", "roles": ["admin", "standard", "anonymous"] }
```

```bash
# .env  (git-ignored). Each role's variables carry its name in capitals.
WEBLIB_LOGIN_USERNAME_ADMIN=admin@example.com
WEBLIB_LOGIN_PASSWORD_ADMIN=...
WEBLIB_LOGIN_USERNAME_STANDARD=sam@example.com
WEBLIB_LOGIN_PASSWORD_STANDARD=...
# "anonymous" is reserved: it never logs in, and needs nothing.
```

```bash
healix crawl --config run_config.json
```

```text
run 3f9a1c2b7d10: 3 role(s)
  admin: 14 of 14 pages extracted (discovery complete)
  standard: 9 of 9 pages extracted (discovery complete)
  anonymous: 2 of 2 pages extracted (discovery complete)
compared admin, standard, anonymous: 12 page difference(s), 7 element difference(s)
diff: output/roles-diff.json
output: output/
```

- **Names, not secrets.** `roles` holds names only, so the config stays safe to commit. A name is
  lowercase letters, digits, `-` and `_` (starting with a letter, up to 32 characters), because it
  becomes a folder and part of an environment variable. `read-only` uses `WEBLIB_LOGIN_USERNAME_READ_ONLY`.
  Two names that would share a variable (`a-b` and `a_b`) are refused. In the SDK, pass
  `credentials={"admin": Credentials(...)}` to `RoleCrawler` instead.
- **A missing credential stops the run before any browser starts,** naming the variables to set.
- **One fresh browser per role.** A role's login, cookies and storage are gone before the next role
  starts, so no role can see through another's session. Roles run one after another, in the order
  listed. (A `driver=` you supply is refused for this reason.)
- **One folder per role.** Each role gets its own manifest and page files, and nothing else changes
  about them:

  ```text
  output/
    roles/
      admin/      manifest.json  pages/…  screenshots/…
      standard/   manifest.json  pages/…
      anonymous/  manifest.json  pages/…
    roles-diff.json
  ```

  Every role's manifest carries `"role"`, and all share one `run_id`. Generate a script for one role
  from its own manifest: `healix generate --input output/roles/admin/manifest.json`.
- **The `anonymous` role** is how to see what a visitor who is not signed in can reach. It never
  logs in, so pages behind a login simply are not reached, and it never needs credentials.
- **Resume and retry.** Re-run with the same `--run-id` to resume every role where it stopped
  (finished roles log nobody in). `--role standard` runs just that role, and the diff still covers
  every role that has a result on disk. A role that cannot log in is reported (`login_failed`, exit
  `4`) and does not stop the others. `healix extract --manifest output/roles/admin/manifest.json`
  finishes one role and logs in as it, because the manifest knows its role.

### The diff

`roles-diff.json` compares every role that has a result:

```json
{
  "roles": ["admin", "standard"],
  "summary": { "pages_reached": {"admin": 14, "standard": 9}, "pages_in_all_roles": 9,
               "page_differences": 5, "element_differences": 3,
               "only": {"admin": {"pages": 5, "elements": 3}, "standard": {"pages": 0, "elements": 0}} },
  "page_differences":    [ {"url": "https://app.example.com/admin", "roles": ["admin"], "missing": ["standard"]} ],
  "element_differences": [ {"url": "https://app.example.com/reports", "element": "button:export-csv",
                            "roles": ["admin"], "missing": ["standard"]} ]
}
```

- **Pages.** A role reached a URL if its manifest has it (as an entry or as a variant of one) and it
  did not fail to load. `page_differences` lists every URL not reached by all roles.
- **Elements.** On each page that two or more roles extracted to files of their own, an element is
  compared by its *element role* (`button:export-csv`, the name a [fingerprint](#self-healing) is
  stored under), and only **visible, actionable** elements count: a button, link or field a person
  could use, not markup. So a control that is in the page but hidden from one role is a difference,
  and a paragraph is not. An element with no stable name compares as its kind alone (`button`),
  which is coarse.
- **The event.** A `roles_compared` event (`roles`, `diff_path`, `differences`) is emitted once, last.
  Every other event of a multi-role run carries `"role"`. See [Events](#events).
- The diff holds URLs and element names taken from the output files, so treat it as sensitive in the
  same way.

### What roles do and do not do

- **Fingerprints are not per role.** They stay keyed by `(page_url, element_role)`. An element two
  roles both see is recorded once, by the first role that reaches it, and one only the administrator
  sees is recorded by the administrator. `fingerprint_mode="refresh"` is refused for a multi-role
  run, since whichever role ran last would overwrite the rest. A generated script heals against that
  shared baseline, so an element that differs between roles is healed against whichever role saw it
  first.
- **Extraction stays sequential**, including across roles, so a run takes as long as its roles added
  together.
- **A role is a set of credentials, not a permission model.** Healix reports what each identity could
  reach and see. It does not know what it *should* be able to, so a difference is a fact to review,
  not a finding.
- **One credential per role.** A user with several credentials, or several sessions, is several roles.
  MFA still aborts the login, per role, as it does for a single run.

## Self-healing

A script that finds `#user-4471` breaks the day the id becomes `user-9032`. Healix records a
**fingerprint** of every actionable element — everything extraction captured about it — and when a
locator stops matching, it finds the element again by *resemblance*, then repairs the fingerprint.

```python
from healix import Healer

with Healer("healix.db") as healer:
    healer.learn("https://shop.example.com/login")            # record the baseline once

    # ... later, after the page has changed ...
    healer.write("alice", "https://shop.example.com/login", "textbox:login-username")
    healer.click("https://shop.example.com/login", "button:login-submit")
```

Fingerprints usually come from a crawl: `healix crawl --config run_config.json --fingerprint-db
healix.db`, or `Crawler(config, fingerprint_store="healix.db")`. Each is stored under
`(page_url, element_role)`. A **role** is a stable name built from the element's most durable
identity — `textbox:login-username`, `button:sign-in`, `link:forgot-password` — with repeats numbered
`#2`, `#3`. Recording keeps an existing baseline (`fingerprint_mode="keep"`); pass `"refresh"` to
overwrite it after an intended redesign.

### How an element is found

1. **Primary locators, in priority order:** stable test attributes (`data-testid`, `data-test`,
   `data-qa`, `data-cy`, …) → `id` → `name` → `aria-label` → css → xpath → normalized id → text. The
   first that matches exactly one element wins. A test id, id, name, or aria-label is trusted when it
   is the *first* locator tried. Any other match is **verified** against the fingerprint and rejected
   if it doesn't resemble the stored element — a locator that matches *something* isn't necessarily
   matching *the element*.
2. **Scoring.** If no locator survives, every element with the same tag is scored against the
   fingerprint.
3. **The gate.** The best candidate must reach the confidence **threshold** (default `0.5`,
   configurable), and must not be a near-tie with the runner-up (`ambiguity_margin`, default `0.05`).
   A best-but-weak or ambiguous match is **rejected** with `ElementNotHealedError` — never silently
   used. A page drawn on a canvas raises `UnsupportedRenderingError` instead (see below).

### How candidates are scored

The score is a weighted comparison, not a flat one. Stable signals count for far more than volatile
ones:

| Signal | Weight | Signal | Weight |
|---|---|---|---|
| stable test attribute | 0.25 | other attributes (`type`, `href`, …) | 0.08 |
| name | 0.12 | DOM path (ancestor tags) | 0.08 |
| visible text | 0.12 | `id` (raw, or same after normalizing) | 0.04 |
| nearby label text | 0.12 | classes | 0.04 |
| aria-label | 0.10 | parent · sibling index | 0.03 · 0.02 |

Three rules decide how they combine:

- **A signal missing on either side is neutral, not a mismatch.** A developer stripping a test id
  doesn't make the element a different one. A *different value* is a mismatch.
- The score is the weighted mean of the signals that *could* be compared, **damped by how much
  evidence there was**: a candidate that can only be compared on one or two weak signals can't score
  high however well they match.
- Text-like signals are rescaled so merely *unrelated* strings score 0 rather than a comfortable 0.3.

**One exception, for a locator's own hit.** Damping keeps a *search* honest, but it also means an
element with little to identify it (an image-only link, an icon-only button: no text, label, name or
test id) can never reach 0.5, even when it is exactly the element that was recorded. A page that had
not changed at all would then fail. (A real book shop found this: every product image link, and a
bare disabled text box on another site, failed on an unchanged page.) So when the css or xpath
locator resolves to a single element, it is accepted if the live element provides **everything the
fingerprint has to offer** and **all of it agrees** (raw similarity of at least 0.9, or your
threshold if that is higher). An element that has lost the test id, name or text it was recorded with
is *not* accepted this way, and neither is one whose `href`, text, label or class differs: those are
weak matches, rejected as before. What this cannot catch is a look-alike that differs in nothing
Healix can see, so an element with nothing but its position to go on is trusted at that position.
It applies only to checking a locator's hit, never to choosing among candidates.

A scored heal's `healed element` log record (at `info` level) lists each signal's similarity, and
`Score.explain()` returns the same, so a surprising heal is debuggable.

### What it does on a real page

One URL, changing between "deploys" (from the integration tests; roles are the username and password
fields and the submit button):

| The page changes to… | Result | Confidence |
|---|---|---|
| only generated `id`s regenerated | found exactly by the test id — nothing to heal | — |
| ids, classes regenerated; test ids stripped | healed via `name` (fields), `text` (button); **churn** | 0.87 · 0.87 · 0.66 |
| heavy refactor: new wrappers, ids, classes; no test ids, names or aria-labels | healed by **weighted score** (fields), `text` (button); **churn** | 0.54 · 0.51 · 0.58 |
| a field's label becomes "Email address or mobile number" | healed via `name`, flagged **regression** | 0.75 |
| the form is removed; only a search box remains | **refused** — best candidate 0.13 | — |
| two indistinguishable text boxes | **refused** — below threshold (or ambiguous) | 0.42 |
| the UI is drawn on a `<canvas>` | **refused** with `UnsupportedRenderingError` | — |

Note the heavy refactor: the healed fields clear the threshold by a hair (0.51, 0.54). The gate is
real, and a redesign that also changes labels or types will not pass it.

### The healing history

Every heal replaces the fingerprint **and** appends a record to the history, in one transaction, so
the fix survives future runs and the store never holds one without the other. A record carries the
old and new fingerprints, the old and new locators, the strategy, the confidence, and which fields
changed — and a verdict on *what kind* of change it was:

- **`churn`** — ids, classes, test ids, name attributes, DOM position. The handles moved; the element
  did not. Routine and harmless.
- **`regression`** — the element's visible meaning changed: a different tag, input `type` or `role`,
  or text, accessible name, placeholder or label that no longer resembles the old. A healed script
  would keep passing while the product changed underneath it, so this deserves a look.

```python
healer.history(change_kind="regression")    # heal records, newest first
healer.report()                              # per element: heals, churn, regressions
```

`healer.report()` ranks elements by how often they heal — the flaky locators worth stabilizing.

### Events

Each heal emits an `element_healed` event through `on_event` / `webhook_url` — the same payloads as
every other event:

```json
{"event": "element_healed", "run_id": "demo", "timestamp": "2026-09-19T18:59:00.860Z", "data": {"element_key": "https://shop.example.com/login#textbox:login-username", "old_locator": "stable_attr:data-testid=[data-testid=\"login-username\"]", "new_locator": "id=[id=\"f_8d2a\"]", "strategy_used": "name", "confidence_score": 0.873, "page_url": "https://shop.example.com/login"}}
```

`strategy_used` is the locator strategy that matched, or `weighted_score` for a scored heal.

### The store

Two backends implement the same `FingerprintStore` interface (`get`, `put`, `put_if_absent`,
`fingerprints`, `apply_heal`, `history`, `close`), and one contract test suite runs against both:

| | Use | Opened with |
|---|---|---|
| **SQLite** (default) | one file, no extra dependency (stdlib `sqlite3`) | a path: `Healer("healix.db")` |
| **PostgreSQL** | fingerprints shared across machines or CI runs | a URL: `Healer("postgresql://user:pass@host:5432/db")` |

The path or URL is accepted everywhere a store is: `Healer(...)`, `Crawler(config, fingerprint_store=...)`,
and `healix crawl --fingerprint-db ...`. You can also pass your own `FingerprintStore`.

```bash
pip install "healix[postgres]"      # adds the psycopg driver
healix crawl --config run_config.json --fingerprint-db postgresql://user:pass@host:5432/healix
```

- **Both** are safe to share between threads, refuse a database written by a newer Healix, and write a
  heal's fingerprint update and its history record in **one transaction**.
- **PostgreSQL** creates three tables, `healix_meta`, `healix_fingerprints`, and
  `healix_healing_history`, in the connection's default schema on first use (prefixed, so they can
  share a database with other applications). JSON columns are `JSONB`, so the audit trail is
  queryable in SQL, e.g. `SELECT … FROM healix_healing_history WHERE change_kind = 'regression'`.
  Several processes starting at once is safe (schema creation takes an advisory lock).
- The connection URL usually carries a password. It is never logged and is masked in `repr` and in
  error messages, even if a driver echoes it.

### The canvas boundary

**Canvas-rendered UI is unsupported, and the healer fails cleanly on it.** Some Dynamics 365 canvas
apps and legacy Java-applet Oracle Forms draw their UI instead of building it from DOM elements, so
there is nothing for any locator to find. When healing fails on a page that is essentially one
canvas (or plugin object) with almost no DOM controls, `Healer` raises `UnsupportedRenderingError`
with an explanation — not a low-confidence guess.

### Limits

- Only elements that were **fingerprinted** can be healed, and only among **same-tag** candidates.
- Roles are derived from an element's identity; if that identity changes, so does the role a fresh
  crawl would assign. Heal against the stored role, and `refresh` after an intended redesign.
- The threshold and weights are heuristics calibrated on realistic test pages, not learned from your
  site. Expect to tune `threshold` — and read the regression flags.

## Script generation

`ScriptGenerator` turns the extracted pages of a crawl into a script you can run, for Playwright or
Selenium:

```python
from healix import Crawler, ScriptGenerator

run = Crawler("run_config.json").discover_and_extract()
script = ScriptGenerator(run).to_playwright(style="pom", output="./scripts")
print(script.file_path, script.page_count, "pages,", script.element_count, "elements")
```

```bash
healix generate --input ./output/manifest.json --backend playwright --style pom
```

`ScriptGenerator(run.pages)` works too; the page files are then read from `./output` (or
`base_dir=`). `to_playwright`/`to_selenium` return a `GeneratedScript` (`.source`, `.file_path`,
`.page_count`, `.element_count`, `.skipped`, `.truncated`); pass `output=` (a directory, or a `.py`
path) to write it.

**One generator, three styles, two backends.** `style` picks what is written; `backend` picks which
browser the script drives.

| Style | File | What it is |
|---|---|---|
| `pom` | `pages_<backend>.py` | A class per page with a method per element (`fill_username`, `click_sign_in`, `locate_…`), and a `session()` that opens a browser |
| `test` | `test_healix_<backend>.py` | A `pytest` module: one test per page that checks the address and that every element is found, visible and (if it was) enabled. It types and clicks nothing, so it is safe against a live site |
| `action` | `actions_<backend>.py` | A plain script: per page, fill the inputs and click the submit button. No assertions |

For example, an `action` script for a login page and an order form:

```python
def run_login(healer: Healer) -> None:
    """The login page."""
    url = "http://shop.example.test/login"
    healer.driver.navigate(url)
    healer.write(os.environ["WEBLIB_LOGIN_USERNAME"], url, "textbox:login-username", navigate=False)
    healer.write(os.environ["WEBLIB_LOGIN_PASSWORD"], url, "textbox:password", navigate=False)
    healer.click(url, "button:sign-in", navigate=False)
```

**Self-healing comes from `Healer`.** Every step names an element by its stable role
(`textbox:login-username`), not by a locator, so when the page changes the script resolves it the way
[`Healer`](#self-healing) does — through the next locator that still matches, else a weighted,
threshold-gated score — and records the fix. That needs the fingerprints: generating records them
into `--fingerprint-db` (default `healix.db`) unless you pass `--no-record` because the crawl already
did (`healix crawl --fingerprint-db`) — in which case pass that same `--fingerprint-db`, because a
script looks in `healix.db` unless told otherwise. `generate` warns (`GeneratedScript.warnings`) when
a local SQLite database has no fingerprints for the pages it scripted; a Postgres URL is not
connected to just to check. The generated code calls `healix.driver`, so it does not
import `playwright` or `selenium` itself, and it needs `healix` installed to run.

**Secrets never go into a script.** The value for a login page's username and password fields is
read from `WEBLIB_LOGIN_USERNAME` / `WEBLIB_LOGIN_PASSWORD` (a `.env` is loaded). Nothing is copied
from the page's own values. A `postgresql://` `--fingerprint-db` is not written into the file (it
carries a password): the script reads `HEALIX_FINGERPRINT_DB` instead. Page text is quoted as data,
so a hostile page cannot inject code into a script, and the output is deterministic — the same
manifest gives the same file.

Only pages whose status is `extracted` are used; pages left out are listed in `.skipped` with the
reason, and a page with more than `max_elements_per_page` (default 200) actionable elements is cut
off, counted in `.truncated`.

### What generated scripts do not do

- **They do not log in on their own, and pages are visited one at a time from a fresh navigation.**
  A page behind a login needs a session first. An `action` script visits pages in manifest order, so
  if the login page comes before the pages behind it, the browser stays signed in; otherwise, sign in
  first (in `pom` style, call `LoginPage(healer).open().fill_…().click_…()` before the others).
- **`action` is deliberately simple:** it types sample values into text inputs and clicks one submit
  button. It does not choose options in a `<select>`, tick checkboxes, upload files or follow links,
  and it does not string pages into a journey. The sample values (`user@example.com`, `sample text`,
  …) are placeholders, not meaningful business data. The other elements are still in `pom` (as
  `click_…` or `locate_…` methods) for you to drive.
- **`test` checks presence, not behaviour.** It cannot tell you a form works, only that its elements
  are still there — and, because it heals, that they are still found when their ids change. The
  healing is what you would otherwise have written by hand; it can also hide a real regression, so
  read the [healing history](#the-healing-history).
- **Elements the crawl could not see are not scripted** — cross-origin iframes, closed shadow roots,
  canvas-rendered UI ([the canvas boundary](#the-canvas-boundary)) — and a script fails cleanly
  there, as `Healer` does.
- **`action` needs a page with an input to fill.** A site with none (a catalogue of links) is refused
  with `no page has an input to fill: an action script needs one`; use `pom` or `test` there.
- Tested against the fixture sites on both backends — the scripts run in a real browser, survive a
  redesign, and the generated tests fail when the form is removed — and against six real public sites
  ([Validated on real sites](#validated-on-real-sites)), but **not against a large real
  application**. Expect to edit what is generated.

## Validated on real sites

The fixture sites in the test-suite were written alongside Healix, so they cannot say whether it works
on pages nobody wrote for it. Before 2.0, every kind of script was generated for six public sites
built to be scraped and automated, and the generated tests were run against the live sites on both
backends (each crawl bounded to at most 14 pages):

| Site | Pages crawled | Generated tests passing (Playwright and Selenium) |
|---|---|---|
| `quotes.toscrape.com` | 4 | 4 of 4 |
| `quotes.toscrape.com/js/` (rendered by script) | 5 | 5 of 5 |
| `books.toscrape.com` | 2 distinct structures | 2 of 2 (about 500 elements each, all found again) |
| `the-internet.herokuapp.com` | 13 | 9 to 11 of 11, depending on which randomised page misbehaves that load |
| `demo.playwright.dev/todomvc` (single-page app) | 1 | 1 of 1 |
| `saucedemo.com` (logs in with the demo login it publishes) | 2 | 1 of 2 |

The failures are the sites' doing, and are what Healix should do:

- `challenging_dom` **re-randomises its labels and ids on every load**, so an element cannot be told
  from its neighbours. The healer refuses ("the two best candidates scored 0.57 and 0.56") instead of
  guessing.
- `disappearing_elements` **shows a menu item on some loads and not others**, so a test that looks for
  it fails on the loads where it is gone. Which of these two pages fails varies from run to run.
- `saucedemo.com/inventory` sits behind a login, and **generated scripts do not log in**.

What validation found, and fixed: on an *unchanged* page, the generated tests failed for every element
with little to identify it (each book's image link; a bare disabled text box). The scorer's evidence
damping capped such an element below the threshold even when it was exactly the one recorded. It is
fixed (see [How candidates are scored](#how-candidates-are-scored)), and a fixture with the same
shapes now guards it. Both backends also found the same pages and the same element counts on all five
sites they were compared on, except where the site itself is non-deterministic or Chrome behaves
differently (a `401` page, and `http` links Chrome upgrades to `https`).

It also measured the Playwright quiet-window wait (`settle_quiet_ms`) on three client-rendered sites:
the pages and the element counts were identical with and without it, and it cost 15 to 45 percent
more time. That is why it is still off by default.

Re-run it yourself, against the live sites, whenever you change how pages are read or elements are
found:

```bash
HEALIX_REAL_SITES=1 pytest tests/real_sites -v
```

It is opt-in (network, several minutes), so CI does not run it, and a site changing can fail it.
These are practice sites, not the enterprise applications Healix is meant for: a large real application
is still the test that has not been run.

### Checking the Salesforce adapter against your org

Nothing in the suite can load a Lightning org, so the adapter is unverified until someone runs it on
one. A free Developer Edition org is enough, and nothing is changed in it:

1. Put a login in `.env` (`WEBLIB_LOGIN_USERNAME`, `WEBLIB_LOGIN_PASSWORD`) and crawl a few pages:
   `healix crawl --config run.json` with `"start_url"` at your `…lightning.force.com` home and
   `"max_pages": 5`.
2. Open `output/manifest.json`. `"platform_detected": "salesforce_lwc"` means the adapter saw the
   Lightning globals. `null` means it did not.
3. Open a page file. Elements inside Lightning components should carry a `platform_signal` of the form
   `{"platform": "salesforce_lwc", "component": "lightning-input", "is_component_host": false,
   "aura_attributes": {…}}`, and `element_counts.in_shadow_root` should be above zero.
4. If it does not detect Lightning, or a component tag is wrong, open an issue with the page's
   `platform_signal` values (never with credentials or record data). The generic pipeline works
   either way: an adapter only ever *adds* a signal.

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
or `selenium`; they go through `Driver`. There are two adapters, and the rest of Healix cannot tell
them apart: `PlaywrightDriverAdapter` (the default) and `SeleniumDriverAdapter`
(`healix.driver.selenium_adapter`, `browser="chrome"`, `"firefox"` or `"edge"`; Chrome and Firefox are
both run through Healix's own Selenium tests in CI (the Firefox job informs but does not block a
merge or a release), and Edge is untested). Both take `platform_adapters=` (see
[Platform adapters](#platform-adapters)), and both accept an existing `Page` / `WebDriver` to embed
in a session you manage.

`navigate()` waits for the `load` event and then, best-effort, up to `settle_timeout_ms`
(default 3000) for the page to go quiet so client-rendered pages have content before it is read.
Set it to `0` to skip that wait. `quiet_ms` (or `crawl.extraction.settle_quiet_ms` in the run
config) sets how long the page must stay unchanged, on both backends; see below.

**Selenium differs from Playwright in three ways, and the adapter makes up for each:**

| | Playwright | Selenium adapter |
|---|---|---|
| Waiting for the page | Network idle. Off by default, `quiet_ms` adds a second wait for no new resources or elements, for pages that render from a timer or a script of their own after network idle | No "network idle" exists in WebDriver, so it waits for `load`, then for no new resources or elements for `quiet_ms` (default 500). A request still *in flight* is invisible, so an API that answers more slowly than `quiet_ms` can be missed: raise `quiet_ms` for slow back ends |
| Clicking and typing | Waits until the element is actionable | Retries for up to `action_timeout_ms` (default 5000) while the element is missing, covered or not yet interactable. An ambiguous selector is reported at once, never retried |
| A page that will not load | `goto` raises | Some browsers show an error page and report success; the adapter detects it and raises, so a failed load is never read as a page |

Shadow roots and frames are read by the same in-page scripts on both backends, so a page gives the
same elements either way: the test-suite compares them, and every locator's result, side by side.
Selenium is not asked to walk shadow roots one `getShadowRoot()` call at a time; the in-page walk is
one round trip per frame and, like Playwright, sees open shadow roots only.

### Platform adapters

Thin, optional, and **additive only**. The generic pipeline — iframe traversal, shadow DOM piercing,
stable-id normalization — always runs, whatever platform a page is built with. An adapter can only
*add* an element's `platform_signal`; if it does not fire, or throws, extraction is unchanged.

| Adapter | Fires when | `platform_signal` |
|---|---|---|
| `sap_ui5` | `window.sap.ui` exists | `{"platform": "sap_ui5", "control_id": "__xmlview0--saveButton", "control_type": "sap.m.Button", "is_control_root": true}` for an element inside a UI5 control, found through `sap.ui.getCore().byId()` (or `Element.getElementById()` on UI5 versions without `getCore`) |
| `salesforce_lwc` | `$A` or `Aura` exists, or an element carries `data-aura-rendered-by` / `data-aura-class` | `{"platform": "salesforce_lwc", "component": "lightning-input", "is_component_host": false, "aura_attributes": {"data-aura-rendered-by": "1:0"}}` — the Lightning component the element belongs to (its own tag, or the shadow host that rendered it) and its `data-aura-*` attributes |

An element outside any control or component has `platform_signal: null`. The run's manifest records
`platform_detected` (the first platform that put a signal on an element). Turn it all off with
`"platform_detection": "off"` in `crawl.extraction` or `platform_adapters=()` on a driver.

Limits, stated plainly: the signal is recorded on the element and its fingerprint, but the healer's
scorer does not weight it. The SAP adapter was checked by hand against a real OpenUI5 runtime; the
suite checks both adapters against stubs shaped like the real APIs, since real SAPUI5 and Salesforce
orgs are not something a test-suite can load. **The Salesforce adapter has not been run against a real
org, so it is not claimed as supported.** [Checking it against yours](#checking-the-salesforce-adapter-against-your-org)
takes a few minutes. Custom adapters (`healix.platform_adapters.PlatformAdapter`, two snippets of JavaScript) must be
valid JavaScript: it runs in the page next to the collector.

### Iframes, shadow DOM, and stable IDs

These three are generic core capabilities, not per-vendor code.

| Capability | Behavior |
|---|---|
| Iframes | The frame tree is walked recursively. Same-origin frames are merged into the page's elements, each tagged with its `iframe_path`. Cross-origin frames (and everything beneath them) are listed by `get_frames()` with `same_origin=False` and skipped; `skipped_frames()` says which frames a read left out and why, and the page output records them |
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
    "nearby_label_text": null,
    "tag_path": ["html", "body", "div", "p", "a"]
  },
  "shadow_path": []
}
```

Notes on individual fields:

- `attributes` holds every attribute except `id`, `class`, and `name`, which have their own fields.
- `text_content` is the element's **own** text nodes only, so parents don't repeat their children's text.
- `dom_context.tag_path` is the ancestor tag names from the root down to the element. Unlike
  `xpath` and `css_selector` it is not shortened by an `id` anchor, so it still says where an
  element sits — the healer uses it.
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

| `click_discovery` | `false` | Also click buttons and script links that have no `<a href>`, to find pages only script can reach. See [Click-through discovery](#click-through-discovery) |
| `max_clicks_per_page` | `15` | Most clicks on any one page |
| `max_clicks` | `100` | Most clicks in the whole run |
| `click_deny` | `[]` | Extra words that mark a control as never to be clicked (added to the built-in list) |

`template_sample_size` exists so a family of look-alike pages can't use up `max_pages` ahead
of distinct pages. With 200 product links and `max_pages=50`, `/about` still gets visited.

### Click-through discovery

Some navigation is only script: a `<button>` that calls `history.pushState`, a
`<div role="button">` that sets `location`, an `<a href="#">` with a click handler. There is no
link to follow, so ordinary discovery cannot see the page behind it. Turn on
`crawl.discovery.click_discovery` and Healix clicks such controls and notes where the page ends up:

```json
{ "crawl": { "discovery": { "click_discovery": true, "max_clicks": 50 } } }
```

It is **off by default**, and a run without it behaves exactly as before. Clicking things on a live
site can do harm, so what it does *not* do matters more than what it does:

- **It never submits a form.** A `<button>` inside a form (unless `type="button"`), an
  `<input type="submit|image|reset">`, and anything with a `formaction` are skipped.
- **It never clicks anything that looks like it commits something.** The label, `aria-label`,
  `title`, `id`, classes, `data-testid`, `href` and `onclick` are checked, and the text inside the
  control too, so `<button><span>Delete</span></button>` and an icon-only trash button with
  `class="btn-danger"` are both caught. The words are in `healix.discovery.clicks.DENY_WORDS`:
  delete, remove, pay, purchase, sign out, log out, submit, send, save, confirm, cancel, reset,
  publish, transfer and so on. Add your own with `click_deny`. **It errs towards skipping:** words
  match at the start of a word, so a "Payments" menu is skipped with "Pay now". The cost of a missed
  page is lower than the cost of a click that deletes something.
- **The page cannot send data while it is clicked.** A best-effort guard is injected into each page
  (Playwright and Selenium alike): `fetch` and `XMLHttpRequest` with any method but GET, HEAD or
  OPTIONS, beacons and form submissions do nothing and are counted. It is a safety net, not a
  sandbox. It cannot stop a GET request that has a side effect, a WebSocket message, a request from
  a cross-origin frame, or a page that saved its own reference to `fetch` before the guard ran.
- **It stays in scope.** A click that leads outside `domain_scope` is ignored.
- **It is bounded.** `max_clicks_per_page` and `max_clicks` cap it. A control repeated down a list
  (`Edit`, `Edit`, `Edit`, …) is clicked once. Clicks are not page visits, so they do not count
  against `max_pages`.

Each click starts from a fresh load of the page, so one click cannot change what the next finds. That
makes a run with many clicks slow, and a navigation bar that repeats on every page is clicked on
every page: keep the caps tight and add the noisy controls to `click_deny`.

A page found this way says so in the manifest (`"discovered_via": "click"`, `"discovered_from":
"<the page whose button led here>"`), and the manifest records what happened overall:

```json
"click_discovery": { "clicks": 12, "pages_found": 4, "skipped_unsafe": 5, "blocked_writes": 1 }
```

`skipped_unsafe` is how many controls were left alone for being unsafe, and `blocked_writes` how many
requests the guard stopped. A non-zero `blocked_writes` means a button you did not expect to write
did try to. Both keys are left out of the manifest when the option is off.

A found route is later extracted from a **fresh navigation** like every other page, so it has to load
when opened directly. A single-page app whose server only answers `/` will record such a route as
`failed`, which is honest: the page cannot be reached without the click. Routes in the URL hash
(`#/team`) work, since `normalize_url` keeps them.

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
  "blocked_on_auth": false,
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

- `blocked_on_auth` is `true` when a login page was reached but no credentials were set; the
  pages behind it are then recorded as `failed` (see [Authentication](#authentication)).
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
| `platform_detection` | `"auto"` | `auto`: the [platform adapters](#platform-adapters) may add `platform_signal`, and the manifest records `platform_detected`. `off`: neither |
| `settle_quiet_ms` | `null` | How long a page must stay unchanged (no new resources, no new elements) before it is read. `null` keeps each backend's own behaviour: Selenium waits 500 ms, Playwright waits for network idle only. Set it (for example `2000`) for client-rendered sites that render after network idle. It applies to discovery too, and is bounded by the driver's `settle_timeout_ms`. See [The `Driver` abstraction](#the-driver-abstraction) |

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

`final_url` is added only when the page redirected. `skipped_frames` is added only when a frame's
elements are missing from `elements`: a list of `{"path": ["main", "ads"], "url": "…", "reason": "…"}`
with `reason` one of `cross_origin` (the frame has another origin, so a script cannot read it),
`inside_cross_origin_frame` (it has the page's origin, but sits inside a cross-origin frame, so it
cannot be reached either) and `unreadable` (it could be reached but reading it failed, for example
because it navigated away mid-read). A page without the key had nothing skipped. `page_type` and `structural_hash` are from
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
| `login` | A single password input plus a submit control and few other fields, with a sign-in cue in the URL or a heading; or an OAuth/SSO redirect URL, or an app sign-in page that offers only SSO (an SSO button plus a `/login`-style URL or a "Sign in" heading). Sign-up cues count against it |
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
result.page_type  # "list"
result.confidence  # 0.9
result.scores  # {"login": 0.15, "dashboard": 0.15, "list": 0.9, "detail": 0.1, "form": 0.45,
#  "search": 0.0, "checkout": 0.0, "nav_shell": 0.7, "modal": 0.0}
result.signals["list"]  # ["repeating_rows", "pagination", "few_fields"]
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

configure_logging(level="info")  # just change the level
configure_logging(transports=[FileTransport("healix.log")])  # send records elsewhere
configure_logging(transports=[])  # silence Healix entirely
```

Levels: `info` records discovery start and finish, `debug` adds every discovered page, every
classification with its scores, redirects, and skipped frames; `warn` reports pages that
failed to load and frames that couldn't be read. Any logquill transport or plugin works —
see the [logquill docs](https://github.com/nikhilvdev/logquill-python).

In your own code around Healix, `from healix.log import get_logger` gives you a logger under
the same configuration. `configure_logging` updates every Healix logger, including ones
created before it was called.

## Design decisions

The full picture of how the package is built (layers, data flow, the healing algorithm, script
generation, extension points) is in [ARCHITECTURE.md](ARCHITECTURE.md).

These are fixed unless explicitly reopened:

- **Pure Python.** No second implementation language and no compiled core. Cross-language
  consumers are served by the CLI and webhook boundary.
- **Rule-based classification, no LLM calls** anywhere in the crawl/classify/extract path —
  deterministic, reproducible runs with no external API cost.
- **Secrets live in `.env` only.** Never in the run config, which must always be safe to
  commit — the config parser enforces this by rejecting credential-looking keys. Copy
  [`.env.example`](.env.example) to `.env` (git-ignored).
- **Login is a page classification**, not a separate config step. A page classified `login`
  is handed to a login handler that applies `.env` credentials. It aborts on MFA, submits a
  password at most once per attempt, and gives up rather than retry — see
  [Authentication](#authentication).
- **Platform adapters are additive.** The SAP UI5 and Salesforce LWC adapters only add signal on
  top of the generic pipeline, which always runs on its own.
- **Healing must reject weak matches.** Attributes are weighted (stable signals over volatile
  ones), and a best-but-weak candidate below the confidence threshold is rejected, not used.

## Known limitations

- **Canvas-rendered UI is unsupported.** Some Dynamics 365 canvas apps and legacy Java-applet
  Oracle Forms aren't in the DOM at all, so no locator strategy can reach their elements.
  This is a hard boundary, and the healer is designed to fail cleanly on it.
- **Closed shadow roots** are not reachable.
- **Cross-origin iframes** are not extracted, and a same-origin frame nested inside a
  cross-origin one is skipped too. Neither is dropped silently: the page's `skipped_frames`
  names each one and the reason.
- **Extraction reads a page from a fresh navigation.** Client-side state and filled-in forms are
  not reproduced. The browser keeps its login session across the run, but with `--no-login`, no
  credentials, or a failed login, a page that redirects to a login page is recorded as `failed`.
- **Script-only navigation** — buttons and router pushes with no `<a href>` — is invisible
  to discovery unless you turn on [click-through discovery](#click-through-discovery). That is
  off by default, skips anything that looks unsafe (so it misses some real navigation), cannot
  fully sandbox a page, and is slow.
- **Client-rendered sites** that render after network idle, or never go idle, may be read before
  they finish rendering, and pages with an identical (e.g. empty) structure would then collapse
  into one manifest entry. Set `crawl.extraction.settle_quiet_ms` to wait for the page to stop
  changing; it is off by default on Playwright, and it cannot see a request still in flight.
- **Structural dedup is coarse by design.** Two different pages whose elements produce the
  same signatures are merged. Use `dedupe_by="url_normalized"` to turn it off.
- **Webhook delivery is best-effort by default**; `--webhook-outbox` makes it durable and
  at-least-once (not exactly-once) — see [Durable delivery](#durable-delivery).
- **Generated scripts need Healix at runtime** — that is what makes them self-healing — and do not
  log in by themselves. See [Script generation](#script-generation) for what they do and do not do.
- **Healing is not a substitute for a test.** A healed element is *probably* the same one; it can be
  wrong. That is why heals are confidence-gated and audited — read the [regression
  flags](#the-healing-history) — and why a heavy refactor heals only just above the threshold.
- **Classification is heuristic** — see [Accuracy and limits](#accuracy-and-limits).
- **Multi-role runs** compare what each role can reach and see; they do not know what a role should
  be allowed to, run roles one after another, and share one fingerprint baseline across roles — see
  [Multi-role runs](#multi-role-runs).
- **Login covers the common shapes only** — see [What it will not do](#what-it-will-not-do). A
  login that lives in a pop-up window or a cross-origin iframe is not handled. Login detection
  relies on the classifier, so a login page it does not recognise is treated as an ordinary page.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| 1 | `Driver` ABC, Playwright adapter, iframe + shadow DOM traversal, ID normalization | ✅ Done |
| 2 | Discovery, manifest, dedup, per-page status | ✅ Done |
| 3 | Rule-based page classification (`login`, `dashboard`, `list`, `detail`, `form`, `search`, `checkout`, `nav_shell`, `modal`), and logquill-based logging | ✅ Done |
| 4 | Sequential extraction — one JSON file per page, resumable | ✅ Done |
| 5 | SDK (`Crawler`, `Extractor`), CLI (`crawl`, `extract`), event schema and webhooks | ✅ Done |
| 6 | Auto-detected login with `.env` credentials, SSO, MFA abort, mid-crawl re-login | ✅ Done |
| 7 | Self-healing: fingerprints, weighted scorer, confidence threshold, persistent store and history | ✅ Done (SQLite and PostgreSQL stores) |
| 8 | Selenium adapter, SAP UI5 and Salesforce LWC platform adapters | ✅ Done |
| — | Script generation: `ScriptGenerator`, `healix generate`, the `script_generated` event | ✅ Done |
| 9 | Packaging (extras, `.env.example`, MIT licence, README), `healix doctor`, PyPI release | ✅ Done |
| 10 | Release gate, opt-in Playwright quiet window, skipped-frame reporting, Firefox in CI | ✅ Done (1.1) |
| 11 | Durable webhook delivery: an outbox, `healix flush-events`, a delivery id | ✅ Done |
| 12 | Opt-in click-through discovery with a write guard | ✅ Done |
| 13 | Multi-role runs and the comparison between roles | ✅ Done |
| 14 | Validation on real sites, the locator-hit fix, release 2.0 | ✅ Done. The Salesforce adapter is still unverified against a real org |

Releases go out from a version tag (`v*`) through the `release` workflow, which publishes to PyPI
with trusted publishing. Before anything is built it checks that the tag matches the version in
`pyproject.toml` and `healix.__version__` and that the changelog has an entry for it, and it runs the
full CI suite on the tagged commit; a failure in any of them stops the release.


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
Playwright or its browsers aren't installed. Most of them run once per backend, so Selenium tests
need Chrome. The generation tests build scripts from a real crawl and run them, on both backends.
Set `HEALIX_TEST_SELENIUM_BROWSER=firefox` to run the Selenium tests on Firefox (CI has a
non-blocking job that does); asking for a browser by name means a launch failure fails the test
instead of skipping it.

The PostgreSQL store tests need a real server. Set `HEALIX_TEST_POSTGRES_URL` to point at one (CI
does, with a service container), or just have Docker running: the tests start a throwaway
`postgres:16-alpine` container and remove it afterwards. With neither, they skip.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR workflow, the
[Code of Conduct](CODE_OF_CONDUCT.md) for community standards, and
[SECURITY.md](.github/SECURITY.md) for how to report a vulnerability.

## License

[MIT](LICENSE)
