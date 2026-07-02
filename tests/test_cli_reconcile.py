"""CLI reconcile tests: prober loading, once and daemon modes, DSN routing."""

from __future__ import annotations

import textwrap

import pytest

from openonce import EffectState, EffectUnknown, OpenOnce
from openonce.cli import main, open_store
from openonce.store.sqlite import SQLiteStore


@pytest.fixture
def unknown_ledger(tmp_path) -> tuple[str, str]:
    """A ledger with one UNKNOWN pay.charge effect. Returns (db, effect_id)."""
    db = str(tmp_path / "rec.db")
    oo = OpenOnce(SQLiteStore(db))

    @oo.effect(tool="pay.charge")
    def charge(amount_cents: int) -> str:
        raise TimeoutError("ambiguous")

    with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
        charge(amount_cents=500)
    return db, exc_info.value.record.effect_id


@pytest.fixture
def prober_module(tmp_path, monkeypatch) -> str:
    """A loadable module exposing PROBERS that always answers HAPPENED."""
    (tmp_path / "my_probers.py").write_text(
        textwrap.dedent(
            """
            from openonce import ProbeOutcome, ProbeResult

            class AlwaysHappened:
                def probe(self, record):
                    return ProbeResult(ProbeOutcome.HAPPENED, receipt={"id": "x_1"})

            PROBERS = {"pay.charge": AlwaysHappened()}
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "my_probers:PROBERS"


class TestReconcileCommand:
    def test_once_resolves_unknown(self, unknown_ledger, prober_module, capsys) -> None:
        db, eid = unknown_ledger
        code = main(["--db", db, "reconcile", "--probers", prober_module, "--grace", "0"])
        assert code == 0
        assert "committed=1" in capsys.readouterr().out
        assert SQLiteStore(db).get(eid).state == EffectState.COMMITTED

    def test_no_probers_escalates_and_names_the_effect(self, unknown_ledger, capsys) -> None:
        db, eid = unknown_ledger
        assert main(["--db", db, "reconcile", "--grace", "0"]) == 0
        out = capsys.readouterr().out
        assert "escalated=1" in out and eid in out
        assert SQLiteStore(db).get(eid).state == EffectState.HUMAN_REVIEW

    def test_watch_mode_loops(self, unknown_ledger, prober_module, capsys) -> None:
        db, eid = unknown_ledger
        code = main(
            [
                "--db", db, "reconcile", "--probers", prober_module,
                "--grace", "0", "--watch", "--interval", "0", "--max-loops", "3",
            ]
        )  # fmt: skip
        assert code == 0
        assert SQLiteStore(db).get(eid).state == EffectState.COMMITTED

    def test_bad_probers_spec_fails_loudly(self, unknown_ledger, capsys) -> None:
        db, _ = unknown_ledger
        assert main(["--db", db, "reconcile", "--probers", "nope"]) == 2
        assert main(["--db", db, "reconcile", "--probers", "no.such.module:X"]) == 2


class TestStoreRouting:
    def test_sqlite_path_routes_to_sqlite(self, tmp_path) -> None:
        store = open_store(str(tmp_path / "x.db"))
        assert type(store).__name__ == "SQLiteStore"

    def test_postgres_dsn_routes_to_postgres(self) -> None:
        pytest.importorskip("psycopg")
        import os

        dsn = os.environ.get("OPENONCE_TEST_PG_DSN", "host=/tmp dbname=openonce_test")
        try:
            store = open_store(dsn)
        except Exception as exc:  # no test PG available
            pytest.skip(f"no test Postgres: {exc}")
        assert type(store).__name__ == "PostgresStore"
