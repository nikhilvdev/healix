"""Discovery followed by extraction, end to end, in real Chromium."""

import json

import pytest

pytest.importorskip("playwright")

from healix.discovery.crawler import DiscoveryConfig, DiscoveryCrawler  # noqa: E402
from healix.discovery.manifest import EXTRACTED, Manifest  # noqa: E402
from healix.driver.playwright_adapter import PlaywrightDriverAdapter  # noqa: E402
from healix.extraction import ElementExtractor, ExtractionConfig  # noqa: E402

ELEMENT_KEYS = {
    "tag", "id", "id_normalized", "name", "classes", "attributes", "text_content", "computed",
    "xpath", "css_selector", "iframe_path", "platform_signal", "dom_context", "shadow_path",
}  # fmt: skip
PAGE_KEYS = {
    "schema_version", "run_id", "url", "page_type", "structural_hash", "captured_at",
    "element_counts", "elements",
}  # fmt: skip


@pytest.fixture(scope="module")
def driver():
    adapter = PlaywrightDriverAdapter()
    try:
        adapter.start()
    except Exception as exc:
        pytest.skip(f"cannot launch Chromium: {exc}")
    yield adapter
    adapter.close()


def _load(out, page):
    return json.loads((out / page.output_file).read_text())


def test_discover_then_extract_produces_one_schema_valid_file_per_page(
    driver, crawl_site_url, tmp_path
):
    out = tmp_path / "output"
    manifest_path = out / "manifest.json"
    discovered = DiscoveryCrawler(driver, DiscoveryConfig(max_pages=50)).discover(
        [crawl_site_url + "/index.html"], run_id="it-1", manifest_path=manifest_path
    )
    assert discovered.pages_extracted == 0

    config = ExtractionConfig(output_path=str(out))
    manifest = ElementExtractor(driver, config).extract(manifest_path)

    # one JSON file per manifest page, none extra
    pages_dir = out / "pages"
    assert len(list(pages_dir.glob("*.json"))) == manifest.pages_discovered > 5
    assert all(p.status == EXTRACTED and (out / p.output_file).is_file() for p in manifest.pages)
    assert manifest.pages_extracted == manifest.pages_discovered

    # the manifest on disk is the source of truth and matches
    assert Manifest.load(manifest_path).to_dict() == manifest.to_dict()

    for page in manifest.pages:
        doc = _load(out, page)
        assert set(doc) == PAGE_KEYS
        assert doc["url"] == page.url and doc["run_id"] == "it-1"
        assert doc["page_type"] == page.page_type
        assert doc["captured_at"].endswith("Z")
        assert doc["element_counts"]["total"] == len(doc["elements"]) > 0
        for element in doc["elements"]:
            assert set(element) == ELEMENT_KEYS, element["tag"]


def test_iframe_and_shadow_dom_elements_are_in_the_output(driver, crawl_site_url, tmp_path):
    out = tmp_path / "output"
    manifest = Manifest("it-2")
    manifest.add_page(crawl_site_url + "/index.html", "h")
    ElementExtractor(driver, ExtractionConfig(output_path=str(out))).extract(manifest)
    doc = _load(out, manifest.pages[0])

    frame_paths = {tuple(e["iframe_path"]) for e in doc["elements"]}
    assert ("main",) in frame_paths and ("main", "iframe[0]") in frame_paths
    embed_input = [e for e in doc["elements"] if e["name"] == "embed"]
    assert embed_input and embed_input[0]["iframe_path"] == ["main", "iframe[0]"]

    shadow_link = [e for e in doc["elements"] if e["shadow_path"] and e["tag"] == "a"]
    assert shadow_link and shadow_link[0]["shadow_path"] == ["#lh"]
    assert doc["element_counts"]["in_shadow_root"] >= 1
    assert doc["element_counts"]["by_frame"]["main/iframe[0]"] >= 1


def test_iframe_traversal_off_extracts_only_the_main_frame(driver, crawl_site_url, tmp_path):
    out = tmp_path / "output"
    manifest = Manifest("it-3")
    manifest.add_page(crawl_site_url + "/index.html", "h")
    config = ExtractionConfig(output_path=str(out), iframe_traversal=False)
    ElementExtractor(driver, config).extract(manifest)
    doc = _load(out, manifest.pages[0])
    assert {tuple(e["iframe_path"]) for e in doc["elements"]} == {("main",)}
    assert not any(e["name"] == "embed" for e in doc["elements"])
    assert any(e["shadow_path"] for e in doc["elements"])  # shadow DOM is still pierced


def test_extraction_reclassifies_pages_from_fresh_elements(driver, classify_site_url, tmp_path):
    out = tmp_path / "output"
    manifest = Manifest("it-4")
    manifest.add_page(classify_site_url + "/case_login_basic.html", "h")  # provisional: unknown
    ElementExtractor(driver, ExtractionConfig(output_path=str(out))).extract(manifest)
    assert manifest.pages[0].page_type == "login"
    assert _load(out, manifest.pages[0])["page_type"] == "login"


def test_password_markup_value_never_reaches_the_output_file(driver, classify_site_url, tmp_path):
    out = tmp_path / "output"
    manifest = Manifest("it-5")
    manifest.add_page(classify_site_url + "/case_prefilled_password.html", "h")
    ElementExtractor(driver, ExtractionConfig(output_path=str(out))).extract(manifest)

    text = (out / manifest.pages[0].output_file).read_text()
    assert "s3cr3t-pass-123" not in text
    by_name = {e["name"]: e for e in json.loads(text)["elements"] if e["name"]}
    assert by_name["secret"]["attributes"]["value"] == "[redacted]"
    assert (
        by_name["username"]["attributes"]["value"] == "visible-user"
    )  # only passwords are redacted


def test_resume_does_not_touch_already_extracted_pages(driver, crawl_site_url, tmp_path):
    out = tmp_path / "output"
    manifest_path = out / "manifest.json"
    manifest = DiscoveryCrawler(driver, DiscoveryConfig(max_pages=50)).discover(
        [crawl_site_url + "/index.html"], run_id="it-6", manifest_path=manifest_path
    )
    config = ExtractionConfig(output_path=str(out))
    ElementExtractor(driver, config).extract(manifest_path)
    first_pass = {p.url: _load(out, p)["captured_at"] for p in Manifest.load(manifest_path).pages}

    # simulate an interrupted run: one page is back to pending and its file is gone
    interrupted = Manifest.load(manifest_path)
    victim = interrupted.pages[2]
    (out / victim.output_file).unlink()
    victim.status, victim.output_file = "pending", None
    interrupted.save(manifest_path)

    ElementExtractor(driver, config).extract(manifest_path)
    final = Manifest.load(manifest_path)
    second_pass = {p.url: _load(out, p)["captured_at"] for p in final.pages}
    assert final.pages_extracted == manifest.pages_discovered
    assert {u for u in first_pass if first_pass[u] != second_pass[u]} == {victim.url}
