import pytest

from healix.healing.roles import assign_roles, derive_role, element_kind, is_healable
from tests.elements import el
from tests.healing.pages import password, submit, username


@pytest.mark.parametrize(
    "element, kind",
    [
        (el("input", type="text"), "textbox"),
        (el("input"), "textbox"),
        (el("input", type="email"), "textbox"),
        (el("input", type="password"), "textbox"),
        (el("input", type="search"), "searchbox"),
        (el("input", type="checkbox"), "checkbox"),
        (el("input", type="radio"), "radio"),
        (el("input", type="submit"), "button"),
        (el("button"), "button"),
        (el("a", href="/x"), "link"),
        (el("select"), "combobox"),
        (el("textarea"), "textbox"),
        (el("div", role="tab"), "tab"),
        (el("input", type="text", role="combobox"), "combobox"),
    ],
)
def test_element_kind(element, kind):
    assert element_kind(element) == kind


def test_roles_use_the_most_stable_identity_available():
    assert derive_role(username()) == "textbox:login-username"  # the test id wins
    assert derive_role(username(data_testid=None, aria_label="Your name")) == "textbox:your-name"
    assert derive_role(el("input", type="text", name="user_email")) == "textbox:user-email"
    assert (
        derive_role(el("input", type="text", placeholder="Search products", dom={}))
        == "textbox:search-products"
    )
    assert derive_role(submit()) == "button:sign-in"


def test_a_label_names_a_field_when_nothing_better_exists():
    field = el("input", type="text", dom={"nearby_label_text": "Email address"})
    assert derive_role(field) == "textbox:email-address"


def test_links_are_named_by_their_text_or_href_tail():
    assert derive_role(el("a", text="Forgot password?", href="/forgot")) == "link:forgot-password"
    assert derive_role(el("a", href="/help/contact-us/")) == "link:contact-us"


def test_an_element_with_no_identity_is_just_its_kind():
    assert derive_role(el("input", type="checkbox")) == "checkbox"


def test_roles_do_not_depend_on_volatile_ids_or_position():
    a = username(data_testid=None, aria_label=None, name=None, id="user-4471")
    b = username(data_testid=None, aria_label=None, name=None, id="user-9032", sel="other")
    assert derive_role(a) == derive_role(b)  # both normalise to user-{n}


def test_long_identities_are_truncated():
    role = derive_role(
        el("button", text="Please read this very long instruction carefully before continuing " * 3)
    )
    assert len(role.split(":")[1]) <= 40 and not role.endswith("-")


def test_duplicate_roles_are_numbered_in_document_order():
    page = [
        el("button", text="Save", sel="a"),
        el("input", type="text", name="q", sel="b"),
        el("button", text="Save", sel="c"),
        el("button", text="Save", sel="d"),
    ]
    assert [r for r, _ in assign_roles(page)] == [
        "button:save",
        "textbox:q",
        "button:save#2",
        "button:save#3",
    ]
    assert [e.css_selector for _, e in assign_roles(page)] == ["a", "b", "c", "d"]


def test_only_things_a_script_would_act_on_are_healable():
    assert all(
        is_healable(e)
        for e in (username(), password(), submit(), el("a", href="/x"), el("div", role="button"))
    )
    assert is_healable(el("div", data_testid="card"))  # a test id marks it as targetable
    assert not is_healable(el("div"))
    assert not is_healable(el("p", text="hello"))
    assert not is_healable(el("a"))  # an anchor without a destination
    assert not is_healable(el("input", type="hidden"))
    assert not is_healable(el("button", visible=False))


def test_assign_roles_skips_unhealable_elements():
    page = [el("h1", text="Title"), submit(), el("input", type="hidden", name="csrf")]
    assert [r for r, _ in assign_roles(page)] == ["button:sign-in"]
