from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from openonce.store.memory import InMemoryStore
from openonce.store.sqlite import SQLiteStore


class FakeClock:
    """Deterministic, manually-advanced clock for grace/lease tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=["memory", "sqlite"])
def make_store(request: pytest.FixtureRequest, tmp_path) -> Callable[..., object]:
    """Factory building either store, so every semantic test runs on both."""

    def factory(clk: Callable[[], float] = time.time):
        if request.param == "memory":
            return InMemoryStore(clock=clk)
        return SQLiteStore(str(tmp_path / "oo.db"), clock=clk)

    return factory
