"""The run records which platform it saw, and the run config can turn platform signals off."""

import pytest

from healix import Crawler
from healix.discovery.manifest import Manifest
from healix.driver.base import Driver, Element
from healix.extraction import ElementExtractor, ExtractionConfig
from tests.platform_adapters import pages

URL = "https://app.example.com/"


class OnePageDriver(Driver):
    def __init__(self, elements):
        self.elements = elements

    @property
    def current_url(self):
        return URL

    def navigate(self, url):
        pass

    def get_elements(self, *, iframe_traversal=True):
        return self.elements

    def find(self, fingerprint):
        raise NotImplementedError

    def click(self, target):
        raise NotImplementedError

    def write(self, text, into):
        raise NotImplementedError

    def get_frames(self):
        return []

    def screenshot(self):
        return b""


def elements(*, signal):
    return [
        Element(tag="button", id="save", css_selector="#save", platform_signal=signal),
        Element(tag="p", id="plain", css_selector="#plain"),
    ]


def extract(tmp_path, driver_elements, **config):
    manifest = Manifest("r")
    manifest.add_page(URL, "hash")
    cfg = ExtractionConfig(output_path=str(tmp_path), **config)
    return ElementExtractor(OnePageDriver(driver_elements), cfg).extract(manifest)


SAP = {"platform": "sap_ui5", "control_id": "save"}


def test_the_manifest_records_the_platform_the_adapters_saw(tmp_path):
    assert extract(tmp_path, elements(signal=SAP)).platform_detected == "sap_ui5"


def test_it_stays_null_when_no_adapter_fired(tmp_path):
    assert extract(tmp_path, elements(signal=None)).platform_detected is None


def test_platform_detection_off_records_nothing_and_strips_signals(tmp_path):
    manifest = extract(tmp_path, elements(signal=SAP), platform_detection="off")
    assert manifest.platform_detected is None
    page = manifest.pages[0]
    saved = (tmp_path / page.output_file).read_text()
    assert "sap_ui5" not in saved and "control_id" not in saved


def test_the_first_platform_seen_is_kept(tmp_path):
    manifest = Manifest("r")
    manifest.add_page(URL, "hash")
    manifest.platform_detected = "salesforce_lwc"
    ElementExtractor(
        OnePageDriver(elements(signal=SAP)), ExtractionConfig(output_path=str(tmp_path))
    ).extract(manifest)
    assert manifest.platform_detected == "salesforce_lwc"


# --- a real crawl -------------------------------------------------------------------------- #


@pytest.fixture
def platform_site(tmp_path):
    from tests.conftest import _serve

    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><html>" + pages.SAP_UI5_CORE + "</html>")
    server = _serve(root)
    yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(("mode", "expected"), [("auto", "sap_ui5"), ("off", None)])
def test_a_real_crawl_of_a_ui5_page_records_the_platform(
    use_backend, platform_site, tmp_path, mode, expected
):
    config = {
        "base_url": platform_site,
        "crawl": {
            "extraction": {"output_path": str(tmp_path / "out"), "platform_detection": mode},
        },
    }
    run = Crawler(config, run_id="ui5").discover_and_extract()
    assert run.platform_detected == expected
    saved = (run.output_path / run.pages[0].output_file).read_text()
    assert ("sap_ui5" in saved) == (expected is not None)
