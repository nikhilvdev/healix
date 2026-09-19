"""The PostgreSQL fingerprint store.

The same contract as ``SQLiteFingerprintStore`` — fingerprints keyed by ``(page_url,
element_role)``, and every heal written to the history in the same transaction as the fingerprint
update — for teams that want fingerprints shared across machines or CI runs.

Needs the ``psycopg`` driver: ``pip install 'healix[postgres]'``. Open one with a URL::

    Healer("postgresql://user:password@host:5432/dbname")

or through ``healix.healing.open_store``. The tables (``healix_meta``, ``healix_fingerprints``,
``healix_healing_history``) are created on first use in the connection's default schema, and are
prefixed so they can share a database with other applications. JSON columns are ``JSONB``, so the
history can be queried in SQL. Creating the schema takes an advisory lock, so several processes
starting at once is safe.

The connection URL usually carries a password: it is never logged, and is masked in ``repr`` and in
error messages.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from healix.healing.fingerprint import Fingerprint
from healix.healing.history import HealRecord
from healix.healing.store import (
    HISTORY_COLUMNS,
    SCHEMA_VERSION,
    FingerprintStore,
    StoreError,
    history_filter,
    record_from_row,
)
from healix.timeutil import iso_utc, utc_now

_SCHEMA_LOCK_KEY = 0x48454C49  # "HELI": serialises schema creation between processes

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS healix_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS healix_fingerprints (
        seq          BIGINT GENERATED ALWAYS AS IDENTITY,
        page_url     TEXT NOT NULL,
        element_role TEXT NOT NULL,
        data         JSONB NOT NULL,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (page_url, element_role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS healix_healing_history (
        id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        page_url        TEXT NOT NULL,
        element_role    TEXT NOT NULL,
        at              TEXT NOT NULL,
        kind            TEXT NOT NULL,
        strategy        TEXT NOT NULL,
        confidence      DOUBLE PRECISION NOT NULL,
        old_locator     TEXT NOT NULL,
        new_locator     TEXT NOT NULL,
        change_kind     TEXT NOT NULL,
        changed_fields  JSONB NOT NULL,
        old_fingerprint JSONB NOT NULL,
        new_fingerprint JSONB NOT NULL,
        run_id          TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS healix_healing_history_element
        ON healix_healing_history (page_url, element_role)
    """,
)


def mask_url(url: str) -> str:
    """``url`` with any password replaced by ``***`` — safe to show."""
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
            return parts._replace(netloc=netloc).geturl()
    except ValueError:
        pass
    return url


def _redact(message: str, url: str) -> str:
    """Remove the URL's password from ``message``, in case a driver echoed it."""
    try:
        password = urlsplit(url).password
    except ValueError:
        password = None
    return message.replace(password, "***") if password else message


class PostgresFingerprintStore(FingerprintStore):
    """A ``FingerprintStore`` in PostgreSQL. See the module docstring."""

    def __init__(self, url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise StoreError(
                "the Postgres store needs the psycopg driver: pip install 'healix[postgres]'"
            ) from exc

        self._url = url
        self._jsonb = Jsonb
        self._lock = threading.RLock()
        try:
            # autocommit: each statement is its own transaction unless wrapped in
            # ``conn.transaction()``, which is how ``apply_heal`` makes its two writes atomic.
            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        except psycopg.Error as exc:
            target = mask_url(url)
            raise StoreError(
                f"could not connect to Postgres at {target}: {_redact(str(exc), url)}"
            ) from None
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    def __repr__(self) -> str:
        return f"PostgresFingerprintStore({mask_url(self._url)!r})"

    def _init_schema(self) -> None:
        with self._lock, self._conn.transaction():
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
            for statement in _SCHEMA:
                self._conn.execute(statement)
            row = self._conn.execute(
                "SELECT value FROM healix_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO healix_meta (key, value) VALUES ('schema_version', %s)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise StoreError(
                    f"this database uses schema version {row['value']}, but this Healix only "
                    f"understands up to {SCHEMA_VERSION}; upgrade Healix to use it"
                )

    # -- fingerprints ------------------------------------------------------------- #

    def get(self, page_url: str, element_role: str) -> Fingerprint | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM healix_fingerprints WHERE page_url = %s AND element_role = %s",
                (page_url, element_role),
            ).fetchone()
        return Fingerprint.from_dict(row["data"]) if row else None

    def _upsert(self, fingerprint: Fingerprint) -> None:
        now = iso_utc(utc_now())
        self._conn.execute(
            "INSERT INTO healix_fingerprints "
            "(page_url, element_role, data, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (page_url, element_role) DO UPDATE SET "
            "data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
            (
                fingerprint.page_url,
                fingerprint.element_role,
                self._jsonb(fingerprint.to_dict()),
                now,
                now,
            ),
        )

    def put(self, fingerprint: Fingerprint) -> None:
        with self._lock:
            self._upsert(fingerprint)

    def put_if_absent(self, fingerprint: Fingerprint) -> bool:
        now = iso_utc(utc_now())
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO healix_fingerprints "
                "(page_url, element_role, data, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (page_url, element_role) DO NOTHING",
                (
                    fingerprint.page_url,
                    fingerprint.element_role,
                    self._jsonb(fingerprint.to_dict()),
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def fingerprints(self, page_url: str | None = None) -> list[Fingerprint]:
        query = "SELECT data FROM healix_fingerprints"
        args: tuple[str, ...] = ()
        if page_url is not None:
            query, args = query + " WHERE page_url = %s", (page_url,)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY seq", args).fetchall()
        return [Fingerprint.from_dict(row["data"]) for row in rows]

    # -- healing ------------------------------------------------------------------- #

    def apply_heal(self, fingerprint: Fingerprint, record: HealRecord) -> HealRecord:
        with self._lock, self._conn.transaction():  # both writes or neither
            self._upsert(fingerprint)
            row = self._conn.execute(
                "INSERT INTO healix_healing_history (page_url, element_role, at, kind, strategy, "
                "confidence, old_locator, new_locator, change_kind, changed_fields, "
                "old_fingerprint, new_fingerprint, run_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
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
                    self._jsonb(list(record.changed_fields)),
                    self._jsonb(record.old_fingerprint),
                    self._jsonb(record.new_fingerprint),
                    record.run_id,
                ),
            ).fetchone()
        assert row is not None
        return replace(record, id=row["id"])

    def history(
        self,
        page_url: str | None = None,
        element_role: str | None = None,
        *,
        change_kind: str | None = None,
        limit: int | None = None,
    ) -> list[HealRecord]:
        clause, args = history_filter(page_url, element_role, change_kind, "%s")
        query = f"SELECT {HISTORY_COLUMNS} FROM healix_healing_history{clause} ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT %s"
            args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [record_from_row(row, _identity) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _identity(value: Any) -> Any:
    return value
