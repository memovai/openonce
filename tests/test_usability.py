"""Usability surface: bare decorator, positional args, auto scope, direct wrap.

These pin the low-friction spellings — the Temporal lesson: decorate normal
code, no ceremony. All key-derivation semantics stay identical underneath.
"""

from __future__ import annotations

import asyncio

import pytest

from openonce import EffectState, OpenOnce, ScopeRequiredError


class TestBareDecorator:
    def test_bare_decorator_tool_defaults_to_function_name(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[str] = []

        @oo.effect
        def send_email(to: str) -> str:
            calls.append(to)
            return "sent"

        with oo.scope("r"):
            send_email(to="a@b.c")
            send_email(to="a@b.c")
        assert calls == ["a@b.c"]
        rec = oo.store.scan_states({EffectState.COMMITTED}, updated_before=float("inf"))[0]
        assert rec.tool == "send_email"

    def test_empty_parens_also_work(self, make_store) -> None:
        oo = OpenOnce(make_store())

        @oo.effect()
        def do(x: int) -> int:
            return x

        with oo.scope("r"):
            assert do(x=1) == 1

    def test_direct_wrap_of_existing_tool(self, make_store) -> None:
        """oo.effect(fn) — protect an existing callable without decorating."""
        oo = OpenOnce(make_store())
        calls: list[int] = []

        def legacy_tool(x: int) -> int:
            calls.append(x)
            return x * 2

        protected = oo.effect(legacy_tool)
        with oo.scope("r"):
            assert protected(x=3) == 6
            assert protected(x=3) == 6
        assert calls == [3]


class TestPositionalArguments:
    def test_positional_and_keyword_calls_share_a_key(self, make_store) -> None:
        """Names enter the key either way — positional is not a re-execution."""
        oo = OpenOnce(make_store())
        calls: list[str] = []

        @oo.effect
        def send(to: str, body: str) -> str:
            calls.append(to)
            return "sent"

        with oo.scope("r"):
            a = send("a@b.c", "hello")
            b = send(to="a@b.c", body="hello")
            c = send("a@b.c", body="hello")
        assert a == b == c == "sent"
        assert calls == ["a@b.c"]

    def test_bad_arity_raises_a_normal_typeerror(self, make_store) -> None:
        oo = OpenOnce(make_store())

        @oo.effect
        def send(to: str) -> str:
            return "sent"

        with oo.scope("r"), pytest.raises(TypeError, match="send"):
            send("a@b.c", "extra")

    def test_var_kwargs_handler_flattens_into_key_material(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[dict] = []

        @oo.effect
        def flexible(**params: str) -> str:
            calls.append(params)
            return "ok"

        with oo.scope("r"):
            flexible(a="1", b="2")
            flexible(b="2", a="1")  # order-insensitive: same key
        assert calls == [{"a": "1", "b": "2"}]

    def test_star_args_handler_rejected_at_decoration(self, make_store) -> None:
        oo = OpenOnce(make_store())
        with pytest.raises(TypeError, match=r"\*args"):

            @oo.effect
            def bad(*args: int) -> int:
                return 0

    def test_positional_only_rejected_at_decoration(self, make_store) -> None:
        oo = OpenOnce(make_store())
        with pytest.raises(TypeError, match="positional-only"):

            @oo.effect
            def bad(x: int, /) -> int:
                return x

    def test_async_positional_binding(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[str] = []

        @oo.effect
        async def send(to: str, body: str) -> str:
            calls.append(to)
            return "sent"

        async def scenario() -> None:
            with oo.scope("r"):
                assert await send("a@b.c", "hi") == "sent"
                assert await send(to="a@b.c", body="hi") == "sent"

        asyncio.run(scenario())
        assert calls == ["a@b.c"]


class TestAutoScope:
    def test_no_arg_scope_dedupes_within_the_run(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[int] = []

        @oo.effect
        def do(x: int) -> int:
            calls.append(x)
            return x

        with oo.scope() as run_id:
            assert run_id.startswith("auto-")
            do(x=1)
            do(x=1)  # LLM retry inside the run: replayed
        assert calls == [1]

    def test_separate_auto_scopes_are_separate_runs(self, make_store) -> None:
        oo = OpenOnce(make_store())
        calls: list[int] = []

        @oo.effect
        def do(x: int) -> int:
            calls.append(x)
            return x

        with oo.scope():
            do(x=1)
        with oo.scope():
            do(x=1)  # a new run legitimately wants the same call
        assert calls == [1, 1]

    def test_named_scope_yields_its_id(self, make_store) -> None:
        oo = OpenOnce(make_store())
        with oo.scope("run-42") as run_id:
            assert run_id == "run-42"

    def test_no_scope_error_still_loud_and_helpful(self, make_store) -> None:
        oo = OpenOnce(make_store())

        @oo.effect
        def do(x: int) -> int:
            return x

        with pytest.raises(ScopeRequiredError, match=r"oo\.scope"):
            do(x=1)
