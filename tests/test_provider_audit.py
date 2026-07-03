from __future__ import annotations

import pytest

from openonce.providers.audit import (
    handler_contract_failures,
    receipt_contract_failures,
    scan_effect_tools,
    suggest_capabilities_for_refs,
)
from openonce.providers.capabilities import (
    CapabilityTier,
    ProviderCapability,
    capabilities_for_tool,
)


class TestProviderAudit:
    def test_scan_effect_tools_finds_supported_decorators(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            """
@oo.effect(tool="stripe.charge")
def charge():
    return {"stripe_id": "pi_1", "status": "succeeded"}

@effect_tool(oo, tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])
def create_pr(owner, repo, head):
    return {"number": 42, "head": head}

@effect_function_tool(oo, tool="email.send")
async def send_email():
    pass

TOOL = "stripe.refund"
@oo.effect(tool=TOOL)
def constant_tool():
    pass

@oo.effect(tool=build_tool())
def dynamic_tool():
    pass
""",
            encoding="utf-8",
        )

        refs = scan_effect_tools([path])

        assert [(ref.decorator, ref.tool, ref.function) for ref in refs] == [
            ("effect", "stripe.charge", "charge"),
            ("effect_tool", "github.create_pr", "create_pr"),
            ("effect_function_tool", "email.send", "send_email"),
            ("effect", "stripe.refund", "constant_tool"),
            ("effect", None, "dynamic_tool"),
        ]
        assert refs[-1].dynamic is True
        assert refs[-1].to_dict()["dynamic"] is True
        assert refs[1].function_args == ("owner", "repo", "head")
        assert refs[1].idempotency_fields == ("owner", "repo", "head")
        assert refs[0].return_field_sets == (("stripe_id", "status"),)
        assert refs[1].return_field_sets == (("number", "head"),)
        assert refs[1].return_field_sources == ((("head", "head"),),)
        assert refs[0].to_dict()["return_field_sets"] == [["stripe_id", "status"]]
        assert refs[1].to_dict()["return_field_sources"] == [{"head": "head"}]
        assert refs[1].to_dict()["reassigned_handler_args"] == []

    def test_scan_effect_tools_recurses_directories_and_skips_non_python(self, tmp_path) -> None:
        (tmp_path / "README.md").write_text('@oo.effect(tool="pay.charge")', encoding="utf-8")
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "app.py").write_text(
            '@oo.effect(tool="stripe.refund")\ndef refund():\n    pass\n',
            encoding="utf-8",
        )

        refs = scan_effect_tools([tmp_path])

        assert len(refs) == 1
        assert refs[0].tool == "stripe.refund"

    def test_scan_effect_tools_reports_bad_paths(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="scan path does not exist"):
            scan_effect_tools([tmp_path / "missing"])

    def test_scan_effect_tools_reports_syntax_errors(self, tmp_path) -> None:
        path = tmp_path / "broken.py"
        path.write_text("@oo.effect(tool='stripe.charge')\ndef nope(:\n", encoding="utf-8")

        with pytest.raises(ValueError, match="cannot parse Python"):
            scan_effect_tools([path])

    def test_scan_effect_tools_rejects_shadowed_tool_constants(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'TOOL = "github.create_pr"\n'
            "def TOOL():\n"
            "    return 'stripe.charge'\n"
            "@oo.effect(tool=TOOL)\n"
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )

        [ref] = scan_effect_tools([path])

        assert ref.tool is None
        assert ref.dynamic is True

    def test_scan_effect_tools_rejects_conditionally_rebound_tool_constants(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'TOOL = "github.create_pr"\n'
            "if use_stripe:\n"
            '    TOOL = "stripe.charge"\n'
            "@oo.effect(tool=TOOL)\n"
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )

        [ref] = scan_effect_tools([path])

        assert ref.tool is None
        assert ref.dynamic is True

    def test_scan_effect_tools_rejects_tool_constants_defined_after_decorator(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            "@oo.effect(tool=TOOL)\n"
            "def create_pr(owner, repo, head):\n"
            "    pass\n"
            'TOOL = "github.create_pr"\n',
            encoding="utf-8",
        )

        [ref] = scan_effect_tools([path])

        assert ref.tool is None
        assert ref.dynamic is True

    def test_scan_effect_tools_keeps_decorator_constants_when_later_rebound(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'TOOL = "github.create_pr"\n'
            "@oo.effect(tool=TOOL)\n"
            "def create_pr(owner, repo, head):\n"
            "    pass\n"
            'TOOL = "stripe.charge"\n',
            encoding="utf-8",
        )

        [ref] = scan_effect_tools([path])

        assert ref.tool == "github.create_pr"

    def test_handler_contract_passes_when_required_fields_are_present(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo", "head"])\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert handler_contract_failures(ref, (github,)) == ()

    def test_handler_contract_reports_missing_args_and_idempotency_fields(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr", idempotency_fields=["owner", "repo"])\n'
            "def create_pr(owner, repo):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        failures = handler_contract_failures(ref, (github,))

        assert len(failures) == 2
        assert "requires handler args: head" in failures[0]
        assert "requires idempotency_fields: head" in failures[1]

    def test_handler_contract_rejects_idempotency_fields_that_are_not_handler_args(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            "@oo.effect(\n"
            '    tool="slack.post_message",\n'
            '    idempotency_fields=["channel", "client_msg_id"],\n'
            ")\n"
            "def post_message(channel):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        capability = ProviderCapability(
            name="slack_metadata",
            tool_pattern="slack.post_message",
            tier=CapabilityTier.SENDER_CONTROLLED_AUTHORITATIVE,
            key_strategy="metadata client_msg_id",
            probe_basis="history by metadata",
            miss_semantics="not-happened after grace",
            can_auto_rearm_on_miss=True,
            default_grace_seconds=30.0,
            prober="SlackMetadataProber",
            handler_requirements=("stamp client_msg_id",),
            risk="medium",
            required_args=("channel",),
            required_idempotency_fields=("channel", "client_msg_id"),
            required_receipt_fields=("ts",),
        )

        failures = handler_contract_failures(ref, (capability,))

        assert failures == (
            f"{path}:1: 'slack.post_message' capability 'slack_metadata' "
            "requires idempotency field(s) that are not required_args: client_msg_id",
        )

    def test_handler_contract_rejects_unrequired_idempotency_field_even_when_arg_exists(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            "@oo.effect(\n"
            '    tool="slack.post_message",\n'
            '    idempotency_fields=["channel", "client_msg_id"],\n'
            ")\n"
            "def post_message(channel, client_msg_id):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        capability = ProviderCapability(
            name="slack_metadata",
            tool_pattern="slack.post_message",
            tier=CapabilityTier.SENDER_CONTROLLED_AUTHORITATIVE,
            key_strategy="metadata client_msg_id",
            probe_basis="history by metadata",
            miss_semantics="not-happened after grace",
            can_auto_rearm_on_miss=True,
            default_grace_seconds=30.0,
            prober="SlackMetadataProber",
            handler_requirements=("stamp client_msg_id",),
            risk="medium",
            required_args=("channel",),
            required_idempotency_fields=("channel", "client_msg_id"),
            required_receipt_fields=("ts",),
        )

        failures = handler_contract_failures(ref, (capability,))

        assert failures == (
            f"{path}:1: 'slack.post_message' capability 'slack_metadata' "
            "requires idempotency field(s) that are not required_args: client_msg_id",
        )

    def test_handler_contract_accepts_static_idempotency_field_constants(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'FIELDS = ["owner", "repo", "head"]\n'
            '@oo.effect(tool="github.create_pr", idempotency_fields=FIELDS)\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_idempotency_fields is False
        assert ref.idempotency_fields == ("owner", "repo", "head")
        assert handler_contract_failures(ref, (github,)) == ()

    def test_handler_contract_rejects_shadowed_idempotency_field_constants(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'FIELDS = ["owner", "repo", "head"]\n'
            "class FIELDS:\n"
            "    pass\n"
            '@oo.effect(tool="github.create_pr", idempotency_fields=FIELDS)\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.idempotency_fields is None
        assert ref.dynamic_idempotency_fields is True
        assert "requires literal idempotency_fields" in handler_contract_failures(ref, (github,))[0]

    def test_handler_contract_rejects_conditionally_rebound_idempotency_field_constants(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'FIELDS = ["owner", "repo", "head"]\n'
            "try:\n"
            '    FIELDS = ["owner", "repo"]\n'
            "except RuntimeError:\n"
            "    pass\n"
            '@oo.effect(tool="github.create_pr", idempotency_fields=FIELDS)\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.idempotency_fields is None
        assert ref.dynamic_idempotency_fields is True
        assert "requires literal idempotency_fields" in handler_contract_failures(ref, (github,))[0]

    def test_handler_contract_rejects_idempotency_field_constants_defined_after_decorator(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr", idempotency_fields=FIELDS)\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n"
            'FIELDS = ["owner", "repo", "head"]\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.idempotency_fields is None
        assert ref.dynamic_idempotency_fields is True
        assert "requires literal idempotency_fields" in handler_contract_failures(ref, (github,))[0]

    def test_handler_contract_reports_dynamic_idempotency_fields(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'FIELDS = list(["owner", "repo", "head"])\n'
            '@oo.effect(tool="github.create_pr", idempotency_fields=FIELDS)\n'
            "def create_pr(owner, repo, head):\n"
            "    pass\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_idempotency_fields is True
        assert "requires literal idempotency_fields" in handler_contract_failures(ref, (github,))[0]

    def test_receipt_contract_passes_when_literal_return_contains_fields(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, "head": head, "html_url": "https://example.test"}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is False
        assert ref.return_field_sets == (("number", "head", "html_url"),)
        assert ref.return_field_sources == ((("head", "head"),),)
        assert receipt_contract_failures(ref, (github,)) == ()

    def test_receipt_contract_accepts_local_literal_return_receipt(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head}\n'
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is False
        assert ref.return_field_sets == (("number", "head"),)
        assert ref.return_field_sources == ((("head", "head"),),)
        assert receipt_contract_failures(ref, (github,)) == ()

    def test_receipt_contract_rejects_local_receipt_assigned_after_return(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            "    return receipt\n"
            '    receipt = {"number": 42, "head": head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_rejects_branch_local_receipt_assignment(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head, ok):\n"
            "    if ok:\n"
            '        receipt = {"number": 42, "head": head}\n'
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_required_receipt_field_source_mismatch(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, "head": "acme:wrong"}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        failures = receipt_contract_failures(ref, (github,))

        assert failures == (
            f"{path}:1: 'github.create_pr' capability 'github_pr' "
            "return[0] receipt field 'head' must come from handler arg 'head'",
        )

    def test_receipt_contract_rejects_duplicate_literal_return_fields(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, "head": head, "head": "other-branch"}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is False
        assert ref.return_field_sets == (("number", "head", "head"),)
        assert receipt_contract_failures(ref, (github,)) == (
            f"{path}:1: 'github.create_pr' capability 'github_pr' "
            "return[0] contains duplicate receipt fields: head",
        )

    def test_receipt_contract_rejects_shadowed_field_constants(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'HEAD = "head"\n'
            "def HEAD():\n"
            "    return 'head'\n"
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, HEAD: head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_rejects_conditionally_rebound_field_constants(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            'HEAD = "head"\n'
            "match provider:\n"
            "    case 'github':\n"
            '        HEAD = "head"\n'
            "    case _:\n"
            '        HEAD = "branch"\n'
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, HEAD: head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_allows_stable_field_constants_defined_after_handler(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42, HEAD: head}\n'
            'HEAD = "head"\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is False
        assert ref.return_field_sets == (("number", "head"),)
        assert receipt_contract_failures(ref, (github,)) == ()

    def test_receipt_contract_rejects_local_variable_that_mimics_handler_arg(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo):\n"
            '    head = "fix-login"\n'
            '    return {"number": 42, "head": head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        failures = receipt_contract_failures(ref, (github,))

        assert failures == (
            f"{path}:1: 'github.create_pr' capability 'github_pr' "
            "return[0] receipt field 'head' must come from handler arg 'head', "
            "but the handler has no such arg",
        )

    def test_receipt_contract_rejects_reassigned_handler_arg_source(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    head = "fix-login"\n'
            '    return {"number": 42, "head": head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.reassigned_handler_args == ("head",)
        assert receipt_contract_failures(ref, (github,)) == (
            f"{path}:1: 'github.create_pr' capability 'github_pr' "
            "return[0] receipt field 'head' must come from handler arg 'head', "
            "but that arg is reassigned in the handler body",
        )

    def test_receipt_contract_rejects_match_rebound_handler_arg_source(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head, payload):\n"
            "    match payload:\n"
            '        case {"head": head}:\n'
            "            pass\n"
            '    return {"number": 42, "head": head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.reassigned_handler_args == ("head",)
        assert receipt_contract_failures(ref, (github,)) == (
            f"{path}:1: 'github.create_pr' capability 'github_pr' "
            "return[0] receipt field 'head' must come from handler arg 'head', "
            "but that arg is reassigned in the handler body",
        )

    @pytest.mark.parametrize(
        "mutation",
        [
            '    head["ref"] = "fix-login"\n',
            '    head.update({"ref": "fix-login"})\n',
        ],
    )
    def test_receipt_contract_rejects_mutated_handler_arg_source(
        self, tmp_path, mutation: str
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            f"{mutation}"
            '    return {"number": 42, "head": head}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.reassigned_handler_args == ("head",)
        assert (
            "arg 'head', but that arg is reassigned" in receipt_contract_failures(ref, (github,))[0]
        )

    def test_receipt_contract_accepts_literal_dict_constructor_return(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="stripe.charge")\n'
            "def charge():\n"
            '    return dict(stripe_id="pi_1", status="succeeded")\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [stripe] = capabilities_for_tool("stripe.charge")

        assert ref.dynamic_return is False
        assert ref.return_field_sets == (("stripe_id", "status"),)
        assert receipt_contract_failures(ref, (stripe,)) == ()

    def test_receipt_contract_reports_missing_return_fields(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    return {"number": 42}\n',
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        failures = receipt_contract_failures(ref, (github,))

        assert failures == (
            f"{path}:1: 'github.create_pr' capability 'github_pr' "
            "return[0] is missing receipt fields: head",
        )

    def test_receipt_contract_reports_reassigned_return_receipt(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head}\n'
            '    receipt = {"number": 43, "head": head}\n'
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_destructured_return_receipt_reassignment(
        self, tmp_path
    ) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head}\n'
            "    receipt, _unused = fetch_receipt()\n"
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_mutated_return_receipt(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head}\n'
            '    receipt["head"] = "rewritten"\n'
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_method_mutated_return_receipt(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head}\n'
            '    receipt.pop("head")\n'
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_update_mutated_return_receipt(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head):\n"
            '    receipt = {"number": 42, "head": head}\n'
            '    receipt.update({"html_url": "https://example.test"})\n'
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_match_rebound_return_receipt(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="github.create_pr")\n'
            "def create_pr(owner, repo, head, payload):\n"
            '    receipt = {"number": 42, "head": head}\n'
            "    match payload:\n"
            '        case {"receipt": receipt}:\n'
            "            pass\n"
            "    return receipt\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [github] = capabilities_for_tool("github.create_pr")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (github,))[0]

    def test_receipt_contract_reports_dynamic_returns(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="stripe.charge")\n'
            "def charge(client):\n"
            "    return client.create_payment_intent()\n",
            encoding="utf-8",
        )
        [ref] = scan_effect_tools([path])
        [stripe] = capabilities_for_tool("stripe.charge")

        assert ref.dynamic_return is True
        assert "requires literal return dicts" in receipt_contract_failures(ref, (stripe,))[0]

    def test_suggest_capabilities_for_unknown_literal_tools(self, tmp_path) -> None:
        path = tmp_path / "app.py"
        path.write_text(
            '@oo.effect(tool="pay.charge")\ndef charge():\n    pass\n'
            '@oo.effect(tool="pay.charge")\ndef charge_again():\n    pass\n'
            '@oo.effect(tool="stripe.charge")\ndef stripe_charge():\n    pass\n',
            encoding="utf-8",
        )
        refs = scan_effect_tools([path])
        [stripe] = capabilities_for_tool("stripe.charge")

        [suggestion] = suggest_capabilities_for_refs(refs, (stripe,))

        assert suggestion.tool == "pay.charge"
        assert len(suggestion.refs) == 2
        assert suggestion.capability["name"] == "pay_charge"
        assert suggestion.capability["tool_pattern"] == "pay.charge"
        assert suggestion.capability["tier"] == "tier_3_non_authoritative"
        assert suggestion.capability["can_auto_rearm_on_miss"] is False
        assert "TODO" in suggestion.capability["probe_basis"]
