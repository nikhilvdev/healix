import json
import sys
from pathlib import Path

import pytest

from healix import Crawler, Extractor, RunConflictError
from healix.discovery.manifest import EXTRACTED, PENDING, Manifest
from healix.driver.factory import BackendUnavailableError
from healix.events import EVENT_DATA_FIELDS
from tests.fakes import FakeSiteDriver, Interrupt, small_site


def config(tmp_path, **extra):
    return {
        "base_url": "https://e.com/",
        "crawl": {"extraction": {"output_path": str(tmp_path / "out")}},
        **extra,
    }


def names(events):
    return [e["event"] for e in events]


def run_crawl(tmp_path, driver=None, **kwargs):
    events = []
    driver = driver or FakeSiteDriver(small_site())
    crawler = Crawler(config(tmp_path), driver=driver, on_event=events.append, **kwargs)
    return crawler, crawler.discover_and_extract(), events, driver


# --- the crawl ------------------------------------------------------------------- #


def test_discover_and_extract_produces_a_run_with_pages_and_files(tmp_path):
    _, run, _, _ = run_crawl(tmp_path)
    assert (run.pages_discovered, run.pages_extracted, run.pages_failed) == (3, 3, 0)
    assert run.discovery_status == "complete" and run.platform_detected is None
    assert [p.url for p in run.pages] == ["https://e.com/", "https://e.com/a", "https://e.com/b"]
    assert run.manifest_path == tmp_path / "out" / "manifest.json"
    assert len(list((tmp_path / "out" / "pages").glob("*.json"))) == 3
    assert Manifest.load(run.manifest_path).pages_extracted == 3
    assert set(run.summary()) == {
        "run_id", "discovery_status", "pages_discovered", "pages_extracted", "pages_failed",
        "platform_detected", "blocked_on_auth", "manifest_path",
    }  # fmt: skip


def test_events_are_emitted_in_order_with_the_documented_payloads(tmp_path):
    _, run, events, _ = run_crawl(tmp_path)
    assert names(events) == ["page_discovered"] * 3 + ["page_extracted"] * 3 + ["run_complete"]
    assert {e["run_id"] for e in events} == {run.run_id}
    for e in events:
        assert list(e) == ["event", "run_id", "timestamp", "data"]
        assert list(e["data"]) == list(EVENT_DATA_FIELDS[e["event"]])

    discovered = events[0]["data"]
    assert discovered["url"] == "https://e.com/" and discovered["structural_hash"]
    extracted = events[3]["data"]
    assert extracted["output_file"] == "pages/0001-e.com.json" and extracted["element_count"] == 3
    assert events[-1]["data"] == {
        "pages_discovered": 3,
        "pages_extracted": 3,
        "platform_detected": None,
        "manifest_path": str(run.manifest_path),
    }


def test_on_event_and_the_webhook_receive_identical_payloads(tmp_path, webhook_receiver):
    _, _, events, _ = run_crawl(tmp_path, webhook_url=webhook_receiver.url)
    assert webhook_receiver.payloads == events


def test_discover_only_extracts_nothing_and_still_completes(tmp_path):
    events = []
    driver = FakeSiteDriver(small_site())
    run = Crawler(config(tmp_path), driver=driver, on_event=events.append).discover()
    assert names(events) == ["page_discovered"] * 3 + ["run_complete"]
    assert (run.pages_discovered, run.pages_extracted) == (3, 0)
    assert not (tmp_path / "out" / "pages").exists()


def test_a_page_variant_is_not_announced_as_discovered(tmp_path):
    site = small_site()
    site["https://e.com/c"] = ([], "b")  # same structure as /b
    site["https://e.com/"] = (["https://e.com/a", "https://e.com/b", "https://e.com/c"], "home")
    _, run, events, _ = run_crawl(tmp_path, driver=FakeSiteDriver(site))
    assert names(events).count("page_discovered") == run.pages_discovered == 3


def test_guided_mode_starts_from_the_start_url(tmp_path):
    driver = FakeSiteDriver(small_site())
    cfg = {
        "start_url": "https://e.com/a",
        "crawl": {"extraction": {"output_path": str(tmp_path / "o")}},
    }
    Crawler(cfg, driver=driver).discover()
    assert driver.navigations[0] == "https://e.com/a"


def test_config_can_be_a_path_a_dict_or_a_runconfig(tmp_path):
    path = tmp_path / "run_config.json"
    path.write_text(json.dumps(config(tmp_path)))
    for cfg in (path, str(path), config(tmp_path)):
        crawler = Crawler(cfg, driver=FakeSiteDriver(small_site()))
        assert crawler.config.start_urls == ["https://e.com/"]


def test_a_raising_on_event_callback_does_not_break_the_run(tmp_path):
    def boom(event):
        raise RuntimeError("consumer bug")

    run = Crawler(
        config(tmp_path), driver=FakeSiteDriver(small_site()), on_event=boom
    ).discover_and_extract()
    assert run.pages_extracted == 3


# --- resume & conflicts ---------------------------------------------------------- #


def test_resume_reuses_discovery_and_extracts_only_what_is_left(tmp_path):
    # navigations 1-3 are discovery, 4-6 are extraction: interrupt while extracting /b
    first = FakeSiteDriver(small_site(), interrupt_on_nth=6)
    with pytest.raises(Interrupt):
        Crawler(config(tmp_path), run_id="r1", driver=first).discover_and_extract()
    saved = Manifest.load(tmp_path / "out" / "manifest.json")
    assert saved.discovery_status == "complete"
    assert [p.status for p in saved.pages] == [EXTRACTED, EXTRACTED, PENDING]

    events = []
    second = FakeSiteDriver(small_site())
    run = Crawler(
        config(tmp_path), run_id="r1", driver=second, on_event=events.append
    ).discover_and_extract()
    assert second.navigations == ["https://e.com/b"]  # no rediscovery, no redoing finished pages
    assert names(events) == ["page_extracted", "run_complete"]  # only this call's work
    assert run.run_id == "r1" and run.pages_extracted == 3
    assert events[-1]["data"]["pages_extracted"] == 3


def test_resume_after_interrupted_discovery_discovers_again(tmp_path):
    first = FakeSiteDriver(small_site(), interrupt_on_nth=2)  # while discovering /a
    with pytest.raises(Interrupt):
        Crawler(config(tmp_path), run_id="r1", driver=first).discover()
    assert Manifest.load(tmp_path / "out" / "manifest.json").discovery_status == "interrupted"

    run = Crawler(
        config(tmp_path), run_id="r1", driver=FakeSiteDriver(small_site())
    ).discover_and_extract()
    assert (run.pages_discovered, run.pages_extracted) == (3, 3)


def test_generated_run_id_is_kept_on_the_crawler_for_resuming(tmp_path):
    crawler, run, _, _ = run_crawl(tmp_path)
    assert crawler.run_id == run.run_id and len(run.run_id) == 12


def test_an_existing_run_without_a_run_id_is_an_error_not_an_overwrite(tmp_path):
    run_crawl(tmp_path, run_id="keep-me")
    before = (tmp_path / "out" / "manifest.json").read_text()
    driver = FakeSiteDriver(small_site())
    with pytest.raises(RunConflictError, match="keep-me"):
        Crawler(config(tmp_path), driver=driver).discover_and_extract()
    assert driver.navigations == []  # refused before doing any work
    assert (tmp_path / "out" / "manifest.json").read_text() == before


def test_a_different_run_id_over_an_existing_run_is_an_error(tmp_path):
    run_crawl(tmp_path, run_id="one")
    with pytest.raises(RunConflictError, match="not 'two'"):
        Crawler(config(tmp_path), run_id="two", driver=FakeSiteDriver(small_site())).discover()


def test_a_corrupt_manifest_gives_a_clear_error(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "manifest.json").write_text("{broken")
    with pytest.raises(ValueError, match="could not read the manifest"):
        Crawler(config(tmp_path), run_id="x", driver=FakeSiteDriver(small_site())).discover()


# --- driver & webhook lifecycle -------------------------------------------------- #


def test_a_caller_supplied_driver_is_never_started_or_closed(tmp_path):
    _, _, _, driver = run_crawl(tmp_path)
    assert (driver.started, driver.closed) == (0, 0)


def test_an_owned_driver_is_started_and_closed(tmp_path, monkeypatch):
    made = []

    def fake_create(backend, *, headless, platform_detection, quiet_ms):
        made.append((backend, headless))
        driver = FakeSiteDriver(small_site())
        made.append(driver)
        return driver

    monkeypatch.setattr("healix.sdk.create_driver", fake_create)
    Crawler(config(tmp_path), headless=False).discover()
    assert made[0] == ("playwright", False)
    assert (made[1].started, made[1].closed) == (1, 1)


def test_an_owned_driver_is_closed_even_when_the_run_fails(tmp_path, monkeypatch):
    driver = FakeSiteDriver(small_site(), interrupt_on="https://e.com/")
    monkeypatch.setattr("healix.sdk.create_driver", lambda backend, **_: driver)
    with pytest.raises(Interrupt):
        Crawler(config(tmp_path)).discover()
    assert driver.closed == 1


def test_the_selenium_backend_says_how_to_install_it_when_it_is_missing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "healix.driver.selenium_adapter", None)  # import fails
    with pytest.raises(BackendUnavailableError, match=r"healix\[selenium\]"):
        Crawler(config(tmp_path, backend="selenium")).discover()


def test_the_run_config_decides_whether_platform_adapters_run(tmp_path, monkeypatch):
    seen = []

    def fake_create(backend, *, headless, platform_detection, quiet_ms):
        seen.append(platform_detection)
        return FakeSiteDriver(small_site())

    monkeypatch.setattr("healix.sdk.create_driver", fake_create)
    Crawler(config(tmp_path)).discover()
    off = config(tmp_path)
    off["crawl"]["extraction"].update(platform_detection="off", output_path=str(tmp_path / "off"))
    Crawler(off).discover()
    assert seen == ["auto", "off"]


def test_the_run_config_decides_how_long_a_page_must_be_quiet(tmp_path, monkeypatch):
    seen = []

    def fake_create(backend, *, headless, platform_detection, quiet_ms):
        seen.append(quiet_ms)
        return FakeSiteDriver(small_site())

    monkeypatch.setattr("healix.sdk.create_driver", fake_create)
    Crawler(config(tmp_path)).discover()  # not set: the backend's own default
    slow = config(tmp_path)
    slow["crawl"]["extraction"].update(settle_quiet_ms=1500, output_path=str(tmp_path / "slow"))
    Crawler(slow).discover()
    assert seen == [None, 1500]


def test_an_invalid_webhook_url_fails_at_construction(tmp_path):
    with pytest.raises(ValueError, match="webhook"):
        Crawler(config(tmp_path), webhook_url="not-a-url")


def test_the_webhook_secret_comes_from_the_environment(tmp_path, monkeypatch, webhook_receiver):
    monkeypatch.setenv("HEALIX_WEBHOOK_SECRET", "env-secret")
    Crawler(
        config(tmp_path), driver=FakeSiteDriver(small_site()), webhook_url=webhook_receiver.url
    ).discover()
    assert all("X-Healix-Signature" in r["headers"] for r in webhook_receiver.requests)

    monkeypatch.delenv("HEALIX_WEBHOOK_SECRET")
    webhook_receiver.requests.clear()
    Crawler(
        config(tmp_path / "again"),
        driver=FakeSiteDriver(small_site()),
        webhook_url=webhook_receiver.url,
    ).discover()
    assert all("X-Healix-Signature" not in r["headers"] for r in webhook_receiver.requests)


# --- Extractor -------------------------------------------------------------------- #


def discovered_manifest(tmp_path):
    Crawler(config(tmp_path), run_id="ex-1", driver=FakeSiteDriver(small_site())).discover()
    return tmp_path / "out" / "manifest.json"


def test_extractor_extracts_a_manifest_path_beside_the_manifest(tmp_path):
    manifest_path = discovered_manifest(tmp_path)
    events = []
    run = Extractor(driver=FakeSiteDriver(small_site()), on_event=events.append).extract(
        manifest_path
    )
    assert run.pages_extracted == 3 and run.run_id == "ex-1"
    assert run.output_path == manifest_path.parent
    assert names(events) == ["page_extracted"] * 3 + ["run_complete"]
    assert {e["run_id"] for e in events} == {"ex-1"}
    assert len(list((manifest_path.parent / "pages").glob("*.json"))) == 3


def test_extractor_resumes_from_a_partially_extracted_manifest(tmp_path):
    manifest_path = discovered_manifest(tmp_path)
    interrupted = FakeSiteDriver(small_site(), interrupt_on="https://e.com/a")
    with pytest.raises(Interrupt):
        Extractor(driver=interrupted).extract(manifest_path)

    events = []
    resume = FakeSiteDriver(small_site())
    run = Extractor(driver=resume, on_event=events.append).extract(manifest_path)
    assert resume.navigations == ["https://e.com/a", "https://e.com/b"]
    assert names(events) == ["page_extracted", "page_extracted", "run_complete"]
    assert run.pages_extracted == 3


def test_extractor_with_a_config_writes_to_its_output_path(tmp_path):
    manifest_path = discovered_manifest(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    cfg = {"crawl": {"extraction": {"output_path": str(elsewhere)}}}
    run = Extractor(cfg, driver=FakeSiteDriver(small_site())).extract(manifest_path)
    assert run.output_path == elsewhere
    assert len(list((elsewhere / "pages").glob("*.json"))) == 3
    assert (
        Manifest.load(manifest_path).pages_extracted == 3
    )  # progress still saved at the given path


def test_extractor_accepts_a_manifest_object(tmp_path):
    manifest = Manifest.load(discovered_manifest(tmp_path))
    cfg = {"crawl": {"extraction": {"output_path": str(tmp_path / "out")}}}
    run = Extractor(cfg, driver=FakeSiteDriver(small_site())).extract(manifest)
    assert run.pages_extracted == 3 and manifest.pages_extracted == 3


def test_extractor_reports_a_missing_manifest(tmp_path):
    with pytest.raises(FileNotFoundError):
        Extractor(driver=FakeSiteDriver({})).extract(tmp_path / "nope.json")


def test_extractor_reports_failed_pages_in_the_run(tmp_path):
    manifest_path = discovered_manifest(tmp_path)
    run = Extractor(driver=FakeSiteDriver(small_site(), broken={"https://e.com/b"})).extract(
        manifest_path
    )
    assert (run.pages_extracted, run.pages_failed) == (2, 1)
    assert Path(run.manifest_path) == manifest_path
