"""Generated scripts, run for real: crawl a page, generate, execute — then change the page.

The scripts are produced from an actual crawl of the login page in ``tests.healing.site`` and run in
a real browser on each backend. A redesign between generating and running must not break them.
"""

import importlib.util
import subprocess
import sys

import pytest

from healix import Crawler, ScriptGenerator, SQLiteFingerprintStore
from healix.generation import STYLES
from tests.driver.backends import evaluate
from tests.healing.site import SwitchSite

USERNAME, PASSWORD = "alice@example.test", "correct horse battery staple"


@pytest.fixture
def site():
    s = SwitchSite()
    yield s
    s.close()


@pytest.fixture
def scripts(backend, site, tmp_path, monkeypatch):
    """Crawl the baseline page, generate every style for ``backend``, and return the directory."""
    monkeypatch.setenv("WEBLIB_LOGIN_USERNAME", USERNAME)
    monkeypatch.setenv("WEBLIB_LOGIN_PASSWORD", PASSWORD)
    config = {
        "backend": backend,
        "base_url": site.url,
        "crawl": {"extraction": {"output_path": str(tmp_path / "output")}},
    }
    run = Crawler(config, run_id="gen", auto_login=False).discover_and_extract()
    assert run.pages_extracted == 1
    generator = ScriptGenerator(run, fingerprint_db=tmp_path / "fingerprints.db")
    for style in STYLES:
        generator.generate(backend, style, output=tmp_path / "scripts")
    return tmp_path / "scripts"


def load(path):
    spec = importlib.util.spec_from_file_location(f"generated_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def page_state(healer):
    return evaluate(healer.driver, ["main"], "({typed: window.__typed, sent: window.__submitted})")


def test_the_baseline_is_recorded_for_the_scripts(scripts, tmp_path):
    with SQLiteFingerprintStore(tmp_path / "fingerprints.db") as store:
        assert sorted(f.element_role for f in store.fingerprints()) == [
            "button:login-submit",
            "textbox:login-password",
            "textbox:login-username",
        ]


def test_the_action_script_fills_the_form_and_submits(backend, scripts):
    module = load(scripts / f"actions_{backend}.py")
    with module.session() as healer:
        module.run_login(healer)
        assert page_state(healer) == {"typed": {"text": USERNAME, "password": PASSWORD}, "sent": 1}
        assert healer.history() == []  # nothing changed, so nothing was healed


def test_the_page_objects_drive_the_page(backend, scripts):
    module = load(scripts / f"pages_{backend}.py")
    with module.session() as healer:
        page = module.LoginPage(healer).open()
        page.fill_login_username("bob").fill_login_password("pw").click_login_submit()
        assert page_state(healer) == {"typed": {"text": "bob", "password": "pw"}, "sent": 1}


@pytest.mark.parametrize(
    ("redesign", "expected_strategy"), [("churn", "name"), ("refactor", "weighted_score")]
)
def test_the_action_script_survives_a_redesign_and_records_the_fix(
    backend, scripts, site, redesign, expected_strategy
):
    site.set(
        redesign
    )  # after generating: ids, classes and test ids change, or the markup is rebuilt
    module = load(scripts / f"actions_{backend}.py")
    with module.session() as healer:
        module.run_login(healer)
        assert page_state(healer) == {"typed": {"text": USERNAME, "password": PASSWORD}, "sent": 1}
        strategies = {record.strategy for record in healer.history()}
        assert expected_strategy in strategies


def test_the_page_objects_survive_a_redesign(backend, scripts, site):
    site.set("churn")
    module = load(scripts / f"pages_{backend}.py")
    with module.session() as healer:
        module.LoginPage(healer).open().fill_login_username("carol").click_login_submit()
        assert page_state(healer) == {"typed": {"text": "carol"}, "sent": 1}
        assert healer.history()


def run_generated_tests(path):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)],
        capture_output=True,
        text=True,
        cwd=path.parent,
        timeout=300,
    )


def test_the_generated_tests_pass_on_the_page_and_after_a_redesign(backend, scripts, site):
    path = scripts / f"test_healix_{backend}.py"
    result = run_generated_tests(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout

    site.set("churn")
    result = run_generated_tests(path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_generated_tests_fail_when_the_form_is_gone(backend, scripts, site):
    """A test that cannot fail is no test: with the form removed, the elements are not found."""
    site.set("form-removed")
    result = run_generated_tests(scripts / f"test_healix_{backend}.py")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed" in result.stdout
