"""``healix doctor``: what it reports, when the machine is not ready, and that it never guesses."""

import importlib.util
import json

import pytest

from healix import doctor
from healix.driver.diagnose import BackendReport
from healix.driver.factory import diagnose_backend

READY = {
    "playwright": BackendReport("playwright", True, "1.63", browser="/pw/chromium"),
    "selenium": BackendReport("selenium", True, "4.49", browser="/Applications/Chrome"),
}


def checks_by_name(report):
    return {c.name: c for c in report.checks}


@pytest.fixture
def reports(monkeypatch):
    """Choose what each backend reports."""
    table = dict(READY)
    monkeypatch.setattr(doctor, "diagnose_backend", lambda backend: table[backend])
    return table


def test_a_machine_with_both_backends_is_ready(reports):
    report = doctor.run_doctor(environ={})
    assert report.ok and report.ready_backends == ("playwright", "selenium")
    assert checks_by_name(report)["playwright"].status == "ok"


def test_one_working_backend_is_enough_and_the_other_is_only_reported(reports):
    reports["selenium"] = BackendReport(
        "selenium", False, problem="the Python package is not installed", hint="pip install x"
    )
    report = doctor.run_doctor(environ={})
    assert report.ok and report.ready_backends == ("playwright",)
    assert checks_by_name(report)["selenium"].status == "missing"
    assert "pip install x" in doctor.format_report(report)


def test_no_usable_backend_is_not_ready_and_says_how_to_fix_it(reports):
    reports["playwright"] = BackendReport(
        "playwright", True, "1.63", problem="the Chromium browser is not installed",
        hint="playwright install chromium",
    )  # fmt: skip
    reports["selenium"] = BackendReport("selenium", False, hint="pip install 'healix[selenium]'")
    report = doctor.run_doctor(environ={})
    assert not report.ok and report.ready_backends == ()
    text = doctor.format_report(report)
    assert "FAIL" in text and "playwright install chromium" in text
    assert "Not ready: no browser backend is usable" in text


def test_a_missing_core_dependency_is_a_failure_even_with_a_browser(reports, monkeypatch):
    def version(package):
        if package == "jinja2":
            raise doctor.PackageNotFoundError(package)
        return "1.0"

    monkeypatch.setattr(doctor, "version", version)
    report = doctor.run_doctor(environ={})
    assert checks_by_name(report)["jinja2"].status == "fail"
    assert report.ready_backends and not report.ok
    assert "fix the FAIL lines" in doctor.format_report(report)


def test_credentials_are_reported_as_set_or_not_and_never_shown(reports):
    secret = "hunter2-very-secret"
    report = doctor.run_doctor(
        environ={"WEBLIB_LOGIN_USERNAME": "someone@example.test", "WEBLIB_LOGIN_PASSWORD": secret}
    )
    assert checks_by_name(report)["credentials"].status == "ok"
    rendered = doctor.format_report(report) + json.dumps(report.to_dict())
    assert secret not in rendered and "someone@example.test" not in rendered

    unset = doctor.run_doctor(environ={"WEBLIB_LOGIN_USERNAME": "x"})  # half set is not set
    assert checks_by_name(unset)["credentials"].status == "info"
    assert unset.ok  # missing credentials never make the machine "not ready"


def test_launch_is_off_unless_asked_for(reports, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a browser was launched")

    monkeypatch.setattr(doctor, "create_driver", boom)
    assert doctor.run_doctor(environ={}).ok


def test_a_browser_that_will_not_launch_is_a_failure_with_its_reason(reports, monkeypatch):
    def create(backend, **kwargs):
        raise RuntimeError("chromedriver\nexited with code 127")

    monkeypatch.setattr(doctor, "create_driver", create)
    report = doctor.run_doctor(launch=True, environ={})
    launch = checks_by_name(report)["playwright launch"]
    assert launch.status == "fail" and "chromedriver exited with code 127" in launch.detail
    assert report.ready_backends == () and not report.ok  # installed is not the same as working


def test_a_page_that_reads_no_elements_is_a_failure(reports, monkeypatch):
    class Empty:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def navigate(self, url):
            pass

        def get_elements(self):
            return []

    monkeypatch.setattr(doctor, "create_driver", lambda backend, **kw: Empty())
    launch = checks_by_name(doctor.run_doctor(launch=True, environ={}))["selenium launch"]
    assert launch.status == "fail" and "read no elements" in launch.detail


def test_a_backend_whose_package_is_missing_is_found_without_importing_it(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    report = diagnose_backend("selenium")
    assert not report.installed and not report.ready
    assert "healix[selenium]" in report.hint


def test_an_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="unknown backend"):
        diagnose_backend("cypress")


# --- against the real machine ------------------------------------------------------------------ #


def test_the_real_diagnosis_agrees_with_what_can_launch(backend, monkeypatch):
    """``ready`` from the cheap check and a real launch must tell the same story."""
    monkeypatch.setattr(doctor, "BACKENDS", (backend,))
    report = doctor.run_doctor(launch=True, environ={})
    checks = checks_by_name(report)
    assert checks[backend].status == "ok"
    assert checks[f"{backend} launch"].status == "ok"
    assert "read" in checks[f"{backend} launch"].detail
    assert report.ready_backends == (backend,) and report.ok
