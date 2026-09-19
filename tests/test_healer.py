import pytest

from healix import (
    ElementNotHealedError,
    Healer,
    SQLiteFingerprintStore,
    UnsupportedRenderingError,
)
from healix.events import EVENT_DATA_FIELDS
from healix.healing import FingerprintNotFoundError
from tests.elements import el
from tests.healing.pages import URL, PageDriver, password, submit, username, v1

ROLE = "textbox:login-username"


def moved_page():
    """The username field with every handle changed (so it must be scored to be found)."""
    moved = username(
        id="field_7",
        name=None,
        data_testid=None,
        aria_label=None,
        classes=["css-1x"],
        sel="html > body > main > section > form > input",
        xpath="/html[1]/body[1]/main[1]/section[1]/form[1]/input[1]",
    )
    return [moved, password(), submit()]


@pytest.fixture
def store():
    with SQLiteFingerprintStore(":memory:") as s:
        yield s


@pytest.fixture
def driver():
    return PageDriver(v1())


def healer(store, driver, **kwargs):
    return Healer(store, driver=driver, **kwargs)


# --- learning & resolving ---------------------------------------------------------------- #


def test_learn_records_the_baseline_and_resolve_finds_it(store, driver):
    h = healer(store, driver)
    summary = h.learn(URL, v1())
    assert summary.added == 3
    result = h.resolve(URL, ROLE)
    assert result.outcome == "exact" and result.element.id == "user-4471"


def test_learn_without_elements_loads_and_reads_the_page(store, driver):
    summary = healer(store, driver).learn(URL)
    assert summary.added == 3 and driver.navigations == [URL] and driver.reads == 1


def test_resolve_navigates_unless_told_not_to(store, driver):
    h = healer(store, driver)
    h.learn(URL, v1())
    h.resolve(URL, ROLE)
    h.resolve(URL, ROLE, navigate=False)
    assert driver.navigations == [URL]


def test_page_urls_are_normalised_so_noise_does_not_split_a_fingerprint(store, driver):
    h = healer(store, driver)
    h.learn(URL + "?utm_source=mail#top", v1())
    assert h.resolve(URL + "/", ROLE).element.id == "user-4471"
    assert {f.page_url for f in store.fingerprints()} == {URL}


def test_click_and_write_act_on_the_resolved_element(store, driver):
    h = healer(store, driver)
    h.learn(URL, v1())
    driver.elements = moved_page()  # the page changed
    assert h.click(URL, ROLE).healed
    assert driver.clicked[0].id == "field_7"
    h.write("alice", URL, ROLE)
    assert driver.written[0][0] == "alice" and driver.written[0][1].id == "field_7"


def test_unknown_roles_and_unfindable_elements_raise(store, driver):
    h = healer(store, driver)
    h.learn(URL, v1())
    with pytest.raises(FingerprintNotFoundError):
        h.resolve(URL, "button:nope")
    driver.elements = [el("input", type="search", name="q", sel="s")]
    with pytest.raises(ElementNotHealedError):
        h.resolve(URL, ROLE)


def test_a_canvas_page_raises_the_unsupported_rendering_error(store, driver):
    h = healer(store, driver)
    h.learn(URL, v1())
    page = (1280, 800)
    driver.elements = [
        el("html", sel="html", box=page),
        el("body", sel="b", box=page),
        el("canvas", sel="c", box=(1280, 780)),
    ]
    with pytest.raises(UnsupportedRenderingError):
        h.resolve(URL, ROLE)


def test_the_threshold_and_margin_are_passed_through(store, driver):
    h = healer(store, driver, threshold=0.99)
    h.learn(URL, v1())
    driver.elements = moved_page()
    with pytest.raises(ElementNotHealedError):
        h.resolve(URL, ROLE)


# --- events ------------------------------------------------------------------------------- #


def test_a_heal_emits_an_element_healed_event_with_the_documented_fields(store, driver):
    events = []
    h = healer(store, driver, on_event=events.append, run_id="heal-run")
    h.learn(URL, v1())
    driver.elements = moved_page()
    result = h.resolve(URL, ROLE)

    [event] = events
    assert event["event"] == "element_healed" and event["run_id"] == "heal-run"
    assert list(event["data"]) == list(EVENT_DATA_FIELDS["element_healed"])
    data = event["data"]
    assert data["element_key"] == f"{URL}#{ROLE}" and data["page_url"] == URL
    assert data["strategy_used"] == "weighted_score"
    assert data["confidence_score"] == result.confidence
    assert data["old_locator"].startswith("stable_attr:data-testid=")
    assert data["new_locator"] == 'id=[id="field_7"]'


def test_no_event_for_an_exact_match(store, driver):
    events = []
    h = healer(store, driver, on_event=events.append)
    h.learn(URL, v1())
    h.resolve(URL, ROLE)
    assert events == []


def test_the_webhook_receives_the_same_event(store, driver, webhook_receiver):
    events = []
    h = healer(store, driver, on_event=events.append, webhook_url=webhook_receiver.url)
    h.learn(URL, v1())
    driver.elements = moved_page()
    h.resolve(URL, ROLE)
    h.close()
    assert webhook_receiver.payloads == events


def test_a_raising_on_event_callback_does_not_undo_the_heal(store, driver):
    h = healer(store, driver, on_event=lambda e: 1 / 0)
    h.learn(URL, v1())
    driver.elements = moved_page()
    assert h.resolve(URL, ROLE).healed
    assert store.get(URL, ROLE).id == "field_7"


def test_an_invalid_webhook_url_fails_at_construction(store, driver):
    with pytest.raises(ValueError, match="webhook"):
        healer(store, driver, webhook_url="not-a-url")


# --- history --------------------------------------------------------------------------------- #


def test_history_and_report_expose_the_audit_trail(store, driver):
    h = healer(store, driver)
    h.learn(URL, v1())
    driver.elements = moved_page()
    h.resolve(URL, ROLE)
    [record] = h.history()
    assert record.element_role == ROLE and record.change_kind == "churn"
    assert h.history(URL + "?utm_source=x", ROLE) == [record]
    assert h.history(change_kind="regression") == []
    assert h.report() == [
        {
            "element_key": f"{URL}#{ROLE}",
            "heals": 1,
            "regressions": 0,
            "churn": 1,
            "last_healed_at": record.at,
        }
    ]


# --- ownership --------------------------------------------------------------------------------- #


def test_a_caller_supplied_driver_and_store_are_left_open(store, driver):
    with healer(store, driver) as h:
        h.learn(URL, v1())
    assert driver.started == 0 and driver.closed == 0
    assert store.get(URL, ROLE) is not None  # the store still works after the healer closed


def test_a_path_store_is_opened_and_closed_by_the_healer(tmp_path, driver):
    path = tmp_path / "sub" / "fp.db"
    with Healer(path, driver=driver) as h:
        h.learn(URL, v1())
        opened = h.store
    with pytest.raises(Exception, match="closed"):
        opened.get(URL, ROLE)  # closed on exit
    with SQLiteFingerprintStore(path) as reopened:
        assert len(reopened.fingerprints()) == 3  # and it persisted


def test_an_owned_driver_is_started_and_closed(store, monkeypatch):
    made = PageDriver(v1())
    monkeypatch.setattr("healix.healer.create_driver", lambda backend, *, headless: made)
    with Healer(store, headless=False) as h:
        h.learn(URL, v1())
    assert (made.started, made.closed) == (1, 1)


def test_the_default_store_is_healix_db_in_the_working_directory(tmp_path, monkeypatch, driver):
    monkeypatch.chdir(tmp_path)
    with Healer(driver=driver) as h:
        h.learn(URL, v1())
    assert (tmp_path / "healix.db").is_file()


def test_the_healer_can_be_used_again_after_close(store, driver):
    h = healer(store, driver)
    h.learn(URL, v1())
    h.close()
    assert h.resolve(URL, ROLE).outcome == "exact"  # restarts lazily
    h.close()
    h.close()  # idempotent
