import pytest

from healix.driver.base import ElementNotFoundError
from healix.healing import (
    EXACT,
    HEALED,
    ElementNotHealedError,
    FingerprintNotFoundError,
    HealingError,
    Resolver,
    SQLiteFingerprintStore,
    UnsupportedRenderingError,
    looks_canvas_rendered,
)
from tests.elements import el
from tests.healing.pages import URL, PageDriver, fingerprint_of, password, submit, username, v1

ROLE = "textbox:login-username"


@pytest.fixture
def store():
    with SQLiteFingerprintStore(":memory:") as s:
        s.put(fingerprint_of(username(), ROLE))
        s.put(fingerprint_of(password(), "textbox:login-password"))
        s.put(fingerprint_of(submit(), "button:sign-in"))
        yield s


def resolver(store, **kwargs):
    healed = []
    r = Resolver(store, on_heal=healed.append, run_id="run-1", **kwargs)
    return r, healed


# --- exact -------------------------------------------------------------------------------- #


def test_an_unchanged_page_resolves_exactly_by_the_highest_priority_locator(store):
    r, healed = resolver(store)
    driver = PageDriver(v1())
    result = r.resolve(driver, URL, ROLE)
    assert (result.outcome, result.strategy) == (EXACT, "stable_attr:data-testid")
    assert not result.healed and result.confidence is None and result.record is None
    assert driver.locates == ["stable_attr:data-testid"]  # stopped at the first hit
    assert driver.reads == 0 and healed == [] and store.history() == []


def test_a_changed_id_alone_still_resolves_exactly_through_the_test_id(store):
    page = [username(id="user-9032"), password(), submit()]
    result = resolver(store)[0].resolve(PageDriver(page), URL, ROLE)
    assert result.outcome == EXACT and result.element.id == "user-9032"
    assert store.get(URL, ROLE).id == "user-4471"  # nothing to repair, so nothing was rewritten


# --- healed through a lower-priority locator ------------------------------------------------- #


def test_a_stale_test_id_and_id_fall_back_to_the_name_and_are_repaired(store):
    page = [username(id="user-9032", data_testid=None), password(), submit()]
    r, healed = resolver(store)
    result = r.resolve(PageDriver(page), URL, ROLE)

    assert result.healed and result.outcome == HEALED
    assert result.strategy == "name"  # the highest-priority locator that still matches
    assert result.record.kind == "fallback" and result.confidence >= 0.5
    assert store.get(URL, ROLE).id == "user-9032"  # persisted
    assert len(healed) == 1 and healed[0].id == 1


def test_the_locator_priority_decides_which_fallback_is_used(store):
    # test id and id are gone; name and aria-label both still match; name ranks higher
    page = [username(id="new", data_testid=None), password(), submit()]
    result = resolver(store)[0].resolve(PageDriver(page), URL, ROLE)
    assert result.strategy == "name"


def test_a_fallback_match_is_recorded_with_old_and_new_locators(store):
    page = [username(id="new-id", data_testid=None), password(), submit()]
    record = resolver(store)[0].resolve(PageDriver(page), URL, ROLE).record
    assert record.old_locator == 'stable_attr:data-testid=[data-testid="login-username"]'
    assert record.new_locator == 'id=[id="new-id"]'
    assert record.change_kind == "churn" and "id" in record.changed_fields
    assert record.run_id == "run-1" and record.page_url == URL and record.element_role == ROLE


def test_a_healed_fingerprint_resolves_exactly_next_time(store):
    page = [username(id="new-id", data_testid=None), password(), submit()]
    r = resolver(store)[0]
    assert r.resolve(PageDriver(page), URL, ROLE).healed
    again = r.resolve(PageDriver(page), URL, ROLE)
    assert again.outcome == EXACT and again.strategy == "id"
    assert len(store.history()) == 1  # no second heal


# --- healed by scoring -------------------------------------------------------------------- #


def scored_page():
    """Every handle changed and the structure moved, but it is recognisably the same field."""
    moved = username(
        id="field_7",
        name=None,
        data_testid=None,
        aria_label=None,
        classes=["css-1x"],
        sel="html > body > main > section > form > input",
        xpath="/html[1]/body[1]/main[1]/section[1]/form[1]/input[1]",
        dom={
            "parent_tag": "form",
            "parent_id": "login-form",
            "sibling_index": 1,
            "nearby_label_text": "Username",
        },
    )
    return [moved, password(), submit()]


def test_when_no_locator_matches_the_best_scoring_candidate_is_healed(store):
    r, healed = resolver(store)
    driver = PageDriver(scored_page())
    result = r.resolve(driver, URL, ROLE)

    assert result.healed and result.strategy == "weighted_score"
    assert result.record.kind == "scored" and 0.5 <= result.confidence <= 1.0
    assert result.element.id == "field_7"
    assert driver.reads == 1
    assert store.get(URL, ROLE).id == "field_7"
    assert healed[0].change_kind == "churn"


def test_the_scored_heal_is_audited_and_survives_a_new_resolver(store):
    resolver(store)[0].resolve(PageDriver(scored_page()), URL, ROLE)
    [entry] = store.history(URL, ROLE)
    assert entry.strategy == "weighted_score" and entry.old_fingerprint["id"] == "user-4471"
    assert entry.new_fingerprint["id"] == "field_7"

    again = resolver(store)[0].resolve(PageDriver(scored_page()), URL, ROLE)  # "a later run"
    assert again.outcome == EXACT and again.strategy == "id"


def test_a_heal_that_changes_the_meaning_is_flagged_as_a_regression(store):
    changed = username(
        id="x",
        name=None,
        data_testid=None,
        aria_label=None,
        sel="html > body > main > form > input",
        xpath="/html[1]/body[1]/main[1]/form[1]/input[1]",
        dom={
            "parent_tag": "form",
            "parent_id": "login-form",
            "sibling_index": 1,
            "nearby_label_text": "Email address or phone",
        },
    )
    result = resolver(store, threshold=0.4)[0].resolve(
        PageDriver([changed, password(), submit()]), URL, ROLE
    )
    assert result.healed and result.record.change_kind == "regression"
    assert "dom_context.nearby_label_text" in result.record.changed_fields


# --- the confidence gate ------------------------------------------------------------------ #


def test_a_best_but_weak_candidate_is_rejected_not_used(store):
    page = [
        el(
            "input",
            type="search",
            name="q",
            classes=["search"],
            sel="s",
            xpath="/html[1]/body[1]/header[1]/input[1]",
        ),
        submit(),
    ]
    r, healed = resolver(store)
    with pytest.raises(ElementNotHealedError, match="below the 0.50 threshold"):
        r.resolve(PageDriver(page), URL, ROLE)
    assert healed == [] and store.history() == []
    assert store.get(URL, ROLE).id == "user-4471"  # nothing was changed


def test_the_threshold_is_configurable(store):
    page = [
        el(
            "input",
            type="text",
            classes=["form-control", "input-lg"],
            sel="x",
            xpath="/html[1]/body[1]/div[1]/form[1]/input[1]",
        )
    ]
    with pytest.raises(ElementNotHealedError):
        resolver(store, threshold=0.9)[0].resolve(PageDriver(page), URL, ROLE)
    assert resolver(store, threshold=0.0)[0].resolve(PageDriver(page), URL, ROLE).healed


def test_the_error_names_what_was_tried_and_how_close_it_got(store):
    page = [el("input", type="search", name="q", sel="s")]
    with pytest.raises(ElementNotHealedError) as info:
        resolver(store)[0].resolve(PageDriver(page), URL, ROLE)
    message = str(info.value)
    assert ROLE in message and URL in message and "scored" in message
    assert "stable_attr:data-testid: no unique match" in message


def test_two_look_alikes_are_ambiguous_and_rejected(store):
    twin_a = username(
        id="a",
        name=None,
        data_testid=None,
        aria_label=None,
        sel="x1",
    )
    twin_b = username(
        id="b",
        name=None,
        data_testid=None,
        aria_label=None,
        sel="x2",
    )
    r, healed = resolver(store)
    with pytest.raises(ElementNotHealedError, match="closer than the 0.05 margin"):
        r.resolve(PageDriver([twin_a, twin_b]), URL, ROLE)
    assert healed == [] and store.history() == []


def test_a_clear_winner_over_a_weaker_rival_is_not_ambiguous(store):
    good = scored_page()[0]
    weaker = username(
        id="other",
        name=None,
        data_testid=None,
        aria_label=None,
        classes=[],
        type="text",
        dom={"nearby_label_text": "Something else", "parent_tag": "div", "sibling_index": 9},
        sel="z",
        xpath="/html[1]/body[1]/footer[1]/input[1]",
    )
    assert (
        resolver(store)[0]
        .resolve(PageDriver([weaker, good, password(), submit()]), URL, ROLE)
        .element.id
        == "field_7"
    )


def test_the_ambiguity_margin_can_be_disabled(store):
    a = username(id="a", name=None, data_testid=None, aria_label=None, sel="x1")
    b = username(id="b", name=None, data_testid=None, aria_label=None, sel="x2")
    assert resolver(store, ambiguity_margin=0.0)[0].resolve(PageDriver([a, b]), URL, ROLE).healed


# --- a locator that matches the wrong thing ----------------------------------------------- #


def test_a_positional_locator_that_now_points_at_a_different_element_is_rejected(store):
    """Only a css selector is stored; after a redesign it lands on the *password* field."""
    fp = fingerprint_of(username(), "textbox:bare")
    fp.id = fp.name = fp.id_normalized = None
    fp.attributes = {"type": "text"}
    store.put(fp)
    page = [
        password(
            sel=fp.css_selector, xpath=None, id=None, name=None, data_testid=None, aria_label=None
        ),
        username(
            id="real",
            name=None,
            data_testid=None,
            aria_label=None,
            sel="html > body > main > input",
            xpath=None,
        ),
    ]
    result = resolver(store)[0].resolve(PageDriver(page), URL, "textbox:bare")
    assert result.element.id == "real"  # not the element the css selector happened to hit
    assert result.strategy == "weighted_score"


def test_a_positional_first_match_that_resembles_the_element_is_accepted_without_a_heal(store):
    fp = fingerprint_of(username(), "textbox:bare")
    fp.id = fp.name = fp.id_normalized = None
    fp.attributes = {"type": "text"}
    store.put(fp)
    result = resolver(store)[0].resolve(PageDriver(v1()), URL, "textbox:bare")
    assert result.outcome == EXACT and result.strategy == "css" and result.confidence >= 0.5
    assert store.history() == []


def test_a_text_match_on_a_different_element_is_verified_and_rejected(store):
    fp = fingerprint_of(submit(), "button:sign-in")
    store.put(fp)
    # id gone; the text "Sign in" now belongs to a differently-shaped link-styled button
    impostor = el(
        "button",
        text="Sign in",
        type="button",
        classes=["nav-link"],
        sel="n",
        xpath="/html[1]/body[1]/nav[1]/button[1]",
        dom={"parent_tag": "nav"},
    )
    with pytest.raises(ElementNotHealedError):
        resolver(store)[0].resolve(PageDriver([impostor]), URL, "button:sign-in")


# --- failing cleanly ---------------------------------------------------------------------- #


def test_an_unknown_role_says_how_to_record_one(store):
    with pytest.raises(FingerprintNotFoundError, match="record one first"):
        resolver(store)[0].resolve(PageDriver(v1()), URL, "button:missing")


def test_a_page_with_no_elements_of_that_tag_fails_with_a_clear_message(store):
    with pytest.raises(ElementNotHealedError, match="no <input> elements"):
        resolver(store)[0].resolve(PageDriver([el("div", text="empty")]), URL, ROLE)


def test_the_errors_are_a_healing_error_and_still_an_element_not_found_error(store):
    with pytest.raises(HealingError):
        resolver(store)[0].resolve(PageDriver([]), URL, ROLE)
    with pytest.raises(ElementNotFoundError):
        resolver(store)[0].resolve(PageDriver([]), URL, ROLE)


def test_a_failing_on_heal_callback_does_not_undo_the_heal(store):
    r = Resolver(store, on_heal=lambda rec: 1 / 0)
    assert r.resolve(PageDriver(scored_page()), URL, ROLE).healed
    assert store.get(URL, ROLE).id == "field_7"


def test_a_driver_that_cannot_locate_by_strategy_says_so(store):
    class NoLocate(PageDriver):
        def locate(self, spec, fingerprint):
            raise NotImplementedError("cannot locate")

    with pytest.raises(NotImplementedError):
        resolver(store)[0].resolve(NoLocate(v1()), URL, ROLE)


@pytest.mark.parametrize(
    "kwargs", [{"threshold": -0.1}, {"threshold": 1.5}, {"ambiguity_margin": -1}]
)
def test_invalid_settings_are_rejected(store, kwargs):
    with pytest.raises(ValueError):
        Resolver(store, **kwargs)


# --- the canvas boundary ------------------------------------------------------------------ #

PAGE = (1280, 800)


def canvas_page():
    return [
        el("html", sel="html", box=PAGE),
        el("body", sel="html > body", box=PAGE),
        el("canvas", sel="html > body > canvas", box=(1280, 780), id="app"),
    ]


def test_a_canvas_only_page_is_detected():
    assert looks_canvas_rendered(canvas_page())
    assert looks_canvas_rendered(
        [*canvas_page(), el("button", text="x", sel="b1")]
    )  # a stray control or two


def test_an_ordinary_page_with_a_small_canvas_is_not_canvas_rendered():
    small = el("canvas", sel="c", box=(200, 100))
    assert not looks_canvas_rendered([el("body", sel="b", box=PAGE), small, *v1()])


def test_a_big_canvas_alongside_a_real_form_is_not_canvas_rendered():
    controls = [el("input", type="text", sel=f"i{n}") for n in range(6)]
    assert not looks_canvas_rendered([*canvas_page(), *controls])


def test_controls_inside_the_canvas_fallback_content_do_not_count_as_dom_ui():
    fallback = [el("button", sel=f"html > body > canvas b{n}") for n in range(6)]
    assert looks_canvas_rendered([*canvas_page(), *fallback])


def test_healing_on_a_canvas_rendered_page_refuses_with_a_specific_error_and_changes_nothing(store):
    r, healed = resolver(store)
    with pytest.raises(UnsupportedRenderingError, match="canvas"):
        r.resolve(PageDriver(canvas_page()), URL, ROLE)
    assert healed == [] and store.history() == []
    assert store.get(URL, ROLE).id == "user-4471"


def test_the_canvas_error_is_a_healing_error_but_not_a_plain_not_found(store):
    with pytest.raises(HealingError) as info:
        resolver(store)[0].resolve(PageDriver(canvas_page()), URL, ROLE)
    assert not isinstance(info.value, ElementNotFoundError)
