"""openonce CLI — the receipts, visible.

    openonce --db openonce.db ls                 # all effects, newest last
    openonce --db openonce.db ls --state unknown,human_review
    openonce --db openonce.db review             # the human queue
    openonce --db openonce.db show eff_abc123    # record + full journal
    openonce --db openonce.db approve eff_abc123 --by eric
    openonce --db openonce.db resolve-happened eff_abc123 --receipt-json '{"id":"x"}'
    openonce --db openonce.db resolve-not-happened eff_abc123 --reason "not in provider"
    openonce --db openonce.db deny eff_abc123 --reason "wrong customer"
    openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch

``--db`` accepts a SQLite path or a Postgres DSN (``postgres://...`` or
``host=... dbname=...``). Read paths never mutate; approve/deny/reconcile
and manual resolution commands are the only writes.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .records import EffectRecord
from .state import TERMINAL, EffectState
from .store.base import Store

if TYPE_CHECKING:
    from .providers.audit import EffectToolReference
    from .providers.capabilities import ProviderCapability
    from .providers.conformance import ConformanceReport

_ALL_STATES = frozenset(EffectState)


def open_store(db: str) -> Store:
    """SQLite path or Postgres DSN — pick by shape, not by flag."""
    if db.startswith(("postgres://", "postgresql://")) or (
        "=" in db and "/" not in db.split("=")[0]
    ):
        from .store.postgres import PostgresStore

        return PostgresStore(db)
    from .store.sqlite import SQLiteStore

    return SQLiteStore(db)


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _row(rec: EffectRecord) -> str:
    marker = " " if rec.state in TERMINAL else "*"
    return (
        f"{marker} {rec.effect_id}  {rec.state.value:<17}  {rec.tool:<24}  "
        f"attempt {rec.attempt}/{rec.max_attempts}  {_ts(rec.updated_at)}"
    )


def _parse_states(raw: str | None) -> frozenset[EffectState]:
    if raw is None:
        return _ALL_STATES
    try:
        return frozenset(EffectState(s.strip()) for s in raw.split(","))
    except ValueError:
        valid = ", ".join(s.value for s in EffectState)
        raise SystemExit(f"invalid --state; valid states: {valid}") from None


def cmd_ls(store: Store, args: argparse.Namespace) -> int:
    recs = store.scan_states(_parse_states(args.state), updated_before=float("inf"))
    for rec in recs:
        print(_row(rec))
    if not recs:
        print("(no effects)")
    return 0


def cmd_review(store: Store, args: argparse.Namespace) -> int:
    states = {EffectState.REQUIRES_APPROVAL, EffectState.HUMAN_REVIEW, EffectState.UNKNOWN}
    recs = store.scan_states(states, updated_before=float("inf"))
    if not recs:
        print("review queue is empty")
        return 0
    for rec in recs:
        print(_row(rec))
        if rec.note:
            print(f"    note: {rec.note}")
    print(
        f"\n{len(recs)} effect(s) need a human. Use approve for pending approvals, "
        "resolve-happened to commit evidence, resolve-not-happened to retry, "
        "or deny pending approvals."
    )
    return 0


def cmd_show(store: Store, args: argparse.Namespace) -> int:
    rec = store.get(args.effect_id)
    if rec is None:
        print(f"no effect {args.effect_id!r}", file=sys.stderr)
        return 1
    print(_row(rec))
    print(f"  scope:        {rec.scope}")
    print(f"  key:          {rec.idempotency_key}")
    print(f"  provider_key: {rec.provider_key}")
    print(f"  args:         {rec.args_json}")
    if rec.result is not None:
        outcome = "ok" if rec.result.ok else f"failed ({rec.result.error_type})"
        displayed = rec.result.value if rec.result.ok else rec.result.error
        print(f"  result:       {outcome}: {json.dumps(displayed)}")
    if rec.note:
        print(f"  note:         {rec.note}")
    print("  journal:")
    for e in store.journal(rec.effect_id):
        frm = e.from_state.value if e.from_state else "∅"
        extra = f"  {json.dumps(e.payload)}" if e.payload else ""
        print(f"    {_ts(e.at)}  {frm:>17} -> {e.to_state.value}{extra}")
    return 0


def cmd_approve(store: Store, args: argparse.Namespace) -> int:
    rec = store.transition(
        args.effect_id,
        {EffectState.REQUIRES_APPROVAL},
        EffectState.APPROVED,
        payload={"approved_by": args.by, "via": "cli"},
    )
    if rec is None:
        print(f"{args.effect_id} is not awaiting approval", file=sys.stderr)
        return 1
    print(f"approved {rec.effect_id} — the agent's next identical call will execute it")
    return 0


def cmd_deny(store: Store, args: argparse.Namespace) -> int:
    rec = store.transition(
        args.effect_id,
        {EffectState.REQUIRES_APPROVAL},
        EffectState.DENIED,
        payload={"denied_by": args.by, "reason": args.reason, "via": "cli"},
        set_fields={"note": args.reason or None},
    )
    if rec is None:
        print(f"{args.effect_id} is not awaiting approval", file=sys.stderr)
        return 1
    print(f"denied {rec.effect_id}")
    return 0


def cmd_resolve_happened(store: Store, args: argparse.Namespace) -> int:
    from .client import OpenOnce
    from .providers.capabilities import load_capabilities_file

    try:
        receipt = _load_receipt_arg(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        extra_capabilities = (
            load_capabilities_file(args.capability_file) if args.capability_file else ()
        )
        provider_capabilities = _parse_provider_capability_pins(args.provider_capability)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        rec = OpenOnce(
            store,
            extra_capabilities=extra_capabilities,
            provider_capabilities=provider_capabilities,
            enforce_provider_receipts=args.require_receipt_contract,
            require_provider_capability_for_receipts=args.require_receipt_contract,
        ).resolve_happened(
            args.effect_id,
            receipt=receipt,
            by=args.by,
            reason=args.reason,
            require_provider_capability=args.require_receipt_contract,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"resolved {rec.effect_id} as happened — committed with manual receipt")
    return 0


def cmd_resolve_not_happened(store: Store, args: argparse.Namespace) -> int:
    from .client import OpenOnce
    from .providers.capabilities import load_capabilities_file

    try:
        extra_capabilities = (
            load_capabilities_file(args.capability_file) if args.capability_file else ()
        )
        provider_capabilities = _parse_provider_capability_pins(args.provider_capability)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        rec = OpenOnce(
            store,
            extra_capabilities=extra_capabilities,
            provider_capabilities=provider_capabilities,
        ).resolve_not_happened(
            args.effect_id,
            by=args.by,
            reason=args.reason,
            require_auto_rearm=args.require_auto_rearm,
            require_provider_capability=args.require_provider_capability,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if rec.state is EffectState.APPROVED:
        print(f"resolved {rec.effect_id} as not happened — re-armed for retry")
    else:
        print(f"resolved {rec.effect_id} as not happened — attempts exhausted")
    return 0


def cmd_reconcile(store: Store, args: argparse.Namespace) -> int:
    from .providers.capabilities import load_capabilities_file
    from .providers.conformance import load_conformance_file
    from .reconciler import Reconciler

    probers = {}
    if args.probers:
        module_path, _, attr = args.probers.partition(":")
        if not attr:
            print("--probers must be module.path:ATTR (a dict[str, Prober])", file=sys.stderr)
            return 2
        try:
            probers = getattr(importlib.import_module(module_path), attr)
        except (ImportError, AttributeError) as exc:
            print(f"cannot load probers from {args.probers!r}: {exc}", file=sys.stderr)
            return 2
    if args.require_conformance and not args.conformance_file:
        print("--require-conformance requires --conformance-file", file=sys.stderr)
        return 2
    try:
        conformance_evidence = (
            load_conformance_file(args.conformance_file) if args.conformance_file else None
        )
    except (OSError, ValueError) as exc:
        print(f"cannot configure provider conformance: {exc}", file=sys.stderr)
        return 2

    try:
        extra_capabilities = (
            load_capabilities_file(args.capability_file) if args.capability_file else ()
        )
        provider_capabilities = _parse_provider_capability_pins(args.provider_capability)
        rec = Reconciler(
            store,
            probers=dict(probers),
            extra_capabilities=extra_capabilities,
            provider_capabilities=provider_capabilities,
            require_capability_for_resolution=args.require_provider_capability,
            conformance_evidence=conformance_evidence,
            require_conformance_for_resolution=args.require_conformance,
            grace_seconds=args.grace,
        )
    except (OSError, ValueError) as exc:
        print(f"cannot configure provider capabilities: {exc}", file=sys.stderr)
        return 2

    loops = 0
    while True:
        report = rec.run_once()
        if report.total() or not args.watch:
            print(
                f"reconciled: committed={len(report.committed)} "
                f"rearmed={len(report.rearmed)} failed={len(report.failed)} "
                f"escalated={len(report.escalated)}"
            )
            for eid in report.escalated:
                print(f"  needs a human: {eid}")
        loops += 1
        if not args.watch or (args.max_loops and loops >= args.max_loops):
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:  # pragma: no cover
            return 0


def _parse_provider_capability_pins(raw: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for value in raw:
        tool, sep, capability = value.partition("=")
        if sep != "=" or not tool or not capability:
            raise ValueError(
                "--provider-capability must be TOOL=CAPABILITY, "
                "for example email.send=email_provider_api"
            )
        if tool in pins:
            raise ValueError(f"duplicate provider capability pin for tool {tool!r}")
        pins[tool] = capability
    return pins


def _provider_policy(
    tool: str | None,
    capability: str | None,
    rows: tuple[ProviderCapability, ...],
    *,
    require_known: bool,
    require_auto_rearm: bool,
) -> tuple[dict[str, object] | None, list[str]]:
    if not require_known and not require_auto_rearm:
        return None, []

    required: list[str] = []
    checks: dict[str, dict[str, object]] = {}
    failures: list[str] = []

    def add_check(name: str, *, passed: bool, reason: str) -> None:
        checks[name] = {"passed": passed, "reason": reason}
        if not passed:
            failures.append(reason)

    if require_known:
        required.append("known")
    if require_auto_rearm:
        required.append("auto_rearm_on_miss")

    if tool is None:
        reason = "provider policy gates require a tool argument"
        for name in required:
            add_check(name, passed=False, reason=reason)
        return {"passed": False, "required": required, "checks": checks}, failures

    if require_known:
        if rows:
            add_check(
                "known",
                passed=True,
                reason=f"{len(rows)} provider capability match(es)",
            )
        else:
            add_check(
                "known",
                passed=False,
                reason=_no_provider_capability_message(tool, capability),
            )

    if require_auto_rearm:
        if not rows:
            add_check(
                "auto_rearm_on_miss",
                passed=False,
                reason=(
                    f"cannot prove auto_rearm_on_miss for {tool!r}: "
                    f"{_no_provider_capability_message(tool, capability)}"
                ),
            )
        else:
            blockers = [cap.name for cap in rows if not cap.can_auto_rearm_on_miss]
            if blockers:
                add_check(
                    "auto_rearm_on_miss",
                    passed=False,
                    reason=(
                        f"{tool!r} is not safe to auto-rearm on a miss; "
                        f"blocking capabilities: {', '.join(blockers)}"
                    ),
                )
            else:
                add_check(
                    "auto_rearm_on_miss",
                    passed=True,
                    reason="all matching capabilities allow post-grace auto-rearm",
                )

    return {"passed": not failures, "required": required, "checks": checks}, failures


def _no_provider_capability_message(tool: str | None, capability: str | None) -> str:
    if tool is None:
        if capability is not None:
            return f"no provider capability named {capability!r}"
        return "no provider capability matches"
    if capability is not None:
        return f"no provider capability {capability!r} matches {tool!r}"
    return f"no provider capability matches {tool!r}"


def _print_provider_policy(failures: list[str]) -> None:
    print("\npolicy: failed" if failures else "\npolicy: passed")
    for failure in failures:
        print(f"  - {failure}")


def _print_provider_validation(
    *,
    capability_file: str | None,
    custom_count: int,
    total_count: int,
    as_json: bool,
    conformance: dict[str, object] | None = None,
    conformance_failures: list[str] | None = None,
    readiness: dict[str, object] | None = None,
    readiness_failures: list[str] | None = None,
) -> int:
    failures = [*(conformance_failures or []), *(readiness_failures or [])]
    payload: dict[str, object] = {
        "schema_version": 1,
        "capability_file": capability_file,
        "valid": True,
        "capability_count": total_count,
        "custom_capability_count": custom_count,
    }
    if conformance is not None:
        payload["conformance"] = conformance
        payload["valid"] = not failures
    if readiness is not None:
        payload["readiness"] = readiness
        payload["valid"] = not failures
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        source = capability_file or "built-ins only"
        print(
            f"provider capability matrix valid: {total_count} capability(ies), "
            f"{custom_count} custom from {source}"
        )
        if conformance is not None:
            _print_provider_conformance(conformance_failures or [])
        if readiness is not None:
            _print_provider_readiness(readiness_failures or [])
    return 1 if failures else 0


def _provider_capability_payload(
    cap: ProviderCapability,
    *,
    include_conformance_plan: bool,
    include_conformance_template: bool,
) -> dict[str, object]:
    from .providers.capabilities import capability_fingerprint

    payload = cap.to_dict()
    payload["fingerprint"] = capability_fingerprint(cap)
    if include_conformance_plan or include_conformance_template:
        from .providers.conformance import conformance_evidence_template, conformance_plan

    if include_conformance_plan:
        payload["conformance_plan"] = [case.to_dict() for case in conformance_plan(cap)]
    if include_conformance_template:
        payload["conformance_template"] = conformance_evidence_template(cap)
    return payload


def _provider_conformance_file_template_payload(
    rows: tuple[ProviderCapability, ...],
) -> dict[str, object]:
    from .providers.conformance import conformance_evidence_file_template

    return conformance_evidence_file_template(rows)


def _provider_readiness_payload(
    rows: tuple[ProviderCapability, ...],
    *,
    tool: str | None = None,
    capability: str | None = None,
    require_reviewed: bool,
) -> tuple[dict[str, object] | None, list[str]]:
    if not require_reviewed:
        return None, []
    from .providers.capabilities import capability_readiness_errors

    failures = list(capability_readiness_errors(rows))
    if not rows:
        failures.append(
            "cannot prove provider capability readiness for a dynamic tool expression"
            if tool is None
            else (
                f"cannot prove provider capability readiness for {tool!r}: "
                f"{_no_provider_capability_message(tool, capability)}"
            )
        )
    return (
        {
            "required": True,
            "passed": not failures,
            "failures": failures,
        },
        failures,
    )


def _provider_conformance_payload(
    matrix: tuple[ProviderCapability, ...],
    rows: tuple[ProviderCapability, ...],
    *,
    tool: str | None = None,
    capability: str | None = None,
    conformance_file: str | None,
    require_conformance: bool,
) -> tuple[dict[str, object] | None, list[str]]:
    if conformance_file is None:
        if not require_conformance:
            return None, []
        return (
            {
                "file": None,
                "required": True,
                "passed": False,
                "reports": [],
            },
            ["--require-conformance requires --conformance-file"],
        )

    from .providers.conformance import load_conformance_file, validate_conformance_evidence

    if require_conformance and not rows:
        failure = (
            "cannot prove conformance for a dynamic tool expression"
            if tool is None
            else (
                f"cannot prove conformance for {tool!r}: "
                f"{_no_provider_capability_message(tool, capability)}"
            )
        )
        return (
            {
                "file": conformance_file,
                "required": True,
                "passed": False,
                "reports": [],
            },
            [failure],
        )

    evidence = load_conformance_file(conformance_file)
    reports = validate_conformance_evidence(
        matrix,
        evidence,
        required_capabilities=tuple(cap.name for cap in rows),
    )
    failures = _provider_conformance_failures(reports)
    return (
        {
            "file": conformance_file,
            "required": require_conformance,
            "passed": not failures,
            "reports": [report.to_dict() for report in reports],
        },
        failures,
    )


def _provider_conformance_failures(reports: tuple[ConformanceReport, ...]) -> list[str]:
    failures: list[str] = []
    for report in reports:
        for failure in report.failures:
            failures.append(f"{report.capability}.{failure.scenario}: {failure.reason}")
    return failures


def _print_provider_conformance(failures: list[str]) -> None:
    print("\nconformance: failed" if failures else "\nconformance: passed")
    for failure in failures:
        print(f"  - {failure}")


def _print_provider_readiness(failures: list[str]) -> None:
    print("\nreadiness: failed" if failures else "\nreadiness: passed")
    for failure in failures:
        print(f"  - {failure}")


def _print_provider_conformance_template(cap: ProviderCapability) -> None:
    from .providers.capabilities import capability_fingerprint
    from .providers.conformance import conformance_plan

    print("  conformance evidence template:")
    print(f"    capability_fingerprint: {capability_fingerprint(cap)}")
    if cap.required_args:
        print(f"    source_args required: {', '.join(cap.required_args)}")
    for case in conformance_plan(cap):
        expected = "|".join(outcome.value for outcome in case.expected_outcomes)
        receipt = " + receipt" if case.receipt_required else ""
        print(f"    - {case.scenario.value}: fill outcome={expected}{receipt}")
        if case.required_receipt_fields:
            print(f"      receipt fields: {', '.join(case.required_receipt_fields)}")
        print(f"      fixture: {case.fixture_guidance}")
        print("      detail: required audit note describing the exercised fixture")


def _print_provider_requirements(cap: ProviderCapability) -> None:
    print("  requirements:")
    print("    handler:")
    for requirement in cap.handler_requirements:
        print(f"      - {requirement}")
    if cap.required_args:
        print(f"    handler args: {', '.join(cap.required_args)}")
    if cap.required_idempotency_fields:
        print(f"    idempotency fields: {', '.join(cap.required_idempotency_fields)}")
    if cap.required_receipt_fields:
        print(f"    receipt fields: {', '.join(cap.required_receipt_fields)}")
    if cap.required_receipt_source_fields:
        sources = ", ".join(
            f"{receipt_field} <- {source_field}"
            for receipt_field, source_field in cap.required_receipt_source_fields
        )
        print(f"    receipt sources: {sources}")


def _cmd_providers_scan_conformance_file_template(
    refs: tuple[EffectToolReference, ...],
    matrix: tuple[ProviderCapability, ...],
    args: argparse.Namespace,
) -> int:
    from .providers.capabilities import capabilities_for_tool

    by_name: dict[str, ProviderCapability] = {}
    failures: list[str] = []
    for ref in refs:
        if ref.tool is None:
            failures.append(
                f"{ref.path}:{ref.line}: dynamic tool expression cannot be provider-audited"
            )
            continue
        rows = capabilities_for_tool(ref.tool, args.capability, capabilities=matrix)
        if not rows:
            failures.append(
                f"{ref.path}:{ref.line}: "
                f"{_no_provider_capability_message(ref.tool, args.capability)}"
            )
            continue
        for cap in rows:
            by_name.setdefault(cap.name, cap)

    if failures:
        print(failures[0], file=sys.stderr)
        return 1
    print(
        json.dumps(
            _provider_conformance_file_template_payload(tuple(by_name.values())),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _cmd_providers_scan(matrix: tuple[ProviderCapability, ...], args: argparse.Namespace) -> int:
    from .providers.audit import (
        handler_contract_failures,
        receipt_contract_failures,
        scan_effect_tools,
        suggest_capabilities_for_refs,
    )
    from .providers.capabilities import capabilities_for_tool

    refs = scan_effect_tools(args.scan)
    if args.conformance_file_template:
        return _cmd_providers_scan_conformance_file_template(refs, matrix, args)
    items: list[dict[str, object]] = []
    all_failures: list[str] = []
    for ref in refs:
        rows = (
            ()
            if ref.tool is None
            else capabilities_for_tool(ref.tool, args.capability, capabilities=matrix)
        )
        policy, policy_failures = _provider_policy(
            ref.tool,
            args.capability,
            rows,
            require_known=args.require_known,
            require_auto_rearm=args.require_auto_rearm,
        )
        conformance, conformance_failures = _provider_conformance_payload(
            matrix,
            rows,
            tool=ref.tool,
            capability=args.capability,
            conformance_file=args.conformance_file,
            require_conformance=args.require_conformance,
        )
        readiness, readiness_failures = _provider_readiness_payload(
            rows,
            tool=ref.tool,
            capability=args.capability,
            require_reviewed=args.require_reviewed,
        )
        dynamic_failures: list[str] = []
        if ref.dynamic and (
            args.require_known
            or args.require_auto_rearm
            or args.require_conformance
            or args.conformance_file
            or args.require_handler_contract
            or args.require_receipt_contract
            or args.require_reviewed
        ):
            dynamic_failures.append(
                f"{ref.path}:{ref.line}: dynamic tool expression cannot be provider-audited"
            )
        contract_failures = (
            list(handler_contract_failures(ref, rows)) if args.require_handler_contract else []
        )
        if args.require_handler_contract and not rows:
            contract_failures.append(
                f"{ref.path}:{ref.line}: cannot prove handler contract for a dynamic "
                "tool expression"
                if ref.tool is None
                else (
                    f"{ref.path}:{ref.line}: cannot prove handler contract for "
                    f"{ref.tool!r}: {_no_provider_capability_message(ref.tool, args.capability)}"
                )
            )
        receipt_failures = (
            list(receipt_contract_failures(ref, rows)) if args.require_receipt_contract else []
        )
        if args.require_receipt_contract and not rows:
            receipt_failures.append(
                f"{ref.path}:{ref.line}: cannot prove receipt contract for a dynamic "
                "tool expression"
                if ref.tool is None
                else (
                    f"{ref.path}:{ref.line}: cannot prove receipt contract for "
                    f"{ref.tool!r}: {_no_provider_capability_message(ref.tool, args.capability)}"
                )
            )
        item_failures = [
            *dynamic_failures,
            *policy_failures,
            *conformance_failures,
            *readiness_failures,
            *contract_failures,
            *receipt_failures,
        ]
        all_failures.extend(item_failures)

        item = ref.to_dict()
        item["capabilities"] = [
            _provider_capability_payload(
                cap,
                include_conformance_plan=args.conformance_plan,
                include_conformance_template=args.conformance_template,
            )
            for cap in rows
        ]
        if policy is not None:
            item["policy"] = policy
        if conformance is not None:
            item["conformance"] = conformance
        if readiness is not None:
            item["readiness"] = readiness
        if args.require_handler_contract:
            item["handler_contract"] = {
                "passed": not contract_failures,
                "failures": contract_failures,
            }
        if args.require_receipt_contract:
            item["receipt_contract"] = {
                "passed": not receipt_failures,
                "failures": receipt_failures,
            }
        if item_failures:
            item["failures"] = item_failures
        items.append(item)

    suggestions = suggest_capabilities_for_refs(refs, matrix) if args.suggest_capabilities else ()
    payload: dict[str, object] = {
        "schema_version": 1,
        "scan": args.scan,
        "capability": args.capability,
        "capability_file": args.capability_file,
        "tools": items,
    }
    if args.suggest_capabilities:
        payload["suggested_capabilities"] = [suggestion.to_dict() for suggestion in suggestions]
        payload["suggested_capability_file"] = {
            "schema_version": 1,
            "capabilities": [suggestion.capability for suggestion in suggestions],
        }
    if all_failures:
        payload["error"] = all_failures[0]

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("provider scan")
        if not refs:
            print("(no OpenOnce effect tools found)")
        for item in items:
            tool = item["tool"] if item["tool"] is not None else "<dynamic>"
            print(f"{item['path']}:{item['line']}  {item['decorator']}  {tool}")
            capability_items = item.get("capabilities", [])
            names = (
                [
                    str(cap["name"])
                    for cap in capability_items
                    if isinstance(cap, dict) and isinstance(cap.get("name"), str)
                ]
                if isinstance(capability_items, list)
                else []
            )
            print(f"  capabilities: {', '.join(names) if names else '-'}")
            failures = item.get("failures", [])
            if isinstance(failures, list):
                for failure in failures:
                    print(f"  - {failure}")
        if args.suggest_capabilities:
            print("\nsuggested capabilities")
            if not suggestions:
                print("(none)")
            for suggestion in suggestions:
                print(f"  - {suggestion.capability['name']}: {suggestion.tool}")
                for ref in suggestion.refs:
                    print(f"    {ref.path}:{ref.line} {ref.function}")
        print("\nscan: failed" if all_failures else "\nscan: passed")
    return 1 if all_failures else 0


def cmd_providers(_store: Store | None, args: argparse.Namespace) -> int:
    from .providers.capabilities import (
        capabilities_for_tool,
        capability_matrix,
        load_capabilities_file,
    )

    try:
        extra = load_capabilities_file(args.capability_file) if args.capability_file else ()
        matrix = capability_matrix(extra)
    except (OSError, ValueError) as exc:
        print(f"cannot load provider capability file: {exc}", file=sys.stderr)
        return 2

    if args.conformance_file_template and (args.conformance_file or args.require_conformance):
        print(
            "--conformance-file-template cannot be combined with "
            "--conformance-file or --require-conformance",
            file=sys.stderr,
        )
        return 2

    if args.scan:
        try:
            return _cmd_providers_scan(matrix, args)
        except (OSError, ValueError) as exc:
            print(f"cannot scan provider tools: {exc}", file=sys.stderr)
            return 2

    if args.require_handler_contract or args.require_receipt_contract or args.suggest_capabilities:
        flag = (
            "--require-handler-contract"
            if args.require_handler_contract
            else (
                "--require-receipt-contract"
                if args.require_receipt_contract
                else "--suggest-capabilities"
            )
        )
        print(f"{flag} requires --scan", file=sys.stderr)
        return 2

    if args.tool:
        rows = capabilities_for_tool(args.tool, args.capability, capabilities=matrix)
    else:
        rows = (
            tuple(cap for cap in matrix if cap.name == args.capability)
            if args.capability
            else matrix
        )
    if args.conformance_file_template:
        if (args.tool or args.capability) and not rows:
            print(_no_provider_capability_message(args.tool, args.capability), file=sys.stderr)
            return 1
        print(
            json.dumps(
                _provider_conformance_file_template_payload(rows),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        conformance, conformance_failures = _provider_conformance_payload(
            matrix,
            rows,
            tool=args.tool,
            capability=args.capability,
            conformance_file=args.conformance_file,
            require_conformance=args.require_conformance,
        )
    except (OSError, ValueError) as exc:
        print(f"cannot load provider conformance file: {exc}", file=sys.stderr)
        return 2
    reviewed_rows = matrix if args.validate_only else rows
    readiness, readiness_failures = _provider_readiness_payload(
        reviewed_rows,
        tool=None if args.validate_only else args.tool,
        capability=args.capability,
        require_reviewed=args.require_reviewed,
    )

    if args.validate_only:
        return _print_provider_validation(
            capability_file=args.capability_file,
            custom_count=len(extra),
            total_count=len(matrix),
            as_json=args.json,
            conformance=conformance,
            conformance_failures=conformance_failures,
            readiness=readiness,
            readiness_failures=readiness_failures,
        )

    policy, failures = _provider_policy(
        args.tool,
        args.capability,
        rows,
        require_known=args.require_known,
        require_auto_rearm=args.require_auto_rearm,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "tool": args.tool,
        "capability": args.capability,
        "capability_file": args.capability_file,
        "capabilities": [
            _provider_capability_payload(
                cap,
                include_conformance_plan=args.conformance_plan,
                include_conformance_template=args.conformance_template,
            )
            for cap in rows
        ],
    }
    if policy is not None:
        payload["policy"] = policy
    if conformance is not None:
        payload["conformance"] = conformance
    if readiness is not None:
        payload["readiness"] = readiness

    gate_failures = [*failures, *conformance_failures, *readiness_failures]

    if gate_failures and args.tool is None:
        if args.json:
            payload["error"] = gate_failures[0]
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_provider_policy(failures)
            if conformance is not None:
                _print_provider_conformance(conformance_failures)
            if readiness is not None:
                _print_provider_readiness(readiness_failures)
        return 1

    if (args.tool or args.capability) and not rows:
        message = _no_provider_capability_message(args.tool, args.capability)
        if args.json:
            payload["error"] = message
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(message)
            if policy is not None:
                _print_provider_policy(failures)
            if conformance is not None:
                _print_provider_conformance(conformance_failures)
            if readiness is not None:
                _print_provider_readiness(readiness_failures)
        return 1
    if args.json:
        if gate_failures:
            payload["error"] = gate_failures[0]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if gate_failures else 0
    print("provider capability matrix")
    print(f"{'name':<19} {'tool':<18} {'tier':<40} {'auto_rearm':<11} {'grace_s':<7} prober")
    for cap in rows:
        auto = "yes" if cap.can_auto_rearm_on_miss else "no"
        print(
            f"{cap.name:<19} {cap.tool_pattern:<18} {cap.tier.value:<40} "
            f"{auto:<11} {cap.default_grace_seconds:<7.0f} {cap.prober or '-'}"
        )
        print(f"  probe: {cap.probe_basis}")
        print(f"  miss:  {cap.miss_semantics}")
        print(f"  risk:  {cap.risk}")
        if args.requirements:
            _print_provider_requirements(cap)
        if args.conformance_plan:
            from .providers.conformance import conformance_plan

            print("  conformance:")
            for case in conformance_plan(cap):
                expected = "|".join(outcome.value for outcome in case.expected_outcomes)
                receipt = " + receipt" if case.receipt_required else ""
                print(f"    - {case.scenario.value}: expect {expected}{receipt}")
                if case.required_receipt_fields:
                    print(f"      receipt fields: {', '.join(case.required_receipt_fields)}")
                print(f"      fixture: {case.fixture_guidance}")
        if args.conformance_template:
            _print_provider_conformance_template(cap)
    print("\nauto_rearm=yes means a post-grace miss can move UNKNOWN back to APPROVED.")
    print("auto_rearm=no means a miss must remain inconclusive and route to human review.")
    if policy is not None:
        _print_provider_policy(failures)
    if conformance is not None:
        _print_provider_conformance(conformance_failures)
    if readiness is not None:
        _print_provider_readiness(readiness_failures)
    return 1 if gate_failures else 0


def _load_receipt_arg(args: argparse.Namespace) -> dict[str, object]:
    if args.receipt_json is not None:
        raw = args.receipt_json
        source = "--receipt-json"
    else:
        source = args.receipt_file
        try:
            raw = (
                sys.stdin.read()
                if args.receipt_file == "-"
                else Path(args.receipt_file).read_text(encoding="utf-8")
            )
        except OSError as exc:
            raise ValueError(f"{source}: cannot read receipt file: {exc}") from exc

    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: invalid receipt JSON: {exc.msg}") from exc
    if not isinstance(receipt, dict) or not receipt:
        raise ValueError(f"{source}: receipt must be a non-empty JSON object")
    return cast(dict[str, object], receipt)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openonce", description="Inspect the effect ledger.")
    p.add_argument("--db", help="SQLite path or Postgres DSN")
    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("ls", help="list effects")
    ls.add_argument("--state", help="comma-separated state filter (e.g. unknown,failed)")
    ls.set_defaults(fn=cmd_ls, needs_store=True)

    review = sub.add_parser("review", help="the human queue: approvals + unresolved outcomes")
    review.set_defaults(fn=cmd_review, needs_store=True)

    show = sub.add_parser("show", help="one effect: record, result, full journal")
    show.add_argument("effect_id")
    show.set_defaults(fn=cmd_show, needs_store=True)

    approve = sub.add_parser("approve", help="approve a parked effect")
    approve.add_argument("effect_id")
    approve.add_argument("--by", default="cli")
    approve.set_defaults(fn=cmd_approve, needs_store=True)

    deny = sub.add_parser("deny", help="deny a parked effect")
    deny.add_argument("effect_id")
    deny.add_argument("--by", default="cli")
    deny.add_argument("--reason", default="")
    deny.set_defaults(fn=cmd_deny, needs_store=True)

    happened = sub.add_parser(
        "resolve-happened",
        help="manually commit an UNKNOWN/HUMAN_REVIEW effect with an external receipt",
    )
    happened.add_argument("effect_id")
    happened.add_argument("--by", default="cli")
    happened.add_argument("--reason", default="")
    happened.add_argument(
        "--capability-file",
        help="JSON file with repo/team provider capabilities for receipt validation",
    )
    happened.add_argument(
        "--provider-capability",
        action="append",
        default=[],
        metavar="TOOL=CAPABILITY",
        help="pin an ambiguous tool to a provider capability; may be repeated",
    )
    happened.add_argument(
        "--require-receipt-contract",
        action="store_true",
        help="fail unless the manual receipt satisfies a matching provider capability contract",
    )
    receipt_group = happened.add_mutually_exclusive_group(required=True)
    receipt_group.add_argument(
        "--receipt-json",
        metavar="JSON",
        help="non-empty JSON object with external evidence",
    )
    receipt_group.add_argument(
        "--receipt-file",
        metavar="PATH",
        help="file containing a non-empty JSON object, or '-' for stdin",
    )
    happened.set_defaults(fn=cmd_resolve_happened, needs_store=True)

    not_happened = sub.add_parser(
        "resolve-not-happened",
        help="manually re-arm an UNKNOWN/HUMAN_REVIEW effect after proving it did not happen",
    )
    not_happened.add_argument("effect_id")
    not_happened.add_argument("--by", default="cli")
    not_happened.add_argument("--reason", required=True)
    not_happened.add_argument(
        "--capability-file",
        help="JSON file with repo/team provider capabilities for auto-rearm validation",
    )
    not_happened.add_argument(
        "--provider-capability",
        action="append",
        default=[],
        metavar="TOOL=CAPABILITY",
        help="pin an ambiguous tool to a provider capability; may be repeated",
    )
    not_happened.add_argument(
        "--require-provider-capability",
        action="store_true",
        help="fail unless the effect's tool has a matching provider capability",
    )
    not_happened.add_argument(
        "--require-auto-rearm",
        action="store_true",
        help="fail unless provider knowledge says a miss is safe to re-arm",
    )
    not_happened.set_defaults(fn=cmd_resolve_not_happened, needs_store=True)

    reconcile = sub.add_parser(
        "reconcile", help="drive UNKNOWN outcomes to resolution (once, or as a daemon)"
    )
    reconcile.add_argument(
        "--probers",
        help="module.path:ATTR pointing at a dict[str, Prober] keyed by tool name",
    )
    reconcile.add_argument(
        "--grace",
        type=float,
        default=None,
        help=(
            "global grace period seconds; omitted means provider capability defaults "
            "(fallback 300s)"
        ),
    )
    reconcile.add_argument(
        "--capability-file",
        help="JSON file with repo/team provider capabilities to use while reconciling",
    )
    reconcile.add_argument(
        "--provider-capability",
        action="append",
        default=[],
        metavar="TOOL=CAPABILITY",
        help=("pin an ambiguous tool to a provider capability while reconciling; may be repeated"),
    )
    reconcile.add_argument(
        "--require-provider-capability",
        action="store_true",
        help="require a matching provider capability before definitive probe results resolve",
    )
    reconcile.add_argument(
        "--conformance-file",
        help="JSON file with observed provider conformance evidence to enforce while reconciling",
    )
    reconcile.add_argument(
        "--require-conformance",
        action="store_true",
        help=(
            "require passing provider conformance evidence before definitive probe results resolve"
        ),
    )
    reconcile.add_argument("--watch", action="store_true", help="run forever (daemon mode)")
    reconcile.add_argument("--interval", type=float, default=30.0, help="watch poll seconds")
    reconcile.add_argument("--max-loops", type=int, default=0, help=argparse.SUPPRESS)
    reconcile.set_defaults(fn=cmd_reconcile, needs_store=True)

    providers = sub.add_parser("providers", help="show built-in provider safety tiers")
    providers.add_argument("tool", nargs="?", help="optional tool name filter, e.g. stripe.charge")
    providers.add_argument(
        "--capability",
        help="optional capability name filter, e.g. email_provider_api",
    )
    providers.add_argument(
        "--capability-file",
        help="JSON file with repo/team provider capabilities to merge with built-ins",
    )
    providers.add_argument(
        "--scan",
        action="append",
        default=[],
        metavar="PATH",
        help="scan Python files or directories for OpenOnce effect tool declarations",
    )
    providers.add_argument(
        "--validate-only",
        action="store_true",
        help="validate built-ins plus --capability-file and exit without listing tools",
    )
    providers.add_argument(
        "--requirements", action="store_true", help="show handler requirements for each match"
    )
    providers.add_argument(
        "--conformance-plan",
        action="store_true",
        help="show standard probe scenarios this capability should prove",
    )
    providers.add_argument(
        "--conformance-template",
        action="store_true",
        help="show a fill-in JSON evidence template with the current capability fingerprint",
    )
    providers.add_argument(
        "--conformance-file-template",
        action="store_true",
        help="emit a fill-in openonce-conformance.json template for every matching capability",
    )
    providers.add_argument(
        "--conformance-file",
        help="JSON file with observed provider conformance evidence to validate",
    )
    providers.add_argument(
        "--require-conformance",
        action="store_true",
        help="fail unless --conformance-file proves every matching capability",
    )
    providers.add_argument(
        "--require-reviewed",
        action="store_true",
        help="fail if matched capabilities still contain TODOs or unreviewed auto-rearm settings",
    )
    providers.add_argument(
        "--require-handler-contract",
        action="store_true",
        help=(
            "with --scan, fail unless handler args and idempotency_fields satisfy "
            "capability contracts"
        ),
    )
    providers.add_argument(
        "--require-receipt-contract",
        action="store_true",
        help=(
            "with --scan, fail unless literal return dicts include required receipt "
            "fields and source contracts"
        ),
    )
    providers.add_argument(
        "--suggest-capabilities",
        action="store_true",
        help="with --scan, emit conservative capability stubs for unknown literal tools",
    )
    providers.add_argument(
        "--require-known",
        action="store_true",
        help="fail unless the tool matches a provider capability",
    )
    providers.add_argument(
        "--require-auto-rearm",
        action="store_true",
        help="fail unless every matching capability can safely auto-rearm on a miss",
    )
    providers.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    providers.set_defaults(fn=cmd_providers, needs_store=False)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.needs_store:
        if args.db is None:
            parser.error(f"--db is required for `{args.command}`")
        store: Store | None = open_store(args.db)
    else:
        store = None
    result: int = args.fn(store, args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
