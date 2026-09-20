"""The command line for multi-role runs: which class it picks, what it passes, what it says."""

import json
from pathlib import Path

import pytest

from healix import cli
from healix.discovery.manifest import Manifest
from healix.rolediff import RoleDiff
from healix.sdk import MultiRoleRun, Run

CONFIG = {
    "base_url": "https://e.com/",
    "roles": ["admin", "standard"],
    "crawl": {"extraction": {"output_path": "OUT"}},
}


def make_run(role, *, failed=False, blocked=False):
    manifest = Manifest("run-9", role=role)
    manifest.add_page("https://e.com/a", "h1")
    manifest.add_page("https://e.com/b", "h2")
    manifest.mark_extracted("https://e.com/a", "pages/1.json")
    if failed:
        manifest.mark_failed("https://e.com/b", "boom")
    else:
        manifest.mark_extracted("https://e.com/b", "pages/2.json")
    manifest.discovery_status = "complete"
    manifest.blocked_on_auth = blocked
    out = Path("OUT/roles") / role
    return Run("run-9", manifest, out / "manifest.json", out)


def make_result(*, failed=False, blocked=False, diff=True):
    runs = {
        "admin": make_run("admin"),
        "standard": make_run("standard", failed=failed, blocked=blocked),
    }
    compared = (
        RoleDiff(
            "run-9",
            ["admin", "standard"],
            {"admin": 2, "standard": 2},
            1,
            page_differences=[
                {"url": "https://e.com/x", "roles": ["admin"], "missing": ["standard"]}
            ],
        )
        if diff
        else None
    )
    return MultiRoleRun(
        "run-9", runs, Path("OUT"), compared, Path("OUT/roles-diff.json") if diff else None
    )


class FakeRoleCrawler:
    instances: list["FakeRoleCrawler"] = []
    result = None

    def __init__(self, config, **kwargs):
        self.config, self.kwargs, self.calls = config, kwargs, []
        type(self).instances.append(self)

    def discover(self):
        self.calls.append("discover")
        return FakeRoleCrawler.result

    def discover_and_extract(self):
        self.calls.append("discover_and_extract")
        return FakeRoleCrawler.result


class FakeCrawler(FakeRoleCrawler):
    instances: list["FakeCrawler"] = []


class FakeExtractor:
    instances: list["FakeExtractor"] = []

    def __init__(self, config=None, **kwargs):
        self.config, self.kwargs = config, kwargs
        FakeExtractor.instances.append(self)

    def extract(self, manifest):
        return make_run("standard")


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    FakeRoleCrawler.instances, FakeRoleCrawler.result = [], make_result()
    FakeCrawler.instances, FakeExtractor.instances = [], []
    monkeypatch.setattr(cli, "RoleCrawler", FakeRoleCrawler)
    monkeypatch.setattr(cli, "Crawler", FakeCrawler)
    monkeypatch.setattr(cli, "Extractor", FakeExtractor)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def config_file(fakes, raw=CONFIG):
    path = fakes / "run.json"
    path.write_text(json.dumps(raw))
    return path


def test_a_config_with_roles_is_run_by_the_role_crawler(fakes):
    assert cli.main(["crawl", "--config", str(config_file(fakes))]) == 0
    [crawler] = FakeRoleCrawler.instances
    assert crawler.calls == ["discover_and_extract"] and FakeCrawler.instances == []
    assert crawler.config.roles == ("admin", "standard")


def test_a_config_without_roles_is_run_by_the_plain_crawler(fakes):
    plain = {"base_url": "https://e.com/"}
    cli.main(["crawl", "--config", str(config_file(fakes, plain))])
    assert len(FakeCrawler.instances) == 1 and FakeRoleCrawler.instances == []


def test_role_limits_the_crawl_to_the_roles_named(fakes):
    cli.main(["crawl", "--config", str(config_file(fakes)), "--role", "standard"])
    assert FakeRoleCrawler.instances[0].kwargs["only"] == ["standard"]
    FakeRoleCrawler.instances.clear()
    cli.main(
        ["crawl", "--config", str(config_file(fakes)), "--role", "admin", "--role", "standard"]
    )
    assert FakeRoleCrawler.instances[0].kwargs["only"] == ["admin", "standard"]


def test_role_without_roles_in_the_config_is_a_usage_error(fakes, capsys):
    plain = {"base_url": "https://e.com/"}
    code = cli.main(["crawl", "--config", str(config_file(fakes, plain)), "--role", "admin"])
    assert code == 2 and "roles" in capsys.readouterr().err


def test_an_invalid_role_in_the_config_is_a_usage_error(fakes, capsys):
    bad = {"base_url": "https://e.com/", "roles": ["Admin User"]}
    assert cli.main(["crawl", "--config", str(config_file(fakes, bad))]) == 2
    assert "roles" in capsys.readouterr().err


def test_the_report_names_each_role_and_the_difference(fakes, capsys):
    cli.main(["crawl", "--config", str(config_file(fakes))])
    out = capsys.readouterr().out
    assert "run run-9: 2 role(s)" in out
    assert "admin: 2 of 2 pages extracted" in out and "standard: 2 of 2 pages extracted" in out
    assert "compared admin, standard: 1 page difference(s), 0 element difference(s)" in out
    assert "roles-diff.json" in out


def test_json_prints_each_roles_summary_and_the_diff_path(fakes, capsys):
    cli.main(["crawl", "--config", str(config_file(fakes)), "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert set(summary["roles"]) == {"admin", "standard"}
    assert summary["roles"]["admin"]["role"] == "admin"
    assert summary["differences"] == 1 and summary["diff_path"].endswith("roles-diff.json")


def test_a_failed_page_in_any_role_exits_3_and_says_how_to_retry_that_role(fakes, capsys):
    FakeRoleCrawler.result = make_result(failed=True)
    assert cli.main(["crawl", "--config", str(config_file(fakes))]) == 3
    assert "--run-id run-9 --role standard" in capsys.readouterr().out


def test_a_role_blocked_on_login_exits_4_and_names_its_variables(fakes, capsys):
    FakeRoleCrawler.result = make_result(blocked=True)
    assert cli.main(["crawl", "--config", str(config_file(fakes))]) == 4
    assert "WEBLIB_LOGIN_USERNAME_STANDARD" in capsys.readouterr().out


def test_no_diff_line_when_there_was_nothing_to_compare(fakes, capsys):
    FakeRoleCrawler.result = make_result(diff=False)
    cli.main(["crawl", "--config", str(config_file(fakes))])
    assert "compared" not in capsys.readouterr().out


def test_extract_can_be_told_which_roles_credentials_to_use(fakes):
    cli.main(["extract", "--manifest", "m.json", "--role", "standard"])
    assert FakeExtractor.instances[0].kwargs["role"] == "standard"
    FakeExtractor.instances.clear()
    cli.main(["extract", "--manifest", "m.json"])
    assert FakeExtractor.instances[0].kwargs["role"] is None  # the manifest's own role is used
