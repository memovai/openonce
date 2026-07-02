from __future__ import annotations

import os
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


PG_TEST_DSN = os.environ.get("OPENONCE_TEST_PG_DSN", "host=/tmp dbname=openonce_test")


def _postgres_store(clk: Callable[[], float]):
    psycopg = pytest.importorskip("psycopg")
    from openonce.store.postgres import PostgresStore

    try:
        store = PostgresStore(PG_TEST_DSN, clock=clk)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no test Postgres at {PG_TEST_DSN!r}: {exc}")
    # Isolate tests: the schema persists, the data must not.
    with psycopg.connect(PG_TEST_DSN) as conn:
        conn.execute("TRUNCATE effects, effect_journal")
    return store


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def make_store(request: pytest.FixtureRequest, tmp_path) -> Callable[..., object]:
    """Factory building any store, so every semantic test runs on all of them."""

    def factory(clk: Callable[[], float] = time.time):
        if request.param == "memory":
            return InMemoryStore(clock=clk)
        if request.param == "postgres":
            return _postgres_store(clk)
        return SQLiteStore(str(tmp_path / "oo.db"), clock=clk)

    return factory
