"""Click-through discovery against a single-page app, in a real browser on every backend."""

from types import SimpleNamespace

import pytest

from healix.discovery.crawler import DiscoveryConfig, DiscoveryCrawler
from healix.discovery.manifest import Manifest
from tests.discovery.spa_site import SpaSite


@pytest.fixture
def spa():
    # "localhost" is another host from 127.0.0.1: the "Elsewhere" button leaves the crawl's scope.
    site = SpaSite(external="http://localhost:1/away")
    yield site
    site.close()


@pytest.fixture(scope="module")
def home(driver):
    """One click-through crawl of the app's home page, shared by the tests that only read it."""
    site = SpaSite(external="http://localhost:1/away")
    try:
        yield SimpleNamespace(manifest=crawl(driver, site.url + "/"), spa=site)
    finally:
        site.close()


def crawl(driver, url, **config):
    config.setdefault("click_discovery", True)
    return DiscoveryCrawler(driver, DiscoveryConfig(**config)).discover([url])


def known(manifest, spa, *paths):
    return {path: manifest.find_by_url(spa.url + path) is not None for path in paths}


# --- off by default ----------------------------------------------------------------------- #


def test_nothing_is_clicked_unless_it_is_asked_for(driver, spa):
    manifest = crawl(driver, spa.url + "/", click_discovery=False)
    assert [p.url for p in manifest.pages] == [spa.url + "/"]
    assert manifest.click_discovery is None
    assert "click_discovery" not in manifest.to_dict()
    assert spa.writes == []


# --- what it finds -------------------------------------------------------------------------- #


def test_routes_that_only_buttons_reach_are_discovered(home):
    assert known(home.manifest, home.spa, "/orders", "/settings", "/reports", "/help") == {
        "/orders": True,
        "/settings": True,
        "/reports": True,
        "/help": True,
    }  # a <button>, a div with role=button, a script link (href="#") and a role=tab


def test_it_keeps_going_from_the_pages_it_found(home):
    assert known(home.manifest, home.spa, "/orders/new", "/billing") == {
        "/orders/new": True,  # a button on /orders
        "/billing": True,  # a button on /settings
    }


def test_the_same_button_repeated_down_a_list_is_pressed_once(driver, spa):
    manifest = crawl(driver, spa.url + "/orders")
    assert known(manifest, spa, "/orders/1", "/orders/2") == {"/orders/1": True, "/orders/2": False}


def test_a_page_found_by_clicking_says_where_it_was_found(home):
    manifest, spa = home.manifest, home.spa
    orders = manifest.find_by_url(spa.url + "/orders")
    assert orders.discovered_via == "click"
    assert orders.discovered_from == spa.url + "/"
    assert manifest.pages[0].discovered_via is None  # the start page was not found by clicking
    assert manifest.pages[0].to_dict().keys() == {
        "url", "page_type", "structural_hash", "status", "output_file", "variant_urls", "error",
    }  # fmt: skip


def test_routes_in_the_url_hash_are_found_too(driver, spa):
    manifest = crawl(driver, spa.url + "/hash", max_clicks=6)
    assert manifest.find_by_url(spa.url + "/hash#/team") is not None
    assert manifest.find_by_url(spa.url + "/hash#/admins") is not None


def test_going_somewhere_else_is_not_followed(home):
    assert all("localhost" not in p.url for p in home.manifest.pages)


# --- what it must never do ------------------------------------------------------------------- #


def test_nothing_that_writes_is_ever_sent_to_the_site(home):
    assert home.spa.writes == [], home.spa.writes
    # Nothing on the home page that is dangerous, or that would submit its form, was pressed.
    assert ("POST", "/api/delete") not in home.spa.hits
    assert ("POST", "/api/logout") not in home.spa.hits


def test_a_harmless_looking_button_that_writes_is_pressed_but_its_write_is_stopped(home):
    # "Refresh" has an innocent label, so it is clicked; the guard is what stops its POST.
    assert home.manifest.click_discovery["blocked_writes"] >= 1
    assert ("POST", "/api/refresh") not in home.spa.hits


def test_the_order_form_is_never_pressed(driver, spa):
    crawl(driver, spa.url + "/orders/new")
    assert spa.writes == []


def test_what_was_skipped_as_unsafe_is_counted(home):
    # delete, sign out, trash (class), and the form's button
    assert home.manifest.click_discovery["skipped_unsafe"] >= 4


def test_words_can_be_added_to_what_is_never_clicked(driver, spa):
    manifest = crawl(driver, spa.url + "/", click_deny=["orders", "help"])
    assert known(manifest, spa, "/orders", "/help", "/settings") == {
        "/orders": False,
        "/help": False,
        "/settings": True,
    }


# --- limits ---------------------------------------------------------------------------------- #


def test_a_page_is_clicked_at_most_max_clicks_per_page_times(driver, spa):
    manifest = crawl(driver, spa.url + "/many", max_clicks_per_page=5)
    found = [n for n in range(1, 13) if manifest.find_by_url(f"{spa.url}/many/{n}")]
    assert found == [1, 2, 3, 4, 5]
    assert manifest.click_discovery["clicks"] == 5


def test_a_run_clicks_at_most_max_clicks_times_in_all(driver, spa):
    manifest = crawl(driver, spa.url + "/", max_clicks=3)
    assert manifest.click_discovery["clicks"] == 3


def test_clicks_do_not_use_up_max_pages(driver, spa):
    manifest = crawl(driver, spa.url + "/", max_pages=2)
    # Two page visits, however many clicks it took to find where to go next.
    assert manifest.click_discovery["clicks"] > 2
    assert manifest.discovery_status == "max_pages_reached"


# --- the manifest ---------------------------------------------------------------------------- #


def test_the_click_summary_survives_saving_and_loading(home, tmp_path):
    manifest, spa = home.manifest, home.spa
    manifest.save(tmp_path / "manifest.json")
    loaded = Manifest.load(tmp_path / "manifest.json")
    assert loaded.click_discovery == manifest.click_discovery
    assert loaded.find_by_url(spa.url + "/orders").discovered_via == "click"
    assert set(manifest.click_discovery) == {
        "clicks", "pages_found", "skipped_unsafe", "blocked_writes",
    }  # fmt: skip
    assert manifest.click_discovery["pages_found"] >= 4


def test_a_manifest_from_before_click_discovery_still_loads():
    old = {
        "run_id": "r",
        "pages": [{"url": "https://e.com/", "status": "pending", "structural_hash": "h"}],
    }
    manifest = Manifest.from_dict(old)
    assert manifest.click_discovery is None
    assert manifest.pages[0].discovered_via is None


# --- the config ------------------------------------------------------------------------------- #


def test_click_discovery_is_off_and_bounded_by_default():
    config = DiscoveryConfig()
    assert config.click_discovery is False
    assert (config.max_clicks_per_page, config.max_clicks, config.click_deny) == (15, 100, [])


@pytest.mark.parametrize(
    "bad",
    [
        {"max_clicks_per_page": 0},
        {"max_clicks": 0},
        {"max_clicks_per_page": -1},
        {"click_deny": "delete"},
        {"click_deny": [1]},
        {"click_deny": [""]},
        {"click_discovery": "yes"},
    ],
)
def test_bad_click_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        DiscoveryConfig(**bad)


def test_the_config_block_of_a_run_config_can_turn_it_on():
    config = DiscoveryConfig.from_dict(
        {"click_discovery": True, "max_clicks_per_page": 5, "max_clicks": 20, "click_deny": ["x"]}
    )
    assert config.click_discovery and config.max_clicks == 20 and config.click_deny == ["x"]


# --- through the run config and the SDK ------------------------------------------------------- #


def test_a_run_config_turns_click_discovery_on_and_the_events_and_manifest_show_it(
    use_backend, spa, tmp_path
):
    from healix import Crawler

    events = []
    config = {
        "base_url": spa.url + "/",
        "crawl": {
            "discovery": {"click_discovery": True, "max_clicks": 8, "click_deny": ["help"]},
            "extraction": {"output_path": str(tmp_path / "out")},
        },
    }
    run = Crawler(config, on_event=events.append).discover()
    discovered = [e["data"]["url"] for e in events if e["event"] == "page_discovered"]
    assert spa.url + "/orders" in discovered  # a click-found page is announced like any other
    assert spa.url + "/help" not in discovered  # ...and the run's own deny words were honoured
    saved = Manifest.load(run.manifest_path)
    assert saved.click_discovery["clicks"] <= 8
    assert saved.find_by_url(spa.url + "/orders").discovered_via == "click"
    assert spa.writes == []
    payload = next(e for e in events if e["event"] == "page_discovered")
    assert set(payload["data"]) == {
        "url",
        "page_type",
        "structural_hash",
    }  # the payload is unchanged
