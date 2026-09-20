"""Crawl real public sites, generate every kind of script, and run the generated tests.

Opt-in, because it needs the network and takes several minutes::

    HEALIX_REAL_SITES=1 pytest tests/real_sites -v

The sites are ones built to be scraped and automated (quotes/books.toscrape.com, the-internet, the
Sauce Labs demo shop, Playwright's own demo), and every crawl is small. It is the check that
Healix works on pages nobody wrote for Healix: the fixture sites in the rest of the suite cannot
say that. The failures allowed below are the sites' own doing, not bugs:

* ``challenging_dom`` re-randomises its labels and ids on every load, so an element cannot be told
  from its neighbours and the healer refuses, as it should;
* ``disappearing_elements`` shows a menu item on some loads and not on others, so a test that looks
  for it fails on the loads where it is gone. Which of these two pages misbehaves varies from one
  run to the next, so they are *allowed* to fail, not required to;
* ``saucedemo/inventory`` sits behind a login, and generated scripts do not log in.

Whatever else fails is a regression. The published login for the demo shop is passed through the
environment of the child process and never written anywhere.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HEALIX_REAL_SITES"),
    reason="needs the network: set HEALIX_REAL_SITES=1 to run",
)

# name: (start URL, max_pages, generated tests that may fail, environment for the crawl)
SITES = {
    "quotes": ("https://quotes.toscrape.com/", 10, set(), {}),
    "quotes-js": ("https://quotes.toscrape.com/js/", 6, set(), {}),
    "books": ("https://books.toscrape.com/", 8, set(), {}),
    "internet": (
        "https://the-internet.herokuapp.com/",
        14,
        {"test_challenging_dom", "test_disappearing_elements"},
        {},
    ),
    "todomvc": ("https://demo.playwright.dev/todomvc/", 6, set(), {}),
    "saucedemo": (
        "https://www.saucedemo.com/",
        8,
        {"test_inventory"},
        {"WEBLIB_LOGIN_USERNAME": "standard_user", "WEBLIB_LOGIN_PASSWORD": "secret_sauce"},
    ),
}


def run(*args, cwd, env=None, timeout=1200):
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture(scope="module", params=list(SITES))
def crawled(request, tmp_path_factory):
    name = request.param
    start, max_pages, allowed, env = SITES[name]
    work = tmp_path_factory.mktemp(name)
    config = work / "run.json"
    config.write_text(
        json.dumps(
            {
                "base_url": start,
                "crawl": {
                    "discovery": {"max_pages": max_pages},
                    "extraction": {"output_path": "out"},
                },
            }
        )
    )
    result = run(
        "healix",
        "crawl",
        "--config",
        str(config),
        "--fingerprint-db",
        "fp.db",
        "--json",
        cwd=work,
        env=env,
    )
    # 0: done; 4: a login page was reached with no credentials, which is expected of the demo shops
    assert result.returncode in (0, 4), result.stderr[-800:]
    return work, allowed


@pytest.mark.parametrize("backend", ["playwright", "selenium"])
def test_the_generated_tests_pass_on_the_real_site_except_the_ones_the_site_defeats(
    crawled, backend
):
    work, allowed = crawled
    made = run(
        "healix",
        "generate",
        "--input",
        "out/manifest.json",
        "--backend",
        backend,
        "--style",
        "test",
        "--output",
        "scripts",
        "--fingerprint-db",
        "fp.db",
        "--no-record",
        cwd=work,
    )
    assert made.returncode == 0, made.stderr[-800:]
    ran = run(
        "pytest",
        f"scripts/test_healix_{backend}.py",
        "-q",
        "--tb=no",
        "-rf",
        "-p",
        "no:cacheprovider",
        cwd=work,
    )
    failed = set(re.findall(r"^FAILED \S+::(test_\w+)", ran.stdout, flags=re.MULTILINE))
    assert failed <= allowed, (
        f"unexpected failures: {sorted(failed - allowed)}\n{ran.stdout[-1500:]}"
    )


def test_every_style_can_be_generated_for_both_backends(crawled):
    work, _ = crawled
    for backend in ("playwright", "selenium"):
        for style in ("pom", "test"):
            made = run(
                "healix", "generate", "--input", "out/manifest.json", "--backend", backend,
                "--style", style, "--output", "scripts", "--fingerprint-db", "fp.db", "--no-record",
                cwd=work,
            )  # fmt: skip
            assert made.returncode == 0, made.stderr[-800:]
    scripts = sorted(p.name for p in Path(work, "scripts").glob("*.py"))
    assert "pages_playwright.py" in scripts and "test_healix_selenium.py" in scripts


def test_an_action_script_needs_an_input_and_says_so_when_there_is_none(crawled):
    work, _ = crawled
    made = run(
        "healix", "generate", "--input", "out/manifest.json", "--backend", "playwright",
        "--style", "action", "--output", "scripts", "--fingerprint-db", "fp.db", "--no-record",
        cwd=work,
    )  # fmt: skip
    if made.returncode != 0:  # a catalogue with nothing to fill in: refused cleanly, not crashed
        assert "needs one" in made.stderr and made.returncode == 2
