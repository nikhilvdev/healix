"""What is particular to the Selenium adapter."""

import time

import pytest

pytest.importorskip("selenium")

from selenium.webdriver import Chrome, ChromeOptions  # noqa: E402

from healix.driver.base import Element, ElementNotFoundError  # noqa: E402
from healix.driver.selenium_adapter import SeleniumDriverAdapter  # noqa: E402
from tests.driver.backends import make_driver  # noqa: E402
from tests.driver.dynamic_site import DynamicSite  # noqa: E402


@pytest.fixture(scope="module")
def dynamic():
    site = DynamicSite()
    yield site.url
    site.close()


@pytest.fixture(scope="module")
def selenium():
    driver = make_driver("selenium", action_timeout_ms=600)
    yield driver
    driver.close()


def test_an_unknown_browser_is_refused_up_front():
    with pytest.raises(ValueError, match="unknown browser"):
        SeleniumDriverAdapter(browser="netscape")


def test_using_it_before_start_says_so():
    with pytest.raises(RuntimeError, match="not started"):
        SeleniumDriverAdapter().navigate("http://127.0.0.1/")


def test_a_caller_owned_webdriver_is_used_and_left_running(dynamic):
    options = ChromeOptions()
    options.add_argument("--headless=new")
    try:
        webdriver = Chrome(options=options)
    except Exception as exc:
        pytest.skip(f"cannot launch Chrome: {str(exc).splitlines()[0]}")
    try:
        adapter = SeleniumDriverAdapter(webdriver)
        adapter.start()
        adapter.navigate(dynamic + "/twins")
        assert [e.tag for e in adapter.get_elements()][-2:] == ["button", "button"]
        adapter.close()  # not the adapter's browser: it must stay open
        assert webdriver.current_url.endswith("/twins")
    finally:
        webdriver.quit()


def test_the_session_is_left_in_the_top_document_after_reading_frames(selenium, site_url):
    selenium.navigate(site_url + "/index.html")
    selenium.get_elements()
    assert selenium.webdriver.execute_script("return window.frameElement") is None


def test_the_drivers_own_bookkeeping_attributes_are_not_extracted(selenium, site_url):
    selenium.navigate(site_url + "/index.html")
    iframes = [e for e in selenium.get_elements() if e.tag == "iframe"]
    assert iframes
    assert all("cd_frame_id_" not in e.attributes for e in iframes)


def test_an_ambiguous_target_is_reported_at_once_not_retried_until_the_timeout(selenium, dynamic):
    selenium.navigate(dynamic + "/twins")
    twins = Element(tag="button", css_selector="button.twin", iframe_path=["main"])
    started = time.monotonic()
    with pytest.raises(ElementNotFoundError, match="matches 2 elements"):
        selenium.click(twins)
    assert time.monotonic() - started < 0.5  # the action timeout is 0.6 s: it did not wait


def test_a_target_that_never_appears_fails_after_the_action_timeout(selenium, dynamic):
    selenium.navigate(dynamic + "/twins")
    started = time.monotonic()
    with pytest.raises(ElementNotFoundError, match="not found"):
        selenium.click(Element(tag="button", css_selector="#nope", iframe_path=["main"]))
    assert 0.5 < time.monotonic() - started < 5


def test_a_target_in_a_frame_that_does_not_exist_is_reported(selenium, dynamic):
    selenium.navigate(dynamic + "/twins")
    ghost = Element(tag="button", css_selector="button", iframe_path=["main", "nope"])
    with pytest.raises(ElementNotFoundError, match="frame"):
        selenium.click(ghost)


def test_a_slow_api_can_outlast_the_quiet_window_unless_it_is_raised(dynamic):
    """The documented limit: a request still in flight is invisible to WebDriver.

    The API takes 1.2 s, so a 50 ms quiet window misses it with about a second to spare, however
    slow the machine is; a window longer than the API waits for it.
    """
    quick = make_driver("selenium", quiet_ms=50)
    try:
        quick.navigate(dynamic + "/slow")
        assert not any(e.id == "late" for e in quick.get_elements())
    finally:
        quick.close()
    patient = make_driver("selenium", quiet_ms=1600, settle_timeout_ms=6000)
    try:
        patient.navigate(dynamic + "/slow")
        assert any(e.id == "late" for e in patient.get_elements())
    finally:
        patient.close()
