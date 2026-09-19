## Summary

<!-- What does this change, and why? -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes (`--strict`)
- [ ] `pytest` passes, and new surface area has test coverage
- [ ] Public API additions are documented in `README.md` with a runnable example
- [ ] `CHANGELOG.md` has an entry under `Unreleased`
- [ ] No extraction/healing/classification/generation code imports `playwright` or `selenium`
      directly — it goes through `Driver`
- [ ] No LLM call added to the crawl/discover/classify/extract path
- [ ] No credentials in `run_config.json` or any committed file (secrets live in `.env` only)
- [ ] If this adds a page type, locator strategy, or event type, the matching table in the
      README / `CLAUDE.md` is updated in the same change

## Scope

<!-- One concern per PR where possible. Call out here if this
     intentionally spans more than one. -->
