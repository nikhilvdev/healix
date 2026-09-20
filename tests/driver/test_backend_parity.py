"""Playwright and Selenium, side by side on the same page.

Whatever one backend extracts or locates, the other must too, or "the same pipeline runs on either"
is not true. Both are asked the same questions here, and their answers are compared.
"""

import pytest

from healix.driver.base import Element
from healix.healing.fingerprint import Fingerprint, LocatorSpec
from tests.driver.backends import make_driver


@pytest.fixture(scope="module")
def drivers():
    playwright = make_driver("playwright")
    selenium = make_driver("selenium")
    yield playwright, selenium
    selenium.close()
    playwright.close()


@pytest.fixture
def both(drivers, site_url):
    for driver in drivers:
        driver.navigate(site_url + "/parity.html")
    return drivers


def key(element: Element | None):
    return None if element is None else (tuple(element.iframe_path), element.css_selector)


def comparable(elements):
    """The elements as dicts, without pixel positions: those come from the browser build (fonts,
    rounding), and Playwright's bundled Chromium and the installed Chrome are different builds."""
    out = []
    for element in elements:
        raw = element.to_dict()
        raw["computed"] = {k: v for k, v in raw["computed"].items() if k != "bounding_box"}
        out.append(raw)
    return out


def test_they_extract_identical_elements(both):
    playwright, selenium = both
    assert comparable(playwright.get_elements()) == comparable(selenium.get_elements())


def test_they_extract_identical_elements_from_the_frames_page_too(drivers, site_url):
    for driver in drivers:
        driver.navigate(site_url + "/index.html")
    playwright, selenium = drivers
    assert comparable(playwright.get_elements()) == comparable(selenium.get_elements())
    assert [(f.path, f.same_origin) for f in playwright.get_frames()] == [
        (f.path, f.same_origin) for f in selenium.get_frames()
    ]


def test_a_closed_shadow_root_is_out_of_reach_for_both(both):
    for driver in both:
        assert all(e.id != "hidden-btn" for e in driver.get_elements())


CSS = [
    "#p1", "p", "div, p", "ul > li:nth-of-type(2)", "li", "[data-x=\"a b\"]", "#odd",
    "my-el > span", "my-el span", "#host1 button", "#host1 section.s button", "#host1 > button",
    "section.s button", "#host2 input", "#host2 #inner-host input", "input[name=deep]",
    "#dup", "#host1 #dup", "span#dup", ":not(.nothing)", "html > body > h1", "body h1",
    "##invalid", "[", "#does-not-exist",
]  # fmt: skip
XPATH = ["//li[2]", '//*[@id="three"]', "//h1", "//span", "//button", "//[", "//div[@id='d1']/p"]
TEXT = [
    ("*", "Hello"), ("div", "Hello"), ("p", "Hello"), ("button", "Save"), ("button", "Save now"),
    ("*", "now"), ("*", "Press"), ("input", "Press"), ("a", "Next page"), ("a", "Deep link"),
    ("li", "two"), ("h1", "Parity"), ("*", "  Hello  "), ("*", "nothing like this"),
]  # fmt: skip
ID_PATTERNS = ["p{n}", "d{n}", "sb", "dup", "n{n}", "nx{n}"]


def specs():
    for value in CSS:
        yield "*", LocatorSpec("css", "css", value)
    for value in XPATH:
        yield "*", LocatorSpec("xpath", "xpath", value)
    for tag, value in TEXT:
        yield tag, LocatorSpec("text", "text", value)
    for value in ID_PATTERNS:
        yield "*", LocatorSpec("normalized_id", "id_pattern", value)


@pytest.mark.parametrize(
    ("tag", "spec"), list(specs()), ids=lambda v: v if isinstance(v, str) else f"{v.kind}={v.value}"
)
def test_a_locator_matches_the_same_element_on_both(both, tag, spec):
    playwright, selenium = both
    fingerprint = Fingerprint(page_url="u", element_role="r", tag=tag)
    assert key(selenium.locate(spec, fingerprint)) == key(playwright.locate(spec, fingerprint))


def test_every_extracted_element_is_found_again_the_same_way_by_every_one_of_its_locators(both):
    playwright, selenium = both
    checked = 0
    for element in playwright.get_elements():
        fingerprint = Fingerprint.from_element(element, "u")
        for spec in fingerprint.locators():
            assert key(selenium.locate(spec, fingerprint)) == key(
                playwright.locate(spec, fingerprint)
            ), (element.css_selector, spec)
            checked += 1
    assert checked > 50


def test_find_agrees_on_which_element_and_which_strategy(both):
    playwright, selenium = both
    for element in playwright.get_elements():
        fingerprint = Fingerprint.from_element(element, "u")
        try:
            expected = key(playwright.find(fingerprint))
        except LookupError:
            expected = "missing"
        try:
            actual = key(selenium.find(fingerprint))
        except LookupError:
            actual = "missing"
        assert actual == expected, element.css_selector
