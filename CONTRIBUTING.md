# Contributing

Thanks for considering a contribution to `healix`. This project also
follows a [Code of Conduct](CODE_OF_CONDUCT.md) — participation in issues,
PRs, and discussions means agreeing to abide by it.

## Setup

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

Python 3.10+ is required. The browser tests launch real headless Chromium and skip
themselves if Playwright or its browsers aren't installed.

## Pull request strategy

- **Branch from `main`**, name branches by intent: `feat/…`, `fix/…`,
  `docs/…`, `chore/…` (e.g. `feat/rule-based-classifier`).
- **Keep PRs scoped to one concern** where possible. A PR that mixes an
  unrelated refactor with a feature is harder to review and harder to revert.
- **Every PR must satisfy this definition of done** before it's ready for
  review:
  1. Type hints throughout, `mypy --strict` clean
  2. Unit tests cover the new surface; existing tests still pass. Behavior that
     depends on a real DOM (frames, shadow roots, selectors) gets a browser test
     against a fixture page, not only a mock
  3. Public API additions documented in the README with a runnable example
  4. `CHANGELOG.md` has an entry under `Unreleased`
  5. None of the architecture invariants below is broken
- **CI must be green** (`ruff check`, `ruff format --check`, `mypy`, `pytest`) and
  **at least one review approval** is required before merge — enforced by branch
  protection on `main`.
- **Squash-merge** into `main` — keep the squash commit message a clear
  summary of the change; per-commit history within a PR branch doesn't need
  to be clean.
- Commit messages and PR titles: imperative mood, e.g. "Add rule-based
  classifier" not "Added" or "Adds".

## Architecture invariants

These are deliberate, fixed design decisions. A change that needs to bend one should be
raised as an issue and discussed *before* code is written.

- **Everything goes through `Driver`.** Extraction, discovery, healing, classification, and
  generation must never import `playwright` or `selenium`. Only adapter modules
  (`driver/playwright_adapter.py`, and later `driver/selenium_adapter.py`) may.
- **Pure Python.** No second implementation language and no compiled core.
- **No LLM calls** in the crawl/discover/classify/extract path. Classification is
  rule-based so runs are deterministic and cost nothing.
- **The run config never holds secrets, and the parser enforces it.** `RunConfig` rejects
  credential-looking keys; don't loosen that check to make a new option convenient.
- **Login credentials are never logged, evented, or written to disk.** `Credentials` masks itself,
  log/exception messages from the login path carry only error *types*, and URLs are logged without
  their query string. The login handler must keep submitting a password at most once per attempt
  and giving up after a failure; the tests break if either is removed.
- **No credentials in committed files.** Secrets live in `.env` (git-ignored), never in
  `run_config.json`, fixtures, logs, manifests, or events. Ship placeholders only in
  `.env.example`.
- **Generic first, vendor-specific never required.** Iframe traversal, shadow DOM piercing,
  and ID normalization are generic. Platform adapters (SAP UI5, Salesforce LWC) are additive
  only, and a test with every adapter disabled must still pass on the generic pipeline.
- **Log through `healix.log`.** `logger = get_logger(__name__)`, a short constant message, and
  metadata as keyword arguments (`logger.warn("could not load page", url=url, error=str(exc))`).
  Not stdlib `logging`, not `print`. Never log credentials, tokens, cookies, or input values.
- **Capture full detail at extraction time.** Never a curated subset, never deferred to a
  later pass.
- **Healing must reject weak matches.** Attribute weights are unequal (stable signals over
  volatile ones), and a candidate below the confidence threshold is rejected, not used.

## Keeping the docs and the code in sync

Some things are documented in more than one place. Change them together:

- **A new page type** → the classification table in the README and `classification/rules.py`
  (a test fails if the two disagree). Add a rendered fixture page for it, plus one for any
  look-alike it could be confused with.
- **A new locator strategy** → its priority position in `healing/scorer.py` and in the
  README's description of the resolver.
- **A new event type** → `events/schema.py` and the README's event table, in the same change (a
  test fails if the two disagree). Emit it through `EventEmitter` so the SDK callback and the
  webhook see the same payload.
- **A new run-config key** → the config table in the README.

## Reporting issues

Bug reports and feature requests are welcome via GitHub issues. For a wrong extraction or a
missed link, the smallest HTML that reproduces it is the most useful thing you can attach.
Please never paste credentials, tokens, or session cookies. Security problems go through
[SECURITY.md](.github/SECURITY.md), not a public issue.
