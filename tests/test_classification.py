"""Exception classification tests — the review's critical finding #1.

Third-party HTTP timeouts (requests/httpx/urllib3/aiohttp shapes) are NOT
builtin TimeoutError subclasses; misclassifying them as definitive failures
caches-and-replays "maybe happened" as "didn't happen". These tests pin the
MRO-name classification and the end-to-end state outcomes.
"""

from __future__ import annotations

import pytest

from openonce import (
    EffectState,
    EffectUnknown,
    OpenOnce,
    ReplayedFailure,
    classify_exception,
)

# Shapes mirroring real libraries. Classification matches on CLASS NAMES in
# the MRO, so these are built with type() to carry the exact real-world names
# (requests.ReadTimeout is class "ReadTimeout", etc.).
_RequestException = type("RequestException", (OSError,), {})  # requests: IOError base
_RequestsConnectionError = type("ConnectionError", (_RequestException,), {})
_RequestsTimeout = type("Timeout", (_RequestException,), {})
RequestsConnectionError = _RequestsConnectionError
RequestsReadTimeout = type("ReadTimeout", (_RequestsConnectionError, _RequestsTimeout), {})
RequestsConnectTimeout = type("ConnectTimeout", (_RequestsConnectionError, _RequestsTimeout), {})

_HttpxTimeoutException = type("TimeoutException", (Exception,), {})
HttpxReadTimeout = type("ReadTimeout", (_HttpxTimeoutException,), {})
HttpxConnectError = type("ConnectError", (Exception,), {})  # connect phase, nothing sent


class TestClassifyFunction:
    def test_read_timeouts_are_unknown(self) -> None:
        assert classify_exception(RequestsReadTimeout("read timed out")) == "unknown"
        assert classify_exception(HttpxReadTimeout("timed out")) == "unknown"
        assert classify_exception(TimeoutError("builtin")) == "unknown"

    def test_connect_phase_is_retryable_even_with_timeout_in_mro(self) -> None:
        # ConnectTimeout's MRO also contains ConnectionError/Timeout (unknown
        # names) — the retryable check must win.
        assert classify_exception(RequestsConnectTimeout("connect timed out")) == "retryable"
        assert classify_exception(HttpxConnectError("refused")) == "retryable"
        assert classify_exception(ConnectionRefusedError()) == "retryable"

    def test_generic_connection_error_is_unknown_not_retryable(self) -> None:
        # requests.ConnectionError also wraps mid-flight resets — never
        # assume "not sent".
        assert classify_exception(RequestsConnectionError("reset")) == "unknown"
        assert classify_exception(ConnectionResetError()) == "unknown"

    def test_business_errors_are_failed(self) -> None:
        assert classify_exception(ValueError("repo exists")) == "failed"
        assert classify_exception(KeyError("missing")) == "failed"

    def test_subclasses_match_via_mro(self) -> None:
        class MyCustomReadTimeout(RequestsReadTimeout):
            pass

        assert classify_exception(MyCustomReadTimeout()) == "unknown"


class TestEndToEnd:
    def test_requests_style_read_timeout_parks_unknown(self, make_store) -> None:
        """The exact scenario from the review: a requests-style ReadTimeout
        must park UNKNOWN (reconcilable), never FAILED (cached forever)."""
        oo = OpenOnce(make_store())
        calls: list[int] = []

        @oo.effect(tool="pay.charge")
        def charge(amount_cents: int) -> str:
            calls.append(1)
            raise RequestsReadTimeout("Read timed out. (read timeout=30)")

        with oo.scope("run1"):
            with pytest.raises(EffectUnknown):
                charge(amount_cents=500)
            with pytest.raises(EffectUnknown):  # duplicate never re-executes
                charge(amount_cents=500)
        assert len(calls) == 1
        assert oo.pending_review()[0].state == EffectState.UNKNOWN

    def test_connect_error_auto_retries_then_succeeds(self, make_store) -> None:
        oo = OpenOnce(make_store())
        attempts: list[int] = []

        @oo.effect(tool="t", max_attempts=3)
        def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise HttpxConnectError("connection refused")
            return "ok"

        with oo.scope("run1"):
            assert flaky() == "ok"
        assert len(attempts) == 3

    def test_business_error_still_replays(self, make_store) -> None:
        oo = OpenOnce(make_store())

        @oo.effect(tool="t")
        def boom() -> None:
            raise ValueError("repo exists")

        with oo.scope("run1"):
            with pytest.raises(ValueError):
                boom()
            with pytest.raises(ReplayedFailure):
                boom()


class TestCanonicalResult:
    def test_first_call_and_replay_identical_for_tuples(self, make_store) -> None:
        oo = OpenOnce(make_store())

        @oo.effect(tool="t")
        def pair() -> tuple[int, int]:
            return (1, 2)

        with oo.scope("r"):
            first = pair()
            second = pair()
        assert first == second == [1, 2]  # canonicalized for everyone

    def test_non_json_result_warns_and_is_consistent(self, make_store) -> None:
        oo = OpenOnce(make_store())

        class Receipt:
            def __repr__(self) -> str:
                return "Receipt(42)"

        @oo.effect(tool="t")
        def make() -> object:
            return Receipt()

        with oo.scope("r"):
            with pytest.warns(RuntimeWarning, match="non-JSON-serializable"):
                first = make()
            second = make()
        assert first == second == "Receipt(42)"  # same value, both callers
