import json

import pytest

from healix import Credentials
from healix.auth import (
    AUTH_REJECTED,
    AUTHENTICATED,
    BLOCKED,
    FAILED,
    MFA_REQUIRED,
    NOT_NEEDED,
    SELECTOR_NOT_FOUND,
    SKIPPED,
    TIMEOUT,
    LoginHandler,
    redact_url,
)
from tests.auth.screens import Screen, ScreenDriver, dashboard, password_form
from tests.elements import el

APP = "https://app.example.com"
CREDS = Credentials("alice@example.com", "pw-9Xk2-UNIQUE")


def in_scope(url):
    return url.startswith(APP)


def make(driver, creds=CREDS, **kwargs):
    failed = []
    now = [0.0]
    kwargs.setdefault("clock", lambda: now[0])
    kwargs.setdefault("sleep", lambda s: now.__setitem__(0, now[0] + s))
    handler = LoginHandler(
        driver,
        creds,
        in_scope=in_scope,
        on_login_failed=lambda url, reason, ref: failed.append((url, reason, ref)),
        **kwargs,
    )
    return handler, failed


def enter(handler, driver):
    """What a crawler does: it has just loaded the login page and asks the handler."""
    elements = driver.get_elements()
    return handler.handle_page(driver.current_url, driver.current_url, elements, "login")


# --- credentials & url helpers ------------------------------------------------------- #


def test_credentials_never_show_in_repr_or_str():
    assert "alice" not in repr(CREDS) and "UNIQUE" not in repr(CREDS)
    assert "alice" not in f"{CREDS}" and "UNIQUE" not in f"{CREDS}"


def test_credentials_from_env_needs_both_variables():
    env = {"WEBLIB_LOGIN_USERNAME": "u", "WEBLIB_LOGIN_PASSWORD": "p"}
    assert Credentials.from_env(env) == Credentials("u", "p")
    assert Credentials.from_env({"WEBLIB_LOGIN_USERNAME": "u"}) is None
    assert Credentials.from_env({"WEBLIB_LOGIN_PASSWORD": "p"}) is None
    assert Credentials.from_env({"WEBLIB_LOGIN_USERNAME": "", "WEBLIB_LOGIN_PASSWORD": "p"}) is None
    assert Credentials.from_env({}) is None


def test_credentials_from_env_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("WEBLIB_LOGIN_USERNAME", "envuser")
    monkeypatch.setenv("WEBLIB_LOGIN_PASSWORD", "envpass")
    assert Credentials.from_env() == Credentials("envuser", "envpass")


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://idp.example/oauth/authorize?state=SECRET&code=abc#frag",
            "https://idp.example/oauth/authorize",
        ),
        ("https://user:pw@host.example:8443/a/b?x=1", "https://host.example:8443/a/b"),
        ("http://[::1]:8080/login?next=/x", "http://[::1]:8080/login"),
    ],
)
def test_redact_url_drops_userinfo_query_and_fragment(url, expected):
    assert redact_url(url) == expected


# --- when the handler acts ------------------------------------------------------------ #


def screens_ok():
    return (
        {
            "login": Screen(f"{APP}/login", password_form()),
            "home": Screen(f"{APP}/home", dashboard()),
        },
        {("login", "#f > #go"): "home"},
    )


def test_a_page_that_is_not_a_login_page_needs_nothing():
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "home", clicks)
    handler, _ = make(driver)
    result = handler.handle_page(f"{APP}/home", f"{APP}/home", driver.get_elements(), "detail")
    assert result.status == NOT_NEEDED and driver.writes == []


def test_a_successful_login_types_each_credential_once_and_reports_the_landing_page():
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver)
    result = enter(handler, driver)
    assert result.status == AUTHENTICATED and result.authenticated
    assert result.landing_url == f"{APP}/home"
    assert [(s, t) for s, t, _ in driver.writes] == [
        ("#f > #u", "alice@example.com"),
        ("#f > #p", "pw-9Xk2-UNIQUE"),
    ]
    assert driver.clicks == ["#f > #go"]
    assert handler.authenticated and handler.logins == 1 and failed == []


def test_no_credentials_marks_the_run_blocked_and_types_nothing(captured_logs):
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver, creds=None)
    assert enter(handler, driver).status == BLOCKED
    assert enter(handler, driver).status == BLOCKED  # still blocked, and it warns only once
    assert handler.blocked and driver.writes == [] and failed == []
    assert [r["message"] for r in captured_logs if r["level"] == "WARN"].count(
        "login page reached but no credentials are set; the run is blocked on auth"
    ) == 1


def test_a_login_page_visited_directly_after_logging_in_needs_no_second_login():
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)
    handler, _ = make(driver)
    enter(handler, driver)
    driver.current = "login"
    assert enter(handler, driver).status == NOT_NEEDED  # not a bounce: just a page to record
    assert len(driver.writes) == 2


def test_a_bounce_to_the_login_page_after_logging_in_is_session_expiry_and_logs_in_again():
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)
    handler, _ = make(driver)
    enter(handler, driver)
    driver.current = "login"
    result = handler.handle_page(f"{APP}/orders", f"{APP}/login", driver.get_elements(), "login")
    assert result.status == AUTHENTICATED and handler.logins == 2
    assert len(driver.writes) == 4


def test_re_logins_are_capped_so_an_expiring_session_cannot_loop():
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver, max_logins=2)
    for _ in range(2):
        driver.current = "login"
        assert handler.handle_page(
            f"{APP}/x", f"{APP}/login", driver.get_elements(), "login"
        ).authenticated
    driver.current = "login"
    result = handler.handle_page(f"{APP}/x", f"{APP}/login", driver.get_elements(), "login")
    assert result.status == FAILED and result.reason == AUTH_REJECTED
    assert [(r) for _, r, _ in failed] == [AUTH_REJECTED]
    assert len(driver.writes) == 4  # two logins, and no third


# --- failures ------------------------------------------------------------------------- #


def rejected_screens(after):
    return (
        {
            "login": Screen(f"{APP}/login", password_form()),
            "after": Screen(f"{APP}/login", after),
        },
        {("login", "#f > #go"): "after", ("after", "#f > #go"): "after"},
    )


def test_a_rejected_password_is_never_submitted_twice():
    screens, clicks = rejected_screens(password_form(error="Invalid username or password"))
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver)
    result = enter(handler, driver)
    assert result.status == FAILED and result.reason == AUTH_REJECTED
    assert [t for _, t, _ in driver.writes].count("pw-9Xk2-UNIQUE") == 1  # the point of the rule
    assert driver.clicks == ["#f > #go"]
    assert failed[0][1] == AUTH_REJECTED


def test_after_any_failure_login_is_given_up_for_the_rest_of_the_run():
    screens, clicks = rejected_screens(password_form(error="Invalid username or password"))
    driver = ScreenDriver(screens, "login", clicks)
    handler, _ = make(driver)
    enter(handler, driver)
    driver.current = "login"
    again = handler.handle_page(f"{APP}/x", f"{APP}/login", driver.get_elements(), "login")
    assert again.status == SKIPPED
    assert len(driver.writes) == 2  # the first attempt only


def mfa_screens():
    return (
        {
            "login": Screen(f"{APP}/login", password_form()),
            "mfa": Screen(
                f"{APP}/mfa",
                [
                    el("p", text="Enter the code from your authenticator app"),
                    el("input", sel="#otp", type="text", autocomplete="one-time-code"),
                    el("button", sel="#verify", text="Verify"),
                ],
            ),
        },
        {("login", "#f > #go"): "mfa"},
    )


def test_an_mfa_challenge_aborts_with_mfa_required_and_does_not_hang():
    screens, clicks = mfa_screens()
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver)
    result = enter(handler, driver)
    assert (result.status, result.reason) == (FAILED, MFA_REQUIRED)
    assert failed[0][1] == MFA_REQUIRED
    assert all(sel != "#otp" for sel, _, _ in driver.writes)  # never types into the OTP box
    assert len(driver.writes) == 2


def test_a_login_page_that_starts_as_an_mfa_prompt_aborts_before_typing_anything():
    screens, clicks = mfa_screens()
    driver = ScreenDriver(screens, "mfa", clicks)
    handler, failed = make(driver)
    assert enter(handler, driver).reason == MFA_REQUIRED
    assert driver.writes == []


def test_a_failure_saves_a_screenshot_and_reports_it_relative_to_the_output_directory(tmp_path):
    screens, clicks = mfa_screens()
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver, screenshots_dir=tmp_path / "out" / "screenshots")
    result = enter(handler, driver)
    assert result.screenshot_ref == "screenshots/login-failed-1.png"
    assert failed[0][2] == "screenshots/login-failed-1.png"
    assert (tmp_path / "out" / result.screenshot_ref).read_bytes() == b"\x89PNG-fake"


def test_without_a_screenshots_directory_the_reference_is_none():
    screens, clicks = mfa_screens()
    handler, failed = make(ScreenDriver(screens, "login", clicks))
    enter(handler, handler.driver)
    assert failed[0][2] is None


def test_a_failing_screenshot_does_not_fail_the_failure_report(tmp_path):
    screens, clicks = mfa_screens()
    driver = ScreenDriver(screens, "login", clicks)
    driver.screenshot = lambda: (_ for _ in ()).throw(RuntimeError("no display"))
    handler, failed = make(driver, screenshots_dir=tmp_path / "s")
    assert enter(handler, driver).reason == MFA_REQUIRED
    assert failed[0][2] is None


def test_a_raising_login_failed_callback_does_not_propagate():
    screens, clicks = mfa_screens()
    driver = ScreenDriver(screens, "login", clicks)
    handler = LoginHandler(driver, CREDS, in_scope=in_scope, on_login_failed=lambda *a: 1 / 0)
    assert enter(handler, driver).status == FAILED


def test_the_failure_url_is_reported_without_its_query_string():
    screens, clicks = mfa_screens()
    screens["mfa"].url = f"{APP}/mfa?state=SECRET-STATE&code=SECRET-CODE"
    driver = ScreenDriver(screens, "login", clicks)
    handler, failed = make(driver)
    enter(handler, driver)
    assert failed[0][0] == f"{APP}/mfa"
    assert "SECRET" not in json.dumps(failed)


def test_a_form_with_no_submit_control_fails_with_selector_not_found_and_types_nothing():
    page = [el("input", sel="#u", name="username"), el("input", sel="#p", type="password")]
    driver = ScreenDriver({"login": Screen(f"{APP}/login", page)}, "login", {})
    handler, failed = make(driver)
    result = enter(handler, driver)
    assert (result.status, result.reason) == (FAILED, SELECTOR_NOT_FOUND)
    assert driver.writes == []


def test_a_click_that_changes_nothing_times_out_instead_of_hanging():
    driver = ScreenDriver({"login": Screen(f"{APP}/login", password_form())}, "login", {})
    handler, failed = make(driver, timeout=5.0)
    result = enter(handler, driver)
    assert (result.status, result.reason) == (FAILED, TIMEOUT)
    assert len(driver.writes) == 2


def test_a_driver_error_while_operating_the_form_is_selector_not_found_and_leaks_no_secret(
    captured_logs,
):
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)

    def broken_write(text, into):
        raise RuntimeError(f"could not fill {text}")  # an exception that echoes its input

    driver.write = broken_write
    handler, failed = make(driver)
    assert enter(handler, driver).reason == SELECTOR_NOT_FOUND
    assert "UNIQUE" not in json.dumps(captured_logs) and "alice" not in json.dumps(captured_logs)


def test_pages_that_cannot_be_read_mid_navigation_are_waited_out():
    screens, clicks = screens_ok()
    driver = ScreenDriver(screens, "login", clicks)
    handler, _ = make(driver)
    elements = driver.get_elements()
    driver.unreadable_reads = 3
    result = handler.handle_page(driver.current_url, driver.current_url, elements, "login")
    assert result.authenticated


def test_a_step_limit_stops_a_flow_that_never_finishes():
    # every click leads to a fresh, different login-looking screen: no progress, no end
    screens = {f"s{i}": Screen(f"{APP}/l{i}", password_form(error=f"try {i}")) for i in range(10)}
    clicks = {(f"s{i}", "#f > #go"): f"s{i + 1}" for i in range(9)}
    driver = ScreenDriver(screens, "s0", clicks)
    handler, _ = make(driver, max_steps=3)
    result = enter(handler, driver)
    assert result.status == FAILED
    assert [t for _, t, _ in driver.writes].count("pw-9Xk2-UNIQUE") == 1


# --- single sign-on ---------------------------------------------------------------------- #

IDP = "https://idp.example.net/oauth/authorize"


def sso_screens(idp_url=IDP):
    return (
        {
            "app": Screen(
                f"{APP}/login",
                [
                    el("h1", sel="h", text="Welcome"),
                    el("button", sel="#sso", text="Sign in with SSO"),
                ],
            ),
            "idp": Screen(idp_url + "?client_id=x&state=s", password_form()),
            "back": Screen(f"{APP}/home", dashboard()),
        },
        {("app", "#sso"): "idp", ("idp", "#f > #go"): "back"},
    )


def test_an_sso_login_follows_the_button_to_the_identity_provider_and_back():
    screens, clicks = sso_screens()
    driver = ScreenDriver(screens, "app", clicks)
    handler, failed = make(driver)
    result = enter(handler, driver)
    assert result.authenticated and result.landing_url == f"{APP}/home"
    assert driver.clicks == ["#sso", "#f > #go"]
    assert all(url.startswith(IDP) for _, _, url in driver.writes)  # credentials only at the IdP
    assert len(driver.writes) == 2 and failed == []


def test_identifier_first_providers_get_the_username_then_the_password():
    idp_user = [
        el("input", sel="#id", type="email", name="identifier"),
        el("button", sel="#next", text="Next"),
    ]
    idp_pass = [
        el("input", sel="#pw", type="password", name="password"),
        el("button", sel="#signin", type="submit", text="Sign in"),
    ]
    screens = {
        "app": Screen(
            f"{APP}/login",
            [
                el("h1", sel="h", text="Sign in"),
                el("button", sel="#sso", text="Continue with Okta"),
            ],
        ),
        "idp1": Screen("https://login.okta.com/oauth2/authorize?a=1", idp_user),
        "idp2": Screen("https://login.okta.com/oauth2/authorize?a=2", idp_pass),
        "back": Screen(f"{APP}/home", dashboard()),
    }
    clicks = {("app", "#sso"): "idp1", ("idp1", "#next"): "idp2", ("idp2", "#signin"): "back"}
    driver = ScreenDriver(screens, "app", clicks)
    handler, _ = make(driver)
    assert enter(handler, driver).authenticated
    assert [(s, t) for s, t, _ in driver.writes] == [
        ("#id", "alice@example.com"),
        ("#pw", "pw-9Xk2-UNIQUE"),
    ]


def test_credentials_are_refused_on_an_out_of_scope_page_that_is_not_an_sso_endpoint():
    screens, clicks = sso_screens(idp_url="https://evil.example.org/login")
    driver = ScreenDriver(screens, "app", clicks)
    handler, failed = make(driver)
    result = enter(handler, driver)
    assert (result.status, result.reason) == (FAILED, SELECTOR_NOT_FOUND)
    assert driver.writes == []  # nothing was typed on the untrusted page
    assert driver.clicks == ["#sso"]


def test_a_login_page_that_is_itself_untrusted_is_refused_outright():
    driver = ScreenDriver({"x": Screen("https://evil.example.org/login", password_form())}, "x", {})
    handler, _ = make(driver)
    assert enter(handler, driver).reason == SELECTOR_NOT_FOUND
    assert driver.writes == []


# --- credentials per role -------------------------------------------------------------------- #


def test_a_roles_credentials_are_read_from_variables_named_after_it():
    env = {
        "WEBLIB_LOGIN_USERNAME": "plain-user",
        "WEBLIB_LOGIN_PASSWORD": "plain-pass",
        "WEBLIB_LOGIN_USERNAME_ADMIN": "admin-user",
        "WEBLIB_LOGIN_PASSWORD_ADMIN": "admin-pass",
    }
    assert Credentials.from_env(env) == Credentials("plain-user", "plain-pass")
    assert Credentials.from_env(env, role="admin") == Credentials("admin-user", "admin-pass")
    assert Credentials.from_env(env, role="standard") is None


def test_a_role_needs_both_of_its_variables_and_never_falls_back_to_the_plain_ones():
    env = {
        "WEBLIB_LOGIN_USERNAME": "u",
        "WEBLIB_LOGIN_PASSWORD": "p",
        "WEBLIB_LOGIN_USERNAME_A": "x",
    }
    assert Credentials.from_env(env, role="a") is None


def test_the_anonymous_role_has_no_credentials_even_if_the_variables_exist():
    env = {"WEBLIB_LOGIN_USERNAME_ANONYMOUS": "u", "WEBLIB_LOGIN_PASSWORD_ANONYMOUS": "p"}
    assert Credentials.from_env(env, role="anonymous") is None
