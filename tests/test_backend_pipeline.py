"""The whole pipeline, chosen by ``backend=`` alone, gives the same results on either backend.

Nothing but the backend name changes: the same crawl, the same extraction, the same healing.
"""

import json

import pytest

from healix import Crawler, Healer
from healix.discovery.manifest import EXTRACTED
from tests.driver.backends import make_driver
from tests.healing.site import SwitchSite


@pytest.fixture(scope="module", autouse=True)
def _both_backends_launch():
    for name in ("playwright", "selenium"):
        make_driver(name).close()  # skips the module unless both can run


def crawl(backend, site_url, out):
    config = {
        "backend": backend,
        "base_url": f"{site_url}/index.html",
        "crawl": {"discovery": {"max_pages": 50}, "extraction": {"output_path": str(out)}},
    }
    return Crawler(config, run_id="parity").discover_and_extract()


def page_documents(run):
    """Each page's JSON, keyed by URL, without what legitimately differs between two runs."""
    documents = {}
    for page in run.pages:
        document = json.loads((run.output_path / page.output_file).read_text())
        for key in ("captured_at",):
            document.pop(key)
        for element in document["elements"]:  # pixel positions depend on the browser build
            element["computed"].pop("bounding_box", None)
        documents[page.url] = document
    return documents


def test_the_same_crawl_finds_and_extracts_the_same_things(crawl_site_url, tmp_path):
    runs = {b: crawl(b, crawl_site_url, tmp_path / b) for b in ("playwright", "selenium")}
    playwright, selenium = runs["playwright"], runs["selenium"]

    assert playwright.pages_extracted == playwright.pages_discovered > 5
    assert all(p.status == EXTRACTED for p in selenium.pages)
    assert [p.url for p in selenium.pages] == [p.url for p in playwright.pages]
    assert [(p.page_type, p.structural_hash) for p in selenium.pages] == [
        (p.page_type, p.structural_hash) for p in playwright.pages
    ]
    assert page_documents(selenium) == page_documents(playwright)


def test_healer_takes_selenium_as_its_backend(tmp_path):
    site = SwitchSite()
    try:
        db = tmp_path / "fingerprints.db"
        with Healer(db, backend="selenium") as healer:
            healer.learn(site.url)
        site.set("churn")  # ids and the like change between deploys
        events = []
        with Healer(db, backend="selenium", on_event=events.append) as healer:
            result = healer.resolve(site.url, "textbox:login-username")
        assert result.healed and result.confidence >= 0.5
        assert [e["event"] for e in events] == ["element_healed"]
    finally:
        site.close()
