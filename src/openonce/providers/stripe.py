"""Stripe prober — the tier-1 reference implementation.

Stripe is the easiest provider class (see PLAN.md §4): it supports native
idempotency keys, so OpenOnce gets a *layered* defense:

1. The handler passes ``current_effect().provider_key`` as Stripe's
   ``Idempotency-Key`` and stamps ``metadata={"openonce_effect_id": ...}``
   on the object (use :func:`effect_metadata`).
2. This prober resolves UNKNOWN by **searching for that metadata** — a pure
   read of external truth.
3. Even a wrong NOT_HAPPENED verdict is recoverable: re-execution reuses the
   same provider_key, and Stripe replays the original response for 24h
   instead of double-charging. Tier-1 means probe errors are survivable.

The one real failure mode is Stripe's Search API indexing lag (typically
under a minute, not transactionally guaranteed). Inside that window a miss
means "not indexed yet", not "did not happen" — so this prober answers
INCONCLUSIVE until the effect is older than ``indexing_lag_seconds``.

No hard dependency on the ``stripe`` SDK: the prober takes any
``search_fn(resource, query) -> list[dict]``; ``from_api_key`` builds one
from the SDK lazily.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..records import EffectRecord
from ..runtime import EffectContext
from .base import ProbeOutcome, ProbeResult

METADATA_KEY = "openonce_effect_id"

#: Stripe Search API is eventually consistent; give it this long before a
#: miss is trusted as NOT_HAPPENED. Conservative on purpose.
DEFAULT_INDEXING_LAG_SECONDS = 120.0

SearchFn = Callable[[str, str], list[dict[str, Any]]]


def effect_metadata(ctx: EffectContext) -> dict[str, str]:
    """Metadata to stamp on the Stripe object at creation time::

    stripe.PaymentIntent.create(
        amount=amount, currency="usd",
        idempotency_key=ctx.provider_key,
        metadata=effect_metadata(ctx),
    )
    """
    return {METADATA_KEY: ctx.effect_id}


class StripeProber:
    """Resolves UNKNOWN Stripe effects by metadata search.

    ``resource`` is the Search API resource to query: ``"payment_intents"``,
    ``"charges"``, ``"invoices"``... one prober instance per tool/resource.
    """

    def __init__(
        self,
        search_fn: SearchFn,
        *,
        resource: str = "payment_intents",
        indexing_lag_seconds: float = DEFAULT_INDEXING_LAG_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._search = search_fn
        self.resource = resource
        self.indexing_lag_seconds = indexing_lag_seconds
        self._clock = clock

    @classmethod
    def from_api_key(cls, api_key: str, **kwargs: Any) -> StripeProber:
        """Build from the official SDK (requires ``pip install stripe``)."""
        try:
            import stripe  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "StripeProber.from_api_key requires the stripe SDK: pip install stripe"
            ) from exc

        client = stripe.StripeClient(api_key)

        def search_fn(resource: str, query: str) -> list[dict[str, Any]]:
            svc = getattr(client, resource)  # client.payment_intents, ...
            return [dict(obj) for obj in svc.search(params={"query": query}).data]

        return cls(search_fn, **kwargs)

    # ------------------------------------------------------------------ #

    def probe(self, record: EffectRecord) -> ProbeResult:
        query = f'metadata["{METADATA_KEY}"]:"{record.effect_id}"'
        hits = self._search(self.resource, query)

        if hits:
            first = hits[0]
            receipt = {
                "stripe_id": first.get("id"),
                "status": first.get("status"),
                "amount": first.get("amount"),
                "resource": self.resource,
            }
            if len(hits) > 1:
                # Same effect_id on multiple objects should be impossible
                # (provider_key dedup) — if it happens, a human must look.
                return ProbeResult(
                    ProbeOutcome.INCONCLUSIVE,
                    receipt=receipt,
                    detail=f"{len(hits)} objects share {record.effect_id}; expected 1",
                )
            return ProbeResult(ProbeOutcome.HAPPENED, receipt=receipt, detail="metadata match")

        age = self._clock() - record.created_at
        if age < self.indexing_lag_seconds:
            return ProbeResult(
                ProbeOutcome.INCONCLUSIVE,
                detail=(
                    f"no match, but effect is {age:.0f}s old — inside Stripe search "
                    f"indexing lag ({self.indexing_lag_seconds:.0f}s); retry later"
                ),
            )
        return ProbeResult(
            ProbeOutcome.NOT_HAPPENED,
            detail=(
                "no object carries this effect_id past the indexing window; "
                "re-execution is additionally safe because Stripe dedupes on "
                "the same provider_key for 24h"
            ),
        )
