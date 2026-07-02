"""SQLite store: zero-infrastructure durability.

The same schema and CAS shape port 1:1 to Postgres (UNIQUE constraint ->
ON CONFLICT DO NOTHING; the guarded UPDATE -> UPDATE ... WHERE state = ANY(...)
with SKIP LOCKED workers). SQLite's BEGIN IMMEDIATE gives us the single-writer
serialization we need for correctness on one machine.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..records import EffectRecord, EffectResult, JournalEntry
from ..state import EffectState, can_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    effect_id        TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL UNIQUE,
    tool             TEXT NOT NULL,
    state            TEXT NOT NULL,
    args_fingerprint TEXT NOT NULL,
    args_json        TEXT NOT NULL,
    scope            TEXT NOT NULL,
    provider_key     TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    result_json      TEXT,
    lease_owner      TEXT,
    lease_expires_at REAL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_effects_state_updated ON effects (state, updated_at);

CREATE TABLE IF NOT EXISTS effect_journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    effect_id  TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    at         REAL NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_journal_effect ON effect_journal (effect_id, seq);
"""

_FIELDS = (
    "effect_id, idempotency_key, tool, state, args_fingerprint, args_json, scope, "
    "provider_key, attempt, max_attempts, result_json, lease_owner, lease_expires_at, "
    "created_at, updated_at, note"
)


class SQLiteStore:
    """Store backed by a SQLite file (or a shared in-memory database).

    ``path=":memory:"`` creates a private shared-cache database that lives as
    long as this store object — handy for demos and tests.
    """

    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        if path == ":memory:":
            # Named shared-cache URI so per-call connections see one database;
            # the anchor connection keeps it alive.
            self._uri = f"file:openonce_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._anchor: sqlite3.Connection | None = sqlite3.connect(self._uri, uri=True)
        else:
            self._uri = f"file:{path}"
            self._anchor = None
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._uri, uri=True, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # -- protocol ---------------------------------------------------------

    def create_or_get(self, record: EffectRecord) -> tuple[EffectRecord, bool]:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"INSERT INTO effects ({_FIELDS}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    record.effect_id,
                    record.idempotency_key,
                    record.tool,
                    record.state.value,
                    record.args_fingerprint,
                    record.args_json,
                    record.scope,
                    record.provider_key,
                    record.attempt,
                    record.max_attempts,
                    record.result.to_json() if record.result else None,
                    record.lease_owner,
                    record.lease_expires_at,
                    now,
                    now,
                    record.note,
                ),
            )
            created = cur.rowcount == 1
            if created:
                self._append_journal(conn, record.effect_id, None, record.state, {})
            row = conn.execute(
                f"SELECT {_FIELDS} FROM effects WHERE idempotency_key = ?",
                (record.idempotency_key,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._to_record(row), created

    def transition(
        self,
        effect_id: str,
        from_states: frozenset[EffectState] | set[EffectState],
        to_state: EffectState,
        *,
        set_fields: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        lease_owner: str | None = None,
        require_lease_owner: str | None = None,
    ) -> EffectRecord | None:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_FIELDS} FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            current = EffectState(row["state"])
            if current not in from_states or not can_transition(current, to_state):
                conn.execute("ROLLBACK")
                return None
            if require_lease_owner is not None:
                lease_live = row["lease_expires_at"] is not None and row["lease_expires_at"] > now
                if lease_live and row["lease_owner"] != require_lease_owner:
                    conn.execute("ROLLBACK")
                    return None

            sets: dict[str, Any] = {"state": to_state.value, "updated_at": now}
            for k, v in (set_fields or {}).items():
                if k == "result" and isinstance(v, EffectResult):
                    sets["result_json"] = v.to_json()
                else:
                    sets[k] = v
            if lease_owner is not None:
                sets["lease_owner"] = lease_owner
            assignments = ", ".join(f"{k} = ?" for k in sets)
            conn.execute(
                f"UPDATE effects SET {assignments} WHERE effect_id = ?",
                (*sets.values(), effect_id),
            )
            self._append_journal(conn, effect_id, current, to_state, payload or {})
            row = conn.execute(
                f"SELECT {_FIELDS} FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._to_record(row)

    def get(self, effect_id: str) -> EffectRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_FIELDS} FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
        return self._to_record(row) if row else None

    def get_by_key(self, idempotency_key: str) -> EffectRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_FIELDS} FROM effects WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._to_record(row) if row else None

    def journal(self, effect_id: str) -> list[JournalEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, effect_id, from_state, to_state, at, payload "
                "FROM effect_journal WHERE effect_id = ? ORDER BY seq",
                (effect_id,),
            ).fetchall()
        return [
            JournalEntry(
                seq=r["seq"],
                effect_id=r["effect_id"],
                from_state=EffectState(r["from_state"]) if r["from_state"] else None,
                to_state=EffectState(r["to_state"]),
                at=r["at"],
                payload=json.loads(r["payload"]),
            )
            for r in rows
        ]

    def scan_states(
        self, states: frozenset[EffectState] | set[EffectState], *, updated_before: float
    ) -> list[EffectRecord]:
        marks = ",".join("?" for _ in states)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_FIELDS} FROM effects "
                f"WHERE state IN ({marks}) AND updated_at <= ? ORDER BY updated_at",
                (*[s.value for s in states], updated_before),
            ).fetchall()
        return [self._to_record(r) for r in rows]

    # -- helpers ----------------------------------------------------------

    def _append_journal(
        self,
        conn: sqlite3.Connection,
        effect_id: str,
        from_state: EffectState | None,
        to_state: EffectState,
        payload: dict[str, object],
    ) -> None:
        conn.execute(
            "INSERT INTO effect_journal (effect_id, from_state, to_state, at, payload) "
            "VALUES (?,?,?,?,?)",
            (
                effect_id,
                from_state.value if from_state else None,
                to_state.value,
                self._clock(),
                json.dumps(payload),
            ),
        )

    @staticmethod
    def _to_record(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(
            effect_id=row["effect_id"],
            idempotency_key=row["idempotency_key"],
            tool=row["tool"],
            state=EffectState(row["state"]),
            args_fingerprint=row["args_fingerprint"],
            args_json=row["args_json"],
            scope=row["scope"],
            provider_key=row["provider_key"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            result=EffectResult.from_json(row["result_json"]) if row["result_json"] else None,
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            note=row["note"],
        )
