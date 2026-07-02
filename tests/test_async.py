"""Async handler tests: same semantics, event loop never blocked by the ledger."""

from __future__ import annotations

import asyncio

import pytest

from openonce import (
    ApprovalPending,
    EffectUnknown,
    OpenOnce,
    ReplayedFailure,
    current_effect,
    require_approval_for,
)


class TestAsyncEffects:
    def test_async_handler_executes_once_and_replays(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[str] = []

        @oo.effect(tool="email.send", idempotency_fields=["to"])
        async def send(to: str, body: str) -> str:
            await asyncio.sleep(0)  # prove we're really on the loop
            calls.append(to)
            return f"sent to {to}"

        async def scenario() -> tuple[str, str]:
            with oo.scope("run1"):
                a = await send(to="a@b.c", body="v1")
                b = await send(to="a@b.c", body="v2 reworded")
            return a, b

        a, b = asyncio.run(scenario())
        assert a == b == "sent to a@b.c"
        assert calls == ["a@b.c"]

    def test_current_effect_available_inside_async_handler(self, make_store) -> None:
        oo = OpenOnce(make_store())
        seen: list[str] = []

        @oo.effect(tool="stripe.charge")
        async def charge(amount_cents: int) -> str:
            ctx = current_effect()
            assert ctx is not None
            seen.append(ctx.provider_key)
            return "ok"

        async def scenario() -> None:
            with oo.scope("run1"):
                await charge(amount_cents=500)

        asyncio.run(scenario())
        assert len(seen) == 1 and seen[0].endswith(":stripe.charge")

    def test_async_failure_replay(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[int] = []

        @oo.effect(tool="t")
        async def boom() -> None:
            calls.append(1)
            raise ValueError("already exists")

        async def scenario() -> None:
            with oo.scope("run1"):
                with pytest.raises(ValueError, match="already exists"):
                    await boom()
                with pytest.raises(ReplayedFailure):
                    await boom()

        asyncio.run(scenario())
        assert len(calls) == 1

    def test_async_timeout_parks_unknown(self, make_store) -> None:
        oo = OpenOnce(make_store())

        @oo.effect(tool="pay.charge")
        async def charge() -> str:
            raise TimeoutError("read timed out after send")

        async def scenario() -> None:
            with oo.scope("run1"), pytest.raises(EffectUnknown):
                await charge()

        asyncio.run(scenario())

    def test_async_approval_pending(self, make_store) -> None:
        oo = OpenOnce(make_store(), policy=require_approval_for(["stripe.*"]))
        calls: list[int] = []

        @oo.effect(tool="stripe.refund")
        async def refund(charge: str) -> str:
            calls.append(1)
            return "refunded"

        async def scenario() -> str:
            with oo.scope("run1"):
                with pytest.raises(ApprovalPending) as exc_info:
                    await refund(charge="ch_1")
                oo.approve(exc_info.value.effect_id, by="eric")
                return await refund(charge="ch_1")

        assert asyncio.run(scenario()) == "refunded"
        assert calls == [1]

    def test_loop_stays_responsive_during_effect(self, make_store) -> None:
        """The ledger machinery must not block the event loop: a concurrent
        heartbeat task keeps ticking while an effect executes."""
        oo = OpenOnce(make_store())
        ticks: list[int] = []

        @oo.effect(tool="slow.op")
        async def slow() -> str:
            await asyncio.sleep(0.15)
            return "done"

        async def heartbeat() -> None:
            for i in range(10):
                ticks.append(i)
                await asyncio.sleep(0.02)

        async def scenario() -> str:
            with oo.scope("run1"):
                hb = asyncio.create_task(heartbeat())
                result = await slow()
                await hb
            return result

        assert asyncio.run(scenario()) == "done"
        assert len(ticks) == 10  # the loop kept breathing throughout
