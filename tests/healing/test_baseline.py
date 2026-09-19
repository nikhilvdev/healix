import pytest

from healix.healing import KEEP, REFRESH, SQLiteFingerprintStore, record_page
from tests.elements import el
from tests.healing.pages import URL, v1


@pytest.fixture
def store():
    with SQLiteFingerprintStore(":memory:") as s:
        yield s


def test_a_page_is_recorded_under_stable_roles(store):
    summary = record_page(store, URL, v1())
    assert (summary.added, summary.kept, summary.refreshed, summary.total) == (3, 0, 0, 3)
    roles = sorted(f.element_role for f in store.fingerprints(URL))
    assert roles == ["button:sign-in", "textbox:login-password", "textbox:login-username"]


def test_only_healable_elements_are_recorded(store):
    page = [
        *v1(),
        el("h1", text="Title"),
        el("p", text="hi"),
        el("input", type="hidden", name="csrf"),
    ]
    assert record_page(store, URL, page).total == 3


def test_keep_mode_never_overwrites_the_baseline(store):
    record_page(store, URL, v1())
    changed = list(v1())
    changed[0] = el(
        "input", type="text", data_testid="login-username", id="a-brand-new-id", sel="x"
    )
    summary = record_page(store, URL, changed)
    assert (summary.added, summary.kept) == (0, 3)
    assert (
        store.get(URL, "textbox:login-username").id == "user-4471"
    )  # the known-good baseline stays


def test_refresh_mode_overwrites_and_reports_it(store):
    record_page(store, URL, v1())
    changed = [
        el("input", type="text", data_testid="login-username", id="a-brand-new-id", sel="x"),
        *v1()[1:],
    ]
    summary = record_page(store, URL, changed, mode=REFRESH)
    assert (summary.added, summary.refreshed) == (0, 3)
    assert store.get(URL, "textbox:login-username").id == "a-brand-new-id"


def test_new_roles_are_added_even_in_keep_mode(store):
    record_page(store, URL, v1())
    summary = record_page(store, URL, [*v1(), el("a", text="Forgot password?", href="/forgot")])
    assert (summary.added, summary.kept) == (1, 3)


def test_the_mode_is_validated(store):
    with pytest.raises(ValueError, match="mode"):
        record_page(store, URL, v1(), mode="overwrite")
    assert KEEP == "keep" and REFRESH == "refresh"


def test_recording_is_idempotent(store):
    record_page(store, URL, v1())
    before = [f.to_dict() for f in store.fingerprints()]
    record_page(store, URL, v1())
    assert [f.to_dict() for f in store.fingerprints()] == before
