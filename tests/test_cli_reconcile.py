"""CLI reconcile tests: prober loading, once and daemon modes, DSN routing."""

from __future__ import annotations

import json
import textwrap

import pytest

from openonce import EffectState, EffectUnknown, OpenOnce
from openonce.cli import main, open_store
from openonce.providers.capabilities import capabilities_for_tool, capability_fingerprint
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
def slack_unknown_ledger(tmp_path) -> tuple[str, str]:
    """A ledger with one UNKNOWN custom Slack effect. Returns (db, effect_id)."""
    db = str(tmp_path / "slack.db")
    oo = OpenOnce(SQLiteStore(db))

    @oo.effect(tool="slack.post_message")
    def post_message(channel: str, text: str) -> str:
        raise TimeoutError("ambiguous")

    with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
        post_message(channel="C1", text="hello")
    return db, exc_info.value.record.effect_id


@pytest.fixture
def stripe_unknown_ledger(tmp_path) -> tuple[str, str]:
    """A ledger with one UNKNOWN Stripe effect. Returns (db, effect_id)."""
    db = str(tmp_path / "stripe.db")
    oo = OpenOnce(SQLiteStore(db))

    @oo.effect(tool="stripe.charge")
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


@pytest.fixture
def stripe_happened_prober_module(tmp_path, monkeypatch) -> str:
    """A loadable module exposing a Stripe prober that answers HAPPENED."""
    (tmp_path / "stripe_probers.py").write_text(
        textwrap.dedent(
            """
            from openonce import ProbeOutcome, ProbeResult

            class StripeHappened:
                def probe(self, record):
                    return ProbeResult(ProbeOutcome.HAPPENED, receipt={"stripe_id": "pi_1"})

            PROBERS = {"stripe.charge": StripeHappened()}
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "stripe_probers:PROBERS"


@pytest.fixture
def pay_miss_prober_module(tmp_path, monkeypatch) -> str:
    """A loadable module exposing a pay.charge prober that answers NOT_HAPPENED."""
    (tmp_path / "pay_probers.py").write_text(
        textwrap.dedent(
            """
            from openonce import ProbeOutcome, ProbeResult

            class AlwaysMissing:
                def probe(self, record):
                    return ProbeResult(ProbeOutcome.NOT_HAPPENED, detail="not found")

            PROBERS = {"pay.charge": AlwaysMissing()}
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "pay_probers:PROBERS"


@pytest.fixture
def slack_miss_prober_module(tmp_path, monkeypatch) -> str:
    """A loadable module exposing a Slack prober that answers NOT_HAPPENED."""
    (tmp_path / "slack_probers.py").write_text(
        textwrap.dedent(
            """
            from openonce import ProbeOutcome, ProbeResult

            class AlwaysMissing:
                def probe(self, record):
                    return ProbeResult(ProbeOutcome.NOT_HAPPENED, detail="not found")

            PROBERS = {"slack.post_message": AlwaysMissing()}
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return "slack_probers:PROBERS"


def write_stripe_conformance(tmp_path, *, complete: bool = True) -> str:
    [stripe] = capabilities_for_tool("stripe.charge")
    scenarios: dict[str, object] = {
        "happened": {
            "outcome": "happened",
            "receipt": {"stripe_id": "pi_1"},
            "detail": "fixture returned exactly one payment intent with metadata",
        },
        "young_miss": {
            "outcome": "inconclusive",
            "detail": "fixture returned no hits before Stripe search grace elapsed",
        },
        "mature_miss": {
            "outcome": "not_happened",
            "detail": "fixture returned no hits after Stripe search grace elapsed",
        },
    }
    if complete:
        scenarios["ambiguous"] = {
            "outcome": "inconclusive",
            "detail": "fixture returned duplicate payment intents for one effect",
        }
    path = tmp_path / "openonce-conformance.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": {
                    "stripe": {
                        "capability_fingerprint": capability_fingerprint(stripe),
                        "scenarios": scenarios,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def write_slack_capabilities(tmp_path) -> str:
    path = tmp_path / "openonce-providers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": [
                    {
                        "name": "slack_metadata",
                        "tool_pattern": "slack.post_message",
                        "tier": "tier_2_sender_controlled_authoritative",
                        "key_strategy": "message metadata event_payload.openonce_effect_id",
                        "probe_basis": "conversations.history search by metadata",
                        "miss_semantics": "not-happened after Slack history propagation",
                        "can_auto_rearm_on_miss": True,
                        "default_grace_seconds": 30,
                        "prober": "SlackMetadataProber",
                        "handler_requirements": [
                            "stamp openonce effect_id into Slack message metadata",
                            "grant the prober history scope for the target channel",
                        ],
                        "risk": "medium: depends on Slack retention and history permissions",
                        "required_receipt_fields": ["ts"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


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

    def test_strict_resolution_requires_provider_capability_before_commit(
        self, unknown_ledger, prober_module, capsys
    ) -> None:
        db, eid = unknown_ledger

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                prober_module,
                "--require-provider-capability",
                "--grace",
                "0",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "escalated=1" in out
        reviewed = SQLiteStore(db).get(eid)
        assert reviewed.state == EffectState.HUMAN_REVIEW
        assert "Strict resolution requires reviewed provider knowledge" in reviewed.note

    def test_strict_resolution_requires_provider_capability_before_rearm(
        self, unknown_ledger, pay_miss_prober_module, capsys
    ) -> None:
        db, eid = unknown_ledger

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                pay_miss_prober_module,
                "--require-provider-capability",
                "--grace",
                "0",
            ]
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "escalated=1" in out
        reviewed = SQLiteStore(db).get(eid)
        assert reviewed.state == EffectState.HUMAN_REVIEW
        assert "Strict resolution requires reviewed provider knowledge" in reviewed.note

    def test_conformance_gate_commits_when_evidence_passes(
        self, stripe_unknown_ledger, stripe_happened_prober_module, tmp_path, capsys
    ) -> None:
        db, eid = stripe_unknown_ledger
        conformance = write_stripe_conformance(tmp_path)

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                stripe_happened_prober_module,
                "--conformance-file",
                conformance,
                "--require-conformance",
                "--grace",
                "0",
            ]
        )

        assert code == 0
        assert "committed=1" in capsys.readouterr().out
        assert SQLiteStore(db).get(eid).state == EffectState.COMMITTED

    def test_conformance_gate_escalates_when_evidence_is_incomplete(
        self, stripe_unknown_ledger, stripe_happened_prober_module, tmp_path, capsys
    ) -> None:
        db, eid = stripe_unknown_ledger
        conformance = write_stripe_conformance(tmp_path, complete=False)

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                stripe_happened_prober_module,
                "--conformance-file",
                conformance,
                "--require-conformance",
                "--grace",
                "0",
            ]
        )

        assert code == 0
        assert "escalated=1" in capsys.readouterr().out
        reviewed = SQLiteStore(db).get(eid)
        assert reviewed.state == EffectState.HUMAN_REVIEW
        assert "provider conformance is not proven" in reviewed.note
        assert "stripe.ambiguous" in reviewed.note

    def test_require_conformance_requires_file(self, unknown_ledger, prober_module, capsys) -> None:
        db, _ = unknown_ledger

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                prober_module,
                "--require-conformance",
                "--grace",
                "0",
            ]
        )

        assert code == 2
        assert "--require-conformance requires --conformance-file" in capsys.readouterr().err

    def test_bad_conformance_file_is_usage_error(
        self, unknown_ledger, prober_module, tmp_path, capsys
    ) -> None:
        db, _ = unknown_ledger
        bad = tmp_path / "bad-conformance.json"
        bad.write_text('{"schema_version": 1, "capabilities": []}', encoding="utf-8")

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                prober_module,
                "--conformance-file",
                str(bad),
                "--require-conformance",
                "--grace",
                "0",
            ]
        )

        assert code == 2
        err = capsys.readouterr().err
        assert "cannot configure provider conformance" in err
        assert "capabilities must be an object" in err

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

    def test_capability_file_and_pin_apply_to_runtime_reconciliation(
        self,
        slack_unknown_ledger,
        slack_miss_prober_module,
        tmp_path,
        capsys,
    ) -> None:
        db, eid = slack_unknown_ledger
        capability_file = write_slack_capabilities(tmp_path)

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--probers",
                slack_miss_prober_module,
                "--capability-file",
                capability_file,
                "--provider-capability",
                "slack.post_message=slack_metadata",
                "--grace",
                "0",
            ]
        )

        assert code == 0
        assert "rearmed=1" in capsys.readouterr().out
        assert SQLiteStore(db).get(eid).state == EffectState.APPROVED

    def test_invalid_provider_capability_pin_fails_before_reconciling(
        self, unknown_ledger, capsys
    ) -> None:
        db, _ = unknown_ledger

        code = main(
            [
                "--db",
                db,
                "reconcile",
                "--provider-capability",
                "email.send=nope",
                "--grace",
                "0",
            ]
        )

        assert code == 2
        err = capsys.readouterr().err
        assert "cannot configure provider capabilities" in err
        assert "No provider capability 'nope' matches tool 'email.send'" in err

    def test_malformed_provider_capability_pin_is_usage_error(self, unknown_ledger, capsys) -> None:
        db, _ = unknown_ledger

        code = main(["--db", db, "reconcile", "--provider-capability", "email.send"])

        assert code == 2
        assert "--provider-capability must be TOOL=CAPABILITY" in capsys.readouterr().err


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
