import json
from datetime import datetime, timezone

import pytest

from healix.discovery.manifest import EXTRACTED, FAILED, PENDING, Manifest
from healix.driver.base import CROSS_ORIGIN, Driver, Element, SkippedFrame
from healix.extraction import (
    ElementExtractor,
    ExtractionConfig,
    build_page_document,
    count_elements,
    output_filename,
)

FIXED_NOW = datetime(2026, 9, 19, 12, 30, 45, 123000, tzinfo=timezone.utc)


class Interrupt(BaseException):
    """Stands in for KeyboardInterrupt (a BaseException the extractor must not swallow)."""


class FakeDriver(Driver):
    def __init__(self, pages, redirects=None, broken=(), extract_broken=(), interrupt_on=None):
        self.pages = pages
        self.redirects = redirects or {}
        self.broken = set(broken)
        self.extract_broken = set(extract_broken)
        self.interrupt_on = interrupt_on
        self.calls: list[tuple] = []
        self._current = ""

    @property
    def current_url(self):
        return self._current

    def navigate(self, url):
        self.calls.append(("navigate", url))
        if url == self.interrupt_on:
            raise Interrupt
        if url in self.broken:
            raise TimeoutError(f"timeout loading {url}")
        self._current = self.redirects.get(url, url)

    def get_elements(self, *, iframe_traversal=True):
        self.calls.append(("get_elements", self._current, iframe_traversal))
        if self._current in self.extract_broken:
            raise RuntimeError("frame detached")
        return self.pages[self._current]

    def find(self, fingerprint):
        raise NotImplementedError

    def click(self, target):
        raise NotImplementedError

    def write(self, text, into):
        raise NotImplementedError

    def get_frames(self):
        return []

    def skipped_frames(self):
        return list(getattr(self, "skipped", []))

    def screenshot(self):
        return b""


def _els(name):
    return [
        Element.from_dict(
            {"tag": "input", "name": name, "computed": {"visible": True}, "css_selector": "input"},
            iframe_path=["main"],
        ),
        Element.from_dict(
            {"tag": "a", "computed": {"visible": False}, "css_selector": "a"},
            iframe_path=["main", "panel"],
        ),
    ]


def make_manifest(*names, run_id="run-1"):
    manifest = Manifest(run_id)
    for name in names:
        manifest.add_page(f"https://e.com/{name}", f"hash-{name}")
    return manifest


def site(*names):
    return {f"https://e.com/{n}": _els(n) for n in names}


def extractor(driver, tmp_path, **kwargs):
    config = ExtractionConfig(output_path=str(tmp_path / "out"))
    kwargs.setdefault("clock", lambda: FIXED_NOW)
    return ElementExtractor(driver, config, **kwargs)


def load(path):
    return json.loads(path.read_text())


# --- output ------------------------------------------------------------------ #


def test_writes_one_json_file_per_page_with_the_documented_shape(tmp_path):
    manifest = make_manifest("a", "b")
    extractor(FakeDriver(site("a", "b")), tmp_path).extract(manifest)

    out = tmp_path / "out"
    files = sorted((out / "pages").glob("*.json"))
    assert [f.name for f in files] == ["0001-e.com-a.json", "0002-e.com-b.json"]

    doc = load(files[0])
    assert list(doc) == [
        "schema_version", "run_id", "url", "page_type", "structural_hash",
        "captured_at", "element_counts", "elements",
    ]  # fmt: skip
    assert doc["schema_version"] == 1 and doc["run_id"] == "run-1"
    assert doc["url"] == "https://e.com/a"
    assert doc["captured_at"] == "2026-09-19T12:30:45.123Z"
    assert doc["elements"] == [e.to_dict() for e in _els("a")]
    assert doc["element_counts"] == {
        "total": 2,
        "visible": 1,
        "in_shadow_root": 0,
        "by_tag": {"a": 1, "input": 1},
        "by_frame": {"main": 1, "main/panel": 1},
    }


def test_manifest_records_status_and_relative_output_file(tmp_path):
    manifest = make_manifest("a")
    extractor(FakeDriver(site("a")), tmp_path).extract(manifest)
    page = manifest.pages[0]
    assert (page.status, page.output_file) == (EXTRACTED, "pages/0001-e.com-a.json")
    assert (tmp_path / "out" / page.output_file).exists()
    saved = Manifest.load(tmp_path / "out" / "manifest.json")
    assert saved.to_dict() == manifest.to_dict()
    assert manifest.pages_extracted == 1


def test_redirected_page_records_final_url(tmp_path):
    manifest = make_manifest("old")
    driver = FakeDriver(
        {"https://e.com/new": _els("new")}, redirects={"https://e.com/old": "https://e.com/new"}
    )
    extractor(driver, tmp_path).extract(manifest)
    doc = load(tmp_path / "out" / manifest.pages[0].output_file)
    assert (doc["url"], doc["final_url"]) == ("https://e.com/old", "https://e.com/new")


def test_no_temp_files_are_left_behind(tmp_path):
    extractor(FakeDriver(site("a", "b")), tmp_path).extract(make_manifest("a", "b"))
    assert not list((tmp_path / "out").rglob(".*.tmp"))


def test_only_the_structural_representative_is_extracted(tmp_path):
    manifest = Manifest("r")
    manifest.add_page("https://e.com/p/1", "same")
    manifest.add_page("https://e.com/p/2", "same")  # a variant of the first, not its own entry
    driver = FakeDriver({"https://e.com/p/1": _els("p")})
    extractor(driver, tmp_path).extract(manifest)
    assert [c[1] for c in driver.calls if c[0] == "navigate"] == ["https://e.com/p/1"]
    assert len(list((tmp_path / "out" / "pages").glob("*.json"))) == 1


# --- sequencing & flags -------------------------------------------------------- #


def test_pages_are_walked_strictly_one_at_a_time_in_manifest_order(tmp_path):
    driver = FakeDriver(site("a", "b", "c"))
    extractor(driver, tmp_path).extract(make_manifest("a", "b", "c"))
    assert driver.calls == [
        ("navigate", "https://e.com/a"), ("get_elements", "https://e.com/a", True),
        ("navigate", "https://e.com/b"), ("get_elements", "https://e.com/b", True),
        ("navigate", "https://e.com/c"), ("get_elements", "https://e.com/c", True),
    ]  # fmt: skip


def test_iframe_traversal_flag_is_passed_to_the_driver(tmp_path):
    driver = FakeDriver(site("a"))
    config = ExtractionConfig(output_path=str(tmp_path / "out"), iframe_traversal=False)
    ElementExtractor(driver, config).extract(make_manifest("a"))
    assert ("get_elements", "https://e.com/a", False) in driver.calls


# --- classification --------------------------------------------------------------- #


def test_page_is_reclassified_from_fresh_elements_and_the_manifest_updated(tmp_path):
    manifest = make_manifest("a")
    manifest.pages[0].page_type = "unknown"
    ex = extractor(FakeDriver(site("a")), tmp_path, classifier=lambda els, url: "form")
    ex.extract(manifest)
    assert manifest.pages[0].page_type == "form"
    assert load(tmp_path / "out" / manifest.pages[0].output_file)["page_type"] == "form"


def test_classifier_none_keeps_the_provisional_type(tmp_path):
    manifest = make_manifest("a")
    manifest.pages[0].page_type = "list"
    extractor(FakeDriver(site("a")), tmp_path, classifier=None).extract(manifest)
    assert manifest.pages[0].page_type == "list"


def test_classifier_receives_the_final_url(tmp_path):
    seen = []
    driver = FakeDriver(
        {"https://e.com/new": _els("n")}, redirects={"https://e.com/old": "https://e.com/new"}
    )
    extractor(driver, tmp_path, classifier=lambda els, url: seen.append(url) or "x").extract(
        make_manifest("old")
    )
    assert seen == ["https://e.com/new"]


# --- failure & resume -------------------------------------------------------------- #


def test_load_and_extraction_failures_are_marked_and_the_run_continues(tmp_path):
    manifest = make_manifest("ok1", "down", "broken", "ok2")
    driver = FakeDriver(
        site("ok1", "broken", "ok2"),
        broken={"https://e.com/down"},
        extract_broken={"https://e.com/broken"},
    )
    extractor(driver, tmp_path).extract(manifest)
    status = {p.url.rsplit("/", 1)[1]: p.status for p in manifest.pages}
    assert status == {"ok1": EXTRACTED, "down": FAILED, "broken": FAILED, "ok2": EXTRACTED}
    assert "timeout" in manifest.find_by_url("https://e.com/down").error
    assert len(list((tmp_path / "out" / "pages").glob("*.json"))) == 2


def test_interruption_keeps_progress_and_resume_continues_from_the_first_unextracted_page(tmp_path):
    manifest = make_manifest("a", "b", "c", "d")
    first = FakeDriver(site("a", "b", "c", "d"), interrupt_on="https://e.com/c")
    with pytest.raises(Interrupt):
        extractor(first, tmp_path).extract(manifest)

    on_disk = Manifest.load(tmp_path / "out" / "manifest.json")
    assert [p.status for p in on_disk.pages] == [EXTRACTED, EXTRACTED, PENDING, PENDING]

    second = FakeDriver(site("a", "b", "c", "d"))
    extractor(second, tmp_path).extract(tmp_path / "out" / "manifest.json")  # resume from the path
    assert [c[1] for c in second.calls if c[0] == "navigate"] == [
        "https://e.com/c",
        "https://e.com/d",
    ]
    final = Manifest.load(tmp_path / "out" / "manifest.json")
    assert [p.status for p in final.pages] == [EXTRACTED] * 4
    assert final.pages_extracted == 4


def test_rerun_retries_failed_pages_and_clears_their_error(tmp_path):
    manifest = make_manifest("a", "b")
    extractor(FakeDriver(site("a", "b"), broken={"https://e.com/b"}), tmp_path).extract(manifest)
    assert manifest.find_by_url("https://e.com/b").status == FAILED

    retry = FakeDriver(site("a", "b"))
    extractor(retry, tmp_path).extract(manifest)
    assert [c[1] for c in retry.calls if c[0] == "navigate"] == ["https://e.com/b"]
    page = manifest.find_by_url("https://e.com/b")
    assert (page.status, page.error) == (EXTRACTED, None)


def test_extracted_page_with_a_missing_output_file_is_extracted_again(tmp_path):
    manifest = make_manifest("a", "b")
    extractor(FakeDriver(site("a", "b")), tmp_path).extract(manifest)
    (tmp_path / "out" / manifest.pages[0].output_file).unlink()

    again = FakeDriver(site("a", "b"))
    extractor(again, tmp_path).extract(manifest)
    assert [c[1] for c in again.calls if c[0] == "navigate"] == ["https://e.com/a"]
    assert (tmp_path / "out" / manifest.pages[0].output_file).exists()


def test_a_fully_extracted_manifest_does_no_work(tmp_path):
    manifest = make_manifest("a")
    extractor(FakeDriver(site("a")), tmp_path).extract(manifest)
    idle = FakeDriver(site("a"))
    extractor(idle, tmp_path).extract(manifest)
    assert idle.calls == []


# --- events & logging ------------------------------------------------------------------- #


def test_on_page_extracted_receives_the_page_extracted_event_fields(tmp_path):
    seen = []
    extractor(
        FakeDriver(site("a")),
        tmp_path,
        classifier=lambda e, u: "list",
        on_page_extracted=seen.append,
    ).extract(make_manifest("a"))
    assert [(p.url, p.page_type, p.element_count, p.output_file) for p in seen] == [
        ("https://e.com/a", "list", 2, "pages/0001-e.com-a.json")
    ]


def test_progress_and_failures_are_logged(tmp_path, captured_logs):
    manifest = make_manifest("a", "down")
    extractor(FakeDriver(site("a"), broken={"https://e.com/down"}), tmp_path).extract(manifest)
    by_message = {r["message"]: r for r in captured_logs}
    assert by_message["extraction started"]["meta"]["pages_remaining"] == 2
    assert by_message["could not extract page"]["meta"]["url"] == "https://e.com/down"
    finished = by_message["extraction finished"]["meta"]
    assert (finished["extracted"], finished["failed"]) == (1, 1)


def test_structure_drift_since_discovery_is_logged(tmp_path, captured_logs):
    manifest = make_manifest(
        "a"
    )  # discovery hash is "hash-a"; the fresh structure hashes differently
    extractor(FakeDriver(site("a")), tmp_path).extract(manifest)
    assert any(r["message"] == "page structure changed since discovery" for r in captured_logs)


# --- helpers & config ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "index, url, expected",
    [
        (1, "https://example.com/", "pages/0001-example.com.json"),
        (
            12,
            "https://example.com/orders/42?tab=items",
            "pages/0012-example.com-orders-42-tab-items.json",
        ),
        (3, "https://e.com/Ünïcode/ÿ", "pages/0003-e.com-n-code.json"),
        (7, "https://e.com/" + "x" * 200, "pages/0007-e.com-" + "x" * 54 + ".json"),
        (2, "https://e.com:8080/a", "pages/0002-e.com-8080-a.json"),
    ],
)
def test_output_filename(index, url, expected):
    assert output_filename(index, url) == expected


def test_output_filenames_are_unique_per_index_even_for_identical_slugs():
    assert output_filename(1, "https://e.com/a") != output_filename(2, "https://e.com/a")


def test_count_elements_handles_shadow_and_empty():
    shadow = Element.from_dict(
        {"tag": "b", "shadow_path": ["#h"], "computed": {"visible": True}}, iframe_path=["main"]
    )
    assert count_elements([shadow])["in_shadow_root"] == 1
    assert count_elements([])["total"] == 0


def test_build_page_document_omits_final_url_when_not_redirected():
    doc = build_page_document(
        run_id="r", url="u", final_url="u", page_type="x", elements=[], captured_at="t"
    )
    assert "final_url" not in doc


def test_build_page_document_lists_skipped_frames_only_when_there_are_some():
    skipped = [SkippedFrame(["main", "ads"], "https://ads.example/x", CROSS_ORIGIN)]
    doc = build_page_document(
        run_id="r",
        url="u",
        final_url="u",
        page_type="x",
        elements=[],
        captured_at="t",
        skipped_frames=skipped,
    )
    assert doc["skipped_frames"] == [
        {"path": ["main", "ads"], "url": "https://ads.example/x", "reason": "cross_origin"}
    ]


def test_a_page_with_skipped_frames_says_so_in_its_output_file(tmp_path):
    driver = FakeDriver({"https://e.com/a": _els("a")})
    driver.skipped = [SkippedFrame(["main", "ads"], "https://ads.example/x", CROSS_ORIGIN)]
    manifest = make_manifest("a")
    ElementExtractor(driver, ExtractionConfig(output_path=str(tmp_path))).extract(manifest)
    doc = json.loads((tmp_path / manifest.pages[0].output_file).read_text())
    assert doc["skipped_frames"][0]["path"] == ["main", "ads"]
    assert doc["element_counts"]["total"] == 2  # the elements that were read are all still there


@pytest.mark.parametrize(
    "bad",
    [
        {"sequence": "parallel"},
        {"output_format": "csv"},
        {"platform_detection": "yes"},
        {"settle_quiet_ms": -1},
        {"settle_quiet_ms": "500"},
        {"settle_quiet_ms": 1.5},
        {"settle_quiet_ms": True},
    ],
)
def test_config_validation(bad):
    with pytest.raises(ValueError):
        ExtractionConfig.from_dict(bad)


def test_settle_quiet_ms_defaults_to_the_backends_own_behaviour():
    assert ExtractionConfig().settle_quiet_ms is None
    assert ExtractionConfig.from_dict({"settle_quiet_ms": 0}).settle_quiet_ms == 0
    assert ExtractionConfig.from_dict({"settle_quiet_ms": 1500}).settle_quiet_ms == 1500


def test_config_from_the_run_config_block():
    cfg = ExtractionConfig.from_dict(
        {
            "sequence": "one_by_one",
            "output_format": "json",
            "output_path": "./output/",
            "iframe_traversal": True,
            "platform_detection": "auto",
        }
    )
    assert cfg.iframe_traversal is True and cfg.output_path == "./output/"
