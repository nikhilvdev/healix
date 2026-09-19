# Changelog

All notable changes to this project are documented in this file.

## Unreleased

Pre-release: nothing is published to PyPI yet. Phases 1 and 2 of the build plan are
implemented; classification, extraction output, login, healing, the Selenium adapter,
script generation, and the SDK/CLI/event surface are not.

### Added

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

- `PlaywrightDriverAdapter.navigate()` now also waits, best-effort and bounded by
  `settle_timeout_ms` (default 3000; `0` disables), for the network to go idle after `load`,
  so client-rendered pages have content before it is read.
- Extracted link elements report the browser-resolved absolute URL in `computed.href`
  (honoring `<base href>`).

### Fixed

- Detached frames left in Playwright's `child_frames` after a re-navigation are no longer
  walked, which had produced phantom duplicate frames with mangled labels.
