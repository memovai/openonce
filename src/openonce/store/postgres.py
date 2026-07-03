"""Postgres store: the production tier.

Semantics are identical to the SQLite store (which is identical to the
in-memory reference — the executable spec):

- ``create_or_get``: ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING``
  — first-writer-wins across any number of processes/hosts.
- ``transition``: ``SELECT ... FOR UPDATE`` + guarded state CAS + journal
  append, all in one transaction. A raced or illegal transition rolls back
  and returns None; it can never be half-applied.

Requires ``pip install openonce[postgres]`` (psycopg 3). One connection per
store instance; pass distinct instances to distinct threads, or use a
connection pool at the layer above (each method is a single short
transaction, so pooling composes trivially).
"""

from __future__ import annotations

import json
import time
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
    lease_expires_at DOUBLE PRECISION,
    created_at       DOUBLE PRECISION NOT NULL,
    updated_at       DOUBLE PRECISION NOT NULL,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_effects_state_updated ON effects (state, updated_at);

CREATE TABLE IF NOT EXISTS effect_journal (
    seq        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effect_id  TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    at         DOUBLE PRECISION NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_journal_effect ON effect_journal (effect_id, seq);
"""

_FIELDS = (
    "effect_id, idempotency_key, tool, state, args_fingerprint, args_json, scope, "
    "provider_key, attempt, max_attempts, result_json, lease_owner, lease_expires_at, "
    "created_at, updated_at, note"
)

#: Columns allowed through transition(set_fields=...). Everything else is
#: immutable after admission — enforced here, not by convention.
_MUTABLE = frozenset(
    {"attempt", "max_attempts", "result_json", "lease_owner", "lease_expires_at", "note"}
)


class PostgresStore:
    def __init__(self, dsn: str, *, clock: Callable[[], float] = time.time) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "PostgresStore requires psycopg: pip install 'openonce[postgres]'"
            ) from exc
        self._psycopg = psycopg
        self._dsn = dsn
        self._clock = clock
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> Any:
        # autocommit=False: every method is one explicit transaction.
        return self._psycopg.connect(self._dsn)

    # -- protocol ---------------------------------------------------------

    def create_or_get(self, record: EffectRecord) -> tuple[EffectRecord, bool]:
        now = self._clock()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO effects ({_FIELDS}) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
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
                self._append_journal(cur, record.effect_id, None, record.state, {})
            cur.execute(
                f"SELECT {_FIELDS} FROM effects WHERE idempotency_key = %s",
                (record.idempotency_key,),
            )
            row = cur.fetchone()
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
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_FIELDS} FROM effects WHERE effect_id = %s FOR UPDATE",
                (effect_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            current = EffectState(row[3])
            if current not in from_states or not can_transition(current, to_state):
                return None  # context managers roll back nothing-changed txns
            lease_expires_at = row[12]
            lease_owner_now = row[11]
            if require_lease_owner is not None:
                lease_live = lease_expires_at is not None and lease_expires_at > now
                if lease_live and lease_owner_now != require_lease_owner:
                    return None

            sets: dict[str, Any] = {"state": to_state.value, "updated_at": now}
            for k, v in (set_fields or {}).items():
                if k == "result" and isinstance(v, EffectResult):
                    sets["result_json"] = v.to_json()
                elif k == "result" and v is None:
                    sets["result_json"] = None
                elif k in _MUTABLE:
                    sets[k] = v
                else:
                    raise ValueError(f"field {k!r} is not mutable via transition()")
            if lease_owner is not None:
                sets["lease_owner"] = lease_owner
            assignments = ", ".join(f"{k} = %s" for k in sets)
            cur.execute(
                f"UPDATE effects SET {assignments} WHERE effect_id = %s",
                (*sets.values(), effect_id),
            )
            self._append_journal(cur, effect_id, current, to_state, payload or {})
            cur.execute(f"SELECT {_FIELDS} FROM effects WHERE effect_id = %s", (effect_id,))
            row = cur.fetchone()
        return self._to_record(row)

    def get(self, effect_id: str) -> EffectRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_FIELDS} FROM effects WHERE effect_id = %s", (effect_id,))
            row = cur.fetchone()
        return self._to_record(row) if row else None

    def get_by_key(self, idempotency_key: str) -> EffectRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_FIELDS} FROM effects WHERE idempotency_key = %s", (idempotency_key,)
            )
            row = cur.fetchone()
        return self._to_record(row) if row else None

    def journal(self, effect_id: str) -> list[JournalEntry]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT seq, effect_id, from_state, to_state, at, payload "
                "FROM effect_journal WHERE effect_id = %s ORDER BY seq",
                (effect_id,),
            )
            rows = cur.fetchall()
        return [
            JournalEntry(
                seq=r[0],
                effect_id=r[1],
                from_state=EffectState(r[2]) if r[2] else None,
                to_state=EffectState(r[3]),
                at=r[4],
                payload=json.loads(r[5]),
            )
            for r in rows
        ]

    def scan_states(
        self, states: frozenset[EffectState] | set[EffectState], *, updated_before: float
    ) -> list[EffectRecord]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_FIELDS} FROM effects "
                "WHERE state = ANY(%s) AND updated_at <= %s ORDER BY updated_at",
                ([s.value for s in states], updated_before),
            )
            rows = cur.fetchall()
        return [self._to_record(r) for r in rows]

    # -- helpers ----------------------------------------------------------

    def _append_journal(
        self,
        cur: Any,
        effect_id: str,
        from_state: EffectState | None,
        to_state: EffectState,
        payload: dict[str, object],
    ) -> None:
        cur.execute(
            "INSERT INTO effect_journal (effect_id, from_state, to_state, at, payload) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                effect_id,
                from_state.value if from_state else None,
                to_state.value,
                self._clock(),
                json.dumps(payload),
            ),
        )

    @staticmethod
    def _to_record(row: tuple[Any, ...]) -> EffectRecord:
        return EffectRecord(
            effect_id=row[0],
            idempotency_key=row[1],
            tool=row[2],
            state=EffectState(row[3]),
            args_fingerprint=row[4],
            args_json=row[5],
            scope=row[6],
            provider_key=row[7],
            attempt=row[8],
            max_attempts=row[9],
            result=EffectResult.from_json(row[10]) if row[10] else None,
            lease_owner=row[11],
            lease_expires_at=row[12],
            created_at=row[13],
            updated_at=row[14],
            note=row[15],
        )
