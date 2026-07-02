"""Email prober — the tier-2/3 boundary, honestly.

Email's natural key is the RFC 5322 ``Message-ID`` — and unlike most natural
keys, the *sender* controls it. So OpenOnce derives it deterministically from
the effect: :func:`make_message_id` gives the same ID before and after a
crash, which makes it both the dedup key and the probe target.

The handler stamps it on the outgoing message::

    @oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
    def send(to: str, subject: str, body: str) -> str:
        msg["Message-ID"] = make_message_id(openonce.current_effect())
        smtp.send_message(msg)

The tier honesty lives in ``sent_store_is_authoritative``:

- **True** (Gmail/Graph API sends, provider writes the Sent copy atomically
  with the send): a miss past the propagation window means NOT_HAPPENED.
- **False** (bare SMTP, where the Sent copy is a separate client-side APPEND
  that can fail independently of the send): a miss proves nothing — the mail
  may be delivered with no Sent copy. Miss -> INCONCLUSIVE -> human. This is
  tier-3 territory and no amount of cleverness makes a miss authoritative.

Duck-typed ``search_fn(message_id) -> dict | None``; ``from_imap`` builds one
on stdlib imaplib.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..records import EffectRecord
from ..runtime import EffectContext
from .base import ProbeOutcome, ProbeResult

#: A found message is evidence regardless; misses inside this window are
#: never trusted (Sent-copy propagation).
DEFAULT_PROPAGATION_LAG_SECONDS = 120.0

SearchFn = Callable[[str], "dict[str, Any] | None"]


def make_message_id(ctx: EffectContext, domain: str = "openonce.local") -> str:
    """Deterministic Message-ID for this effect — same value on every attempt,
    so provider-side threading dedups and the prober knows what to look for."""
    return f"<{ctx.effect_id}@{domain}>"


def message_id_for_record(record: EffectRecord, domain: str = "openonce.local") -> str:
    """The probe-side twin of :func:`make_message_id`."""
    return f"<{record.effect_id}@{domain}>"


class EmailMessageIdProber:
    def __init__(
        self,
        search_fn: SearchFn,
        *,
        sent_store_is_authoritative: bool = False,
        domain: str = "openonce.local",
        propagation_lag_seconds: float = DEFAULT_PROPAGATION_LAG_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._search = search_fn
        self.authoritative = sent_store_is_authoritative
        self.domain = domain
        self.propagation_lag_seconds = propagation_lag_seconds
        self._clock = clock

    @classmethod
    def from_imap(
        cls,
        host: str,
        user: str,
        password: str,
        *,
        mailbox: str = '"[Gmail]/Sent Mail"',
        **kwargs: Any,
    ) -> EmailMessageIdProber:
        """Build on stdlib imaplib. A fresh connection per probe — probes are
        rare (reconciler-only) and stale IMAP connections are not worth it."""
        import imaplib

        def search_fn(message_id: str) -> dict[str, Any] | None:
            with imaplib.IMAP4_SSL(host) as imap:
                imap.login(user, password)
                imap.select(mailbox, readonly=True)
                status, data = imap.search(None, "HEADER", "Message-ID", message_id)
                if status == "OK" and data and data[0].split():
                    return {"mailbox": mailbox, "uids": data[0].decode()}
                return None

        return cls(search_fn, **kwargs)

    # ------------------------------------------------------------------ #

    def probe(self, record: EffectRecord) -> ProbeResult:
        message_id = message_id_for_record(record, self.domain)
        found = self._search(message_id)

        if found is not None:
            return ProbeResult(
                ProbeOutcome.HAPPENED,
                receipt={"message_id": message_id, **found},
                detail="Message-ID present in the sent store",
            )

        # updated_at, not created_at: see the Stripe prober — approval delays
        # must not eat the propagation window.
        age = self._clock() - record.updated_at
        if age < self.propagation_lag_seconds:
            return ProbeResult(
                ProbeOutcome.INCONCLUSIVE,
                detail=(
                    f"no match, but effect is {age:.0f}s old — inside the sent-store "
                    f"propagation window ({self.propagation_lag_seconds:.0f}s)"
                ),
            )
        if self.authoritative:
            return ProbeResult(
                ProbeOutcome.NOT_HAPPENED,
                detail=(
                    "sent store is written atomically with the send (provider API); "
                    "a miss past the propagation window means the send did not happen"
                ),
            )
        return ProbeResult(
            ProbeOutcome.INCONCLUSIVE,
            detail=(
                "bare-SMTP tier: the Sent copy is a separate client-side write, so a "
                "miss cannot prove the mail was not delivered. A human must decide — "
                "set sent_store_is_authoritative=True only for provider-API sends"
            ),
        )
