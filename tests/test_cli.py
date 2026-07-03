"""CLI tests: exercised through main() against a real SQLite ledger."""

from __future__ import annotations

import json

import pytest

from openonce import (
    ApprovalPending,
    EffectState,
    EffectUnknown,
    OpenOnce,
    require_approval_for,
)
from openonce.cli import main
from openonce.providers.capabilities import capabilities_for_tool, capability_fingerprint
from openonce.providers.conformance import load_conformance_file
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


def write_custom_capability_payload() -> dict[str, object]:
    return {
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


def write_custom_capabilities(tmp_path) -> str:
    path = tmp_path / "openonce-providers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": [write_custom_capability_payload()],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def write_draft_capabilities(tmp_path) -> str:
    path = tmp_path / "draft-providers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": [
                    {
                        "name": "pay_charge",
                        "tool_pattern": "pay.charge",
                        "tier": "tier_3_non_authoritative",
                        "key_strategy": "TODO: document provider key",
                        "probe_basis": "TODO: document probe source",
                        "miss_semantics": "inconclusive until reviewed",
                        "can_auto_rearm_on_miss": False,
                        "default_grace_seconds": 300,
                        "prober": None,
                        "handler_requirements": ["TODO: stamp provider key"],
                        "risk": "TODO: document duplicate risk",
                        "required_args": [],
                        "required_idempotency_fields": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def write_github_conformance(tmp_path, *, complete: bool = True) -> str:
    [github] = capabilities_for_tool("github.create_pr")
    scenarios: dict[str, object] = {
        "happened": {
            "outcome": "happened",
            "receipt": {
                "number": 42,
                "html_url": "https://github.com/acme/api/pull/42",
                "head": "fix-login",
                "head_label": "acme:fix-login",
            },
            "detail": "fixture returned exactly one PR for acme:fix-login",
        },
        "mature_miss": {
            "outcome": "not_happened",
            "detail": "fixture returned no PRs for acme:fix-login",
        },
    }
    if complete:
        scenarios["ambiguous"] = {
            "outcome": "inconclusive",
            "detail": "fixture returned duplicate PRs for acme:fix-login",
        }
    path = tmp_path / "openonce-conformance.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": {
                    "github_pr": {
                        "capability_fingerprint": capability_fingerprint(github),
                        "source_args": {"owner": "acme", "repo": "api", "head": "fix-login"},
                        "scenarios": scenarios,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def write_stale_github_conformance(tmp_path) -> str:
    path = tmp_path / "stale-openonce-conformance.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capabilities": {
                    "github_pr": {
                        "capability_fingerprint": "sha256:stale",
                        "source_args": {"owner": "acme", "repo": "api", "head": "fix-login"},
                        "scenarios": {
                            "happened": {
                                "outcome": "happened",
                                "receipt": {"number": 42, "head": "fix-login"},
                                "detail": "fixture returned exactly one PR",
                            },
                            "mature_miss": {
                                "outcome": "not_happened",
                                "detail": "fixture returned no PRs",
                            },
                            "ambiguous": {
                                "outcome": "inconclusive",
                                "detail": "fixture returned duplicate PRs",
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


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

    def test_ledger_commands_require_db(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["ls"])
        assert exc_info.value.code == 2

    def test_providers_lists_capability_matrix_without_db(self, capsys) -> None:
        assert main(["providers"]) == 0
        out = capsys.readouterr().out
        assert "provider capability matrix" in out
        assert "stripe.*" in out
        assert "email_smtp" in out
        assert "auto_rearm=no" in out

    def test_providers_filters_by_tool_and_shows_requirements(self, capsys) -> None:
        assert main(["providers", "stripe.charge", "--requirements"]) == 0
        out = capsys.readouterr().out
        assert "stripe.*" in out
        assert "github.create_pr" not in out
        assert "requirements:" in out
        assert "handler:" in out
        assert "Idempotency-Key" in out
        assert "receipt fields: stripe_id" in out

    def test_providers_requirements_show_static_contract_fields(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--requirements"]) == 0
        out = capsys.readouterr().out

        assert "handler args: owner, repo, head" in out
        assert "idempotency fields: owner, repo, head" in out
        assert "receipt fields: number, head" in out
        assert "receipt sources: head <- head" in out

    def test_providers_can_show_conformance_plan(self, capsys) -> None:
        assert main(["providers", "stripe.charge", "--conformance-plan"]) == 0
        out = capsys.readouterr().out

        assert "conformance:" in out
        assert "happened: expect happened + receipt" in out
        assert "receipt fields: stripe_id" in out
        assert "young_miss: expect inconclusive" in out
        assert "mature_miss: expect not_happened" in out

    def test_providers_human_conformance_template_mentions_required_detail(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--conformance-template"]) == 0
        out = capsys.readouterr().out

        assert "conformance evidence template:" in out
        assert "source_args required: owner, repo, head" in out
        assert "detail: required audit note" in out

    def test_providers_unknown_tool_returns_nonzero(self, capsys) -> None:
        assert main(["providers", "slack.post_message"]) == 1
        assert "no provider capability" in capsys.readouterr().out

    def test_providers_json_is_machine_readable(self, capsys) -> None:
        assert main(["providers", "email.send", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["schema_version"] == 1
        assert payload["tool"] == "email.send"
        assert {cap["name"] for cap in payload["capabilities"]} == {
            "email_provider_api",
            "email_smtp",
        }
        smtp = next(cap for cap in payload["capabilities"] if cap["name"] == "email_smtp")
        assert smtp["can_auto_rearm_on_miss"] is False
        assert smtp["fingerprint"].startswith("sha256:")
        assert "route misses to human review" in smtp["handler_requirements"][1]
        assert "conformance_plan" not in smtp

    def test_providers_json_can_include_conformance_plan(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--conformance-plan", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        [github] = payload["capabilities"]
        assert [case["scenario"] for case in github["conformance_plan"]] == [
            "happened",
            "mature_miss",
            "ambiguous",
        ]
        assert github["conformance_plan"][0]["receipt_required"] is True
        assert github["conformance_plan"][0]["required_receipt_source_fields"] == {"head": "head"}

    def test_providers_json_can_include_conformance_template(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--conformance-template", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        [github] = payload["capabilities"]
        template = github["conformance_template"]
        happened = template["scenarios"]["happened"]
        assert template["capability_fingerprint"] == github["fingerprint"]
        assert happened["expected_outcomes"] == ["happened"]
        assert happened["receipt_required"] is True
        assert happened["required_receipt_fields"] == ["number", "head"]
        assert happened["required_receipt_source_fields"] == {"head": "head"}
        assert happened["outcome"] is None
        assert happened["receipt"] is None
        assert template["source_args"] == {
            "owner": "TODO: value used by the conformance fixture",
            "repo": "TODO: value used by the conformance fixture",
            "head": "TODO: value used by the conformance fixture",
        }
        assert "conformance_plan" not in github

    def test_providers_can_emit_conformance_file_template(self, tmp_path, capsys) -> None:
        assert main(["providers", "github.create_pr", "--conformance-file-template"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["schema_version"] == 1
        assert set(payload["capabilities"]) == {"github_pr"}
        github = payload["capabilities"]["github_pr"]
        assert github["scenarios"]["happened"]["required_receipt_fields"] == [
            "number",
            "head",
        ]
        assert github["scenarios"]["happened"]["required_receipt_source_fields"] == {"head": "head"}
        assert github["source_args"] == {
            "owner": "TODO: value used by the conformance fixture",
            "repo": "TODO: value used by the conformance fixture",
            "head": "TODO: value used by the conformance fixture",
        }
        path = tmp_path / "openonce-conformance.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="outcome must be a non-empty string"):
            load_conformance_file(path)

    def test_conformance_file_template_respects_capability_filter(self, capsys) -> None:
        assert (
            main(
                [
                    "providers",
                    "email.send",
                    "--capability",
                    "email_provider_api",
                    "--conformance-file-template",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert set(payload["capabilities"]) == {"email_provider_api"}

    def test_conformance_file_template_fails_unknown_tool(self, capsys) -> None:
        assert main(["providers", "pay.charge", "--conformance-file-template"]) == 1

        assert "no provider capability matches 'pay.charge'" in capsys.readouterr().err

    def test_conformance_file_template_cannot_validate_at_the_same_time(
        self, tmp_path, capsys
    ) -> None:
        path = write_github_conformance(tmp_path)

        assert (
            main(
                [
                    "providers",
                    "github.create_pr",
                    "--conformance-file-template",
                    "--conformance-file",
                    path,
                ]
            )
            == 2
        )

        assert "--conformance-file-template cannot be combined" in capsys.readouterr().err

    def test_provider_conformance_file_gate_passes(self, tmp_path, capsys) -> None:
        path = write_github_conformance(tmp_path)

        assert (
            main(
                [
                    "providers",
                    "github.create_pr",
                    "--conformance-file",
                    path,
                    "--require-conformance",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["conformance"]["file"] == path
        assert payload["conformance"]["passed"] is True
        assert payload["conformance"]["reports"][0]["capability"] == "github_pr"

    def test_provider_conformance_file_gate_fails_missing_scenario(self, tmp_path, capsys) -> None:
        path = write_github_conformance(tmp_path, complete=False)

        assert (
            main(
                [
                    "providers",
                    "github.create_pr",
                    "--conformance-file",
                    path,
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["conformance"]["passed"] is False
        assert "github_pr.ambiguous" in payload["error"]
        failures = payload["conformance"]["reports"][0]["failures"]
        assert failures[0]["reason"] == "missing conformance observation"

    def test_provider_conformance_file_gate_fails_stale_fingerprint(self, tmp_path, capsys) -> None:
        path = write_stale_github_conformance(tmp_path)

        assert (
            main(
                [
                    "providers",
                    "github.create_pr",
                    "--conformance-file",
                    path,
                    "--require-conformance",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["conformance"]["passed"] is False
        assert "github_pr.capability_fingerprint" in payload["error"]
        assert "does not match" in payload["conformance"]["reports"][0]["failures"][0]["reason"]

    def test_provider_require_conformance_requires_a_file(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--require-conformance", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert payload["conformance"]["file"] is None
        assert payload["conformance"]["passed"] is False
        assert "--require-conformance requires --conformance-file" in payload["error"]

    def test_provider_bad_conformance_file_returns_usage_error(self, tmp_path, capsys) -> None:
        path = tmp_path / "bad-conformance.json"
        path.write_text('{"schema_version": 1, "capabilities": []}', encoding="utf-8")

        assert main(["providers", "github.create_pr", "--conformance-file", str(path)]) == 2
        err = capsys.readouterr().err
        assert "cannot load provider conformance file" in err
        assert "capabilities must be an object" in err

    def test_provider_scan_gate_passes_for_known_safe_tools(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            """
@oo.effect(tool="stripe.charge")
def charge():
    pass

@effect_tool(oo, tool="github.create_pr")
def create_pr():
    pass
""",
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-known",
                    "--require-auto-rearm",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert [item["tool"] for item in payload["tools"]] == [
            "stripe.charge",
            "github.create_pr",
        ]
        assert "error" not in payload

    def test_provider_scan_can_emit_conformance_file_template(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            """
@oo.effect(tool="stripe.charge")
def charge():
    pass

@effect_tool(oo, tool="github.create_pr")
def create_pr():
    pass
""",
            encoding="utf-8",
        )

        assert main(["providers", "--scan", str(app), "--conformance-file-template"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["schema_version"] == 1
        assert set(payload["capabilities"]) == {"stripe", "github_pr"}

    def test_provider_scan_conformance_file_template_fails_unknown_tools(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text('@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n', encoding="utf-8")

        assert main(["providers", "--scan", str(app), "--conformance-file-template"]) == 1

        assert "no provider capability matches 'pay.charge'" in capsys.readouterr().err

    def test_provider_scan_gate_fails_unknown_tools(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text('@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n', encoding="utf-8")

        assert main(["providers", "--scan", str(app), "--require-known", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["tool"] == "pay.charge"
        assert "no provider capability matches 'pay.charge'" in payload["error"]

    def test_provider_scan_conformance_gate_passes(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="github.create_pr")\ndef create_pr():\n    pass\n',
            encoding="utf-8",
        )
        conformance = write_github_conformance(tmp_path)

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--conformance-file",
                    conformance,
                    "--require-conformance",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["conformance"]["passed"] is True

    def test_provider_scan_conformance_gate_fails_unknown_tools(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text('@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n', encoding="utf-8")
        conformance = tmp_path / "openonce-conformance.json"
        conformance.write_text('{"schema_version": 1, "capabilities": {}}', encoding="utf-8")

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--conformance-file",
                    str(conformance),
                    "--require-conformance",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["conformance"]["passed"] is False
        assert "cannot prove conformance for 'pay.charge'" in payload["error"]

    def test_provider_scan_handler_contract_gate_passes(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-known",
                    "--require-handler-contract",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["handler_contract"]["passed"] is True
        assert payload["tools"][0]["idempotency_fields"] == ["owner", "repo", "head"]

    def test_provider_scan_handler_contract_gate_accepts_static_constants(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            'TOOL = "github.create_pr"\n'
            'FIELDS = ["owner", "repo", "head"]\n'
            "@oo.effect(tool=TOOL, idempotency_fields=FIELDS)\n"
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-known",
                    "--require-handler-contract",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["tool"] == "github.create_pr"
        assert payload["tools"][0]["idempotency_fields"] == ["owner", "repo", "head"]
        assert payload["tools"][0]["handler_contract"]["passed"] is True

    def test_provider_scan_handler_contract_gate_fails(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])\n'
            "def create_pr(owner, repo):\n"
            "    pass\n",
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-handler-contract",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert "requires handler args: head" in payload["error"]
        failures = payload["tools"][0]["handler_contract"]["failures"]
        assert any("requires idempotency_fields: head" in failure for failure in failures)

    def test_provider_scan_handler_contract_gate_fails_unknown_tools(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text('@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n', encoding="utf-8")

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-handler-contract",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert "cannot prove handler contract for 'pay.charge'" in payload["error"]
        assert payload["tools"][0]["handler_contract"]["passed"] is False

    def test_provider_scan_receipt_contract_gate_passes(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head, "html_url": "https://example.test"}\n'
            "    return receipt\n",
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-known",
                    "--require-handler-contract",
                    "--require-receipt-contract",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["receipt_contract"]["passed"] is True
        assert payload["tools"][0]["return_field_sets"] == [["number", "head", "html_url"]]
        assert payload["tools"][0]["return_field_sources"] == [{"head": "head"}]

    def test_provider_scan_receipt_contract_gate_fails_missing_fields(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42}\n',
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-receipt-contract",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert "return[0] is missing receipt fields: head" in payload["error"]
        assert payload["tools"][0]["receipt_contract"]["passed"] is False

    def test_provider_scan_receipt_contract_gate_fails_wrong_receipt_source(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, "head": "acme:wrong"}\n',
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-receipt-contract",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert "receipt field 'head' must come from handler arg 'head'" in payload["error"]
        assert payload["tools"][0]["receipt_contract"]["passed"] is False

    def test_provider_scan_receipt_contract_gate_fails_unknown_tools(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text('@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n', encoding="utf-8")

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--require-receipt-contract",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert "cannot prove receipt contract for 'pay.charge'" in payload["error"]
        assert payload["tools"][0]["receipt_contract"]["passed"] is False

    def test_provider_require_receipt_contract_requires_scan(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--require-receipt-contract"]) == 2
        assert "--require-receipt-contract requires --scan" in capsys.readouterr().err

    def test_provider_require_handler_contract_requires_scan(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--require-handler-contract"]) == 2
        assert "--require-handler-contract requires --scan" in capsys.readouterr().err

    def test_provider_scan_reports_dynamic_tool_as_gate_failure(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            "@oo.effect(tool=build_tool())\ndef charge():\n    pass\n",
            encoding="utf-8",
        )

        assert main(["providers", "--scan", str(app), "--require-known", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert payload["tools"][0]["tool"] is None
        assert "dynamic tool expression cannot be provider-audited" in payload["error"]

    def test_provider_scan_bad_path_returns_usage_error(self, tmp_path, capsys) -> None:
        missing = tmp_path / "missing"

        assert main(["providers", "--scan", str(missing)]) == 2
        err = capsys.readouterr().err
        assert "cannot scan provider tools" in err
        assert "scan path does not exist" in err

    def test_provider_scan_suggests_capabilities_for_unknown_tools(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n',
            encoding="utf-8",
        )

        assert main(["providers", "--scan", str(app), "--suggest-capabilities", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        [suggestion] = payload["suggested_capabilities"]
        assert suggestion["tool"] == "pay.charge"
        assert suggestion["capability"]["name"] == "pay_charge"
        assert suggestion["capability"]["can_auto_rearm_on_miss"] is False
        assert payload["suggested_capability_file"] == {
            "schema_version": 1,
            "capabilities": [suggestion["capability"]],
        }

    def test_provider_require_reviewed_fails_draft_capability_file(self, tmp_path, capsys) -> None:
        path = write_draft_capabilities(tmp_path)

        assert main(["providers", "--capability-file", path, "--validate-only"]) == 0
        capsys.readouterr()

        assert (
            main(
                [
                    "providers",
                    "--capability-file",
                    path,
                    "--validate-only",
                    "--require-reviewed",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["valid"] is False
        assert payload["readiness"]["passed"] is False
        assert "pay_charge: key_strategy still contains TODO" in payload["readiness"]["failures"]

    def test_provider_require_reviewed_passes_reviewed_builtin_tool(self, capsys) -> None:
        assert main(["providers", "github.create_pr", "--require-reviewed", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["readiness"]["passed"] is True

    def test_provider_scan_require_reviewed_fails_draft_custom_capability(
        self, tmp_path, capsys
    ) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n',
            encoding="utf-8",
        )
        capability_file = write_draft_capabilities(tmp_path)

        assert (
            main(
                [
                    "providers",
                    "--scan",
                    str(app),
                    "--capability-file",
                    capability_file,
                    "--require-reviewed",
                    "--json",
                ]
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)

        assert "pay_charge: key_strategy still contains TODO" in payload["error"]
        assert payload["tools"][0]["readiness"]["passed"] is False

    def test_provider_scan_require_reviewed_fails_unknown_tools(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text('@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n', encoding="utf-8")

        assert main(["providers", "--scan", str(app), "--require-reviewed", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert "cannot prove provider capability readiness for 'pay.charge'" in payload["error"]
        assert payload["tools"][0]["readiness"]["passed"] is False

    def test_provider_scan_suggestion_human_output(self, tmp_path, capsys) -> None:
        app = tmp_path / "app.py"
        app.write_text(
            '@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n',
            encoding="utf-8",
        )

        assert main(["providers", "--scan", str(app), "--suggest-capabilities"]) == 0
        out = capsys.readouterr().out

        assert "suggested capabilities" in out
        assert "pay_charge: pay.charge" in out

    def test_provider_suggest_capabilities_requires_scan(self, capsys) -> None:
        assert main(["providers", "pay.charge", "--suggest-capabilities"]) == 2
        assert "--suggest-capabilities requires --scan" in capsys.readouterr().err

    def test_providers_json_unknown_tool_returns_structured_error(self, capsys) -> None:
        assert main(["providers", "slack.post_message", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert payload["schema_version"] == 1
        assert payload["tool"] == "slack.post_message"
        assert payload["capabilities"] == []
        assert "no provider capability" in payload["error"]

    def test_provider_require_known_gate_passes_for_known_tool(self, capsys) -> None:
        assert main(["providers", "stripe.charge", "--require-known"]) == 0

        out = capsys.readouterr().out
        assert "policy: passed" in out

    def test_provider_require_known_gate_fails_for_unknown_tool(self, capsys) -> None:
        assert main(["providers", "slack.post_message", "--require-known"]) == 1

        out = capsys.readouterr().out
        assert "policy: failed" in out
        assert "no provider capability" in out

    def test_provider_require_auto_rearm_gate_passes_for_safe_tool(self, capsys) -> None:
        assert main(["providers", "stripe.charge", "--require-auto-rearm"]) == 0

        out = capsys.readouterr().out
        assert "policy: passed" in out

    def test_provider_require_auto_rearm_gate_fails_for_non_authoritative_tool(
        self, capsys
    ) -> None:
        assert main(["providers", "email.send", "--require-auto-rearm"]) == 1

        out = capsys.readouterr().out
        assert "policy: failed" in out
        assert "auto-rearm" in out
        assert "email_smtp" in out

    def test_provider_capability_filter_disambiguates_auto_rearm_gate(self, capsys) -> None:
        assert (
            main(
                [
                    "providers",
                    "email.send",
                    "--capability",
                    "email_provider_api",
                    "--require-auto-rearm",
                ]
            )
            == 0
        )

        out = capsys.readouterr().out
        assert "email_provider_api" in out
        assert "email_smtp" not in out
        assert "policy: passed" in out

    def test_provider_invalid_capability_returns_structured_error(self, capsys) -> None:
        assert main(["providers", "email.send", "--capability", "nope", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert payload["tool"] == "email.send"
        assert payload["capability"] == "nope"
        assert payload["capabilities"] == []
        assert "no provider capability 'nope' matches 'email.send'" in payload["error"]

    def test_provider_capability_file_extends_policy_gate(self, tmp_path, capsys) -> None:
        path = write_custom_capabilities(tmp_path)

        assert (
            main(
                [
                    "providers",
                    "slack.post_message",
                    "--capability-file",
                    path,
                    "--require-known",
                    "--require-auto-rearm",
                    "--json",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)

        assert payload["capability_file"] == path
        assert payload["policy"]["passed"] is True
        assert [cap["name"] for cap in payload["capabilities"]] == ["slack_metadata"]

    def test_provider_bad_capability_file_returns_usage_error(self, tmp_path, capsys) -> None:
        path = tmp_path / "bad.json"
        path.write_text('{"schema_version": 1, "capabilities": "nope"}', encoding="utf-8")

        assert main(["providers", "--capability-file", str(path)]) == 2
        err = capsys.readouterr().err
        assert "cannot load provider capability file" in err
        assert "capabilities must be a list" in err

    def test_provider_capability_file_validate_only(self, tmp_path, capsys) -> None:
        path = write_custom_capabilities(tmp_path)

        assert main(["providers", "--capability-file", path, "--validate-only"]) == 0
        out = capsys.readouterr().out
        assert "provider capability matrix valid" in out
        assert "1 custom" in out
        assert path in out

    def test_provider_capability_file_validate_only_json(self, tmp_path, capsys) -> None:
        path = write_custom_capabilities(tmp_path)

        assert main(["providers", "--capability-file", path, "--validate-only", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload == {
            "schema_version": 1,
            "capability_file": path,
            "valid": True,
            "capability_count": 5,
            "custom_capability_count": 1,
        }

    def test_provider_capability_file_duplicate_name_fails_validation(
        self, tmp_path, capsys
    ) -> None:
        path = tmp_path / "duplicate.json"
        duplicate = write_custom_capability_payload()
        duplicate["name"] = "stripe"
        payload = {"schema_version": 1, "capabilities": [duplicate]}
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert main(["providers", "--capability-file", str(path), "--validate-only"]) == 2
        err = capsys.readouterr().err
        assert "duplicate provider capability name 'stripe'" in err

    def test_provider_policy_gate_json_is_structured(self, capsys) -> None:
        assert main(["providers", "email.send", "--require-auto-rearm", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)

        assert payload["schema_version"] == 1
        assert payload["tool"] == "email.send"
        assert payload["policy"]["passed"] is False
        assert payload["policy"]["required"] == ["auto_rearm_on_miss"]
        check = payload["policy"]["checks"]["auto_rearm_on_miss"]
        assert check["passed"] is False
        assert "email_smtp" in check["reason"]

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

    def test_show_preserves_falsey_success_result(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "falsey.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store)

        @oo.effect(tool="counter.get")
        def count() -> int:
            return 0

        with oo.scope("run1"):
            count()
        [rec] = store.scan_states({EffectState.COMMITTED}, updated_before=float("inf"))

        assert main(["--db", db, "show", rec.effect_id]) == 0
        assert "result:       ok: 0" in capsys.readouterr().out

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

    def test_approve_human_review_fails(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "approve-review.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store, enforce_provider_receipts=True)

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            return {"status": "succeeded"}

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            charge()

        assert main(["--db", db, "approve", exc_info.value.record.effect_id]) == 1
        assert "not awaiting approval" in capsys.readouterr().err

    def test_deny_records_reason(self, ledger, capsys) -> None:
        db, eid = ledger
        assert main(["--db", db, "deny", eid, "--reason", "wrong customer"]) == 0
        store = SQLiteStore(db)
        rec = store.get(eid)
        assert rec.state == EffectState.DENIED
        assert rec.note == "wrong customer"

    def test_resolve_happened_commits_human_review_with_manual_receipt(
        self, tmp_path, capsys
    ) -> None:
        db = str(tmp_path / "manual-resolution.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store, enforce_provider_receipts=True)

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            return {"status": "succeeded"}

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        assert (
            main(
                [
                    "--db",
                    db,
                    "resolve-happened",
                    eid,
                    "--require-receipt-contract",
                    "--receipt-json",
                    '{"stripe_id":"pi_1"}',
                    "--by",
                    "eric",
                    "--reason",
                    "matched in Stripe",
                ]
            )
            == 0
        )
        assert "committed with manual receipt" in capsys.readouterr().out

        rec = SQLiteStore(db).get(eid)
        assert rec.state == EffectState.COMMITTED
        assert rec.result is not None
        assert rec.result.ok is True
        assert rec.result.value == {"stripe_id": "pi_1"}
        assert rec.note == "matched in Stripe"
        assert [entry.to_state for entry in SQLiteStore(db).journal(eid)] == [
            EffectState.PLANNED,
            EffectState.APPROVED,
            EffectState.STARTED,
            EffectState.RECEIPT_RECORDED,
            EffectState.HUMAN_REVIEW,
            EffectState.COMMITTED,
        ]

    def test_resolve_happened_rejects_non_object_receipt(self, ledger, capsys) -> None:
        db, eid = ledger

        assert main(["--db", db, "resolve-happened", eid, "--receipt-json", "[]"]) == 2
        assert "receipt must be a non-empty JSON object" in capsys.readouterr().err

    def test_resolve_happened_require_receipt_contract_blocks_missing_fields(
        self, tmp_path, capsys
    ) -> None:
        db = str(tmp_path / "manual-contract.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store, enforce_provider_receipts=True)

        @oo.effect(tool="stripe.charge")
        def charge() -> dict[str, str]:
            return {"status": "succeeded"}

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        assert (
            main(
                [
                    "--db",
                    db,
                    "resolve-happened",
                    eid,
                    "--require-receipt-contract",
                    "--receipt-json",
                    '{"status":"succeeded"}',
                ]
            )
            == 1
        )
        assert "missing required external evidence field(s): stripe_id" in capsys.readouterr().err
        assert SQLiteStore(db).get(eid).state == EffectState.HUMAN_REVIEW

    def test_resolve_not_happened_rearms_for_retry(self, tmp_path, capsys) -> None:
        db = str(tmp_path / "manual-not-happened.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store)
        mode = {"timeout": True}

        @oo.effect(tool="pay.charge")
        def charge() -> str:
            if mode["timeout"]:
                raise TimeoutError("read timed out after send")
            return "charged"

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            charge()
        eid = exc_info.value.record.effect_id

        assert (
            main(
                [
                    "--db",
                    db,
                    "resolve-not-happened",
                    eid,
                    "--by",
                    "eric",
                    "--reason",
                    "not found in provider",
                ]
            )
            == 0
        )
        assert "re-armed for retry" in capsys.readouterr().out
        assert SQLiteStore(db).get(eid).state == EffectState.APPROVED

        mode["timeout"] = False
        with oo.scope("run1"):
            assert charge() == "charged"

    def test_resolve_not_happened_require_auto_rearm_blocks_risky_tools(
        self, tmp_path, capsys
    ) -> None:
        db = str(tmp_path / "manual-not-happened-policy.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store)

        @oo.effect(tool="email.send")
        def send() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            send()

        assert (
            main(
                [
                    "--db",
                    db,
                    "resolve-not-happened",
                    exc_info.value.record.effect_id,
                    "--reason",
                    "not found in sent store",
                    "--require-auto-rearm",
                ]
            )
            == 1
        )
        assert "not safe to auto-rearm on a miss" in capsys.readouterr().err
        assert SQLiteStore(db).get(exc_info.value.record.effect_id).state == EffectState.UNKNOWN

    def test_resolve_not_happened_require_auto_rearm_blocks_unknown_tools(
        self, tmp_path, capsys
    ) -> None:
        db = str(tmp_path / "manual-not-happened-unknown.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store)

        @oo.effect(tool="custom.side_effect")
        def do() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            do()

        assert (
            main(
                [
                    "--db",
                    db,
                    "resolve-not-happened",
                    exc_info.value.record.effect_id,
                    "--reason",
                    "not found in provider",
                    "--require-auto-rearm",
                ]
            )
            == 1
        )
        assert "no provider capability matches 'custom.side_effect'" in capsys.readouterr().err
        assert SQLiteStore(db).get(exc_info.value.record.effect_id).state == EffectState.UNKNOWN

    def test_resolve_happened_require_receipt_contract_rejects_unknown_tools(
        self, tmp_path, capsys
    ) -> None:
        db = str(tmp_path / "manual-unknown.db")
        store = SQLiteStore(db)
        oo = OpenOnce(store)

        @oo.effect(tool="custom.side_effect")
        def do() -> None:
            raise TimeoutError("read timed out after send")

        with oo.scope("run1"), pytest.raises(EffectUnknown) as exc_info:
            do()

        assert (
            main(
                [
                    "--db",
                    db,
                    "resolve-happened",
                    exc_info.value.record.effect_id,
                    "--require-receipt-contract",
                    "--receipt-json",
                    '{"id":"x_1"}',
                ]
            )
            == 1
        )
        assert "no provider capability matches 'custom.side_effect'" in capsys.readouterr().err
