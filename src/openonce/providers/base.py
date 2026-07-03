"""Prober protocol: how UNKNOWN outcomes get resolved against reality.

The ledger is not the source of truth — the external world is. A prober
answers one question for one tool: *did this effect actually happen?* by
reading external state (search by provider_key, natural business key, list
recent objects, read a settlement feed...).

This package is where OpenOnce's real accumulated value lives: per-provider
knowledge of how to ask that question reliably. Three tiers of provider
support (see PLAN.md §4):

1. Native idempotency keys (Stripe, ...): pass provider_key through; probe by key.
2. Natural business keys (GitHub PR (owner, repo, head), email Message-ID):
   probe-before-write / search by the business key.
3. No idempotency support at all: fuzzy matching only — which is why
   INCONCLUSIVE exists and routes to a human instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..records import EffectRecord


class ProbeOutcome(StrEnum):
    #: The effect verifiably happened; ``receipt`` carries the evidence.
    HAPPENED = "happened"
    #: The effect verifiably did not happen; safe to re-execute.
    NOT_HAPPENED = "not_happened"
    #: Cannot tell. Routes to HUMAN_REVIEW — never to a blind retry.
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ProbeResult:
    outcome: ProbeOutcome
    #: External evidence (provider object id, URL, matched receipt fields...).
    receipt: dict[str, Any] | None = None
    detail: str = ""


class Prober(Protocol):
    def probe(self, record: EffectRecord) -> ProbeResult:
        """Check external state for the effect described by ``record``.

        Implementations should prefer, in order: lookup by provider_key,
        lookup by natural business key, then conservative fuzzy matching.
        When in doubt, return INCONCLUSIVE — a human reviewing a receipt is
        cheap; a duplicated payment is not.
        """
        ...
