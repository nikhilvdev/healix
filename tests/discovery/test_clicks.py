"""What click-through discovery will and will not click. No browser: elements are built by hand."""

import pytest

from healix.discovery.clicks import DENY_WORDS, deny_pattern, select_candidates
from healix.driver.base import Element


def el(
    tag="button", *, text=None, css=None, id=None, classes=(), form=False, visible=True, **attrs
):
    """One element as the collector would describe it. ``form`` puts it inside a <form>."""
    path = ["html", "body", *(["form"] if form else []), tag]
    return Element(
        tag=tag,
        id=id,
        classes=list(classes),
        attributes={k.replace("_", "-"): v for k, v in attrs.items()},
        text_content=text,
        computed={"visible": visible, "enabled": attrs.get("disabled") is None},
        css_selector=css or f"{tag}#{id or text or 'x'}".replace(" ", "-"),
        iframe_path=["main"],
        dom_context={"tag_path": path},
    )


def labels(elements, **kwargs):
    return [c.label for c in select_candidates(elements, limit=50, **kwargs).candidates]


# --- what is clickable ----------------------------------------------------------------- #


def test_buttons_script_links_and_role_buttons_are_clickable():
    elements = [
        el("button", text="Orders"),
        el("a", text="Reports", href="#"),
        el("a", text="Help", href="javascript:openHelp()"),
        el("a", text="No href"),
        el("div", text="Settings", role="button"),
        el("span", text="Tab two", role="tab"),
        el("li", text="Menu item", role="menuitem"),
        el("div", text="Clicky", onclick="go()"),
        el("input", type="button", value="Open"),
    ]
    assert labels(elements) == [
        "Orders", "Reports", "Help", "No href", "Settings", "Tab two", "Menu item", "Clicky", "Open"
    ]  # fmt: skip


def test_a_real_link_is_left_to_ordinary_discovery():
    assert (
        labels([el("a", text="About", href="/about"), el("a", text="Away", href="https://x.y/")])
        == []
    )


def test_plain_text_headings_and_inputs_are_not_clickable():
    elements = [el("h1", text="Title"), el("p", text="Body"), el("input", type="text"), el("div")]
    assert labels(elements) == []


def test_hidden_and_disabled_elements_are_skipped():
    elements = [
        el("button", text="Visible"),
        el("button", text="Hidden", visible=False),
        el("button", text="Disabled", disabled=""),
    ]
    assert labels(elements) == ["Visible"]


# --- what is never clicked ------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "Delete", "delete account", "Remove item", "Pay now", "Payments", "Purchase", "Buy",
        "Checkout", "Place order", "Sign out", "Log out", "Logout", "Submit", "Send", "Save",
        "Confirm", "Approve", "Cancel", "Unsubscribe", "Reset password", "Clear all", "Publish",
        "Transfer", "Withdraw", "Deactivate", "Archive", "Block user", "Ban", "Add to cart",
    ],
)  # fmt: skip
def test_a_button_that_commits_something_is_never_clicked(text):
    selection = select_candidates([el("button", text=text)], limit=10)
    assert selection.candidates == [] and selection.skipped_unsafe == 1


@pytest.mark.parametrize(
    "text", ["Orders", "Settings", "Dashboard", "Reports", "Profile", "Bank", "Blocks"]
)
def test_ordinary_navigation_is_not_mistaken_for_something_dangerous(text):
    assert labels([el("button", text=text)]) == [text]


def test_the_word_is_read_from_inside_the_button_not_only_its_own_text():
    button = el("button", css="#b")
    inner = el("span", text="Delete", css="#b > span")
    other = el("button", text="Home", css="#h")
    assert labels([button, inner, other]) == ["Home"]


def test_an_icon_button_is_judged_by_its_accessible_name_and_classes():
    assert labels([el("button", css="#a", aria_label="Delete row")]) == []
    assert labels([el("button", css="#b", title="Remove")]) == []
    assert labels([el("button", css="#c", classes=["btn", "btn-danger"])]) == []
    assert labels([el("button", css="#d", id="deleteBtn")]) == []
    assert labels([el("button", css="#e", data_testid="sign-out")]) == []
    assert labels([el("button", css="#f", aria_label="Open menu")]) == ["Open menu"]


def test_a_handler_that_names_a_dangerous_action_is_enough():
    assert labels([el("div", text="Row", role="button", onclick="deleteRow(4)")]) == []
    assert labels([el("a", text="Go", href="javascript:logout()")]) == []


def test_a_button_in_a_form_would_submit_it_unless_it_says_it_does_not():
    elements = [
        el("button", text="Details", form=True, css="#one"),
        el("button", text="Reveal", form=True, type="button", css="#two"),
        el("button", text="Outside", css="#three"),
    ]
    assert labels(elements) == ["Reveal", "Outside"]


@pytest.mark.parametrize("kind", ["submit", "image", "reset"])
def test_inputs_that_submit_are_never_clicked(kind):
    assert labels([el("input", type=kind, value="Go")]) == []


def test_formaction_and_downloads_are_never_clicked():
    assert labels([el("button", text="Go", formaction="/x", type="button")]) == []
    assert labels([el("a", text="Get", href="#", download="f.csv")]) == []


def test_a_run_can_add_its_own_words():
    elements = [el("button", text="Escalate"), el("button", text="Home")]
    assert labels(elements) == ["Escalate", "Home"]
    assert labels(elements, extra_deny=["escalat"]) == ["Home"]


def test_the_deny_list_never_loses_the_essentials():
    for word in ("delet", "pay", "sign out", "log out", "submit", "remov"):
        assert word in DENY_WORDS
    assert deny_pattern(["  "]).search("delete")  # a blank extra word is ignored, not a wildcard


# --- picking ---------------------------------------------------------------------------- #


def test_the_same_button_repeated_is_clicked_once():
    rows = [el("button", text="Edit", css=f"#row{i} > button") for i in range(5)]
    assert labels(rows) == ["Edit"]


def test_the_limit_bounds_a_page_and_says_how_many_it_left_out():
    buttons = [el("button", text=f"Item {c}", css=f"#{c}") for c in "abcdef"]
    selection = select_candidates(buttons, limit=4)
    assert [c.label for c in selection.candidates] == ["Item a", "Item b", "Item c", "Item d"]
    assert selection.over_limit == 2


def test_order_is_document_order():
    assert labels([el("button", text="B", css="#b"), el("button", text="A", css="#a")]) == [
        "B",
        "A",
    ]


def test_an_element_with_no_selector_cannot_be_found_again_so_it_is_skipped():
    nameless = el("button", text="Go")
    nameless.css_selector = None
    assert labels([nameless]) == []


def test_elements_in_different_frames_are_different_buttons():
    a, b = el("button", text="Open", css="#o"), el("button", text="Open", css="#o")
    b.iframe_path = ["main", "panel"]
    assert len(select_candidates([a, b], limit=5).candidates) == 2
