"""Integration tests against a real Chromium and the local fixture site."""

import pytest

pytest.importorskip("playwright")

from healix.driver.base import ElementNotFoundError  # noqa: E402
from healix.driver.playwright_adapter import PlaywrightDriverAdapter  # noqa: E402
from healix.healing.fingerprint import Fingerprint  # noqa: E402


@pytest.fixture(scope="module")
def driver():
    adapter = PlaywrightDriverAdapter()
    try:
        adapter.start()
    except Exception as exc:  # browser binaries not installed
        pytest.skip(f"cannot launch Chromium: {exc}")
    yield adapter
    adapter.close()


@pytest.fixture
def page(driver, site_url):
    driver.navigate(site_url + "/index.html")
    return driver


def _by(elements, **match):
    found = [e for e in elements if all(getattr(e, k) == v for k, v in match.items())]
    assert len(found) == 1, f"expected one element matching {match}, got {len(found)}"
    return found[0]


def test_extracts_light_dom_element_with_full_detail(page):
    el = _by(page.get_elements(), id="user-4471")
    assert el.tag == "input"
    assert el.id_normalized == "user-{n}"
    assert el.name == "username"
    assert el.classes == ["form-control", "input-lg"]
    assert el.attributes == {"type": "text", "data-testid": "login-username", "aria-label": "Username"}
    assert el.css_selector == "#user-4471"
    assert el.xpath == '//*[@id="user-4471"]'
    assert el.iframe_path == ["main"]
    assert el.platform_signal is None
    assert el.computed["visible"] is True and el.computed["enabled"] is True
    assert el.computed["bounding_box"]["width"] > 0
    assert el.dom_context == {
        "parent_tag": "form",
        "parent_id": "login",
        "sibling_index": 1,
        "nearby_label_text": "Username",
    }


def test_disabled_and_hidden_state(page):
    elements = page.get_elements()
    assert _by(elements, id="submit-btn").computed["enabled"] is False
    assert _by(elements, id="ghost").computed["visible"] is False


def test_non_element_tags_are_excluded(page):
    tags = {e.tag for e in page.get_elements()}
    assert not tags & {"script", "style", "head", "title", "meta"}


def test_colon_delimited_ids_normalize_and_stay_locatable(page):
    el = _by(page.get_elements(), id="pt1:r1:0:link")
    assert el.id_normalized == "pt{n}:r{n}:{n}:link"
    assert page.find(Fingerprint.from_element(el, "u")).id == "pt1:r1:0:link"


def test_nested_iframes_are_merged_with_iframe_path(page):
    elements = page.get_elements()
    assert _by(elements, id="panel-title").iframe_path == ["main", "workspace_panel"]
    email = _by(elements, id="email")
    assert email.iframe_path == ["main", "workspace_panel", "form_frame"]
    assert email.dom_context["nearby_label_text"] == "Email"


def test_cross_origin_frame_is_listed_but_not_extracted(page):
    frames = {tuple(f.path): f for f in page.get_frames()}
    assert frames[("main", "foreign")].same_origin is False
    assert frames[("main", "workspace_panel", "form_frame")].same_origin is True
    assert all(e.id != "foreign-btn" for e in page.get_elements())


def test_open_shadow_roots_are_pierced_recursively(page):
    elements = page.get_elements()
    button = _by(elements, tag="button", css_selector="#lwc-host div > button")
    assert button.attributes["data-testid"] == "shadow-btn"
    assert button.shadow_path == ["#lwc-host"]
    deep = _by(elements, id="deep-1")
    assert deep.shadow_path == ["#lwc-host", "#inner-host"]
    assert deep.css_selector == "#lwc-host #inner-host #deep-1"


def test_every_extracted_css_selector_resolves_to_its_own_element(page):
    for frame in page.get_frames():
        if not frame.same_origin:
            continue
        for el in (e for e in page.get_elements() if e.iframe_path == frame.path):
            assert frame.handle.locator(el.css_selector).count() == 1, el.css_selector


def test_click_and_write_reach_iframe_and_shadow_elements(page):
    elements = page.get_elements()
    page.click(_by(elements, id="go"))
    form_frame = next(f for f in page.get_frames() if f.path[-1] == "form_frame")
    assert form_frame.handle.evaluate("window.__clicks") == ["go"]

    page.click(_by(elements, tag="button", css_selector="#lwc-host div > button"))
    assert page.page.evaluate("window.__clicks") == ["shadow-btn"]

    page.write("a@b.co", _by(elements, id="email"))
    assert form_frame.handle.evaluate("document.getElementById('email').value") == "a@b.co"
    page.write("deep text", _by(elements, id="deep-1"))
    assert page.page.evaluate(
        "document.getElementById('lwc-host').shadowRoot.getElementById('inner-host').shadowRoot.getElementById('deep-1').value"
    ) == "deep text"


def _fingerprint(**overrides):
    base = dict(page_url="u", element_role="textbox", tag="input")
    base.update(overrides)
    return Fingerprint(**base)


def test_find_prefers_stable_attribute_over_stale_id(page):
    fp = _fingerprint(id="user-STALE", attributes={"data-testid": "login-username"})
    assert page.find(fp).id == "user-4471"


def test_find_falls_back_to_normalized_id_when_raw_id_changed(page):
    fp = _fingerprint(id="user-9999", id_normalized="user-{n}")
    el = page.find(fp)
    assert el.id == "user-4471"


def test_find_falls_back_through_name_aria_and_text(page):
    assert page.find(_fingerprint(id="nope", name="username")).id == "user-4471"
    assert page.find(_fingerprint(attributes={"aria-label": "Username"})).id == "user-4471"
    assert page.find(_fingerprint(tag="a", text_content="Next page")).id == "pt1:r1:0:link"


def test_find_searches_inside_frames_and_shadow_roots(page):
    assert page.find(_fingerprint(id="email")).iframe_path == ["main", "workspace_panel", "form_frame"]
    assert page.find(_fingerprint(attributes={"data-testid": "shadow-btn"})).shadow_path == ["#lwc-host"]


def test_find_is_scoped_by_iframe_path(page):
    fp = _fingerprint(id="email", iframe_path=["main"])
    with pytest.raises(ElementNotFoundError):
        page.find(fp)


def test_find_raises_when_nothing_matches(page):
    with pytest.raises(ElementNotFoundError):
        page.find(_fingerprint(id="missing", name="missing", attributes={"aria-label": "Missing"}))


def test_find_skips_ambiguous_locators(page):
    # three inputs match in the main frame (one inside a shadow root); that is ambiguous, not a hit
    with pytest.raises(ElementNotFoundError):
        page.find(_fingerprint(css_selector="input", iframe_path=["main"]))


def test_screenshot_returns_png_bytes(page):
    assert page.screenshot().startswith(b"\x89PNG")
