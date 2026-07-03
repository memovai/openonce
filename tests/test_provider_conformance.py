from __future__ import annotations

import json

import pytest

from openonce.providers.base import ProbeOutcome, ProbeResult
from openonce.providers.capabilities import capabilities_for_tool, capability_fingerprint
from openonce.providers.conformance import (
    CapabilityConformanceEvidence,
    ConformanceScenario,
    assert_conformance,
    conformance_evidence_file_template,
    conformance_evidence_template,
    conformance_plan,
    load_conformance_file,
    validate_conformance,
    validate_conformance_evidence,
)
from openonce.providers.stripe import StripeProber
from openonce.records import EffectRecord
from openonce.state import EffectState


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Search:
    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits

    def __call__(self, resource: str, query: str) -> list[dict]:
        return self.hits


def make_record(*, tool: str = "stripe.charge", updated_at: float = 1_000.0) -> EffectRecord:
    return EffectRecord(
        effect_id="eff_conformance",
        idempotency_key="oo1_k",
        tool=tool,
        state=EffectState.UNKNOWN,
        args_fingerprint="fp",
        args_json=json.dumps({"amount": 500}),
        scope="run1",
        provider_key=f"eff_conformance:{tool}",
        attempt=1,
        created_at=updated_at,
        updated_at=updated_at,
    )


class TestProviderConformance:
    def test_plan_is_derived_from_capability_semantics(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")
        [smtp] = capabilities_for_tool("email.send", "email_smtp")

        github_cases = conformance_plan(github)
        smtp_cases = conformance_plan(smtp)

        assert [case.scenario for case in github_cases] == [
            ConformanceScenario.HAPPENED,
            ConformanceScenario.MATURE_MISS,
            ConformanceScenario.AMBIGUOUS,
        ]
        assert github_cases[0].required_receipt_source_fields == (("head", "head"),)
        mature_smtp = next(
            case for case in smtp_cases if case.scenario is ConformanceScenario.MATURE_MISS
        )
        assert mature_smtp.expected_outcomes == (ProbeOutcome.INCONCLUSIVE,)
        assert "non-authoritative" in mature_smtp.description

    def test_stripe_prober_passes_the_standard_capability_plan(self) -> None:
        [stripe] = capabilities_for_tool("stripe.charge")
        record = make_record()

        observations = {
            ConformanceScenario.HAPPENED: StripeProber(
                Search([{"id": "pi_1", "status": "succeeded", "amount": 500}]),
                clock=Clock(1_000),
            ).probe(record),
            ConformanceScenario.YOUNG_MISS: StripeProber(
                Search([]), indexing_lag_seconds=120, clock=Clock(1_030)
            ).probe(record),
            ConformanceScenario.MATURE_MISS: StripeProber(
                Search([]), indexing_lag_seconds=120, clock=Clock(1_600)
            ).probe(record),
            ConformanceScenario.AMBIGUOUS: StripeProber(
                Search([{"id": "pi_1"}, {"id": "pi_2"}]), clock=Clock(1_000)
            ).probe(record),
        }

        report = validate_conformance(stripe, observations)

        assert report.passed is True
        assert report.to_dict()["failures"] == []
        assert_conformance(stripe, observations)

    def test_non_authoritative_mature_miss_must_not_return_not_happened(self) -> None:
        [smtp] = capabilities_for_tool("email.send", "email_smtp")

        report = validate_conformance(
            smtp,
            {
                "happened": ProbeResult(ProbeOutcome.HAPPENED, receipt={"id": "msg_1"}),
                "young_miss": ProbeResult(ProbeOutcome.INCONCLUSIVE),
                "mature_miss": ProbeResult(ProbeOutcome.NOT_HAPPENED),
                "ambiguous": ProbeResult(ProbeOutcome.INCONCLUSIVE),
            },
        )

        assert report.passed is False
        [failure] = [failure for failure in report.failures if failure.scenario == "mature_miss"]
        assert failure.expected == ("inconclusive",)
        assert failure.actual == "not_happened"

    def test_happened_requires_external_receipt_evidence(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        with pytest.raises(AssertionError, match="receipt"):
            assert_conformance(
                github,
                {
                    ConformanceScenario.HAPPENED: ProbeResult(ProbeOutcome.HAPPENED),
                    ConformanceScenario.MATURE_MISS: ProbeResult(ProbeOutcome.NOT_HAPPENED),
                    ConformanceScenario.AMBIGUOUS: ProbeResult(ProbeOutcome.INCONCLUSIVE),
                },
            )

    def test_happened_requires_capability_receipt_fields(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        report = validate_conformance(
            github,
            {
                ConformanceScenario.HAPPENED: ProbeResult(
                    ProbeOutcome.HAPPENED,
                    receipt={"number": 42},
                ),
                ConformanceScenario.MATURE_MISS: ProbeResult(ProbeOutcome.NOT_HAPPENED),
                ConformanceScenario.AMBIGUOUS: ProbeResult(ProbeOutcome.INCONCLUSIVE),
            },
        )

        assert report.passed is False
        [failure] = [
            failure
            for failure in report.failures
            if failure.scenario == "happened" and "required field" in failure.reason
        ]
        assert failure.expected == ("number", "head")
        assert "head" in failure.reason

    def test_happened_receipt_must_satisfy_source_contract(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        report = validate_conformance(
            github,
            {
                ConformanceScenario.HAPPENED: ProbeResult(
                    ProbeOutcome.HAPPENED,
                    receipt={"number": 42, "head": "other-branch"},
                ),
                ConformanceScenario.MATURE_MISS: ProbeResult(ProbeOutcome.NOT_HAPPENED),
                ConformanceScenario.AMBIGUOUS: ProbeResult(ProbeOutcome.INCONCLUSIVE),
            },
            source_args={"head": "fix-login"},
        )

        assert report.passed is False
        [failure] = [
            failure
            for failure in report.failures
            if failure.scenario == "happened" and "source contract" in failure.reason
        ]
        assert failure.expected == ("head<-head",)
        assert "expected 'fix-login'" in failure.reason

    def test_programmatic_conformance_requires_all_source_args(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        report = validate_conformance(
            github,
            {
                ConformanceScenario.HAPPENED: ProbeResult(
                    ProbeOutcome.HAPPENED,
                    receipt={"number": 42, "head": "fix-login"},
                ),
                ConformanceScenario.MATURE_MISS: ProbeResult(ProbeOutcome.NOT_HAPPENED),
                ConformanceScenario.AMBIGUOUS: ProbeResult(ProbeOutcome.INCONCLUSIVE),
            },
            source_args={"head": "fix-login"},
        )

        assert report.passed is False
        [failure] = [failure for failure in report.failures if failure.scenario == "source_args"]
        assert failure.expected == ("owner", "repo", "head")
        assert "missing: owner, repo" in failure.reason

    def test_missing_required_scenarios_fail_the_report(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        report = validate_conformance(
            github,
            {
                ConformanceScenario.HAPPENED: ProbeResult(
                    ProbeOutcome.HAPPENED,
                    {"number": 42, "head": "fix-login"},
                )
            },
            source_args={"head": "fix-login"},
        )

        assert report.passed is False
        missing = {failure.scenario for failure in report.failures if failure.actual is None}
        assert missing == {"mature_miss", "ambiguous"}

    def test_load_conformance_file_and_validate_evidence(self, tmp_path) -> None:
        path = tmp_path / "openonce-conformance.json"
        [github] = capabilities_for_tool("github.create_pr")
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": {
                        "github_pr": {
                            "capability_fingerprint": capability_fingerprint(github),
                            "source_args": {
                                "owner": "acme",
                                "repo": "api",
                                "head": "fix-login",
                            },
                            "scenarios": {
                                "happened": {
                                    "outcome": "happened",
                                    "receipt": {"number": 42, "head": "fix-login"},
                                    "detail": "PR exists",
                                },
                                "mature_miss": {
                                    "outcome": "not_happened",
                                    "detail": "no PR found by authoritative head lookup",
                                },
                                "ambiguous": {
                                    "outcome": "inconclusive",
                                    "detail": "fixture returned duplicate head matches",
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        evidence = load_conformance_file(path)
        reports = validate_conformance_evidence(
            (github,),
            evidence,
            required_capabilities=("github_pr",),
        )

        assert len(reports) == 1
        assert reports[0].passed is True
        assert evidence["github_pr"].capability_fingerprint == capability_fingerprint(github)
        assert evidence["github_pr"].source_args == {
            "owner": "acme",
            "repo": "api",
            "head": "fix-login",
        }

    def test_conformance_evidence_template_includes_current_fingerprint(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        template = conformance_evidence_template(github)
        scenarios = template["scenarios"]

        assert template["capability_fingerprint"] == capability_fingerprint(github)
        assert isinstance(scenarios, dict)
        assert set(scenarios) == {"happened", "mature_miss", "ambiguous"}
        happened = scenarios["happened"]
        assert isinstance(happened, dict)
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

    def test_conformance_evidence_file_template_wraps_capability_templates(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        template = conformance_evidence_file_template((github,))

        assert template["schema_version"] == 1
        capabilities = template["capabilities"]
        assert isinstance(capabilities, dict)
        assert set(capabilities) == {"github_pr"}
        github_template = capabilities["github_pr"]
        assert isinstance(github_template, dict)
        assert github_template["capability_fingerprint"] == capability_fingerprint(github)
        assert "happened" in github_template["scenarios"]

    def test_unfilled_conformance_template_is_not_valid_evidence(self, tmp_path) -> None:
        [github] = capabilities_for_tool("github.create_pr")
        path = tmp_path / "template.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": {
                        "github_pr": conformance_evidence_template(github),
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="outcome must be a non-empty string"):
            load_conformance_file(path)

    def test_conformance_file_requires_audit_detail(self, tmp_path) -> None:
        path = tmp_path / "missing-detail.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": {
                        "github_pr": {
                            "capability_fingerprint": "sha256:abc",
                            "scenarios": {
                                "happened": {
                                    "outcome": "happened",
                                    "receipt": {"number": 42, "head": "fix-login"},
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="detail must be a non-empty string"):
            load_conformance_file(path)

    def test_conformance_file_rejects_todo_audit_detail(self, tmp_path) -> None:
        path = tmp_path / "todo-detail.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": {
                        "github_pr": {
                            "capability_fingerprint": "sha256:abc",
                            "scenarios": {
                                "happened": {
                                    "outcome": "happened",
                                    "receipt": {"number": 42, "head": "fix-login"},
                                    "detail": "TODO: record how this was exercised",
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="detail must be filled in"):
            load_conformance_file(path)

    def test_conformance_file_rejects_bad_result_shape(self, tmp_path) -> None:
        path = tmp_path / "bad-conformance.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": {
                        "github_pr": {
                            "capability_fingerprint": "sha256:abc",
                            "scenarios": {"happened": {"outcome": "happened", "receipt": "pr_42"}},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="receipt must be an object or null"):
            load_conformance_file(path)

    def test_conformance_evidence_fails_without_capability_fingerprint(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        reports = validate_conformance_evidence(
            (github,),
            {
                "github_pr": CapabilityConformanceEvidence(
                    capability="github_pr",
                    source_args={"owner": "acme", "repo": "api", "head": "fix-login"},
                    observations={
                        "happened": ProbeResult(
                            ProbeOutcome.HAPPENED,
                            {"number": 42, "head": "fix-login"},
                        ),
                        "mature_miss": ProbeResult(ProbeOutcome.NOT_HAPPENED),
                        "ambiguous": ProbeResult(ProbeOutcome.INCONCLUSIVE),
                    },
                )
            },
        )

        assert reports[0].passed is False
        [failure] = [
            failure
            for failure in reports[0].failures
            if failure.scenario == "capability_fingerprint"
        ]
        assert failure.actual is None
        assert "missing capability_fingerprint" in failure.reason

    def test_conformance_evidence_requires_all_required_source_args(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        reports = validate_conformance_evidence(
            (github,),
            {
                "github_pr": CapabilityConformanceEvidence(
                    capability="github_pr",
                    capability_fingerprint=capability_fingerprint(github),
                    source_args={"head": "fix-login"},
                    observations={
                        "happened": ProbeResult(
                            ProbeOutcome.HAPPENED,
                            {"number": 42, "head": "fix-login"},
                        ),
                        "mature_miss": ProbeResult(ProbeOutcome.NOT_HAPPENED),
                        "ambiguous": ProbeResult(ProbeOutcome.INCONCLUSIVE),
                    },
                )
            },
        )

        assert reports[0].passed is False
        [failure] = [
            failure for failure in reports[0].failures if failure.scenario == "source_args"
        ]
        assert failure.expected == ("owner", "repo", "head")
        assert "missing: owner, repo" in failure.reason

    def test_conformance_evidence_requires_matching_capability_identity(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        reports = validate_conformance_evidence(
            (github,),
            {
                "github_pr": CapabilityConformanceEvidence(
                    capability="stripe",
                    capability_fingerprint=capability_fingerprint(github),
                    source_args={"owner": "acme", "repo": "api", "head": "fix-login"},
                    observations={
                        "happened": ProbeResult(
                            ProbeOutcome.HAPPENED,
                            {"number": 42, "head": "fix-login"},
                        ),
                        "mature_miss": ProbeResult(ProbeOutcome.NOT_HAPPENED),
                        "ambiguous": ProbeResult(ProbeOutcome.INCONCLUSIVE),
                    },
                )
            },
        )

        assert reports[0].passed is False
        [failure] = [failure for failure in reports[0].failures if failure.scenario == "capability"]
        assert failure.expected == ("github_pr",)
        assert failure.actual == "stripe"
        assert "does not match" in failure.reason

    def test_conformance_evidence_fails_when_capability_fingerprint_is_stale(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        reports = validate_conformance_evidence(
            (github,),
            {
                "github_pr": CapabilityConformanceEvidence(
                    capability="github_pr",
                    capability_fingerprint="sha256:stale",
                    source_args={"owner": "acme", "repo": "api", "head": "fix-login"},
                    observations={
                        "happened": ProbeResult(
                            ProbeOutcome.HAPPENED,
                            {"number": 42, "head": "fix-login"},
                        ),
                        "mature_miss": ProbeResult(ProbeOutcome.NOT_HAPPENED),
                        "ambiguous": ProbeResult(ProbeOutcome.INCONCLUSIVE),
                    },
                )
            },
        )

        assert reports[0].passed is False
        [failure] = [
            failure
            for failure in reports[0].failures
            if failure.scenario == "capability_fingerprint"
        ]
        assert failure.expected == (capability_fingerprint(github),)
        assert failure.actual == "sha256:stale"
        assert "does not match" in failure.reason

    def test_conformance_evidence_names_unknown_capabilities(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        reports = validate_conformance_evidence(
            (github,),
            {
                "typo": CapabilityConformanceEvidence(
                    capability="typo",
                    capability_fingerprint="sha256:abc",
                    observations={"happened": ProbeResult(ProbeOutcome.HAPPENED, {"id": "1"})},
                )
            },
        )

        assert reports[0].capability == "typo"
        assert reports[0].failures[0].reason == "no matching provider capability in the matrix"
