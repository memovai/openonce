"""Storage protocol.

Every store must provide two atomic primitives; everything else is bookkeeping:

1. ``create_or_get`` — first-writer-wins insert on the idempotency key's
   UNIQUE constraint. The winner proceeds; losers get the existing record.
2. ``transition`` — compare-and-swap on state (+ lease), appending the journal
   entry in the same transaction. All state movement goes through this, so an
   illegal or raced transition can never be half-applied.

Note the deliberate absence of any "execute" primitive: a UNIQUE constraint is
necessary but NOT sufficient for at-most-once — the runtime composes these
primitives into atomic phases (see runtime.py).
"""

from __future__ import annotations

from typing import Protocol

from ..records import EffectRecord, JournalEntry
from ..state import EffectState


class Store(Protocol):
    def create_or_get(self, record: EffectRecord) -> tuple[EffectRecord, bool]:
        """Insert ``record`` if its idempotency_key is unused.

        Returns ``(record, True)`` if this caller won the insert, otherwise
        ``(existing_record, False)``. Must be atomic across processes.
        """
        ...

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
        """Atomically move the effect to ``to_state`` iff its current state is
        in ``from_states`` (and, if ``require_lease_owner`` is given, the lease
        is held by that owner or expired). Appends a JournalEntry in the same
        transaction. Returns the updated record, or None if the CAS failed.
        """
        ...

    def get(self, effect_id: str) -> EffectRecord | None: ...

    def get_by_key(self, idempotency_key: str) -> EffectRecord | None: ...

    def journal(self, effect_id: str) -> list[JournalEntry]:
        """Full transition history for one effect (audit trail)."""
        ...

    def scan_states(
        self, states: frozenset[EffectState] | set[EffectState], *, updated_before: float
    ) -> list[EffectRecord]:
        """Effects sitting in ``states`` untouched since ``updated_before``.
        Used by the reconciler (grace period) — never by the hot path.
        """
        ...
