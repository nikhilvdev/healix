import pytest

from healix import Crawler, SQLiteFingerprintStore
from healix.discovery.manifest import Manifest
from healix.extraction import ElementExtractor, ExtractionConfig
from tests.fakes import FakeSiteDriver, small_site


def manifest():
    m = Manifest("r1")
    for name in ("", "a", "b"):
        m.add_page(f"https://e.com/{name}", f"hash-{name}")
    return m


@pytest.fixture
def store():
    with SQLiteFingerprintStore(":memory:") as s:
        yield s


def extract(tmp_path, store, **kwargs):
    driver = FakeSiteDriver(small_site())
    config = ExtractionConfig(output_path=str(tmp_path / "out"))
    ElementExtractor(driver, config, fingerprint_store=store, **kwargs).extract(manifest())
    return driver


def test_extraction_records_fingerprints_for_the_healable_elements_of_every_page(tmp_path, store):
    extract(tmp_path, store)
    pages = {f.page_url for f in store.fingerprints()}
    assert pages == {"https://e.com/", "https://e.com/a", "https://e.com/b"}
    home = {f.element_role for f in store.fingerprints("https://e.com/")}
    assert {"link:a", "link:b", "textbox:home"} <= home


def test_without_a_store_nothing_is_recorded_and_extraction_is_unchanged(tmp_path, store):
    driver = FakeSiteDriver(small_site())
    ElementExtractor(driver, ExtractionConfig(output_path=str(tmp_path / "out"))).extract(
        manifest()
    )
    assert store.fingerprints() == []


def test_re_extraction_keeps_the_baseline_by_default(tmp_path, store):
    extract(tmp_path, store)
    before = [f.to_dict() for f in store.fingerprints()]
    extract(tmp_path / "again", store)
    assert [f.to_dict() for f in store.fingerprints()] == before


def test_refresh_mode_overwrites(tmp_path, store):
    extract(tmp_path, store)
    fp = store.get("https://e.com/", "textbox:home")
    fp.id = "tampered"
    store.put(fp)
    extract(tmp_path / "again", store, fingerprint_mode="refresh")
    assert store.get("https://e.com/", "textbox:home").id is None


def test_an_invalid_mode_is_rejected(tmp_path, store):
    with pytest.raises(ValueError, match="fingerprint_mode"):
        ElementExtractor(FakeSiteDriver({}), fingerprint_store=store, fingerprint_mode="nope")


def test_a_failing_store_does_not_lose_the_extracted_page(tmp_path, captured_logs):
    class Broken(SQLiteFingerprintStore):
        def put_if_absent(self, fingerprint):
            raise RuntimeError("disk full")

    with Broken(":memory:") as broken:
        m = manifest()
        driver = FakeSiteDriver(small_site())
        config = ExtractionConfig(output_path=str(tmp_path / "out"))
        ElementExtractor(driver, config, fingerprint_store=broken).extract(m)
    assert m.pages_extracted == 3  # every page still extracted
    assert any(r["message"] == "could not record fingerprints" for r in captured_logs)


def test_the_crawler_records_fingerprints_to_a_path_it_opens_and_closes(tmp_path):
    db = tmp_path / "fp.db"
    config = {
        "base_url": "https://e.com/",
        "crawl": {"extraction": {"output_path": str(tmp_path / "out")}},
    }
    Crawler(
        config, driver=FakeSiteDriver(small_site()), fingerprint_store=db
    ).discover_and_extract()
    with SQLiteFingerprintStore(db) as reopened:
        assert {f.page_url for f in reopened.fingerprints()} == {
            "https://e.com/",
            "https://e.com/a",
            "https://e.com/b",
        }


def test_the_crawler_leaves_a_store_instance_open(tmp_path, store):
    config = {
        "base_url": "https://e.com/",
        "crawl": {"extraction": {"output_path": str(tmp_path / "out")}},
    }
    Crawler(
        config, driver=FakeSiteDriver(small_site()), fingerprint_store=store
    ).discover_and_extract()
    assert len(store.fingerprints()) > 0  # still usable: the caller owns it


def test_the_crawler_records_fingerprints_into_postgres_from_a_url(tmp_path, postgres_url):
    from healix.healing import open_store
    from tests.conftest import drop_healix_tables

    drop_healix_tables(postgres_url)
    config = {
        "base_url": "https://e.com/",
        "crawl": {"extraction": {"output_path": str(tmp_path / "out")}},
    }
    Crawler(
        config, driver=FakeSiteDriver(small_site()), fingerprint_store=postgres_url
    ).discover_and_extract()
    with open_store(postgres_url) as reopened:  # the crawler closed its own connection
        assert {f.page_url for f in reopened.fingerprints()} == {
            "https://e.com/",
            "https://e.com/a",
            "https://e.com/b",
        }
