"""Self-healing against a real page in real Chromium.

One URL serves different versions of a login page (like successive deploys). A baseline is recorded
against ``v1``, the page changes, and the resolver must find each element again — or refuse.
"""

import pytest

pytest.importorskip("playwright")

from healix import (  # noqa: E402
    ElementNotHealedError,
    Healer,
    SQLiteFingerprintStore,
    UnsupportedRenderingError,
)
from tests.healing.site import SwitchSite  # noqa: E402

USER, PASSWORD, SUBMIT = "textbox:login-username", "textbox:login-password", "button:login-submit"


@pytest.fixture(scope="module", autouse=True)
def _browser_available():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:
        pytest.skip(f"cannot launch Chromium: {exc}")


@pytest.fixture
def site():
    s = SwitchSite()
    yield s
    s.close()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "fingerprints.db"


def page(healer):
    return healer.driver.page  # the Playwright page, for checking what really happened


def learn_baseline(site, db):
    with Healer(db) as healer:
        summary = healer.learn(site.url)
    assert summary.added == 3
    return summary


# --- the baseline ------------------------------------------------------------------------- #


def test_the_baseline_is_recorded_under_stable_roles(site, db):
    learn_baseline(site, db)
    with SQLiteFingerprintStore(db) as store:
        roles = sorted(f.element_role for f in store.fingerprints())
    assert roles == [SUBMIT, PASSWORD, USER] or roles == sorted([SUBMIT, PASSWORD, USER])


def test_an_unchanged_page_resolves_exactly(site, db):
    learn_baseline(site, db)
    with Healer(db) as healer:
        result = healer.resolve(site.url, USER)
        assert result.outcome == "exact" and result.strategy == "stable_attr:data-testid"
        assert healer.history() == []


def test_regenerated_ids_alone_need_no_healing_because_the_test_id_still_matches(site, db):
    learn_baseline(site, db)
    site.set("ids-only")
    with Healer(db) as healer:
        result = healer.resolve(site.url, USER)
        assert result.outcome == "exact" and result.element.id == "user-9032"
        assert healer.history() == []


# --- healed through a lower-priority locator ----------------------------------------------- #


def test_routine_churn_is_healed_through_the_next_locator_that_still_matches(site, db):
    learn_baseline(site, db)
    site.set("churn")
    events = []
    with Healer(db, on_event=events.append) as healer:
        user = healer.resolve(site.url, USER)
        password = healer.resolve(site.url, PASSWORD)
        submit = healer.resolve(site.url, SUBMIT)

        assert (user.strategy, password.strategy) == ("name", "name")
        assert submit.strategy == "text"  # no id, name or aria-label left: the button text still is
        assert all(r.healed and r.confidence >= 0.5 for r in (user, password, submit))
        assert {r.record.change_kind for r in (user, password, submit)} == {"churn"}
        assert {r.record.kind for r in (user, password, submit)} == {"fallback"}
    assert [e["event"] for e in events] == ["element_healed"] * 3


# --- the deliverable: break a stored locator, heal through the gate, persist ------------------ #


def break_stored_locators(db, url, role, *, keep_stable: bool):
    """Break the id (and the id-based css/xpath) in the stored fingerprint, deliberately.

    With ``keep_stable=False`` the fingerprint also loses its test id, name and aria-label — as
    if it had been recorded from a page that only ever had an id.
    """
    with SQLiteFingerprintStore(db) as store:
        fp = store.get(url, role)
        fp.id = fp.id_normalized = "gone-id"
        fp.css_selector, fp.xpath = "#gone-id", '//*[@id="gone-id"]'
        if not keep_stable:
            fp.name = None
            fp.attributes = {k: v for k, v in fp.attributes.items() if k == "type"}
        else:
            fp.attributes = {k: v for k, v in fp.attributes.items() if k != "data-testid"}
        store.put(fp)


def test_a_changed_id_is_healed_through_the_next_locator_and_the_fix_persists(site, db):
    learn_baseline(site, db)
    break_stored_locators(db, site.url, USER, keep_stable=True)  # id gone; name and aria remain

    events = []
    with Healer(db, on_event=events.append) as healer:
        result = healer.resolve(site.url, USER)
        assert result.healed and result.strategy == "name"
        assert result.record.kind == "fallback" and 0.5 <= result.confidence <= 1.0
        assert result.element.id == "user-4471"
        assert result.record.old_locator == 'id=[id="gone-id"]'
        assert [e["data"]["strategy_used"] for e in events] == ["name"]

    with SQLiteFingerprintStore(db) as store:  # persisted
        assert store.get(site.url, USER).id == "user-4471"
        assert len(store.history(site.url, USER)) == 1

    with Healer(db) as later:  # a later run, a fresh browser: exact, and no second heal
        assert later.resolve(site.url, USER).outcome == "exact"
        assert len(later.history()) == 1


def test_with_no_locator_left_the_best_scoring_element_is_healed_and_persisted(site, db):
    learn_baseline(site, db)
    break_stored_locators(db, site.url, USER, keep_stable=False)  # only the id (now wrong) and type

    events = []
    with Healer(db, on_event=events.append) as healer:
        result = healer.resolve(site.url, USER)

        assert result.healed and result.strategy == "weighted_score"
        assert result.record.kind == "scored" and 0.5 <= result.confidence <= 1.0
        assert result.element.id == "user-4471"  # the right field, not the password box
        assert [e["data"]["strategy_used"] for e in events] == ["weighted_score"]
        healer.write("alice", site.url, USER, navigate=False)
        assert page(healer).evaluate("window.__typed.text") == "alice"  # and it is usable

    with SQLiteFingerprintStore(db) as store:
        [record] = store.history(site.url, USER)
        assert record.old_fingerprint["id"] == "gone-id"
        assert record.new_fingerprint["id"] == "user-4471"
    with Healer(db) as later:
        assert later.resolve(site.url, USER).outcome == "exact"


def test_renaming_every_identifier_at_once_is_refused_by_default_but_can_be_allowed(site, db):
    """Test id, aria-label, name and id all changed to different values: only the label, input type
    and position still agree. That is not enough at the default threshold, so it is refused — and a
    team that accepts the risk can lower the threshold."""
    learn_baseline(site, db)
    with SQLiteFingerprintStore(db) as store:
        fp = store.get(site.url, USER)
        fp.id = fp.id_normalized = "gone-id"
        fp.name = "gone-name"
        fp.attributes = {**fp.attributes, "data-testid": "gone-testid", "aria-label": "Gone"}
        fp.css_selector, fp.xpath = "#gone-id", '//*[@id="gone-id"]'
        store.put(fp)

    with Healer(db) as healer, pytest.raises(ElementNotHealedError, match="below the 0.50"):
        healer.resolve(site.url, USER)
    with Healer(db, threshold=0.4) as healer:
        assert healer.resolve(site.url, USER).element.id == "user-4471"


def test_a_heavy_refactor_is_healed_by_scoring_and_the_healed_fields_work(site, db):
    learn_baseline(site, db)
    site.set("refactor")
    with Healer(db) as healer:
        user = healer.write("alice@example.com", site.url, USER)
        pw = healer.write("hunter2", site.url, PASSWORD, navigate=False)
        healer.click(site.url, SUBMIT, navigate=False)

        assert user.strategy == "weighted_score" and pw.strategy == "weighted_score"
        assert user.element.id == "x1" and pw.element.id == "x2"  # each to the right field
        assert 0.5 <= user.confidence < 0.9  # heavy change: healed, but not with false certainty
        assert page(healer).evaluate("window.__typed") == {
            "text": "alice@example.com",
            "password": "hunter2",
        }
        assert page(healer).evaluate("window.__submitted") == 1
        assert {r.element_role for r in healer.history()} == {USER, PASSWORD, SUBMIT}

    with Healer(db) as later:  # every heal persisted
        assert [later.resolve(site.url, r).outcome for r in (USER, PASSWORD, SUBMIT)] == [
            "exact"
        ] * 3


def test_a_healed_element_whose_meaning_changed_is_flagged_as_a_regression(site, db):
    learn_baseline(site, db)
    site.set("meaning-changed")
    with Healer(db) as healer:
        result = healer.resolve(site.url, USER)
        assert result.healed and result.element.id == "a1"
        assert result.record.change_kind == "regression"
        assert "dom_context.nearby_label_text" in result.record.changed_fields
        [row] = [r for r in healer.report() if r["element_key"].endswith(USER)]
        assert (row["heals"], row["regressions"]) == (1, 1)
        assert healer.history(change_kind="regression")[0].element_role == USER


# --- refusing ----------------------------------------------------------------------------- #


def test_a_weak_match_is_rejected_and_nothing_is_stored(site, db):
    learn_baseline(site, db)
    with SQLiteFingerprintStore(db) as store:
        before = [f.to_dict() for f in store.fingerprints()]
    site.set("form-removed")
    events = []
    with Healer(db, on_event=events.append) as healer:
        with pytest.raises(ElementNotHealedError, match="below the 0.50 threshold"):
            healer.resolve(site.url, USER)
        assert healer.history() == []
    assert events == []
    with SQLiteFingerprintStore(db) as store:
        assert [f.to_dict() for f in store.fingerprints()] == before


def test_indistinguishable_candidates_are_rejected_as_ambiguous(site, db):
    learn_baseline(site, db)
    site.set("ambiguous")
    with Healer(db, threshold=0.2) as healer, pytest.raises(ElementNotHealedError):
        healer.resolve(site.url, USER)
    with Healer(db) as healer:
        assert healer.history() == []


def test_the_threshold_is_configurable_against_the_real_page(site, db):
    learn_baseline(site, db)
    site.set("refactor")
    with Healer(db, threshold=0.95) as healer, pytest.raises(ElementNotHealedError):
        healer.resolve(site.url, USER)
    with Healer(db, threshold=0.4) as healer:
        assert healer.resolve(site.url, USER).healed


def test_a_canvas_rendered_page_fails_cleanly_and_says_why(site, db):
    learn_baseline(site, db)
    site.set("canvas")
    with Healer(db) as healer:
        with pytest.raises(UnsupportedRenderingError, match="canvas"):
            healer.resolve(site.url, USER)
        assert healer.history() == []


# --- the same, with the fingerprints in PostgreSQL ---------------------------------------- #


def test_healing_persists_to_postgres_and_a_new_healer_sees_the_fix(site, postgres_url):
    from tests.conftest import drop_healix_tables

    drop_healix_tables(postgres_url)
    with Healer(postgres_url) as healer:
        assert healer.learn(site.url).added == 3

    site.set("churn")
    events = []
    with Healer(postgres_url, on_event=events.append) as healer:
        result = healer.resolve(site.url, USER)
        assert result.healed and result.strategy == "name"
        [record] = healer.history(site.url, USER)
        assert record.change_kind == "churn"
    assert [e["event"] for e in events] == ["element_healed"]

    with Healer(postgres_url) as later:  # a different process, in effect: a fresh connection
        again = later.resolve(site.url, USER)
        assert again.outcome == "exact" and again.strategy == "id"
        assert len(later.history()) == 1  # no second heal
        assert [r["heals"] for r in later.report()] == [1]
