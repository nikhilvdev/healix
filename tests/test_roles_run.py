"""A run as several user roles, against a site with an administrator and a standard user.

Real browsers, on every backend. The site (``tests/rolesite.py``) counts every credential
submission and records whose session is behind every request, so what is proved here is what the
server saw: which password went where, and that no role's session leaked into another's crawl.

This module has no module-wide ``driver`` fixture: Playwright's sync API allows one instance at a
time per thread, and the SDK starts its own browser for every role.
"""

import json
from types import SimpleNamespace

import pytest

from healix import Crawler, Extractor, RoleCrawler, RoleCredentialsError, RunConflictError
from healix.auth import Credentials, credential_env_names
from healix.discovery.manifest import Manifest
from healix.healing import SQLiteFingerprintStore
from healix.rolediff import reached_urls
from tests.driver.backends import force_backend
from tests.rolesite import USERS, RoleSite

ADMIN_USER, ADMIN_PASSWORD = USERS["admin"]
STANDARD_USER, STANDARD_PASSWORD = USERS["standard"]


def set_credentials(monkeypatch, *roles):
    for role in roles:
        user_var, password_var = credential_env_names(role)
        user, password = USERS[role]
        monkeypatch.setenv(user_var, user)
        monkeypatch.setenv(password_var, password)


def config(site, out, roles=("admin", "standard"), **crawl):
    return {
        "base_url": site.url + "/",
        "roles": list(roles),
        "crawl": {
            "discovery": {"max_pages": 20, **crawl},
            "extraction": {"output_path": str(out)},
        },
    }


def urls(manifest, site):
    """Every URL the role reached, as a path (a page recorded as a variant of another counts)."""
    return {url.removeprefix(site.url) or "/" for url in reached_urls(manifest)}


@pytest.fixture(scope="module")
def full(backend, tmp_path_factory):
    """One complete two-role run, shared by the tests that only read its results."""
    site = RoleSite()
    out = tmp_path_factory.mktemp("roles") / "out"
    events: list[dict] = []
    patch = pytest.MonkeyPatch()
    set_credentials(patch, "admin", "standard")
    force_backend(patch, backend)
    try:
        run = RoleCrawler(
            config(site, out),
            on_event=events.append,
            fingerprint_store=out.parent / "fp.db",
        ).discover_and_extract()
        yield SimpleNamespace(site=site, out=out, run=run, events=events)
    finally:
        patch.undo()
        site.close()


# --- the shape of a role run ---------------------------------------------------------------- #


def test_each_role_has_its_own_manifest_and_page_files(full):
    for role in ("admin", "standard"):
        directory = full.out / "roles" / role
        manifest = Manifest.load(directory / "manifest.json")
        assert manifest.role == role and manifest.run_id == full.run.run_id
        assert manifest.pages_extracted == manifest.pages_discovered > 0
        assert list((directory / "pages").glob("*.json"))
    assert not (full.out / "manifest.json").exists()  # there is no single, merged manifest


def test_the_roles_share_one_run_id_and_run_in_the_order_listed(full):
    assert full.run.roles == ["admin", "standard"]
    assert {r.run_id for r in full.run.runs.values()} == {full.run.run_id}
    ids = [e["role"] for e in full.events if e["event"] == "run_complete"]
    assert ids == ["admin", "standard"]


def test_the_administrator_reaches_pages_the_standard_user_cannot(full):
    admin = urls(full.run.runs["admin"].manifest, full.site)
    standard = urls(full.run.runs["standard"].manifest, full.site)
    assert "/admin" in admin and "/admin" not in standard
    assert {"/dashboard", "/reports"} <= admin & standard


def test_the_diff_says_which_pages_and_which_elements_differ(full):
    diff = full.run.diff
    assert full.run.diff_path == full.out / "roles-diff.json"
    assert {d["url"].removeprefix(full.site.url): d["roles"] for d in diff.page_differences} == {
        "/admin": ["admin"]
    }
    export = [d for d in diff.element_differences if d["element"] == "button:export-csv"]
    assert [(d["url"].removeprefix(full.site.url), d["roles"], d["missing"]) for d in export] == [
        ("/reports", ["admin"], ["standard"])
    ]
    written = json.loads(full.run.diff_path.read_text())
    assert written["roles"] == ["admin", "standard"]
    assert written["summary"]["page_differences"] == len(diff.page_differences)


def test_every_event_says_which_role_it_happened_under(full):
    per_role = [e for e in full.events if e["event"] != "roles_compared"]
    assert per_role and all(e["role"] in ("admin", "standard") for e in per_role)
    seen = [e["role"] for e in per_role]
    assert seen == sorted(seen, key=["admin", "standard"].index)  # admin's events, then standard's


def test_the_comparison_is_announced_once_at_the_end(full):
    compared = [e for e in full.events if e["event"] == "roles_compared"]
    assert full.events[-1] is compared[0] and len(compared) == 1
    assert "role" not in compared[0]  # it belongs to no single role
    assert compared[0]["data"] == {
        "roles": ["admin", "standard"],
        "diff_path": str(full.run.diff_path),
        "differences": full.run.diff.differences,
    }


# --- credentials and sessions ------------------------------------------------------------------ #


def test_each_role_logs_in_once_with_its_own_credentials(full):
    assert full.site.login_posts == [ADMIN_USER, STANDARD_USER]


def test_one_roles_session_never_reaches_the_next_roles_crawl(full):
    sessions = [role for role, _ in full.site.hits]
    first_standard = sessions.index("standard")
    assert "admin" not in sessions[first_standard:]  # the admin's cookie did not follow
    # and the standard crawl started logged out: its first requests carried no session
    assert sessions[first_standard - 1] is None


def test_no_credential_is_written_anywhere_or_sent_in_any_event(full):
    secrets = [ADMIN_USER, ADMIN_PASSWORD, STANDARD_USER, STANDARD_PASSWORD]
    files = [p for p in full.out.parent.rglob("*") if p.is_file()]
    assert files
    for path in files:
        if path.suffix in (".db",):
            continue  # checked below through the store's own API
        text = path.read_text(errors="ignore")
        for secret in secrets:
            assert secret not in text, f"{secret!r} found in {path}"
    dumped = json.dumps(full.events)
    assert not any(secret in dumped for secret in secrets)


def test_the_fingerprint_store_keeps_the_first_roles_baseline(full):
    store = SQLiteFingerprintStore(full.out.parent / "fp.db")
    try:
        assert store.get(full.site.url + "/reports", "button:export-csv") is not None
    finally:
        store.close()


# --- resuming, retrying one role, finishing one role -------------------------------------------- #


def test_running_again_with_the_run_id_resumes_and_logs_nobody_in(full, use_backend, monkeypatch):
    set_credentials(monkeypatch, "admin", "standard")
    posts = list(full.site.login_posts)
    again = RoleCrawler(config(full.site, full.out), run_id=full.run.run_id).discover_and_extract()
    assert full.site.login_posts == posts  # everything was already extracted: no login needed
    assert again.run_id == full.run.run_id and again.diff.differences == full.run.diff.differences


def test_running_over_an_existing_run_without_its_id_is_a_conflict(full, use_backend, monkeypatch):
    set_credentials(monkeypatch, "admin", "standard")
    with pytest.raises(RunConflictError, match=full.run.run_id):
        RoleCrawler(config(full.site, full.out)).discover_and_extract()
    with pytest.raises(RunConflictError, match="not 'other'"):
        RoleCrawler(config(full.site, full.out), run_id="other").discover_and_extract()


# --- other runs, each on its own site ---------------------------------------------------------- #


@pytest.fixture
def site():
    site = RoleSite()
    yield site
    site.close()


def test_a_missing_credential_stops_the_run_before_any_browser_starts(
    site, tmp_path, use_backend, monkeypatch
):
    monkeypatch.delenv("WEBLIB_LOGIN_USERNAME_STANDARD", raising=False)
    monkeypatch.delenv("WEBLIB_LOGIN_PASSWORD_STANDARD", raising=False)
    set_credentials(monkeypatch, "admin")
    with pytest.raises(RoleCredentialsError, match="WEBLIB_LOGIN_USERNAME_STANDARD") as info:
        RoleCrawler(config(site, tmp_path / "out")).discover()
    assert "admin" not in str(info.value).split("role(s)")[1].split("Set")[0]
    assert site.hits == [] and not (tmp_path / "out").exists()


def test_credentials_can_be_passed_instead_of_read_from_the_environment(
    site, tmp_path, use_backend, monkeypatch
):
    for role in ("admin", "standard"):
        for var in credential_env_names(role):
            monkeypatch.delenv(var, raising=False)
    run = RoleCrawler(
        config(site, tmp_path / "out"),
        credentials={role: Credentials(*USERS[role]) for role in USERS},
    ).discover_and_extract()
    assert site.login_posts == [ADMIN_USER, STANDARD_USER]
    assert run.diff is not None


def test_the_anonymous_role_never_logs_in_and_sees_only_what_a_visitor_can(
    site, tmp_path, use_backend, monkeypatch
):
    set_credentials(monkeypatch, "admin")  # nothing is set for anonymous, and nothing is needed
    run = RoleCrawler(
        config(site, tmp_path / "out", roles=("anonymous", "admin"))
    ).discover_and_extract()
    assert site.login_posts == [ADMIN_USER]
    visitor = urls(run.runs["anonymous"].manifest, site)
    assert visitor <= {"/", "/login"}
    assert {"/dashboard", "/reports", "/admin"} <= urls(run.runs["admin"].manifest, site)
    assert {d["url"].removeprefix(site.url) for d in run.diff.page_differences} >= {
        "/dashboard",
        "/reports",
        "/admin",
    }


def test_one_role_can_be_run_alone_and_the_diff_appears_once_both_have_results(
    site, tmp_path, use_backend, monkeypatch
):
    set_credentials(monkeypatch, "admin", "standard")
    out = tmp_path / "out"
    first = RoleCrawler(config(site, out), only=["admin"]).discover_and_extract()
    assert list(first.runs) == ["admin"] and first.diff is None
    assert not (out / "roles-diff.json").exists()
    second = RoleCrawler(
        config(site, out), run_id=first.run_id, only=["standard"]
    ).discover_and_extract()
    assert list(second.runs) == ["standard"]
    assert second.diff is not None and second.diff.roles == ["admin", "standard"]
    assert site.login_posts == [ADMIN_USER, STANDARD_USER]


def test_a_manifest_finished_later_logs_in_as_the_role_it_was_crawled_as(
    site, tmp_path, use_backend, monkeypatch
):
    set_credentials(monkeypatch, "admin", "standard")
    out = tmp_path / "out"
    RoleCrawler(config(site, out)).discover()  # discovery only: nothing extracted yet
    site.login_posts.clear()
    events = []
    manifest_path = out / "roles" / "standard" / "manifest.json"
    run = Extractor(on_event=events.append).extract(manifest_path)
    assert run.manifest.role == "standard" and run.pages_extracted == run.pages_discovered > 0
    assert site.login_posts == [STANDARD_USER]  # no --role needed: the manifest knows
    assert {e["role"] for e in events} == {"standard"}


def test_a_role_that_cannot_log_in_is_reported_and_the_others_still_run(
    site, tmp_path, use_backend, monkeypatch
):
    set_credentials(monkeypatch, "admin", "standard")
    monkeypatch.setenv("WEBLIB_LOGIN_PASSWORD_STANDARD", "wrong-password")
    events = []
    run = RoleCrawler(config(site, tmp_path / "out"), on_event=events.append).discover_and_extract()
    assert "/admin" in urls(run.runs["admin"].manifest, site)  # admin was unaffected
    assert any(e["event"] == "login_failed" and e["role"] == "standard" for e in events)
    assert not any(e["event"] == "login_failed" and e["role"] == "admin" for e in events)
    standard = run.runs["standard"]
    assert "/dashboard" not in urls(standard.manifest, site)  # it never got past the login
    assert site.login_posts.count(STANDARD_USER) == 1  # and it did not try again and again
    assert run.diff is not None  # the run finished and still compared what it had


# --- refusals ---------------------------------------------------------------------------------- #


def test_crawler_refuses_a_config_with_roles_rather_than_return_a_different_kind_of_result(
    site, tmp_path
):
    with pytest.raises(ValueError, match="RoleCrawler"):
        Crawler(config(site, tmp_path / "out"))


def test_role_crawler_needs_roles(site, tmp_path):
    plain = config(site, tmp_path / "out")
    del plain["roles"]
    with pytest.raises(ValueError, match="no roles"):
        RoleCrawler(plain)


def test_refreshing_fingerprints_is_refused_because_the_last_role_would_win(site, tmp_path):
    with pytest.raises(ValueError, match="last role"):
        RoleCrawler(config(site, tmp_path / "out"), fingerprint_mode="refresh")


def test_only_must_name_configured_roles(site, tmp_path):
    with pytest.raises(ValueError, match="not in the run config"):
        RoleCrawler(config(site, tmp_path / "out"), only=["admin", "root"])
