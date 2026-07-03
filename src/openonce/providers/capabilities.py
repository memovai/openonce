"""Provider capability matrix.

This is product surface, not just documentation. The hard part of safe agent
side effects is knowing when a provider miss means "safe to retry" versus
"the world is ambiguous; ask a human." Keep that knowledge structured so
probers, docs, and operators can agree.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import Path
from typing import Any, cast


class CapabilityTier(StrEnum):
    NATIVE_IDEMPOTENCY_KEY = "tier_1_native_idempotency_key"
    NATURAL_BUSINESS_KEY = "tier_2_natural_business_key"
    SENDER_CONTROLLED_AUTHORITATIVE = "tier_2_sender_controlled_authoritative"
    NON_AUTHORITATIVE = "tier_3_non_authoritative"


@dataclass(frozen=True)
class ProviderCapability:
    name: str
    tool_pattern: str
    tier: CapabilityTier
    key_strategy: str
    probe_basis: str
    miss_semantics: str
    can_auto_rearm_on_miss: bool
    default_grace_seconds: float
    prober: str | None
    handler_requirements: tuple[str, ...]
    risk: str
    required_args: tuple[str, ...] = ()
    required_idempotency_fields: tuple[str, ...] = ()
    required_receipt_fields: tuple[str, ...] = ()
    required_receipt_source_fields: tuple[tuple[str, str], ...] = ()

    def matches(self, tool: str) -> bool:
        return fnmatchcase(tool, self.tool_pattern)

    def to_dict(self) -> dict[str, object]:
        """Stable JSON-serializable representation for tools and UIs."""
        return {
            "name": self.name,
            "tool_pattern": self.tool_pattern,
            "tier": self.tier.value,
            "key_strategy": self.key_strategy,
            "probe_basis": self.probe_basis,
            "miss_semantics": self.miss_semantics,
            "can_auto_rearm_on_miss": self.can_auto_rearm_on_miss,
            "default_grace_seconds": float(self.default_grace_seconds),
            "prober": self.prober,
            "handler_requirements": list(self.handler_requirements),
            "risk": self.risk,
            "required_args": list(self.required_args),
            "required_idempotency_fields": list(self.required_idempotency_fields),
            "required_receipt_fields": list(self.required_receipt_fields),
            "required_receipt_source_fields": {
                receipt_field: source_field
                for receipt_field, source_field in self.required_receipt_source_fields
            },
        }


_BUILTIN_CAPABILITIES: tuple[ProviderCapability, ...] = (
    ProviderCapability(
        name="stripe",
        tool_pattern="stripe.*",
        tier=CapabilityTier.NATIVE_IDEMPOTENCY_KEY,
        key_strategy="provider idempotency key",
        probe_basis='metadata["openonce_effect_id"] search',
        miss_semantics="inconclusive inside indexing lag; not-happened after grace",
        can_auto_rearm_on_miss=True,
        default_grace_seconds=120.0,
        prober="StripeProber",
        handler_requirements=(
            "pass current_effect().provider_key as Stripe Idempotency-Key",
            "stamp effect_metadata(current_effect()) on the created object",
        ),
        risk="lowest: provider-key replay is the duplicate-charge backstop",
        required_receipt_fields=("stripe_id",),
    ),
    ProviderCapability(
        name="github_pr",
        tool_pattern="github.create_pr",
        tier=CapabilityTier.NATURAL_BUSINESS_KEY,
        key_strategy="natural key: owner/repo/head",
        probe_basis="list pull requests by head with state=all and verify returned head metadata",
        miss_semantics="not-happened: primary-store read is authoritative",
        can_auto_rearm_on_miss=True,
        default_grace_seconds=0.0,
        prober="GitHubPullRequestProber",
        handler_requirements=(
            "include owner, repo, and head in handler args",
            "use those fields in idempotency_fields",
        ),
        risk="low: GitHub itself rejects duplicate open PRs for one head",
        required_args=("owner", "repo", "head"),
        required_idempotency_fields=("owner", "repo", "head"),
        required_receipt_fields=("number", "head"),
        required_receipt_source_fields=(("head", "head"),),
    ),
    ProviderCapability(
        name="email_provider_api",
        tool_pattern="email.send",
        tier=CapabilityTier.SENDER_CONTROLLED_AUTHORITATIVE,
        key_strategy="deterministic Message-ID",
        probe_basis="authoritative sent-store search by Message-ID",
        miss_semantics="inconclusive inside propagation lag; not-happened after grace",
        can_auto_rearm_on_miss=True,
        default_grace_seconds=120.0,
        prober="EmailMessageIdProber(authoritative=True)",
        handler_requirements=(
            "stamp make_message_id(current_effect()) on the outgoing message",
            "use only for provider APIs whose sent store is atomic with send",
        ),
        risk="medium: a false miss can duplicate user-visible mail",
        required_receipt_fields=("message_id",),
    ),
    ProviderCapability(
        name="email_smtp",
        tool_pattern="email.send",
        tier=CapabilityTier.NON_AUTHORITATIVE,
        key_strategy="deterministic Message-ID",
        probe_basis="non-authoritative sent-folder search by Message-ID",
        miss_semantics="inconclusive forever: sent copy can fail after delivery",
        can_auto_rearm_on_miss=False,
        default_grace_seconds=120.0,
        prober="EmailMessageIdProber(authoritative=False)",
        handler_requirements=(
            "stamp make_message_id(current_effect()) on the outgoing message",
            "route misses to human review; never auto-rearm from a sent-folder miss",
        ),
        risk="high: bare SMTP can deliver mail without a sent copy",
        required_receipt_fields=("message_id",),
    ),
)


def builtin_capabilities() -> tuple[ProviderCapability, ...]:
    """Built-in provider knowledge OpenOnce can explain and test."""
    return _BUILTIN_CAPABILITIES


def capability_fingerprint(capability: ProviderCapability) -> str:
    """Stable hash of the reviewed capability contract."""
    payload = json.dumps(
        capability.to_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def capability_matrix(
    extra_capabilities: Iterable[ProviderCapability] = (),
) -> tuple[ProviderCapability, ...]:
    """Built-ins plus repo/team-specific provider knowledge."""
    matrix = (*_BUILTIN_CAPABILITIES, *tuple(extra_capabilities))
    errors = validate_capability_matrix(matrix)
    if errors:
        raise ValueError("; ".join(errors))
    return matrix


def validate_capability_matrix(
    capabilities: Iterable[ProviderCapability],
) -> tuple[str, ...]:
    """Structural checks that require seeing the whole capability matrix."""
    seen: dict[str, str] = {}
    errors: list[str] = []
    for cap in capabilities:
        if cap.tier is CapabilityTier.NON_AUTHORITATIVE and cap.can_auto_rearm_on_miss:
            errors.append(
                f"provider capability {cap.name!r} is non-authoritative and "
                "cannot auto-rearm on a miss"
            )
        previous = seen.get(cap.name)
        if previous is not None:
            errors.append(
                f"duplicate provider capability name {cap.name!r} "
                f"for tool patterns {previous!r} and {cap.tool_pattern!r}"
            )
        else:
            seen[cap.name] = cap.tool_pattern
        for field_name, values in (
            ("required_args", cap.required_args),
            ("required_idempotency_fields", cap.required_idempotency_fields),
            ("required_receipt_fields", cap.required_receipt_fields),
        ):
            duplicates = _duplicate_values(values)
            if duplicates:
                errors.append(
                    f"provider capability {cap.name!r} {field_name} contains "
                    f"duplicate field(s): {', '.join(duplicates)}"
                )
        duplicate_receipt_source_fields = _duplicate_values(
            tuple(receipt_field for receipt_field, _ in cap.required_receipt_source_fields)
        )
        if duplicate_receipt_source_fields:
            errors.append(
                f"provider capability {cap.name!r} required_receipt_source_fields "
                "contains duplicate receipt field(s): "
                f"{', '.join(duplicate_receipt_source_fields)}"
            )
        receipt_fields = set(cap.required_receipt_fields)
        required_args = set(cap.required_args)
        idempotency_fields = set(cap.required_idempotency_fields)
        for field in cap.required_idempotency_fields:
            if field not in required_args:
                errors.append(
                    f"provider capability {cap.name!r} idempotency field {field!r} "
                    "is not in required_args"
                )
        for receipt_field, source_field in cap.required_receipt_source_fields:
            if receipt_field not in receipt_fields:
                errors.append(
                    f"provider capability {cap.name!r} receipt source field "
                    f"{receipt_field!r} is not in required_receipt_fields"
                )
            if source_field not in required_args:
                errors.append(
                    f"provider capability {cap.name!r} receipt source {source_field!r} "
                    "is not in required_args"
                )
            if source_field not in idempotency_fields:
                errors.append(
                    f"provider capability {cap.name!r} receipt source {source_field!r} "
                    "is not in required_idempotency_fields"
                )
    return tuple(errors)


def _duplicate_values(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    duplicate_seen: set[str] = set()
    for value in values:
        if value in seen and value not in duplicate_seen:
            duplicates.append(value)
            duplicate_seen.add(value)
        seen.add(value)
    return tuple(duplicates)


def capability_readiness_errors(
    capabilities: Iterable[ProviderCapability],
) -> tuple[str, ...]:
    """Review-readiness checks for provider capability files.

    This is intentionally stricter than schema validation: draft TODO stubs are
    useful backlog items, but release/CI gates should not treat them as reviewed
    provider knowledge.
    """
    errors: list[str] = []
    for cap in capabilities:
        for field, value in _readiness_strings(cap):
            if "TODO" in value.upper():
                errors.append(f"{cap.name}: {field} still contains TODO")
        if cap.can_auto_rearm_on_miss and cap.prober is None:
            errors.append(f"{cap.name}: auto-rearm capabilities must name a prober")
        if cap.prober is not None and not cap.required_receipt_fields:
            errors.append(f"{cap.name}: prober capabilities must define required_receipt_fields")
    return tuple(errors)


def _readiness_strings(cap: ProviderCapability) -> tuple[tuple[str, str], ...]:
    values = [
        ("key_strategy", cap.key_strategy),
        ("probe_basis", cap.probe_basis),
        ("miss_semantics", cap.miss_semantics),
        ("risk", cap.risk),
    ]
    values.extend(
        (f"handler_requirements[{index}]", requirement)
        for index, requirement in enumerate(cap.handler_requirements)
    )
    return tuple(values)


def capability_from_dict(data: Mapping[str, object]) -> ProviderCapability:
    """Parse one capability from the stable JSON representation."""
    name = _required_str(data, "name")
    tool_pattern = _required_str(data, "tool_pattern")
    tier_raw = _required_str(data, "tier")
    try:
        tier = CapabilityTier(tier_raw)
    except ValueError as exc:
        valid = ", ".join(t.value for t in CapabilityTier)
        raise ValueError(f"invalid capability tier {tier_raw!r}; valid tiers: {valid}") from exc

    return ProviderCapability(
        name=name,
        tool_pattern=tool_pattern,
        tier=tier,
        key_strategy=_required_str(data, "key_strategy"),
        probe_basis=_required_str(data, "probe_basis"),
        miss_semantics=_required_str(data, "miss_semantics"),
        can_auto_rearm_on_miss=_required_bool(data, "can_auto_rearm_on_miss"),
        default_grace_seconds=_required_float(data, "default_grace_seconds"),
        prober=_optional_str(data, "prober"),
        handler_requirements=_required_str_tuple(data, "handler_requirements"),
        risk=_required_str(data, "risk"),
        required_args=_optional_str_tuple(data, "required_args"),
        required_idempotency_fields=_optional_str_tuple(data, "required_idempotency_fields"),
        required_receipt_fields=_optional_str_tuple(data, "required_receipt_fields"),
        required_receipt_source_fields=_optional_str_mapping_tuple(
            data, "required_receipt_source_fields"
        ),
    )


def load_capabilities_file(path: str | Path) -> tuple[ProviderCapability, ...]:
    """Load a repo-owned provider capability JSON file.

    Shape:
        {"schema_version": 1, "capabilities": [{...ProviderCapability...}]}
    """
    file_path = Path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{file_path}: invalid JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{file_path}: expected a JSON object")
    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise ValueError(f"{file_path}: schema_version must be 1")
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, list):
        raise ValueError(f"{file_path}: capabilities must be a list")

    parsed: list[ProviderCapability] = []
    for index, item in enumerate(capabilities):
        if not isinstance(item, dict):
            raise ValueError(f"{file_path}: capabilities[{index}] must be an object")
        try:
            parsed.append(capability_from_dict(item))
        except ValueError as exc:
            raise ValueError(f"{file_path}: capabilities[{index}]: {exc}") from exc
    errors = validate_capability_matrix(parsed)
    if errors:
        raise ValueError(f"{file_path}: {'; '.join(errors)}")
    return tuple(parsed)


def capabilities_for_tool(
    tool: str,
    capability: str | None = None,
    *,
    capabilities: Iterable[ProviderCapability] | None = None,
) -> tuple[ProviderCapability, ...]:
    """Return all capabilities that could apply to ``tool``."""
    matrix = _BUILTIN_CAPABILITIES if capabilities is None else tuple(capabilities)
    matches = tuple(cap for cap in matrix if cap.matches(tool))
    if capability is None:
        return matches
    return tuple(cap for cap in matches if cap.name == capability)


def default_grace_for_tool(
    tool: str,
    capability: str | None = None,
    *,
    capabilities: Iterable[ProviderCapability] | None = None,
) -> float | None:
    """Provider-specific default grace, if OpenOnce knows this tool class.

    If multiple capabilities match, use the largest grace. Waiting longer is
    the conservative choice: a premature miss can duplicate real-world effects.
    """
    matches = capabilities_for_tool(tool, capability, capabilities=capabilities)
    if not matches:
        return None
    return max(cap.default_grace_seconds for cap in matches)


def can_auto_rearm_on_miss(
    tool: str,
    capability: str | None = None,
    *,
    capabilities: Iterable[ProviderCapability] | None = None,
) -> bool:
    """Whether a NOT_HAPPENED probe may automatically re-arm this tool.

    Unknown tools remain opt-in-by-prober for backward compatibility. For
    known tools, one non-authoritative match is enough to block auto-rearm:
    ambiguous duplicate side effects are worse than manual review.
    """
    matches = capabilities_for_tool(tool, capability, capabilities=capabilities)
    if not matches:
        return capability is None
    return all(cap.can_auto_rearm_on_miss for cap in matches)


def minimum_builtin_grace_seconds() -> float:
    """Smallest built-in grace used to make reconciler scans inclusive enough."""
    return min(cap.default_grace_seconds for cap in _BUILTIN_CAPABILITIES)


def minimum_grace_seconds(capabilities: Iterable[ProviderCapability]) -> float:
    """Smallest grace in a capability matrix."""
    matrix = tuple(capabilities)
    if not matrix:
        raise ValueError("capabilities must not be empty")
    return min(cap.default_grace_seconds for cap in matrix)


def capability_guidance_for_tool(
    tool: str,
    capability: str | None = None,
    *,
    capabilities: Iterable[ProviderCapability] | None = None,
) -> str:
    """Operator-facing guidance for unresolved effects of ``tool``."""
    matches = capabilities_for_tool(tool, capability, capabilities=capabilities)
    if not matches:
        if capability is not None:
            return (
                f"No provider capability {capability!r} matches tool {tool!r}. "
                "Choose a matching capability or resolve the effect manually."
            )
        return (
            "No provider capability matches this tool. Register a Prober "
            "that can read external state, or resolve the effect manually."
        )

    lines: list[str] = []
    for cap in matches:
        auto = "yes" if cap.can_auto_rearm_on_miss else "no"
        lines.append(
            f"{cap.name}: {cap.tier.value}; prober={cap.prober or 'custom'}; "
            f"auto_rearm_on_miss={auto}; miss={cap.miss_semantics}; "
            f"requirements={'; '.join(cap.handler_requirements)}"
            f"{_structured_contract_guidance(cap)}"
        )
    return " ".join(lines)


def provider_receipt_contract_failures(
    tool: str,
    args: Mapping[str, object],
    receipt: object,
    capability: str | None = None,
    *,
    capabilities: Iterable[ProviderCapability] | None = None,
) -> tuple[str, ...]:
    """Validate a handler/prober receipt against the provider contract.

    Unknown tools intentionally return no failures: callers opt into strict
    behavior by using built-in capabilities, adding repo-owned capabilities, or
    pinning a named capability for the tool.
    """
    matches = capabilities_for_tool(tool, capability, capabilities=capabilities)
    if not matches:
        return ()

    required_args = tuple(
        sorted({field for capability in matches for field in capability.required_args})
    )
    required_fields = _required_receipt_fields(matches)
    required_sources = tuple(
        (cap.name, receipt_field, source_field)
        for cap in matches
        for receipt_field, source_field in cap.required_receipt_source_fields
    )
    if not required_args and not required_fields and not required_sources:
        return ()

    failures: list[str] = []
    missing_args = tuple(field for field in required_args if field not in args)
    if missing_args:
        failures.append("missing required handler arg(s): " + ", ".join(missing_args))

    if not isinstance(receipt, Mapping):
        failures.append(
            "receipt must be an object with required provider evidence field(s): "
            f"{', '.join(required_fields)}"
        )
        return tuple(failures)
    receipt_map = cast(Mapping[str, Any], receipt)

    missing = _missing_receipt_fields(receipt_map, required_fields)
    if missing:
        failures.append("missing required external evidence field(s): " + ", ".join(missing))
    for cap_name, receipt_field, source_field in required_sources:
        if source_field not in args:
            failures.append(
                f"{cap_name}.{receipt_field} requires missing handler arg {source_field!r}"
            )
            continue
        receipt_value = receipt_map.get(receipt_field)
        arg_value = args[source_field]
        if receipt_value != arg_value:
            failures.append(
                f"{cap_name}.{receipt_field} expected {arg_value!r} from arg "
                f"{source_field!r}, got {receipt_value!r}"
            )
    return tuple(failures)


def _structured_contract_guidance(cap: ProviderCapability) -> str:
    parts: list[str] = []
    if cap.required_args:
        parts.append(f"handler_args={','.join(cap.required_args)}")
    if cap.required_idempotency_fields:
        parts.append(f"idempotency_fields={','.join(cap.required_idempotency_fields)}")
    if cap.required_receipt_fields:
        parts.append(f"receipt_fields={','.join(cap.required_receipt_fields)}")
    if cap.required_receipt_source_fields:
        sources = ",".join(
            f"{receipt_field}<-{source_field}"
            for receipt_field, source_field in cap.required_receipt_source_fields
        )
        parts.append(f"receipt_sources={sources}")
    if not parts:
        return ""
    return "; " + "; ".join(parts)


def _required_receipt_fields(capabilities: tuple[ProviderCapability, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {field for capability in capabilities for field in capability.required_receipt_fields}
        )
    )


def _missing_receipt_fields(
    receipt: Mapping[str, object],
    required_fields: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        field
        for field in required_fields
        if field not in receipt or receipt[field] in (None, "", [], {})
    )


def _required_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be null or a non-empty string")
    return value


def _required_bool(data: Mapping[str, object], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _required_float(data: Mapping[str, object], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    if value < 0:
        raise ValueError(f"{key} must be non-negative")
    return float(value)


def _required_str_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{key} must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{key}[{index}] must be a non-empty string")
        items.append(item)
    if not items:
        raise ValueError(f"{key} must not be empty")
    return tuple(items)


def _optional_str_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{key} must be a list of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{key}[{index}] must be a non-empty string")
        items.append(item)
    return tuple(items)


def _optional_str_mapping_tuple(
    data: Mapping[str, object], key: str
) -> tuple[tuple[str, str], ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object mapping strings to strings")
    items: list[tuple[str, str]] = []
    for map_key, map_value in value.items():
        if not isinstance(map_key, str) or not map_key:
            raise ValueError(f"{key} keys must be non-empty strings")
        if not isinstance(map_value, str) or not map_value:
            raise ValueError(f"{key}[{map_key!r}] must be a non-empty string")
        items.append((map_key, map_value))
    return tuple(sorted(items))
