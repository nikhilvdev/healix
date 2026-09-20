"""The write guard: while it is on, clicking around cannot make a page send data."""

import time

import pytest

from tests.discovery.spa_site import SpaSite


@pytest.fixture(scope="module")
def spa():
    site = SpaSite()
    yield site
    site.close()


def press(driver, id):
    element = next(e for e in driver.get_elements() if e.id == id)
    driver.click(element)


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_without_the_guard_a_click_can_write(driver, spa):
    """The control: this is what the guard exists to stop."""
    spa.hits.clear()
    driver.navigate(spa.url + "/")
    press(driver, "refresh")
    assert wait_for(lambda: ("POST", "/api/refresh") in spa.hits)


def test_with_the_guard_the_write_never_leaves_the_page_and_is_counted(driver, spa):
    spa.hits.clear()
    with driver.guarded():
        driver.navigate(spa.url + "/")
        press(driver, "refresh")
        assert wait_for(lambda: driver.blocked_writes() >= 1)
    time.sleep(0.3)
    assert spa.writes == []


def test_a_form_cannot_be_submitted_while_the_guard_is_on(driver, spa):
    spa.hits.clear()
    with driver.guarded():
        driver.navigate(spa.url + "/")
        press(driver, "details")
        assert wait_for(lambda: driver.blocked_writes() >= 1)
    time.sleep(0.3)
    assert spa.writes == [] and driver.current_url == spa.url + "/"  # and it did not navigate


def test_reading_is_unaffected(driver, spa):
    with driver.guarded():
        driver.navigate(spa.url + "/")
        assert any(e.id == "nav-orders" for e in driver.get_elements())
        press(driver, "nav-orders")
        driver.settle()
        assert driver.current_url == spa.url + "/orders"  # navigation still works
        assert driver.blocked_writes() == 0


def test_the_guard_is_off_again_afterwards(driver, spa):
    with driver.guarded():
        driver.navigate(spa.url + "/")
    spa.hits.clear()
    driver.navigate(spa.url + "/")  # a fresh document, outside the guard
    press(driver, "refresh")
    assert wait_for(lambda: ("POST", "/api/refresh") in spa.hits)


def test_blocked_writes_is_zero_when_nothing_was_blocked(driver, spa):
    driver.navigate(spa.url + "/")
    assert driver.blocked_writes() == 0
