# Changelog

All notable changes to this project are documented in this file.

## Unreleased

Pre-release: nothing is published to PyPI yet. Phases 1–3 of the build plan are
implemented; extraction output, login, healing, the Selenium adapter, script generation,
and the SDK/CLI/event surface are not.

### Added

- **Rule-based page classification (Phase 3).**
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

- **Driver abstraction (Phase 1).**
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
- **Discovery and manifest (Phase 2).**
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
    phases.
- Packaging and project scaffolding: `pyproject.toml` (hatchling, `playwright`/`dev`/`docs`
  extras, `py.typed`), CI, release and docs workflows, issue and PR templates, dependabot,
  pre-commit, `.env.example`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, and
  the MIT `LICENSE`.

### Changed

- **New runtime dependency: `logquill>=1.0`** (itself dependency-free). Healix previously had
  none.
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

- Detached frames left in Playwright's `child_frames` after a re-navigation are no longer
  walked, which had produced phantom duplicate frames with mangled labels.
