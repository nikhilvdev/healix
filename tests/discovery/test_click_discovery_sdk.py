"""Click-through discovery through the run config and the SDK.

Kept apart from ``test_click_discovery.py``, which holds a module-wide driver open: Playwright's
sync API allows one instance at a time per thread, so the SDK, which starts its own browser,
cannot run in the same module.
"""

import pytest

from healix import Crawler
from healix.discovery.manifest import Manifest
from tests.discovery.spa_site import SpaSite


@pytest.fixture
def spa():
    site = SpaSite(external="http://localhost:1/away")
    yield site
    site.close()


def test_a_run_config_turns_click_discovery_on_and_the_events_and_manifest_show_it(
    use_backend, spa, tmp_path
):
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
