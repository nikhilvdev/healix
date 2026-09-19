"""End-to-end discovery with real Chromium against the generated crawl site."""

import pytest

pytest.importorskip("playwright")

from healix.discovery import manifest as m  # noqa: E402
from healix.discovery.crawler import DiscoveryConfig, DiscoveryCrawler  # noqa: E402
from healix.discovery.manifest import Manifest  # noqa: E402
from healix.driver.playwright_adapter import PlaywrightDriverAdapter  # noqa: E402


@pytest.fixture(scope="module")
def driver():
    adapter = PlaywrightDriverAdapter()
    try:
        adapter.start()
    except Exception as exc:
        pytest.skip(f"cannot launch Chromium: {exc}")
    yield adapter
    adapter.close()


def test_discovers_the_whole_reachable_site(driver, crawl_site_url, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest = DiscoveryCrawler(driver, DiscoveryConfig(max_pages=50)).discover(
        [crawl_site_url + "/index.html"], run_id="it-1", manifest_path=manifest_path
    )

    found = {p.url.removeprefix(crawl_site_url) for p in manifest.pages}
    assert found == {
        "/index.html",
        "/a.html", "/b.html", "/c.html", "/d.html",  # chain: depth is unlimited
        "/products/1.html",                            # /products/2..4 are variants of it
        "/hidden.html",                                # only linked from a structural duplicate (/products/3)
        "/embedded-target.html",                       # link lives inside an iframe
        "/shadow-target.html",                         # link lives inside a shadow root
    }
    # d.html links to "/", which serves the same page as /index.html: same structure, so a variant
    index = manifest.find_by_url(crawl_site_url + "/index.html")
    assert [v.removeprefix(crawl_site_url) for v in index.variant_urls] == ["/"]
    product = manifest.find_by_url(crawl_site_url + "/products/1.html")
    assert sorted(v.removeprefix(crawl_site_url) for v in product.variant_urls) == [
        "/products/2.html", "/products/3.html", "/products/4.html",
    ]
    assert manifest.discovery_status == m.COMPLETE
    assert all(p.status == m.PENDING and p.structural_hash for p in manifest.pages)
    assert not any("external.invalid" in p.url or p.url.endswith(".pdf") for p in manifest.pages)
    assert Manifest.load(manifest_path).to_dict() == manifest.to_dict()


def test_max_pages_is_respected_against_a_real_browser(driver, crawl_site_url):
    manifest = DiscoveryCrawler(driver, DiscoveryConfig(max_pages=3)).discover([crawl_site_url + "/index.html"])
    assert manifest.pages_discovered <= 3
    assert manifest.discovery_status == m.MAX_PAGES_REACHED


def test_unreachable_page_is_recorded_as_failed(driver, crawl_site_url):
    manifest = DiscoveryCrawler(driver).discover(["http://127.0.0.1:1/nothing"])
    assert [p.status for p in manifest.pages] == [m.FAILED]
    assert manifest.pages[0].error
