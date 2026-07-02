"""Ledger data model: the materialized effect row + the append-only journal.

The journal is the source of truth for *our* view (Temporal's Event History,
scaled down); the ``effects`` row is a materialized projection for fast lookup.
Neither is the source of truth for the *world* — the external provider is.
That asymmetry is why RECEIPT_RECORDED and reconciliation exist.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from .state import EffectState


def new_effect_id() -> str:
    return f"eff_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class EffectResult:
    """Cached outcome of the first execution — replayed for duplicate keys.

    Failures are cached too (Stripe semantics): the same key returns the same
    error instead of re-executing.
    """

    ok: bool
    value: Any = None
    error: str | None = None
    error_type: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "value": self.value, "error": self.error, "error_type": self.error_type}
        )

    @classmethod
    def from_json(cls, raw: str) -> EffectResult:
        d = json.loads(raw)
        return cls(ok=d["ok"], value=d["value"], error=d["error"], error_type=d["error_type"])


@dataclass(frozen=True)
class EffectRecord:
    """Materialized state of one effect (one intended external side effect)."""

    effect_id: str
    idempotency_key: str
    tool: str
    state: EffectState
    args_fingerprint: str
    #: Full args, stored for audit and for the reconciler's probe.
    args_json: str
    scope: str
    #: The key passed *through* to the provider — the only hard defense against
    #: duplicates when we crash after the request left the building.
    provider_key: str
    attempt: int = 0
    max_attempts: int = 3
    result: EffectResult | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    #: Free-form annotation (approval reason, probe evidence, review notes).
    note: str | None = None

    def args(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.args_json)
        return loaded

    def with_(self, **changes: Any) -> EffectRecord:
        return replace(self, **changes)


@dataclass(frozen=True)
class JournalEntry:
    """One immutable state transition. Append-only; never updated or deleted."""

    seq: int
    effect_id: str
    from_state: EffectState | None
    to_state: EffectState
    at: float
    payload: dict[str, Any] = field(default_factory=dict)
