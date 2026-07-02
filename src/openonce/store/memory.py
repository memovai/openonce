"""In-memory reference store: the executable specification of the protocol.

Single-process only. Useful for tests and for reading — the SQLite/Postgres
stores implement exactly these semantics with real durability.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..records import EffectRecord, JournalEntry
from ..state import EffectState, can_transition


class InMemoryStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._by_id: dict[str, EffectRecord] = {}
        self._by_key: dict[str, str] = {}
        self._journal: list[JournalEntry] = []
        self._seq = 0

    def create_or_get(self, record: EffectRecord) -> tuple[EffectRecord, bool]:
        with self._lock:
            existing_id = self._by_key.get(record.idempotency_key)
            if existing_id is not None:
                return self._by_id[existing_id], False
            now = self._clock()
            record = record.with_(created_at=now, updated_at=now)
            self._by_id[record.effect_id] = record
            self._by_key[record.idempotency_key] = record.effect_id
            self._append_journal(record.effect_id, None, record.state, {})
            return record, True

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
        with self._lock:
            rec = self._by_id.get(effect_id)
            if rec is None or rec.state not in from_states:
                return None
            if not can_transition(rec.state, to_state):
                return None
            now = self._clock()
            if require_lease_owner is not None:
                lease_live = rec.lease_expires_at is not None and rec.lease_expires_at > now
                if lease_live and rec.lease_owner != require_lease_owner:
                    return None
            changes: dict[str, object] = dict(set_fields or {})
            if lease_owner is not None:
                changes["lease_owner"] = lease_owner
            from_state = rec.state
            rec = rec.with_(state=to_state, updated_at=now, **changes)
            self._by_id[effect_id] = rec
            self._append_journal(effect_id, from_state, to_state, payload or {})
            return rec

    def get(self, effect_id: str) -> EffectRecord | None:
        with self._lock:
            return self._by_id.get(effect_id)

    def get_by_key(self, idempotency_key: str) -> EffectRecord | None:
        with self._lock:
            eid = self._by_key.get(idempotency_key)
            return self._by_id[eid] if eid else None

    def journal(self, effect_id: str) -> list[JournalEntry]:
        with self._lock:
            return [e for e in self._journal if e.effect_id == effect_id]

    def scan_states(
        self, states: frozenset[EffectState] | set[EffectState], *, updated_before: float
    ) -> list[EffectRecord]:
        with self._lock:
            return [
                r
                for r in self._by_id.values()
                if r.state in states and r.updated_at <= updated_before
            ]

    def _append_journal(
        self,
        effect_id: str,
        from_state: EffectState | None,
        to_state: EffectState,
        payload: dict[str, object],
    ) -> None:
        self._seq += 1
        self._journal.append(
            JournalEntry(
                seq=self._seq,
                effect_id=effect_id,
                from_state=from_state,
                to_state=to_state,
                at=self._clock(),
                payload=payload,
            )
        )
