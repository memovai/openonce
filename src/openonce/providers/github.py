"""GitHub PR prober — the tier-2 (natural business key) reference.

GitHub has no idempotency-key header, but PR creation carries a natural key:
``(owner, repo, head branch)``. GitHub itself refuses a second open PR for
the same head/base, and *listing PRs by head is an authoritative read of the
primary store* — unlike Stripe Search's eventually-consistent index, a miss
here genuinely means "does not exist".

The handler's args must therefore include the natural-key fields (defaults:
``owner``, ``repo``, ``head``) — which is exactly what you'd whitelist as
``idempotency_fields`` anyway. If the natural key can't be assembled from
the recorded args, the prober answers INCONCLUSIVE and a human looks.

No dependency on any GitHub SDK: duck-typed ``list_pulls_fn``;
``from_token`` builds one on stdlib urllib.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ..records import EffectRecord
from .base import ProbeOutcome, ProbeResult

ListPullsFn = Callable[[str, str, str], list[dict[str, Any]]]


class GitHubPullRequestProber:
    """Resolves UNKNOWN ``create_pr``-style effects by listing PRs by head.

    ``field_map`` renames the natural-key fields if the handler's args use
    different names, e.g. ``{"head": "branch"}``.
    """

    def __init__(self, list_pulls_fn: ListPullsFn, *, field_map: dict[str, str] | None = None):
        self._list_pulls = list_pulls_fn
        self._fields = {"owner": "owner", "repo": "repo", "head": "head", **(field_map or {})}

    @classmethod
    def from_token(cls, token: str, **kwargs: Any) -> GitHubPullRequestProber:
        """Build on stdlib urllib (no SDK). ``state=all``: a created-then-closed
        PR still means the effect happened."""

        def list_pulls(owner: str, repo: str, head: str) -> list[dict[str, Any]]:
            query = urllib.parse.urlencode({"head": f"{owner}:{head}", "state": "all"})
            req = urllib.request.Request(
                f"https://api.github.com/repos/{owner}/{repo}/pulls?{query}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "openonce",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                loaded: list[dict[str, Any]] = json.loads(resp.read())
                return loaded

        return cls(list_pulls, **kwargs)

    # ------------------------------------------------------------------ #

    def probe(self, record: EffectRecord) -> ProbeResult:
        args = record.args()
        try:
            owner = str(args[self._fields["owner"]])
            repo = str(args[self._fields["repo"]])
            head = str(args[self._fields["head"]])
        except KeyError as missing:
            return ProbeResult(
                ProbeOutcome.INCONCLUSIVE,
                detail=(
                    f"cannot assemble the natural key: recorded args lack {missing}. "
                    f"Include it in the handler args (and idempotency_fields)."
                ),
            )

        pulls = self._list_pulls(owner, repo, head)
        if pulls:
            first = pulls[0]
            return ProbeResult(
                ProbeOutcome.HAPPENED,
                receipt={
                    "number": first.get("number"),
                    "html_url": first.get("html_url"),
                    "state": first.get("state"),
                },
                detail=f"PR exists for head {owner}:{head}",
            )
        return ProbeResult(
            ProbeOutcome.NOT_HAPPENED,
            detail=(
                f"no PR (any state) for head {owner}:{head} — the PR list is an "
                f"authoritative primary-store read, so a miss means it was not created"
            ),
        )
