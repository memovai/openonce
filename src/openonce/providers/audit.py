"""Static provider audits for OpenOnce effect declarations."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .capabilities import ProviderCapability

_EFFECT_DECORATORS = {"effect", "effect_tool", "effect_function_tool"}
_SKIP_DIRS = {".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
_LiteralConstant = str | tuple[str, ...]
_ReturnFieldSources = tuple[tuple[str, str], ...]
_ReturnContract = tuple[tuple[str, ...], _ReturnFieldSources]
_LocalReturnContract = tuple[int, _ReturnContract]


@dataclass(frozen=True)
class EffectToolReference:
    path: str
    line: int
    decorator: str
    tool: str | None
    function: str
    function_args: tuple[str, ...]
    idempotency_fields: tuple[str, ...] | None = None
    dynamic_idempotency_fields: bool = False
    reassigned_handler_args: tuple[str, ...] = ()
    return_field_sets: tuple[tuple[str, ...], ...] = ()
    return_field_sources: tuple[_ReturnFieldSources, ...] = ()
    dynamic_return: bool = False

    @property
    def dynamic(self) -> bool:
        return self.tool is None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "decorator": self.decorator,
            "tool": self.tool,
            "function": self.function,
            "function_args": list(self.function_args),
            "idempotency_fields": (
                None if self.idempotency_fields is None else list(self.idempotency_fields)
            ),
            "dynamic_idempotency_fields": self.dynamic_idempotency_fields,
            "reassigned_handler_args": list(self.reassigned_handler_args),
            "return_field_sets": [list(fields) for fields in self.return_field_sets],
            "return_field_sources": [dict(sources) for sources in self.return_field_sources],
            "dynamic_return": self.dynamic_return,
            "dynamic": self.dynamic,
        }


@dataclass(frozen=True)
class ProviderCapabilitySuggestion:
    tool: str
    refs: tuple[EffectToolReference, ...]
    capability: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "refs": [ref.to_dict() for ref in self.refs],
            "capability": self.capability,
        }


def scan_effect_tools(paths: list[str | Path]) -> tuple[EffectToolReference, ...]:
    """Find OpenOnce effect decorators with statically known ``tool=...`` values."""
    refs: list[EffectToolReference] = []
    for file_path in _iter_python_files(paths):
        refs.extend(_scan_python_file(file_path))
    return tuple(sorted(refs, key=lambda ref: (ref.path, ref.line, ref.function)))


def suggest_capabilities_for_refs(
    refs: tuple[EffectToolReference, ...],
    capabilities: tuple[ProviderCapability, ...],
) -> tuple[ProviderCapabilitySuggestion, ...]:
    """Return conservative custom capability stubs for unknown scanned tools."""
    grouped: dict[str, list[EffectToolReference]] = {}
    for ref in refs:
        if ref.tool is None:
            continue
        if any(capability.matches(ref.tool) for capability in capabilities):
            continue
        grouped.setdefault(ref.tool, []).append(ref)

    used_names = {capability.name for capability in capabilities}
    suggestions: list[ProviderCapabilitySuggestion] = []
    for tool in sorted(grouped):
        name = _unique_suggestion_name(tool, used_names)
        used_names.add(name)
        suggestions.append(
            ProviderCapabilitySuggestion(
                tool=tool,
                refs=tuple(grouped[tool]),
                capability={
                    "name": name,
                    "tool_pattern": tool,
                    "tier": "tier_3_non_authoritative",
                    "key_strategy": (
                        "TODO: document provider key, natural key, or sender-controlled key"
                    ),
                    "probe_basis": "TODO: document the authoritative read/probe source",
                    "miss_semantics": (
                        "inconclusive until a provider-specific prober proves otherwise"
                    ),
                    "can_auto_rearm_on_miss": False,
                    "default_grace_seconds": 300.0,
                    "prober": None,
                    "handler_requirements": [
                        "TODO: stamp current_effect().provider_key or a deterministic natural key",
                        "TODO: add a Prober and conformance evidence before enabling auto-rearm",
                    ],
                    "risk": "TODO: document duplicate side-effect risk and false-miss behavior",
                    "required_args": [],
                    "required_idempotency_fields": [],
                    "required_receipt_fields": [],
                    "required_receipt_source_fields": {},
                },
            )
        )
    return tuple(suggestions)


def handler_contract_failures(
    ref: EffectToolReference,
    capabilities: tuple[ProviderCapability, ...],
) -> tuple[str, ...]:
    """Validate statically-checkable handler contracts for matched capabilities."""
    failures: list[str] = []
    for capability in capabilities:
        if capability.required_args:
            missing = tuple(
                field for field in capability.required_args if field not in ref.function_args
            )
            if missing:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    f"requires handler args: {', '.join(missing)}"
                )
        if capability.required_idempotency_fields:
            idempotency_args_missing = tuple(
                field
                for field in capability.required_idempotency_fields
                if field not in capability.required_args
            )
            if idempotency_args_missing:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    "requires idempotency field(s) that are not required_args: "
                    f"{', '.join(idempotency_args_missing)}"
                )
            if ref.dynamic_idempotency_fields:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    "requires literal idempotency_fields for static audit"
                )
                continue
            if ref.idempotency_fields is None:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    "requires idempotency_fields: "
                    f"{', '.join(capability.required_idempotency_fields)}"
                )
                continue
            missing = tuple(
                field
                for field in capability.required_idempotency_fields
                if field not in ref.idempotency_fields
            )
            if missing:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    f"requires idempotency_fields: {', '.join(missing)}"
                )
    return tuple(failures)


def receipt_contract_failures(
    ref: EffectToolReference,
    capabilities: tuple[ProviderCapability, ...],
) -> tuple[str, ...]:
    """Validate statically-checkable handler return receipt contracts."""
    failures: list[str] = []
    for capability in capabilities:
        if not capability.required_receipt_fields:
            continue
        if ref.dynamic_return or not ref.return_field_sets:
            failures.append(
                f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                "requires literal return dicts for static receipt audit"
            )
            continue
        for index, fields in enumerate(ref.return_field_sets):
            duplicates = _duplicate_values(fields)
            if duplicates:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    f"return[{index}] contains duplicate receipt fields: "
                    f"{', '.join(duplicates)}"
                )
                continue
            missing = tuple(
                field for field in capability.required_receipt_fields if field not in fields
            )
            if missing:
                failures.append(
                    f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                    f"return[{index}] is missing receipt fields: {', '.join(missing)}"
                )
                continue
            source_failures = _receipt_source_failures(ref, capability, index, fields)
            failures.extend(source_failures)
    return tuple(failures)


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


def _receipt_source_failures(
    ref: EffectToolReference,
    capability: ProviderCapability,
    index: int,
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    if not capability.required_receipt_source_fields:
        return ()
    sources = dict(ref.return_field_sources[index]) if index < len(ref.return_field_sources) else {}
    failures: list[str] = []
    for receipt_field, source_field in capability.required_receipt_source_fields:
        if receipt_field not in fields:
            continue
        if source_field not in ref.function_args:
            failures.append(
                f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                f"return[{index}] receipt field {receipt_field!r} must come from handler "
                f"arg {source_field!r}, but the handler has no such arg"
            )
            continue
        if source_field in ref.reassigned_handler_args:
            failures.append(
                f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
                f"return[{index}] receipt field {receipt_field!r} must come from handler "
                f"arg {source_field!r}, but that arg is reassigned in the handler body"
            )
            continue
        actual = sources.get(receipt_field)
        if actual == source_field:
            continue
        got = f"; got {actual!r}" if actual is not None else ""
        failures.append(
            f"{ref.path}:{ref.line}: {ref.tool!r} capability {capability.name!r} "
            f"return[{index}] receipt field {receipt_field!r} must come from handler "
            f"arg {source_field!r}{got}"
        )
    return tuple(failures)


def _unique_suggestion_name(tool: str, used_names: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", tool).strip("_").lower() or "provider"
    if base[0].isdigit():
        base = f"provider_{base}"
    name = base
    index = 2
    while name in used_names:
        name = f"{base}_{index}"
        index += 1
    return name


def _iter_python_files(paths: list[str | Path]) -> tuple[Path, ...]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            if path.suffix == ".py":
                files.append(path)
            continue
        if path.is_dir():
            for child in path.rglob("*.py"):
                if any(part in _SKIP_DIRS for part in child.parts):
                    continue
                files.append(child)
            continue
        raise ValueError(f"scan path does not exist: {path}")
    return tuple(sorted(files))


def _scan_python_file(path: Path) -> tuple[EffectToolReference, ...]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"{path}: cannot parse Python: {exc.msg}") from exc

    refs: list[EffectToolReference] = []
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            decorator_constants = _module_constants(
                tree, before_line=getattr(decorator, "lineno", node.lineno)
            )
            parsed = _parse_effect_decorator(decorator, decorator_constants)
            if parsed is None:
                continue
            assert isinstance(decorator, ast.Call)
            name, tool = parsed
            function_args = _function_arg_names(node)
            idempotency_fields = _parse_idempotency_fields(decorator, decorator_constants)
            return_field_sets, return_field_sources, dynamic_return = _parse_return_contract(
                node, constants
            )
            refs.append(
                EffectToolReference(
                    path=str(path),
                    line=getattr(decorator, "lineno", node.lineno),
                    decorator=name,
                    tool=tool,
                    function=node.name,
                    function_args=function_args,
                    idempotency_fields=idempotency_fields,
                    dynamic_idempotency_fields=_has_dynamic_idempotency_fields(
                        decorator, decorator_constants
                    ),
                    reassigned_handler_args=_reassigned_handler_args(node, function_args),
                    return_field_sets=return_field_sets,
                    return_field_sources=return_field_sources,
                    dynamic_return=dynamic_return,
                )
            )
    return tuple(refs)


def _parse_effect_decorator(
    decorator: ast.expr,
    constants: dict[str, _LiteralConstant],
) -> tuple[str, str | None] | None:
    if not isinstance(decorator, ast.Call):
        return None
    name = _decorator_name(decorator.func)
    if name not in _EFFECT_DECORATORS:
        return None
    for keyword in decorator.keywords:
        if keyword.arg != "tool":
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return name, keyword.value.value
        if isinstance(keyword.value, ast.Name):
            constant = constants.get(keyword.value.id)
            if isinstance(constant, str):
                return name, constant
        return name, None
    return None


def _function_arg_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    args = [arg.arg for arg in node.args.posonlyargs]
    args.extend(arg.arg for arg in node.args.args)
    args.extend(arg.arg for arg in node.args.kwonlyargs)
    return tuple(args)


def _reassigned_handler_args(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    function_args: tuple[str, ...],
) -> tuple[str, ...]:
    collector = _ReassignedHandlerArgCollector(frozenset(function_args))
    for statement in node.body:
        collector.visit(statement)
    return tuple(sorted(collector.names))


class _ReassignedHandlerArgCollector(ast.NodeVisitor):
    _MUTATING_METHODS = frozenset(
        {
            "append",
            "clear",
            "extend",
            "insert",
            "pop",
            "popitem",
            "remove",
            "reverse",
            "setdefault",
            "sort",
            "update",
        }
    )

    def __init__(self, function_args: frozenset[str]) -> None:
        self.function_args = function_args
        self.names: set[str] = set()

    def visit_Assign(self, node: ast.Assign) -> None:
        self._record_targets(node.targets)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        self._record_targets(node.targets)

    def visit_For(self, node: ast.For) -> None:
        self._record_target(node.target)
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._record_target(node.target)
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._record_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._record_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._record_name(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record_target(node.target)
        self.visit(node.value)

    def visit_Match(self, node: ast.Match) -> None:
        for case in node.cases:
            for name in _pattern_bound_names(case.pattern):
                self._record_name(name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in self._MUTATING_METHODS
        ):
            self._record_name(node.func.value.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record_name(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            self._record_name(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # pragma: no cover - defensive
        return

    def visit_AsyncFunctionDef(
        self, node: ast.AsyncFunctionDef
    ) -> None:  # pragma: no cover - defensive
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # pragma: no cover - defensive
        return

    def _record_targets(self, targets: list[ast.expr]) -> None:
        for target in targets:
            self._record_target(target)

    def _record_target(self, target: ast.expr) -> None:
        for name in _mutated_names(target):
            self._record_name(name)

    def _record_name(self, name: str) -> None:
        if name in self.function_args:
            self.names.add(name)


def _parse_idempotency_fields(
    decorator: ast.Call,
    constants: dict[str, _LiteralConstant],
) -> tuple[str, ...] | None:
    for keyword in decorator.keywords:
        if keyword.arg != "idempotency_fields":
            continue
        value = keyword.value
        if isinstance(value, ast.Name):
            constant = constants.get(value.id)
            if isinstance(constant, tuple):
                return constant
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None
        fields: list[str] = []
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            fields.append(item.value)
        return tuple(fields)
    return None


def _has_dynamic_idempotency_fields(
    decorator: ast.Call,
    constants: dict[str, _LiteralConstant],
) -> bool:
    return any(
        keyword.arg == "idempotency_fields"
        and _parse_idempotency_fields(decorator, constants) is None
        for keyword in decorator.keywords
    )


def _parse_return_contract(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    constants: dict[str, _LiteralConstant],
) -> tuple[tuple[tuple[str, ...], ...], tuple[_ReturnFieldSources, ...], bool]:
    returns = _return_nodes(node)
    local_receipts = _local_return_dicts(node, constants)
    field_sets: list[tuple[str, ...]] = []
    field_sources: list[_ReturnFieldSources] = []
    dynamic = False
    for return_node in returns:
        parsed = _parse_return_fields(
            return_node.value,
            constants,
            local_receipts,
            return_line=return_node.lineno,
        )
        if parsed is None:
            dynamic = True
            continue
        fields, sources = parsed
        field_sets.append(fields)
        field_sources.append(sources)
    return tuple(field_sets), tuple(field_sources), dynamic


def _return_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.Return, ...]:
    collector = _ReturnCollector()
    for statement in node.body:
        collector.visit(statement)
    return tuple(collector.returns)


class _ReturnCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.returns: list[ast.Return] = []

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # pragma: no cover - defensive
        return

    def visit_AsyncFunctionDef(
        self, node: ast.AsyncFunctionDef
    ) -> None:  # pragma: no cover - defensive
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # pragma: no cover - defensive
        return


def _parse_return_dict_contract(
    node: ast.expr | None,
    constants: dict[str, _LiteralConstant],
) -> _ReturnContract | None:
    if isinstance(node, ast.Dict):
        return _dict_literal_contract(node, constants)
    if isinstance(node, ast.Call):
        return _dict_call_contract(node)
    return None


def _parse_return_fields(
    node: ast.expr | None,
    constants: dict[str, _LiteralConstant],
    local_receipts: dict[str, _LocalReturnContract],
    *,
    return_line: int,
) -> _ReturnContract | None:
    if isinstance(node, ast.Name):
        local = local_receipts.get(node.id)
        if local is None:
            return None
        assignment_line, contract = local
        if assignment_line >= return_line:
            return None
        return contract
    return _parse_return_dict_contract(node, constants)


def _dict_literal_contract(
    node: ast.Dict,
    constants: dict[str, _LiteralConstant],
) -> _ReturnContract | None:
    fields: list[str] = []
    sources: list[tuple[str, str]] = []
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            return None
        field = _literal_string(key, constants)
        if field is None:
            return None
        fields.append(field)
        if isinstance(value, ast.Name):
            sources.append((field, value.id))
    return tuple(fields), tuple(sources)


def _dict_call_contract(node: ast.Call) -> _ReturnContract | None:
    if not isinstance(node.func, ast.Name) or node.func.id != "dict" or node.args:
        return None
    fields: list[str] = []
    sources: list[tuple[str, str]] = []
    for keyword in node.keywords:
        if keyword.arg is None:
            return None
        fields.append(keyword.arg)
        if isinstance(keyword.value, ast.Name):
            sources.append((keyword.arg, keyword.value.id))
    return tuple(fields), tuple(sources)


def _local_return_dicts(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    constants: dict[str, _LiteralConstant],
) -> dict[str, _LocalReturnContract]:
    collector = _LocalReturnDictCollector(constants)
    for statement in node.body:
        collector.visit_top_level(statement)
    return collector.values()


class _LocalReturnDictCollector(ast.NodeVisitor):
    _MUTATING_METHODS = frozenset({"clear", "pop", "popitem", "setdefault", "update"})

    def __init__(self, constants: dict[str, _LiteralConstant]) -> None:
        self.constants = constants
        self._values: dict[str, _LocalReturnContract] = {}
        self._invalid: set[str] = set()
        self._depth = 0

    def values(self) -> dict[str, _LocalReturnContract]:
        return {name: value for name, value in self._values.items() if name not in self._invalid}

    def visit_top_level(self, node: ast.stmt) -> None:
        previous = self._depth
        self._depth = 0
        try:
            self.visit(node)
        finally:
            self._depth = previous

    def generic_visit(self, node: ast.AST) -> None:
        self._depth += 1
        try:
            super().generic_visit(node)
        finally:
            self._depth -= 1

    def visit_Assign(self, node: ast.Assign) -> None:
        parsed = _parse_return_dict_contract(node.value, self.constants)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._record(target.id, parsed, line=node.lineno)
            else:
                for mutated in _mutated_names(target):
                    self._invalidate(mutated)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._record(
                node.target.id,
                _parse_return_dict_contract(node.value, self.constants),
                line=node.lineno,
            )
        else:
            for mutated in _mutated_names(node.target):
                self._invalidate(mutated)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        for mutated in _mutated_names(node.target):
            self._invalidate(mutated)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            for mutated in _mutated_names(target):
                self._invalidate(mutated)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        for mutated in _mutated_names(node.target):
            self._invalidate(mutated)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        for mutated in _mutated_names(node.target):
            self._invalidate(mutated)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                for mutated in _mutated_names(item.optional_vars):
                    self._invalidate(mutated)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                for mutated in _mutated_names(item.optional_vars):
                    self._invalidate(mutated)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._invalidate(node.name)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        for case in node.cases:
            for name in _pattern_bound_names(case.pattern):
                self._invalidate(name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in self._MUTATING_METHODS
        ):
            self._invalidate(node.func.value.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # pragma: no cover - defensive
        return

    def visit_AsyncFunctionDef(
        self, node: ast.AsyncFunctionDef
    ) -> None:  # pragma: no cover - defensive
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # pragma: no cover - defensive
        return

    def _record(self, name: str, parsed: _ReturnContract | None, *, line: int) -> None:
        if self._depth > 0 or name in self._values or parsed is None:
            self._invalidate(name)
            return
        self._values[name] = (line, parsed)

    def _invalidate(self, name: str) -> None:
        self._invalid.add(name)
        self._values.pop(name, None)


def _mutated_names(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return (node.value.id,)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return (node.value.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for item in node.elts for name in _mutated_names(item))
    return ()


def _pattern_bound_names(pattern: ast.pattern) -> tuple[str, ...]:
    names: set[str] = set()

    def collect(current: ast.pattern) -> None:
        if isinstance(current, ast.MatchAs):
            if current.name is not None:
                names.add(current.name)
            if current.pattern is not None:
                collect(current.pattern)
            return
        if isinstance(current, ast.MatchStar):
            if current.name is not None:
                names.add(current.name)
            return
        if isinstance(current, ast.MatchMapping):
            if current.rest is not None:
                names.add(current.rest)
            for nested in current.patterns:
                collect(nested)
            return
        if isinstance(current, ast.MatchClass):
            for nested in (*current.patterns, *current.kwd_patterns):
                collect(nested)
            return
        if isinstance(current, ast.MatchSequence):
            for nested in current.patterns:
                collect(nested)
            return
        if isinstance(current, ast.MatchOr):
            for nested in current.patterns:
                collect(nested)

    collect(pattern)
    return tuple(sorted(names))


def _module_constants(
    tree: ast.Module,
    *,
    before_line: int | None = None,
) -> dict[str, _LiteralConstant]:
    values: dict[str, _LiteralConstant] = {}
    invalid: set[str] = set()
    for statement in tree.body:
        if before_line is not None and statement.lineno >= before_line:
            break
        if isinstance(statement, ast.Assign):
            parsed = _literal_constant(statement.value)
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    _record_module_constant(target.id, parsed, values, invalid)
                else:
                    for name in _mutated_names(target):
                        _invalidate_module_constant(name, values, invalid)
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            _record_module_constant(
                statement.target.id,
                _literal_constant(statement.value),
                values,
                invalid,
            )
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            for name in _mutated_names(statement.target):
                _invalidate_module_constant(name, values, invalid)
        elif isinstance(statement, ast.Delete):
            for target in statement.targets:
                for name in _mutated_names(target):
                    _invalidate_module_constant(name, values, invalid)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            for name in _mutated_names(statement.target):
                _invalidate_module_constant(name, values, invalid)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                if item.optional_vars is not None:
                    for name in _mutated_names(item.optional_vars):
                        _invalidate_module_constant(name, values, invalid)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _invalidate_module_constant(statement.name, values, invalid)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                _invalidate_module_constant(
                    alias.asname or alias.name.split(".", 1)[0],
                    values,
                    invalid,
                )
        elif isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                if alias.name == "*":
                    continue
                _invalidate_module_constant(alias.asname or alias.name, values, invalid)
        else:
            for name in _module_bound_names(statement):
                _invalidate_module_constant(name, values, invalid)
    return {name: value for name, value in values.items() if name not in invalid}


def _record_module_constant(
    name: str,
    value: _LiteralConstant | None,
    values: dict[str, _LiteralConstant],
    invalid: set[str],
) -> None:
    if name in values or value is None:
        invalid.add(name)
        values.pop(name, None)
        return
    values[name] = value


def _invalidate_module_constant(
    name: str,
    values: dict[str, _LiteralConstant],
    invalid: set[str],
) -> None:
    invalid.add(name)
    values.pop(name, None)


def _module_bound_names(statement: ast.stmt) -> tuple[str, ...]:
    collector = _ModuleBindingCollector()
    collector.visit(statement)
    return tuple(sorted(collector.names))


class _ModuleBindingCollector(ast.NodeVisitor):
    """Collect names rebound by top-level compound statements.

    ``_module_constants`` handles simple top-level assignments inline because it
    needs their literal values. This collector is for compound statements where
    a binding may be conditional or hidden in a branch; any such binding makes a
    previous literal constant unsafe for provider proof.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_target(node.target)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record_target(target)

    def visit_For(self, node: ast.For) -> None:
        self._record_target(node.target)
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._record_target(node.target)
        self.visit(node.iter)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._record_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._record_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_Try(self, node: ast.Try) -> None:
        for statement in (*node.body, *node.orelse, *node.finalbody):
            self.visit(statement)
        for handler in node.handlers:
            self.visit(handler)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.names.update(_pattern_bound_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record_target(node.target)
        self.visit(node.value)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            self.names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def _record_target(self, target: ast.expr) -> None:
        self.names.update(_mutated_names(target))


def _literal_constant(node: ast.expr | None) -> _LiteralConstant | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        fields: list[str] = []
        for item in node.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            fields.append(item.value)
        return tuple(fields)
    return None


def _literal_string(node: ast.expr, constants: dict[str, _LiteralConstant]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        constant = constants.get(node.id)
        if isinstance(constant, str):
            return constant
    return None


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
