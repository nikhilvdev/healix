"""What a driver does when the page is not ready yet, on every backend.

Playwright waits for pages to go quiet and for elements to be actionable by itself; Selenium does
neither, so the Selenium adapter does it. These tests hold both to the same behaviour.
"""

import time

import pytest

from healix.driver.base import Element
from tests.driver.backends import evaluate
from tests.driver.dynamic_site import DynamicSite


@pytest.fixture(scope="module")
def dynamic():
    site = DynamicSite()
    yield site.url
    site.close()


def by_id(driver, id):
    return next(e for e in driver.get_elements() if e.id == id)


def test_settle_waits_for_content_that_arrives_from_a_request_after_load(driver, dynamic):
    driver.navigate(dynamic + "/late")
    assert by_id(driver, "late").text_content == "ready"


def test_settle_gives_up_on_a_page_that_never_goes_quiet(driver, dynamic):
    started = time.monotonic()
    driver.navigate(dynamic + "/chatty")
    assert time.monotonic() - started < 10  # bounded by the settle timeout
    assert any(e.tag == "h1" for e in driver.get_elements())


def test_click_and_write_wait_for_an_element_that_is_not_there_yet(driver, dynamic):
    driver.navigate(dynamic + "/appears")
    # The controls render 1.5 s after load, later than settling waits, so they are missing now.
    assert not any(e.id == "later" for e in driver.get_elements())
    field = Element(tag="input", css_selector="#later-input", iframe_path=["main"])
    button = Element(tag="button", css_selector="#later", iframe_path=["main"])
    driver.write("typed", field)
    driver.click(button)
    assert evaluate(driver, ["main"], "window.__clicks.length") == 1
    assert evaluate(driver, ["main"], "document.getElementById('later-input').value") == "typed"


def test_an_alert_on_the_page_is_dismissed_and_does_not_block_extraction(driver, dynamic):
    driver.navigate(dynamic + "/alert")
    assert by_id(driver, "after").text_content == "after"


def test_a_page_that_cannot_be_loaded_raises_and_the_next_page_still_loads(driver, dynamic):
    for _ in range(6):  # the failure that follows a failed load is intermittent, so repeat
        with pytest.raises(Exception):  # noqa: B017 - each backend has its own error type
            driver.navigate("http://127.0.0.1:1/nothing")
        driver.navigate(dynamic + "/twins")
        buttons = [e.text_content for e in driver.get_elements() if e.tag == "button"]
        assert buttons == ["One", "Two"]
