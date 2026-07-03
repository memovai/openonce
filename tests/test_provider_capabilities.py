from __future__ import annotations

import json
from dataclasses import replace

from openonce.providers.capabilities import (
    CapabilityTier,
    builtin_capabilities,
    can_auto_rearm_on_miss,
    capabilities_for_tool,
    capability_fingerprint,
    capability_from_dict,
    capability_guidance_for_tool,
    capability_matrix,
    capability_readiness_errors,
    default_grace_for_tool,
    load_capabilities_file,
    provider_receipt_contract_failures,
    validate_capability_matrix,
)


def custom_slack_capability() -> dict[str, object]:
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


class TestProviderCapabilityMatrix:
    def test_stripe_documents_the_tier_1_backstop(self) -> None:
        [stripe] = capabilities_for_tool("stripe.charge")

        assert stripe.name == "stripe"
        assert stripe.tier is CapabilityTier.NATIVE_IDEMPOTENCY_KEY
        assert stripe.can_auto_rearm_on_miss is True
        assert stripe.default_grace_seconds == 120.0
        assert "Idempotency-Key" in stripe.handler_requirements[0]

    def test_github_pr_uses_an_authoritative_natural_key(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")

        assert github.name == "github_pr"
        assert github.tier is CapabilityTier.NATURAL_BUSINESS_KEY
        assert github.can_auto_rearm_on_miss is True
        assert "owner/repo/head" in github.key_strategy

    def test_email_exposes_both_authoritative_and_smtp_modes(self) -> None:
        email = {cap.name: cap for cap in capabilities_for_tool("email.send")}

        assert email["email_provider_api"].can_auto_rearm_on_miss is True
        assert email["email_smtp"].can_auto_rearm_on_miss is False
        assert "inconclusive forever" in email["email_smtp"].miss_semantics

    def test_capability_filter_disambiguates_shared_tool_names(self) -> None:
        [authoritative] = capabilities_for_tool("email.send", "email_provider_api")
        [smtp] = capabilities_for_tool("email.send", "email_smtp")

        assert authoritative.can_auto_rearm_on_miss is True
        assert smtp.can_auto_rearm_on_miss is False
        assert capabilities_for_tool("email.send", "nope") == ()

    def test_non_authoritative_capabilities_never_auto_rearm_on_miss(self) -> None:
        risky = [
            cap for cap in builtin_capabilities() if cap.tier is CapabilityTier.NON_AUTHORITATIVE
        ]

        assert risky
        assert all(not cap.can_auto_rearm_on_miss for cap in risky)

    def test_guidance_explains_known_tool_requirements(self) -> None:
        guidance = capability_guidance_for_tool("stripe.charge")

        assert "StripeProber" in guidance
        assert "Idempotency-Key" in guidance
        assert "auto_rearm_on_miss=yes" in guidance
        assert "receipt_fields=stripe_id" in guidance

    def test_guidance_includes_structured_handler_and_receipt_contracts(self) -> None:
        guidance = capability_guidance_for_tool("github.create_pr")

        assert "handler_args=owner,repo,head" in guidance
        assert "idempotency_fields=owner,repo,head" in guidance
        assert "receipt_fields=number,head" in guidance
        assert "receipt_sources=head<-head" in guidance

    def test_guidance_for_unknown_tool_stays_conservative(self) -> None:
        guidance = capability_guidance_for_tool("slack.post_message")

        assert "No provider capability" in guidance
        assert "resolve the effect manually" in guidance

    def test_receipt_contract_failures_are_empty_for_unknown_tools(self) -> None:
        assert provider_receipt_contract_failures("custom.tool", {}, "ok") == ()

    def test_receipt_contract_failures_require_declared_evidence_fields(self) -> None:
        failures = provider_receipt_contract_failures(
            "stripe.charge",
            {},
            {"status": "succeeded"},
        )

        assert failures == ("missing required external evidence field(s): stripe_id",)

    def test_receipt_contract_failures_check_receipt_source_contracts(self) -> None:
        failures = provider_receipt_contract_failures(
            "github.create_pr",
            {"owner": "acme", "repo": "api", "head": "fix-login"},
            {"number": 42, "head": "other-branch"},
            "github_pr",
        )

        assert failures == (
            "github_pr.head expected 'fix-login' from arg 'head', got 'other-branch'",
        )

    def test_receipt_contract_failures_require_declared_handler_args(self) -> None:
        failures = provider_receipt_contract_failures(
            "github.create_pr",
            {"head": "fix-login"},
            {"number": 42, "head": "fix-login"},
            "github_pr",
        )

        assert failures == ("missing required handler arg(s): owner, repo",)

    def test_default_grace_comes_from_provider_capabilities(self) -> None:
        assert default_grace_for_tool("stripe.charge") == 120.0
        assert default_grace_for_tool("github.create_pr") == 0.0
        assert default_grace_for_tool("slack.post_message") is None

    def test_auto_rearm_is_blocked_if_any_matching_capability_is_non_authoritative(
        self,
    ) -> None:
        assert can_auto_rearm_on_miss("stripe.charge") is True
        assert can_auto_rearm_on_miss("github.create_pr") is True
        assert can_auto_rearm_on_miss("email.send") is False
        assert can_auto_rearm_on_miss("email.send", "email_provider_api") is True
        assert can_auto_rearm_on_miss("email.send", "email_smtp") is False
        assert can_auto_rearm_on_miss("email.send", "nope") is False
        assert can_auto_rearm_on_miss("unknown.tool") is True

    def test_load_custom_capability_file_extends_the_matrix(self, tmp_path) -> None:
        path = tmp_path / "openonce-providers.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": [custom_slack_capability()],
                }
            ),
            encoding="utf-8",
        )

        extra = load_capabilities_file(path)
        matrix = capability_matrix(extra)
        [slack] = capabilities_for_tool("slack.post_message", capabilities=matrix)

        assert slack.name == "slack_metadata"
        assert slack.can_auto_rearm_on_miss is True
        assert default_grace_for_tool("slack.post_message", capabilities=matrix) == 30.0
        assert can_auto_rearm_on_miss("slack.post_message", capabilities=matrix) is True
        assert slack.required_args == ()
        assert slack.required_idempotency_fields == ()
        assert slack.required_receipt_fields == ("ts",)
        assert slack.required_receipt_source_fields == ()

    def test_load_custom_capability_file_can_define_handler_contract(self, tmp_path) -> None:
        path = tmp_path / "openonce-providers.json"
        payload = custom_slack_capability()
        payload["required_args"] = ["channel", "text"]
        payload["required_idempotency_fields"] = ["channel", "text"]
        payload["required_receipt_fields"] = ["ts"]
        path.write_text(
            json.dumps({"schema_version": 1, "capabilities": [payload]}),
            encoding="utf-8",
        )

        [slack] = load_capabilities_file(path)

        assert slack.required_args == ("channel", "text")
        assert slack.required_idempotency_fields == ("channel", "text")
        assert slack.required_receipt_fields == ("ts",)
        assert slack.required_receipt_source_fields == ()

    def test_load_custom_capability_file_can_define_receipt_source_contract(self, tmp_path) -> None:
        path = tmp_path / "openonce-providers.json"
        payload = custom_slack_capability()
        payload["required_args"] = ["channel", "client_msg_id"]
        payload["required_idempotency_fields"] = ["channel", "client_msg_id"]
        payload["required_receipt_fields"] = ["ts", "client_msg_id"]
        payload["required_receipt_source_fields"] = {"client_msg_id": "client_msg_id"}
        path.write_text(
            json.dumps({"schema_version": 1, "capabilities": [payload]}),
            encoding="utf-8",
        )

        [slack] = load_capabilities_file(path)

        assert slack.required_receipt_source_fields == (("client_msg_id", "client_msg_id"),)

    def test_custom_capability_file_validation_names_bad_fields(self, tmp_path) -> None:
        path = tmp_path / "bad-providers.json"
        bad = custom_slack_capability()
        bad["default_grace_seconds"] = -1
        path.write_text(
            json.dumps({"schema_version": 1, "capabilities": [bad]}),
            encoding="utf-8",
        )

        try:
            load_capabilities_file(path)
        except ValueError as exc:
            assert "capabilities[0]" in str(exc)
            assert "default_grace_seconds must be non-negative" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected invalid capability file to fail")

    def test_capability_matrix_rejects_duplicate_names(self) -> None:
        duplicate = custom_slack_capability()
        duplicate["name"] = "stripe"
        extra = capability_from_dict(duplicate)

        errors = validate_capability_matrix((*builtin_capabilities(), extra))
        assert len(errors) == 1
        assert "duplicate provider capability name 'stripe'" in errors[0]

        try:
            capability_matrix((extra,))
        except ValueError as exc:
            assert "duplicate provider capability name 'stripe'" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate capability name to fail")

    def test_non_authoritative_capability_cannot_auto_rearm(self) -> None:
        bad = custom_slack_capability()
        bad["tier"] = "tier_3_non_authoritative"
        bad["can_auto_rearm_on_miss"] = True
        extra = capability_from_dict(bad)

        errors = validate_capability_matrix((extra,))

        assert len(errors) == 1
        assert "non-authoritative" in errors[0]
        assert "cannot auto-rearm" in errors[0]

    def test_receipt_source_contract_must_reference_declared_fields(self) -> None:
        bad = custom_slack_capability()
        bad["required_args"] = ["channel"]
        bad["required_receipt_fields"] = ["ts"]
        bad["required_receipt_source_fields"] = {"message_id": "client_msg_id"}
        extra = capability_from_dict(bad)

        errors = validate_capability_matrix((extra,))

        assert len(errors) == 3
        assert "receipt source field 'message_id' is not in required_receipt_fields" in errors[0]
        assert "receipt source 'client_msg_id' is not in required_args" in errors[1]
        assert "receipt source 'client_msg_id' is not in required_idempotency_fields" in errors[2]

    def test_idempotency_fields_must_reference_declared_handler_args(self) -> None:
        bad = custom_slack_capability()
        bad["required_args"] = ["channel"]
        bad["required_idempotency_fields"] = ["channel", "client_msg_id"]
        extra = capability_from_dict(bad)

        errors = validate_capability_matrix((extra,))

        assert errors == (
            "provider capability 'slack_metadata' idempotency field "
            "'client_msg_id' is not in required_args",
        )

    def test_capability_matrix_rejects_duplicate_contract_fields(self) -> None:
        bad = custom_slack_capability()
        bad["required_args"] = ["channel", "channel"]
        bad["required_idempotency_fields"] = ["channel", "channel"]
        bad["required_receipt_fields"] = ["ts", "ts"]
        extra = capability_from_dict(bad)

        errors = validate_capability_matrix((extra,))

        assert errors == (
            "provider capability 'slack_metadata' required_args contains "
            "duplicate field(s): channel",
            "provider capability 'slack_metadata' required_idempotency_fields "
            "contains duplicate field(s): channel",
            "provider capability 'slack_metadata' required_receipt_fields "
            "contains duplicate field(s): ts",
        )

    def test_capability_matrix_rejects_duplicate_receipt_source_fields(self) -> None:
        [github] = capabilities_for_tool("github.create_pr")
        bad = replace(
            github,
            required_receipt_source_fields=(("head", "head"), ("head", "head")),
        )

        errors = validate_capability_matrix((bad,))

        assert errors == (
            "provider capability 'github_pr' required_receipt_source_fields "
            "contains duplicate receipt field(s): head",
        )

    def test_readiness_errors_keep_suggested_stubs_out_of_release_gates(self) -> None:
        draft = custom_slack_capability()
        draft["key_strategy"] = "TODO: document provider key"
        draft["probe_basis"] = "TODO: document probe source"
        draft["handler_requirements"] = ["TODO: stamp provider key"]
        draft["risk"] = "TODO: document false-miss behavior"
        cap = capability_from_dict(draft)

        errors = capability_readiness_errors((cap,))

        assert "slack_metadata: key_strategy still contains TODO" in errors
        assert "slack_metadata: probe_basis still contains TODO" in errors
        assert "slack_metadata: handler_requirements[0] still contains TODO" in errors
        assert "slack_metadata: risk still contains TODO" in errors

    def test_readiness_requires_auto_rearm_capabilities_to_name_a_prober(self) -> None:
        draft = custom_slack_capability()
        draft["prober"] = None
        cap = capability_from_dict(draft)

        assert capability_readiness_errors((cap,)) == (
            "slack_metadata: auto-rearm capabilities must name a prober",
        )

    def test_readiness_requires_probers_to_define_receipt_fields(self) -> None:
        draft = custom_slack_capability()
        draft["required_receipt_fields"] = []
        cap = capability_from_dict(draft)

        assert capability_readiness_errors((cap,)) == (
            "slack_metadata: prober capabilities must define required_receipt_fields",
        )

    def test_custom_capability_file_rejects_duplicate_names(self, tmp_path) -> None:
        path = tmp_path / "duplicate-providers.json"
        duplicate = custom_slack_capability()
        duplicate["tool_pattern"] = "slack.chat_postMessage"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "capabilities": [custom_slack_capability(), duplicate],
                }
            ),
            encoding="utf-8",
        )

        try:
            load_capabilities_file(path)
        except ValueError as exc:
            assert "duplicate provider capability name 'slack_metadata'" in str(exc)
            assert "slack.post_message" in str(exc)
            assert "slack.chat_postMessage" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate capability name to fail")

    def test_capability_dict_is_stable_and_json_ready(self) -> None:
        [stripe] = capabilities_for_tool("stripe.charge")

        payload = stripe.to_dict()
        assert payload == {
            "name": "stripe",
            "tool_pattern": "stripe.*",
            "tier": "tier_1_native_idempotency_key",
            "key_strategy": "provider idempotency key",
            "probe_basis": 'metadata["openonce_effect_id"] search',
            "miss_semantics": "inconclusive inside indexing lag; not-happened after grace",
            "can_auto_rearm_on_miss": True,
            "default_grace_seconds": 120.0,
            "prober": "StripeProber",
            "handler_requirements": [
                "pass current_effect().provider_key as Stripe Idempotency-Key",
                "stamp effect_metadata(current_effect()) on the created object",
            ],
            "risk": "lowest: provider-key replay is the duplicate-charge backstop",
            "required_args": [],
            "required_idempotency_fields": [],
            "required_receipt_fields": ["stripe_id"],
            "required_receipt_source_fields": {},
        }

    def test_capability_fingerprint_normalizes_numeric_grace(self) -> None:
        cap = capability_from_dict(custom_slack_capability())
        direct_with_int_grace = replace(cap, default_grace_seconds=30)

        assert direct_with_int_grace.to_dict()["default_grace_seconds"] == 30.0
        assert capability_fingerprint(direct_with_int_grace) == capability_fingerprint(cap)
