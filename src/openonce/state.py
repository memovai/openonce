"""Effect lifecycle state machine.

An explicit transition table (not an implicit status column) is the load-bearing
design here: every state move is validated, journaled, and crash-recoverable.
Modeled on Temporal's Activity lifecycle, reduced to what a side-effect layer needs.

Key semantic: UNKNOWN is a first-class state. A crash between STARTED and
RECEIPT_RECORDED means the side effect *may or may not have happened* — the only
honest answer is to reconcile against the external world, never to blindly retry.
"""

from __future__ import annotations

from enum import StrEnum


class EffectState(StrEnum):
    PLANNED = "planned"
    REQUIRES_APPROVAL = "requires_approval"
    APPROVED = "approved"
    DENIED = "denied"
    STARTED = "started"
    RECEIPT_RECORDED = "receipt_recorded"
    COMMITTED = "committed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    HUMAN_REVIEW = "human_review"
    CANCELED = "canceled"


S = EffectState

#: Legal transitions. Anything not listed here raises IllegalTransition.
TRANSITIONS: dict[EffectState, frozenset[EffectState]] = {
    S.PLANNED: frozenset({S.REQUIRES_APPROVAL, S.APPROVED, S.DENIED, S.CANCELED}),
    S.REQUIRES_APPROVAL: frozenset({S.APPROVED, S.DENIED, S.CANCELED}),
    S.APPROVED: frozenset({S.STARTED, S.CANCELED}),
    S.STARTED: frozenset({S.RECEIPT_RECORDED, S.FAILED, S.UNKNOWN}),
    S.RECEIPT_RECORDED: frozenset({S.COMMITTED, S.HUMAN_REVIEW}),
    # UNKNOWN resolves only via reconciliation (probe) or a human:
    #   probe says "it happened"      -> RECEIPT_RECORDED
    #   probe says "it did not happen"-> APPROVED (re-eligible for execution)
    #   probe inconclusive / absent   -> HUMAN_REVIEW
    S.UNKNOWN: frozenset({S.RECEIPT_RECORDED, S.APPROVED, S.FAILED, S.HUMAN_REVIEW}),
    # A human resolving a review can commit, allow retry, fail, or cancel.
    S.HUMAN_REVIEW: frozenset({S.COMMITTED, S.APPROVED, S.FAILED, S.CANCELED}),
    # Explicit operator retry of a failed effect re-arms it.
    S.FAILED: frozenset({S.APPROVED}),
    S.COMMITTED: frozenset(),
    S.DENIED: frozenset(),
    S.CANCELED: frozenset(),
}

#: States with a cached result that is replayed to duplicate callers.
#: Failures replay too (Stripe semantics): a 400 stays a 400 for the same key.
REPLAYABLE: frozenset[EffectState] = frozenset({S.COMMITTED, S.FAILED})

#: States from which no further automatic progress happens.
TERMINAL: frozenset[EffectState] = frozenset({S.COMMITTED, S.FAILED, S.DENIED, S.CANCELED})

#: States the reconciler is responsible for driving forward.
RECONCILABLE: frozenset[EffectState] = frozenset({S.STARTED, S.UNKNOWN})


def can_transition(from_state: EffectState, to_state: EffectState) -> bool:
    return to_state in TRANSITIONS[from_state]
