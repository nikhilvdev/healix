# Changelog

All notable changes to this project are documented in this file.

## Unreleased

Three opt-in additions: multi-role runs, click-through discovery, and durable webhook delivery.
Without them, a run behaves exactly as before.

### Added
- **Multi-role runs** (`"roles": ["admin", "standard"]` in the run config). The site is crawled once
  per role, each with its own credentials (`WEBLIB_LOGIN_USERNAME_<ROLE>` /
  `WEBLIB_LOGIN_PASSWORD_<ROLE>`, from the environment only; the config holds names, never secrets)
  and in a fresh browser, so no role's session reaches another's. Each role gets its own manifest and
  page files under `output/roles/<role>/`, and `output/roles-diff.json` says which pages and which
  visible, actionable elements each role can reach. `anonymous` is a reserved role that never logs
  in. New: `RoleCrawler` and `MultiRoleRun` (`Crawler` refuses a config with roles, so its return
  type never changes), `Extractor(role=…)` (defaults to the manifest's own role), `healix crawl
  --role NAME` and `healix extract --role NAME`, and `Credentials.from_env(role=…)`.
- **`roles_compared` event** (`roles`, `diff_path`, `differences`), emitted once at the end of a
  multi-role run. In a multi-role run every event also carries `"role"` in the envelope; a run without
  roles has no such key, so its payloads are unchanged.
- Manifests of a role carry `"role"`; it is left out of a manifest without one.
- **Click-through discovery** (`crawl.discovery.click_discovery`, off by default). Clicks the buttons and
  script links that have no `<a href>` to find pages that only script can reach, such as
  single-page-app routes. It never submits a form and never clicks anything that looks like it deletes,
  pays, signs out or saves (`click_deny` adds words), and while it clicks the page cannot send data: a
  best-effort guard, on both backends, stops `fetch` and `XMLHttpRequest` other than GET, beacons and
  form submission. It is bounded by `max_clicks_per_page` (15) and `max_clicks` (100), stays in
  `domain_scope`, and does not count clicks against `max_pages`. A page it finds says so in the
  manifest (`discovered_via`, `discovered_from`), and the manifest gains a `click_discovery` summary
  (`clicks`, `pages_found`, `skipped_unsafe`, `blocked_writes`). Both are left out when the option is off.
- `Driver.guarded()` and `Driver.blocked_writes()`: the write guard, available to any caller.
- **`webhook_outbox=`** on `Crawler`, `Extractor`, `Healer` and `ScriptGenerator`, and
  **`--webhook-outbox PATH`** on `crawl`, `extract` and `generate`. Every event is written to a SQLite
  file before it is sent and removed only once the receiver accepts it, so a receiver that goes down
  mid-run, or is still down when the run ends, loses nothing. Events are delivered oldest first; a
  failing event is retried before any later one is sent; a receiver that rejects an event (a 4xx
  other than 408 and 429) has that one set aside and the rest go out. What a run leaves in the file
  is sent first by the next run that opens it.
- **`healix flush-events --outbox PATH --webhook-url URL`** sends what is waiting, with `--timeout`,
  `--retry-rejected` and `--json`. It exits `0` when the outbox is empty, `1` when events are still
  waiting and `3` when the receiver rejected some.
- **`X-Healix-Delivery`** header on every webhook request: an id that is the same for every attempt at
  one event, so a receiver can drop a repeat. It is a header, so the payload is unchanged and still
  identical to what `on_event` receives.
- `healix.events`: `DurableWebhookSender`, `Outbox`, `OutboxError`, `open_sender`, `EventSender`.

### Notes
- Multi-role fingerprints stay keyed by `(page_url, element_role)`: the first role to see an element
  records it, and `fingerprint_mode="refresh"` is refused for a multi-role run so the last role cannot
  overwrite the rest. Roles run one after another. A role is a set of credentials, not a permission
  model: the diff reports what was reached, not what should have been.
- Click-through discovery errs towards skipping, so it misses some real navigation, and it is slow: each click loads the page again. Keep the caps tight.
- Durable delivery is at least once, not exactly once, and expects one sender per outbox file.
- After a run, a note on stderr says how many events are still waiting in the outbox.

## 1.1.0 — 2026-09-20

Existing configs, manifests, event payloads and generated scripts keep working, and every new
setting is optional with a default that keeps 1.0 behaviour. The one visible difference is the new
`skipped_frames` key in a page file whose page had a frame that could not be read.

### Added
- **`skipped_frames` in the page output.** A frame whose elements are missing from a page — a
  cross-origin frame, a frame inside one, or a frame that could not be read — is now listed with its
  `path`, `url` and a `reason` (`cross_origin`, `inside_cross_origin_frame`, `unreadable`) instead of
  being dropped silently. The key is present only when something was skipped. On the driver side,
  `Driver.skipped_frames()` returns the same list for the latest `get_elements()`, and `SkippedFrame`
  is exported from `healix.driver`.
- **A quiet-window wait for Playwright**, for client-rendered sites that render after network idle
  (from a timer, or from a script of their own). `PlaywrightDriverAdapter(quiet_ms=…)` waits for no
  new resources or elements for that long, inside `settle_timeout_ms`. It is off by default.
- **`crawl.extraction.settle_quiet_ms`** in the run config, and `quiet_ms=` on `create_driver`, set
  that window on either backend from one place. `null` (the default) keeps each backend's own
  behaviour. It applies to discovery as well as extraction.

### Changed
- **The release workflow gates on the tests.** Before anything is built it checks that the tag matches
  the version in `pyproject.toml` and `healix.__version__` and that this changelog has an entry for
  it, and it runs the whole CI suite on the tagged commit. A failure in any of them stops the release.
- Selenium's quiet-window wait now shares one implementation with Playwright's
  (`healix/driver/settle.py`). Its behaviour is unchanged.
- CI has a Selenium-on-Firefox job. It does not block merges or releases until it has a green
  history, so Firefox is still not a supported Selenium browser.

### Fixed
- A timing-sensitive test of Selenium's quiet window failed on slow machines. The behaviour it covers
  is unchanged.

## 1.0.0 — 2026-09-20

The first release: discovery, classification, extraction, login, self-healing, the Selenium and
Playwright backends, the SAP UI5 and Salesforce LWC platform adapters, script generation and
`healix doctor`, behind one SDK, CLI and event schema. See the README's *Known limitations* for what
it does not do; in particular the Salesforce adapter has not been run against a real org, and only
Chrome is tested for Selenium.

### Added

- **Script generation.** `ScriptGenerator` (`healix.ScriptGenerator`) and `healix generate` turn
  the extracted pages of a crawl into a script for Playwright or Selenium, in one of three styles:
  `pom` (a page-object class per page), `test` (a `pytest` module that asserts each page's address
  and that every element is found, visible and enabled) and `action` (fill the inputs, click
  submit). Every step goes through `Healer`, so the script heals when the page changes; generating
  also records the fingerprints it heals against (`--fingerprint-db`, `--no-record`). Login values
  come from `WEBLIB_LOGIN_USERNAME` / `WEBLIB_LOGIN_PASSWORD`, a Postgres URL is read from
  `HEALIX_FINGERPRINT_DB` rather than written into the file, and page text is emitted as quoted
  data. `generate` warns when the database a script points at has no fingerprints for its pages.
  The `script_generated` event is now emitted (one per script). New runtime dependency:
  `jinja2`. Templates live in `healix/generation/templates/`.
- **`healix doctor`.** Checks the Python version, the dependencies, each backend's package and
  browser, the optional Postgres driver and whether login credentials are set (never their values);
  `--launch` opens each usable browser and reads a page; `--json` for machines. Exits `0` when at
  least one backend is usable, `1` when none is. The probes live in the adapter modules
  (`diagnose()`), so only they import a browser library.

- **Selenium backend.** `SeleniumDriverAdapter` (`healix.driver.selenium_adapter`), selected with
  `"backend": "selenium"` or `Healer(backend="selenium")`; install with `healix[selenium]` (or
  `healix[all]`). It implements the same `Driver` interface, so crawling, extraction, login and
  healing run unmodified. Shadow roots and frames go through the same in-page scripts as Playwright,
  so both backends extract identical elements. Because WebDriver has no network-idle or auto-wait,
  the adapter settles pages by watching resources and elements (`quiet_ms`), retries `click` and
  `write` while an element is not ready (`action_timeout_ms`), and raises when a browser shows an
  error page instead of failing. Only Chrome is tested. The shadow walk is in-page rather than one
  `getShadowRoot()` call per host.
- **Platform adapters.** `healix.platform_adapters`: `sap_ui5` (UI5 control id, type and root flag
  from `sap.ui.getCore().byId()`) and `salesforce_lwc` (Lightning component tag and `data-aura-*`
  attributes) add `platform_signal` to elements, only where their platform is detected. Additive
  only: nothing depends on them firing. `crawl.extraction.platform_detection` (`auto` / `off`) now
  takes effect, and the manifest's `platform_detected` records the platform seen.

- **Self-healing.**
  - `Healer` (`healix.Healer`): `learn` records fingerprints, `resolve` / `click` / `write` find an
    element again after the page changes, `history` and `report` expose the audit trail. A context
    manager that owns its browser and store unless you pass them; emits `element_healed` through
    `on_event` and webhooks.
  - Resolution order: a fingerprint's locators in priority order (stable attributes → id → name →
    aria-label → css → xpath → normalized id → text); then, if none survive, **weighted scoring**
    of every same-tag element. The best candidate is accepted only at or above the confidence
    threshold (default 0.5) and only if it is not a near-tie with the runner-up
    (`ambiguity_margin`, default 0.05). A weak or ambiguous match raises `ElementNotHealedError`;
    it is never silently used.
  - A locator that matches uniquely but is not the first *trusted* one tried (a later strategy, a
    positional css/xpath, or text) is verified against the fingerprint and rejected if it does not
    resemble the stored element.
  - `healix.healing.scorer`: stable signals outweigh volatile ones (test id 0.25 … sibling index
    0.02). A signal missing on either side is neutral; a different value is a mismatch; confidence is
    damped by how much evidence could be compared, so sparse fingerprints cannot heal confidently.
  - `FingerprintStore` (abstract), `SQLiteFingerprintStore` (default) and
    `PostgresFingerprintStore`, keyed by `(page_url, element_role)`. A heal updates the fingerprint
    and appends to the history in one transaction. Both are thread-safe and refuse a schema from a
    newer Healix.
  - **PostgreSQL store** (`pip install 'healix[postgres]'`, `psycopg` 3): `healix_`-prefixed tables,
    `JSONB` columns so the audit trail is queryable in SQL, insertion order preserved, and schema
    creation under an advisory lock so concurrent starts are safe. The connection URL's password is
    never logged and is masked in `repr` and in error messages, even if a driver echoes it.
  - `open_store(target)`: a store, a `postgresql://` URL, or a SQLite path. It is what `Healer`,
    `Crawler(fingerprint_store=...)`, and `healix crawl --fingerprint-db` use, so all of them accept
    either backend. One contract test suite runs against both, against a real PostgreSQL server.
  - The healing history: every heal records old/new fingerprints and locators, strategy,
    confidence, changed fields, and a verdict — `churn` (handles moved) or `regression` (the
    element's visible meaning changed). `churn_report` ranks elements by how often they heal.
  - **Canvas boundary:** healing on a page that is essentially one canvas/plugin surface raises
    `UnsupportedRenderingError` instead of guessing.
  - Baselines are recorded during extraction: `Crawler` / `Extractor` take `fingerprint_store` and
    `fingerprint_mode` (`keep`, the default, never overwrites a baseline; `refresh` does), and the
    CLI has `--fingerprint-db PATH`. Roles are stable names such as `textbox:login-username` or
    `button:sign-in`, with repeats numbered `#2`.
  - `Driver.locate(spec, fingerprint)`: resolves one locator strategy, so the healer can see which
    strategy matched; `find` is now built on it. Implemented for Playwright.
  - `dom_context.tag_path` on every extracted element: ancestor tag names from the root, not
    shortened by an id anchor (an id-anchored xpath carries no path).

- **Automatic login.**
  - `healix.auth.LoginHandler`: when discovery or extraction lands on a page classified `login`, it
    fills and submits the form with credentials from `WEBLIB_LOGIN_USERNAME` /
    `WEBLIB_LOGIN_PASSWORD` (or `Crawler(..., credentials=Credentials(...))`), then the crawl
    continues. Handles a username + password form (also in iframes and shadow roots), a
    single-sign-on redirect or POST-back including identifier-first providers, and **session
    expiry**: a page that bounces to a login after logging in triggers a new login, the page is
    loaded again, and the crawl resumes. Works during both discovery and extraction, in guided and
    autonomous mode.
  - Safe by construction: MFA is detected and aborted (`login_failed`, `mfa_required`); a password
    is never submitted twice in one attempt (a repeat is `auth_rejected`); any failure ends login
    for the rest of the run; re-logins are capped at three; every login has a 30 s deadline and a
    step limit. Credentials are typed only on in-scope pages or on host/path-recognised SSO
    endpoints — never on the strength of a query string — and only into a form with exactly one
    password field.
  - `login_failed` is now emitted: `url` (no query string), `reason`, and `screenshot_ref`, a
    screenshot saved under `<output>/screenshots/`.
  - **Blocked on auth.** With no credentials set, a login page flags the run `blocked_on_auth`
    (manifest field, `Run.blocked_on_auth`, `--json` output), logs one warning, and records the
    pages behind it as `failed`. No `login_failed` event is raised for this, since its reasons
    describe an attempted login. Resuming a blocked run rediscovers.
  - `Credentials` never shows its values in `repr`/`str`; credentials are never logged, evented, or
    written to disk. A test crawls with a distinctive password and checks it and the username are
    absent from every log record, event, summary, manifest, and output file.
  - CLI: `--no-login`, and exit status `4` for a run blocked on auth. SDK: `credentials=` and
    `auto_login=` on `Crawler` and `Extractor`.
  - `Driver.settle()`: a no-op hook adapters can override to wait for in-flight navigation
    (implemented for Playwright; `navigate` now uses it).
  - Classification: an app sign-in page that offers only SSO is now a `login` when it has a
    `/login`-style URL or a "Sign in" heading. An SSO button alone, or the button's own "sign in"
    wording, is not enough. `is_oauth_url` (host and path only) is public in
    `healix.classification`.
  - `Scope` (which URLs a crawl may visit) is now public in `healix.discovery.crawler`.

- **SDK, CLI, and webhooks on one event schema.**
  - `Crawler` (`discover()`, `discover_and_extract()`) and `Extractor` (`extract()`), exported from
    `healix`, each returning a `Run` (`run_id`, `pages`, counts, `manifest_path`, `summary()`).
    They accept a run config as a path, a dict, or a `RunConfig`, plus `on_event`, `webhook_url`,
    `webhook_secret`, a caller-supplied `driver`, and `headless`.
  - `run_id` semantics: re-running with the same id over the same output directory resumes it
    (finished discovery is reused; extraction continues from the first page not `extracted`). A
    run without an id over a directory that already holds a run raises `RunConflictError` instead
    of overwriting it.
  - The `healix` command: `healix crawl` (with `--output`, `--run-id`, `--discover-only`,
    `--webhook-url`, `--headed`, `--json`, `--log-level`) and `healix extract`, also available as
    `python -m healix`. Exit codes: 0 ok, 1 runtime error, 2 usage/config error, 3 some pages
    failed, 130 interrupted. The CLI goes through the SDK, so it emits exactly the SDK's events.
  - `healix.events`: the shared schema for all six event types (`page_discovered`,
    `page_extracted`, `element_healed`, `script_generated`, `run_complete`, `login_failed`) with
    validated `data` shapes, an emitter, and `WebhookSender`. `on_event` and `--webhook-url`
    receive identical payload dicts. Emitted today: `page_discovered`, `page_extracted`,
    `run_complete`; the others are defined for the features that will emit them. A test keeps the
    README event table in sync with the schema.
  - Webhook delivery runs on a background thread so a slow endpoint never stalls a crawl, retries
    network errors/5xx/408/429 with exponential backoff, signs the body with HMAC-SHA256
    (`X-Healix-Signature`) when `HEALIX_WEBHOOK_SECRET` is set, and never logs the URL (only its
    host). A failing `on_event` callback or webhook is logged and never fails the run.
  - `RunConfig`: the run-config JSON, validated. Unknown keys are errors, `mode` is inferred, and
    **credential-looking keys (`password`, `secret`, `token`, `api_key`, `credential…`,
    `username`) are rejected at any depth** with a pointer to `.env`, enforcing that the config
    is always safe to commit.
  - `healix.driver.factory.create_driver` and `BackendUnavailableError` (Playwright missing, or the
    not-yet-implemented `selenium` backend).
  - `.env.example` gains `HEALIX_WEBHOOK_SECRET`.

- **Sequential, resumable extraction.**
  - `ElementExtractor` in `healix.extraction` walks the manifest one page at a time — navigate,
    wait for load, extract every element at full detail, write the page's JSON, mark the entry
    `extracted` — and writes `output/manifest.json` plus `output/pages/NNNN-<slug>.json`.
  - Page JSON: `schema_version`, `run_id`, `url` (and `final_url` on redirect), `page_type`,
    `structural_hash`, `captured_at`, `element_counts` (total, visible, in shadow roots, by tag,
    by frame) and `elements`. Files are written atomically.
  - Resumable: the manifest is saved after every page. Re-running continues from the pages that
    are not `extracted`; failed pages are retried; an `extracted` page whose output file is
    missing is extracted again. The file is written before the manifest records it.
  - A page that fails to load or read is marked `failed` with its error and the run continues.
  - Each page is re-classified from its fresh elements, so the manifest's provisional
    discovery-time `page_type` becomes the final one. Structure and type drift since discovery
    are logged at `info`.
  - `ExtractionConfig` (the `crawl.extraction` run-config block: `sequence`, `output_format`,
    `output_path`, `iframe_traversal`, `platform_detection`) and an `on_page_extracted` hook
    carrying the `page_extracted` event fields. `platform_detection` is validated but inert
    until the platform adapters land.
  - `healix.fs.write_json_atomic`, now also used by `Manifest.save`.

- **Rule-based page classification.**
  - `classify(elements, url)` and `classify_page(elements, url)` in `healix.classification`
    label a page `login`, `dashboard`, `list`, `detail`, `form`, `search`, `checkout`,
    `nav_shell`, `modal`, or `unknown`. Deterministic element-count and attribute heuristics
    — no LLM calls, no network.
  - Each type is a rule awarding weighted signals; a type is chosen at score ≥ 0.5, near-ties
    (within 0.15) go to the more specific type by a fixed priority order, and below 0.5 the
    answer is `unknown`. `classify_page` returns the scores and the signals that fired, plus
    a `confidence`.
  - Only visible elements vote. Guards against the common false positives: a registration
    form is not a login, a header search box is not a search page, a link-heavy product grid
    is not a `nav_shell`, a form-like settings menu on a content page is not a form, and a
    thin cookie banner or toast is not a `modal`.
  - Covered by unit tests, by realistic rendered pages for every type run through Chromium,
    and by a test that keeps the README's page-type table in sync with the code.
- **Structured logging on logquill.** `healix.log` provides `get_logger(__name__)` and
  `configure_logging(level=, transports=)`. Records are a constant message plus metadata.
  Default is `WARN` and above as JSON lines on stderr; `HEALIX_LOG_LEVEL` overrides the level;
  `configure_logging` updates every logger already handed out (logquill's `child()` copies
  its parent's transports, so loggers created at import time would otherwise go stale).
  Discovery now logs start and finish at `info`, and each discovered page at `debug`.
- `computed.position` and `computed.z_index` on every extracted element.

- **Driver abstraction.**
  - `Driver` ABC (`navigate`, `find`, `click`, `write`, `get_elements`, `get_frames`,
    `screenshot`, plus `current_url` and optional `start`/`close` lifecycle hooks) and the
    `Element`, `Frame`, and `ElementNotFoundError` types.
  - `PlaywrightDriverAdapter` (sync API): launches and owns its own browser, or wraps a
    caller-supplied `Page`.
  - Generic iframe traversal (`driver/frames.py`): the frame tree is walked recursively,
    same-origin frames are merged, and every element carries an `iframe_path`
    (`["main", "workspace_panel", "form_frame"]`). Cross-origin frames, and everything
    beneath them, are reported with `same_origin=False` and skipped. Sibling frame labels
    are made unique so a path identifies exactly one frame.
  - Generic open-shadow-DOM piercing, recursive through nested roots, with a `shadow_path`
    listing each host's selector.
  - Raw element extraction at maximum detail: tag, id, name, classes, all other attributes,
    own text, computed state (visibility, enabled, bounding box, checked, selected,
    readonly, required, focused), xpath, css selector, and DOM context. Input values are
    intentionally not captured.
  - Stable-ID normalization (`healix/ids.py`): numeric runs, UUIDs, long hex fragments, and
    colon-delimited positional prefixes such as `pt1:r1:0:…`.
  - `Fingerprint` with ordered primary locators (stable attributes → id → name → aria-label →
    css → xpath → normalized id → text); `driver.find(fingerprint)` resolves through them and
    rejects ambiguous matches.
- **Discovery and manifest.**
  - `DiscoveryCrawler`: a link walk from one or more start URLs with no depth cutoff and a
    `max_pages` safety ceiling counted in page visits.
  - `DiscoveryConfig` (`domain_scope`, `max_pages`, `dedupe_by`, `template_sample_size`).
    `template_sample_size` defers — never drops — extra URLs from an already-sampled path
    template so a family of look-alike pages can't use up `max_pages` ahead of distinct pages.
  - `Manifest` / `ManifestPage`: dedup by normalized URL and structural hash (a structural
    match is recorded as a `variant_urls` entry, and its links are still followed),
    `pending | extracted | failed` status tracking, `remaining_pages()` as the resume set,
    and atomic JSON save/load.
  - `normalize_url` (session/tracking parameter stripping, sorted query, hash-route
    fragments kept), `template_key`, and `structural_hash`.
  - Pages that fail to load, or whose elements can't be read, are recorded as `failed` and
    the crawl continues; `manifest_path` is written even when discovery is interrupted.
  - Optional `classifier` and `on_page_discovered` hooks for the classification and event
    work.
- Packaging and project scaffolding: `pyproject.toml` (hatchling, `playwright`/`dev`/`docs`
  extras, `py.typed`), CI, release and docs workflows, issue and PR templates, dependabot,
  pre-commit, `.env.example`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, and
  the MIT `LICENSE`.

### Changed

- `create_driver` takes a `platform_detection` keyword (`"auto"` / `"off"`), and the `selenium`
  backend now works instead of raising "not implemented". `PlaywrightDriverAdapter` gains a
  `platform_adapters` argument; `healix.driver.frames` builds its in-page scripts through
  `build_collect_js` / `build_describe_js` (`COLLECT_ALL_JS` / `DESCRIBE_ONE_JS` remain).
- The browser integration tests (discovery, extraction, classification, healing, login) and the
  driver contract tests now run once per backend, and further tests compare Playwright and Selenium
  side by side.
- `Fingerprint` gains `shadow_path`, `to_dict` / `from_dict` (unknown keys ignored), `key` and
  `element_key`. `healix.healing` exports its names lazily, to avoid an import cycle with the driver.

- `Manifest` gains `blocked_on_auth` (default `false`; older manifests load unchanged), and
  `Run.summary()` gains the same key.

- **`Driver.get_elements()` takes a keyword-only `iframe_traversal: bool = True`.** With `False`
  only the main frame is read (open shadow roots are still pierced). Custom `Driver`
  implementations must accept the keyword.
- `Manifest.save` writes non-ASCII characters as-is instead of `\uXXXX` escapes.
- A lone "Next" *link* now counts as list pagination (page 1 of a list has no "Previous"); a lone
  "Next" *button* still does not.

- **New runtime dependencies:** `logquill>=1.0` (itself dependency-free) and
  `python-dotenv>=1.0` (the CLI loads a `.env` from the current directory). Healix previously
  had none. The package now declares a `healix` console script.
- `DiscoveryCrawler` classifies every page by default with `healix.classification.classify`.
  The `classifier` hook now takes `(elements, url)` instead of `(elements)`; pass
  `classifier=None` to skip classification.
- Stdlib `logging` is no longer used anywhere in Healix; all log calls go through logquill.

- `PlaywrightDriverAdapter.navigate()` now also waits, best-effort and bounded by
  `settle_timeout_ms` (default 3000; `0` disables), for the network to go idle after `load`,
  so client-rendered pages have content before it is read.
- Extracted link elements report the browser-resolved absolute URL in `computed.href`
  (honoring `<base href>`).

### Fixed

- **`PlaywrightDriverAdapter.navigate()` could fail on a good page right after a failed one.**
  Chromium can still be committing the failed load's error page when the next navigation starts,
  and reports "interrupted by another navigation". A crawl would then have marked the next, healthy
  page as failed too. The navigation is now retried (up to three attempts) on that specific error;
  a page that really fails still raises its own error at once.

- **A password field's markup `value` attribute was captured verbatim** into an element's
  `attributes`. It is now recorded as `"[redacted]"`. This mattered from the moment extraction
  began writing files: without it, `<input type="password" value="…">` would have put the secret
  in the output JSON. Only password fields are redacted; hidden-input values (e.g. CSRF tokens)
  are still recorded as found, which is why the README warns that output files are sensitive.

- Detached frames left in Playwright's `child_frames` after a re-navigation are no longer
  walked, which had produced phantom duplicate frames with mangled labels.
