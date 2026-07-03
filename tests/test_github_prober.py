"""GitHubPullRequestProber tests: natural-key probe, field mapping, missing key."""

from __future__ import annotations

import json

from openonce.providers.base import ProbeOutcome
from openonce.providers.github import GitHubPullRequestProber
from openonce.records import EffectRecord
from openonce.state import EffectState


def make_record(args: dict) -> EffectRecord:
    return EffectRecord(
        effect_id="eff_1",
        idempotency_key="oo1_k",
        tool="github.create_pr",
        state=EffectState.UNKNOWN,
        args_fingerprint="fp",
        args_json=json.dumps(args),
        scope="run1",
        provider_key="eff_1:github.create_pr",
    )


class RecordingList:
    def __init__(self, pulls: list[dict]) -> None:
        self.pulls = pulls
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, owner: str, repo: str, head: str) -> list[dict]:
        self.calls.append((owner, repo, head))
        return self.pulls


ARGS = {"owner": "acme", "repo": "api", "head": "fix-login", "title": "Fix login", "body": "..."}


def github_pull(
    *,
    number: int = 42,
    owner: str = "acme",
    head: str = "fix-login",
    state: str = "open",
) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/api/pull/{number}",
        "state": state,
        "head": {
            "ref": head,
            "label": f"{owner}:{head}",
            "user": {"login": owner},
            "repo": {"owner": {"login": owner}},
        },
    }


class TestGitHubProber:
    def test_existing_pr_is_happened_with_receipt(self) -> None:
        lister = RecordingList([github_pull()])
        result = GitHubPullRequestProber(lister).probe(make_record(ARGS))

        assert result.outcome is ProbeOutcome.HAPPENED
        assert result.receipt["number"] == 42
        assert result.receipt["head"] == "fix-login"
        assert result.receipt["head_label"] == "acme:fix-login"
        assert lister.calls == [("acme", "api", "fix-login")]

    def test_no_pr_is_authoritatively_not_happened(self) -> None:
        result = GitHubPullRequestProber(RecordingList([])).probe(make_record(ARGS))
        assert result.outcome is ProbeOutcome.NOT_HAPPENED
        assert "authoritative" in result.detail

    def test_field_map_renames_natural_key(self) -> None:
        lister = RecordingList([github_pull(number=7, state="closed")])
        prober = GitHubPullRequestProber(lister, field_map={"head": "branch"})
        args = {"owner": "acme", "repo": "api", "branch": "fix-login"}
        result = prober.probe(make_record(args))
        assert result.outcome is ProbeOutcome.HAPPENED
        assert lister.calls == [("acme", "api", "fix-login")]

    def test_label_only_match_still_returns_source_contract_head(self) -> None:
        pull = github_pull()
        del pull["head"]["ref"]

        result = GitHubPullRequestProber(RecordingList([pull])).probe(make_record(ARGS))

        assert result.outcome is ProbeOutcome.HAPPENED
        assert result.receipt["head"] == "fix-login"
        assert result.receipt["head_label"] == "acme:fix-login"

    def test_missing_natural_key_field_is_inconclusive(self) -> None:
        result = GitHubPullRequestProber(RecordingList([])).probe(
            make_record({"owner": "acme", "repo": "api"})  # no head
        )
        assert result.outcome is ProbeOutcome.INCONCLUSIVE
        assert "idempotency_fields" in result.detail

    def test_unverified_pr_results_are_inconclusive(self) -> None:
        lister = RecordingList([github_pull(number=99, owner="other", head="different")])

        result = GitHubPullRequestProber(lister).probe(make_record(ARGS))

        assert result.outcome is ProbeOutcome.INCONCLUSIVE
        assert "none carried the requested head acme:fix-login" in result.detail

    def test_multiple_matching_prs_are_inconclusive(self) -> None:
        lister = RecordingList([github_pull(number=1), github_pull(number=2, state="closed")])

        result = GitHubPullRequestProber(lister).probe(make_record(ARGS))

        assert result.outcome is ProbeOutcome.INCONCLUSIVE
        assert "multiple matches" in result.detail
