"""OpenOnce — durable side effects for AI agent tool calls.

Honest guarantee: **at-least-once execution + idempotency + reconciliation**.
Exactly-once side effects against external systems are impossible from a local
process; what OpenOnce gives you is: duplicates get replayed instead of
re-executed, ambiguous outcomes are reconciled against the external world
instead of blindly retried, and every effect has an auditable receipt trail.
"""

from .client import OpenOnce, current_effect
from .errors import (
    ApprovalPending,
    EffectDenied,
    EffectInFlight,
    EffectUnknown,
    IdempotencyMismatch,
    KeyDerivationError,
    OpenOnceError,
    ReplayedFailure,
    RetryableEffectError,
    ScopeRequiredError,
    UnknownOutcomeError,
)
from .policy import Decision, Verdict, allow_all, require_approval_for
from .providers.audit import (
    EffectToolReference,
    ProviderCapabilitySuggestion,
    handler_contract_failures,
    receipt_contract_failures,
    scan_effect_tools,
    suggest_capabilities_for_refs,
)
from .providers.base import ProbeOutcome, Prober, ProbeResult
from .providers.capabilities import (
    CapabilityTier,
    ProviderCapability,
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
    minimum_builtin_grace_seconds,
    minimum_grace_seconds,
    provider_receipt_contract_failures,
    validate_capability_matrix,
)
from .providers.conformance import (
    CapabilityConformanceEvidence,
    ConformanceCase,
    ConformanceFailure,
    ConformanceReport,
    ConformanceScenario,
    assert_conformance,
    conformance_evidence_file_template,
    conformance_evidence_template,
    conformance_plan,
    load_conformance_file,
    validate_conformance,
    validate_conformance_evidence,
)
from .reconciler import Reconciler, ReconcileReport
from .records import EffectRecord, EffectResult, JournalEntry
from .runtime import (
    DEFAULT_RETRYABLE_EXCEPTION_NAMES,
    DEFAULT_UNKNOWN_EXCEPTION_NAMES,
    classify_exception,
)
from .state import EffectState
from .store import InMemoryStore, SQLiteStore, Store

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_RETRYABLE_EXCEPTION_NAMES",
    "DEFAULT_UNKNOWN_EXCEPTION_NAMES",
    "ApprovalPending",
    "CapabilityConformanceEvidence",
    "CapabilityTier",
    "ConformanceCase",
    "ConformanceFailure",
    "ConformanceReport",
    "ConformanceScenario",
    "Decision",
    "EffectDenied",
    "EffectInFlight",
    "EffectRecord",
    "EffectResult",
    "EffectState",
    "EffectToolReference",
    "EffectUnknown",
    "IdempotencyMismatch",
    "InMemoryStore",
    "JournalEntry",
    "KeyDerivationError",
    "OpenOnce",
    "OpenOnceError",
    "ProbeOutcome",
    "ProbeResult",
    "Prober",
    "ProviderCapability",
    "ProviderCapabilitySuggestion",
    "ReconcileReport",
    "Reconciler",
    "ReplayedFailure",
    "RetryableEffectError",
    "SQLiteStore",
    "ScopeRequiredError",
    "Store",
    "UnknownOutcomeError",
    "Verdict",
    "allow_all",
    "assert_conformance",
    "builtin_capabilities",
    "can_auto_rearm_on_miss",
    "capabilities_for_tool",
    "capability_fingerprint",
    "capability_from_dict",
    "capability_guidance_for_tool",
    "capability_matrix",
    "capability_readiness_errors",
    "classify_exception",
    "conformance_evidence_file_template",
    "conformance_evidence_template",
    "conformance_plan",
    "current_effect",
    "default_grace_for_tool",
    "handler_contract_failures",
    "load_capabilities_file",
    "load_conformance_file",
    "minimum_builtin_grace_seconds",
    "minimum_grace_seconds",
    "provider_receipt_contract_failures",
    "receipt_contract_failures",
    "require_approval_for",
    "scan_effect_tools",
    "suggest_capabilities_for_refs",
    "validate_capability_matrix",
    "validate_conformance",
    "validate_conformance_evidence",
]
