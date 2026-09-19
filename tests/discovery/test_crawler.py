import pytest

from healix.discovery import manifest as m
from healix.discovery.crawler import DiscoveryConfig, DiscoveryCrawler
from healix.discovery.manifest import Manifest
from healix.driver.base import Driver, Element


class FakeDriver(Driver):
    """A scripted site: ``pages`` maps normalized URL -> (links, structure key)."""

    def __init__(self, pages, redirects=None, broken=(), extract_broken=()):
        self.pages = pages
        self.redirects = redirects or {}
        self.broken = set(broken)
        self.extract_broken = set(extract_broken)
        self.navigations: list[str] = []
        self._current = ""

    @property
    def current_url(self):
        return self._current

    def navigate(self, url):
        self.navigations.append(url)
        if url in self.broken:
            raise TimeoutError(f"timeout loading {url}")
        self._current = self.redirects.get(url, url)

    def get_elements(self):
        if self._current in self.extract_broken:
            raise RuntimeError("frame detached")
        links, structure = self.pages[self._current]
        anchors = [Element(tag="a", computed={"href": href}, attributes={"href": href}) for href in links]
        return [*anchors, Element(tag="input", name=structure)]

    def find(self, fingerprint): raise NotImplementedError
    def click(self, target): raise NotImplementedError
    def write(self, text, into): raise NotImplementedError
    def get_frames(self): return []
    def screenshot(self): return b""


def crawl(pages, start="https://e.com/", **kwargs):
    config = kwargs.pop("config", DiscoveryConfig())
    driver = FakeDriver(pages, **kwargs)
    manifest = DiscoveryCrawler(driver, config).discover([start])
    return manifest, driver


def urls(manifest):
    return [p.url for p in manifest.pages]


def test_no_depth_cutoff():
    pages = {f"https://e.com/p{i}": ([f"https://e.com/p{i + 1}"], f"s{i}") for i in range(12)}
    pages["https://e.com/"] = (["https://e.com/p0"], "home")
    pages["https://e.com/p12"] = ([], "s12")
    manifest, _ = crawl(pages, config=DiscoveryConfig(max_pages=100))
    assert manifest.pages_discovered == 14
    assert manifest.discovery_status == m.COMPLETE


def test_max_pages_is_a_hard_ceiling_on_visits():
    pages = {f"https://e.com/p{i}": ([f"https://e.com/p{i + 1}"], f"s{i}") for i in range(20)}
    pages["https://e.com/"] = (["https://e.com/p0"], "home")
    manifest, driver = crawl(pages, config=DiscoveryConfig(max_pages=5))
    assert len(driver.navigations) == 5
    assert manifest.pages_discovered == 5
    assert manifest.discovery_status == m.MAX_PAGES_REACHED


def test_scope_same_domain_ignores_www_and_skips_other_hosts():
    pages = {
        "https://e.com/": (
            ["https://www.e.com/x", "https://other.com/y", "https://sub.e.com/z", "http://e.com/plain"],
            "home",
        ),
        "https://www.e.com/x": ([], "x"),
        "http://e.com/plain": ([], "plain"),
    }
    manifest, _ = crawl(pages)
    assert sorted(urls(manifest)) == ["http://e.com/plain", "https://e.com/", "https://www.e.com/x"]


def test_scope_same_origin_is_stricter():
    pages = {
        "https://e.com/": (["https://www.e.com/x", "http://e.com/plain", "https://e.com/ok"], "home"),
        "https://e.com/ok": ([], "ok"),
    }
    manifest, _ = crawl(pages, config=DiscoveryConfig(domain_scope="same_origin"))
    assert sorted(urls(manifest)) == ["https://e.com/", "https://e.com/ok"]


def test_non_page_links_are_not_followed():
    pages = {
        "https://e.com/": (
            ["mailto:a@e.com", "tel:123", "javascript:void(0)", "https://e.com/report.PDF", "https://e.com/logo.png",
             "https://e.com/real"],
            "home",
        ),
        "https://e.com/real": ([], "real"),
    }
    manifest, driver = crawl(pages)
    assert driver.navigations == ["https://e.com/", "https://e.com/real"]


def test_urls_differing_only_by_noise_are_visited_once():
    pages = {
        "https://e.com/": (["https://e.com/a?utm_source=x", "https://e.com/a/", "https://e.com/a#top"], "home"),
        "https://e.com/a": ([], "a"),
    }
    _, driver = crawl(pages)
    assert driver.navigations == ["https://e.com/", "https://e.com/a"]


def product_site(n=6):
    pages = {"https://e.com/": ([f"https://e.com/product/{i}" for i in range(1, n + 1)] + ["https://e.com/about"], "home")}
    for i in range(1, n + 1):
        pages[f"https://e.com/product/{i}"] = ([], "product")
    pages["https://e.com/about"] = ([], "about")
    return pages


def test_same_template_pages_collapse_into_one_manifest_entry():
    manifest, _ = crawl(product_site(), config=DiscoveryConfig(max_pages=100))
    assert sorted(urls(manifest)) == ["https://e.com/", "https://e.com/about", "https://e.com/product/1"]
    product = manifest.find_by_url("https://e.com/product/1")
    assert product.variant_urls == [f"https://e.com/product/{i}" for i in range(2, 7)]


def test_links_are_followed_from_structural_duplicates():
    pages = product_site(3)
    pages["https://e.com/product/3"] = (["https://e.com/secret"], "product")
    pages["https://e.com/secret"] = ([], "secret")
    manifest, _ = crawl(pages, config=DiscoveryConfig(max_pages=100))
    assert "https://e.com/secret" in urls(manifest)


def test_url_only_dedupe_keeps_every_page():
    manifest, _ = crawl(product_site(), config=DiscoveryConfig(max_pages=100, dedupe_by="url_normalized"))
    assert manifest.pages_discovered == 8


def test_look_alike_family_does_not_starve_distinct_pages_of_max_pages():
    # budget of 4: home, two sampled products, then /about — not the third product
    manifest, _ = crawl(product_site(), config=DiscoveryConfig(max_pages=4, template_sample_size=2))
    assert "https://e.com/about" in urls(manifest)

    starved, _ = crawl(product_site(), config=DiscoveryConfig(max_pages=4, template_sample_size=0))
    assert "https://e.com/about" not in urls(starved)


def test_deferred_urls_are_still_visited_when_budget_allows():
    _, driver = crawl(product_site(), config=DiscoveryConfig(max_pages=100, template_sample_size=2))
    assert sorted(driver.navigations) == sorted(
        ["https://e.com/", "https://e.com/about", *[f"https://e.com/product/{i}" for i in range(1, 7)]]
    )
    assert driver.navigations.index("https://e.com/about") < driver.navigations.index("https://e.com/product/3")


def test_redirect_to_already_visited_page_is_not_recorded_twice():
    pages = {
        # /new is visited first, so /old lands on a page we already have
        "https://e.com/": (["https://e.com/new", "https://e.com/old"], "home"),
        "https://e.com/new": ([], "new"),
    }
    manifest, driver = crawl(pages, redirects={"https://e.com/old": "https://e.com/new"}, config=DiscoveryConfig(dedupe_by="url_normalized"))
    assert driver.navigations == ["https://e.com/", "https://e.com/new", "https://e.com/old"]
    assert sorted(urls(manifest)) == ["https://e.com/", "https://e.com/new"]


def test_manifest_records_the_final_url_after_redirect():
    pages = {"https://e.com/": (["https://e.com/old"], "home"), "https://e.com/landing": ([], "landing")}
    manifest, _ = crawl(pages, redirects={"https://e.com/old": "https://e.com/landing"})
    assert "https://e.com/landing" in urls(manifest) and "https://e.com/old" not in urls(manifest)


def test_redirect_out_of_scope_is_skipped():
    pages = {"https://e.com/": (["https://e.com/sso"], "home"), "https://idp.com/login": ([], "idp")}
    manifest, _ = crawl(pages, redirects={"https://e.com/sso": "https://idp.com/login"})
    assert urls(manifest) == ["https://e.com/"]


def test_load_and_extraction_failures_are_recorded_and_the_crawl_continues():
    pages = {
        "https://e.com/": (["https://e.com/down", "https://e.com/broken", "https://e.com/fine"], "home"),
        "https://e.com/broken": ([], "b"),
        "https://e.com/fine": ([], "fine"),
    }
    manifest, _ = crawl(pages, broken={"https://e.com/down"}, extract_broken={"https://e.com/broken"})
    status = {p.url: p.status for p in manifest.pages}
    assert status == {
        "https://e.com/": m.PENDING,
        "https://e.com/down": m.FAILED,
        "https://e.com/broken": m.FAILED,
        "https://e.com/fine": m.PENDING,
    }
    assert "timeout" in manifest.find_by_url("https://e.com/down").error
    assert manifest.discovery_status == m.COMPLETE


def test_classifier_sets_provisional_page_type_and_callback_fires_for_new_pages_only():
    seen = []
    driver = FakeDriver(product_site(3))
    crawler = DiscoveryCrawler(
        driver,
        DiscoveryConfig(max_pages=100),
        classifier=lambda elements: "detail" if any(e.name == "product" for e in elements) else "nav_shell",
        on_page_discovered=seen.append,
    )
    manifest = crawler.discover(["https://e.com/"])
    assert {p.url: p.page_type for p in manifest.pages}["https://e.com/product/1"] == "detail"
    assert [p.url for p in seen] == urls(manifest)  # one callback per manifest entry, none for variants


def test_multiple_start_urls_and_run_id():
    pages = {"https://e.com/": ([], "home"), "https://e.com/login": ([], "login")}
    manifest = DiscoveryCrawler(FakeDriver(pages)).discover(["https://e.com/", "https://e.com/login"], run_id="run-1")
    assert manifest.run_id == "run-1"
    assert urls(manifest) == ["https://e.com/", "https://e.com/login"]


def test_manifest_is_written_on_success_and_on_interruption(tmp_path):
    pages = {"https://e.com/": (["https://e.com/a"], "home"), "https://e.com/a": ([], "a")}
    ok = DiscoveryCrawler(FakeDriver(pages)).discover(["https://e.com/"], manifest_path=tmp_path / "ok.json")
    assert Manifest.load(tmp_path / "ok.json").to_dict() == ok.to_dict()

    def explode(elements):
        if any(e.name == "a" for e in elements):
            raise RuntimeError("classifier bug")
        return "x"

    with pytest.raises(RuntimeError):
        DiscoveryCrawler(FakeDriver(pages), classifier=explode).discover(
            ["https://e.com/"], manifest_path=tmp_path / "partial.json"
        )
    partial = Manifest.load(tmp_path / "partial.json")
    assert partial.discovery_status == m.INTERRUPTED
    assert urls(partial) == ["https://e.com/"]


@pytest.mark.parametrize(
    "bad",
    [{"domain_scope": "everywhere"}, {"dedupe_by": "vibes"}, {"max_pages": 0}, {"template_sample_size": -1}],
)
def test_config_validation(bad):
    with pytest.raises(ValueError):
        DiscoveryConfig.from_dict(bad)


def test_config_from_run_config_block():
    cfg = DiscoveryConfig.from_dict(
        {"domain_scope": "same_domain", "max_pages": 50, "dedupe_by": "url_normalized_and_structural_hash"}
    )
    assert cfg.max_pages == 50 and cfg.template_sample_size == 3


def test_requires_a_start_url():
    with pytest.raises(ValueError):
        DiscoveryCrawler(FakeDriver({})).discover([])
