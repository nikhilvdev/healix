"""Persistence for fingerprints and the healing history.

``FingerprintStore`` is the interface the rest of Healix depends on. ``SQLiteFingerprintStore`` is
the default implementation (stdlib ``sqlite3``, no extra dependency);
``healix.healing.postgres_store.PostgresFingerprintStore`` is the Postgres one (needs
``pip install 'healix[postgres]'``). ``open_store`` picks between them from a path or a
``postgresql://`` URL. Both are held to the same contract test suite.

Fingerprints are keyed by ``(page_url, element_role)``. Every heal is written to the history in
the **same transaction** as the fingerprint update it caused, so the store can never hold a
healed fingerprint without its audit record, or the reverse.

Page URLs are used as given: callers normalise them (``healix.discovery.manifest.normalize_url``)
so that a fingerprint recorded during extraction and one looked up while healing agree.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from healix.healing.fingerprint import Fingerprint
from healix.healing.history import HealRecord
from healix.timeutil import iso_utc, utc_now

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "healix.db"


class StoreError(RuntimeError):
    """The store cannot be used (for example, its schema is from a newer Healix)."""


HISTORY_COLUMNS = (
    "id, page_url, element_role, at, kind, strategy, confidence, old_locator, new_locator, "
    "change_kind, changed_fields, old_fingerprint, new_fingerprint, run_id"
)


def record_from_row(row: Mapping[str, Any], decode: Callable[[Any], Any]) -> HealRecord:
    """Build a ``HealRecord`` from a history row.

    ``decode`` turns a JSON column into Python: ``json.loads`` for SQLite (JSON stored as text),
    the identity for Postgres (``JSONB`` arrives already decoded).
    """
    return HealRecord(
        id=row["id"],
        page_url=row["page_url"],
        element_role=row["element_role"],
        at=row["at"],
        kind=row["kind"],
        strategy=row["strategy"],
        confidence=row["confidence"],
        old_locator=row["old_locator"],
        new_locator=row["new_locator"],
        change_kind=row["change_kind"],
        changed_fields=tuple(decode(row["changed_fields"])),
        old_fingerprint=decode(row["old_fingerprint"]),
        new_fingerprint=decode(row["new_fingerprint"]),
        run_id=row["run_id"],
    )


def history_filter(
    page_url: str | None,
    element_role: str | None,
    change_kind: str | None,
    placeholder: str,
) -> tuple[str, list[Any]]:
    """The ``WHERE`` clause (with ``placeholder`` parameters) and arguments for a history query."""
    where: list[str] = []
    args: list[Any] = []
    for column, value in (
        ("page_url", page_url),
        ("element_role", element_role),
        ("change_kind", change_kind),
    ):
        if value is not None:
            where.append(f"{column} = {placeholder}")
            args.append(value)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def is_postgres_url(target: str) -> bool:
    return target.startswith(("postgresql://", "postgres://"))


def open_store(target: FingerprintStore | str | os.PathLike[str]) -> FingerprintStore:
    """A store from what a caller has: a store (returned as is), a ``postgresql://`` URL, or a path.

    Anything that is not a Postgres URL is a SQLite file path.
    """
    if isinstance(target, FingerprintStore):
        return target
    text = os.fspath(target)
    if is_postgres_url(text):
        from healix.healing.postgres_store import PostgresFingerprintStore

        return PostgresFingerprintStore(text)
    return SQLiteFingerprintStore(text)


class FingerprintStore(ABC):
    """Where fingerprints and the healing history live."""

    @abstractmethod
    def get(self, page_url: str, element_role: str) -> Fingerprint | None:
        """The stored fingerprint, or ``None``."""

    @abstractmethod
    def put(self, fingerprint: Fingerprint) -> None:
        """Insert or replace a fingerprint (no history is written)."""

    @abstractmethod
    def put_if_absent(self, fingerprint: Fingerprint) -> bool:
        """Insert only if its key is new. ``True`` if it was inserted."""

    @abstractmethod
    def fingerprints(self, page_url: str | None = None) -> list[Fingerprint]:
        """Every fingerprint (of one page, if ``page_url`` is given), in insertion order."""

    @abstractmethod
    def apply_heal(self, fingerprint: Fingerprint, record: HealRecord) -> HealRecord:
        """Atomically replace the fingerprint and append ``record`` to the history.

        Returns the record as stored (with its ``id``).
        """

    @abstractmethod
    def history(
        self,
        page_url: str | None = None,
        element_role: str | None = None,
        *,
        change_kind: str | None = None,
        limit: int | None = None,
    ) -> list[HealRecord]:
        """Heal records, newest first, optionally filtered."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection."""

    def __enter__(self) -> FingerprintStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fingerprints (
    page_url     TEXT NOT NULL,
    element_role TEXT NOT NULL,
    data         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (page_url, element_role)
);
CREATE TABLE IF NOT EXISTS healing_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    page_url        TEXT NOT NULL,
    element_role    TEXT NOT NULL,
    at              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    confidence      REAL NOT NULL,
    old_locator     TEXT NOT NULL,
    new_locator     TEXT NOT NULL,
    change_kind     TEXT NOT NULL,
    changed_fields  TEXT NOT NULL,
    old_fingerprint TEXT NOT NULL,
    new_fingerprint TEXT NOT NULL,
    run_id          TEXT
);
CREATE INDEX IF NOT EXISTS healing_history_element
    ON healing_history (page_url, element_role);
"""


class SQLiteFingerprintStore(FingerprintStore):
    """The default store: one SQLite file (or ``":memory:"``).

    Safe to share between threads (access is serialised with a lock). Opening a file created by
    a newer Healix raises ``StoreError`` rather than risk misreading it.
    """

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path} uses schema version {row['value']}, but this Healix only "
                    f"understands up to {SCHEMA_VERSION}; upgrade Healix to use it"
                )

    # -- fingerprints ------------------------------------------------------------- #

    @staticmethod
    def _load(row: sqlite3.Row) -> Fingerprint:
        return Fingerprint.from_dict(json.loads(row["data"]))

    @staticmethod
    def _dump(fingerprint: Fingerprint) -> str:
        return json.dumps(fingerprint.to_dict(), ensure_ascii=False, sort_keys=True)

    def get(self, page_url: str, element_role: str) -> Fingerprint | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM fingerprints WHERE page_url = ? AND element_role = ?",
                (page_url, element_role),
            ).fetchone()
        return self._load(row) if row else None

    def put(self, fingerprint: Fingerprint) -> None:
        with self._lock, self._conn:
            self._upsert(fingerprint)

    def _upsert(self, fingerprint: Fingerprint) -> None:
        now = iso_utc(utc_now())
        self._conn.execute(
            "INSERT INTO fingerprints (page_url, element_role, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (page_url, element_role) DO UPDATE SET "
            "data = excluded.data, updated_at = excluded.updated_at",
            (fingerprint.page_url, fingerprint.element_role, self._dump(fingerprint), now, now),
        )

    def put_if_absent(self, fingerprint: Fingerprint) -> bool:
        now = iso_utc(utc_now())
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO fingerprints (page_url, element_role, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (page_url, element_role) DO NOTHING",
                (fingerprint.page_url, fingerprint.element_role, self._dump(fingerprint), now, now),
            )
            return cursor.rowcount == 1

    def fingerprints(self, page_url: str | None = None) -> list[Fingerprint]:
        query = "SELECT data FROM fingerprints"
        args: tuple[str, ...] = ()
        if page_url is not None:
            query, args = query + " WHERE page_url = ?", (page_url,)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY rowid", args).fetchall()
        return [self._load(row) for row in rows]

    # -- healing ------------------------------------------------------------------- #

    def apply_heal(self, fingerprint: Fingerprint, record: HealRecord) -> HealRecord:
        with self._lock, self._conn:  # one transaction: both writes or neither
            self._upsert(fingerprint)
            cursor = self._conn.execute(
                "INSERT INTO healing_history (page_url, element_role, at, kind, strategy, "
                "confidence, old_locator, new_locator, change_kind, changed_fields, "
                "old_fingerprint, new_fingerprint, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.page_url,
                    record.element_role,
                    record.at,
                    record.kind,
                    record.strategy,
                    record.confidence,
                    record.old_locator,
                    record.new_locator,
                    record.change_kind,
                    json.dumps(list(record.changed_fields)),
                    json.dumps(record.old_fingerprint, ensure_ascii=False, sort_keys=True),
                    json.dumps(record.new_fingerprint, ensure_ascii=False, sort_keys=True),
                    record.run_id,
                ),
            )
        return replace(record, id=cursor.lastrowid)

    def history(
        self,
        page_url: str | None = None,
        element_role: str | None = None,
        *,
        change_kind: str | None = None,
        limit: int | None = None,
    ) -> list[HealRecord]:
        clause, args = history_filter(page_url, element_role, change_kind, "?")
        query = f"SELECT {HISTORY_COLUMNS} FROM healing_history{clause} ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [record_from_row(row, json.loads) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
