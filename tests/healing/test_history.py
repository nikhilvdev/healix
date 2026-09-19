import pytest

from healix.healing.history import (
    CHURN,
    REGRESSION,
    HealRecord,
    churn_report,
    diff_fingerprints,
)
from tests.healing.pages import fingerprint_of, submit, username


def fp(element, role="r"):
    return fingerprint_of(element, role)


def record(role="textbox:username", change_kind=CHURN, at="2026-01-01T00:00:00.000Z"):
    return HealRecord(
        page_url="https://e.com/",
        element_role=role,
        at=at,
        kind="scored",
        strategy="weighted_score",
        confidence=0.8,
        old_locator="a",
        new_locator="b",
        change_kind=change_kind,
        changed_fields=("id",),
    )


def test_regenerated_ids_classes_and_position_are_churn():
    old = fp(username())
    new = fp(
        username(
            id="user-9032",
            classes=["css-1x2y"],
            data_testid="renamed-testid",
            sel="html > body > main > form > input",
            xpath="/html[1]/body[1]/main[1]/form[1]/input[1]",
            dom={**username().dom_context, "sibling_index": 4, "parent_id": "f-77"},
        )
    )
    changed, kind = diff_fingerprints(old, new)
    assert kind == CHURN
    assert {"id", "classes", "css_selector", "xpath", "attributes.data-testid"} <= set(changed)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Save changes"},  # the visible text changed meaning
        {"aria_label": "Email address"},
        {"placeholder": "Something else entirely"},
        {"type": "password"},  # the input type changed
        {"role": "combobox"},
        {"dom": {"nearby_label_text": "Completely different label"}},
    ],
)
def test_a_change_to_what_the_user_sees_or_what_the_element_is_is_a_regression(change):
    old_el = submit(text="Sign in", aria_label="Sign in", placeholder="Sign in here", role="button")
    new_el = submit(
        **{
            "text": "Sign in",
            "aria_label": "Sign in",
            "placeholder": "Sign in here",
            "role": "button",
            **change,
        }
    )
    if "dom" in change:  # label lives in dom_context
        old_el = submit(dom={"nearby_label_text": "Sign in"})
        new_el = submit(dom=change["dom"])
    _, kind = diff_fingerprints(fp(old_el), fp(new_el))
    assert kind == REGRESSION


def test_a_tag_change_is_a_regression():
    assert diff_fingerprints(fp(username()), fp(submit()))[1] == REGRESSION


def test_a_small_wording_tweak_is_not_a_regression():
    _, kind = diff_fingerprints(
        fp(submit(text="Sign in to your account")), fp(submit(text="Sign in to account"))
    )
    assert kind == CHURN


def test_identical_fingerprints_have_no_changes_and_count_as_churn():
    assert diff_fingerprints(fp(username()), fp(username())) == ([], CHURN)


def test_changed_fields_are_named_precisely():
    changed, _ = diff_fingerprints(
        fp(username()),
        fp(username(name="login_user", dom={**username().dom_context, "sibling_index": 2})),
    )
    assert "name" in changed and "dom_context.sibling_index" in changed and "id" not in changed


def test_a_record_round_trips_through_a_dict():
    r = record()
    assert HealRecord.from_dict(r.to_dict()) == r
    assert HealRecord.from_dict({**r.to_dict(), "from_the_future": 1}) == r
    assert r.element_key == "https://e.com/#textbox:username"


def test_the_churn_report_ranks_the_most_healed_elements_and_counts_regressions():
    records = [
        record("a", CHURN, "2026-01-01T00:00:00.000Z"),
        record("a", CHURN, "2026-01-03T00:00:00.000Z"),
        record("a", REGRESSION, "2026-01-02T00:00:00.000Z"),
        record("b", CHURN, "2026-01-01T00:00:00.000Z"),
    ]
    report = churn_report(records)
    assert [r["element_key"] for r in report] == ["https://e.com/#a", "https://e.com/#b"]
    assert report[0] == {
        "element_key": "https://e.com/#a", "heals": 3, "regressions": 1, "churn": 2,
        "last_healed_at": "2026-01-03T00:00:00.000Z",
    }  # fmt: skip
    assert churn_report([]) == []
