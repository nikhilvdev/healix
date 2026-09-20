import dataclasses

import pytest

from healix.driver.base import Element
from healix.healing.fingerprint import Fingerprint
from healix.healing.scorer import (
    DEFAULT_THRESHOLD,
    EVIDENCE_TARGET,
    LOCATOR_PRIORITY,
    MIN_LOCATOR_EVIDENCE,
    WEIGHTS,
    is_trusted_strategy,
    locator_hit_holds,
    rank_candidates,
    score_candidate,
    text_similarity,
)
from tests.elements import el
from tests.healing.pages import URL, fingerprint_of, password, submit, username


def fp_username():
    return fingerprint_of(username(), "textbox:username")


# --- the weighting ------------------------------------------------------------------- #


def test_weights_sum_to_one_and_stable_signals_outweigh_volatile_ones():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
    stable = WEIGHTS["stable_attr"], WEIGHTS["aria_label"], WEIGHTS["name"]
    volatile = WEIGHTS["id"], WEIGHTS["sibling_index"], WEIGHTS["classes"]
    assert min(stable) > max(volatile)
    assert WEIGHTS["stable_attr"] == max(WEIGHTS.values())
    assert len(set(WEIGHTS.values())) > 1  # not a flat, equal-weight comparison


def test_an_unchanged_element_scores_one():
    score = score_candidate(fp_username(), username())
    assert score.confidence == 1.0 and score.raw == 1.0 and score.evidence == 1.0
    assert all(s.similarity == 1.0 for s in score.signals if s.similarity is not None)


def test_stable_signals_beat_volatile_ones():
    fp = fp_username()
    keeps_stable = username(  # every volatile handle changed; stable identity intact
        id="auto-8f3a",
        classes=["css-9x2k"],
        sel="html > body > main > form > input:nth-of-type(7)",
        xpath="/html[1]/body[1]/main[1]/form[1]/input[7]",
        dom={**username().dom_context, "sibling_index": 9, "parent_id": "f-9912"},
    )
    keeps_volatile = username(  # every volatile handle kept; stable identity changed
        data_testid="checkout-email",
        aria_label="Email address",
        name="email",
        dom={**username().dom_context, "nearby_label_text": "Email"},
    )
    assert (
        score_candidate(fp, keeps_stable).confidence
        > score_candidate(fp, keeps_volatile).confidence
    )
    assert score_candidate(fp, keeps_stable).confidence >= DEFAULT_THRESHOLD
    assert score_candidate(fp, keeps_volatile).confidence < DEFAULT_THRESHOLD


def test_changing_only_the_id_barely_moves_the_score():
    other = username(id="user-9032")  # same shape, regenerated number
    assert score_candidate(fp_username(), other).confidence > 0.95


def test_a_different_id_that_is_not_the_same_pattern_costs_only_the_small_id_weight():
    other = username(id="field_email")
    lost = 1.0 - score_candidate(fp_username(), other).confidence
    # the id's weight as a share of what could be compared (text is not comparable here)
    assert lost == pytest.approx(WEIGHTS["id"] / (1.0 - WEIGHTS["text"]), abs=1e-3)
    assert lost < 0.1


def test_a_different_tag_scores_zero():
    assert score_candidate(fp_username(), submit()).confidence == 0.0


def test_the_wrong_field_of_the_same_form_scores_low():
    assert score_candidate(fp_username(), password()).confidence < DEFAULT_THRESHOLD


# --- absence is neutral, a different value is not ---------------------------------------- #


def test_a_stripped_test_id_is_neutral_not_a_mismatch():
    stripped = username(data_testid=None)
    del_attrs = {k: v for k, v in stripped.attributes.items() if v}
    assert "data-testid" not in del_attrs
    score = score_candidate(fp_username(), username(id="user-9032", data_testid=""))
    signal = {s.name: s for s in score.signals}["stable_attr"]
    assert signal.similarity is None  # not comparable: neither a match nor a mismatch


def test_a_changed_test_id_is_a_mismatch():
    score = score_candidate(fp_username(), username(data_testid="something-else"))
    assert {s.name: s for s in score.signals}["stable_attr"].similarity == 0.0


def test_losing_attributes_lowers_confidence_only_by_reducing_evidence():
    fp = fp_username()
    bare = el(
        "input",
        type="text",
        sel="x",
        xpath="/html[1]/body[1]/div[1]/form[1]/input[1]",
        dom={
            "parent_tag": "form",
            "parent_id": "login-form",
            "sibling_index": 1,
            "nearby_label_text": "Username",
        },
    )
    score = score_candidate(fp, bare)
    assert score.raw == 1.0  # everything it can be compared on matches
    assert score.evidence < 1.0 and score.confidence == pytest.approx(
        score.raw * score.evidence, abs=1e-3
    )


def test_sparse_evidence_caps_confidence_however_well_it_matches():
    fp = Fingerprint(page_url=URL, element_role="x", tag="div", id="box", classes=["a"])
    twin = el("div", id="box", classes=["a"], sel="d")
    score = score_candidate(fp, twin)
    assert score.raw == 1.0
    assert score.confidence == pytest.approx(
        min(1.0, (WEIGHTS["id"] + WEIGHTS["classes"]) / EVIDENCE_TARGET), abs=1e-3
    )
    assert score.confidence < DEFAULT_THRESHOLD


# --- individual signals ---------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("Sign in", "sign   IN", 1.0),
        ("username", "username", 1.0),
        ("Save", "Cancel", 0.0),
        ("", "", 1.0),
    ],
)
def test_text_similarity_normalises_and_zeroes_noise(a, b, expected):
    assert text_similarity(a, b) == expected


def test_text_similarity_is_graded_for_near_matches():
    assert 0.5 < text_similarity("Sign in to your account", "Sign in to account") < 1.0
    assert text_similarity("abcdef", "uvwxyz") == 0.0


def test_moving_to_another_frame_or_shadow_root_halves_the_path_signal():
    fp = fp_username()
    same = {s.name: s for s in score_candidate(fp, username()).signals}["dom_path"].similarity
    framed = {s.name: s for s in score_candidate(fp, username(iframe=("main", "panel"))).signals}[
        "dom_path"
    ].similarity
    shadowed = {s.name: s for s in score_candidate(fp, username(shadow=("#host",))).signals}[
        "dom_path"
    ].similarity
    assert framed == pytest.approx(same * 0.5) and shadowed == pytest.approx(same * 0.5)


def test_a_wrapper_div_only_slightly_lowers_the_path_signal():
    wrapped = username(xpath="/html[1]/body[1]/div[1]/div[1]/form[1]/input[1]")
    path = {s.name: s for s in score_candidate(fp_username(), wrapped).signals}[
        "dom_path"
    ].similarity
    assert 0.8 < path < 1.0


def test_volatile_attributes_are_ignored():
    fp = Fingerprint(
        page_url=URL,
        element_role="x",
        tag="input",
        attributes={"style": "a", "tabindex": "0", "data-v-1a2b": ""},
    )
    other = el("input", style="b", tabindex="5", **{"data_v_9z9z": ""})
    assert {s.name: s for s in score_candidate(fp, other).signals}["attributes"].similarity is None


# --- ranking ---------------------------------------------------------------------------- #


def test_rank_candidates_orders_best_first_and_only_scores_the_same_tag():
    fp = fp_username()
    page = [
        submit(),
        password(),
        username(id="user-9032"),
        username(id="x", data_testid="other", name="other", aria_label="Other"),
    ]
    ranked = rank_candidates(fp, page)
    assert [s.element.id for s in ranked][0] == "user-9032"
    assert all(s.element.tag == "input" for s in ranked) and len(ranked) == 3
    assert [s.confidence for s in ranked] == sorted((s.confidence for s in ranked), reverse=True)


def test_ties_keep_document_order():
    fp = fp_username()
    a, b = username(id="first"), username(id="second")
    assert [s.element.id for s in rank_candidates(fp, [a, b])] == ["first", "second"]


def test_scoring_is_deterministic():
    fp, page = fp_username(), [password(), username(id="user-9032")]
    assert [s.confidence for s in rank_candidates(fp, page)] == [
        s.confidence for s in rank_candidates(fp, page)
    ]


def test_a_score_explains_itself():
    explained = score_candidate(
        fp_username(), username(id="user-9032", data_testid="renamed")
    ).explain()
    assert explained["stable_attr"] == 0.0 and explained["id"] == 0.8 and explained["name"] == 1.0
    assert set(explained) == set(WEIGHTS)


# --- the locator priority ----------------------------------------------------------------- #


def test_the_locator_priority_matches_what_a_fingerprint_yields():
    fp = fingerprint_of(username(text="Hello"), "r")
    fp.id_normalized = "user-{n}"  # so the normalized-id strategy applies too
    order = [s.strategy.split(":")[0] for s in fp.locators()]
    assert order == list(LOCATOR_PRIORITY)


def test_only_specific_strategies_are_trusted_on_their_own():
    assert all(
        is_trusted_strategy(s) for s in ("stable_attr:data-testid", "id", "name", "aria_label")
    )
    assert not any(is_trusted_strategy(s) for s in ("css", "xpath", "normalized_id", "text"))


def path_signal(fp, element):
    return {s.name: s for s in score_candidate(fp, element).signals}["dom_path"].similarity


def test_the_recorded_tag_path_is_used_when_the_xpath_is_anchored_at_an_id():
    """An id-anchored xpath carries no path; ``dom_context.tag_path`` does."""
    ctx = {**username().dom_context, "tag_path": ["html", "body", "main", "form", "input"]}
    fp = fingerprint_of(username(xpath='//*[@id="user-4471"]', dom=ctx), "r")
    same = username(xpath='//*[@id="x"]', dom=ctx)
    moved = username(
        xpath='//*[@id="x"]',
        dom={**ctx, "tag_path": ["html", "body", "div", "div", "div", "form", "div", "input"]},
    )
    assert path_signal(fp, same) == 1.0
    assert 0.5 < path_signal(fp, moved) < 1.0


def test_without_a_tag_path_the_signal_falls_back_to_the_xpath():
    assert path_signal(fp_username(), username()) == 1.0
    anchored = fingerprint_of(username(xpath='//*[@id="user-4471"]'), "r")
    assert path_signal(anchored, username(xpath='//*[@id="x"]')) is None  # nothing to compare


# --- a locator's hit on an element that has almost nothing to identify it -------------------- #
# Found on a real site: an image-only link (no text, label, name or test id) can never reach the
# threshold once damping caps it, even when it is exactly the element that was recorded.


def image_link(href="catalogue/a-light-in-the-attic_1000/index.html"):
    return Element.from_dict(
        {
            "tag": "a",
            "attributes": {"href": href},
            "computed": {"visible": True, "href": "https://books.example/" + href},
            "css_selector": "#default > ol > li:nth-of-type(1) > article > div > a",
            "xpath": "//ol[1]/li[1]/article[1]/div[1]/a[1]",
            "dom_context": {
                "parent_tag": "div",
                "sibling_index": 0,
                "tag_path": ["html", "body", "ol", "li", "a"],
            },
        },
        iframe_path=["main"],
    )


def test_an_unchanged_image_link_is_identical_but_damped_below_the_threshold():
    fp = fingerprint_of(image_link(), "link:index-html")
    score = score_candidate(fp, image_link())
    assert score.raw == 1.0
    assert score.confidence < DEFAULT_THRESHOLD  # only its href and its position can be compared


def test_a_locator_hit_that_agrees_on_everything_comparable_holds_even_when_damped():
    fp = fingerprint_of(image_link(), "link:index-html")
    assert locator_hit_holds(fp, score_candidate(fp, image_link()), DEFAULT_THRESHOLD)


def test_a_locator_hit_whose_href_differs_does_not_hold():
    fp = fingerprint_of(image_link(), "link:index-html")
    other = image_link(href="catalogue/tipping-the-velvet_999/index.html")
    assert not locator_hit_holds(fp, score_candidate(fp, other), DEFAULT_THRESHOLD)


def test_an_element_with_nothing_but_its_position_is_trusted_at_that_position():
    """A bare disabled text box: no name, label, text or id. Found on a real page; it could not be
    found again even though nothing had changed."""

    def bare():
        return el(
            "input",
            type="text",
            disabled="",
            sel="#input-example > input",
            dom={
                "parent_tag": "form",
                "sibling_index": 0,
                "tag_path": ["html", "body", "form", "input"],
            },
        )

    fp = fingerprint_of(bare(), "textbox")
    score = score_candidate(fp, bare())
    assert score.confidence < DEFAULT_THRESHOLD and score.raw == 1.0
    assert locator_hit_holds(fp, score, DEFAULT_THRESHOLD)


def test_a_hit_that_differs_in_what_the_element_says_never_holds():
    def button(text):
        return el(
            "button",
            text=text,
            sel="#b",
            dom={
                "parent_tag": "li",
                "sibling_index": 0,
                "tag_path": ["html", "body", "ul", "li", "button"],
            },
        )

    fp = fingerprint_of(button("Add to basket"), "button")
    assert not locator_hit_holds(fp, score_candidate(fp, button("Remove")), DEFAULT_THRESHOLD)


def test_an_element_that_lost_the_identity_it_was_recorded_with_is_not_accepted_this_way():
    """It agrees on every signal it still has, but the test id and name are gone: a weak match."""
    fp = fingerprint_of(username(), "textbox:username")
    stripped = username(data_testid=None, aria_label=None, name=None, id=None)
    stripped.attributes.pop("data-testid", None)
    stripped.attributes.pop("aria-label", None)
    score = score_candidate(fp, stripped)
    assert score.raw >= 0.9 and score.evidence < 1.0
    assert not locator_hit_holds(fp, score, DEFAULT_THRESHOLD)


def test_a_fingerprint_with_almost_nothing_to_compare_never_holds():
    lonely = el("div", sel="#x")  # no attributes, no context: nothing beyond the tag
    fp = fingerprint_of(lonely, "div")
    score = score_candidate(fp, el("div", sel="#x"))
    assert score.evidence < MIN_LOCATOR_EVIDENCE
    assert not locator_hit_holds(fp, score, DEFAULT_THRESHOLD)


def test_a_caller_who_asks_for_a_stricter_match_still_gets_one():
    fp = fingerprint_of(image_link(), "link:index-html")
    identical = score_candidate(fp, image_link())
    assert locator_hit_holds(fp, identical, 1.0)  # raw is 1.0, so even a perfect bar is met
    assert not locator_hit_holds(fp, dataclasses.replace(identical, raw=0.92), 0.95)
    assert locator_hit_holds(fp, dataclasses.replace(identical, raw=0.92), 0.5)
