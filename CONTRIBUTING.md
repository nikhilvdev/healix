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

Python 3.10+ is required. The browser tests launch a real headless browser and skip
themselves if it isn't available: Chromium through Playwright, and Chrome through Selenium
(`.[dev]` installs both libraries; Selenium finds the matching chromedriver itself, which needs
network access the first time). Tests that use the `driver` or `backend` fixtures run once per
backend, so a change to the driver layer is checked against both.
The PostgreSQL store tests need a real server: set `HEALIX_TEST_POSTGRES_URL`, or have Docker
running and they start a throwaway `postgres:16-alpine` container. CI provides one as a service.

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
  (`driver/playwright_adapter.py` and `driver/selenium_adapter.py`) may; `tests/test_architecture.py`
  enforces it. [ARCHITECTURE.md](ARCHITECTURE.md) describes the layers and how to extend them. That includes the
  environment checks in `healix doctor`, which call each adapter's `diagnose()`. Generated scripts
  reach a browser only through `healix.driver` too.
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
- **Healing must reject weak matches, and every heal is audited.** Attribute weights are unequal
  (stable signals over volatile ones), and a candidate below the confidence threshold is rejected,
  not used. The confidence gate, the verification of non-first locator matches, the ambiguity guard,
  the canvas boundary, and the atomic fingerprint-plus-history write are each covered by a test that
  fails if the rule is removed; keep it that way. Recalibrating `WEIGHTS` means re-running the
  real-browser healing tests and updating the README's weights table.

## Keeping the docs and the code in sync

Some things are documented in more than one place. Change them together:

- **A new page type** → the classification table in the README and `classification/rules.py`
  (a test fails if the two disagree). Add a rendered fixture page for it, plus one for any
  look-alike it could be confused with.
- **A change to the `FingerprintStore` interface** → both backends, and the shared contract tests in
  `tests/healing/test_store.py` (they run against SQLite and PostgreSQL).
- **A new locator strategy** → its priority position in `healing/scorer.py` (`LOCATOR_PRIORITY`, kept
  in step with `Fingerprint.locators` by a test) and in the README's description of the resolver.
- **A new event type** → `events/schema.py` and the README's event table, in the same change (a
  test fails if the two disagree). Emit it through `EventEmitter` so the SDK callback and the
  webhook see the same payload.
- **A new run-config key** → the config table in the README.

## Checking against real sites

The fixture sites were written with the code, so they cannot say how it behaves on pages nobody wrote
for it. `tests/real_sites` crawls six public practice sites, generates scripts for them and runs the
generated tests on both backends. It needs the network and takes a few minutes, so it is opt-in:

```bash
HEALIX_REAL_SITES=1 pytest tests/real_sites -v
```

Run it after changing how pages are read, how elements are named, or how the healer decides. A failure
is worth a look, not always a bug: a site can change or be down, and some pages randomise themselves on
purpose (the test says which). Its first run found a real defect the fixtures had hidden.

## Releasing

Releases are cut from a tag, and the `release` workflow refuses one it cannot vouch for. To release:

1. Set the same version in `pyproject.toml` and `healix/__init__.py`.
2. Give `CHANGELOG.md` a `## X.Y.Z — date` heading for it (and say plainly what changed, including
   every changed default, or that none did).
3. Merge to `main` and wait for CI to pass.
4. Tag that commit `vX.Y.Z` and push the tag.

The workflow then checks that the tag, both version strings and the changelog heading agree, runs the
whole CI suite on the tagged commit, builds, and publishes through PyPI trusted publishing. A failed
gate is fixed and re-tagged: PyPI never accepts the same version twice. Build locally with
`python -m build --outdir <somewhere outside the repo>` if you want to inspect the files.

## Reporting issues

Bug reports and feature requests are welcome via GitHub issues. For a wrong extraction or a
missed link, the smallest HTML that reproduces it is the most useful thing you can attach.
Please never paste credentials, tokens, or session cookies. Security problems go through
[SECURITY.md](.github/SECURITY.md), not a public issue.
