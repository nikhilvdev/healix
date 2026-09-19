"""Automatic login against a real site in real Chromium.

The server counts every credential submission, so "the password was sent once" is checked from
the server's side, not just the client's.
"""

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from healix import Crawler, Credentials, cli  # noqa: E402
from healix.discovery.manifest import EXTRACTED, FAILED, Manifest  # noqa: E402
from tests.loginsite import PASSWORD, USER, LoginSite  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _browser_available():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:
        pytest.skip(f"cannot launch Chromium: {exc}")


@pytest.fixture
def make_site():
    sites = []

    def factory(**kwargs):
        site = LoginSite(**kwargs)
        sites.append(site)
        return site

    yield factory
    for site in sites:
        site.close()


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("WEBLIB_LOGIN_USERNAME", USER)
    monkeypatch.setenv("WEBLIB_LOGIN_PASSWORD", PASSWORD)


def crawl(site, tmp_path, *, start="autonomous", run_id="r1", **kwargs):
    events = []
    config = {
        "crawl": {
            "discovery": {"max_pages": 30},
            "extraction": {"output_path": str(tmp_path / "out")},
        }
    }
    if start == "autonomous":
        config["base_url"] = site.app_url + "/"
    else:
        config["start_url"] = site.app_url + start
    run = Crawler(config, run_id=run_id, on_event=events.append, **kwargs).discover_and_extract()
    return run, events


def by_path(run):
    """Manifest entries keyed by URL path (query strings dropped)."""
    from urllib.parse import urlsplit

    return {urlsplit(p.url).path: p for p in run.pages}


def page_json(run, path):
    return json.loads((run.output_path / by_path(run)[path].output_file).read_text())


def texts(doc):
    return {e["text_content"] for e in doc["elements"] if e["text_content"]}


def event_names(events):
    return [e["event"] for e in events]


PROTECTED_PAGES = ["/account", "/account/orders", "/account/settings"]


# --- the happy paths ---------------------------------------------------------------- #


def test_autonomous_run_logs_in_when_a_page_bounces_to_the_login_and_carries_on(
    make_site, tmp_path
):
    site = make_site()
    run, events = crawl(site, tmp_path)

    assert site.login_posts == 1
    assert "login_failed" not in event_names(events) and not run.blocked_on_auth
    pages = by_path(run)
    assert {"/", "/about", "/login"} <= set(pages) and set(PROTECTED_PAGES) <= set(pages)
    assert all(p.status == EXTRACTED for p in run.pages)

    # the protected pages hold the real content, not a login form
    for path in PROTECTED_PAGES:
        doc = page_json(run, path)
        assert f"SECRET-CONTENT-{path}" in texts(doc)
        assert not any(e["attributes"].get("type") == "password" for e in doc["elements"])
    assert pages["/login"].page_type == "login"  # the login page itself is recorded, as a login


def test_guided_run_starting_at_the_login_page_logs_in_and_continues_past_it(make_site, tmp_path):
    site = make_site()
    run, events = crawl(site, tmp_path, start="/login")

    assert site.login_posts == 1
    pages = by_path(run)
    assert set(PROTECTED_PAGES) <= set(pages)  # discovery continued past the login page
    assert "SECRET-CONTENT-/account/orders" in texts(page_json(run, "/account/orders"))
    assert "login_failed" not in event_names(events)


def test_session_expiring_during_discovery_logs_in_again_and_resumes(make_site, tmp_path):
    site = make_site(expire_after=3)
    run, events = crawl(site, tmp_path)

    assert site.login_posts == 2  # the first login, and one after the session ended
    assert "login_failed" not in event_names(events)
    pages = by_path(run)
    assert set(PROTECTED_PAGES) <= set(pages)
    assert all(pages[p].status == EXTRACTED for p in PROTECTED_PAGES)


def test_session_expiring_during_extraction_logs_in_again_and_finishes_the_run(make_site, tmp_path):
    # 4 authenticated protected requests happen while discovering; the 6th falls in extraction
    site = make_site(expire_after=6)
    run, events = crawl(site, tmp_path)

    assert site.login_posts == 2
    assert run.pages_extracted == run.pages_discovered and run.pages_failed == 0
    for path in PROTECTED_PAGES:
        assert f"SECRET-CONTENT-{path}" in texts(
            page_json(run, path)
        )  # none was captured as a login
    assert "login_failed" not in event_names(events)


# --- single sign-on ------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "idp_flow, postback",
    [("password", False), ("password", True), ("identifier-first", False)],
    ids=["redirect", "postback", "identifier-first"],
)
def test_sso_login_goes_out_to_the_identity_provider_and_back(
    make_site, tmp_path, idp_flow, postback
):
    site = make_site(mode="sso", idp_flow=idp_flow, idp_postback=postback)
    run, events = crawl(site, tmp_path)

    assert site.idp_posts == 1  # the password was sent to the identity provider exactly once
    assert site.login_posts == 0 and site.evil_posts == 0
    assert "login_failed" not in event_names(events)
    assert "SECRET-CONTENT-/account" in texts(page_json(run, "/account"))
    assert run.pages_failed == 0


# --- failures never hang, never retry --------------------------------------------------- #


def test_wrong_password_is_reported_once_and_never_retried(make_site, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBLIB_LOGIN_PASSWORD", "not-the-password")
    site = make_site()
    run, events = crawl(site, tmp_path)

    assert site.login_posts == 1  # three protected pages bounced; the password was sent once
    failed = [e for e in events if e["event"] == "login_failed"]
    assert len(failed) == 1
    data = failed[0]["data"]
    assert data["reason"] == "auth_rejected"
    assert data["url"].startswith(site.app_url) and "?" not in data["url"]
    assert (run.output_path / data["screenshot_ref"]).read_bytes().startswith(b"\x89PNG")
    assert event_names(events)[-1] == "run_complete"

    pages = by_path(run)
    assert pages["/account"].status == FAILED
    assert "requires authentication" in pages["/account"].error
    assert "/account/orders" not in pages  # never reachable: the crawl could not get past the login


def test_mfa_challenge_aborts_with_mfa_required_and_does_not_hang(make_site, tmp_path):
    site = make_site(mode="mfa")
    started = time.monotonic()
    run, events = crawl(site, tmp_path)

    assert time.monotonic() - started < 90
    failed = [e["data"] for e in events if e["event"] == "login_failed"]
    assert [f["reason"] for f in failed] == ["mfa_required"]
    assert failed[0]["url"] == site.app_url + "/mfa"
    assert site.login_posts == 1 and site.mfa_posts == 0  # never touched the one-time-code form
    assert (run.output_path / failed[0]["screenshot_ref"]).is_file()
    assert run.pages_failed >= 1 and not run.blocked_on_auth


def test_no_credentials_marks_the_run_blocked_on_auth_and_types_nothing(
    make_site, tmp_path, monkeypatch
):
    monkeypatch.delenv("WEBLIB_LOGIN_USERNAME")
    monkeypatch.delenv("WEBLIB_LOGIN_PASSWORD")
    site = make_site()
    run, events = crawl(site, tmp_path)

    assert run.blocked_on_auth and Manifest.load(run.manifest_path).blocked_on_auth
    assert site.login_posts == 0
    assert "login_failed" not in event_names(events)  # no credentials is not a failed login
    assert run.summary()["blocked_on_auth"] is True
    assert "no credentials are set" in by_path(run)["/account"].error


def test_credentials_can_be_passed_explicitly_instead_of_through_the_environment(
    make_site, tmp_path, monkeypatch
):
    monkeypatch.delenv("WEBLIB_LOGIN_USERNAME")
    monkeypatch.delenv("WEBLIB_LOGIN_PASSWORD")
    site = make_site()
    run, _ = crawl(site, tmp_path, credentials=Credentials(USER, PASSWORD))
    assert site.login_posts == 1 and not run.blocked_on_auth and run.pages_failed == 0


def test_auto_login_can_be_turned_off(make_site, tmp_path):
    site = make_site()
    run, events = crawl(site, tmp_path, auto_login=False)
    assert site.login_posts == 0 and not run.blocked_on_auth
    assert "/account/orders" not in by_path(run)  # never got behind the login
    assert "login_failed" not in event_names(events)


def test_a_blocked_run_rediscovers_when_resumed_with_credentials(make_site, tmp_path, monkeypatch):
    site = make_site()
    with monkeypatch.context() as m:
        m.delenv("WEBLIB_LOGIN_USERNAME")
        m.delenv("WEBLIB_LOGIN_PASSWORD")
        blocked, _ = crawl(site, tmp_path, run_id="resume-me")
    assert blocked.blocked_on_auth and "/account/orders" not in by_path(blocked)

    run, _ = crawl(site, tmp_path, run_id="resume-me")  # credentials are back
    assert not run.blocked_on_auth and run.pages_failed == 0
    assert set(PROTECTED_PAGES) <= set(by_path(run))


# --- where credentials may be typed ------------------------------------------------------- #


def test_a_bounce_to_an_untrusted_login_page_never_receives_the_credentials(make_site, tmp_path):
    site = make_site(bounce_to_untrusted=True)
    run, events = crawl(site, tmp_path)

    assert site.evil_posts == 0 and site.login_posts == 0 and site.idp_posts == 0
    assert "login_failed" not in event_names(events)
    assert not set(PROTECTED_PAGES) & set(by_path(run))  # the pages behind it were not reachable


# --- secrets stay secret ------------------------------------------------------------------ #


def test_the_credentials_appear_nowhere_in_logs_events_or_output(
    make_site, tmp_path, captured_logs
):
    site = make_site(mode="mfa")  # a failing run exercises every failure-reporting path too
    run, events = crawl(site, tmp_path)
    ok_site = make_site()
    ok_run, ok_events = crawl(ok_site, tmp_path / "ok")

    haystacks = {
        "logs": json.dumps(captured_logs),
        "events": json.dumps([*events, *ok_events]),
        "summaries": json.dumps([run.summary(), ok_run.summary()]),
    }
    for root in (run.output_path, ok_run.output_path):
        for path in Path(root).rglob("*"):
            if path.is_file() and path.suffix in (".json", ".txt"):
                haystacks[str(path)] = path.read_text()
    for name, text in haystacks.items():
        assert PASSWORD not in text, f"the password leaked into {name}"
        assert USER not in text, f"the username leaked into {name}"


# --- through the CLI -------------------------------------------------------------------------- #


def write_config(path, site, out):
    path.write_text(
        json.dumps(
            {"base_url": site.app_url + "/", "crawl": {"extraction": {"output_path": str(out)}}}
        )
    )
    return path


def test_the_cli_loads_credentials_from_a_dotenv_file_and_logs_in(
    make_site, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("WEBLIB_LOGIN_USERNAME")
    monkeypatch.delenv("WEBLIB_LOGIN_PASSWORD")
    (tmp_path / ".env").write_text(
        f"WEBLIB_LOGIN_USERNAME={USER}\nWEBLIB_LOGIN_PASSWORD={PASSWORD}\n"
    )
    monkeypatch.chdir(tmp_path)
    site = make_site()
    cfg = write_config(tmp_path / "run.json", site, tmp_path / "out")

    assert cli.main(["crawl", "--config", str(cfg), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert site.login_posts == 1 and summary["blocked_on_auth"] is False
    assert summary["pages_failed"] == 0


def test_the_cli_exits_four_when_blocked_on_auth(make_site, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("WEBLIB_LOGIN_USERNAME")
    monkeypatch.delenv("WEBLIB_LOGIN_PASSWORD")
    monkeypatch.chdir(tmp_path)  # no .env here either
    site = make_site()
    cfg = write_config(tmp_path / "run.json", site, tmp_path / "out")

    assert cli.main(["crawl", "--config", str(cfg)]) == 4
    assert "blocked on authentication" in capsys.readouterr().out
    assert site.login_posts == 0


def test_the_cli_no_login_flag_disables_logging_in(make_site, tmp_path, capsys):
    site = make_site()
    cfg = write_config(tmp_path / "run.json", site, tmp_path / "out")
    assert cli.main(["crawl", "--config", str(cfg), "--no-login"]) == 0
    assert site.login_posts == 0
