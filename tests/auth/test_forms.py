import pytest

from healix.auth.forms import detect_mfa, find_login_form, login_error_visible
from tests.elements import el


def login_page(**extra):
    return [
        el("form", sel="#login"),
        el("input", sel="#login > input:nth-of-type(1)", name="username", type="text"),
        el("input", sel="#login > input:nth-of-type(2)", name="password", type="password"),
        el("button", sel="#login > button", type="submit", text="Sign in"),
    ]


def test_finds_username_password_and_submit():
    form = find_login_form(login_page())
    assert form.username.name == "username"
    assert form.password.name == "password"
    assert form.submit.text_content == "Sign in"
    assert form.sso == []


def test_username_is_the_input_closest_before_the_password():
    page = [
        el("form", sel="#f"),
        el("input", sel="#f > a", name="first", type="text"),
        el("input", sel="#f > b", name="second", type="text"),
        el("input", sel="#f > c", name="pw", type="password"),
        el("button", sel="#f > d", type="submit", text="Log in"),
    ]
    assert find_login_form(page).username.name == "second"


def test_a_hinted_username_beats_a_closer_unhinted_input():
    page = [
        el("form", sel="#f"),
        el("input", sel="#f > a", name="email", type="email"),
        el("input", sel="#f > b", name="tenant", type="text"),
        el("input", sel="#f > c", name="pw", type="password"),
        el("button", sel="#f > d", type="submit"),
    ]
    assert find_login_form(page).username.name == "email"


def test_search_boxes_are_never_the_username():
    page = [
        el("input", sel="a", type="search", name="q"),
        el("input", sel="b", type="text", placeholder="Search the site"),
        el("input", sel="c", type="password", name="pw"),
        el("button", sel="d", type="submit", text="Sign in"),
    ]
    assert find_login_form(page).username is None


def test_several_password_fields_mean_no_password_step():
    page = [
        el("input", sel="a", type="password", name="new"),
        el("input", sel="b", type="password", name="confirm"),
        el("button", sel="c", type="submit", text="Sign up"),
    ]
    assert find_login_form(page).password is None


def test_hidden_and_disabled_controls_are_ignored():
    page = [
        el("form", sel="#f"),
        el("input", sel="#f > a", name="ghost", type="text", visible=False),
        el("input", sel="#f > b", name="user", type="text"),
        el("input", sel="#f > c", name="pw", type="password"),
        el("button", sel="#f > d", type="submit", text="Sign in", enabled=False),
        el("button", sel="#f > e", type="submit", text="Log in"),
    ]
    form = find_login_form(page)
    assert form.username.name == "user"
    assert form.submit.text_content == "Log in"


def test_submit_prefers_the_password_field_form_over_another_form():
    page = [
        el("form", sel="#news"),
        el("input", sel="#news > email", name="email", type="email"),
        el("button", sel="#news > go", type="submit", text="Subscribe"),
        el("form", sel="#login"),
        el("input", sel="#login > u", name="username", type="text"),
        el("input", sel="#login > p", name="password", type="password"),
        el("button", sel="#login > go", type="submit", text="Sign in"),
    ]
    form = find_login_form(page)
    assert form.submit.css_selector == "#login > go"
    assert form.username.css_selector == "#login > u"


def test_submit_can_be_an_input_button_labelled_by_its_value():
    page = [
        el("input", sel="a", name="user", type="text"),
        el("input", sel="b", name="pw", type="password"),
        el("input", sel="c", type="submit", value="Log in"),
    ]
    assert find_login_form(page).submit.css_selector == "c"


def test_a_form_in_an_iframe_is_found_with_its_iframe_path():
    page = [
        el("input", sel="u", name="username", type="text", iframe=("main", "auth")),
        el("input", sel="p", name="password", type="password", iframe=("main", "auth")),
        el("button", sel="s", type="submit", text="Sign in", iframe=("main", "auth")),
        el("button", sel="other", type="submit", text="Sign in", iframe=("main",)),
    ]
    form = find_login_form(page)
    assert form.submit.iframe_path == ["main", "auth"]
    assert form.username.iframe_path == ["main", "auth"]


def test_no_submit_control_means_none():
    page = [el("input", sel="a", name="user"), el("input", sel="b", type="password")]
    assert find_login_form(page).submit is None


def test_sso_buttons_are_found_by_text():
    page = [
        el("button", sel="g", text="Sign in with Google"),
        el("a", sel="o", text="Use your Okta account", href="/sso"),
        el("button", sel="x", text="Cancel"),
    ]
    assert [e.css_selector for e in find_login_form(page).sso] == ["g", "o"]


def test_identifier_first_step_has_a_username_and_a_next_button_but_no_password():
    page = [
        el("input", sel="u", type="email", name="identifier"),
        el("button", sel="n", text="Next"),
    ]
    form = find_login_form(page)
    assert form.username.name == "identifier" and form.password is None
    assert form.submit.css_selector == "n"


# --- MFA --------------------------------------------------------------------------- #


def test_a_one_time_code_input_is_mfa():
    assert detect_mfa([el("input", sel="a", type="text", autocomplete="one-time-code")])
    assert detect_mfa([el("input", sel="a", type="tel", name="otp")])
    assert detect_mfa([el("input", sel="a", type="text", placeholder="6-digit verification code")])


def test_two_factor_wording_with_a_text_input_is_mfa():
    page = [
        el("p", text="Enter the code from your authenticator app"),
        el("input", sel="a", type="text"),
    ]
    assert detect_mfa(page)


def test_wording_without_an_input_is_not_mfa():
    assert not detect_mfa([el("p", text="Enable two-factor authentication in settings")])


def test_a_plain_login_page_is_not_mfa():
    assert not detect_mfa(login_page())


def test_hidden_mfa_prompts_do_not_count():
    assert not detect_mfa([el("input", sel="a", type="text", name="otp", visible=False)])


# --- rejected logins ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "message",
    [
        "Invalid username or password",
        "Incorrect password. Try again.",
        "Login failed",
        "Sign-in failed: please check your details",
        "Your account is locked",
        "Too many failed attempts",
        "We couldn't find your account",
        "Access denied",
    ],
)
def test_error_messages_are_recognised(message):
    assert login_error_visible([el("div", text=message)])


@pytest.mark.parametrize(
    "message", ["Welcome back", "Sign in to continue", "Forgot your password?"]
)
def test_ordinary_login_page_text_is_not_an_error(message):
    assert not login_error_visible([el("div", text=message)])


def test_a_hidden_error_message_does_not_count():
    assert not login_error_visible([el("div", text="Invalid username or password", visible=False)])
