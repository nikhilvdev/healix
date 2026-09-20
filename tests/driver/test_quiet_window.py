"""The opt-in quiet window, on every backend.

This module builds its own drivers and has no module-scoped ``driver`` fixture, because
Playwright's sync API allows one instance at a time per thread: a second one cannot start while a
module-level driver is open, and ``make_driver`` would then skip the test instead of running it.
"""

import pytest

from tests.driver.backends import make_driver
from tests.driver.dynamic_site import DynamicSite


@pytest.fixture(scope="module")
def dynamic():
    site = DynamicSite()
    yield site.url
    site.close()


def test_a_page_that_renders_from_a_timer_is_read_early_unless_a_quiet_window_is_asked_for(
    backend, dynamic
):
    # No request is involved, so network idle passes long before the timer fires. Playwright
    # does not watch the page itself unless asked; Selenium's default window is shorter than the
    # timer. Either way the element is missing, and a longer window finds it.
    impatient = make_driver(backend, quiet_ms=0 if backend == "playwright" else 50)
    try:
        impatient.navigate(dynamic + "/timer")
        assert not any(e.id == "rendered" for e in impatient.get_elements())
    finally:
        impatient.close()

    patient = make_driver(backend, quiet_ms=2600, settle_timeout_ms=8000)
    try:
        patient.navigate(dynamic + "/timer")
        assert any(e.id == "rendered" for e in patient.get_elements())
    finally:
        patient.close()
