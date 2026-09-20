"""`create_driver` builds the right adapter with the right waiting behaviour; no browser starts."""

import pytest

from healix.driver.factory import create_driver


def test_each_backend_keeps_its_own_quiet_window_unless_told_otherwise():
    pytest.importorskip("playwright")
    pytest.importorskip("selenium")
    assert create_driver("playwright")._quiet_ms == 0
    assert create_driver("selenium")._quiet == 0.5


@pytest.mark.parametrize("backend", ["playwright", "selenium"])
def test_quiet_ms_is_passed_to_the_backend(backend):
    pytest.importorskip(backend)
    driver = create_driver(backend, quiet_ms=1200)
    assert (driver._quiet_ms if backend == "playwright" else driver._quiet * 1000) == 1200


def test_quiet_ms_of_zero_is_a_real_value_not_the_default():
    pytest.importorskip("selenium")
    assert create_driver("selenium", quiet_ms=0)._quiet == 0


def test_a_negative_quiet_ms_is_rejected():
    with pytest.raises(ValueError, match="quiet_ms"):
        create_driver("playwright", quiet_ms=-1)
