"""openonce CLI — the receipts, visible.

    openonce --db openonce.db ls                 # all effects, newest last
    openonce --db openonce.db ls --state unknown,human_review
    openonce --db openonce.db review             # the human queue
    openonce --db openonce.db show eff_abc123    # record + full journal
    openonce --db openonce.db approve eff_abc123 --by eric
    openonce --db openonce.db deny eff_abc123 --reason "wrong customer"
    openonce --db openonce.db reconcile --probers myapp.probers:PROBERS --watch

``--db`` accepts a SQLite path or a Postgres DSN (``postgres://...`` or
``host=... dbname=...``). Read paths never mutate; approve/deny/reconcile
are the only writes.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from datetime import UTC, datetime

from .records import EffectRecord
from .state import TERMINAL, EffectState
from .store.base import Store

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
    print(f"\n{len(recs)} effect(s) need a human. approve/deny by effect_id.")
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
        print(f"  result:       {outcome}: {json.dumps(rec.result.value or rec.result.error)}")
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
        {EffectState.REQUIRES_APPROVAL, EffectState.HUMAN_REVIEW},
        EffectState.APPROVED,
        payload={"approved_by": args.by, "via": "cli"},
    )
    if rec is None:
        print(f"{args.effect_id} is not awaiting approval/review", file=sys.stderr)
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


def cmd_reconcile(store: Store, args: argparse.Namespace) -> int:
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
    rec = Reconciler(store, probers=dict(probers), grace_seconds=args.grace)

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openonce", description="Inspect the effect ledger.")
    p.add_argument("--db", required=True, help="SQLite path or Postgres DSN")
    sub = p.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("ls", help="list effects")
    ls.add_argument("--state", help="comma-separated state filter (e.g. unknown,failed)")
    ls.set_defaults(fn=cmd_ls)

    review = sub.add_parser("review", help="the human queue: approvals + unresolved outcomes")
    review.set_defaults(fn=cmd_review)

    show = sub.add_parser("show", help="one effect: record, result, full journal")
    show.add_argument("effect_id")
    show.set_defaults(fn=cmd_show)

    approve = sub.add_parser("approve", help="approve a parked effect")
    approve.add_argument("effect_id")
    approve.add_argument("--by", default="cli")
    approve.set_defaults(fn=cmd_approve)

    deny = sub.add_parser("deny", help="deny a parked effect")
    deny.add_argument("effect_id")
    deny.add_argument("--by", default="cli")
    deny.add_argument("--reason", default="")
    deny.set_defaults(fn=cmd_deny)

    reconcile = sub.add_parser(
        "reconcile", help="drive UNKNOWN outcomes to resolution (once, or as a daemon)"
    )
    reconcile.add_argument(
        "--probers",
        help="module.path:ATTR pointing at a dict[str, Prober] keyed by tool name",
    )
    reconcile.add_argument("--grace", type=float, default=300.0, help="grace period seconds")
    reconcile.add_argument("--watch", action="store_true", help="run forever (daemon mode)")
    reconcile.add_argument("--interval", type=float, default=30.0, help="watch poll seconds")
    reconcile.add_argument("--max-loops", type=int, default=0, help=argparse.SUPPRESS)
    reconcile.set_defaults(fn=cmd_reconcile)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = open_store(args.db)
    result: int = args.fn(store, args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
