"""CLI tests: exercised through main() against a real SQLite ledger."""

from __future__ import annotations

import pytest

from openonce import (
    ApprovalPending,
    EffectState,
    OpenOnce,
    require_approval_for,
)
from openonce.cli import main
from openonce.store.sqlite import SQLiteStore


@pytest.fixture
def ledger(tmp_path) -> tuple[str, str]:
    """A SQLite ledger with one committed effect and one pending approval.
    Returns (db_path, pending_effect_id)."""
    db = str(tmp_path / "cli.db")
    oo = OpenOnce(SQLiteStore(db), policy=require_approval_for(["stripe.*"]))

    @oo.effect(tool="email.send")
    def send(to: str) -> str:
        return "sent"

    @oo.effect(tool="stripe.refund")
    def refund(charge: str) -> str:
        return "refunded"

    with oo.scope("run1"):
        send(to="a@b.c")
        with pytest.raises(ApprovalPending) as exc_info:
            refund(charge="ch_1")
    return db, exc_info.value.effect_id


class TestCli:
    def test_ls_lists_everything(self, ledger, capsys) -> None:
        db, _ = ledger
        assert main(["--db", db, "ls"]) == 0
        out = capsys.readouterr().out
        assert "email.send" in out and "stripe.refund" in out
        assert "committed" in out and "requires_approval" in out

    def test_ls_state_filter(self, ledger, capsys) -> None:
        db, _ = ledger
        assert main(["--db", db, "ls", "--state", "requires_approval"]) == 0
        out = capsys.readouterr().out
        assert "stripe.refund" in out and "email.send" not in out

    def test_ls_invalid_state_lists_valid_ones(self, ledger) -> None:
        db, _ = ledger
        with pytest.raises(SystemExit, match="valid states"):
            main(["--db", db, "ls", "--state", "bogus"])

    def test_review_shows_the_queue(self, ledger, capsys) -> None:
        db, eid = ledger
        assert main(["--db", db, "review"]) == 0
        out = capsys.readouterr().out
        assert eid in out and "1 effect(s) need a human" in out

    def test_show_prints_journal(self, ledger, capsys) -> None:
        db, eid = ledger
        assert main(["--db", db, "show", eid]) == 0
        out = capsys.readouterr().out
        assert "provider_key" in out
        assert "planned -> requires_approval" in out

    def test_show_missing_effect_fails(self, ledger, capsys) -> None:
        db, _ = ledger
        assert main(["--db", db, "show", "eff_nope"]) == 1

    def test_approve_then_agent_retry_executes(self, ledger, capsys) -> None:
        db, eid = ledger
        assert main(["--db", db, "approve", eid, "--by", "eric"]) == 0
        assert "approved" in capsys.readouterr().out

        # The agent's next identical call picks up APPROVED and executes.
        store = SQLiteStore(db)
        oo = OpenOnce(store, policy=require_approval_for(["stripe.*"]))

        @oo.effect(tool="stripe.refund")
        def refund(charge: str) -> str:
            return "refunded"

        with oo.scope("run1"):
            assert refund(charge="ch_1") == "refunded"
        assert store.get(eid).state == EffectState.COMMITTED

    def test_approve_wrong_state_fails(self, ledger, capsys) -> None:
        db, eid = ledger
        main(["--db", db, "approve", eid])
        assert main(["--db", db, "approve", eid]) == 1  # already approved

    def test_deny_records_reason(self, ledger, capsys) -> None:
        db, eid = ledger
        assert main(["--db", db, "deny", eid, "--reason", "wrong customer"]) == 0
        store = SQLiteStore(db)
        rec = store.get(eid)
        assert rec.state == EffectState.DENIED
        assert rec.note == "wrong customer"
