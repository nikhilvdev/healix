import json
import subprocess
import sys
from pathlib import Path

import pytest

from healix import __version__, cli
from healix.discovery.manifest import Manifest
from healix.driver.factory import BackendUnavailableError
from healix.sdk import Run, RunConflictError

CONFIG = {"base_url": "https://e.com/", "crawl": {"extraction": {"output_path": "OUT"}}}


def make_run(failed=0):
    manifest = Manifest("run-7")
    manifest.add_page("https://e.com/a", "h1")
    manifest.add_page("https://e.com/b", "h2")
    manifest.mark_extracted("https://e.com/a", "pages/0001.json")
    if failed:
        manifest.mark_failed("https://e.com/b", "boom")
    else:
        manifest.mark_extracted("https://e.com/b", "pages/0002.json")
    manifest.discovery_status = "complete"
    return Run("run-7", manifest, Path("output/manifest.json"), Path("output"))


class FakeCrawler:
    instances: list["FakeCrawler"] = []
    run = None
    error = None

    def __init__(self, config, **kwargs):
        self.config, self.kwargs = config, kwargs
        self.run_id = kwargs.get("run_id") or "generated-1"
        self.calls = []
        FakeCrawler.instances.append(self)

    def _go(self, name):
        self.calls.append(name)
        if FakeCrawler.error:
            raise FakeCrawler.error
        return FakeCrawler.run

    def discover(self):
        return self._go("discover")

    def discover_and_extract(self):
        return self._go("discover_and_extract")


class FakeExtractor(FakeCrawler):
    def __init__(self, config=None, **kwargs):
        super().__init__(config, **kwargs)

    def extract(self, manifest):
        self.manifest = manifest
        return self._go("extract")


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    FakeCrawler.instances = []
    FakeCrawler.run = make_run()
    FakeCrawler.error = None
    monkeypatch.setattr(cli, "Crawler", FakeCrawler)
    monkeypatch.setattr(cli, "Extractor", FakeExtractor)
    monkeypatch.delenv("HEALIX_WEBHOOK_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def config_file(fakes):
    path = fakes / "run_config.json"
    path.write_text(json.dumps(CONFIG))
    return path


# --- arguments ------------------------------------------------------------------ #


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"healix {__version__}"


@pytest.mark.parametrize("argv", [[], ["crawl"], ["extract"], ["crawl", "--config"], ["bogus"]])
def test_missing_or_bad_arguments_exit_with_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    assert "usage: healix" in capsys.readouterr().err


def test_crawl_passes_every_option_to_the_sdk(config_file):
    code = cli.main(
        ["crawl", "--config", str(config_file), "--output", "elsewhere", "--run-id", "r9",
         "--webhook-url", "https://hooks.example/x", "--headed", "--no-login"]
    )  # fmt: skip
    [crawler] = FakeCrawler.instances
    assert code == 0 and crawler.calls == ["discover_and_extract"]
    assert crawler.kwargs == {
        "run_id": "r9",
        "webhook_url": "https://hooks.example/x",
        "headless": False,
        "auto_login": False,
    }
    assert crawler.config.extraction.output_path == "elsewhere"  # --output overrides the config


def test_crawl_defaults_are_headless_and_use_the_configured_output(config_file):
    cli.main(["crawl", "--config", str(config_file)])
    [crawler] = FakeCrawler.instances
    assert crawler.kwargs == {
        "run_id": None,
        "webhook_url": None,
        "headless": True,
        "auto_login": True,
    }
    assert crawler.config.extraction.output_path == "OUT"


def test_discover_only_calls_discover(config_file):
    cli.main(["crawl", "--config", str(config_file), "--discover-only"])
    assert FakeCrawler.instances[0].calls == ["discover"]


def test_extract_command_passes_the_manifest_and_optional_config(config_file):
    assert (
        cli.main(
            ["extract", "--manifest", "out/manifest.json", "--webhook-url", "https://h.example/x"]
        )
        == 0
    )
    [extractor] = FakeCrawler.instances
    assert extractor.calls == ["extract"] and extractor.manifest == "out/manifest.json"
    assert extractor.config is None
    assert extractor.kwargs["webhook_url"] == "https://h.example/x"

    FakeCrawler.instances.clear()
    cli.main(["extract", "--manifest", "m.json", "--config", str(config_file)])
    assert FakeCrawler.instances[0].config.extraction.output_path == "OUT"


def test_log_level_option_configures_logging(config_file, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli, "configure_logging", lambda level=None, transports=None: seen.append(level)
    )
    cli.main(["crawl", "--config", str(config_file), "--log-level", "debug"])
    assert seen == ["debug"]


def test_a_dotenv_file_in_the_working_directory_is_loaded(config_file, monkeypatch):
    (config_file.parent / ".env").write_text("HEALIX_WEBHOOK_SECRET=from-dotenv\n")
    cli.main(["crawl", "--config", str(config_file)])
    import os

    assert os.environ["HEALIX_WEBHOOK_SECRET"] == "from-dotenv"
    monkeypatch.delenv("HEALIX_WEBHOOK_SECRET")


# --- output & exit codes -------------------------------------------------------- #


def test_success_prints_a_summary_and_exits_zero(config_file, capsys):
    assert cli.main(["crawl", "--config", str(config_file)]) == 0
    out = capsys.readouterr().out
    assert "run run-7: 2 of 2 pages extracted (discovery complete)" in out
    assert "manifest: output/manifest.json" in out


def test_json_output_is_one_parseable_line(config_file, capsys):
    cli.main(["crawl", "--config", str(config_file), "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "run_id": "run-7",
        "discovery_status": "complete",
        "pages_discovered": 2,
        "pages_extracted": 2,
        "pages_failed": 0,
        "platform_detected": None,
        "blocked_on_auth": False,
        "manifest_path": "output/manifest.json",
    }


def test_failed_pages_exit_three_and_say_how_to_retry(config_file, capsys):
    FakeCrawler.run = make_run(failed=1)
    assert cli.main(["crawl", "--config", str(config_file)]) == 3
    out = capsys.readouterr().out
    assert "1 page(s) failed" in out and "--run-id run-7" in out


# --- errors --------------------------------------------------------------------- #


def test_a_missing_config_file_is_a_usage_error(fakes, capsys):
    assert cli.main(["crawl", "--config", "nope.json"]) == 2
    assert "healix: error:" in capsys.readouterr().err
    assert FakeCrawler.instances == []


def test_an_invalid_config_is_a_usage_error_naming_the_problem(fakes, capsys):
    (fakes / "bad.json").write_text(json.dumps({"base_url": "https://e.com", "colour": 1}))
    assert cli.main(["crawl", "--config", "bad.json"]) == 2
    assert "unknown config keys" in capsys.readouterr().err


def test_a_config_holding_credentials_is_refused_and_points_at_dotenv(fakes, capsys):
    (fakes / "leaky.json").write_text(
        json.dumps({"base_url": "https://e.com", "password": "hunter2"})
    )
    assert cli.main(["crawl", "--config", "leaky.json"]) == 2
    err = capsys.readouterr().err
    assert ".env" in err and "hunter2" not in err


def test_a_run_conflict_is_a_usage_error(config_file, capsys):
    FakeCrawler.error = RunConflictError("out/manifest.json already holds run 'x'")
    assert cli.main(["crawl", "--config", str(config_file)]) == 2
    assert "already holds run" in capsys.readouterr().err


def test_an_unavailable_backend_is_a_runtime_error(config_file, capsys):
    FakeCrawler.error = BackendUnavailableError("the selenium backend is not implemented yet")
    assert cli.main(["crawl", "--config", str(config_file)]) == 1
    assert "not implemented" in capsys.readouterr().err


def test_an_unexpected_exception_exits_one_with_its_type(config_file, capsys):
    FakeCrawler.error = RuntimeError("browser crashed")
    assert cli.main(["crawl", "--config", str(config_file)]) == 1
    assert "RuntimeError: browser crashed" in capsys.readouterr().err


def test_interruption_exits_130_and_says_how_to_resume(config_file, capsys):
    FakeCrawler.error = KeyboardInterrupt()
    assert cli.main(["crawl", "--config", str(config_file), "--run-id", "r5"]) == 130
    err = capsys.readouterr().err
    assert "progress is saved" in err and "--run-id r5" in err


def test_extract_reports_a_missing_manifest(fakes, capsys):
    FakeCrawler.error = FileNotFoundError("out/manifest.json")
    assert cli.main(["extract", "--manifest", "out/manifest.json"]) == 2


def test_python_dash_m_healix_runs_the_cli():
    result = subprocess.run(
        [sys.executable, "-m", "healix", "--version"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0 and result.stdout.strip() == f"healix {__version__}"


def test_a_run_blocked_on_auth_exits_four_and_says_which_variables_to_set(config_file, capsys):
    run = make_run()
    run.manifest.blocked_on_auth = True
    FakeCrawler.run = run
    assert cli.main(["crawl", "--config", str(config_file)]) == 4
    out = capsys.readouterr().out
    assert "blocked on authentication" in out
    assert "WEBLIB_LOGIN_USERNAME" in out and "WEBLIB_LOGIN_PASSWORD" in out
    assert "--run-id run-7" in out


def test_blocked_takes_precedence_over_failed_pages(config_file):
    run = make_run(failed=1)
    run.manifest.blocked_on_auth = True
    FakeCrawler.run = run
    assert cli.main(["crawl", "--config", str(config_file)]) == 4


def test_the_blocked_flag_is_in_the_json_summary(config_file, capsys):
    run = make_run()
    run.manifest.blocked_on_auth = True
    FakeCrawler.run = run
    cli.main(["crawl", "--config", str(config_file), "--json"])
    assert json.loads(capsys.readouterr().out)["blocked_on_auth"] is True


def test_extract_also_honours_no_login(config_file):
    cli.main(["extract", "--manifest", "m.json", "--no-login"])
    assert FakeCrawler.instances[0].kwargs["auto_login"] is False
