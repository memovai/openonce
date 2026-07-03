"""Execution-semantics tests: dedup, replay (incl. failures), approval,
UNKNOWN parking, retryable re-execution, concurrency, audit trail.
Every test runs against both stores via the make_store fixture.
"""

from __future__ import annotations

import threading

import pytest

from openonce import (
    ApprovalPending,
    EffectDenied,
    EffectState,
    EffectUnknown,
    IdempotencyMismatch,
    OpenOnce,
    ReplayedFailure,
    RetryableEffectError,
    ScopeRequiredError,
    UnknownOutcomeError,
    current_effect,
    require_approval_for,
)


def make_oo(make_store, clock=None, **kw) -> OpenOnce:
    import time

    clk = clock or time.time
    return OpenOnce(make_store(clk), clock=clk, **kw)


class TestDedupAndReplay:
    def test_executes_once_and_replays(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="email.send", idempotency_fields=["to", "subject"])
        def send(to: str, subject: str, body: str) -> str:
            calls.append(1)
            return f"sent-{len(calls)}"

        with oo.scope("run1"):
            first = send(to="a@b.c", subject="hi", body="v1")
            second = send(to="a@b.c", subject="hi", body="v2 reworded by the LLM")

        assert first == second == "sent-1"
        assert len(calls) == 1  # noise field changed, effect did not re-fire

    def test_different_scope_reexecutes(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="t")
        def do(x: int) -> int:
            calls.append(x)
            return x

        with oo.scope("run1"):
            do(x=1)
        with oo.scope("run2"):
            do(x=1)
        assert len(calls) == 2

    def test_failure_is_cached_and_replayed(self, make_store) -> None:
        """Stripe semantics: a definitive failure replays, it does not re-execute."""
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="t")
        def boom() -> None:
            calls.append(1)
            raise ValueError("repo already exists")

        with oo.scope("run1"):
            with pytest.raises(ValueError, match="repo already exists"):
                boom()
            with pytest.raises(ReplayedFailure, match="repo already exists"):
                boom()
        assert len(calls) == 1

    def test_explicit_key_mismatch_rejected(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do(x: int) -> int:
            return x

        assert do(x=1, idempotency_key="k1") == 1
        with pytest.raises(IdempotencyMismatch):
            do(x=2, idempotency_key="k1")

    def test_scope_required_for_derived_keys(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do(x: int) -> int:
            return x

        with pytest.raises(ScopeRequiredError, match=r"oo\.scope"):
            do(x=1)
        assert do(x=1, idempotency_key="k") == 1  # explicit key needs no scope

    def test_positional_args_rejected(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do(x: int) -> int:
            return x

        with oo.scope("r"), pytest.raises(TypeError, match="keywords"):
            do(1)


class TestApproval:
    def test_approval_flow_is_reentrant(self, make_store) -> None:
        oo = make_oo(make_store, policy=require_approval_for(["stripe.*"]))
        calls: list[int] = []

        @oo.effect(tool="stripe.refund")
        def refund(charge: str, amount_cents: int) -> str:
            calls.append(1)
            return "refunded"

        with oo.scope("run1"):
            with pytest.raises(ApprovalPending) as exc_info:
                refund(charge="ch_1", amount_cents=500)
            assert calls == []

            oo.approve(exc_info.value.effect_id, by="eric")
            # The agent simply retries the same call — same key, picks up APPROVED.
            assert refund(charge="ch_1", amount_cents=500) == "refunded"
            assert len(calls) == 1
            # And a further duplicate replays.
            assert refund(charge="ch_1", amount_cents=500) == "refunded"
            assert len(calls) == 1

    def test_denied(self, make_store) -> None:
        oo = make_oo(make_store, policy=require_approval_for(["stripe.*"]))

        @oo.effect(tool="stripe.refund")
        def refund(charge: str) -> str:
            return "refunded"

        with oo.scope("run1"):
            with pytest.raises(ApprovalPending) as exc_info:
                refund(charge="ch_1")
            oo.deny(exc_info.value.effect_id, reason="wrong customer")
            with pytest.raises(EffectDenied):
                refund(charge="ch_1")

    def test_pending_review_lists_the_queue(self, make_store) -> None:
        oo = make_oo(make_store, policy=require_approval_for(["stripe.*"]))

        @oo.effect(tool="stripe.refund")
        def refund(charge: str) -> str:
            return "ok"

        with oo.scope("run1"), pytest.raises(ApprovalPending):
            refund(charge="ch_1")
        queue = oo.pending_review()
        assert len(queue) == 1
        assert queue[0].state == EffectState.REQUIRES_APPROVAL


class TestUnknownOutcome:
    def test_timeout_parks_as_unknown_and_never_blind_retries(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="pay.charge")
        def charge(amount_cents: int) -> str:
            calls.append(1)
            raise TimeoutError("read timed out after send")

        with oo.scope("run1"):
            with pytest.raises(EffectUnknown):
                charge(amount_cents=500)
            # Duplicate call must NOT re-execute a maybe-happened charge.
            with pytest.raises(EffectUnknown):
                charge(amount_cents=500)
        assert len(calls) == 1
        rec = oo.pending_review()[0]
        assert rec.state == EffectState.UNKNOWN

    def test_explicit_unknown_outcome_error(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do() -> None:
            raise UnknownOutcomeError("socket closed mid-response")

        with oo.scope("r"), pytest.raises(EffectUnknown):
            do()

    def test_retryable_error_reexecutes_up_to_max(self, make_store) -> None:
        oo = make_oo(make_store)
        attempts: list[int] = []

        @oo.effect(tool="t", max_attempts=3)
        def flaky() -> str:
            ctx = current_effect()
            assert ctx is not None
            attempts.append(ctx.attempt)
            if len(attempts) < 3:
                raise RetryableEffectError("connection refused — nothing sent")
            return "ok"

        with oo.scope("r"):
            assert flaky() == "ok"
        assert attempts == [1, 2, 3]

    def test_retryable_exhaustion_fails_definitively(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="t", max_attempts=2)
        def always_refused() -> str:
            calls.append(1)
            raise RetryableEffectError("connection refused")

        with oo.scope("r"):
            with pytest.raises(RetryableEffectError):
                always_refused()
            with pytest.raises(ReplayedFailure):
                always_refused()
        assert len(calls) == 2


class TestProviderKey:
    def test_provider_key_stable_across_retries(self, make_store) -> None:
        """The key we hand to the provider must not change between attempts —
        it is the provider-side dedup defense."""
        oo = make_oo(make_store)
        seen: list[str] = []

        @oo.effect(tool="stripe.charge", max_attempts=2)
        def charge() -> str:
            ctx = current_effect()
            assert ctx is not None
            seen.append(ctx.provider_key)
            if len(seen) < 2:
                raise RetryableEffectError("refused")
            return "ok"

        with oo.scope("r"):
            charge()
        assert len(seen) == 2
        assert seen[0] == seen[1]


class TestProviderReceiptEnforcement:
    def test_provider_receipt_enforcement_is_opt_in(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            return {"status": "succeeded"}

        with oo.scope("r"):
            assert charge() == {"status": "succeeded"}
        recs = oo.store.scan_states({EffectState.COMMITTED}, updated_before=float("inf"))
        assert len(recs) == 1

    def test_provider_receipt_enforcement_blocks_missing_evidence(self, make_store) -> None:
        oo = make_oo(make_store, enforce_provider_receipts=True)
        calls: list[int] = []

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            calls.append(1)
            return {"status": "succeeded"}

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()

        reviewed = exc_info.value.record
        assert reviewed.state == EffectState.HUMAN_REVIEW
        assert reviewed.result is not None
        assert reviewed.result.value == {"status": "succeeded"}
        assert "handler receipt for 'stripe.charge'" in (reviewed.note or "")
        assert "missing required external evidence field(s): stripe_id" in (reviewed.note or "")
        assert [entry.to_state for entry in oo.journal(reviewed.effect_id)] == [
            EffectState.PLANNED,
            EffectState.APPROVED,
            EffectState.STARTED,
            EffectState.RECEIPT_RECORDED,
            EffectState.HUMAN_REVIEW,
        ]

        with oo.scope("r"), pytest.raises(EffectUnknown):
            charge()
        assert calls == [1]

    def test_provider_receipt_enforcement_blocks_source_mismatch(self, make_store) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            provider_capabilities={"github.create_pr": "github_pr"},
        )

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
        def create_pr(owner: str, repo: str, head: str) -> dict[str, object]:
            return {"number": 42, "head": "other-branch"}

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            create_pr(owner="acme", repo="api", head="fix-login")

        reviewed = exc_info.value.record
        assert reviewed.state == EffectState.HUMAN_REVIEW
        assert "github_pr.head expected 'fix-login'" in (reviewed.note or "")

    def test_provider_receipt_enforcement_allows_matching_source(self, make_store) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            provider_capabilities={"github.create_pr": "github_pr"},
        )

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
        def create_pr(owner: str, repo: str, head: str) -> dict[str, object]:
            return {"number": 42, "head": head}

        with oo.scope("r"):
            assert create_pr(owner="acme", repo="api", head="fix-login") == {
                "number": 42,
                "head": "fix-login",
            }

    def test_provider_receipt_enforcement_preflights_required_idempotency_fields(
        self, make_store
    ) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            provider_capabilities={"github.create_pr": "github_pr"},
        )
        calls: list[str] = []

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])
        def create_pr(owner: str, repo: str, head: str) -> dict[str, object]:
            calls.append(head)
            return {"number": 42, "head": head}

        with (
            oo.scope("r"),
            pytest.raises(
                ValueError,
                match="provider capability 'github_pr' requires idempotency_fields: head",
            ),
        ):
            create_pr(owner="acme", repo="api", head="fix-login")

        assert calls == []
        assert oo.pending_review() == []

    def test_provider_receipt_enforcement_rejects_explicit_key_for_natural_key_contract(
        self, make_store
    ) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            provider_capabilities={"github.create_pr": "github_pr"},
        )
        calls: list[str] = []

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
        def create_pr(owner: str, repo: str, head: str) -> dict[str, object]:
            calls.append(head)
            return {"number": 42, "head": head}

        with pytest.raises(
            ValueError,
            match=(
                "provider capability 'github_pr' requires derived idempotency_fields "
                "instead of an explicit idempotency_key: owner, repo, head"
            ),
        ):
            create_pr(
                owner="acme",
                repo="api",
                head="fix-login",
                idempotency_key="opaque-user-key",
            )

        assert calls == []
        assert oo.pending_review() == []

    def test_provider_receipt_enforcement_preflights_required_handler_args(
        self, make_store
    ) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            provider_capabilities={"github.create_pr": "github_pr"},
        )
        calls: list[int] = []

        @oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
        def create_pr(owner: str, repo: str) -> dict[str, object]:
            calls.append(1)
            return {"number": 42, "head": f"{owner}/{repo}"}

        with (
            oo.scope("r"),
            pytest.raises(
                ValueError,
                match="provider capability 'github_pr' requires handler args: head",
            ),
        ):
            create_pr(owner="acme", repo="api")

        assert calls == []
        assert oo.pending_review() == []

    def test_provider_receipt_enforcement_allows_all_args_idempotency_material(
        self, make_store
    ) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            provider_capabilities={"github.create_pr": "github_pr"},
        )

        @oo.effect(tool="github.create_pr")
        def create_pr(owner: str, repo: str, head: str) -> dict[str, object]:
            return {"number": 42, "head": head}

        with oo.scope("r"):
            assert create_pr(owner="acme", repo="api", head="fix-login") == {
                "number": 42,
                "head": "fix-login",
            }

    def test_invalid_provider_capability_pin_fails_fast(self, make_store) -> None:
        with pytest.raises(
            ValueError,
            match=r"No provider capability 'stripe' matches tool 'github\.create_pr'",
        ):
            make_oo(
                make_store,
                enforce_provider_receipts=True,
                provider_capabilities={"github.create_pr": "stripe"},
            )

    def test_provider_receipt_enforcement_can_require_known_tools(self, make_store) -> None:
        oo = make_oo(
            make_store,
            enforce_provider_receipts=True,
            require_provider_capability_for_receipts=True,
        )
        calls: list[int] = []

        @oo.effect(tool="custom.side_effect")
        def do() -> dict[str, str]:
            calls.append(1)
            return {"id": "x_1"}

        with (
            oo.scope("r"),
            pytest.raises(
                ValueError,
                match=r"no provider capability matches 'custom\.side_effect'",
            ),
        ):
            do()

        assert calls == []
        assert oo.pending_review() == []


class TestEnforcementNeverBlocksTheLedger:
    """Review finding: preflight gates must only apply to NEW intents.
    An effect that already exists replays / joins / surfaces its state no
    matter what enforcement is configured today."""

    def test_enabling_receipt_enforcement_does_not_block_replay(self, make_store) -> None:
        store = make_store()
        oo1 = OpenOnce(store)  # enforcement off: commit an effect

        @oo1.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])
        def create_pr(owner: str, repo: str) -> dict:
            return {"number": 1}

        with oo1.scope("r"):
            first = create_pr(owner="acme", repo="api")

        # Enforcement turned on later; this handler violates the github_pr
        # contract (no 'head'), so preflight would fail for NEW intents.
        oo2 = OpenOnce(store, enforce_provider_receipts=True)

        @oo2.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])
        def create_pr_v2(owner: str, repo: str) -> dict:
            raise AssertionError("replay must not re-execute")

        with oo2.scope("r"):
            assert create_pr_v2(owner="acme", repo="api") == first  # replayed
        # ...while a genuinely NEW intent IS gated by the same preflight:
        with oo2.scope("r"), pytest.raises(ValueError, match="preflight"):
            create_pr_v2(owner="acme", repo="other")

    def test_enforcement_does_not_block_failure_replay(self, make_store) -> None:
        """A 400 stays a 400 — even after enforcement is enabled."""
        store = make_store()
        oo1 = OpenOnce(store)

        @oo1.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])
        def create_pr(owner: str, repo: str) -> dict:
            raise ValueError("repo archived")

        with oo1.scope("r"), pytest.raises(ValueError, match="repo archived"):
            create_pr(owner="acme", repo="api")

        oo2 = OpenOnce(store, enforce_provider_receipts=True)

        @oo2.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])
        def create_pr_v2(owner: str, repo: str) -> dict:
            raise AssertionError("must not re-execute")

        with oo2.scope("r"), pytest.raises(ReplayedFailure, match="repo archived"):
            create_pr_v2(owner="acme", repo="api")


class TestHumanResolution:
    def test_approve_rearms_human_review(self, make_store) -> None:
        """approve() is the generic human resolution: HUMAN_REVIEW -> APPROVED
        re-arms the effect (baseline behavior; resolve_happened/not_happened
        are richer evidence-carrying alternatives, not replacements)."""
        oo = make_oo(make_store, enforce_provider_receipts=True)

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            return {"status": "succeeded"}

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()

        rec = oo.approve(exc_info.value.record.effect_id, by="eric")
        assert rec.state == EffectState.APPROVED

    def test_resolve_happened_commits_unknown_with_manual_receipt(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []

        @oo.effect(tool="pay.charge")
        def charge() -> dict[str, str]:
            calls.append(1)
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        committed = oo.resolve_happened(
            eid,
            receipt={"charge_id": "ch_1"},
            by="eric",
            reason="matched in provider dashboard",
        )

        assert committed.state == EffectState.COMMITTED
        assert committed.result is not None
        assert committed.result.ok is True
        assert committed.result.value == {"charge_id": "ch_1"}
        assert committed.note == "matched in provider dashboard"
        assert [entry.to_state for entry in oo.journal(eid)] == [
            EffectState.PLANNED,
            EffectState.APPROVED,
            EffectState.STARTED,
            EffectState.UNKNOWN,
            EffectState.RECEIPT_RECORDED,
            EffectState.COMMITTED,
        ]

        with oo.scope("r"):
            assert charge() == {"charge_id": "ch_1"}
        assert calls == [1]

    def test_resolve_happened_rejects_empty_receipt(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="pay.charge")
        def charge() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()

        with pytest.raises(ValueError, match="receipt must be a non-empty object"):
            oo.resolve_happened(exc_info.value.record.effect_id, receipt={})

    def test_resolve_happened_enforces_provider_receipt_contract_when_enabled(
        self, make_store
    ) -> None:
        oo = make_oo(make_store, enforce_provider_receipts=True)

        @oo.effect(tool="stripe.charge")
        def charge() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        with pytest.raises(
            ValueError,
            match=r"missing required external evidence field\(s\): stripe_id",
        ):
            oo.resolve_happened(eid, receipt={"status": "succeeded"})
        assert oo.get(eid).state == EffectState.UNKNOWN

        committed = oo.resolve_happened(eid, receipt={"stripe_id": "pi_1"})

        assert committed.state == EffectState.COMMITTED
        assert committed.result is not None
        assert committed.result.value == {"stripe_id": "pi_1"}

    def test_resolve_happened_can_require_a_matching_provider_capability(self, make_store) -> None:
        oo = make_oo(make_store, enforce_provider_receipts=True)

        @oo.effect(tool="custom.side_effect")
        def do() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            do()

        with pytest.raises(
            ValueError,
            match=r"no provider capability matches 'custom\.side_effect'",
        ):
            oo.resolve_happened(
                exc_info.value.record.effect_id,
                receipt={"id": "x_1"},
                require_provider_capability=True,
            )

    def test_resolve_happened_inherits_provider_capability_requirement(self, make_store) -> None:
        store = make_store()
        loose = OpenOnce(store)
        strict = OpenOnce(
            store,
            enforce_provider_receipts=True,
            require_provider_capability_for_receipts=True,
        )

        @loose.effect(tool="custom.side_effect")
        def do() -> None:
            raise TimeoutError("read timed out after send")

        with loose.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            do()

        with pytest.raises(
            ValueError,
            match=r"no provider capability matches 'custom\.side_effect'",
        ):
            strict.resolve_happened(exc_info.value.record.effect_id, receipt={"id": "x_1"})

    def test_resolve_not_happened_rearms_and_clears_bad_receipt(self, make_store) -> None:
        oo = make_oo(make_store, enforce_provider_receipts=True)
        mode = {"receipt": {"status": "succeeded"}}

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            return mode["receipt"]

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id
        reviewed = oo.get(eid)
        assert reviewed.result is not None
        assert reviewed.result.value == {"status": "succeeded"}

        rearmed = oo.resolve_not_happened(eid, by="eric", reason="not found in Stripe")

        assert rearmed.state == EffectState.APPROVED
        assert rearmed.result is None
        assert rearmed.note == "not found in Stripe"

        mode["receipt"] = {"stripe_id": "pi_1"}
        with oo.scope("r"):
            assert charge() == {"stripe_id": "pi_1"}

    def test_resolve_not_happened_requires_a_reason(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="pay.charge")
        def charge() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        with pytest.raises(ValueError, match="reason is required"):
            oo.resolve_not_happened(eid, reason="   ")
        assert oo.get(eid).state == EffectState.UNKNOWN

    def test_resolve_not_happened_can_require_auto_rearm_capability(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="email.send")
        def send() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            send()
        eid = exc_info.value.record.effect_id

        with pytest.raises(ValueError, match="not safe to auto-rearm on a miss"):
            oo.resolve_not_happened(
                eid,
                reason="not found in authoritative sent store",
                require_auto_rearm=True,
            )
        assert oo.get(eid).state == EffectState.UNKNOWN

    def test_resolve_not_happened_require_auto_rearm_blocks_unknown_tools(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="custom.side_effect")
        def do() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            do()

        with pytest.raises(
            ValueError,
            match=r"no provider capability matches 'custom\.side_effect'",
        ):
            oo.resolve_not_happened(
                exc_info.value.record.effect_id,
                reason="not found in provider",
                require_auto_rearm=True,
            )
        assert oo.get(exc_info.value.record.effect_id).state == EffectState.UNKNOWN

    def test_resolve_not_happened_auto_rearm_gate_allows_pinned_capability(
        self, make_store
    ) -> None:
        oo = make_oo(
            make_store,
            provider_capabilities={"email.send": "email_provider_api"},
        )

        @oo.effect(tool="email.send")
        def send() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            send()

        rearmed = oo.resolve_not_happened(
            exc_info.value.record.effect_id,
            reason="not found in authoritative sent store",
            require_auto_rearm=True,
            require_provider_capability=True,
        )

        assert rearmed.state == EffectState.APPROVED

    def test_resolve_not_happened_fails_when_attempts_are_exhausted(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="pay.charge", max_attempts=1)
        def charge() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("r"), pytest.raises(EffectUnknown) as exc_info:
            charge()

        failed = oo.resolve_not_happened(
            exc_info.value.record.effect_id,
            reason="not found in provider",
        )

        assert failed.state == EffectState.FAILED
        assert failed.result is not None
        assert failed.result.ok is False
        assert failed.result.error_type == "AttemptsExhausted"
        assert failed.note == "not found in provider"


class TestConcurrency:
    def test_first_writer_wins_under_threads(self, make_store) -> None:
        oo = make_oo(make_store)
        calls: list[int] = []
        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        @oo.effect(tool="email.send")
        def send(to: str) -> str:
            calls.append(1)
            return "sent"

        def worker() -> None:
            barrier.wait()
            try:
                with oo.scope("run1"):
                    results.append(send(to="a@b.c"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert len(calls) == 1  # exactly one execution
        assert results == ["sent"] * 8  # everyone got the same receipt


class TestAuditTrail:
    def test_journal_records_full_lifecycle(self, make_store) -> None:
        oo = make_oo(make_store)

        @oo.effect(tool="t")
        def do() -> str:
            return "ok"

        with oo.scope("r"):
            do()
        # Find the record via the store scan (no state filter API needed here).
        es = EffectState
        recs = oo.store.scan_states({es.COMMITTED}, updated_before=float("inf"))
        assert len(recs) == 1
        states = [e.to_state for e in oo.journal(recs[0].effect_id)]
        assert states == [es.PLANNED, es.APPROVED, es.STARTED, es.RECEIPT_RECORDED, es.COMMITTED]
