"""The fingerprint store contract, run against every backend.

The same assertions run against SQLite and a real PostgreSQL server, so "swappable" is checked
rather than assumed. Backend-specific behaviour is at the bottom.
"""

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from healix.healing import (
    FingerprintStore,
    SQLiteFingerprintStore,
    StoreError,
    open_store,
)
from healix.healing.history import HealRecord
from tests.conftest import drop_healix_tables
from tests.healing.pages import URL, fingerprint_of, password, username


@dataclass
class Backend:
    name: str
    open: Callable[[], FingerprintStore]
    set_schema_version: Callable[[str], None]


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path):
    if request.param == "sqlite":
        path = tmp_path / "fp.db"

        def set_version(value):
            conn = sqlite3.connect(path)
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (value,))
            conn.commit()
            conn.close()

        return Backend("sqlite", lambda: SQLiteFingerprintStore(path), set_version)

    import psycopg

    from healix.healing.postgres_store import PostgresFingerprintStore

    url = request.getfixturevalue("postgres_url")
    drop_healix_tables(url)

    def set_version(value):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("UPDATE healix_meta SET value = %s WHERE key = 'schema_version'", (value,))

    return Backend("postgres", lambda: PostgresFingerprintStore(url), set_version)


@pytest.fixture
def store(backend):
    with backend.open() as s:
        yield s


def record(role="textbox:username", **overrides):
    base = {
        "page_url": URL,
        "element_role": role,
        "at": "2026-01-01T00:00:00.000Z",
        "kind": "scored",
        "strategy": "weighted_score",
        "confidence": 0.8,
        "old_locator": "id=a",
        "new_locator": "name=b",
        "change_kind": "churn",
        "changed_fields": ("id",),
        "old_fingerprint": {"id": "a"},
        "new_fingerprint": {"id": "b"},
        "run_id": "run-1",
    }
    base.update(overrides)
    return HealRecord(**base)


def test_the_interface_is_abstract():
    with pytest.raises(TypeError):
        FingerprintStore()  # another backend implements it


# --- fingerprints ---------------------------------------------------------------------------- #


def test_put_and_get_round_trip(store):
    fp = fingerprint_of(username(), "textbox:username")
    store.put(fp)
    assert store.get(URL, "textbox:username") == fp
    assert store.get(URL, "missing") is None
    assert store.get("https://other.example/", "textbox:username") is None


def test_put_replaces_an_existing_fingerprint(store):
    store.put(fingerprint_of(username(), "r"))
    store.put(fingerprint_of(username(id="user-9"), "r"))
    assert store.get(URL, "r").id == "user-9"
    assert len(store.fingerprints()) == 1


def test_put_if_absent_never_overwrites(store):
    original = fingerprint_of(username(), "r")
    assert store.put_if_absent(original) is True
    assert store.put_if_absent(fingerprint_of(username(id="changed"), "r")) is False
    assert store.get(URL, "r") == original


def test_fingerprints_are_listed_in_insertion_order_and_filtered_by_page(store):
    store.put(fingerprint_of(username(), "b"))
    store.put(fingerprint_of(password(), "a"))
    store.put(fingerprint_of(username(), "c", url="https://e.com/other"))
    assert [f.element_role for f in store.fingerprints()] == ["b", "a", "c"]
    assert [f.element_role for f in store.fingerprints(URL)] == ["b", "a"]


def test_replacing_a_fingerprint_keeps_its_place_in_the_order(store):
    for role in ("first", "second", "third"):
        store.put(fingerprint_of(username(), role))
    store.put(fingerprint_of(username(id="changed"), "first"))
    assert [f.element_role for f in store.fingerprints()] == ["first", "second", "third"]


def test_the_key_is_page_url_and_role_together(store):
    store.put(fingerprint_of(username(), "r"))
    store.put(fingerprint_of(password(), "r", url="https://e.com/other"))
    assert store.get(URL, "r").name == "username"
    assert store.get("https://e.com/other", "r").name == "password"


def test_unicode_and_nested_data_survive(store):
    fp = fingerprint_of(username(name="ユーザー", aria_label="Prénom"), "r")
    fp.dom_context = {"tag_path": ["html", "body", "form"], "sibling_index": 3, "nearby": None}
    store.put(fp)
    back = store.get(URL, "r")
    assert back.name == "ユーザー" and back == fp
    assert back.dom_context["sibling_index"] == 3 and back.dom_context["nearby"] is None


# --- healing --------------------------------------------------------------------------------- #


def test_apply_heal_replaces_the_fingerprint_and_appends_history(store):
    store.put(fingerprint_of(username(), "textbox:username"))
    healed = fingerprint_of(username(id="user-9032"), "textbox:username")
    stored = store.apply_heal(healed, record())
    assert stored.id == 1
    assert store.get(URL, "textbox:username").id == "user-9032"
    [only] = store.history()
    assert only == stored and only.old_fingerprint == {"id": "a"} and only.changed_fields == ("id",)


def test_a_failing_heal_writes_neither_the_fingerprint_nor_the_history(store):
    original = fingerprint_of(username(), "r")
    store.put(original)
    bad = record(role="r", confidence=None)  # NOT NULL violation on the history insert
    with pytest.raises(Exception, match="(?i)null|constraint"):
        store.apply_heal(fingerprint_of(username(id="never-stored"), "r"), bad)
    assert store.get(URL, "r") == original  # the fingerprint update was rolled back
    assert store.history() == []
    store.put(fingerprint_of(username(id="still-usable"), "r"))  # and the store still works
    assert store.get(URL, "r").id == "still-usable"


def test_history_is_newest_first_filterable_and_limitable(store):
    kinds = [("a", "churn"), ("b", "regression"), ("a", "regression"), ("a", "churn")]
    for i, (role, kind) in enumerate(kinds):
        store.apply_heal(
            fingerprint_of(username(), role),
            record(role=role, change_kind=kind, at=f"2026-01-0{i + 1}T00:00:00.000Z"),
        )
    assert [h.id for h in store.history()] == [4, 3, 2, 1]
    assert [h.id for h in store.history(element_role="a")] == [4, 3, 1]
    assert [h.id for h in store.history(change_kind="regression")] == [3, 2]
    assert [h.id for h in store.history(URL, "a", change_kind="churn")] == [4, 1]
    assert [h.id for h in store.history(limit=2)] == [4, 3]
    assert store.history("https://nowhere.example/") == []


def test_history_preserves_nested_fingerprints_and_the_changed_field_order(store):
    old = fingerprint_of(username(), "r").to_dict()
    new = fingerprint_of(username(id="user-9"), "r").to_dict()
    store.apply_heal(
        fingerprint_of(username(id="user-9"), "r"),
        record(role="r", old_fingerprint=old, new_fingerprint=new, changed_fields=("z", "a", "m")),
    )
    [entry] = store.history()
    assert entry.old_fingerprint == old and entry.new_fingerprint == new
    assert entry.changed_fields == ("z", "a", "m")
    assert entry.run_id == "run-1" and entry.confidence == 0.8


# --- durability ------------------------------------------------------------------------------- #


def test_everything_survives_closing_and_reopening(backend):
    with backend.open() as first:
        first.put(fingerprint_of(username(), "r"))
        first.apply_heal(fingerprint_of(username(id="user-9"), "r"), record(role="r"))
    with backend.open() as second:
        assert second.get(URL, "r").id == "user-9"
        assert len(second.history()) == 1


def test_two_connections_see_each_others_writes(backend):
    with backend.open() as a, backend.open() as b:
        a.put(fingerprint_of(username(), "shared"))
        assert b.get(URL, "shared") is not None
        b.apply_heal(fingerprint_of(username(id="x"), "shared"), record(role="shared"))
        assert len(a.history()) == 1


def test_a_store_from_a_newer_healix_is_refused(backend):
    backend.open().close()
    backend.set_schema_version("99")
    with pytest.raises(StoreError, match="upgrade Healix"):
        backend.open()


def test_reopening_does_not_reset_the_schema_version(backend):
    backend.open().close()
    backend.set_schema_version("1")
    backend.open().close()
    backend.set_schema_version("99")  # still detectable: the version was not rewritten
    with pytest.raises(StoreError):
        backend.open()


def test_concurrent_writers_do_not_lose_data(store):
    def write(n):
        for i in range(25):
            store.put(fingerprint_of(username(), f"role-{n}-{i}"))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.fingerprints()) == 100


# --- open_store ------------------------------------------------------------------------------- #


def test_open_store_returns_an_existing_store_untouched(store):
    assert open_store(store) is store


def test_open_store_treats_a_path_as_sqlite(tmp_path):
    with open_store(tmp_path / "deep" / "fp.db") as s:
        assert isinstance(s, SQLiteFingerprintStore)
        s.put(fingerprint_of(username(), "r"))
    with open_store(str(tmp_path / "deep" / "fp.db")) as again:
        assert again.get(URL, "r") is not None


# --- SQLite specifics -------------------------------------------------------------------------- #


def test_sqlite_creates_missing_directories_and_supports_memory(tmp_path):
    with SQLiteFingerprintStore(tmp_path / "a" / "b" / "fp.db") as s:
        s.put(fingerprint_of(username(), "r"))
    assert (tmp_path / "a" / "b" / "fp.db").is_file()
    with SQLiteFingerprintStore(":memory:") as m:
        m.put(fingerprint_of(username(), "r"))
        assert m.get(URL, "r") is not None


# --- PostgreSQL specifics ----------------------------------------------------------------- #


@pytest.fixture
def pg(postgres_url):
    from healix.healing.postgres_store import PostgresFingerprintStore

    drop_healix_tables(postgres_url)
    return postgres_url, PostgresFingerprintStore


def test_open_store_treats_a_postgres_url_as_postgres(pg):
    url, PostgresFingerprintStore = pg
    for prefix in ("postgresql://", "postgres://"):
        with open_store(prefix + url.split("://", 1)[1]) as s:
            assert isinstance(s, PostgresFingerprintStore)


def test_the_tables_are_prefixed_and_the_history_is_queryable_in_sql(pg):
    import psycopg

    url, PostgresFingerprintStore = pg
    with PostgresFingerprintStore(url) as s:
        s.apply_heal(
            fingerprint_of(username(id="user-9"), "r"),
            record(role="r", change_kind="regression", changed_fields=("id", "name")),
        )
    with psycopg.connect(url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'healix_%'"
            ).fetchall()
        }
        assert tables == {"healix_meta", "healix_fingerprints", "healix_healing_history"}
        # JSONB: the audit trail can be queried directly
        count = conn.execute(
            "SELECT count(*) FROM healix_healing_history WHERE changed_fields ? 'name'"
        ).fetchone()[0]
        assert count == 1
        assert conn.execute(
            "SELECT data->>'id' FROM healix_fingerprints WHERE element_role = 'r'"
        ).fetchone() == ("user-9",)


def test_several_processes_creating_the_schema_at_once_is_safe(pg):
    url, PostgresFingerprintStore = pg
    errors, stores = [], []

    def open_one():
        try:
            stores.append(PostgresFingerprintStore(url))
        except Exception as exc:  # pragma: no cover - this is what the test guards against
            errors.append(exc)

    threads = [threading.Thread(target=open_one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()
    assert errors == [] and len(stores) == 8


def test_the_password_never_appears_in_the_repr_or_in_connection_errors(pg):
    url, PostgresFingerprintStore = pg
    with PostgresFingerprintStore(url) as s:
        assert "healix:***@" in repr(s) and "healix:healix@" not in repr(s)

    bad_password = url.replace("healix:healix@", "healix:TOP-SECRET-PW@")
    with pytest.raises(StoreError) as info:
        PostgresFingerprintStore(bad_password)
    assert "TOP-SECRET-PW" not in str(info.value) and "TOP-SECRET-PW" not in repr(info.value)
    assert "could not connect to Postgres" in str(info.value)

    unreachable = "postgresql://user:TOP-SECRET-PW@127.0.0.1:1/none"
    with pytest.raises(StoreError) as info:
        PostgresFingerprintStore(unreachable)
    assert "TOP-SECRET-PW" not in str(info.value)


def test_the_connection_is_released_if_schema_setup_fails(pg, monkeypatch):
    url, PostgresFingerprintStore = pg
    with PostgresFingerprintStore(url):
        pass
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE healix_meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(StoreError):
        PostgresFingerprintStore(url)
    with psycopg.connect(url) as conn:  # nothing left holding the database
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'"
            ).fetchone()[0]
            == 0
        )


def test_a_missing_driver_explains_how_to_install_it(monkeypatch):
    import sys

    from healix.healing.postgres_store import PostgresFingerprintStore

    monkeypatch.setitem(sys.modules, "psycopg", None)  # makes `import psycopg` raise ImportError
    with pytest.raises(StoreError, match=r"pip install 'healix\[postgres\]'"):
        PostgresFingerprintStore("postgresql://u:p@localhost/db")


def test_a_driver_that_echoes_the_password_in_its_error_is_redacted(monkeypatch):
    import psycopg

    from healix.healing.postgres_store import PostgresFingerprintStore

    def echoing_connect(url, **kwargs):
        raise psycopg.OperationalError(f"connection failed for {url}")  # a leaky driver

    monkeypatch.setattr(psycopg, "connect", echoing_connect)
    with pytest.raises(StoreError) as info:
        PostgresFingerprintStore("postgresql://user:TOP-SECRET-PW@db.example:5432/app")
    assert "TOP-SECRET-PW" not in str(info.value)
    assert "db.example" in str(info.value)  # the host is still shown: that is useful
