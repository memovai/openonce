"""Policy: the pre-execution gate (maps cleanly onto an MCP validator later).

A policy inspects the *planned* effect and decides: allow, require a human,
or deny. It runs before anything leaves the building, and its decision is
journaled — the approval trail is part of the audit story, not a bolt-on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from .records import EffectRecord


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""


Policy = Callable[[EffectRecord], Decision]


def allow_all(_: EffectRecord) -> Decision:
    return Decision(Verdict.ALLOW)


def require_approval_for(tools: Iterable[str], reason: str = "sensitive tool") -> Policy:
    """Require a human for the given tools; allow everything else.

    Matches exact tool names or ``prefix.*`` patterns::

        policy = require_approval_for(["stripe.*", "email.send"])
    """
    exact = {t for t in tools if not t.endswith(".*")}
    prefixes = tuple(t[:-1] for t in tools if t.endswith(".*"))  # keep trailing dot

    def policy(record: EffectRecord) -> Decision:
        if record.tool in exact or (prefixes and record.tool.startswith(prefixes)):
            return Decision(Verdict.REQUIRE_APPROVAL, reason)
        return Decision(Verdict.ALLOW)

    return policy
