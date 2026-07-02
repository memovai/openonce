"""OpenOnce — durable side effects for AI agent tool calls.

Honest guarantee: **at-least-once execution + idempotency + reconciliation**.
Exactly-once side effects against external systems are impossible from a local
process; what OpenOnce gives you is: duplicates get replayed instead of
re-executed, ambiguous outcomes are reconciled against the external world
instead of blindly retried, and every effect has an auditable receipt trail.
"""

from .client import OpenOnce, current_effect
from .errors import (
    ApprovalPending,
    EffectDenied,
    EffectInFlight,
    EffectUnknown,
    IdempotencyMismatch,
    KeyDerivationError,
    OpenOnceError,
    ReplayedFailure,
    RetryableEffectError,
    ScopeRequiredError,
    UnknownOutcomeError,
)
from .policy import Decision, Verdict, allow_all, require_approval_for
from .providers.base import ProbeOutcome, Prober, ProbeResult
from .reconciler import Reconciler, ReconcileReport
from .records import EffectRecord, EffectResult, JournalEntry
from .runtime import (
    DEFAULT_RETRYABLE_EXCEPTION_NAMES,
    DEFAULT_UNKNOWN_EXCEPTION_NAMES,
    classify_exception,
)
from .state import EffectState
from .store import InMemoryStore, SQLiteStore, Store

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_RETRYABLE_EXCEPTION_NAMES",
    "DEFAULT_UNKNOWN_EXCEPTION_NAMES",
    "ApprovalPending",
    "Decision",
    "EffectDenied",
    "EffectInFlight",
    "EffectRecord",
    "EffectResult",
    "EffectState",
    "EffectUnknown",
    "IdempotencyMismatch",
    "InMemoryStore",
    "JournalEntry",
    "KeyDerivationError",
    "OpenOnce",
    "OpenOnceError",
    "ProbeOutcome",
    "ProbeResult",
    "Prober",
    "ReconcileReport",
    "Reconciler",
    "ReplayedFailure",
    "RetryableEffectError",
    "SQLiteStore",
    "ScopeRequiredError",
    "Store",
    "UnknownOutcomeError",
    "Verdict",
    "allow_all",
    "classify_exception",
    "current_effect",
    "require_approval_for",
]
