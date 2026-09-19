import itertools
import re
from pathlib import Path

import pytest

from healix.classification import PAGE_TYPES, UNKNOWN, classify, classify_page
from healix.classification.rules import PRIORITY, RULES, _page_direction, _parent_selectors
from healix.driver.base import Element

_ids = itertools.count(1)


def el(
    tag, *, text=None, sel=None, classes=(), id=None, name=None, visible=True, box=None, **attrs
):
    """A synthetic Element. ``attrs`` become HTML attributes (``aria_label`` -> ``aria-label``)."""
    computed = {"visible": visible, "enabled": True}
    if box is not None:
        computed["bounding_box"] = {"x": 0, "y": 0, "width": box[0], "height": box[1]}
    for key in ("position", "z_index", "readonly"):
        if key in attrs:
            computed[key] = attrs.pop(key)
    return Element.from_dict(
        {
            "tag": tag,
            "id": id,
            "name": name,
            "classes": list(classes),
            "attributes": {k.replace("_", "-"): v for k, v in attrs.items()},
            "text_content": text,
            "computed": computed,
            "css_selector": sel or f"html > body > {tag}:nth-of-type({next(_ids)})",
        },
        iframe_path=["main"],
    )


def types_of(elements, url=""):
    return classify(elements, url)


# --- API shape -------------------------------------------------------------- #


def test_every_page_type_has_exactly_one_rule_and_a_priority():
    assert set(RULES) == set(PAGE_TYPES)
    assert sorted(PRIORITY) == sorted(PAGE_TYPES)
    assert len(set(PAGE_TYPES)) == 9


def test_readme_page_type_table_matches_the_rules():
    readme = (Path(__file__).parents[2] / "README.md").read_text()
    section = readme.split("## Page classification", 1)[1].split("\n## ", 1)[0]
    documented = re.findall(r"^\| `(\w+)` \|", section, flags=re.MULTILINE)
    assert sorted(documented) == sorted(PAGE_TYPES)


def test_empty_page_is_unknown():
    assert classify([], "https://e.com/") == UNKNOWN


def test_result_carries_scores_signals_and_confidence():
    page = [
        el("input", type="password"),
        el("input", type="text", name="username"),
        el("button", text="Sign in", type="submit"),
    ]
    result = classify_page(page, "https://e.com/x")
    assert result.page_type == "login"
    assert set(result.scores) == set(PAGE_TYPES)
    assert {"password_input", "submit_control", "few_fields"} <= set(result.signals["login"])
    assert result.confidence == result.scores["login"] >= 0.5


def test_classification_is_deterministic():
    page = [el("input", type="password"), el("button", text="Log in", type="submit")]
    assert classify_page(page, "u") == classify_page(list(page), "u")


def test_below_min_score_is_unknown_and_confidence_zero():
    result = classify_page([el("p", text="hello"), el("p", text="world")])
    assert result.page_type == UNKNOWN and result.confidence == 0.0


# --- visibility -------------------------------------------------------------- #


def test_hidden_elements_do_not_vote():
    hidden_login = [
        el("input", type="password", visible=False),
        el("input", type="text", visible=False),
        el("button", text="Sign in", type="submit", visible=False),
    ]
    assert classify(hidden_login) == UNKNOWN


# --- login -------------------------------------------------------------------- #


def test_password_plus_submit_is_login_without_any_url_or_text_cue():
    assert (
        classify([el("input", type="password"), el("button", type="submit", text="Go")]) == "login"
    )


def test_two_password_fields_and_signup_cues_are_not_login():
    signup = [
        el("h1", text="Create account"),
        el("input", name="email"),
        el("input", name="pw", type="password"),
        el("input", name="pw2", type="password"),
        el("button", type="submit", text="Sign up"),
    ]
    assert classify(signup) != "login"


def test_oauth_redirect_url_with_sso_buttons_is_login():
    buttons = [
        el("button", text="Sign in with Google"),
        el("button", text="Continue with Microsoft"),
    ]
    assert classify(buttons, "https://idp.example/oauth2/authorize?client_id=abc") == "login"
    assert classify(buttons, "https://app.example/welcome") != "login"


# --- modal --------------------------------------------------------------------- #

PAGE_BOX = (1280, 800)


def _body():
    return [el("html", sel="html", box=PAGE_BOX), el("body", sel="html > body", box=PAGE_BOX)]


def test_tall_dialog_covering_the_page_is_modal():
    dialog = el("div", sel="html > body > div", role="dialog", box=(800, 600))
    inner = el("button", sel="html > body > div > button", text="OK")
    assert classify([*_body(), dialog, inner]) == "modal"


def test_thin_banner_dialog_is_not_modal():
    banner = el(
        "div",
        sel="html > body > div",
        role="dialog",
        box=(1280, 60),
        position="fixed",
        z_index=2000,
    )
    article = [el("h1", text="Title"), *[el("p", text=f"Paragraph {i} " * 5) for i in range(3)]]
    assert classify([*_body(), banner, *article]) != "modal"


def test_high_z_index_fixed_overlay_is_modal_even_without_dialog_role():
    overlay = el("div", sel="html > body > div", box=(1280, 800), position="fixed", z_index=1000)
    assert classify([*_body(), overlay]) == "modal"


def test_low_z_index_overlay_is_not_modal():
    overlay = el("div", sel="html > body > div", box=(1280, 800), position="fixed", z_index=5)
    assert classify([*_body(), overlay]) != "modal"


def test_dialog_without_a_page_box_cannot_dominate():
    assert classify([el("div", role="dialog", box=(800, 600))]) != "modal"


# --- pagination / list ----------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("next", "next"),
        ("next »", "next"),
        ("next page", "next"),
        ("›", "next"),
        ("»", "next"),
        ("« previous", "prev"),
        ("previous", "prev"),
        ("‹", "prev"),
        ("older", "next"),
        ("newer", "prev"),
        ("nextel", None),
        ("2", None),
        ("", None),
    ],
)
def test_page_direction(text, expected):
    assert _page_direction(text) == expected


def _rows(n, tag="tr", parent="html > body > table > tbody"):
    return [
        el(tag, sel=f"{parent} > {tag}:nth-of-type({i})", classes=["row"]) for i in range(1, n + 1)
    ]


def _row_children(n, parent="html > body > table > tbody"):
    return [
        el("td", sel=f"{parent} > tr:nth-of-type({i}) > td", text=f"cell {i}")
        for i in range(1, n + 1)
    ]


def test_many_table_rows_with_pagination_is_list():
    pager = [
        el("a", text="1", href="?p=1"),
        el("a", text="2", href="?p=2"),
        el("a", text="next", href="?p=2"),
    ]
    assert classify([*_rows(8), *_row_children(8), *pager]) == "list"


def test_rel_next_link_counts_as_pagination():
    result = classify_page([*_rows(3), el("a", text="more", href="/p2", rel="next")])
    assert "pagination" in result.signals["list"]


def test_bare_bullets_are_not_data_rows():
    bullets = [
        el("li", sel=f"html > body > ul > li:nth-of-type({i})", text=f"point {i}")
        for i in range(1, 8)
    ]
    assert "repeating_rows" not in classify_page(bullets).signals.get("list", [])


def test_rows_inside_navigation_are_not_content_rows():
    nav = el("nav", sel="html > body > nav")
    items = [
        el("li", sel=f"html > body > nav > ul > li:nth-of-type({i})", classes=["item"])
        for i in range(1, 9)
    ]
    inner = [
        el("a", sel=f"html > body > nav > ul > li:nth-of-type({i}) > a", href=f"/{i}", text=f"L{i}")
        for i in range(1, 9)
    ]
    inner += [
        el("span", sel=f"html > body > nav > ul > li:nth-of-type({i}) > span", text="x")
        for i in range(1, 9)
    ]
    result = classify_page([nav, *items, *inner])
    assert "repeating_rows" not in result.signals.get("list", [])
    assert result.page_type == "nav_shell"


# --- dashboard / checkout / search -------------------------------------------------- #


def test_kpi_widgets_and_charts_are_a_dashboard_and_nested_matches_count_once():
    cards = []
    for i in range(1, 5):
        cards.append(el("div", sel=f"html > body > div:nth-of-type({i})", classes=["kpi-card"]))
        cards.append(
            el("span", sel=f"html > body > div:nth-of-type({i}) > span", classes=["kpi-value"])
        )
    charts = [
        el("canvas", sel="html > body > canvas:nth-of-type(1)"),
        el("canvas", sel="html > body > canvas:nth-of-type(2)"),
    ]
    result = classify_page([*cards, *charts], "https://e.com/overview")
    assert result.page_type == "dashboard"
    # two kpi-cards' worth of nested kpi-value spans must not inflate the count past 4 cards
    cards_only = classify_page(cards[:2] + cards[3:4])  # 2 cards' worth: below the >=3 threshold
    assert "kpi_widgets" not in cards_only.signals.get("dashboard", [])


def test_payment_fields_plus_step_text_is_checkout():
    page = [
        el("input", name="cardnumber", autocomplete="cc-number"),
        el("input", name="cvc", autocomplete="cc-csc"),
        el("span", text="Step 2 of 3"),
        el("button", type="submit", text="Pay now"),
    ]
    assert classify(page) == "checkout"


def test_single_payment_field_alone_is_not_checkout():
    assert (
        classify([el("input", name="cardnumber"), el("button", type="submit", text="Save")])
        != "checkout"
    )


def test_header_search_box_alone_is_not_a_search_page():
    assert (
        classify([el("input", type="search", name="q"), el("a", href="/", text="Home")]) != "search"
    )


def test_search_input_filters_and_results_area_is_search():
    page = [
        el("input", type="search", name="q"),
        el("select", name="sort"),
        el("input", type="checkbox", name="brand"),
        el("div", id="search-results"),
    ]
    assert classify(page, "https://e.com/find?q=shoes") == "search"


# --- tie-breaking --------------------------------------------------------------------- #


def test_specific_type_wins_a_near_tie_by_priority():
    # A credentials-only form scores as both login and (weakly) form; login must win.
    page = [
        el("form", sel="html > body > form"),
        el("input", name="user"),
        el("input", type="password"),
        el("input", type="checkbox", name="remember"),
        el("button", type="submit", text="Go"),
    ]
    result = classify_page(page)
    assert result.scores["form"] >= result.scores["login"] - 0.15
    assert result.page_type == "login"


# --- helpers ---------------------------------------------------------------------------- #


def test_parent_selectors():
    assert list(_parent_selectors("html > body > ul > li")) == [
        "html > body > ul",
        "html > body",
        "html",
    ]
    assert list(_parent_selectors("#host #inner > input")) == ["#host #inner", "#host"]
    assert list(_parent_selectors("#solo")) == []


def test_a_lone_next_link_is_pagination_but_a_lone_next_button_is_not():
    rows = [*_rows(6), *_row_children(6)]
    link = classify_page([*rows, el("a", text="Next", href="?page=2")])
    button = classify_page([*rows, el("button", text="Next")])
    assert "pagination" in link.signals["list"]
    assert "pagination" not in button.signals.get("list", [])
