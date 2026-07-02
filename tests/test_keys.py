from __future__ import annotations

from typing import ClassVar

import pytest

from openonce.errors import KeyDerivationError
from openonce.keys import canonicalize, derive_key, fingerprint, select_fields


class TestCanonicalize:
    def test_key_order_is_irrelevant(self) -> None:
        assert canonicalize({"b": 1, "a": 2}) == canonicalize({"a": 2, "b": 1})

    def test_nested_sorting(self) -> None:
        assert canonicalize({"z": {"b": 1, "a": [1, {"y": 0, "x": 1}]}}) == (
            '{"z":{"a":[1,{"x":1,"y":0}],"b":1}}'
        )

    def test_unicode_passthrough(self) -> None:
        assert canonicalize({"msg": "héllo 世界"}) == '{"msg":"héllo 世界"}'

    def test_control_char_escaping(self) -> None:
        assert canonicalize("a\nb\x01") == '"a\\nb\\u0001"'

    def test_floats_rejected_with_guidance(self) -> None:
        with pytest.raises(KeyDerivationError, match="integer minor units"):
            canonicalize({"amount": 10.5})

    def test_huge_ints_rejected(self) -> None:
        with pytest.raises(KeyDerivationError, match="2\\*\\*53"):
            canonicalize({"n": 2**60})

    def test_non_string_keys_rejected(self) -> None:
        with pytest.raises(KeyDerivationError, match="Non-string dict key"):
            canonicalize({1: "a"})

    def test_unsupported_types_rejected(self) -> None:
        with pytest.raises(KeyDerivationError, match="Unsupported type"):
            canonicalize({"x": object()})


class TestDeriveKey:
    ARGS: ClassVar[dict[str, str]] = {
        "owner": "acme",
        "repo": "api",
        "title": "Fix login",
        "body": "long prose...",
    }

    def test_whitelist_ignores_noise_fields(self) -> None:
        fields = ["owner", "repo", "title"]
        k1 = derive_key("github.create_pr", self.ARGS, scope="run1", fields=fields)
        k2 = derive_key(
            "github.create_pr",
            {**self.ARGS, "body": "completely different"},
            scope="run1",
            fields=fields,
        )
        assert k1 == k2

    def test_no_whitelist_means_all_fields_matter(self) -> None:
        k1 = derive_key("t", self.ARGS, scope="run1", fields=None)
        k2 = derive_key("t", {**self.ARGS, "body": "x"}, scope="run1", fields=None)
        assert k1 != k2

    def test_scope_separates_runs(self) -> None:
        k1 = derive_key("t", self.ARGS, scope="run1", fields=None)
        k2 = derive_key("t", self.ARGS, scope="run2", fields=None)
        assert k1 != k2

    def test_tool_separates_keys(self) -> None:
        k1 = derive_key("email.send", {"to": "a@b.c"}, scope="r", fields=None)
        k2 = derive_key("slack.post", {"to": "a@b.c"}, scope="r", fields=None)
        assert k1 != k2

    def test_key_is_versioned(self) -> None:
        assert derive_key("t", {}, scope="r", fields=None).startswith("oo1_")


class TestSelectFields:
    def test_missing_whitelisted_field_is_just_absent(self) -> None:
        assert select_fields({"a": 1}, ["a", "b"]) == {"a": 1}

    def test_fingerprint_stability(self) -> None:
        assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
