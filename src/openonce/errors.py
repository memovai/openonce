"""OpenOnce exceptions.

The taxonomy encodes the three honest failure classes for external side effects:

- **Failed** — the effect definitively failed (business error). Cached and
  replayed for the same key (a 400 stays a 400).
- **Retryable** — the effect definitively did not happen; safe to execute again.
- **Unknown** — the effect *may* have happened (timeout mid-call, crash between
  STARTED and RECEIPT_RECORDED). Never blindly retried; resolved by
  reconciliation or a human.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .records import EffectRecord


class OpenOnceError(Exception):
    """Base class for all OpenOnce errors."""


class KeyDerivationError(OpenOnceError):
    """Arguments cannot be canonicalized into a stable idempotency key."""


class ScopeRequiredError(OpenOnceError):
    """A derived idempotency key was requested outside any run scope.

    Deduplicating on (tool, args) alone across *all* runs forever is a recipe
    for silently dropped side effects (the AWS EC2 lesson: identical parameters
    may represent different intents). Wrap the call in a scope::

        with oo.scope("run-123"):
            ...

    or pass an explicit ``idempotency_key``.
    """


class IdempotencyMismatch(OpenOnceError):
    """An explicit idempotency key was reused with different arguments.

    Mirrors Stripe's idempotency_error: keys can only be reused with the same
    parameters. This almost always indicates an agent bug (key collision or
    accidental key reuse), so it fails loudly instead of guessing intent.
    """

    def __init__(self, key: str, message: str | None = None) -> None:
        self.key = key
        super().__init__(
            message
            or f"Idempotency key {key!r} was already used with different arguments. "
            f"Reuse a key only for retries of the identical call."
        )


class IllegalTransition(OpenOnceError):
    """A state transition not present in the transition table was attempted."""


class ApprovalPending(OpenOnceError):
    """The effect is parked in REQUIRES_APPROVAL.

    Re-entrant by design: once approved (``oo.approve(effect_id)``), the agent
    retries the same tool call, hits the same idempotency key, and execution
    proceeds — no separate resume path.
    """

    def __init__(self, record: EffectRecord) -> None:
        self.record = record
        self.effect_id = record.effect_id
        super().__init__(
            f"Effect {record.effect_id} ({record.tool}) requires approval. "
            f"Approve with oo.approve({record.effect_id!r}), then retry the call."
        )


class EffectDenied(OpenOnceError):
    """Policy or a human denied this effect."""

    def __init__(self, record: EffectRecord) -> None:
        self.record = record
        super().__init__(f"Effect {record.effect_id} ({record.tool}) was denied.")


class EffectInFlight(OpenOnceError):
    """Another worker holds this effect and it did not finish within the wait
    timeout. First-writer-wins: this caller must not execute a duplicate."""

    def __init__(self, record: EffectRecord) -> None:
        self.record = record
        super().__init__(
            f"Effect {record.effect_id} ({record.tool}) is in flight elsewhere "
            f"(state={record.state.value})."
        )


class EffectUnknown(OpenOnceError):
    """The call's outcome is ambiguous — it may or may not have happened.

    Raised when the handler dies with an outcome-ambiguous error (timeouts,
    connection resets after send). The effect is parked in UNKNOWN for the
    reconciler; retrying it blindly is exactly the bug OpenOnce exists to stop.
    """

    def __init__(self, record: EffectRecord, cause: BaseException | None = None) -> None:
        self.record = record
        self.__cause__ = cause
        super().__init__(
            f"Effect {record.effect_id} ({record.tool}) has UNKNOWN outcome; "
            f"parked for reconciliation. Do not retry blindly."
        )


class ReplayedFailure(OpenOnceError):
    """A duplicate call hit a key whose first execution failed; the recorded
    failure is replayed rather than re-executing the effect."""

    def __init__(self, record: EffectRecord, error: str) -> None:
        self.record = record
        self.error = error
        super().__init__(f"Replayed failure for effect {record.effect_id}: {error}")


class RetryableEffectError(OpenOnceError):
    """Raise (or subclass) inside a handler to signal: the effect definitively
    did NOT happen and may be safely re-executed (e.g. connection refused
    before the request was sent)."""


class UnknownOutcomeError(OpenOnceError):
    """Raise inside a handler to signal: outcome is ambiguous (e.g. timeout
    after the request was sent). Parks the effect in UNKNOWN."""
