"""Idempotency key derivation: canonicalize -> whitelist -> hash.

Design decisions (each traceable to primary sources, see PLAN.md §1):

- Explicit caller-provided keys are first-class (AWS Builders' Library: identical
  parameters may represent *different intents* — never guess).
- When no explicit key is given, derive ``sha256(version | scope | tool | canonical)``
  where *canonical* is an RFC 8785 (JCS)-compatible serialization of the
  **whitelisted** argument fields. The whitelist (``idempotency_fields``) is how the
  caller declares which fields are the intent fingerprint; LLM-generated noise
  (timestamps, UUIDs, prose bodies) stays out of the hash.
- Floats are rejected in key material. RFC 8785 number formatting follows
  ECMAScript, which disagrees with Python's repr in edge cases (JS ``0.000001``
  vs Python ``1e-06``), so cross-language keys would silently drift. Amounts
  belong in integer minor units or decimal strings anyway.
- Semantic-equivalence dedup is deliberately NOT attempted: two differently
  worded calls are two different effects unless their canonical fields match.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import KeyDerivationError

KEY_VERSION = "oo1"

#: Integers beyond 2**53 lose precision when parsed as an IEEE double by a
#: JS/JCS implementation, so the same logical key would hash differently
#: across languages. Reject rather than drift.
_MAX_SAFE_INT = 2**53


def canonicalize(value: Any) -> str:
    """Serialize ``value`` to an RFC 8785 (JCS)-compatible canonical JSON string.

    Supported: None, bool, int (|v| <= 2**53), str, list, dict with str keys.
    Object keys are sorted by their UTF-16 code units, per RFC 8785.
    Floats and non-str dict keys raise :class:`KeyDerivationError`.
    """
    parts: list[str] = []
    _write(value, parts, path="$")
    return "".join(parts)


def _write(value: Any, out: list[str], path: str) -> None:
    if value is None or isinstance(value, bool):
        out.append(json.dumps(value))
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise KeyDerivationError(
                f"Integer at {path} exceeds 2**53 and would lose precision in "
                f"cross-language canonicalization. Pass it as a string."
            )
        out.append(str(value))
    elif isinstance(value, float):
        raise KeyDerivationError(
            f"Float at {path} cannot be part of an idempotency key: float "
            f"canonicalization differs across languages (RFC 8785 uses ECMAScript "
            f"formatting). Use integer minor units (e.g. cents) or a decimal string, "
            f"or exclude this field via idempotency_fields."
        )
    elif isinstance(value, str):
        # Python's json escaping matches JCS: short escapes for \b \t \n \f \r,
        # lowercase \u00xx for other control chars, non-ASCII passed through.
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out, f"{path}[{i}]")
        out.append("]")
    elif isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise KeyDerivationError(f"Non-string dict key {k!r} at {path}.")
        out.append("{")
        # RFC 8785: recursively sort object properties by UTF-16 code units.
        for i, k in enumerate(sorted(value, key=lambda s: s.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(json.dumps(k, ensure_ascii=False))
            out.append(":")
            _write(value[k], out, f"{path}.{k}")
        out.append("}")
    else:
        raise KeyDerivationError(
            f"Unsupported type {type(value).__name__} at {path}. Idempotency key "
            f"material must be JSON-native (None/bool/int/str/list/dict)."
        )


def select_fields(args: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """Project ``args`` onto the intent-fingerprint whitelist.

    ``None`` means all fields participate. Missing whitelisted fields are simply
    absent (deterministic either way, since canonicalization sorts keys).
    """
    if fields is None:
        return args
    return {f: args[f] for f in fields if f in args}


def derive_key(tool: str, args: dict[str, Any], *, scope: str, fields: list[str] | None) -> str:
    """Derive a deterministic idempotency key for a tool call.

    ``sha256("oo1|" + scope + "|" + tool + "|" + canonical(whitelisted args))``
    """
    canonical = canonicalize(select_fields(args, fields))
    material = f"{KEY_VERSION}|{scope}|{tool}|{canonical}"
    return f"{KEY_VERSION}_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def fingerprint(payload: Any) -> str:
    """Stable fingerprint of key material, used for same-key/different-payload
    rejection (Stripe: reusing a key with different parameters is an error)."""
    return hashlib.sha256(canonicalize(payload).encode("utf-8")).hexdigest()
