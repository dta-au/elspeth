"""Closed Task 6 inventory for web-reachable Landscape mutations.

The gate is intentionally RED until Task 6 installs one current-token-bound
Landscape mutation capability.  Its inventory is production-only and
bidirectional: the canonical digests freeze every current DML construction and
production call identity, while the structural checks reject authority aliases,
callable escapes, raw write surfaces, cross-database access, and transactions
whose first database effect is not the full Landscape leader-token fence.

There is one deliberately narrow, non-release creation exception.  Until Task
8B, ``RunLifecycleRepository.begin_run`` may create the run and epoch-1 leader
seat in one transaction through ``register_run_leader_on``.  The exception is
an exact edge and write set, not a repository, file, prefix, or wildcard
allowance.  The standalone ``register_run_leader`` wrapper is never admitted.
"""

from __future__ import annotations

import ast
import hashlib
import re
import textwrap
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MutationApi:
    path: str
    owner: str
    method: str
    category: str

    @property
    def symbol(self) -> str:
        return f"{self.owner}.{self.method}"


@dataclass(frozen=True, slots=True)
class DmlIdentity:
    path: str
    symbol: str
    table: str
    operation: str
    fingerprint: str
    ordinal: int
    authority: str
    line: int = 0


@dataclass(frozen=True, slots=True)
class CallIdentity:
    path: str
    symbol: str
    method: str
    receiver: str
    ordinal: int
    line: int = 0


@dataclass(frozen=True, slots=True)
class AuthorityEstablishmentException:
    classification: str
    caller_path: str
    caller_symbol: str
    callee_path: str
    callee_symbol: str
    write_counts: tuple[tuple[str, str, int], ...]
    temporary: bool
    sunset: str | None


@dataclass(frozen=True, slots=True)
class SubordinateHelperEdge:
    helper_path: str
    helper_symbol: str
    caller_path: str
    caller_symbol: str
    call_fingerprint: str
    ordinal: int
    line: int = 0


@dataclass(frozen=True, slots=True)
class FencedContext:
    owner: ast.With | ast.AsyncWith
    call: ast.Call
    connection: str | None


@dataclass(frozen=True, slots=True)
class SourceUnit:
    path: str
    source: str
    tree: ast.Module


class InventoryScanError(AssertionError):
    """Production source could not be decoded or parsed exactly."""


_RUN_LIFECYCLE_PATH = "src/elspeth/core/landscape/run_lifecycle_repository.py"
_DATA_FLOW_PATH = "src/elspeth/core/landscape/data_flow_repository.py"
_EXECUTION_PATH = "src/elspeth/core/landscape/execution_repository.py"
_SCHEDULER_PATH = "src/elspeth/core/landscape/scheduler_repository.py"
_SINK_EFFECT_PATH = "src/elspeth/core/landscape/execution/sink_effects.py"
_CHECKPOINT_PATH = "src/elspeth/core/checkpoint/manager.py"
_AUDIT_EXPORT_PATH = "src/elspeth/core/landscape/execution/audit_export_snapshots.py"


def _apis(path: str, owner: str, category: str, methods: Sequence[str]) -> tuple[MutationApi, ...]:
    return tuple(MutationApi(path, owner, method, category) for method in methods)


# This is the complete public mutation facade measured before Task 6.  Keeping
# the methods literal makes removal and replacement reviewable; counts alone
# cannot silently exchange one verb for another.
_MUTATION_APIS: tuple[MutationApi, ...] = (
    *_apis(
        _RUN_LIFECYCLE_PATH,
        "RunLifecycleRepository",
        "run-lifecycle",
        (
            "begin_run",
            "complete_run",
            "record_source_field_resolution",
            "record_run_source",
            "update_run_source_contract",
            "update_run_status",
            "record_secret_resolutions",
            "record_preflight_results",
            "record_readiness_check",
            "set_export_status",
            "set_export_failed_unless_completed",
            "set_export_pending_unless_completed",
            "finalize_run",
        ),
    ),
    *_apis(
        _DATA_FLOW_PATH,
        "DataFlowRepository",
        "data-flow",
        (
            "create_row",
            "create_row_with_token",
            "insert_row_with_token_on",
            "create_token",
            "fork_token",
            "coalesce_tokens",
            "finalize_coalesce_effect",
            "expand_token",
            "record_token_outcome",
            "register_node",
            "register_edge",
            "update_node_output_contract",
            "record_validation_error",
            "link_validation_error_to_row",
            "record_transform_error",
        ),
    ),
    *_apis(
        _EXECUTION_PATH,
        "ExecutionRepository",
        "execution",
        (
            "begin_node_state",
            "record_completed_node_state",
            "record_completed_node_state_on",
            "reconcile_source_completions_from_scheduler",
            "begin_node_states_many",
            "complete_node_state",
            "complete_node_states_completed_many",
            "record_routing_event",
            "record_routing_events",
            "allocate_call_index",
            "record_call",
            "begin_operation",
            "complete_operation",
            "allocate_operation_call_index",
            "record_operation_call",
            "create_batch",
            "add_batch_member",
            "update_batch_status",
            "complete_batch",
            "retry_batch",
            "register_artifact",
        ),
    ),
    *_apis(
        _SCHEDULER_PATH,
        "TokenSchedulerRepository",
        "scheduler",
        (
            "enqueue_ready",
            "enqueue_ready_claimed",
            "enqueue_ready_claimed_legacy_unfenced",
            "ingest_row_with_initial_claim",
            "claim_ready",
            "claim_pending_sink",
            "recover_expired_leases",
            "recover_expired_leases_legacy_unfenced",
            "heartbeat_lease",
            "mark_blocked",
            "mark_terminal",
            "mark_terminal_with_ready_children",
            "mark_failed",
            "mark_failed_with_ready_children",
            "mark_pending_sink",
            "mark_pending_sink_with_ready_children",
            "mark_pending_sink_terminal",
            "mark_pending_sink_terminal_many",
            "terminalize_pending_sinks_with_terminal_outcomes",
            "complete_barrier",
            "mark_blocked_barrier_pending_sink_many",
            "mark_blocked_barrier_terminal",
            "adopt_blocked_barrier_item",
            "reset_adoption_marker_to_pending",
            "adopt_coalesce_branch_losses",
        ),
    ),
    *_apis(
        _SINK_EFFECT_PATH,
        "SinkEffectRepository",
        "sink-effect",
        (
            "reserve",
            "claim_preparation",
            "complete_plan",
            "acquire_lease",
            "heartbeat_lease",
            "takeover_expired",
            "begin_attempt",
            "record_attempt_result",
            "complete_member_result",
            "mark_response_lost",
            "finalize",
        ),
    ),
    *_apis(
        _CHECKPOINT_PATH,
        "CheckpointManager",
        "checkpoint",
        ("create_checkpoint", "delete_checkpoints"),
    ),
    *_apis(
        _AUDIT_EXPORT_PATH,
        "AuditExportSnapshotRepository",
        "audit-export",
        ("register_candidate", "register_verified_candidate", "bind_winner"),
    ),
)

_EXPECTED_API_CATEGORY_COUNTS = {
    "run-lifecycle": 13,
    "data-flow": 15,
    "execution": 21,
    "scheduler": 25,
    "sink-effect": 11,
    "checkpoint": 2,
    "audit-export": 3,
}

_FRESH_EPOCH_ONE_EXCEPTION = AuthorityEstablishmentException(
    classification="fresh-run-epoch-1-creation",
    caller_path=_RUN_LIFECYCLE_PATH,
    caller_symbol="RunLifecycleRepository.begin_run",
    callee_path="src/elspeth/core/landscape/run_coordination_repository.py",
    callee_symbol="RunCoordinationRepository.register_run_leader_on",
    write_counts=(
        ("run_attributions", "insert", 1),
        ("run_coordination", "insert", 1),
        ("run_coordination_events", "insert", 2),
        ("run_web_plugin_policy", "insert", 1),
        ("run_workers", "insert", 1),
        ("runs", "insert", 1),
    ),
    temporary=True,
    sunset="Task 8B mandatory sunset; non-release exception",
)

_EXISTING_RUN_LEADERSHIP_ESTABLISHMENT = AuthorityEstablishmentException(
    classification="existing-run-leadership-claim",
    caller_path="src/elspeth/core/landscape/run_coordination_repository.py",
    caller_symbol="RunCoordinationRepository.acquire_run_leadership",
    callee_path="src/elspeth/core/landscape/run_coordination_repository.py",
    callee_symbol="RunCoordinationRepository._acquire_run_leadership_on",
    write_counts=(
        ("run_coordination", "update", 1),
        ("run_coordination_events", "insert", 3),
        ("run_workers", "insert", 1),
        ("run_workers", "update", 1),
        ("runs", "update", 1),
    ),
    temporary=False,
    sunset=None,
)

_FOLLOWER_MEMBERSHIP_ESTABLISHMENT = AuthorityEstablishmentException(
    classification="follower-membership-admission",
    caller_path="src/elspeth/core/landscape/run_coordination_repository.py",
    caller_symbol="RunCoordinationRepository.admit_follower",
    callee_path="src/elspeth/core/landscape/run_coordination_repository.py",
    callee_symbol="RunCoordinationRepository._insert_worker_row",
    write_counts=(
        ("run_coordination_events", "insert", 1),
        ("run_workers", "insert", 1),
    ),
    temporary=False,
    sunset=None,
)

_AUTHORITY_ESTABLISHMENTS = (
    _FRESH_EPOCH_ONE_EXCEPTION,
    _EXISTING_RUN_LEADERSHIP_ESTABLISHMENT,
    _FOLLOWER_MEMBERSHIP_ESTABLISHMENT,
)
_AUTHORITY_ESTABLISHMENT_EXCEPTIONS = tuple(item for item in _AUTHORITY_ESTABLISHMENTS if item.temporary)
_EXACT_BEGIN_RUN_PRODUCTION_CALLERS = frozenset(
    {
        ("src/elspeth/engine/orchestrator/run_lifecycle.py", "RunLifecycleCoordinator.initialize_database_phase"),
        ("src/elspeth/web/_aws_ecs_acceptance/bedrock.py", "run_bedrock_guardrails_live"),
    }
)

_AUTHORITY_PARAMETER_NAMES = frozenset({"coordination_token", "token"})
_FENCED_CONTEXT_NAMES = frozenset({"fenced_leader_transaction", "fenced_write"})
_TRUSTED_FENCE_QUALIFIED = frozenset(
    {
        "elspeth.core.landscape.run_coordination_repository.fenced_leader_transaction",
        "elspeth.core.landscape.scheduler.fencing.fenced_write",
    }
)
_MUTATION_METHOD_NAMES = frozenset(api.method for api in _MUTATION_APIS)
_COORDINATION_MUTATION_METHOD_NAMES = frozenset(
    {
        "register_run_leader",
        "register_run_leader_on",
        "acquire_run_leadership",
        "release_seat",
        "record_fence_refusal",
        "record_heartbeat_degraded",
        "worker_heartbeat",
        "admit_follower",
        "depart_worker",
        "evict_worker",
    }
)
_ALL_MUTATION_METHOD_NAMES = _MUTATION_METHOD_NAMES | _COORDINATION_MUTATION_METHOD_NAMES

# Filled from the canonical scanners below.  These literals intentionally
# represent the pre-Task-6 surface; production migration may satisfy the
# authority tests without silently adding, deleting, moving, or replacing a
# write/caller identity.
_EXPECTED_DML_COUNT = 126
_EXPECTED_DML_INVENTORY_SHA256 = "a18761e893e6837d20cd2437fbcc371fac1d1c461438ae2b432e9ba27d862ee7"
_EXPECTED_DML_WRITE_SET: frozenset[tuple[str, str]] = frozenset(
    {
        ("artifacts", "insert"),
        ("audit_export_snapshot_chunks", "insert"),
        ("audit_export_snapshots", "insert"),
        ("auth_events", "insert"),
        ("batch_members", "insert"),
        ("batches", "insert"),
        ("batches", "update"),
        ("calls", "insert"),
        ("calls", "update"),
        ("checkpoints", "delete"),
        ("checkpoints", "insert"),
        ("coalesce_branch_losses", "insert"),
        ("coalesce_branch_losses", "update"),
        ("coalesce_effect_members", "insert"),
        ("coalesce_effect_members", "update"),
        ("coalesce_effects", "insert"),
        ("coalesce_effects", "update"),
        ("edges", "insert"),
        ("node_states", "insert"),
        ("node_states", "update"),
        ("nodes", "insert"),
        ("nodes", "update"),
        ("operations", "insert"),
        ("operations", "update"),
        ("preflight_results", "insert"),
        ("routing_events", "insert"),
        ("rows", "insert"),
        ("run_attributions", "insert"),
        ("run_coordination", "insert"),
        ("run_coordination", "update"),
        ("run_coordination_events", "insert"),
        ("run_sources", "insert"),
        ("run_sources", "update"),
        ("run_web_plugin_policy", "insert"),
        ("run_workers", "insert"),
        ("run_workers", "update"),
        ("runs", "insert"),
        ("runs", "update"),
        ("scheduler_events", "insert"),
        ("secret_resolutions", "insert"),
        ("sidecar_journal_outbox", "insert"),
        ("sink_effect_attempts", "insert"),
        ("sink_effect_attempts", "update"),
        ("sink_effect_members", "update"),
        ("sink_effect_streams", "update"),
        ("sink_effects", "update"),
        ("token_outcomes", "insert"),
        ("token_parents", "insert"),
        ("token_work_items", "insert"),
        ("token_work_items", "update"),
        ("tokens", "insert"),
        ("transform_errors", "insert"),
        ("validation_errors", "insert"),
        ("validation_errors", "update"),
    }
)
_EXPECTED_CALL_COUNT = 241
_EXPECTED_PRODUCTION_CALLER_SHA256 = "c7fe84414c114833d87406c1b76122f4664901c878cc9a87bc8c9788beb77c7b"
_EXPECTED_SUBORDINATE_EDGE_COUNT = 70
_EXPECTED_SUBORDINATE_EDGE_SHA256 = "0b49ee25a1763bd528a195560b2348a14ac5f937cdfe0360f3305711bc2b54c2"
_EXPECTED_COORDINATION_CALL_COUNT = 15
_EXPECTED_COORDINATION_CALL_SHA256 = "e2c82952e49763ee53e8772dcaedd9afbeecd1448c7c93a99659e25037e8d511"
_EXPECTED_INTERNAL_EDGE_COUNT = 98
_EXPECTED_INTERNAL_EDGE_SHA256 = "f015f32f3a7e4ababa7bdc3af16309a402c4ca404955d189079033202fa65f3b"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _attach_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._landscape_parent = parent  # type: ignore[attr-defined]


def _symbol(node: ast.AST) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = getattr(current, "_landscape_parent", None)
    return ".".join(reversed(names)) or "<module>"


def _parse_source(path: str, source: str) -> SourceUnit:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise InventoryScanError(f"cannot parse production source {path}: {exc}") from exc
    tree._landscape_path = path  # type: ignore[attr-defined]
    _attach_parents(tree)
    return SourceUnit(path=path, source=source, tree=tree)


def _read_source(path: Path, *, anchor: Path) -> SourceUnit:
    relative = path.relative_to(anchor).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InventoryScanError(f"cannot decode production source {relative}: {exc}") from exc
    return _parse_source(relative, source)


@cache
def _production_units() -> tuple[SourceUnit, ...]:
    root = _repo_root()
    return tuple(_read_source(path, anchor=root) for path in sorted((root / "src" / "elspeth").rglob("*.py")))


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _lexical_scope(node: ast.AST) -> ast.AST:
    current = node
    while True:
        if isinstance(current, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return current
        parent = getattr(current, "_landscape_parent", None)
        if parent is None:
            return current
        current = parent


def _resolver_for_node(node: ast.AST) -> _Resolver:
    current = node
    while not isinstance(current, ast.Module):
        parent = getattr(current, "_landscape_parent", None)
        if parent is None:
            raise InventoryScanError("detached AST node has no module resolver")
        current = parent
    return _resolver_for_unit(SourceUnit(path=getattr(current, "_landscape_path", "<synthetic>"), source="", tree=current))


def _module_defines_top_level_name(node: ast.AST, name: str) -> bool:
    current = node
    while not isinstance(current, ast.Module):
        parent = getattr(current, "_landscape_parent", None)
        if parent is None:
            return False
        current = parent
    return any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == name for child in current.body)


class _Resolver:
    """Small lexical resolver for SQLAlchemy/table aliases and statements."""

    def __init__(self, unit: SourceUnit) -> None:
        self.unit = unit
        self.imports: dict[tuple[int, str], list[tuple[int, str]]] = {}
        self.wildcard_imports: dict[int, list[int]] = {}
        self.assignments: dict[tuple[int, str], list[tuple[int, ast.expr]]] = {}
        self.annotation_assignments: dict[tuple[int, str], list[ast.AnnAssign]] = {}
        self.local_names: dict[int, set[str]] = {}
        self.sessions_provenance_cache: dict[tuple[int, int], bool] = {}
        for scope in ast.walk(unit.tree):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                names = {
                    argument.arg
                    for argument in (
                        *scope.args.posonlyargs,
                        *scope.args.args,
                        *scope.args.kwonlyargs,
                    )
                }
                if scope.args.vararg is not None:
                    names.add(scope.args.vararg.arg)
                if scope.args.kwarg is not None:
                    names.add(scope.args.kwarg.arg)
                self.local_names[id(scope)] = names
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    scope = _lexical_scope(node)
                    qualified = alias.name if alias.asname else local
                    self.imports.setdefault((id(scope), local), []).append((node.lineno, qualified))
                    if not isinstance(scope, ast.Module):
                        self.local_names.setdefault(id(scope), set()).add(local)
            elif isinstance(node, ast.ImportFrom):
                module = self._absolute_import_module(node)
                for alias in node.names:
                    if alias.name == "*":
                        scope = _lexical_scope(node)
                        self.wildcard_imports.setdefault(id(scope), []).append(node.lineno)
                        continue
                    local = alias.asname or alias.name
                    scope = _lexical_scope(node)
                    qualified = f"{module}.{alias.name}" if module else alias.name
                    self.imports.setdefault((id(scope), local), []).append((node.lineno, qualified))
                    if not isinstance(scope, ast.Module):
                        self.local_names.setdefault(id(scope), set()).add(local)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parent = getattr(node, "_landscape_parent", None)
                if parent is not None:
                    scope = _lexical_scope(parent)
                    if not isinstance(scope, ast.Module):
                        self.local_names.setdefault(id(scope), set()).add(node.name)
            elif isinstance(node, ast.AnnAssign):
                scope = _lexical_scope(node)
                if isinstance(node.target, ast.Name):
                    key = (id(scope), node.target.id)
                    self.annotation_assignments.setdefault(key, []).append(node)
                value = node.value
                if value is None:
                    continue
                for name in self._target_names(node.target):
                    if not isinstance(scope, ast.Module):
                        self.local_names.setdefault(id(scope), set()).add(name)
                if isinstance(node.target, ast.Name):
                    self.assignments.setdefault(key, []).append((node.lineno, value))
            elif isinstance(node, ast.Assign):
                value = node.value
                for target in node.targets:
                    for name in self._target_names(target):
                        scope = _lexical_scope(node)
                        if not isinstance(scope, ast.Module):
                            self.local_names.setdefault(id(scope), set()).add(name)
                        if isinstance(target, ast.Name):
                            key = (id(scope), name)
                            self.assignments.setdefault(key, []).append((node.lineno, value))
            elif isinstance(node, (ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)):
                target = node.target
                scope = _lexical_scope(node)
                if not isinstance(scope, ast.Module):
                    self.local_names.setdefault(id(scope), set()).update(self._target_names(target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                scope = _lexical_scope(node)
                if not isinstance(scope, ast.Module):
                    for item in node.items:
                        if item.optional_vars is not None:
                            self.local_names.setdefault(id(scope), set()).update(self._target_names(item.optional_vars))
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                scope = _lexical_scope(node)
                if not isinstance(scope, ast.Module):
                    self.local_names.setdefault(id(scope), set()).add(node.name)

    @staticmethod
    def _target_names(target: ast.AST) -> set[str]:
        return {child.id for child in ast.walk(target) if isinstance(child, ast.Name)}

    def _absolute_import_module(self, node: ast.ImportFrom) -> str:
        module = node.module or ""
        if node.level == 0 or not self.unit.path.startswith("src/") or not self.unit.path.endswith(".py"):
            return module
        current = self.unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".").split(".")
        package = current[:-1]
        keep = max(0, len(package) - (node.level - 1))
        return ".".join((*package[:keep], *(module.split(".") if module else ())))

    def _scoped_import(self, name: str, use: ast.AST) -> str | None:
        origin = _lexical_scope(use)
        scope: ast.AST | None = origin
        while scope is not None:
            candidates = self.imports.get((id(scope), name), ())
            eligible = (
                []
                if scope is not origin and isinstance(scope, ast.ClassDef)
                else list(candidates)
                if scope is not origin
                else [(line, value) for line, value in candidates if line < getattr(use, "lineno", 0)]
            )
            if eligible:
                return max(eligible, key=lambda item: item[0])[1]
            if isinstance(scope, ast.Module):
                return None
            parent = getattr(scope, "_landscape_parent", None)
            scope = _lexical_scope(parent) if parent is not None else None
        return None

    def _local_import(self, name: str, use: ast.AST) -> str | None:
        scope = _lexical_scope(use)
        if isinstance(scope, ast.Module):
            return None
        eligible = [(line, value) for line, value in self.imports.get((id(scope), name), ()) if line < getattr(use, "lineno", 0)]
        return None if not eligible else max(eligible, key=lambda item: item[0])[1]

    def _has_wildcard_import(self, use: ast.AST) -> bool:
        origin = _lexical_scope(use)
        scope: ast.AST | None = origin
        while scope is not None:
            lines = self.wildcard_imports.get(id(scope), ())
            if not (scope is not origin and isinstance(scope, ast.ClassDef)) and (
                (scope is not origin and lines) or any(line < getattr(use, "lineno", 0) for line in lines)
            ):
                return True
            if isinstance(scope, ast.Module):
                return False
            parent = getattr(scope, "_landscape_parent", None)
            scope = _lexical_scope(parent) if parent is not None else None
        return False

    def parameter(self, name: str, use: ast.AST) -> ast.arg | None:
        scope: ast.AST | None = _lexical_scope(use)
        while scope is not None:
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = (*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs)
                parameter = next((argument for argument in arguments if argument.arg == name), None)
                if parameter is not None:
                    return parameter
                if name in self.local_names.get(id(scope), set()):
                    return None
            if isinstance(scope, ast.Module):
                return None
            parent = getattr(scope, "_landscape_parent", None)
            scope = _lexical_scope(parent) if parent is not None else None
        return None

    def is_local(self, name: str, use: ast.AST) -> bool:
        scope = _lexical_scope(use)
        return not isinstance(scope, ast.Module) and name in self.local_names.get(id(scope), set())

    def binding(self, name: str, use: ast.AST) -> ast.expr | None:
        origin = _lexical_scope(use)
        scope: ast.AST | None = origin
        while scope is not None:
            candidates = self.assignments.get((id(scope), name), ())
            # Function bodies resolve globals when called, after module setup
            # has completed.  A binding below the function definition is
            # therefore authoritative (and can shadow an earlier import).
            bindings = (
                []
                if scope is not origin and isinstance(scope, ast.ClassDef)
                else list(candidates)
                if scope is not origin
                else [(line, value) for line, value in candidates if line < getattr(use, "lineno", 0)]
            )
            if bindings:
                return max(bindings, key=lambda item: item[0])[1]
            if isinstance(scope, ast.Module):
                return None
            parent = getattr(scope, "_landscape_parent", None)
            scope = _lexical_scope(parent) if parent is not None else None
        return None

    def qualified_name(self, node: ast.AST, *, use: ast.AST | None = None, seen: frozenset[str] = frozenset()) -> str | None:
        if isinstance(node, ast.Subscript):
            resolved = self.resolve_value(node, use=use or node, seen=seen)
            if resolved is not node:
                return self.qualified_name(resolved, use=use or node, seen=seen)
        if isinstance(node, ast.Call) and _call_name(node) == "getattr" and len(node.args) >= 2:
            attribute = node.args[1]
            attribute_name = _constant_string_value(attribute, self, use=node)
            if attribute_name is not None:
                prefix = self.qualified_name(node.args[0], use=use or node, seen=seen)
                return None if prefix is None else f"{prefix}.{attribute_name}"
        if isinstance(node, ast.Call) and _resolved_callable_name(node.func, self, use=node) == "import_module" and node.args:
            return _constant_string_value(node.args[0], self, use=node)
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            binding = self.binding(node.id, use or node)
            if binding is not None:
                return self.qualified_name(binding, use=binding, seen=seen | {node.id})
            if self.is_local(node.id, use or node):
                return self._local_import(node.id, use or node)
            imported = self._scoped_import(node.id, use or node)
            if imported is not None:
                return imported
            if self._has_wildcard_import(use or node):
                return None
            if (
                any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == node.id
                    for child in self.unit.tree.body
                )
                and self.unit.path.startswith("src/")
                and self.unit.path.endswith(".py")
            ):
                module = self.unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
                return f"{module}.{node.id}"
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = self.qualified_name(node.value, use=use or node, seen=seen)
            return None if prefix is None else f"{prefix}.{node.attr}"
        return None

    def resolve_value(self, node: ast.expr, *, use: ast.AST, seen: frozenset[str] = frozenset()) -> ast.expr:
        if isinstance(node, ast.Name) and node.id not in seen:
            binding = self.binding(node.id, use)
            if binding is not None:
                return self.resolve_value(binding, use=binding, seen=seen | {node.id})
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            container = self.resolve_value(node.value, use=node, seen=seen)
            if isinstance(container, (ast.List, ast.Tuple)) and isinstance(node.slice.value, int):
                index = node.slice.value
                if -len(container.elts) <= index < len(container.elts):
                    return self.resolve_value(container.elts[index], use=node, seen=seen)
            if isinstance(container, ast.Dict):
                for key, value in zip(container.keys, container.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == node.slice.value:
                        return self.resolve_value(value, use=node, seen=seen)
        return node

    def resolve_callable(self, node: ast.expr, *, use: ast.AST, seen: frozenset[str] = frozenset()) -> ast.expr:
        if isinstance(node, ast.Name) and node.id not in seen:
            binding = self.binding(node.id, use)
            if binding is not None:
                return self.resolve_callable(binding, use=binding, seen=seen | {node.id})
        if isinstance(node, ast.Subscript):
            resolved = self.resolve_value(node, use=use, seen=seen)
            if resolved is not node:
                return self.resolve_callable(resolved, use=node, seen=seen)
        if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Constant):
            selected = node.body if node.test.value else node.orelse
            return self.resolve_callable(selected, use=node, seen=seen)
        return node

    def resolve_statement(self, node: ast.expr, *, use: ast.AST, seen: frozenset[str] = frozenset()) -> ast.expr | None:
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            binding = self.binding(node.id, use)
            if binding is None:
                return None
            return self.resolve_statement(binding, use=binding, seen=seen | {node.id})
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # SQLAlchemy statement chaining: update(...).where(...).values(...)
            return self.resolve_statement(node.func.value, use=node, seen=seen) or node
        return node


@cache
def _resolver_for_unit(unit: SourceUnit) -> _Resolver:
    return _Resolver(unit)


def _table_name(node: ast.AST, resolver: _Resolver, *, use: ast.AST) -> str | None:
    dotted = resolver.qualified_name(node, use=use)
    if dotted is None:
        return None
    terminal = dotted.rsplit(".", maxsplit=1)[-1]
    if not terminal.endswith("_table"):
        return None
    return terminal.removesuffix("_table")


def _dml_shape(call: ast.Call, resolver: _Resolver) -> tuple[str, str] | None:
    """Return a SQLAlchemy DML construction's exact table and operation."""

    callable_node = resolver.resolve_callable(call.func, use=call)
    if isinstance(callable_node, ast.Call) and _call_name(callable_node) == "getattr" and len(callable_node.args) >= 2:
        operation_node = callable_node.args[1]
        if isinstance(operation_node, ast.Constant) and operation_node.value in {"insert", "update", "delete"}:
            table = _table_name(callable_node.args[0], resolver, use=callable_node)
            return None if table is None else (table, str(operation_node.value))

    if isinstance(callable_node, ast.Attribute) and callable_node.attr in {"insert", "update", "delete"}:
        table = _table_name(callable_node.value, resolver, use=callable_node)
        if table is not None:
            return table, callable_node.attr

    qualified = resolver.qualified_name(callable_node, use=call)
    name = None if qualified is None else qualified.rsplit(".", maxsplit=1)[-1]
    if name is None or not call.args:
        return None
    operation: str | None = None
    if name == "update":
        operation = "update"
    elif name == "delete":
        operation = "delete"
    elif name == "insert" or name.endswith("_insert"):
        operation = "insert"
    if operation is None:
        return None
    table = _table_name(call.args[0], resolver, use=call)
    return None if table is None else (table, operation)


_RAW_DML_RE = re.compile(
    r"\b(?P<operation>insert\s+into|update|delete\s+from)\s+[\"`\[]?(?P<table>[a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)
_RAW_WRITE_RE = re.compile(
    r"\b(?:insert\s+into|update|delete\s+from|replace\s+into|drop\s+table|alter\s+table|"
    r"create\s+(?:(?:temp|temporary)\s+)?table|create\s+(?:unique\s+)?index|drop\s+index|truncate\s+table)\b",
    re.IGNORECASE,
)

_RAW_READ_PRAGMAS = frozenset(
    {
        "compile_options",
        "database_list",
        "foreign_key_list",
        "index_info",
        "index_list",
        "table_info",
        "table_xinfo",
    }
)


def _constant_string_value(
    node: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id not in seen:
        binding = resolver.binding(node.id, use)
        return None if binding is None else _constant_string_value(binding, resolver, use=binding, seen=seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string_value(node.left, resolver, use=node, seen=seen)
        right = _constant_string_value(node.right, resolver, use=node, seen=seen)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        values = [part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)]
        return "".join(values) if len(values) == len(node.values) else None
    return None


def _resolved_callable_name(node: ast.expr, resolver: _Resolver, *, use: ast.AST) -> str | None:
    resolved = resolver.resolve_callable(node, use=use)
    if isinstance(resolved, ast.Call) and _call_name(resolved) == "getattr" and len(resolved.args) >= 2:
        name = _constant_string_value(resolved.args[1], resolver, use=resolved)
        if name is not None:
            return name
    qualified = resolver.qualified_name(resolved, use=use)
    if qualified is not None:
        return qualified.rsplit(".", maxsplit=1)[-1]
    if isinstance(resolved, ast.Attribute):
        return resolved.attr
    return resolved.id if isinstance(resolved, ast.Name) else None


def _resolved_execution_receiver(call: ast.Call, resolver: _Resolver) -> ast.expr | None:
    resolved = resolver.resolve_callable(call.func, use=call)
    if isinstance(resolved, ast.Attribute):
        return resolved.value
    if isinstance(resolved, ast.Call) and _call_name(resolved) == "getattr" and resolved.args:
        return resolved.args[0]
    return None


def _raw_sql_is_proven_read(value: str) -> bool:
    normalized = _normalized_raw_sql(value).rstrip(";").strip()
    lowered = normalized.lower()
    if lowered.startswith("select"):
        return True
    if lowered.startswith("explain"):
        remainder = re.sub(r"^explain\s+(?:query\s+plan\s+)?(?:analyze\s+)?", "", lowered, count=1)
        return remainder.startswith("select") and _RAW_DML_RE.search(remainder) is None
    if lowered.startswith("pragma"):
        if "=" in lowered:
            return False
        match = re.match(r"pragma\s+(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)", lowered)
        return match is not None and match.group("name") in _RAW_READ_PRAGMAS
    return False


def _raw_sql_is_transaction_control(value: str) -> bool:
    return value.lstrip().lower().startswith(("begin", "commit", "rollback", "savepoint", "release"))


def _raw_sql_is_write(value: str) -> bool:
    normalized = _normalized_raw_sql(value)
    return _RAW_WRITE_RE.search(normalized) is not None or (normalized.lower().startswith("pragma") and "=" in normalized)


def _normalized_raw_sql(value: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    without_comments = re.sub(r"--[^\r\n]*", " ", without_comments)
    return re.sub(r"\s+", " ", without_comments).strip()


def _raw_sql_literal(call: ast.Call, resolver: _Resolver) -> str | None:
    name = _resolved_callable_name(call.func, resolver, use=call)
    if name not in {"text", "exec_driver_sql"} or not call.args:
        return None
    return _constant_string_value(call.args[0], resolver, use=call)


def _raw_dml_shape(call: ast.Call, resolver: _Resolver) -> tuple[str, str] | None:
    value = _raw_sql_literal(call, resolver)
    if value is None:
        return None
    match = _RAW_DML_RE.search(_normalized_raw_sql(value))
    if match is None:
        return None
    raw_operation = match.group("operation").lower()
    operation = "insert" if raw_operation.startswith("insert") else "delete" if raw_operation.startswith("delete") else "update"
    return match.group("table").lower(), f"raw-{operation}"


def _execution_callback(
    node: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
) -> tuple[str, ast.expr] | None:
    resolved = resolver.resolve_callable(node, use=use)
    if isinstance(resolved, ast.Attribute) and resolved.attr in _PAYLOAD_EFFECT_NAMES:
        return resolved.attr, resolved.value
    if isinstance(resolved, ast.Call) and _call_name(resolved) == "getattr" and len(resolved.args) >= 2:
        method = _constant_string_value(resolved.args[1], resolver, use=resolved)
        if method in _PAYLOAD_EFFECT_NAMES:
            return method, resolved.args[0]
    return None


def _indirect_execution_payloads(
    call: ast.Call,
    resolver: _Resolver,
) -> tuple[tuple[str, ast.expr, tuple[ast.expr, ...]], ...]:
    """Return callback-dispatched DB effects and their possible payloads."""

    if isinstance(call.func, ast.Call):
        builder = call.func
        builder_name = _resolved_callable_name(builder.func, resolver, use=builder)
        if builder_name == "partial" and builder.args:
            callback = _execution_callback(builder.args[0], resolver, use=builder)
            if callback is not None:
                return ((callback[0], callback[1], (*builder.args[1:], *call.args)),)
        if builder_name == "methodcaller" and builder.args and call.args:
            method = _constant_string_value(builder.args[0], resolver, use=builder)
            if method in _PAYLOAD_EFFECT_NAMES:
                return ((method, call.args[0], (*builder.args[1:], *call.args[1:])),)

    callbacks = [
        (index, callback)
        for index, argument in enumerate(call.args)
        if (callback := _execution_callback(argument, resolver, use=call)) is not None
    ]
    return tuple(
        (callback[0], callback[1], tuple(argument for index, argument in enumerate(call.args) if index != callback_index))
        for callback_index, callback in callbacks
    )


def _semantic_dml_boundary(node: ast.AST) -> ast.AST:
    """Keep SQL statement semantics stable when a required fence wraps it."""

    current = node
    while True:
        parent = getattr(current, "_landscape_parent", None)
        if isinstance(parent, ast.Attribute) and parent.value is current:
            current = parent
            continue
        if isinstance(parent, ast.Call) and parent.func is current:
            current = parent
            continue
        return current


def _fingerprint(node: ast.AST) -> str:
    normalized = ast.dump(_semantic_dml_boundary(node), annotate_fields=True, include_attributes=False)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _required_authority(path: str, symbol: str) -> str:
    if path == _CHECKPOINT_PATH:
        return "CheckpointMutationAuthority"
    if path.endswith("run_coordination_repository.py"):
        if any(symbol in {item.caller_symbol, item.callee_symbol} for item in _AUTHORITY_ESTABLISHMENTS):
            return "RunAuthorityEstablishment"
        return "RunCoordinationMutationAuthority"
    if path == _RUN_LIFECYCLE_PATH:
        return "RunLifecycleMutationAuthority"
    if "/data_flow/" in path or path == _DATA_FLOW_PATH:
        return "DataFlowMutationAuthority"
    if "/scheduler/" in path or path == _SCHEDULER_PATH:
        return "SchedulerMutationAuthority"
    if "/execution/" in path or path == _EXECUTION_PATH:
        return "ExecutionMutationAuthority"
    if path.endswith("auth_audit_repository.py"):
        return "AuthenticationAuditAuthority"
    if path.endswith("journal.py"):
        return "LandscapeJournalAuthority"
    if path.endswith("write_repository.py"):
        return "SynthesisedRunMutationAuthority"
    if path.endswith("reproducibility.py"):
        return "ReproducibilityMutationAuthority"
    return "UNCLASSIFIED_LANDSCAPE_DML"


def scan_dml_identities(units: Iterable[SourceUnit]) -> tuple[DmlIdentity, ...]:
    raw: list[DmlIdentity] = []
    for unit in units:
        if not (unit.path.startswith("src/elspeth/core/landscape/") or unit.path == _CHECKPOINT_PATH):
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            shape = _dml_shape(node, resolver) or _raw_dml_shape(node, resolver)
            if shape is None:
                continue
            table, operation = shape
            raw.append(
                DmlIdentity(
                    path=unit.path,
                    symbol=_symbol(node),
                    table=table,
                    operation=operation,
                    fingerprint=_fingerprint(node),
                    ordinal=0,
                    authority=_required_authority(unit.path, _symbol(node)),
                    line=node.lineno,
                )
            )

    counters: Counter[tuple[str, str, str, str, str]] = Counter()
    result: list[DmlIdentity] = []
    for site in sorted(raw, key=lambda item: (item.path, item.line, item.symbol, item.table, item.operation)):
        key = (site.path, site.symbol, site.table, site.operation, site.fingerprint)
        counters[key] += 1
        result.append(
            DmlIdentity(
                path=site.path,
                symbol=site.symbol,
                table=site.table,
                operation=site.operation,
                fingerprint=site.fingerprint,
                ordinal=counters[key],
                authority=site.authority,
                line=site.line,
            )
        )
    return tuple(result)


def _normalized_receiver(node: ast.AST) -> str:
    dotted = _dotted_name(node)
    if dotted is not None:
        return dotted
    return ast.dump(node, annotate_fields=True, include_attributes=False)


_LANDSCAPE_RECEIVER_MARKERS = frozenset(
    {
        "audit",
        "checkpoint_manager",
        "checkpoints",
        "context",
        "ctx",
        "data_flow",
        "effects",
        "execution",
        "factory",
        "manager",
        "processor",
        "recorder",
        "repositories",
        "run_lifecycle",
        "scheduler",
        "sink_effects",
        "snapshots",
        "token_manager",
    }
)
_TOKEN_BOUND_CAPABILITY_TYPES = frozenset({"LandscapeMutationCapability", "LandscapeMutations"})
_TOKEN_BOUND_CAPABILITY_BINDERS = frozenset({"bind_landscape_mutations", "bind_mutation_capability"})
_TRUSTED_CAPABILITY_MODULES = frozenset(
    {
        "elspeth.core.landscape.mutations",
        "elspeth.web.coordination.lifecycle",
    }
)
_TRUSTED_CAPABILITY_QUALIFIED = frozenset(
    f"{module}.{name}" for module in _TRUSTED_CAPABILITY_MODULES for name in _TOKEN_BOUND_CAPABILITY_TYPES | _TOKEN_BOUND_CAPABILITY_BINDERS
)


_CATEGORY_RECEIVER_MARKERS: dict[str, frozenset[str]] = {
    "run-lifecycle": frozenset({"lifecycle", "run_lifecycle"}),
    "data-flow": frozenset({"data_flow", "token_manager"}),
    "execution": frozenset({"audit", "execution", "landscape", "recorder"}),
    "scheduler": frozenset({"processor", "scheduler"}),
    "sink-effect": frozenset({"effects", "sink_effects"}),
    "checkpoint": frozenset({"checkpoint_manager", "checkpoints", "manager"}),
    "audit-export": frozenset({"audit_export_snapshot_repository", "audit_export_snapshots", "snapshots"}),
    "coordination": frozenset({"repo", "run_coordination"}),
}
_PLUGIN_CONTEXT_METHODS = frozenset(
    {
        "allocate_call_index",
        "record_call",
        "record_operation_call",
        "record_readiness_check",
        "record_routing_event",
        "record_routing_events",
        "record_transform_error",
        "record_validation_error",
        "update_node_output_contract",
    }
)


def _parameter_rebound(owner: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    for child in _walk_same_scope(owner):
        if isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                if (alias.asname or alias.name.rsplit(".", maxsplit=1)[-1]) == name:
                    return True
        if (
            isinstance(child, ast.Call)
            and _call_name(child) in {"setattr", "__setattr__"}
            and child.args
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id == name
        ):
            return True
        if isinstance(child, ast.Subscript) and isinstance(child.ctx, (ast.Store, ast.Del)):
            root: ast.expr = child.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name) and root.id == name:
                return True
    return _subject_rebound(owner, name)


def _subject_rebound(owner: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    parameters = {
        argument.arg
        for argument in (
            *owner.args.posonlyargs,
            *owner.args.args,
            *owner.args.kwonlyargs,
        )
    }
    direct_writes = [
        child
        for child in _walk_same_scope(owner)
        if isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, (ast.Store, ast.Del))
    ]
    if (name in parameters and direct_writes) or (name not in parameters and len(direct_writes) > 1):
        return True
    resolver = _resolver_for_node(owner)

    def aliases_subject(expression: ast.expr, *, use: ast.AST, seen: frozenset[str] = frozenset()) -> bool:
        if not isinstance(expression, ast.Name):
            return False
        if expression.id == name:
            return True
        if expression.id in seen:
            return False
        binding = resolver.binding(expression.id, use)
        return binding is not None and aliases_subject(binding, use=binding, seen=seen | {expression.id})

    for child in _walk_same_scope(owner):
        if isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
            root: ast.expr = child
            while isinstance(root, ast.Attribute):
                root = root.value
            if aliases_subject(root, use=child):
                return True
        if isinstance(child, ast.Subscript) and isinstance(child.ctx, (ast.Store, ast.Del)):
            root = child.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if aliases_subject(root, use=child):
                return True
        if isinstance(child, ast.Call) and _call_name(child) in {"setattr", "__setattr__"} and child.args:
            target = child.args[0]
            if aliases_subject(target, use=child):
                return True
    return False


def _exact_annotated_receiver(node: ast.AST, method: str, resolver: _Resolver, *, use: ast.AST) -> bool:
    if not isinstance(node, ast.Name):
        return False
    parameter = resolver.parameter(node.id, use)
    if parameter is None:
        return False
    annotation = resolver.qualified_name(parameter.annotation, use=use) if parameter.annotation is not None else None
    expected = {
        f"{api.path.removeprefix('src/').removesuffix('.py').replace('/', '.')}.{api.owner}"
        for api in _MUTATION_APIS
        if api.method == method
    }
    if method in _COORDINATION_MUTATION_METHOD_NAMES:
        expected.add("elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository")
    return annotation in expected


def _owner_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    scope: ast.AST | None = _lexical_scope(node)
    while isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        evaluated_outside = [*scope.decorator_list, *scope.args.defaults, *(item for item in scope.args.kw_defaults if item is not None)]
        if scope.returns is not None:
            evaluated_outside.append(scope.returns)
        if not any(_is_descendant(node, expression) for expression in evaluated_outside):
            return scope
        parent = getattr(scope, "_landscape_parent", None)
        scope = _lexical_scope(parent) if parent is not None else None
    return None


def _trusted_repository_construction(
    node: ast.AST,
    method: str,
    *,
    resolver: _Resolver,
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    qualified = resolver.qualified_name(node.func, use=node)
    expected = {
        f"{api.path.removeprefix('src/').removesuffix('.py').replace('/', '.')}.{api.owner}"
        for api in _MUTATION_APIS
        if api.method == method
    }
    if method in _COORDINATION_MUTATION_METHOD_NAMES:
        expected.add("elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository")
    return qualified in expected


def _trusted_qualified_name_is_mutated(qualified: str, *, resolver: _Resolver, use: ast.AST) -> bool:
    module_name, attribute_name = qualified.rsplit(".", maxsplit=1)
    use_scope = _lexical_scope(use)
    scope: ast.AST | None = use_scope
    while scope is not None:
        if not (scope is not use_scope and isinstance(scope, ast.ClassDef)):
            if resolver.assignments.get((id(scope), attribute_name)):
                return True
            imports = resolver.imports.get((id(scope), attribute_name), ())
            if any(imported != qualified for _line, imported in imports):
                return True
        if isinstance(scope, ast.Module):
            break
        parent = getattr(scope, "_landscape_parent", None)
        scope = _lexical_scope(parent) if parent is not None else None
    for candidate in ast.walk(resolver.unit.tree):
        if not isinstance(candidate, ast.Call) or _call_name(candidate) not in {"setattr", "__setattr__"}:
            continue
        if len(candidate.args) < 2 or _constant_string_value(candidate.args[1], resolver, use=candidate) != attribute_name:
            continue
        if resolver.qualified_name(candidate.args[0], use=candidate) != module_name:
            continue
        candidate_scope = _lexical_scope(candidate)
        if candidate_scope is use_scope and candidate.lineno < getattr(use, "lineno", 0):
            return True
        if isinstance(candidate_scope, ast.Module):
            return True
        if isinstance(candidate_scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_descendant(use, candidate_scope):
            return True
    return False


def _exact_capability_construction(
    node: ast.AST,
    *,
    resolver: _Resolver,
    use: ast.AST,
) -> ast.Call | None:
    if isinstance(node, ast.Name):
        binding = resolver.binding(node.id, use)
        if binding is not None:
            return _exact_capability_construction(binding, resolver=resolver, use=use)
    if not isinstance(node, ast.Call):
        return None
    qualified = resolver.qualified_name(node.func, use=node)
    if qualified not in _TRUSTED_CAPABILITY_QUALIFIED or _trusted_qualified_name_is_mutated(qualified, resolver=resolver, use=use):
        return None
    terminal = qualified.rsplit(".", maxsplit=1)[-1]
    return node if terminal in _TOKEN_BOUND_CAPABILITY_TYPES | _TOKEN_BOUND_CAPABILITY_BINDERS else None


def _proven_token_bound_capability_token(
    node: ast.AST,
    *,
    resolver: _Resolver,
    use: ast.AST,
) -> ast.expr | None:
    construction = _exact_capability_construction(node, resolver=resolver, use=use)
    if construction is None or any(keyword.arg is None for keyword in construction.keywords):
        return None
    token_keywords = [keyword.value for keyword in construction.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
    if len(token_keywords) != 1:
        return None
    owner = _owner_function(use)
    if owner is None or not _token_expression_is_explicit(token_keywords[0], owner, resolver=resolver, use=use):
        return None
    return token_keywords[0]


def _looks_like_landscape_receiver(
    node: ast.AST,
    method: str,
    *,
    resolver: _Resolver | None = None,
    use: ast.AST | None = None,
) -> bool:
    if resolver is not None and isinstance(node, ast.Name):
        binding = resolver.binding(node.id, use or node)
        if binding is not None:
            return _looks_like_landscape_receiver(binding, method, resolver=resolver, use=binding)
    if isinstance(node, ast.BoolOp):
        return any(_looks_like_landscape_receiver(value, method, resolver=resolver, use=value) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _looks_like_landscape_receiver(node.body, method, resolver=resolver, use=node.body) or _looks_like_landscape_receiver(
            node.orelse, method, resolver=resolver, use=node.orelse
        )
    if isinstance(node, ast.Call):
        if resolver is not None and _exact_capability_construction(node, resolver=resolver, use=use or node) is not None:
            return True
        if resolver is not None and _trusted_repository_construction(node, method, resolver=resolver):
            return True
        return _looks_like_landscape_receiver(node.func, method, resolver=resolver, use=node)
    if resolver is not None and _exact_annotated_receiver(node, method, resolver, use=use or node):
        return True
    dotted = _dotted_name(node)
    if dotted is None:
        return False
    segments = {re.sub(r"(?<!^)(?=[A-Z])", "_", segment.removeprefix("_")).lower() for segment in dotted.split(".")}
    if {"session_service", "trail"} & segments:
        return False
    categories = {api.category for api in _MUTATION_APIS if api.method == method}
    if method in _COORDINATION_MUTATION_METHOD_NAMES:
        categories.add("coordination")
    expected_markers = frozenset().union(*(_CATEGORY_RECEIVER_MARKERS[category] for category in categories))
    if expected_markers & segments:
        return True
    if "landscape" in segments:
        return True
    if (
        segments == {"self"}
        and resolver is not None
        and resolver.unit.path == "src/elspeth/engine/processor.py"
        and method == "mark_blocked_barrier_terminal"
    ):
        return True
    if segments == {"self"} and resolver is not None and use is not None:
        owner = _symbol(use).rsplit(".", maxsplit=1)[0]
        if any(api.path == resolver.unit.path and api.owner == owner and api.method == method for api in _MUTATION_APIS):
            return True
    return method in _PLUGIN_CONTEXT_METHODS and bool({"context", "ctx"} & segments)


def scan_production_calls(units: Iterable[SourceUnit]) -> tuple[CallIdentity, ...]:
    raw: list[CallIdentity] = []
    for unit in units:
        # Calls inside the implementation establish the facade/helper graph;
        # they are not production consumers and are inventoried separately.
        if unit.path.startswith("src/elspeth/core/landscape/") or unit.path == _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _MUTATION_METHOD_NAMES:
                continue
            if not _looks_like_landscape_receiver(node.func.value, node.func.attr, resolver=resolver, use=node):
                continue
            raw.append(
                CallIdentity(
                    path=unit.path,
                    symbol=_symbol(node),
                    method=node.func.attr,
                    receiver=_normalized_receiver(node.func.value),
                    ordinal=0,
                    line=node.lineno,
                )
            )

    counters: Counter[tuple[str, str, str, str]] = Counter()
    result: list[CallIdentity] = []
    for site in sorted(raw, key=lambda item: (item.path, item.line, item.symbol, item.method, item.receiver)):
        key = (site.path, site.symbol, site.method, site.receiver)
        counters[key] += 1
        result.append(
            CallIdentity(
                path=site.path,
                symbol=site.symbol,
                method=site.method,
                receiver=site.receiver,
                ordinal=counters[key],
                line=site.line,
            )
        )
    return tuple(result)


def _ordinalize_calls(raw: Iterable[CallIdentity]) -> tuple[CallIdentity, ...]:
    counters: Counter[tuple[str, str, str, str]] = Counter()
    result: list[CallIdentity] = []
    for site in sorted(raw, key=lambda item: (item.path, item.line, item.symbol, item.method, item.receiver)):
        key = (site.path, site.symbol, site.method, site.receiver)
        counters[key] += 1
        result.append(
            CallIdentity(
                path=site.path,
                symbol=site.symbol,
                method=site.method,
                receiver=site.receiver,
                ordinal=counters[key],
                line=site.line,
            )
        )
    return tuple(result)


def scan_coordination_production_calls(units: Iterable[SourceUnit]) -> tuple[CallIdentity, ...]:
    raw: list[CallIdentity] = []
    for unit in units:
        if unit.path.startswith("src/elspeth/core/landscape/"):
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _COORDINATION_MUTATION_METHOD_NAMES
                and _looks_like_landscape_receiver(node.func.value, node.func.attr, resolver=resolver, use=node)
            ):
                raw.append(
                    CallIdentity(
                        unit.path,
                        _symbol(node),
                        node.func.attr,
                        _normalized_receiver(node.func.value),
                        0,
                        node.lineno,
                    )
                )
    return _ordinalize_calls(raw)


def scan_internal_landscape_wrapper_edges(units: Iterable[SourceUnit]) -> tuple[CallIdentity, ...]:
    raw: list[CallIdentity] = []
    for unit in units:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        for node in ast.walk(unit.tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr not in _ALL_MUTATION_METHOD_NAMES
            ):
                continue
            caller = _symbol(node)
            if (
                unit.path == "src/elspeth/core/landscape/run_coordination_repository.py"
                and caller == "RunCoordinationRepository.register_run_leader"
                and node.func.attr == "register_run_leader_on"
            ):
                # Task 8B requires removing this temporary wrapper. Excluding
                # its edge makes that removal fillable without rebasing the
                # permanent internal inventory.
                continue
            if (
                node.func.attr == caller.rsplit(".", maxsplit=1)[-1]
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                continue
            raw.append(
                CallIdentity(
                    unit.path,
                    caller,
                    node.func.attr,
                    _normalized_receiver(node.func.value),
                    0,
                    node.lineno,
                )
            )
    return _ordinalize_calls(raw)


def _token_expression_is_explicit(
    node: ast.expr,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    resolver: _Resolver,
    use: ast.AST,
) -> bool:
    if not isinstance(node, ast.Name):
        return False
    arguments = (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)
    parameter = next((argument for argument in arguments if argument.arg == node.id), None)
    return (
        parameter is not None
        and _is_exact_coordination_token_annotation(parameter.annotation, resolver=resolver, use=use)
        and _argument_default(owner, parameter.arg) is None
        and resolver.binding(node.id, use) is None
        and not _parameter_rebound(owner, node.id)
    )


def _api_run_id_position(api: MutationApi, definitions: dict[tuple[str, str], list[ast.FunctionDef | ast.AsyncFunctionDef]]) -> int | None:
    nodes = definitions[(api.path, api.symbol)]
    if len(nodes) != 1:
        return None
    positional = [argument.arg for argument in (*nodes[0].args.posonlyargs, *nodes[0].args.args) if argument.arg != "self"]
    return positional.index("run_id") if "run_id" in positional else None


def _run_id_argument(
    call: ast.Call,
    candidates: Sequence[MutationApi],
    definitions: dict[tuple[str, str], list[ast.FunctionDef | ast.AsyncFunctionDef]],
) -> ast.expr | None:
    keywords = [keyword.value for keyword in call.keywords if keyword.arg == "run_id"]
    if len(keywords) == 1:
        return keywords[0]
    positions = {_api_run_id_position(api, definitions) for api in candidates}
    positions.discard(None)
    if len(positions) != 1:
        return None
    position = next(iter(positions))
    return call.args[position] if position < len(call.args) else None


def _is_exact_token_run_id(run_id: ast.expr, token: ast.expr) -> bool:
    return (
        isinstance(run_id, ast.Attribute)
        and run_id.attr == "run_id"
        and ast.dump(run_id.value, include_attributes=False) == ast.dump(token, include_attributes=False)
    )


def _caller_authority_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    definitions = _find_api_definitions(unit_list)
    violations: list[str] = []
    for unit in unit_list:
        if unit.path.startswith("src/elspeth/core/landscape/") or unit.path == _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for call in ast.walk(unit.tree):
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Attribute)
                or call.func.attr not in _MUTATION_METHOD_NAMES
                or not _looks_like_landscape_receiver(call.func.value, call.func.attr, resolver=resolver, use=call)
            ):
                continue
            identity = (unit.path, _symbol(call))
            if call.func.attr == "begin_run" and identity in _EXACT_BEGIN_RUN_PRODUCTION_CALLERS:
                continue
            candidates = [api for api in _MUTATION_APIS if api.method == call.func.attr]
            owner = _owner_function(call)
            if owner is None:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} call has no lexical owner")
                continue
            if any(keyword.arg is None for keyword in call.keywords):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} forwards authority through **kwargs")
                continue
            capability_token = _proven_token_bound_capability_token(call.func.value, resolver=resolver, use=call)
            if capability_token is not None:
                if any(keyword.arg in _AUTHORITY_PARAMETER_NAMES for keyword in call.keywords):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} overrides token-bound capability authority")
                    continue
                run_id = _run_id_argument(call, candidates, definitions)
                requires_run_id = any(_api_run_id_position(api, definitions) is not None for api in candidates)
                if requires_run_id and run_id is not None and not _is_exact_token_run_id(run_id, capability_token):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} run_id is not capability token.run_id")
                continue
            token_keywords = [keyword for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
            if len(token_keywords) != 1 or not _token_expression_is_explicit(
                token_keywords[0].value,
                owner,
                resolver=resolver,
                use=call,
            ):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} lacks one exact current token")
                continue
            run_id = _run_id_argument(call, candidates, definitions)
            requires_run_id = any(_api_run_id_position(api, definitions) is not None for api in candidates)
            if requires_run_id and (run_id is None or not _is_exact_token_run_id(run_id, token_keywords[0].value)):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} run_id is not exact token.run_id")
    return tuple(violations)


_EXACT_ESTABLISHMENT_CALLERS = {
    "acquire_run_leadership": (
        "src/elspeth/engine/orchestrator/resume.py",
        "ResumeCoordinator._acquire_resume_leadership",
    ),
    "admit_follower": (
        "src/elspeth/engine/orchestrator/join_admission.py",
        "JoinAdmissionService.join_run",
    ),
}


def _exact_keyword_arguments(call: ast.Call) -> dict[str, ast.expr] | None:
    if call.args or any(keyword.arg is None for keyword in call.keywords):
        return None
    result: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        assert keyword.arg is not None
        if keyword.arg in result:
            return None
        result[keyword.arg] = keyword.value
    return result


def _establishment_call_shape_violation(method: str, call: ast.Call) -> str | None:
    arguments = _exact_keyword_arguments(call)
    required = {
        "acquire_run_leadership": {"run_id", "worker_id", "window_seconds", "entry_point"},
        "admit_follower": {"run_id", "worker_id", "config_hash", "window_seconds"},
    }[method]
    if arguments is None or not required <= arguments.keys() or set(arguments) - required - {"now"}:
        return f"{method} must use one explicit complete keyword-bound authority subject"
    run_id = arguments["run_id"]
    worker_id = arguments["worker_id"]
    if method == "acquire_run_leadership":
        if not (
            _dotted_name(run_id) == "snapshot.run_id"
            and _dotted_name(worker_id) == "snapshot.worker_id"
            and isinstance(arguments["window_seconds"], ast.Name)
            and arguments["window_seconds"].id == "window_seconds"
            and isinstance(arguments["entry_point"], ast.Constant)
            and arguments["entry_point"].value == "resume"
        ):
            return "acquire_run_leadership CAS subject is not one exact resume snapshot"
    elif not (
        isinstance(run_id, ast.Name)
        and run_id.id == "run_id"
        and isinstance(worker_id, ast.Name)
        and worker_id.id == "worker_id"
        and isinstance(arguments["config_hash"], ast.Name)
        and arguments["config_hash"].id == "config_hash"
        and isinstance(arguments["window_seconds"], ast.Name)
        and arguments["window_seconds"].id == "window_seconds"
    ):
        return "admit_follower membership subject is not the exact admitted run and worker"
    return None


def _exact_token_subject(node: ast.expr, token: ast.expr, field: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == field
        and ast.dump(node.value, include_attributes=False) == ast.dump(token, include_attributes=False)
    )


def _coordination_subject_violation(
    call: ast.Call,
    token: ast.expr,
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> str | None:
    definition = definitions.get(call.func.attr) if isinstance(call.func, ast.Attribute) else None
    positional = (
        []
        if definition is None
        else [argument.arg for argument in (*definition.args.posonlyargs, *definition.args.args) if argument.arg != "self"]
    )
    for field in ("run_id", "worker_id"):
        values = [keyword.value for keyword in call.keywords if keyword.arg == field]
        if field in positional and positional.index(field) < len(call.args):
            values.append(call.args[positional.index(field)])
        required = definition is not None and any(
            argument.arg == field for argument in (*definition.args.posonlyargs, *definition.args.args, *definition.args.kwonlyargs)
        )
        if required and len(values) != 1:
            return f".{call.func.attr} {field} is not explicitly bound to token.{field}"
        if values and (len(values) != 1 or not _exact_token_subject(values[0], token, field)):
            return f".{call.func.attr} {field} is not exact token.{field}"
    return None


def _coordination_caller_authority_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    violations: list[str] = []
    establishment_calls: dict[str, list[tuple[SourceUnit, ast.Call]]] = {method: [] for method in _EXACT_ESTABLISHMENT_CALLERS}
    coordination_definitions = {
        node.name: node
        for unit in unit_list
        if unit.path == "src/elspeth/core/landscape/run_coordination_repository.py"
        for node in ast.walk(unit.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _COORDINATION_MUTATION_METHOD_NAMES
    }
    for unit in unit_list:
        if unit.path.startswith("src/elspeth/core/landscape/"):
            continue
        resolver = _resolver_for_unit(unit)
        for call in ast.walk(unit.tree):
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Attribute)
                or call.func.attr not in _COORDINATION_MUTATION_METHOD_NAMES
                or not _looks_like_landscape_receiver(call.func.value, call.func.attr, resolver=resolver, use=call)
            ):
                continue
            identity = (unit.path, _symbol(call))
            expected_establishment = _EXACT_ESTABLISHMENT_CALLERS.get(call.func.attr)
            if expected_establishment is not None:
                establishment_calls[call.func.attr].append((unit, call))
                if identity != expected_establishment:
                    violations.append(
                        f"{unit.path}:{call.lineno} {_symbol(call)} unexpected {call.func.attr} authority-establishment caller"
                    )
                shape_violation = _establishment_call_shape_violation(call.func.attr, call)
                if shape_violation is not None:
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} {shape_violation}")
                owner = _owner_function(call)
                subject_names = (
                    ("snapshot", "window_seconds")
                    if call.func.attr == "acquire_run_leadership"
                    else ("run_id", "worker_id", "config_hash", "window_seconds")
                )
                if owner is None or any(_subject_rebound(owner, name) for name in subject_names):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} authority-establishment subject is rebound")
                if owner is not None and _has_repeating_ancestor(call, stop=owner):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} authority-establishment call is runtime-repeating")
                if owner is not None and _is_statically_dead(call, stop=owner):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} authority-establishment call is statically unreachable")
                continue
            owner = _owner_function(call)
            if owner is None:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} coordination call has no owner")
                continue
            if any(keyword.arg is None for keyword in call.keywords):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} forwards authority through **kwargs")
                continue
            capability_token = _proven_token_bound_capability_token(call.func.value, resolver=resolver, use=call)
            if capability_token is not None and not any(keyword.arg in _AUTHORITY_PARAMETER_NAMES for keyword in call.keywords):
                subject_violation = _coordination_subject_violation(call, capability_token, coordination_definitions)
                if subject_violation is not None:
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} {subject_violation}")
                continue
            token_keywords = [keyword for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
            if len(token_keywords) != 1 or not _token_expression_is_explicit(
                token_keywords[0].value,
                owner,
                resolver=resolver,
                use=call,
            ):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} lacks one exact current authority")
                continue
            subject_violation = _coordination_subject_violation(call, token_keywords[0].value, coordination_definitions)
            if subject_violation is not None:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} {subject_violation}")
    for method, calls in establishment_calls.items():
        if len(calls) != 1:
            violations.append(f"{method} approved authority-establishment calls={len(calls)} expected=1")
    return tuple(violations)


def _internal_coordination_authority_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    violations: list[str] = []
    coordination_definitions = {
        node.name: node
        for unit in unit_list
        if unit.path == "src/elspeth/core/landscape/run_coordination_repository.py"
        for node in ast.walk(unit.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _COORDINATION_MUTATION_METHOD_NAMES
    }
    for unit in unit_list:
        if not unit.path.startswith("src/elspeth/core/landscape/"):
            continue
        resolver = _resolver_for_unit(unit)
        for call in ast.walk(unit.tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in _COORDINATION_MUTATION_METHOD_NAMES:
                continue
            identity = (unit.path, _symbol(call), call.func.attr)
            if identity == (
                _FRESH_EPOCH_ONE_EXCEPTION.caller_path,
                _FRESH_EPOCH_ONE_EXCEPTION.caller_symbol,
                "register_run_leader_on",
            ):
                continue
            owner = _owner_function(call)
            token_keywords = [keyword.value for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
            if (
                owner is None
                or len(token_keywords) != 1
                or not _token_expression_is_explicit(token_keywords[0], owner, resolver=resolver, use=call)
            ):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} internal .{call.func.attr} lacks exact current token")
                continue
            subject_violation = _coordination_subject_violation(call, token_keywords[0], coordination_definitions)
            if subject_violation is not None:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} internal {subject_violation}")
    return tuple(violations)


def _scan_exact_attribute_calls(
    units: Iterable[SourceUnit],
    names: frozenset[str],
) -> tuple[CallIdentity, ...]:
    raw: list[CallIdentity] = []
    for unit in units:
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr not in names:
                continue
            raw.append(
                CallIdentity(
                    path=unit.path,
                    symbol=_symbol(node),
                    method=node.func.attr,
                    receiver=_normalized_receiver(node.func.value),
                    ordinal=1,
                    line=node.lineno,
                )
            )
    return tuple(sorted(raw, key=lambda item: (item.path, item.line, item.symbol, item.method)))


def _canonical_digest(items: Iterable[object]) -> str:
    lines: list[str] = []
    for item in items:
        if isinstance(item, DmlIdentity):
            fields = (
                item.path,
                item.symbol,
                item.table,
                item.operation,
                item.fingerprint,
                str(item.ordinal),
                item.authority,
            )
        elif isinstance(item, CallIdentity):
            fields = (item.path, item.symbol, item.method, item.receiver, str(item.ordinal))
        elif isinstance(item, SubordinateHelperEdge):
            fields = (
                item.helper_path,
                item.helper_symbol,
                item.caller_path,
                item.caller_symbol,
                item.call_fingerprint,
                str(item.ordinal),
            )
        else:
            raise TypeError(f"unsupported inventory item: {type(item).__name__}")
        lines.append("\x1f".join(fields))
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_dotted_name(decorator) in {"overload", "typing.overload"} for decorator in node.decorator_list)


def _find_api_definitions(
    units: Iterable[SourceUnit],
) -> dict[tuple[str, str], list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    expected = {(api.path, api.symbol) for api in _MUTATION_APIS}
    found: dict[tuple[str, str], list[ast.FunctionDef | ast.AsyncFunctionDef]] = {key: [] for key in expected}
    for unit in units:
        for node in ast.walk(unit.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or _is_overload(node):
                continue
            key = (unit.path, _symbol(node))
            if key in found:
                found[key].append(node)
    return found


def _argument_default(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> ast.expr | None:
    positional = (*node.args.posonlyargs, *node.args.args)
    positional_defaults = (None,) * (len(positional) - len(node.args.defaults)) + tuple(node.args.defaults)
    for argument, default in zip(positional, positional_defaults, strict=True):
        if argument.arg == name:
            return default
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
        if argument.arg == name:
            return default
    return None


def _authority_parameter(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.arg | None:
    arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    return next((argument for argument in arguments if argument.arg in _AUTHORITY_PARAMETER_NAMES), None)


def _annotation_names(annotation: ast.expr | None) -> frozenset[str]:
    if annotation is None:
        return frozenset()
    return frozenset(name for child in ast.walk(annotation) if (name := _dotted_name(child)) is not None)


def _is_exact_coordination_token_annotation(
    annotation: ast.expr | None,
    *,
    resolver: _Resolver | None = None,
    use: ast.AST | None = None,
) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return False
    dotted = resolver.qualified_name(annotation, use=use or annotation) if resolver is not None else _dotted_name(annotation)
    return dotted == "elspeth.contracts.coordination.CoordinationToken"


def _api_authority_violations(
    units: Iterable[SourceUnit],
) -> tuple[str, ...]:
    unit_list = tuple(units)
    definitions = _find_api_definitions(unit_list)
    resolvers = {unit.path: _resolver_for_unit(unit) for unit in unit_list}
    violations: list[str] = []
    for api in _MUTATION_APIS:
        nodes = definitions[(api.path, api.symbol)]
        if len(nodes) != 1:
            violations.append(f"{api.path}:{api.symbol} definitions={len(nodes)} expected=1")
            continue
        if api.symbol == _FRESH_EPOCH_ONE_EXCEPTION.caller_symbol:
            continue
        node = nodes[0]
        parameter = _authority_parameter(node)
        if parameter is None:
            violations.append(f"{api.path}:{api.symbol} has no explicit current Landscape token")
            continue
        if not _is_exact_coordination_token_annotation(
            parameter.annotation,
            resolver=resolvers.get(api.path),
            use=node,
        ):
            violations.append(f"{api.path}:{api.symbol} token annotation is not CoordinationToken")
        if _argument_default(node, parameter.arg) is not None:
            violations.append(f"{api.path}:{api.symbol} token is optional/defaulted")
        if _parameter_rebound(node, parameter.arg):
            violations.append(f"{api.path}:{api.symbol} token parameter is rebound")
    return tuple(violations)


def _looks_like_any_landscape_receiver(node: ast.AST, *, resolver: _Resolver, use: ast.AST) -> bool:
    representatives = (
        "complete_run",
        "create_row",
        "begin_node_state",
        "claim_ready",
        "reserve",
        "create_checkpoint",
        "register_candidate",
        "release_seat",
    )
    return any(_looks_like_landscape_receiver(node, method, resolver=resolver, use=use) for method in representatives)


def _looks_like_landscape_class(node: ast.AST, *, resolver: _Resolver, use: ast.AST) -> bool:
    qualified = resolver.qualified_name(node, use=use) or ""
    owners = {api.owner for api in _MUTATION_APIS} | {"RunCoordinationRepository"}
    return qualified.rsplit(".", maxsplit=1)[-1] in owners


def _mutation_callable_escapes(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    violations: list[str] = []
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Call) and node.args:
                operator_call = node.func
                operator_name = _resolved_callable_name(operator_call.func, resolver, use=operator_call)
                if operator_name in {"attrgetter", "methodcaller"} and operator_call.args:
                    name = _constant_string_value(operator_call.args[0], resolver, use=operator_call)
                    receiver = node.args[0]
                    if _looks_like_any_landscape_receiver(receiver, resolver=resolver, use=node) and (
                        name is None or name in _ALL_MUTATION_METHOD_NAMES
                    ):
                        violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} operator mutation attribute dispatch")
            if isinstance(node, ast.Call) and _resolved_callable_name(node.func, resolver, use=node) == "setattr" and len(node.args) >= 2:
                name = _constant_string_value(node.args[1], resolver, use=node)
                if _looks_like_any_landscape_receiver(node.args[0], resolver=resolver, use=node) and (
                    name is None or name in _ALL_MUTATION_METHOD_NAMES
                ):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} setattr mutation override {name!r}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__getattribute__"
                and node.args
                and _looks_like_any_landscape_receiver(node.func.value, resolver=resolver, use=node)
            ):
                name = node.args[0]
                if not isinstance(name, ast.Constant) or name.value in _ALL_MUTATION_METHOD_NAMES:
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} dynamic Landscape __getattribute__")
            if (
                isinstance(node, ast.Call)
                and _resolved_callable_name(node.func, resolver, use=node) == "__getattribute__"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "object"
                and len(node.args) >= 2
                and _looks_like_any_landscape_receiver(node.args[0], resolver=resolver, use=node)
            ):
                name = _constant_string_value(node.args[1], resolver, use=node)
                if name is None or name in _ALL_MUTATION_METHOD_NAMES:
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} dynamic object.__getattribute__")
            if isinstance(node, ast.Call) and _resolved_callable_name(node.func, resolver, use=node) == "getattr" and len(node.args) >= 2:
                name = node.args[1]
                dynamic_landscape_receiver = _looks_like_any_landscape_receiver(node.args[0], resolver=resolver, use=node)
                resolved_name = _constant_string_value(name, resolver, use=node)
                if dynamic_landscape_receiver and resolved_name in _ALL_MUTATION_METHOD_NAMES:
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} dynamic getattr({resolved_name!r})")
                elif dynamic_landscape_receiver and resolved_name is None:
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} non-literal Landscape getattr")
            if isinstance(node, ast.Subscript):
                container = node.value
                receiver: ast.AST | None = None
                if (
                    isinstance(container, ast.Call)
                    and _resolved_callable_name(container.func, resolver, use=container) == "vars"
                    and container.args
                ):
                    receiver = container.args[0]
                    if (
                        isinstance(receiver, ast.Call)
                        and _resolved_callable_name(receiver.func, resolver, use=receiver) == "type"
                        and receiver.args
                    ):
                        receiver = receiver.args[0]
                elif isinstance(container, ast.Attribute) and container.attr == "__dict__":
                    receiver = container.value
                name = _constant_string_value(node.slice, resolver, use=node)
                if (
                    receiver is not None
                    and (
                        _looks_like_any_landscape_receiver(receiver, resolver=resolver, use=node)
                        or _looks_like_landscape_class(receiver, resolver=resolver, use=node)
                    )
                    and (name is None or name in _ALL_MUTATION_METHOD_NAMES)
                ):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} mapping mutation attribute dispatch")
            if not isinstance(node, ast.Attribute) or node.attr not in _ALL_MUTATION_METHOD_NAMES:
                continue
            proven_receiver = _looks_like_landscape_receiver(node.value, node.attr, resolver=resolver, use=node)
            if not proven_receiver:
                if node.attr == "finalize" and _dotted_name(node.value) in {
                    "factory",
                    "context",
                    "text_writer",
                    "weakref",
                }:
                    continue
                if unit.path.startswith("src/elspeth/core/landscape/") or unit.path == _CHECKPOINT_PATH:
                    exact_fresh_creation_edge = (
                        unit.path == _FRESH_EPOCH_ONE_EXCEPTION.caller_path
                        and _symbol(node) == _FRESH_EPOCH_ONE_EXCEPTION.caller_symbol
                        and node.attr == "register_run_leader_on"
                    )
                    if node.attr not in _COORDINATION_MUTATION_METHOD_NAMES or exact_fresh_creation_edge:
                        continue
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} unknown mutation receiver .{node.attr}")
                continue
            if not proven_receiver:
                continue
            parent = getattr(node, "_landscape_parent", None)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} callable escape .{node.attr}")
    return tuple(violations)


def _dml_callable_escape_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    violations: list[str] = []
    for unit in units:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.asname is not None and (alias.name in {"insert", "update", "delete"} or alias.name.endswith("_insert")):
                        violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} aliased DML import {alias.name} as {alias.asname}")
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                value = node.value
                qualified = resolver.qualified_name(value, use=node)
                terminal = None if qualified is None else qualified.rsplit(".", maxsplit=1)[-1]
                bound_table_method = (
                    isinstance(value, ast.Attribute)
                    and value.attr in {"insert", "update", "delete"}
                    and _table_name(value.value, resolver, use=value) is not None
                )
                if (
                    terminal in {"insert", "update", "delete"}
                    or (terminal is not None and terminal.endswith("_insert"))
                    or bound_table_method
                ):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} DML callable alias/escape")
                if isinstance(value, ast.Attribute) and value.attr in _PAYLOAD_EFFECT_NAMES:
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} execution callable alias/escape")
            if isinstance(node, ast.Call) and _call_name(node) == "getattr" and len(node.args) >= 2:
                operation = node.args[1]
                if (
                    isinstance(operation, ast.Constant)
                    and operation.value in {"insert", "update", "delete"}
                    and _table_name(node.args[0], resolver, use=node) is not None
                ):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} dynamic DML getattr({operation.value!r})")
    return tuple(violations)


def _statement_contains_dml(
    statement: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[str] = frozenset(),
) -> bool:
    if isinstance(statement, ast.Name):
        if statement.id in seen:
            return True
        binding = resolver.binding(statement.id, use)
        return binding is not None and _statement_contains_dml(
            binding,
            resolver,
            use=binding,
            seen=seen | {statement.id},
        )
    if isinstance(statement, ast.Call) and (_dml_shape(statement, resolver) is not None or _raw_dml_shape(statement, resolver) is not None):
        return True
    return any(
        _statement_contains_dml(child, resolver, use=statement, seen=seen)
        for child in ast.iter_child_nodes(statement)
        if isinstance(child, ast.expr)
    )


def _expression_contains_dml(node: ast.expr, resolver: _Resolver, *, use: ast.AST) -> bool:
    return _statement_contains_dml(node, resolver, use=use)


def _indirect_dml_execution(call: ast.Call, resolver: _Resolver) -> tuple[bool, ast.expr | None]:
    resolved = resolver.resolve_callable(call.func, use=call)
    arguments_contain_dml = any(_expression_contains_dml(argument, resolver, use=call) for argument in call.args)
    if isinstance(resolved, ast.Attribute) and resolved.attr in _PAYLOAD_EFFECT_NAMES and arguments_contain_dml:
        return True, resolved.value
    if isinstance(call.func, ast.Call) and _call_name(call.func) == "getattr" and call.func.args and arguments_contain_dml:
        return True, call.func.args[0]
    if isinstance(resolved, ast.Lambda) and _expression_contains_dml(resolved.body, resolver, use=resolved):
        inner = next(
            (
                child
                for child in ast.walk(resolved.body)
                if isinstance(child, ast.Call)
                and _resolved_execution_receiver(child, resolver) is not None
                and _resolved_callable_name(child.func, resolver, use=child) in _PAYLOAD_EFFECT_NAMES
            ),
            None,
        )
        return True, None if inner is None else _resolved_execution_receiver(inner, resolver)
    if _resolved_callable_name(call.func, resolver, use=call) == "map" and len(call.args) >= 2:
        mapped = resolver.resolve_callable(call.args[0], use=call)
        if (
            isinstance(mapped, ast.Attribute)
            and mapped.attr in _PAYLOAD_EFFECT_NAMES
            and any(_expression_contains_dml(argument, resolver, use=call) for argument in call.args[1:])
        ):
            return True, mapped.value
    return False, None


def _is_indirect_execution_syntax(call: ast.Call, resolver: _Resolver) -> bool:
    resolved = resolver.resolve_callable(call.func, use=call)
    return (
        isinstance(call.func, (ast.Subscript, ast.Call))
        or isinstance(resolved, ast.Lambda)
        or _resolved_callable_name(call.func, resolver, use=call) == "map"
    )


def _is_proven_read(statement: ast.expr, resolver: _Resolver, *, use: ast.AST) -> bool:
    if _statement_contains_dml(statement, resolver, use=use):
        return False
    resolved = resolver.resolve_statement(statement, use=use)
    if not isinstance(resolved, ast.Call):
        return False
    name = _call_name(resolved)
    if name in {"select", "exists"}:
        return True
    if name == "text" and resolved.args:
        value = resolved.args[0]
        return isinstance(value, ast.Constant) and isinstance(value.value, str) and _raw_sql_is_proven_read(value.value)
    return False


def _unknown_or_raw_execution_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    violations: list[str] = []
    for unit in units:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            indirect_payloads = _indirect_execution_payloads(node, resolver)
            if indirect_payloads:
                for method, _receiver, payloads in indirect_payloads:
                    raw_values = [
                        value for payload in payloads if (value := _constant_string_value(payload, resolver, use=node)) is not None
                    ]
                    if any(_raw_sql_is_write(value) for value in raw_values):
                        violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} indirect raw SQL write/DDL")
                        break
                    if method == "exec_driver_sql" and any(
                        not _raw_sql_is_proven_read(value) and not _raw_sql_is_transaction_control(value) for value in raw_values
                    ):
                        violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} indirect unknown exec_driver_sql effect")
                        break
                    if any(_expression_contains_dml(payload, resolver, use=node) for payload in payloads):
                        violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} indirect/unclassified DML execution")
                        break
                else:
                    pass
                if any(
                    _constant_string_value(payload, resolver, use=node) is not None or _expression_contains_dml(payload, resolver, use=node)
                    for _method, _receiver, payloads in indirect_payloads
                    for payload in payloads
                ):
                    continue
            indirect_execution, _receiver = _indirect_dml_execution(node, resolver)
            if indirect_execution and _is_indirect_execution_syntax(node, resolver):
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} indirect/unclassified DML execution")
                continue
            raw_shape = _raw_dml_shape(node, resolver)
            if raw_shape is not None:
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} raw SQL {raw_shape[1]} {raw_shape[0]} is forbidden")
                continue
            name = _resolved_callable_name(node.func, resolver, use=node)
            if name not in {"execute", "execute_insert", "execute_update", "exec_driver_sql"} or not node.args:
                continue
            if name == "exec_driver_sql":
                value = node.args[0]
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and (_raw_sql_is_proven_read(value.value) or _raw_sql_is_transaction_control(value.value))
                ):
                    continue
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} unknown exec_driver_sql effect")
                continue
            resolved = resolver.resolve_statement(node.args[0], use=node)
            if isinstance(resolved, ast.Call) and _dml_shape(resolved, resolver) is not None:
                continue
            if _is_proven_read(node.args[0], resolver, use=node):
                continue
            violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} unclassified .{name} statement")
    return tuple(violations)


def _targets_landscape_schema(expression: ast.expr, resolver: _Resolver, *, use: ast.AST) -> bool:
    for child in ast.walk(expression):
        if isinstance(child, ast.Call) and _call_name(child) == "vars" and child.args:
            base = resolver.qualified_name(child.args[0], use=child) or ""
            if base == "elspeth.core.landscape.schema":
                return True
        if isinstance(child, ast.Call) and _call_name(child) == "getattr" and child.args:
            base = resolver.qualified_name(child.args[0], use=child) or ""
            if base == "elspeth.core.landscape.schema":
                return True
        if isinstance(child, ast.Attribute) and child.attr == "__dict__":
            base = resolver.qualified_name(child.value, use=child) or ""
            if base == "elspeth.core.landscape.schema":
                return True
        if isinstance(child, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
            qualified = resolver.qualified_name(child, use=use) or ""
            if qualified.startswith("elspeth.core.landscape.schema.") and qualified.rsplit(".", maxsplit=1)[-1].endswith("_table"):
                return True
    return False


def _raw_write_surface_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    violations: list[str] = []
    implementation_prefixes = (
        "src/elspeth/core/landscape/",
        _CHECKPOINT_PATH,
    )
    raw_names = {
        "execute_insert",
        "execute_update",
        "write_connection",
        "write_repositories",
    }
    landscape_tables = {table for table, _operation in _EXPECTED_DML_WRITE_SET}
    for unit in units:
        if unit.path.startswith(implementation_prefixes):
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.Call) and _resolved_callable_name(node.func, resolver, use=node) in raw_names:
                violations.append(
                    f"{unit.path}:{node.lineno} {_symbol(node)} raw .{_resolved_callable_name(node.func, resolver, use=node)}()"
                )
            if isinstance(node, ast.Call) and _call_name(node) in {"DatabaseOps", "LandscapeWriteRepositories"}:
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} raw {_call_name(node)} construction")
            if not isinstance(node, ast.Call):
                continue
            for method, _receiver, payloads in _indirect_execution_payloads(node, resolver):
                raw_values = [value for payload in payloads if (value := _constant_string_value(payload, resolver, use=node)) is not None]
                if any(_raw_sql_is_write(value) for value in raw_values):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside indirect raw SQL write/DDL")
                if method == "exec_driver_sql" and not raw_values:
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside unknown indirect raw SQL effect")
                if any(
                    _expression_contains_dml(payload, resolver, use=node) and _targets_landscape_schema(payload, resolver, use=node)
                    for payload in payloads
                ):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside indirect Landscape DML")
            construction_name = _resolved_callable_name(node.func, resolver, use=node)
            if (
                construction_name in {"insert", "update", "delete"}
                and node.args
                and _targets_landscape_schema(node.args[0], resolver, use=node)
            ):
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside Landscape DML construction")
            execution_name = _resolved_callable_name(node.func, resolver, use=node)
            dynamic_execution = isinstance(node.func, ast.Call) and _call_name(node.func) == "getattr"
            if (
                execution_name not in {"execute", "execute_insert", "execute_update", "exec_driver_sql", "scalar"} and not dynamic_execution
            ) or not node.args:
                continue
            shape = _raw_dml_shape(node, resolver)
            raw_sql = _raw_sql_literal(node, resolver)
            if execution_name == "exec_driver_sql" and raw_sql is None:
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside unknown raw SQL effect")
                continue
            if shape is None and raw_sql is not None and _raw_sql_is_write(raw_sql):
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside raw SQL write/DDL")
                continue
            statement = resolver.resolve_statement(node.args[0], use=node)
            if shape is None and isinstance(statement, ast.Call):
                shape = _dml_shape(statement, resolver) or _raw_dml_shape(statement, resolver)
            landscape_schema_target = _targets_landscape_schema(node.args[0], resolver, use=node)
            raw_outside_sessions = (
                shape is not None
                and shape[1].startswith("raw-")
                and (not unit.path.startswith("src/elspeth/web/sessions/") or "landscape" in shape[0])
            )
            if shape is not None and (shape[0] in landscape_tables or landscape_schema_target or raw_outside_sessions):
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside Landscape DML {shape[1]} {shape[0]}")
    return tuple(violations)


def _annotation_qualified_names(annotation: ast.expr | None, resolver: _Resolver, *, use: ast.AST) -> frozenset[str]:
    if annotation is None:
        return frozenset()
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return frozenset()
    names: set[str] = set()
    qualified = resolver.qualified_name(annotation, use=use)
    if qualified is not None:
        names.add(qualified)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        names.update(_annotation_qualified_names(annotation.left, resolver, use=use))
        names.update(_annotation_qualified_names(annotation.right, resolver, use=use))
    elif isinstance(annotation, ast.Subscript):
        names.update(_annotation_qualified_names(annotation.slice, resolver, use=use))
    return frozenset(names)


def _annotation_mentions_sessions(annotation: ast.expr | None, resolver: _Resolver, *, use: ast.AST) -> bool:
    return any(name.startswith("elspeth.web.sessions.") for name in _annotation_qualified_names(annotation, resolver, use=use))


def _local_annotation_mentions_sessions(name: str, resolver: _Resolver, *, use: ast.AST) -> bool:
    return any(
        candidate.lineno <= getattr(use, "lineno", 0) and _annotation_mentions_sessions(candidate.annotation, resolver, use=candidate)
        for candidate in resolver.annotation_assignments.get((id(_lexical_scope(use)), name), ())
    )


def _attribute_has_sessions_provenance(
    node: ast.AST,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int] = frozenset(),
) -> bool:
    if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"):
        return False
    current: ast.AST | None = use
    while current is not None and not isinstance(current, ast.ClassDef):
        current = getattr(current, "_landscape_parent", None)
    if not isinstance(current, ast.ClassDef):
        return False

    def aliases_self(value: ast.expr, *, candidate: ast.AST, seen: frozenset[str] = frozenset()) -> bool:
        if isinstance(value, ast.Name) and value.id == "self":
            return True
        if isinstance(value, ast.Name) and value.id not in seen:
            binding = resolver.binding(value.id, candidate)
            return binding is not None and aliases_self(binding, candidate=binding, seen=seen | {value.id})
        return False

    def value_is_sessions(value: ast.expr, candidate: ast.AST) -> bool:
        return _sessions_receiver_provenance(value, resolver, use=candidate, seen=seen)

    for candidate in ast.walk(current):
        if isinstance(candidate, ast.AnnAssign):
            target_name = (
                candidate.target.id
                if isinstance(candidate.target, ast.Name)
                else candidate.target.attr
                if isinstance(candidate.target, ast.Attribute)
                and isinstance(candidate.target.value, ast.Name)
                and candidate.target.value.id == "self"
                else None
            )
            if target_name == node.attr:
                if _annotation_mentions_sessions(candidate.annotation, resolver, use=candidate):
                    return True
                if candidate.value is not None and value_is_sessions(candidate.value, candidate):
                    return True
        if isinstance(candidate, ast.Assign) and len(candidate.targets) == 1:
            target = candidate.targets[0]
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == node.attr
            ):
                continue
            value = candidate.value
            if value_is_sessions(value, candidate):
                return True
        if (
            isinstance(candidate, ast.Call)
            and _resolved_callable_name(candidate.func, resolver, use=candidate)
            in {
                "setattr",
                "__setattr__",
            }
            and len(candidate.args) >= 3
        ):
            receiver, attribute, value = candidate.args[:3]
            if (
                aliases_self(receiver, candidate=candidate)
                and isinstance(attribute, ast.Constant)
                and attribute.value == node.attr
                and value_is_sessions(value, candidate)
            ):
                return True
    return False


def _sessions_receiver_provenance(
    node: ast.AST,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int] = frozenset(),
) -> bool:
    if seen:
        return _sessions_receiver_provenance_impl(node, resolver, use=use, seen=seen)
    key = (id(node), id(use))
    cached = resolver.sessions_provenance_cache.get(key)
    if cached is not None:
        return cached
    result = _sessions_receiver_provenance_impl(node, resolver, use=use)
    resolver.sessions_provenance_cache[key] = result
    return result


def _sessions_receiver_provenance_impl(
    node: ast.AST,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int] = frozenset(),
) -> bool:
    if id(node) in seen:
        return False
    next_seen = seen | {id(node)}
    if isinstance(node, ast.Name):
        binding = resolver.binding(node.id, use)
        if binding is not None and _sessions_receiver_provenance(binding, resolver, use=binding, seen=next_seen):
            return True
        parameter = resolver.parameter(node.id, use)
        if parameter is not None and _annotation_mentions_sessions(parameter.annotation, resolver, use=use):
            return True
        if _local_annotation_mentions_sessions(node.id, resolver, use=use):
            return True
    if isinstance(node, ast.Call):
        constructor = resolver.qualified_name(node.func, use=node) or ""
        if constructor.startswith("elspeth.web.sessions."):
            return True
        callable_name = _resolved_callable_name(node.func, resolver, use=node)
        if callable_name == "cast" and len(node.args) >= 2:
            return _annotation_mentions_sessions(node.args[0], resolver, use=node) or _sessions_receiver_provenance(
                node.args[1],
                resolver,
                use=node,
                seen=next_seen,
            )
        invoked = resolver.resolve_callable(node.func, use=node)
        if isinstance(invoked, ast.Lambda) and isinstance(invoked.body, ast.Name):
            parameters = [argument.arg for argument in (*invoked.args.posonlyargs, *invoked.args.args)]
            if invoked.body.id in parameters:
                position = parameters.index(invoked.body.id)
                if position < len(node.args):
                    return _sessions_receiver_provenance(node.args[position], resolver, use=node, seen=next_seen)
            return _sessions_receiver_provenance(invoked.body, resolver, use=invoked, seen=next_seen)
        partial_builder = invoked if isinstance(invoked, ast.Call) else node.func if isinstance(node.func, ast.Call) else None
        if (
            isinstance(partial_builder, ast.Call)
            and _resolved_callable_name(partial_builder.func, resolver, use=partial_builder) == "partial"
            and partial_builder.args
        ):
            target = resolver.resolve_callable(partial_builder.args[0], use=partial_builder)
            target_qualified = resolver.qualified_name(target, use=partial_builder) or ""
            if target_qualified.startswith("elspeth.web.sessions."):
                return True
            if isinstance(target, ast.Lambda) and isinstance(target.body, ast.Name):
                parameters = [argument.arg for argument in (*target.args.posonlyargs, *target.args.args)]
                if target.body.id in parameters:
                    position = parameters.index(target.body.id)
                    bound = partial_builder.args[1:]
                    if position < len(bound):
                        return _sessions_receiver_provenance(bound[position], resolver, use=partial_builder, seen=next_seen)
    if _attribute_has_sessions_provenance(node, resolver, use=use, seen=next_seen):
        return True
    qualified = resolver.qualified_name(node, use=use) or _dotted_name(node) or ""
    lowered = qualified.lower()
    if "elspeth.web.sessions" in lowered:
        return True
    segments = {segment.removeprefix("_").lower() for segment in qualified.split(".")}
    return bool(
        {
            "session_db",
            "sessions_db",
            "session_database",
            "sessions_database",
            "session_store",
            "sessions_store",
            "session_repository",
            "sessions_repository",
        }
        & segments
    )


def _cross_database_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    mutation_symbols = {(api.path, api.symbol) for api in _MUTATION_APIS}
    mutation_symbols.update((site.path, site.symbol) for site in scan_dml_identities(unit_list))
    violations: list[str] = []
    index = _function_index(unit_list)
    index_keys = set(index)
    index_keys_by_terminal = _function_terminal_index(unit_list)
    direct_tainted: set[tuple[str, str]] = set()
    calls: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for key in index:
        if (
            key[0] == "src/elspeth/core/landscape/run_coordination_repository.py"
            and key[1].rsplit(".", maxsplit=1)[-1] in _COORDINATION_MUTATION_METHOD_NAMES
        ):
            mutation_symbols.add(key)

    for unit in unit_list:
        resolver = _resolver_for_unit(unit)
        for key, child in _function_owned_attribute_and_call_nodes(unit):
            if isinstance(child, ast.Attribute) and _sessions_receiver_provenance(child.value, resolver, use=child):
                direct_tainted.add(key)
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Call) and child.args:
                operator_name = _resolved_callable_name(child.func.func, resolver, use=child.func)
                if operator_name in {"methodcaller", "attrgetter"} and _sessions_receiver_provenance(
                    child.args[0],
                    resolver,
                    use=child,
                ):
                    direct_tainted.add(key)
            receiver = _resolved_execution_receiver(child, resolver)
            if unit.path.startswith("src/elspeth/web/sessions/") or (
                receiver is not None and _sessions_receiver_provenance(receiver, resolver, use=child)
            ):
                direct_tainted.add(key)
            qualified_call = resolver.qualified_name(child.func, use=child) or ""
            if qualified_call.startswith("elspeth.web.sessions."):
                direct_tainted.add(key)
            selected = set(
                _resolve_helper_candidates(
                    child,
                    unit,
                    index_keys,
                    resolver,
                    helper_keys_by_terminal=index_keys_by_terminal,
                )
            )
            if not selected and isinstance(child.func, ast.Name):
                terminals = {
                    terminal
                    for callable_node in _possible_callable_nodes(child.func, resolver, use=child)
                    if (terminal := _resolved_callable_name(callable_node, resolver, use=child)) is not None
                }
                selected.update(candidate for terminal in terminals for candidate in index_keys_by_terminal.get(terminal, ()))
            calls.setdefault(key, set()).update(selected)

    def reaches_sessions(key: tuple[str, str], seen: frozenset[tuple[str, str]]) -> bool:
        if key in direct_tainted:
            return True
        if key in seen:
            return False
        return any(reaches_sessions(callee, seen | {key}) for callee in calls.get(key, ()))

    for path, symbol in sorted(mutation_symbols):
        if (path, symbol) in index and reaches_sessions((path, symbol), frozenset()):
            node = index[(path, symbol)]
            violations.append(f"{path}:{node.lineno} {symbol} crosses into Sessions database through helper closure")
    return tuple(violations)


def _function_owned_attribute_and_call_nodes(
    unit: SourceUnit,
) -> Iterable[tuple[tuple[str, str], ast.Attribute | ast.Call]]:
    """Yield relevant nodes once with their lexical function owner.

    Function signatures and decorators belong to the function they declare,
    while class-body expressions deliberately have no function owner.  Lambdas
    preserve the surrounding owner so closure calls remain in that function.
    """

    stack: list[tuple[ast.AST, tuple[str, str] | None]] = [(unit.tree, None)]
    while stack:
        node, owner_key = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner_key = (unit.path, _symbol(node))
        elif isinstance(node, ast.ClassDef):
            owner_key = None
        if owner_key is not None and isinstance(node, (ast.Attribute, ast.Call)):
            yield owner_key, node
        stack.extend((child, owner_key) for child in reversed(list(ast.iter_child_nodes(node))))


def _walk_same_scope(node: ast.AST) -> Iterable[ast.AST]:
    """Walk one lexical function body, pruning nested scope decoys."""

    stack = list(reversed(list(ast.iter_child_nodes(node))))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _is_descendant(node: ast.AST, ancestor: ast.AST) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = getattr(current, "_landscape_parent", None)
    return False


def _has_repeating_ancestor(node: ast.AST, *, stop: ast.AST) -> bool:
    current = getattr(node, "_landscape_parent", None)
    repeating = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    while current is not None and current is not stop:
        if isinstance(current, repeating):
            return True
        current = getattr(current, "_landscape_parent", None)
    return False


def _database_effect_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.Call, ...]:
    effect_names = {
        "begin_write",
        "execute",
        "execute_insert",
        "execute_update",
        "exec_driver_sql",
        "fenced_leader_transaction",
        "fenced_write",
        "scalar",
        "write_connection",
    }
    resolver = _resolver_for_node(node)
    return tuple(
        sorted(
            (
                child
                for child in _walk_same_scope(node)
                if isinstance(child, ast.Call)
                and (
                    _resolved_callable_name(child.func, resolver, use=child) in effect_names or _indirect_dml_execution(child, resolver)[0]
                )
                and not (
                    _resolved_callable_name(child.func, resolver, use=child) == "scalar"
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Call)
                    and _resolved_callable_name(child.func.value.func, resolver, use=child.func.value)
                    in {"execute", "execute_insert", "execute_update", "exec_driver_sql"}
                )
            ),
            key=lambda child: (child.lineno, child.col_offset),
        )
    )


def _fenced_contexts(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[FencedContext, ...]:
    result: list[FencedContext] = []
    resolver = _resolver_for_node(node)
    for child in _walk_same_scope(node):
        if not isinstance(child, (ast.With, ast.AsyncWith)):
            continue
        for item in child.items:
            qualified = (
                resolver.qualified_name(item.context_expr.func, use=item.context_expr) if isinstance(item.context_expr, ast.Call) else None
            )
            if (
                isinstance(item.context_expr, ast.Call)
                and _resolved_callable_name(item.context_expr.func, resolver, use=item.context_expr) in _FENCED_CONTEXT_NAMES
                and qualified in _TRUSTED_FENCE_QUALIFIED
                and not _trusted_qualified_name_is_mutated(qualified, resolver=resolver, use=item.context_expr)
            ):
                connection = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else None
                result.append(FencedContext(child, item.context_expr, connection))
    return tuple(result)


def _exact_token_keyword(call: ast.Call, parameter: ast.arg) -> bool:
    token_keywords = [keyword for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
    return len(token_keywords) == 1 and isinstance(token_keywords[0].value, ast.Name) and token_keywords[0].value.id == parameter.arg


def _exact_token_run_id_expression(node: ast.AST, token_parameter: ast.arg) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "run_id"
        and isinstance(node.value, ast.Name)
        and node.value.id == token_parameter.arg
    )


def _is_fail_closed_run_guard(statement: ast.stmt, token_parameter: ast.arg) -> bool:
    if not isinstance(statement, ast.If) or not statement.body or not isinstance(statement.body[0], ast.Raise):
        return False
    test = statement.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.NotEq):
        return False
    left, right = test.left, test.comparators[0]
    return (isinstance(left, ast.Name) and left.id == "run_id" and _exact_token_run_id_expression(right, token_parameter)) or (
        isinstance(right, ast.Name) and right.id == "run_id" and _exact_token_run_id_expression(left, token_parameter)
    )


def _run_id_is_bound_to_token(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    token_parameter: ast.arg,
    context: FencedContext,
) -> bool:
    argument_names = {argument.arg for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    if "run_id" not in argument_names:
        return True
    if _parameter_rebound(node, "run_id"):
        return False
    run_id_keywords = [keyword.value for keyword in context.call.keywords if keyword.arg == "run_id"]
    if run_id_keywords and (len(run_id_keywords) != 1 or not _exact_token_run_id_expression(run_id_keywords[0], token_parameter)):
        return False
    return any(statement.lineno < context.owner.lineno and _is_fail_closed_run_guard(statement, token_parameter) for statement in node.body)


_PAYLOAD_EFFECT_NAMES = frozenset({"execute", "execute_insert", "execute_update", "exec_driver_sql", "scalar"})


def _payload_uses_exact_connection(call: ast.Call, connection: str) -> bool:
    resolver = _resolver_for_node(call)
    name = _resolved_callable_name(call.func, resolver, use=call)
    if isinstance(call.func, ast.Attribute):
        return isinstance(call.func.value, ast.Name) and call.func.value.id == connection
    if name in {"execute_insert", "execute_update"} and call.args:
        return isinstance(call.args[0], ast.Name) and call.args[0].id == connection
    return False


def _owned_dml_constructions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.Call, ...]:
    resolver = _resolver_for_node(node)
    owner_symbol = _symbol(node)
    return tuple(
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and _symbol(child) == owner_symbol
        and (_dml_shape(child, resolver) is not None or _raw_dml_shape(child, resolver) is not None)
    )


def _expression_contains_target(
    expression: ast.expr,
    target: ast.Call,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[str] = frozenset(),
) -> bool:
    if expression is target:
        return True
    if isinstance(expression, ast.Name) and expression.id not in seen:
        binding = resolver.binding(expression.id, use)
        if binding is not None:
            return _expression_contains_target(binding, target, resolver, use=binding, seen=seen | {expression.id})
    return any(
        child is target or (isinstance(child, ast.expr) and _expression_contains_target(child, target, resolver, use=expression, seen=seen))
        for child in ast.iter_child_nodes(expression)
    )


def _expression_guarantees_target(
    expression: ast.expr,
    target: ast.Call,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[str] = frozenset(),
) -> bool:
    if expression is target:
        return True
    if isinstance(expression, ast.Name) and expression.id not in seen:
        binding = resolver.binding(expression.id, use)
        return binding is not None and _expression_guarantees_target(
            binding,
            target,
            resolver,
            use=binding,
            seen=seen | {expression.id},
        )
    if isinstance(expression, ast.IfExp):
        return _expression_guarantees_target(expression.test, target, resolver, use=expression, seen=seen)
    if isinstance(expression, ast.BoolOp):
        return bool(expression.values) and _expression_guarantees_target(
            expression.values[0],
            target,
            resolver,
            use=expression,
            seen=seen,
        )
    if isinstance(expression, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return False
    return any(
        isinstance(child, ast.expr) and _expression_guarantees_target(child, target, resolver, use=expression, seen=seen)
        for child in ast.iter_child_nodes(expression)
    )


def _direct_execution_statement(call: ast.Call, resolver: _Resolver) -> ast.expr | None:
    if isinstance(call.func, ast.Attribute) and call.func.attr in _PAYLOAD_EFFECT_NAMES and call.args:
        return call.args[0]
    name = _resolved_callable_name(call.func, resolver, use=call)
    if name in {"execute_insert", "execute_update"} and len(call.args) >= 2:
        return call.args[1]
    return call if _raw_dml_shape(call, resolver) is not None and isinstance(call.func, ast.Attribute) else None


def _is_statically_dead(node: ast.AST, *, stop: ast.AST) -> bool:
    def always_terminates(statement: ast.stmt) -> bool:
        if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return True
        if isinstance(statement, ast.If) and statement.body and statement.orelse:
            return any(always_terminates(item) for item in statement.body) and any(always_terminates(item) for item in statement.orelse)
        return False

    def preceded_by_terminator(current: ast.AST, parent: ast.AST) -> bool:
        for _field, value in ast.iter_fields(parent):
            if not isinstance(value, list) or current not in value:
                continue
            index = value.index(current)
            return any(isinstance(item, ast.stmt) and always_terminates(item) for item in value[:index])
        return False

    current = node
    while (parent := getattr(current, "_landscape_parent", None)) is not None and parent is not stop:
        if preceded_by_terminator(current, parent):
            return True
        if (
            isinstance(parent, ast.If)
            and (
                (isinstance(parent.test, ast.Constant) and not parent.test.value)
                or (isinstance(parent.test, ast.Name) and parent.test.id == "TYPE_CHECKING")
                or _dotted_name(parent.test) == "typing.TYPE_CHECKING"
            )
            and current in parent.body
        ):
            return True
        if isinstance(parent, ast.If) and isinstance(parent.test, ast.Constant) and parent.test.value and current in parent.orelse:
            return True
        if isinstance(parent, ast.While) and isinstance(parent.test, ast.Constant) and not parent.test.value:
            return True
        current = parent
    return False


def _dml_execution_binding_violation(node: ast.FunctionDef | ast.AsyncFunctionDef, connection: str) -> str | None:
    resolver = _resolver_for_node(node)
    direct_sites = [
        call for call in _walk_same_scope(node) if isinstance(call, ast.Call) and _direct_execution_statement(call, resolver) is not None
    ]
    for construction in _owned_dml_constructions(node):
        sites = [
            site
            for site in direct_sites
            if site is construction
            or (
                (statement := _direct_execution_statement(site, resolver)) is not None
                and _expression_guarantees_target(statement, construction, resolver, use=site)
            )
        ]
        if len(sites) != 1:
            return f"DML construction line {construction.lineno} exact direct executions={len(sites)} expected=1"
        site = sites[0]
        if _is_statically_dead(site, stop=node):
            return f"DML construction line {construction.lineno} executes only in statically dead code"
        if _has_repeating_ancestor(site, stop=node):
            return f"DML construction line {construction.lineno} executes in a runtime-repeating construct"
        if not _payload_uses_exact_connection(site, connection):
            return f"DML construction line {construction.lineno} does not execute once on exact connection {connection}"
    return None


def _dml_subject_roots(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[tuple[ast.Call, ast.expr], ...]:
    resolver = _resolver_for_node(node)

    def subject_statement(statement: ast.expr, *, use: ast.AST, seen: frozenset[str] = frozenset()) -> ast.expr:
        if isinstance(statement, ast.Name) and statement.id not in seen:
            binding = resolver.binding(statement.id, use)
            if binding is not None:
                return subject_statement(binding, use=binding, seen=seen | {statement.id})
        return statement

    direct_sites = [
        call for call in _walk_same_scope(node) if isinstance(call, ast.Call) and _direct_execution_statement(call, resolver) is not None
    ]
    roots: list[tuple[ast.Call, ast.expr]] = []
    for construction in _owned_dml_constructions(node):
        subject_roots: list[ast.expr] = []
        for site in direct_sites:
            statement = _direct_execution_statement(site, resolver)
            if statement is not None and _expression_contains_target(statement, construction, resolver, use=site):
                subject_roots.append(subject_statement(statement, use=site))
        roots.append((construction, subject_roots[0] if len(subject_roots) == 1 else construction))
    return tuple(roots)


def _dml_bare_run_subjects(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    return frozenset(
        child.id
        for _construction, root in _dml_subject_roots(node)
        for child in ast.walk(root)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id.endswith("run_id")
    )


def _is_table_run_id(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "run_id" and isinstance(node.value, ast.Attribute) and node.value.attr == "c"


def _run_column_subjects(root: ast.expr) -> tuple[ast.expr, ...]:
    subjects: list[ast.expr] = []
    for child in ast.walk(root):
        if isinstance(child, ast.Compare) and len(child.ops) == 1 and len(child.comparators) == 1:
            right = child.comparators[0]
            if _is_table_run_id(child.left):
                subjects.append(right)
            elif _is_table_run_id(right):
                subjects.append(child.left)
        if isinstance(child, ast.Call) and _call_name(child) == "values":
            subjects.extend(keyword.value for keyword in child.keywords if keyword.arg == "run_id")
            for argument in child.args:
                if not isinstance(argument, ast.Dict):
                    continue
                subjects.extend(
                    value
                    for key, value in zip(argument.keys, argument.values, strict=True)
                    if isinstance(key, ast.Constant) and key.value == "run_id"
                )
    return tuple(subjects)


def _dml_named_run_subjects(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    return _dml_bare_run_subjects(node) | frozenset(
        subject.id
        for _construction, root in _dml_subject_roots(node)
        for subject in _run_column_subjects(root)
        if isinstance(subject, ast.Name)
    )


def _dml_run_subject_violation(node: ast.FunctionDef | ast.AsyncFunctionDef, token_parameter: ast.arg) -> str | None:
    argument_names = {argument.arg for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    for construction, root in _dml_subject_roots(node):
        for subject in _run_column_subjects(root):
            if _exact_token_run_id_expression(subject, token_parameter):
                continue
            if isinstance(subject, ast.Name) and subject.id == "run_id" and subject.id in argument_names:
                continue
            return f"DML construction line {construction.lineno} uses non-token run-column subject"
        for child in ast.walk(root):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id.endswith("run_id"):
                if child.id == "run_id" and child.id in argument_names:
                    continue
                return f"DML construction line {construction.lineno} uses non-token run subject {child.id}"
            if not isinstance(child, ast.Attribute) or child.attr != "run_id":
                continue
            if _exact_token_run_id_expression(child, token_parameter):
                continue
            if isinstance(child.value, ast.Attribute) and child.value.attr == "c":
                continue
            return f"DML construction line {construction.lineno} uses non-token .run_id subject"
    return None


def _function_fence_violation(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    parameter = _authority_parameter(node)
    if parameter is None:
        return "missing explicit current token"
    resolver = _resolver_for_node(node)
    if not _is_exact_coordination_token_annotation(parameter.annotation, resolver=resolver, use=node):
        return "token annotation is not CoordinationToken"
    if _argument_default(node, parameter.arg) is not None:
        return "token is optional/defaulted"
    if _parameter_rebound(node, parameter.arg):
        return "token parameter is rebound"
    contexts = _fenced_contexts(node)
    if len(contexts) != 1:
        return f"fenced transaction contexts={len(contexts)} expected=1"
    context = contexts[0]
    if context.connection is None:
        return "fenced transaction must bind one exact connection name"
    if len(context.owner.items) != 1:
        return "full-token fence must be the sole context manager"
    if not _exact_token_keyword(context.call, parameter):
        return "fenced transaction does not receive the exact unaliased token parameter"
    if not _run_id_is_bound_to_token(node, parameter, context):
        return "run_id is not structurally bound to token.run_id"
    subject_violation = _dml_run_subject_violation(node, parameter)
    if subject_violation is not None:
        return subject_violation
    binding_violation = _dml_execution_binding_violation(node, context.connection)
    if binding_violation is not None:
        return binding_violation
    if any(_indirect_execution_payloads(child, resolver) for child in _walk_same_scope(node) if isinstance(child, ast.Call)):
        return "database execution is dispatched through an indirect callback"
    effects = _database_effect_calls(node)
    argument_effects = [effect for effect in effects if effect is not context.call and _is_descendant(effect, context.call)]
    if argument_effects or not effects or effects[0] is not context.call:
        return "full-token fence is not the transaction owner's first database effect"
    payload_effects = [
        effect
        for effect in effects
        if effect is not context.call
        and (
            _resolved_callable_name(effect.func, resolver, use=effect) in _PAYLOAD_EFFECT_NAMES
            or _indirect_dml_execution(effect, resolver)[0]
        )
    ]
    if any(not _is_descendant(effect, context.owner) for effect in payload_effects):
        return "payload SQL escapes the exact fenced transaction"
    if any(_has_repeating_ancestor(effect, stop=context.owner) for effect in payload_effects):
        return "payload SQL is nested in a runtime-repeating construct"
    if any(
        isinstance(child, ast.Name) and child.id == context.connection and isinstance(child.ctx, (ast.Store, ast.Del))
        for statement in context.owner.body
        for child in ast.walk(statement)
    ):
        return "exact fenced connection is rebound"
    if any(not _payload_uses_exact_connection(effect, context.connection) for effect in payload_effects):
        return "payload SQL does not use the exact fenced connection"
    return None


def _function_index(units: Iterable[SourceUnit]) -> dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef]:
    return _function_index_for_units(tuple(units))


@cache
def _function_index_for_units(units: tuple[SourceUnit, ...]) -> dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef]:
    result: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for unit in units:
        for node in ast.walk(unit.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_overload(node):
                result[(unit.path, _symbol(node))] = node
    return result


def _module_path_from_qualified(qualified: str) -> str | None:
    if not qualified.startswith("elspeth.") or "." not in qualified:
        return None
    module = qualified.rsplit(".", maxsplit=1)[0]
    return f"src/{module.replace('.', '/')}.py"


def _possible_callable_nodes(
    node: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int] = frozenset(),
) -> tuple[ast.expr, ...]:
    if id(node) in seen:
        return ()
    next_seen = seen | {id(node)}
    resolved = resolver.resolve_callable(node, use=use)
    if resolved is not node:
        return _possible_callable_nodes(resolved, resolver, use=resolved, seen=next_seen)
    if isinstance(node, ast.IfExp):
        return (
            *_possible_callable_nodes(node.body, resolver, use=node, seen=next_seen),
            *_possible_callable_nodes(node.orelse, resolver, use=node, seen=next_seen),
        )
    if isinstance(node, ast.Subscript):
        container = resolver.resolve_value(node.value, use=node)
        if isinstance(container, (ast.List, ast.Tuple, ast.Set)):
            return tuple(
                candidate
                for element in container.elts
                for candidate in _possible_callable_nodes(element, resolver, use=node, seen=next_seen)
            )
        if isinstance(container, ast.Dict):
            return tuple(
                candidate
                for element in container.values
                for candidate in _possible_callable_nodes(element, resolver, use=node, seen=next_seen)
            )
    if isinstance(node, ast.Call) and _resolved_callable_name(node.func, resolver, use=node) == "partial" and node.args:
        return _possible_callable_nodes(node.args[0], resolver, use=node, seen=next_seen)
    if isinstance(node, ast.Call):
        invoked = resolver.resolve_callable(node.func, use=node)
        if isinstance(invoked, ast.Lambda) and isinstance(invoked.body, ast.Name):
            parameters = [argument.arg for argument in (*invoked.args.posonlyargs, *invoked.args.args)]
            if invoked.body.id in parameters:
                position = parameters.index(invoked.body.id)
                if position < len(node.args):
                    return _possible_callable_nodes(node.args[position], resolver, use=node, seen=next_seen)
    if isinstance(node, ast.Lambda):
        return tuple(
            candidate
            for child in ast.walk(node.body)
            if isinstance(child, ast.Call)
            for candidate in _possible_callable_nodes(child.func, resolver, use=child, seen=next_seen)
        )
    return (node,)


def _resolve_helper_candidates(
    call: ast.Call,
    unit: SourceUnit,
    helper_keys: set[tuple[str, str]],
    resolver: _Resolver,
    *,
    helper_keys_by_terminal: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
) -> tuple[tuple[str, str], ...]:
    caller_symbol = _symbol(call)
    result: set[tuple[str, str]] = set()
    if helper_keys_by_terminal is None:
        helper_keys_by_terminal = _helper_key_terminal_index(helper_keys)
    for callable_node in _possible_callable_nodes(call.func, resolver, use=call):
        terminal = _resolved_callable_name(callable_node, resolver, use=call)
        if terminal is None:
            continue
        candidates = helper_keys_by_terminal.get(terminal, ())
        if not candidates:
            continue
        qualified = resolver.qualified_name(callable_node, use=call)
        if qualified is not None and (module_path := _module_path_from_qualified(qualified)) is not None:
            imported = [key for key in candidates if key[0] == module_path]
            if imported:
                result.update(imported)
                continue
        if isinstance(callable_node, ast.Attribute) and isinstance(callable_node.value, ast.Name):
            receiver = callable_node.value.id
            if receiver == "self" and "." in caller_symbol:
                owner = caller_symbol.rsplit(".", maxsplit=1)[0]
                exact = [key for key in candidates if key == (unit.path, f"{owner}.{terminal}")]
                if exact:
                    result.update(exact)
                    continue
            class_exact = [key for key in candidates if key == (unit.path, f"{receiver}.{terminal}")]
            if class_exact:
                result.update(class_exact)
                continue
            if receiver[:1].isupper():
                continue
        same_path = [key for key in candidates if key[0] == unit.path]
        if isinstance(callable_node, ast.Name):
            module_level = [key for key in same_path if key[1] == terminal]
            if module_level:
                result.update(module_level)
                continue
        if len(same_path) == 1:
            result.update(same_path)
        elif len(candidates) == 1:
            result.update(candidates)
    return tuple(sorted(result))


def _helper_key_terminal_index(
    helper_keys: Iterable[tuple[str, str]],
) -> dict[str, tuple[tuple[str, str], ...]]:
    by_terminal: dict[str, list[tuple[str, str]]] = {}
    for key in helper_keys:
        terminal = key[1].rsplit(".", maxsplit=1)[-1]
        by_terminal.setdefault(terminal, []).append(key)
    return {terminal: tuple(sorted(keys)) for terminal, keys in by_terminal.items()}


@cache
def _function_terminal_index(units: tuple[SourceUnit, ...]) -> dict[str, tuple[tuple[str, str], ...]]:
    return _helper_key_terminal_index(_function_index_for_units(units))


def _subordinate_helper_keys(
    index: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef],
    dml: Sequence[DmlIdentity],
) -> set[tuple[str, str]]:
    return {
        (site.path, site.symbol)
        for site in dml
        if (node := index.get((site.path, site.symbol))) is not None
        and any(argument.arg == "conn" for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
    }


def _subordinate_helper_edges(
    units: Iterable[SourceUnit],
    dml: Sequence[DmlIdentity],
) -> tuple[SubordinateHelperEdge, ...]:
    unit_list = tuple(units)
    index = _function_index(unit_list)
    helper_keys = _subordinate_helper_keys(index, dml)
    helper_keys_by_terminal = _helper_key_terminal_index(helper_keys)
    raw: list[SubordinateHelperEdge] = []
    for unit in unit_list:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            candidates = _resolve_helper_candidates(
                node,
                unit,
                helper_keys,
                resolver,
                helper_keys_by_terminal=helper_keys_by_terminal,
            )
            if len(candidates) != 1:
                continue
            helper_path, helper_symbol = candidates[0]
            caller_symbol = _symbol(node)
            if (unit.path, caller_symbol) == (helper_path, helper_symbol):
                continue
            raw.append(
                SubordinateHelperEdge(
                    helper_path,
                    helper_symbol,
                    unit.path,
                    caller_symbol,
                    _fingerprint(node),
                    0,
                    node.lineno,
                )
            )
    counters: Counter[tuple[str, str, str, str, str]] = Counter()
    result: list[SubordinateHelperEdge] = []
    for edge in sorted(raw, key=lambda item: (item.caller_path, item.line, item.helper_path, item.helper_symbol)):
        key = (edge.helper_path, edge.helper_symbol, edge.caller_path, edge.caller_symbol, edge.call_fingerprint)
        counters[key] += 1
        result.append(
            SubordinateHelperEdge(
                edge.helper_path,
                edge.helper_symbol,
                edge.caller_path,
                edge.caller_symbol,
                edge.call_fingerprint,
                counters[key],
                edge.line,
            )
        )
    return tuple(result)


def _subordinate_helper_resolution_violations(
    units: Iterable[SourceUnit],
    dml: Sequence[DmlIdentity],
) -> tuple[str, ...]:
    unit_list = tuple(units)
    index = _function_index(unit_list)
    helper_keys = _subordinate_helper_keys(index, dml)
    helper_keys_by_terminal = _helper_key_terminal_index(helper_keys)
    helper_terminals = helper_keys_by_terminal.keys()
    violations: list[str] = []
    for unit in unit_list:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for call in ast.walk(unit.tree):
            if not isinstance(call, ast.Call):
                continue
            terminals = {
                terminal
                for callable_node in _possible_callable_nodes(call.func, resolver, use=call)
                if (terminal := _resolved_callable_name(callable_node, resolver, use=call)) is not None
            }
            if not terminals & helper_terminals:
                continue
            candidates = _resolve_helper_candidates(
                call,
                unit,
                helper_keys,
                resolver,
                helper_keys_by_terminal=helper_keys_by_terminal,
            )
            if (
                not candidates
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id[:1].isupper()
            ):
                continue
            if len(candidates) != 1:
                violations.append(
                    f"{unit.path}:{call.lineno} {_symbol(call)} ambiguous subordinate helper "
                    f"{sorted(terminals & helper_terminals)!r} candidates={len(candidates)}"
                )
    return tuple(violations)


def _transaction_order_violations(
    units: Iterable[SourceUnit],
    dml: Sequence[DmlIdentity],
) -> tuple[str, ...]:
    unit_list = tuple(units)
    index = _function_index(unit_list)
    dml_symbols = {(site.path, site.symbol) for site in dml}
    exact_establishment_symbols = (
        {(item.caller_path, item.caller_symbol) for item in _AUTHORITY_ESTABLISHMENTS}
        | {(item.callee_path, item.callee_symbol) for item in _AUTHORITY_ESTABLISHMENTS}
        | {
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "verify_and_extend_leader_fence",
            )
        }
    )
    edges = _subordinate_helper_edges(unit_list, dml)
    edges_by_helper: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for edge in edges:
        edges_by_helper.setdefault((edge.helper_path, edge.helper_symbol), set()).add((edge.caller_path, edge.caller_symbol))
    violations: list[str] = list(_subordinate_helper_resolution_violations(unit_list, dml))
    for path, symbol in sorted(dml_symbols):
        if (path, symbol) in exact_establishment_symbols:
            continue
        node = index.get((path, symbol))
        if node is None:
            violations.append(f"{path}:{symbol} DML owner definition missing")
            continue
        connection_parameter = any(argument.arg == "conn" for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
        if connection_parameter:
            callers = edges_by_helper.get((path, symbol), set())
            if len(callers) != 1:
                violations.append(f"{path}:{symbol} subordinate raw-Connection helper callers={len(callers)} expected=1")
                continue
            caller_key = next(iter(callers))
            caller = index.get(caller_key)
            if caller is None:
                violations.append(f"{path}:{symbol} subordinate caller {caller_key!r} is unresolved")
                continue
            caller_violation = _function_fence_violation(caller)
            if caller_violation is not None:
                violations.append(f"{path}:{symbol} subordinate caller {caller_key[0]}:{caller_key[1]} is not fenced: {caller_violation}")
                continue
            fenced_context = _fenced_contexts(caller)[0]
            fenced_with = fenced_context.owner
            caller_unit = next(unit for unit in unit_list if unit.path == caller_key[0])
            caller_resolver = _resolver_for_unit(caller_unit)
            helper_keys = {(path, symbol)}
            helper_calls = [
                child
                for child in _walk_same_scope(caller)
                if isinstance(child, ast.Call)
                and _resolve_helper_candidates(child, caller_unit, helper_keys, caller_resolver) == ((path, symbol),)
            ]
            if len(helper_calls) != 1:
                violations.append(f"{path}:{symbol} subordinate call sites={len(helper_calls)} expected=1")
                continue
            if any(not _is_descendant(call, fenced_with) for call in helper_calls):
                violations.append(f"{path}:{symbol} subordinate call escapes its caller's fenced transaction")
                continue
            if any(_has_repeating_ancestor(call, stop=fenced_with) for call in helper_calls):
                violations.append(f"{path}:{symbol} subordinate call is nested in a runtime-repeating construct")
                continue
            if any(_is_statically_dead(call, stop=caller) for call in helper_calls):
                violations.append(f"{path}:{symbol} subordinate call is statically unreachable")
                continue
            helper = index[(path, symbol)]
            helper_unit = next(unit for unit in unit_list if unit.path == path)
            helper_resolver = _resolver_for_unit(helper_unit)
            recursive_calls = [
                child
                for child in _walk_same_scope(helper)
                if isinstance(child, ast.Call)
                and _resolve_helper_candidates(child, helper_unit, {(path, symbol)}, helper_resolver) == ((path, symbol),)
            ]
            if recursive_calls:
                violations.append(f"{path}:{symbol} subordinate helper recursively invokes itself")
                continue
            if _parameter_rebound(helper, "conn"):
                violations.append(f"{path}:{symbol} subordinate conn parameter is rebound")
                continue
            helper_binding_violation = _dml_execution_binding_violation(helper, "conn")
            if helper_binding_violation is not None:
                violations.append(f"{path}:{symbol} {helper_binding_violation}")
                continue
            positional_parameters = [argument.arg for argument in (*helper.args.posonlyargs, *helper.args.args) if argument.arg != "self"]
            conn_position = positional_parameters.index("conn") if "conn" in positional_parameters else None

            def exact_helper_connection(
                call: ast.Call,
                conn_position: int | None = conn_position,
                fenced_connection: str | None = fenced_context.connection,
            ) -> bool:
                values: list[ast.expr] = [keyword.value for keyword in call.keywords if keyword.arg == "conn"]
                if conn_position is not None and conn_position < len(call.args):
                    values.append(call.args[conn_position])
                return (
                    len(values) == 1
                    and isinstance(values[0], ast.Name)
                    and values[0].id == fenced_connection
                    and not any(keyword.arg is None for keyword in call.keywords)
                )

            if any(not exact_helper_connection(call) for call in helper_calls):
                violations.append(f"{path}:{symbol} subordinate call does not receive the exact caller-owned conn")
                continue
            caller_token = _authority_parameter(caller)
            helper_arguments = [
                argument.arg
                for argument in (*helper.args.posonlyargs, *helper.args.args, *helper.args.kwonlyargs)
                if argument.arg != "self"
            ]
            helper_run_parameters = _dml_named_run_subjects(helper)
            helper_subject_invalid = False
            for call in helper_calls:
                for run_parameter in helper_run_parameters:
                    if run_parameter not in helper_arguments or _parameter_rebound(helper, run_parameter):
                        helper_subject_invalid = True
                        continue
                    values = [keyword.value for keyword in call.keywords if keyword.arg == run_parameter]
                    if run_parameter in positional_parameters:
                        position = positional_parameters.index(run_parameter)
                        if position < len(call.args):
                            values.append(call.args[position])
                    if caller_token is None or len(values) != 1 or not _exact_token_run_id_expression(values[0], caller_token):
                        helper_subject_invalid = True
            if helper_subject_invalid:
                violations.append(f"{path}:{symbol} subordinate run subject is not exact caller token.run_id")
                continue
            helper_effects = [
                effect
                for effect in _database_effect_calls(helper)
                if _resolved_callable_name(effect.func, helper_resolver, use=effect) in _PAYLOAD_EFFECT_NAMES
            ]
            if any(not _payload_uses_exact_connection(effect, "conn") for effect in helper_effects):
                violations.append(f"{path}:{symbol} subordinate DML does not use the exact helper connection")
        else:
            violation = _function_fence_violation(node)
            if violation is not None:
                violations.append(f"{path}:{symbol} {violation}")
    return tuple(violations)


_ESTABLISHMENT_HELPER_SYMBOLS: dict[str, frozenset[tuple[str, str]]] = {
    "fresh-run-epoch-1-creation": frozenset(
        {
            (_RUN_LIFECYCLE_PATH, "RunLifecycleRepository.begin_run"),
            (_RUN_LIFECYCLE_PATH, "RunLifecycleRepository._insert_web_plugin_policy_evidence"),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository.register_run_leader_on",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._insert_worker_row",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "record_coordination_event",
            ),
        }
    ),
    "existing-run-leadership-claim": frozenset(
        {
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository.acquire_run_leadership",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._acquire_run_leadership_on",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._insert_worker_row",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "record_coordination_event",
            ),
        }
    ),
    "follower-membership-admission": frozenset(
        {
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository.admit_follower",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._insert_worker_row",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "record_coordination_event",
            ),
        }
    ),
}


def _establishment_live_write_counts(
    establishment: AuthorityEstablishmentException,
    units: Sequence[SourceUnit],
    dml: Sequence[DmlIdentity],
) -> Counter[tuple[str, str]]:
    allowed = _ESTABLISHMENT_HELPER_SYMBOLS[establishment.classification]
    index = _function_index(units)
    dml_by_symbol: dict[tuple[str, str], Counter[tuple[str, str]]] = {}
    for site in dml:
        if (site.path, site.symbol) in allowed:
            dml_by_symbol.setdefault((site.path, site.symbol), Counter())[(site.table, site.operation.removeprefix("raw-"))] += 1

    def visit(key: tuple[str, str], stack: frozenset[tuple[str, str]]) -> Counter[tuple[str, str]]:
        if key in stack:
            raise AssertionError(f"cyclic authority-establishment helper graph at {key!r}")
        result = Counter(dml_by_symbol.get(key, Counter()))
        node = index[key]
        for call in _walk_same_scope(node):
            if not isinstance(call, ast.Call):
                continue
            method = _call_name(call)
            if method is None:
                continue
            candidates = [candidate for candidate in allowed if candidate[1].rsplit(".", maxsplit=1)[-1] == method]
            if len(candidates) > 1:
                same_path = [candidate for candidate in candidates if candidate[0] == key[0]]
                candidates = same_path or candidates
            if len(candidates) == 1:
                result.update(visit(candidates[0], stack | {key}))
        return result

    return visit((establishment.caller_path, establishment.caller_symbol), frozenset())


def _begin_run_production_call_violations(unit_list: Sequence[SourceUnit]) -> tuple[str, ...]:
    violations: list[str] = []
    begin_calls: list[tuple[SourceUnit, ast.Call]] = []
    for unit in unit_list:
        if unit.path.startswith("src/elspeth/core/landscape/") or unit.path == _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for call in ast.walk(unit.tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "begin_run"
                and _looks_like_landscape_receiver(call.func.value, call.func.attr, resolver=resolver, use=call)
            ):
                begin_calls.append((unit, call))
    begin_callers = Counter((unit.path, _symbol(call)) for unit, call in begin_calls)
    expected_begin_callers = Counter(dict.fromkeys(_EXACT_BEGIN_RUN_PRODUCTION_CALLERS, 1))
    if begin_callers != expected_begin_callers:
        violations.append(
            f"begin_run production caller multiplicity drifted: expected={sorted(expected_begin_callers.items())!r} "
            f"actual={sorted(begin_callers.items())!r}"
        )
    for unit, call in begin_calls:
        arguments = _exact_keyword_arguments(call)
        run_id = None if arguments is None else arguments.get("run_id")
        if arguments is None or not isinstance(run_id, ast.Name) or run_id.id != "run_id":
            violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} begin_run must bind one exact explicit run_id keyword")
    return tuple(violations)


def _standalone_register_run_leader_definition_violation(units: Iterable[SourceUnit]) -> str | None:
    standalone_key = (
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "RunCoordinationRepository.register_run_leader",
    )
    if standalone_key in _function_index(units):
        return "standalone public register_run_leader wrapper still exists; remove or privatize it"
    return None


def _begin_run_edge_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    violations: list[str] = list(_begin_run_production_call_violations(unit_list))

    coordination_calls = _scan_exact_attribute_calls(
        unit_list,
        frozenset({"register_run_leader", "register_run_leader_on"}),
    )
    exact_edges = [
        call
        for call in coordination_calls
        if call.path == _FRESH_EPOCH_ONE_EXCEPTION.caller_path
        and call.symbol == _FRESH_EPOCH_ONE_EXCEPTION.caller_symbol
        and call.method == "register_run_leader_on"
    ]
    if len(exact_edges) != 1:
        violations.append(f"begin_run -> register_run_leader_on edges={len(exact_edges)} expected=1")
    else:
        edge_unit = next(unit for unit in unit_list if unit.path == _FRESH_EPOCH_ONE_EXCEPTION.caller_path)
        edge_node = next(
            node
            for node in ast.walk(edge_unit.tree)
            if isinstance(node, ast.Call)
            and node.lineno == exact_edges[0].line
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_run_leader_on"
        )
        edge_arguments = _exact_keyword_arguments(
            ast.Call(
                func=edge_node.func,
                args=edge_node.args[1:],
                keywords=edge_node.keywords,
            )
        )
        if not (
            len(edge_node.args) == 1
            and isinstance(edge_node.args[0], ast.Name)
            and edge_node.args[0].id == "conn"
            and edge_arguments is not None
            and {"run_id", "worker_id", "window_seconds", "entry_point"} <= edge_arguments.keys()
            and set(edge_arguments) - {"run_id", "worker_id", "window_seconds", "entry_point", "now"} == set()
            and isinstance(edge_arguments["run_id"], ast.Attribute)
            and _dotted_name(edge_arguments["run_id"]) == "run.run_id"
            and isinstance(edge_arguments["worker_id"], ast.Name)
            and edge_arguments["worker_id"].id == "worker_id"
            and isinstance(edge_arguments["window_seconds"], ast.Name)
            and edge_arguments["window_seconds"].id in {"window_seconds", "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS"}
            and isinstance(edge_arguments["entry_point"], ast.Constant)
            and edge_arguments["entry_point"].value == "run"
        ):
            violations.append("begin_run -> register_run_leader_on does not bind the exact transaction/run/worker subject")
        edge_owner = next(
            node
            for node in ast.walk(edge_unit.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _symbol(node) == _FRESH_EPOCH_ONE_EXCEPTION.caller_symbol
        )
        for subject in ("run", "worker_id"):
            if _subject_rebound(edge_owner, subject):
                violations.append(f"begin_run -> register_run_leader_on {subject} subject is rebound")
        if _has_repeating_ancestor(edge_node, stop=edge_owner):
            violations.append("begin_run -> register_run_leader_on edge is runtime-repeating")
        if _is_statically_dead(edge_node, stop=edge_owner):
            violations.append("begin_run -> register_run_leader_on edge is statically unreachable")

    standalone = [call for call in coordination_calls if call.method == "register_run_leader"]
    if standalone:
        violations.append(
            "standalone register_run_leader has production callers: "
            + ", ".join(f"{call.path}:{call.line} {call.symbol}" for call in standalone)
        )

    standalone_definition_violation = _standalone_register_run_leader_definition_violation(unit_list)
    if standalone_definition_violation is not None:
        violations.append(standalone_definition_violation)

    dml = scan_dml_identities(unit_list)
    for establishment in _AUTHORITY_ESTABLISHMENTS:
        actual = _establishment_live_write_counts(establishment, unit_list, dml)
        expected = Counter({(table, operation): count for table, operation, count in establishment.write_counts})
        if actual != expected:
            violations.append(
                f"{establishment.classification} write counts drifted: expected={sorted(expected.items())!r} "
                f"actual={sorted(actual.items())!r}"
            )
    return tuple(violations)


def _format_violations(title: str, violations: Sequence[str]) -> str:
    return title + f" ({len(violations)}):\n" + "\n".join(f"  {item}" for item in violations[:120])


def test_architecture_scanner_detects_duplicate_move_replace_and_write_set_drift() -> None:
    original = _parse_source(
        "src/elspeth/core/landscape/example.py",
        textwrap.dedent(
            """\
            from sqlalchemy import insert
            from elspeth.core.landscape.schema import runs_table

            class Example:
                def write(self, conn):
                    conn.execute(insert(runs_table).values(run_id="r"))
            """
        ),
    )
    live = scan_dml_identities([original])
    assert len(live) == 1
    baseline = _canonical_digest(live)

    duplicate = _parse_source(original.path, original.source + "\nExample.write_again = Example.write\n")
    # A callable alias is rejected even when it does not create a second DML AST.
    assert _mutation_callable_escapes([duplicate]) == ()  # unrelated method name is not over-claimed

    moved = _parse_source(original.path, original.source.replace("class Example:", "class Replacement:"))
    replaced = _parse_source(original.path, original.source.replace("insert(runs_table)", "runs_table.delete()"))
    added = _parse_source(
        original.path,
        original.source.replace(
            'conn.execute(insert(runs_table).values(run_id="r"))',
            'conn.execute(insert(runs_table).values(run_id="r"))\n        conn.execute(insert(runs_table).values(run_id="s"))',
        ),
    )
    assert _canonical_digest(scan_dml_identities([moved])) != baseline
    assert _canonical_digest(scan_dml_identities([replaced])) != baseline
    added_sites = scan_dml_identities([added])
    assert _canonical_digest(added_sites) != baseline
    assert [site.ordinal for site in added_sites] == [1, 1]
    assert {(site.table, site.operation) for site in replaced and scan_dml_identities([replaced])} == {("runs", "delete")}


def test_architecture_scanner_rejects_alias_dynamic_getattr_and_callable_escape() -> None:
    unit = _parse_source(
        "src/elspeth/engine/example.py",
        textwrap.dedent(
            """\
            def escape(factory):
                repo = factory.run_lifecycle
                alias = repo.begin_run
                callback(alias)
                return getattr(repo, "complete_run")
            """
        ),
    )
    violations = _mutation_callable_escapes([unit])
    assert any("callable escape .begin_run" in item for item in violations)
    assert any("dynamic getattr('complete_run')" in item for item in violations)


def test_architecture_scanner_rejects_raw_writable_and_cross_database_surfaces() -> None:
    raw = _parse_source(
        "src/elspeth/web/example.py",
        "def bypass(factory, ops):\n    factory.write_repositories()\n    ops.execute_update(statement)\n",
    )
    assert len(_raw_write_surface_violations([raw])) == 2

    cross_database = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "class RunLifecycleRepository:\n    def complete_run(self, coordination_token):\n        return self.session_db.execute('bad')\n",
    )
    assert _cross_database_violations([cross_database]) == (
        f"{_RUN_LIFECYCLE_PATH}:2 RunLifecycleRepository.complete_run crosses into Sessions database through helper closure",
    )

    transitive = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "def hidden(session_db):\n"
        "    return session_db.execute('bad')\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, coordination_token):\n"
        "        return hidden(self._other)\n",
    )
    assert _cross_database_violations([transitive]) == (
        f"{_RUN_LIFECYCLE_PATH}:4 RunLifecycleRepository.complete_run crosses into Sessions database through helper closure",
    )

    provenance = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, coordination_token):\n"
        "        return self._session_store.execute('bad')\n",
    )
    assert _cross_database_violations([provenance]) == (
        f"{_RUN_LIFECYCLE_PATH}:2 RunLifecycleRepository.complete_run crosses into Sessions database through helper closure",
    )

    ambiguous_mutation = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "class RunLifecycleRepository:\n    def complete_run(self, coordination_token):\n        return hidden(self._other)\n",
    )
    session_helper = _parse_source(
        "src/elspeth/web/sessions/hidden.py",
        "def hidden(store):\n    return store.execute('bad')\n",
    )
    unrelated_helper = _parse_source(
        "src/elspeth/engine/hidden.py",
        "def hidden(store):\n    return 1\n",
    )
    assert _cross_database_violations([ambiguous_mutation, session_helper, unrelated_helper]) == (
        f"{_RUN_LIFECYCLE_PATH}:2 RunLifecycleRepository.complete_run crosses into Sessions database through helper closure",
    )

    dynamic_sessions = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from sqlalchemy import select\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, coordination_token):\n"
        "        return getattr(self._session_store, 'execute')(select(runs_table))\n",
    )
    assert _cross_database_violations([dynamic_sessions]) == (
        f"{_RUN_LIFECYCLE_PATH}:3 RunLifecycleRepository.complete_run crosses into Sessions database through helper closure",
    )

    typed_neutral_sessions = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    _store: SessionsDatabase\n"
        "    def complete_run(self, coordination_token):\n"
        "        return self._store.execute(select(runs_table))\n",
    )
    assert _cross_database_violations([typed_neutral_sessions]) == (
        f"{_RUN_LIFECYCLE_PATH}:4 RunLifecycleRepository.complete_run crosses into Sessions database through helper closure",
    )

    constructed_sessions = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, coordination_token):\n"
        "        store = SessionsDatabase(engine)\n"
        "        return store.lookup('bad')\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([constructed_sessions]))

    dynamic_or_typed_sessions = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, store: SessionsDatabase, operation, coordination_token):\n"
        "        getattr(store, operation)('bad')\n"
        "        store.future_terminal_method('bad')\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([dynamic_or_typed_sessions]))

    coordination_cross = _parse_source(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "class RunCoordinationRepository:\n    def release_seat(self, token):\n        return self._session_store.lookup(token.run_id)\n",
    )
    assert any(
        "RunCoordinationRepository.release_seat crosses into Sessions" in item for item in _cross_database_violations([coordination_cross])
    )

    dml_owner_cross = _parse_source(
        "src/elspeth/core/landscape/new_writer.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def new_writer(conn, session_store):\n"
        "    session_store.lookup('bad')\n"
        "    conn.execute(update(runs_table))\n",
    )
    assert any("new_writer crosses into Sessions" in item for item in _cross_database_violations([dml_owner_cross]))

    relative_sessions_helper = _parse_source(
        "src/elspeth/web/sessions/relative_helper.py",
        "def touch(store):\n    return store.lookup('bad')\n",
    )
    relative_sessions_caller = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.relative_helper import touch as mutate_sessions\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, coordination_token):\n"
        "        return mutate_sessions(self._other)\n",
    )
    relative_sessions_caller = _parse_source(
        _RUN_LIFECYCLE_PATH,
        relative_sessions_caller.source.replace(
            "from elspeth.web.sessions.relative_helper import touch as mutate_sessions",
            "from ...web.sessions.relative_helper import touch as mutate_sessions",
        ),
    )
    assert any(
        "complete_run crosses into Sessions" in item
        for item in _cross_database_violations([relative_sessions_caller, relative_sessions_helper])
    )

    constructor_attribute = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def __init__(self, store: SessionsDatabase):\n        self._store = store\n"
        "    def complete_run(self, coordination_token):\n        return self._store.lookup('bad')\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([constructor_attribute]))

    lambda_sessions = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, store: SessionsDatabase, coordination_token):\n"
        "        return (lambda: store.lookup('bad'))()\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([lambda_sessions]))

    partial_sessions = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from functools import partial\n"
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def complete_run(self, store: SessionsDatabase, coordination_token):\n"
        "        return partial(store.lookup, 'bad')()\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([partial_sessions]))

    setattr_laundering = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from functools import partial\n"
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def __init__(self, store: SessionsDatabase):\n"
        "        setattr(self, '_store', store)\n"
        "    def complete_run(self, coordination_token):\n"
        "        return partial(self._store.lookup, 'bad')()\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([setattr_laundering]))


def test_reopen_sessions_provenance_rejects_annotations_casts_constructors_identity_and_attribute_laundering() -> None:
    cases = {
        "quoted": "def complete_run(self, store: 'SessionsDatabase', coordination_token):\n        return store.lookup('bad')",
        "union": "def complete_run(self, store: SessionsDatabase | None, coordination_token):\n        return store.lookup('bad')",
        "local": "def complete_run(self, value, coordination_token):\n        local: SessionsDatabase = value\n        return local.lookup('bad')",
        "cast_to": "def complete_run(self, value, coordination_token):\n        return cast(SessionsDatabase, value).lookup('bad')",
        "cast_away": "def complete_run(self, store: SessionsDatabase, coordination_token):\n        return cast(object, store).lookup('bad')",
        "partial_constructor": (
            "def complete_run(self, coordination_token):\n"
            "        factory = partial(SessionsDatabase, engine)\n"
            "        return factory().lookup('bad')"
        ),
        "dynamic_constructor": (
            "def complete_run(self, coordination_token):\n"
            "        module_name = 'elspeth.web.' + 'sessions.database'\n"
            "        class_name = 'Sessions' + 'Database'\n"
            "        factory = getattr(importlib.import_module(module_name), class_name)\n"
            "        return factory(engine).lookup('bad')"
        ),
        "lambda_identity": (
            "def complete_run(self, store: SessionsDatabase, coordination_token):\n"
            "        return (lambda value: value)(store).lookup('bad')"
        ),
        "lambda_closure": (
            "def complete_run(self, store: SessionsDatabase, coordination_token):\n        return (lambda: store)().lookup('bad')"
        ),
        "partial_identity": (
            "def complete_run(self, store: SessionsDatabase, coordination_token):\n"
            "        identity = lambda value: value\n"
            "        return partial(identity, store)().lookup('bad')"
        ),
        "operator_methodcaller": (
            "def complete_run(self, store: SessionsDatabase, coordination_token):\n"
            "        return operator.methodcaller('lookup', 'bad')(store)"
        ),
    }
    for name, body in cases.items():
        unit = _parse_source(
            _RUN_LIFECYCLE_PATH,
            "import importlib\n"
            "import operator\n"
            "from functools import partial\n"
            "from typing import cast\n"
            "from elspeth.web.sessions.database import SessionsDatabase\n"
            "class RunLifecycleRepository:\n"
            f"    {body}\n",
        )
        assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([unit])), name

    setter_cases = {
        "object": "object.__setattr__(self, '_store', store)",
        "aliased_builtin": "target = self\n        setter = setattr\n        setter(target, '_store', store)",
        "aliased_object": "target = self\n        setter = object.__setattr__\n        setter(target, '_store', store)",
    }
    for name, setter in setter_cases.items():
        unit = _parse_source(
            _RUN_LIFECYCLE_PATH,
            "from elspeth.web.sessions.database import SessionsDatabase\n"
            "class RunLifecycleRepository:\n"
            "    def __init__(self, store: SessionsDatabase):\n"
            f"        {setter}\n"
            "    def complete_run(self, coordination_token):\n"
            "        return self._store.lookup('bad')\n",
        )
        assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([unit])), name

    neutral_attribute_cycle = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "class RunLifecycleRepository:\n"
        "    def __init__(self):\n"
        "        self._left = self._right\n"
        "        self._right = self._left\n"
        "    def complete_run(self, coordination_token):\n"
        "        return self._left.lookup('safe')\n",
    )
    assert _cross_database_violations([neutral_attribute_cycle]) == ()

    anchored_attribute_cycle = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from elspeth.web.sessions.database import SessionsDatabase\n"
        "class RunLifecycleRepository:\n"
        "    def __init__(self, store: SessionsDatabase):\n"
        "        self._left = self._right\n"
        "        self._right = self._left\n"
        "        self._right: SessionsDatabase = store\n"
        "    def complete_run(self, coordination_token):\n"
        "        return self._left.lookup('bad')\n",
    )
    assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([anchored_attribute_cycle]))

    control = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "from typing import cast\n"
        "class RunLifecycleRepository:\n"
        "    def __init__(self, other, store):\n"
        "        setattr(other, '_store', store)\n"
        "    def complete_run(self, value, session_count, session_id, coordination_token):\n"
        "        cast(str, value).upper()\n"
        "        session_count.bit_length()\n"
        "        return session_id.hex\n",
    )
    assert _cross_database_violations([control]) == ()


def test_reopen_sessions_call_graph_rejects_callable_alias_list_lambda_and_partial_relays() -> None:
    leaf = _parse_source(
        "src/elspeth/web/sessions/reopen_leaf.py",
        "def leaf(store):\n    return store.lookup('bad')\n",
    )
    relay = _parse_source(
        "src/elspeth/engine/reopen_relay.py",
        "from elspeth.web.sessions.reopen_leaf import leaf\ndef relay(store):\n    return leaf(store)\n",
    )
    variants = {
        "alias": "invoke = relay\n        return invoke(store)",
        "list": "return [relay][0](store)",
        "lambda": "return (lambda fn: fn)(relay)(store)",
        "partial": "return partial(relay, store)()",
    }
    for name, invocation in variants.items():
        owner = _parse_source(
            _RUN_LIFECYCLE_PATH,
            "from functools import partial\n"
            "from elspeth.engine.reopen_relay import relay\n"
            "class RunLifecycleRepository:\n"
            "    def complete_run(self, store, coordination_token):\n"
            f"        {invocation}\n",
        )
        assert any("complete_run crosses into Sessions" in item for item in _cross_database_violations([owner, relay, leaf])), name

    coordination = _parse_source(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "from elspeth.engine.reopen_relay import relay\n"
        "class RunCoordinationRepository:\n"
        "    def release_seat(self, store, token):\n"
        "        return [relay][0](store)\n",
    )
    assert any("release_seat crosses into Sessions" in item for item in _cross_database_violations([coordination, relay, leaf]))

    dml_owner = _parse_source(
        "src/elspeth/core/landscape/reopen_dml_owner.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "from elspeth.engine.reopen_relay import relay\n"
        "def write(conn, store):\n"
        "    (lambda fn: fn)(relay)(store)\n"
        "    conn.execute(update(runs_table))\n",
    )
    assert any("write crosses into Sessions" in item for item in _cross_database_violations([dml_owner, relay, leaf]))


def test_architecture_scanner_rejects_optional_or_untyped_authority() -> None:
    required = _parse_source(
        _CHECKPOINT_PATH,
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: CoordinationToken):\n"
        "        pass\n"
        "    def delete_checkpoints(self, *, coordination_token=None):\n"
        "        pass\n",
    )
    violations = _api_authority_violations([required])
    # The synthetic unit deliberately omits every other API; focus on the
    # present delete verb and prove an optional/untyped parameter is refused.
    assert any("CheckpointManager.delete_checkpoints token annotation" in item for item in violations)
    assert any("CheckpointManager.delete_checkpoints token is optional" in item for item in violations)
    assert not any("CheckpointManager.create_checkpoint" in item for item in violations)

    optional_union = _parse_source(
        _CHECKPOINT_PATH,
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: CoordinationToken | None):\n"
        "        pass\n"
        "    def delete_checkpoints(self, *, coordination_token: CoordinationToken):\n"
        "        pass\n",
    )
    assert any(
        "CheckpointManager.create_checkpoint token annotation is not CoordinationToken" in item
        for item in _api_authority_violations([optional_union])
    )

    fake_annotation = _parse_source(
        _CHECKPOINT_PATH,
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: FakeCoordinationToken):\n"
        "        pass\n"
        "    def delete_checkpoints(self, *, coordination_token: CoordinationToken):\n"
        "        pass\n",
    )
    assert any(
        "CheckpointManager.create_checkpoint token annotation is not CoordinationToken" in item
        for item in _api_authority_violations([fake_annotation])
    )

    attacker_annotation = _parse_source(
        _CHECKPOINT_PATH,
        "from attacker import CoordinationToken\n"
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: CoordinationToken):\n"
        "        pass\n"
        "    def delete_checkpoints(self, *, coordination_token: CoordinationToken):\n"
        "        pass\n",
    )
    attacker_violations = _api_authority_violations([attacker_annotation])
    assert any("CheckpointManager.create_checkpoint token annotation" in item for item in attacker_violations)
    assert any("CheckpointManager.delete_checkpoints token annotation" in item for item in attacker_violations)

    unbound_or_quoted = _parse_source(
        _CHECKPOINT_PATH,
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: CoordinationToken):\n"
        "        pass\n"
        "    def delete_checkpoints(self, *, coordination_token: 'CoordinationToken'):\n"
        "        pass\n",
    )
    unbound_violations = _api_authority_violations([unbound_or_quoted])
    assert any("CheckpointManager.create_checkpoint token annotation" in item for item in unbound_violations)
    assert any("CheckpointManager.delete_checkpoints token annotation" in item for item in unbound_violations)

    unrelated_local_import = _parse_source(
        _CHECKPOINT_PATH,
        "from attacker import CoordinationToken\n"
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: CoordinationToken):\n        pass\n"
        "    def delete_checkpoints(self, *, coordination_token: CoordinationToken):\n        pass\n"
        "def unrelated():\n"
        "    from elspeth.contracts.coordination import CoordinationToken\n"
        "    return CoordinationToken\n",
    )
    assert sum("token annotation" in item for item in _api_authority_violations([unrelated_local_import])) == 2

    unrelated_class_import = _parse_source(
        _CHECKPOINT_PATH,
        "from attacker import CoordinationToken\n"
        "class ImportHolder:\n"
        "    from elspeth.contracts.coordination import CoordinationToken\n"
        "class CheckpointManager:\n"
        "    def create_checkpoint(self, *, coordination_token: CoordinationToken):\n        pass\n"
        "    def delete_checkpoints(self, *, coordination_token: CoordinationToken):\n        pass\n",
    )
    assert sum("token annotation" in item for item in _api_authority_violations([unrelated_class_import])) == 2


def test_receiver_provenance_excludes_unrelated_common_names_and_resolves_alias() -> None:
    unrelated = _parse_source(
        "src/elspeth/web/unrelated.py",
        "def unrelated(manager, factory, context):\n    manager.finalize()\n    factory.finalize()\n    context.finalize()\n",
    )
    assert scan_production_calls([unrelated]) == ()
    assert sum("unknown mutation receiver .finalize" in item for item in _mutation_callable_escapes([unrelated])) == 1

    aliased = _parse_source(
        "src/elspeth/engine/aliased.py",
        "def run(factory):\n    writer = factory.run_lifecycle\n    writer.finalize_run(run_id)\n",
    )
    calls = scan_production_calls([aliased])
    assert [(call.method, call.receiver) for call in calls] == [("finalize_run", "writer")]

    neutral = _parse_source(
        "src/elspeth/engine/neutral.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository\n"
        "def run(store: RunLifecycleRepository, coordination_token: CoordinationToken):\n"
        "    store.complete_run(run_id, status, coordination_token=coordination_token)\n",
    )
    assert [call.method for call in scan_production_calls([neutral])] == ["complete_run"]

    neutral_alias = _parse_source(
        "src/elspeth/engine/neutral_alias.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository\n"
        "def run(store: RunLifecycleRepository, coordination_token: CoordinationToken):\n"
        "    writer = store\n"
        "    writer.complete_run(run_id, status, coordination_token=coordination_token)\n",
    )
    assert [call.method for call in scan_production_calls([neutral_alias])] == ["complete_run"]

    constructed = _parse_source(
        "src/elspeth/engine/constructed.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository\n"
        "def run(engine, coordination_token: CoordinationToken):\n"
        "    RunLifecycleRepository(engine).complete_run(run_id, status, coordination_token=coordination_token)\n",
    )
    assert [call.method for call in scan_production_calls([constructed])] == ["complete_run"]

    name_only = _parse_source(
        "src/elspeth/engine/name_only.py",
        "def run(landscape_mutations):\n    landscape_mutations.complete_run(run_id, status)\n",
    )
    assert scan_production_calls([name_only]) == ()

    bound = _parse_source(
        "src/elspeth/engine/bound.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.mutations import LandscapeMutationCapability\n"
        "def run(repo, coordination_token: CoordinationToken):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=coordination_token)\n"
        "    capability.complete_run(status)\n",
    )
    assert [call.method for call in scan_production_calls([bound])] == ["complete_run"]
    assert _caller_authority_violations([bound]) == ()

    bound_by_factory = _parse_source(
        "src/elspeth/engine/bound_by_factory.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.mutations import bind_landscape_mutations\n"
        "def run(repo, coordination_token: CoordinationToken):\n"
        "    capability = bind_landscape_mutations(repo, coordination_token=coordination_token)\n"
        "    capability.complete_run(status)\n",
    )
    assert [call.method for call in scan_production_calls([bound_by_factory])] == ["complete_run"]
    assert _caller_authority_violations([bound_by_factory]) == ()

    shadow_wrapper = _parse_source(
        "src/elspeth/engine/shadow_wrapper.py",
        "class Shadow:\n"
        "    def complete_run(self, *args, **kwargs):\n"
        "        return self.shadow.complete_run(*args, **kwargs)\n"
        "def invoke(shadow):\n"
        "    shadow.complete_run()\n",
    )
    shadow_violations = _mutation_callable_escapes([shadow_wrapper])
    assert sum("unknown mutation receiver .complete_run" in item for item in shadow_violations) == 2

    local_type_shadow = _parse_source(
        "src/elspeth/engine/local_type_shadow.py",
        "class RunLifecycleRepository:\n    pass\ndef run(store: RunLifecycleRepository):\n    store.update_run_status(run_id, status)\n",
    )
    assert scan_production_calls([local_type_shadow]) == ()
    assert any("unknown mutation receiver .update_run_status" in item for item in _mutation_callable_escapes([local_type_shadow]))

    neutral_escape = _parse_source(
        "src/elspeth/engine/neutral_escape.py",
        "def run(store, wrapped):\n"
        "    store.update_run_status(run_id, status)\n"
        "    cast(object, wrapped).update_run_status(run_id, status)\n"
        "    store.release_seat(token=token, now=now)\n",
    )
    neutral_escapes = _mutation_callable_escapes([neutral_escape])
    assert any("unknown mutation receiver .update_run_status" in item for item in neutral_escapes)
    assert any("unknown mutation receiver .release_seat" in item for item in neutral_escapes)

    neutral_finalize_and_dynamic = _parse_source(
        "src/elspeth/engine/neutral_finalize.py",
        "def run(store, factory, method):\n    store.finalize()\n    getattr(factory.run_lifecycle, method)(run_id)\n",
    )
    dynamic_violations = _mutation_callable_escapes([neutral_finalize_and_dynamic])
    assert any("unknown mutation receiver .finalize" in item for item in dynamic_violations)
    assert any("non-literal Landscape getattr" in item for item in dynamic_violations)

    local_capability_import = _parse_source(
        "src/elspeth/engine/local_capability_import.py",
        "from attacker import LandscapeMutationCapability\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def victim(repo, token: CoordinationToken):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=token)\n"
        "    capability.complete_run(status)\n"
        "def unrelated():\n"
        "    from elspeth.core.landscape.mutations import LandscapeMutationCapability\n"
        "    return LandscapeMutationCapability\n",
    )
    assert scan_production_calls([local_capability_import]) == ()
    assert any("unknown mutation receiver .complete_run" in item for item in _mutation_callable_escapes([local_capability_import]))

    class_capability_import = _parse_source(
        "src/elspeth/engine/class_capability_import.py",
        "from attacker import LandscapeMutationCapability\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class ImportHolder:\n"
        "    from elspeth.core.landscape.mutations import LandscapeMutationCapability\n"
        "def victim(repo, token: CoordinationToken):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=token)\n"
        "    capability.complete_run(status)\n",
    )
    assert scan_production_calls([class_capability_import]) == ()

    receiver_dispatch_attacks = _parse_source(
        "src/elspeth/engine/receiver_dispatch_attacks.py",
        "import operator\n"
        "def run(factory, method, attacker):\n"
        "    getattr(factory.scheduler, method)(item)\n"
        "    factory.run_lifecycle.__getattribute__(method)(run_id)\n"
        "    operator.attrgetter('complete_run')(factory.run_lifecycle)(run_id)\n"
        "    operator.methodcaller('complete_run', run_id)(factory.run_lifecycle)\n"
        "    setattr(factory.run_lifecycle, 'complete_run', attacker)\n",
    )
    receiver_dispatch_violations = _mutation_callable_escapes([receiver_dispatch_attacks])
    assert any("non-literal Landscape getattr" in item for item in receiver_dispatch_violations)
    assert any("dynamic Landscape __getattribute__" in item for item in receiver_dispatch_violations)
    assert sum("operator mutation attribute dispatch" in item for item in receiver_dispatch_violations) == 2
    assert any("setattr mutation override" in item for item in receiver_dispatch_violations)

    internal_dynamic_wrapper = _parse_source(
        _RUN_LIFECYCLE_PATH,
        "class RunLifecycleRepository:\n"
        "    def shadow(self):\n"
        "        mutate = getattr(self, 'complete_run')\n"
        "        return mutate(run_id, status)\n"
        "    def helper(self, repo):\n"
        "        repo.release_seat(token=token, now=now)\n",
    )
    assert any("dynamic getattr('complete_run')" in item for item in _mutation_callable_escapes([internal_dynamic_wrapper]))
    assert any(edge.method == "release_seat" for edge in scan_internal_landscape_wrapper_edges([internal_dynamic_wrapper]))


def test_caller_authority_rejects_rebound_or_untyped_attribute_tokens() -> None:
    rebound = _parse_source(
        "src/elspeth/engine/rebound.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def run(factory, coordination_token: CoordinationToken):\n"
        "    coordination_token = stale_token\n"
        "    factory.run_lifecycle.complete_run(run_id, status, coordination_token=coordination_token)\n",
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([rebound]))

    attribute = _parse_source(
        "src/elspeth/engine/attribute.py",
        "def run(factory, holder):\n    factory.run_lifecycle.complete_run(run_id, status, coordination_token=holder.token)\n",
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([attribute]))

    rebound_capability = _parse_source(
        "src/elspeth/engine/rebound_capability.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.mutations import LandscapeMutationCapability\n"
        "def run(repo, coordination_token: CoordinationToken):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=coordination_token)\n"
        "    coordination_token = stale_token\n"
        "    capability.complete_run(status)\n",
    )
    assert [call.method for call in scan_production_calls([rebound_capability])] == ["complete_run"]
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([rebound_capability]))

    spoof = _parse_source(
        "src/elspeth/engine/spoof.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def LandscapeMutationCapability(repo, *, coordination_token):\n    return repo\n"
        "def run(repo, coordination_token: CoordinationToken):\n"
        "    landscape_mutations = LandscapeMutationCapability(repo, coordination_token=coordination_token)\n"
        "    landscape_mutations.complete_run(status)\n",
    )
    assert scan_production_calls([spoof]) == ()

    kwargs_escape = _parse_source(
        "src/elspeth/engine/capability_kwargs.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.mutations import LandscapeMutationCapability\n"
        "def run(repo, coordination_token: CoordinationToken, payload):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=coordination_token)\n"
        "    capability.complete_run(**payload)\n",
    )
    assert any("**kwargs" in item for item in _caller_authority_violations([kwargs_escape]))

    attacker_token = _parse_source(
        "src/elspeth/engine/attacker_token.py",
        "from attacker import CoordinationToken\n"
        "def run(factory, coordination_token: CoordinationToken):\n"
        "    factory.run_lifecycle.complete_run(run_id, status, coordination_token=coordination_token)\n",
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([attacker_token]))

    attacker_capability = _parse_source(
        "src/elspeth/engine/attacker_capability.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.attacker import LandscapeMutationCapability\n"
        "def run(repo, coordination_token: CoordinationToken):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=coordination_token)\n"
        "    capability.complete_run(status)\n",
    )
    assert scan_production_calls([attacker_capability]) == ()


def test_dml_scanner_resolves_aliases_bound_methods_dynamic_getattr_and_raw_sql() -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/aliased.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update as mutate
            from sqlalchemy import text
            from elspeth.core.landscape.schema import runs_table as audit_runs

            def writers(conn):
                conn.execute(mutate(audit_runs).values(status="failed"))
                remove = audit_runs.delete
                conn.execute(remove())
                conn.execute(getattr(audit_runs, "insert")().values(run_id="r"))
                conn.execute(text("UPDATE runs SET status='failed'"))
            """
        ),
    )
    sites = scan_dml_identities([unit])
    assert Counter((site.table, site.operation) for site in sites) == Counter(
        {
            ("runs", "update"): 1,
            ("runs", "delete"): 1,
            ("runs", "insert"): 1,
            ("runs", "raw-update"): 1,
        }
    )
    escapes = _dml_callable_escape_violations([unit])
    assert any("aliased DML import update as mutate" in item for item in escapes)
    assert any("DML callable alias/escape" in item for item in escapes)
    assert any("dynamic DML getattr('insert')" in item for item in escapes)
    assert any("raw SQL raw-update runs" in item for item in _unknown_or_raw_execution_violations([unit]))


def test_dml_scanner_respects_parameter_and_local_shadowing() -> None:
    shadowed = _parse_source(
        "src/elspeth/core/landscape/shadowed.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.core.landscape.schema import runs_table

            def parameters(conn, update, runs_table):
                conn.execute(update(runs_table))

            def locals(conn):
                update = custom_update
                runs_table = custom_table
                conn.execute(update(runs_table))
            """
        ),
    )
    assert scan_dml_identities([shadowed]) == ()


def test_dml_fingerprint_is_stable_when_the_required_fence_wraps_the_same_statement() -> None:
    unfenced = _parse_source(
        "src/elspeth/core/landscape/fillable.py",
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(*, coordination_token: CoordinationToken):\n"
        "    conn.execute(update(runs_table).where(runs_table.c.run_id == coordination_token.run_id).values(status='done'))\n",
    )
    fenced = _parse_source(
        unfenced.path,
        unfenced.source.replace(
            "    conn.execute(update(runs_table).where(runs_table.c.run_id == coordination_token.run_id).values(status='done'))\n",
            "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
            "        guarded.execute(update(runs_table).where(runs_table.c.run_id == coordination_token.run_id).values(status='done'))\n",
        ),
    )
    assert _canonical_digest(scan_dml_identities([unfenced])) == _canonical_digest(scan_dml_identities([fenced]))
    fenced_node = next(node for node in ast.walk(fenced.tree) if isinstance(node, ast.FunctionDef) and node.name == "write")
    assert _function_fence_violation(fenced_node) is None


def test_temporary_register_run_leader_wrapper_removal_is_internal_inventory_fillable() -> None:
    path = "src/elspeth/core/landscape/run_coordination_repository.py"
    with_wrapper = _parse_source(
        path,
        "class RunCoordinationRepository:\n"
        "    def register_run_leader(self, conn, run_id):\n"
        "        return self.register_run_leader_on(conn, run_id=run_id)\n"
        "    def register_run_leader_on(self, conn, *, run_id):\n"
        "        return None\n",
    )
    without_wrapper = _parse_source(
        path,
        "class RunCoordinationRepository:\n    def register_run_leader_on(self, conn, *, run_id):\n        return None\n",
    )
    assert scan_internal_landscape_wrapper_edges([with_wrapper]) == scan_internal_landscape_wrapper_edges([without_wrapper])
    temporary_wrapper = {"register_run_leader"}
    with_definitions = {
        symbol.rsplit(".", maxsplit=1)[-1]
        for candidate_path, symbol in _function_index([with_wrapper])
        if candidate_path == path and symbol.startswith("RunCoordinationRepository.")
    }
    without_definitions = {
        symbol.rsplit(".", maxsplit=1)[-1]
        for candidate_path, symbol in _function_index([without_wrapper])
        if candidate_path == path and symbol.startswith("RunCoordinationRepository.")
    }
    assert with_definitions - temporary_wrapper == without_definitions - temporary_wrapper
    assert _standalone_register_run_leader_definition_violation([with_wrapper]) is not None
    assert _standalone_register_run_leader_definition_violation([without_wrapper]) is None
    assert (
        path,
        "RunCoordinationRepository.register_run_leader",
    ) in _function_index([with_wrapper])
    assert (
        path,
        "RunCoordinationRepository.register_run_leader",
    ) not in _function_index([without_wrapper])


def test_raw_writable_cte_is_not_misclassified_as_a_read() -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/writable_cte.py",
        "from sqlalchemy import text\n"
        "def bypass(conn):\n"
        "    conn.execute(text('WITH changed AS (UPDATE runs SET status=\"failed\" RETURNING *) SELECT * FROM changed'))\n",
    )
    violations = _unknown_or_raw_execution_violations([unit])
    assert any("unclassified .execute statement" in item for item in violations)

    sqlalchemy_cte = _parse_source(
        "src/elspeth/core/landscape/sqlalchemy_writable_cte.py",
        "from sqlalchemy import select, update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def bypass(conn):\n"
        "    changed = update(runs_table).values(status='failed').returning(runs_table.c.run_id).cte()\n"
        "    conn.execute(select(changed))\n",
    )
    assert any("unclassified .execute statement" in item for item in _unknown_or_raw_execution_violations([sqlalchemy_cte]))


def test_raw_execution_scanner_rejects_alias_dynamic_explain_write_pragma_and_outside_core_dml() -> None:
    raw = _parse_source(
        "src/elspeth/core/landscape/raw_aliases.py",
        textwrap.dedent(
            """\
            def bypass(conn):
                driver = conn.exec_driver_sql
                driver("UPDATE runs SET status='failed'")
                getattr(conn, "exec_driver_sql")("EXPLAIN ANALYZE UPDATE runs SET status='failed'")
                conn.exec_driver_sql("PRAGMA user_version = 2")
            """
        ),
    )
    raw_violations = _unknown_or_raw_execution_violations([raw])
    assert len(raw_violations) == 3

    outside = _parse_source(
        "src/elspeth/web/outside_landscape_write.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.core.landscape.schema import runs_table

            def bypass(landscape_conn):
                landscape_conn.execute(update(runs_table).values(status="failed"))
            """
        ),
    )
    assert any("outside Landscape DML" in item for item in _raw_write_surface_violations([outside]))

    control = _parse_source(
        "src/elspeth/web/sessions/write.py",
        "from sqlalchemy import update\n"
        "from elspeth.web.sessions.schema import session_records_table\n"
        "def write(conn):\n    conn.execute(update(session_records_table))\n",
    )
    assert _raw_write_surface_violations([control]) == ()

    dynamic_outside = _parse_source(
        "src/elspeth/web/dynamic_outside.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def bypass(conn, operation):\n"
        "    getattr(conn, operation)(update(runs_table).values(status='failed'))\n",
    )
    assert any("outside Landscape DML" in item for item in _raw_write_surface_violations([dynamic_outside]))

    future_table = _parse_source(
        "src/elspeth/web/future_table.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import future_table\n"
        "def bypass(conn):\n"
        "    conn.execute(update(future_table).values(value='bad'))\n",
    )
    assert any("outside Landscape DML" in item for item in _raw_write_surface_violations([future_table]))

    dynamic_container = _parse_source(
        "src/elspeth/web/dynamic_container.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import future_table\n"
        "def bypass(conn):\n"
        "    ([conn.execute][0])(update(future_table))\n",
    )
    assert any("outside Landscape DML" in item for item in _raw_write_surface_violations([dynamic_container]))

    raw_future = _parse_source(
        "src/elspeth/web/raw_future.py",
        "def bypass(conn):\n    conn.exec_driver_sql('UPDATE future_landscape_table SET value=1')\n",
    )
    assert any("outside Landscape DML" in item for item in _raw_write_surface_violations([raw_future]))

    late_bound_table = _parse_source(
        "src/elspeth/core/landscape/late_bound.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(conn):\n    conn.scalar(update(tbl))\n"
        "tbl = runs_table\n",
    )
    assert [(site.table, site.operation) for site in scan_dml_identities([late_bound_table])] == [("runs", "update")]

    future_obfuscations = _parse_source(
        "src/elspeth/web/future_obfuscations.py",
        "from functools import partial\n"
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape import schema\n"
        "from elspeth.core.landscape.schema import future_table\n"
        "def bypass(conn, factory, table_name):\n"
        "    conn.execute(update(getattr(schema, 'future_table')))\n"
        "    conn.execute(update(getattr(schema, table_name)))\n"
        "    tables = [future_table]\n"
        "    conn.execute(update(tables[0]))\n"
        "    conn.exec_driver_sql('UP/**/DATE future_landscape_table SET value=1')\n"
        "    conn.exec_driver_sql('DROP TABLE future_landscape_table')\n"
        "    raw_update = 'UP' + 'DATE future_landscape_table SET value=2'\n"
        "    conn.exec_driver_sql(raw_update)\n"
        "    raw_ddl = 'CREATE TEMP TABLE future_landscape_table(value TEXT)'\n"
        "    conn.exec_driver_sql(raw_ddl)\n"
        "    getattr(factory, 'write_connection')()\n"
        "    method_name = 'write_connection'\n"
        "    getattr(factory, method_name)()\n"
        "    partial(conn.execute, update(future_table))()\n",
    )
    future_violations = _raw_write_surface_violations([future_obfuscations])
    assert sum("outside Landscape DML" in item for item in future_violations) >= 3
    assert any("write/DDL" in item for item in future_violations)
    assert sum("raw .write_connection" in item for item in future_violations) == 2
    assert sum("outside raw SQL write/DDL" in item for item in future_violations) >= 2


def test_transaction_scanner_rejects_nested_decoy_payload_before_fence_and_multi_caller_helper() -> None:
    decoys = _parse_source(
        "src/elspeth/core/landscape/decoys.py",
        textwrap.dedent(
            """\
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction

            def nested(conn, *, coordination_token: CoordinationToken):
                conn.execute(payload)
                def decoy():
                    with fenced_leader_transaction(engine, token=coordination_token) as conn:
                        conn.execute(payload)

            def late(conn, *, coordination_token: CoordinationToken):
                conn.execute(payload)
                with fenced_leader_transaction(engine, token=coordination_token) as conn:
                    conn.execute(payload)
            """
        ),
    )
    nodes = {node.name: node for node in ast.walk(decoys.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "contexts=0" in (_function_fence_violation(nodes["nested"]) or "")
    assert "first database effect" in (_function_fence_violation(nodes["late"]) or "")

    helpers = _parse_source(
        "src/elspeth/core/landscape/helpers.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
            from elspeth.core.landscape.schema import runs_table

            def helper(conn):
                conn.execute(update(runs_table).values(status="failed"))

            def first(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as conn:
                    helper(conn)

            def second(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as conn:
                    helper(conn)
            """
        ),
    )
    dml = scan_dml_identities([helpers])
    violations = _transaction_order_violations([helpers], dml)
    assert any("helper callers=2 expected=1" in item for item in violations)

    outside = _parse_source(
        "src/elspeth/core/landscape/outside.py",
        helpers.source.replace(
            "def second(*, coordination_token: CoordinationToken):\n    with fenced_leader_transaction(engine, token=coordination_token) as conn:\n        helper(conn)\n",
            "",
        ).replace(
            "with fenced_leader_transaction(engine, token=coordination_token) as conn:\n        helper(conn)",
            "helper(conn)\n    with fenced_leader_transaction(engine, token=coordination_token) as conn:\n        pass",
        ),
    )
    outside_violations = _transaction_order_violations([outside], scan_dml_identities([outside]))
    assert any("subordinate call escapes" in item for item in outside_violations)


def test_transaction_scanner_requires_context_order_exact_connection_and_semantic_run_binding() -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/order_repros.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
            from elspeth.core.landscape.schema import runs_table

            def context_argument(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(conn.execute(update(runs_table)), token=coordination_token) as guarded:
                    guarded.execute(update(runs_table))

            def context_keyword(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, trace=conn.execute(update(runs_table)), token=coordination_token) as guarded:
                    guarded.execute(update(runs_table))

            def scalar_before(*, coordination_token: CoordinationToken):
                conn.scalar(update(runs_table).returning(runs_table.c.run_id))
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    guarded.execute(update(runs_table))

            def wrong_connection(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    other_conn.execute(update(runs_table))

            def aliased_connection(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    alias = guarded
                    alias.execute(update(runs_table))

            def rebound_connection(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    guarded = other_conn
                    guarded.execute(update(runs_table))

            def rebound_token(*, coordination_token: CoordinationToken):
                coordination_token = stale_token
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    guarded.execute(update(runs_table))

            def bound_execute_alias(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    execute_payload = guarded.execute
                    execute_payload(update(runs_table))

            def scalar_other_connection(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    other_connection().scalar(update(runs_table).returning(runs_table.c.run_id))

            def attacker_fence(*, coordination_token: CoordinationToken):
                with fenced_write(engine, token=coordination_token) as guarded:
                    guarded.execute(update(runs_table))

            def rebound_run(run_id, *, coordination_token: CoordinationToken):
                coordination_token.run_id
                run_id = other_run_id
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    guarded.execute(update(runs_table).where(runs_table.c.run_id == run_id))

            def exact(run_id, *, coordination_token: CoordinationToken):
                if run_id != coordination_token.run_id:
                    raise ValueError("mismatch")
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    guarded.execute(update(runs_table).where(runs_table.c.run_id == run_id))
            """
        ),
    )
    nodes = {node.name: node for node in ast.walk(unit.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "exact connection" in (_function_fence_violation(nodes["context_argument"]) or "")
    assert "exact connection" in (_function_fence_violation(nodes["context_keyword"]) or "")
    assert "exact connection" in (_function_fence_violation(nodes["scalar_before"]) or "")
    assert "exact connection" in (_function_fence_violation(nodes["wrong_connection"]) or "")
    assert "exact connection" in (_function_fence_violation(nodes["aliased_connection"]) or "")
    assert "exact fenced connection is rebound" in (_function_fence_violation(nodes["rebound_connection"]) or "")
    assert "token parameter is rebound" in (_function_fence_violation(nodes["rebound_token"]) or "")
    assert "exact direct executions=0" in (_function_fence_violation(nodes["bound_execute_alias"]) or "")
    assert "exact connection" in (_function_fence_violation(nodes["scalar_other_connection"]) or "")
    assert any("execution callable alias/escape" in item for item in _dml_callable_escape_violations([unit]))
    assert "run_id" in (_function_fence_violation(nodes["rebound_run"]) or "")
    assert _function_fence_violation(nodes["exact"]) is None

    imported_attacker_fence = _parse_source(
        unit.path,
        "from elspeth.attacker import fenced_write\n" + unit.source,
    )
    attacker_node = next(
        node
        for node in ast.walk(imported_attacker_fence.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "attacker_fence"
    )
    assert "contexts=0" in (_function_fence_violation(attacker_node) or "")

    self_fence = _parse_source(
        unit.path,
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class Fake:\n"
        "    def fenced_write(self, *args, **kwargs):\n"
        "        return noop()\n"
        "    def write(self, *, coordination_token: CoordinationToken):\n"
        "        with self.fenced_write(engine, token=coordination_token) as guarded:\n"
        "            guarded.execute(update(runs_table))\n",
    )
    self_fence_node = next(node for node in ast.walk(self_fence.tree) if isinstance(node, ast.FunctionDef) and node.name == "write")
    assert "contexts=0" in (_function_fence_violation(self_fence_node) or "")

    fence_spoofs = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.scheduler.fencing import fenced_write\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def late_shadow(*, coordination_token: CoordinationToken):\n"
        "    with fenced_write(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table))\n"
        "fenced_write = attacker_fence\n",
    )
    late_shadow = next(node for node in ast.walk(fence_spoofs.tree) if isinstance(node, ast.FunctionDef) and node.name == "late_shadow")
    assert "contexts=0" in (_function_fence_violation(late_shadow) or "")

    nested_shadow = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.scheduler.fencing import fenced_write\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def owner(*, coordination_token: CoordinationToken):\n"
        "    def fenced_write(*args, **kwargs):\n        return attacker_fence(*args, **kwargs)\n"
        "    with fenced_write(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table))\n",
    )
    nested_owner = next(node for node in ast.walk(nested_shadow.tree) if isinstance(node, ast.FunctionDef) and node.name == "owner")
    assert "contexts=0" in (_function_fence_violation(nested_owner) or "")

    wildcard_shadow = _parse_source(
        unit.path,
        "from attacker import *\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def owner(*, coordination_token: CoordinationToken):\n"
        "    with fenced_write(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(payload)\n",
    )
    wildcard_owner = next(node for node in ast.walk(wildcard_shadow.tree) if isinstance(node, ast.FunctionDef) and node.name == "owner")
    assert "contexts=0" in (_function_fence_violation(wildcard_owner) or "")

    ordering_attacks = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def multi(*, coordination_token: CoordinationToken):\n"
        "    with helper(other_conn) as guarded, fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        pass\n"
        "def dynamic(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        pass\n"
        "    ([other_conn.execute][0])(update(runs_table))\n"
        "def decoy_guard(run_id, *, coordination_token: CoordinationToken):\n"
        "    if run_id != coordination_token.run_id:\n"
        "        def hidden():\n            raise ValueError('decoy')\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table))\n",
    )
    ordering_nodes = {
        node.name: node
        for node in ast.walk(ordering_attacks.tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"multi", "dynamic", "decoy_guard"}
    }
    assert "sole context manager" in (_function_fence_violation(ordering_nodes["multi"]) or "")
    assert "exact direct executions=0" in (_function_fence_violation(ordering_nodes["dynamic"]) or "")
    assert "run_id" in (_function_fence_violation(ordering_nodes["decoy_guard"]) or "")

    indirect_execution = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def before(*, coordination_token: CoordinationToken):\n"
        "    writers = [other_conn.execute]\n"
        "    writers[0](update(runs_table))\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n        pass\n"
        "def dynamic(method, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        getattr(other_conn, method)(update(runs_table))\n"
        "def closure(*, coordination_token: CoordinationToken):\n"
        "    payload = lambda: other_conn.execute(update(runs_table))\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n        payload()\n"
        "def mapped(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        list(map(other_conn.execute, [update(runs_table)]))\n"
        "def repeated(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        for _ in range(2):\n            guarded.execute(update(runs_table))\n",
    )
    indirect_nodes = {
        node.name: node
        for node in ast.walk(indirect_execution.tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"before", "dynamic", "closure", "mapped", "repeated"}
    }
    assert "exact direct executions=0" in (_function_fence_violation(indirect_nodes["before"]) or "")
    assert "exact direct executions=0" in (_function_fence_violation(indirect_nodes["dynamic"]) or "")
    assert "exact direct executions=0" in (_function_fence_violation(indirect_nodes["closure"]) or "")
    assert "exact direct executions=0" in (_function_fence_violation(indirect_nodes["mapped"]) or "")
    assert "runtime-repeating" in (_function_fence_violation(indirect_nodes["repeated"]) or "")
    assert len(_unknown_or_raw_execution_violations([indirect_execution])) >= 4

    local_fence_import = _parse_source(
        unit.path,
        "from attacker import fenced_write\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def victim(*, coordination_token: CoordinationToken):\n"
        "    with fenced_write(engine, token=coordination_token) as guarded:\n        guarded.execute(payload)\n"
        "def unrelated():\n"
        "    from elspeth.core.landscape.scheduler.fencing import fenced_write\n"
        "    return fenced_write\n",
    )
    fence_victim = next(node for node in ast.walk(local_fence_import.tree) if isinstance(node, ast.FunctionDef) and node.name == "victim")
    assert "contexts=0" in (_function_fence_violation(fence_victim) or "")

    hostile_execution_forms = _parse_source(
        unit.path,
        "import operator\n"
        "from functools import partial\n"
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def partial_write(*, coordination_token: CoordinationToken):\n"
        "    payload = partial(other_conn.execute, update(runs_table))\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n        payload()\n"
        "def dispatched(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        dispatch(other_conn.execute, update(runs_table))\n"
        "def methodcalled(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        operator.methodcaller('execute', update(runs_table))(other_conn)\n"
        "def conditional(flag, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        (guarded if flag else other_conn).execute(update(runs_table))\n"
        "def dynamic_index(index, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        writers = [guarded.execute, other_conn.execute]\n"
        "        writers[index](update(runs_table))\n"
        "def lambda_scalar(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        (lambda conn, statement: conn.scalar(statement))(other_conn, update(runs_table))\n"
        "def dead(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        if False:\n            guarded.execute(update(runs_table))\n"
        "def duplicate(*, coordination_token: CoordinationToken):\n"
        "    statement = update(runs_table)\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(statement)\n"
        "        guarded.execute(statement)\n",
    )
    hostile_nodes = {
        node.name: node
        for node in ast.walk(hostile_execution_forms.tree)
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {"partial_write", "dispatched", "methodcalled", "conditional", "dynamic_index", "lambda_scalar", "dead", "duplicate"}
    }
    for name in ("partial_write", "dispatched", "methodcalled", "dynamic_index", "lambda_scalar"):
        assert "exact direct executions=0" in (_function_fence_violation(hostile_nodes[name]) or "")
    assert "exact connection" in (_function_fence_violation(hostile_nodes["conditional"]) or "")
    assert "statically dead" in (_function_fence_violation(hostile_nodes["dead"]) or "")
    assert "exact direct executions=2" in (_function_fence_violation(hostile_nodes["duplicate"]) or "")

    closure_shadow = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def outer(token: CoordinationToken):\n"
        "    def inner(*, coordination_token: CoordinationToken):\n"
        "        with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "            guarded.execute(update(runs_table))\n"
        "    fenced_leader_transaction = attacker_fence\n"
        "    inner(coordination_token=token)\n",
    )
    closure_inner = next(node for node in ast.walk(closure_shadow.tree) if isinstance(node, ast.FunctionDef) and node.name == "inner")
    assert "contexts=0" in (_function_fence_violation(closure_inner) or "")

    class_fence_import = _parse_source(
        unit.path,
        "from attacker import fenced_write\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class ImportHolder:\n"
        "    from elspeth.core.landscape.scheduler.fencing import fenced_write\n"
        "def victim(*, coordination_token: CoordinationToken):\n"
        "    with fenced_write(engine, token=coordination_token) as guarded:\n        guarded.execute(payload)\n",
    )
    class_fence_victim = next(
        node for node in ast.walk(class_fence_import.tree) if isinstance(node, ast.FunctionDef) and node.name == "victim"
    )
    assert "contexts=0" in (_function_fence_violation(class_fence_victim) or "")

    alien_run_subject = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(other_run_id, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table).where(runs_table.c.run_id == other_run_id))\n",
    )
    alien_writer = next(node for node in ast.walk(alien_run_subject.tree) if isinstance(node, ast.FunctionDef))
    assert "non-token run-column subject" in (_function_fence_violation(alien_writer) or "")


def test_subordinate_helper_must_use_and_receive_the_exact_guarded_connection() -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/helper_connection.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
            from elspeth.core.landscape.schema import runs_table

            def helper(conn):
                other_conn.execute(update(runs_table))

            def owner(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    helper(external_conn)
            """
        ),
    )
    violations = _transaction_order_violations([unit], scan_dml_identities([unit]))
    assert any("exact caller-owned" in item or "exact helper connection" in item or "exact connection conn" in item for item in violations)

    rebound = _parse_source(
        unit.path,
        unit.source.replace(
            "other_conn.execute(update(runs_table))",
            "conn = other_conn\n    conn.execute(update(runs_table))",
        ).replace("helper(external_conn)", "helper(guarded)"),
    )
    assert any("conn parameter is rebound" in item for item in _transaction_order_violations([rebound], scan_dml_identities([rebound])))

    wrong_run_subject = _parse_source(
        unit.path,
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def helper(conn, run_id):\n"
        "    conn.execute(update(runs_table).where(runs_table.c.run_id == run_id))\n"
        "def owner(other_run_id, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        helper(guarded, other_run_id)\n",
    )
    wrong_run_violations = _transaction_order_violations([wrong_run_subject], scan_dml_identities([wrong_run_subject]))
    assert any("subordinate run subject is not exact caller token.run_id" in item for item in wrong_run_violations)


def test_subordinate_helper_resolution_rejects_duplicates_without_conflating_unrelated_terminals() -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/helper_identity.py",
        textwrap.dedent(
            """\
            from sqlalchemy import update
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
            from elspeth.core.landscape.schema import runs_table

            class Writer:
                @staticmethod
                def helper(conn):
                    conn.execute(update(runs_table))

            class Unrelated:
                @staticmethod
                def helper(value):
                    return value

            def owner(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as guarded:
                    Writer.helper(guarded)
                    Unrelated.helper(guarded)
            """
        ),
    )
    dml = scan_dml_identities([unit])
    edges = _subordinate_helper_edges([unit], dml)
    assert [(edge.helper_symbol, edge.caller_symbol) for edge in edges] == [("Writer.helper", "owner")]
    assert not any("callers=2" in item for item in _transaction_order_violations([unit], dml))

    duplicated = _parse_source(
        unit.path, unit.source.replace("Writer.helper(guarded)\n", "Writer.helper(guarded)\n        Writer.helper(guarded)\n")
    )
    duplicate_dml = scan_dml_identities([duplicated])
    assert len(_subordinate_helper_edges([duplicated], duplicate_dml)) == 2
    assert any("call sites=2 expected=1" in item for item in _transaction_order_violations([duplicated], duplicate_dml))

    aliased = _parse_source(
        unit.path,
        unit.source.replace(
            "Writer.helper(guarded)\n",
            "Writer.helper(guarded)\n        invoke_again = Writer.helper\n        invoke_again(guarded)\n",
        ),
    )
    aliased_dml = scan_dml_identities([aliased])
    assert len(_subordinate_helper_edges([aliased], aliased_dml)) == 2
    assert any("call sites=2 expected=1" in item for item in _transaction_order_violations([aliased], aliased_dml))

    hidden_duplicate = _parse_source(
        unit.path,
        unit.source.replace(
            "Writer.helper(guarded)\n",
            "Writer.helper(guarded)\n        ([Writer.helper][0])(guarded)\n",
        ),
    )
    hidden_dml = scan_dml_identities([hidden_duplicate])
    assert len(_subordinate_helper_edges([hidden_duplicate], hidden_dml)) == 2
    assert any("call sites=2 expected=1" in item for item in _transaction_order_violations([hidden_duplicate], hidden_dml))


def test_reopen_sql_model_rejects_indirect_raw_conditional_unreachable_and_dynamic_schema_writes() -> None:
    core = _parse_source(
        "src/elspeth/core/landscape/reopen_sql.py",
        "from functools import partial\n"
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import nodes_table, runs_table\n"
        "def raw_dispatch(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        dispatch(guarded.exec_driver_sql, 'UPDATE runs SET status=1')\n"
        "def raw_partial(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        partial(guarded.exec_driver_sql, 'UPDATE runs SET status=1')()\n"
        "def conditional(flag, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table) if flag else update(nodes_table))\n"
        "def unreachable(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        return None\n"
        "        guarded.execute(update(runs_table))\n"
        "def typing_dead(*, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        if TYPE_CHECKING:\n"
        "            guarded.execute(update(runs_table))\n",
    )
    nodes = {node.name: node for node in ast.walk(core.tree) if isinstance(node, ast.FunctionDef)}
    for name in ("raw_dispatch", "raw_partial"):
        assert "indirect callback" in (_function_fence_violation(nodes[name]) or "")
    assert "exact direct executions=0" in (_function_fence_violation(nodes["conditional"]) or "")
    assert "statically dead" in (_function_fence_violation(nodes["unreachable"]) or "")
    assert "statically dead" in (_function_fence_violation(nodes["typing_dead"]) or "")
    assert sum("indirect raw SQL write/DDL" in item for item in _unknown_or_raw_execution_violations([core])) >= 2

    outside = _parse_source(
        "src/elspeth/web/reopen_sql.py",
        "import operator\n"
        "from functools import partial\n"
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape import schema\n"
        "from elspeth.core.landscape.schema import future_table\n"
        "def bypass(conn, key, sql):\n"
        "    partial(conn.exec_driver_sql, 'UPDATE future_landscape_table SET value=1')()\n"
        "    operator.methodcaller('exec_driver_sql', 'DROP INDEX future_idx')(conn)\n"
        "    conn.exec_driver_sql('CREATE INDEX future_idx ON future_landscape_table(value)')\n"
        "    conn.exec_driver_sql('PRAGMA user_version = 4')\n"
        "    conn.exec_driver_sql(sql)\n"
        "    conn.execute(update(vars(schema)[key]))\n"
        "    conn.execute(update(schema.__dict__[key]))\n"
        "    dispatch(conn.execute, future_table.update())\n",
    )
    violations = _raw_write_surface_violations([outside])
    assert sum("raw SQL write/DDL" in item for item in violations) >= 4
    assert any("unknown raw SQL effect" in item for item in violations)
    assert sum("Landscape DML" in item for item in violations) >= 3


def test_reopen_subordinate_model_rejects_dynamic_duplicates_recursion_and_actual_run_subjects() -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/reopen_helpers.py",
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def helper(conn, *, subject):\n"
        "    conn.execute(update(runs_table).where(runs_table.c.run_id == subject))\n"
        "def owner(index, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        helper(guarded, subject=coordination_token.run_id)\n"
        "        callbacks = [helper, noop]\n"
        "        callbacks[index](guarded, subject=coordination_token.run_id)\n",
    )
    violations = _transaction_order_violations([unit], scan_dml_identities([unit]))
    assert any("call sites=2 expected=1" in item for item in violations)

    recursive = _parse_source(
        unit.path,
        unit.source.replace(
            "    conn.execute(update(runs_table).where(runs_table.c.run_id == subject))\n",
            "    conn.execute(update(runs_table).where(runs_table.c.run_id == subject))\n"
            "    if retry:\n"
            "        helper(conn, subject=subject)\n",
        ).replace(
            "        callbacks = [helper, noop]\n        callbacks[index](guarded, subject=coordination_token.run_id)\n",
            "",
        ),
    )
    recursive_violations = _transaction_order_violations([recursive], scan_dml_identities([recursive]))
    assert any("recursively invokes itself" in item for item in recursive_violations)

    global_subject = _parse_source(
        unit.path,
        unit.source.replace("def helper(conn, *, subject):", "def helper(conn):")
        .replace(" == subject", " == run_id")
        .replace("helper(guarded, subject=coordination_token.run_id)", "helper(guarded)")
        .replace(
            "        callbacks = [helper, noop]\n        callbacks[index](guarded, subject=coordination_token.run_id)\n",
            "",
        ),
    )
    global_violations = _transaction_order_violations([global_subject], scan_dml_identities([global_subject]))
    assert any("subordinate run subject is not exact caller token.run_id" in item for item in global_violations)

    rebound_subject = _parse_source(
        unit.path,
        unit.source.replace(
            "    conn.execute(update(runs_table).where(runs_table.c.run_id == subject))",
            "    subject = evil\n    conn.execute(update(runs_table).where(runs_table.c.run_id == subject))",
        ).replace(
            "        callbacks = [helper, noop]\n        callbacks[index](guarded, subject=coordination_token.run_id)\n",
            "",
        ),
    )
    rebound_violations = _transaction_order_violations([rebound_subject], scan_dml_identities([rebound_subject]))
    assert any("subordinate run subject is not exact caller token.run_id" in item for item in rebound_violations)


def test_authority_establishment_edges_require_exact_cardinality_and_subjects() -> None:
    duplicate_begin = _parse_source(
        "src/elspeth/engine/orchestrator/run_lifecycle.py",
        "class RunLifecycleCoordinator:\n"
        "    def initialize_database_phase(self, factory):\n"
        "        factory.run_lifecycle.begin_run()\n"
        "        factory.run_lifecycle.begin_run()\n",
    )
    valid_bedrock = _parse_source(
        "src/elspeth/web/_aws_ecs_acceptance/bedrock.py",
        "def run_bedrock_guardrails_live(repositories, run_id):\n    repositories.run_lifecycle.begin_run(run_id=run_id)\n",
    )
    begin_violations = _begin_run_production_call_violations([duplicate_begin, valid_bedrock])
    assert any("multiplicity drifted" in item for item in begin_violations)
    assert sum("explicit run_id" in item for item in begin_violations) == 2

    duplicate_acquire = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot):\n"
        "        snapshot.factory.run_coordination.acquire_run_leadership()\n"
        "        snapshot.factory.run_coordination.acquire_run_leadership()\n",
    )
    valid_admit = _parse_source(
        "src/elspeth/engine/orchestrator/join_admission.py",
        "class JoinAdmissionService:\n"
        "    def join_run(self, factory, run_id, worker_id, config_hash, window_seconds):\n"
        "        factory.run_coordination.admit_follower(run_id=run_id, worker_id=worker_id, "
        "config_hash=config_hash, window_seconds=window_seconds)\n",
    )
    coordination_violations = _coordination_caller_authority_violations([duplicate_acquire, valid_admit])
    assert any("calls=2 expected=1" in item for item in coordination_violations)
    assert sum("complete keyword-bound authority subject" in item for item in coordination_violations) == 2

    extra_argument = next(
        node
        for node in ast.walk(
            _parse_source(
                "src/elspeth/engine/orchestrator/resume.py",
                "def f(repo, snapshot, now, window_seconds):\n"
                "    repo.acquire_run_leadership(run_id=snapshot.run_id, worker_id=snapshot.worker_id, now=now, "
                "window_seconds=window_seconds, entry_point='resume', extra=True)\n",
            ).tree
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    assert "complete keyword-bound" in (_establishment_call_shape_violation("acquire_run_leadership", extra_argument) or "")

    evil_subject = next(
        node
        for node in ast.walk(
            _parse_source(
                "src/elspeth/engine/orchestrator/resume.py",
                "def f(repo, evil, window_seconds):\n"
                "    repo.acquire_run_leadership(run_id=evil.run_id, worker_id=evil.worker_id, "
                "window_seconds=window_seconds, entry_point='resume')\n",
            ).tree
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    assert "exact resume snapshot" in (_establishment_call_shape_violation("acquire_run_leadership", evil_subject) or "")

    coordination_definition = _parse_source(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "class RunCoordinationRepository:\n    def record_fence_refusal(self, *, run_id, worker_id, token):\n        pass\n",
    )
    subject_mismatch = _parse_source(
        "src/elspeth/engine/subject_mismatch.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def run(factory, other_run_id, other_worker_id, token: CoordinationToken):\n"
        "    factory.run_coordination.record_fence_refusal("
        "run_id=other_run_id, worker_id=other_worker_id, token=token)\n",
    )
    subject_violations = _coordination_caller_authority_violations([coordination_definition, subject_mismatch])
    assert any("run_id is not exact token.run_id" in item for item in subject_violations)

    rebound_snapshot = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot, window_seconds):\n"
        "        snapshot.run_id = evil\n"
        "        snapshot.factory.run_coordination.acquire_run_leadership("
        "run_id=snapshot.run_id, worker_id=snapshot.worker_id, window_seconds=window_seconds, entry_point='resume')\n",
    )
    rebound_config = _parse_source(
        "src/elspeth/engine/orchestrator/join_admission.py",
        "class JoinAdmissionService:\n"
        "    def join_run(self, factory, run_id, worker_id, config_hash, window_seconds):\n"
        "        config_hash = attacker_hash\n"
        "        factory.run_coordination.admit_follower("
        "run_id=run_id, worker_id=worker_id, config_hash=config_hash, window_seconds=window_seconds)\n",
    )
    rebound_subjects = _coordination_caller_authority_violations([rebound_snapshot, rebound_config])
    assert sum("authority-establishment subject is rebound" in item for item in rebound_subjects) == 2

    object_rebound_snapshot = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot, window_seconds):\n"
        "        object.__setattr__(snapshot, 'run_id', evil)\n"
        "        snapshot.factory.run_coordination.acquire_run_leadership("
        "run_id=snapshot.run_id, worker_id=snapshot.worker_id, window_seconds=window_seconds, entry_point='resume')\n",
    )
    assert any(
        "authority-establishment subject is rebound" in item
        for item in _coordination_caller_authority_violations([object_rebound_snapshot])
    )

    alias_rebound_snapshot = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot, window_seconds):\n"
        "        alias = snapshot\n"
        "        alias.run_id = evil\n"
        "        snapshot.factory.run_coordination.acquire_run_leadership("
        "run_id=snapshot.run_id, worker_id=snapshot.worker_id, window_seconds=window_seconds, entry_point='resume')\n",
    )
    assert any(
        "authority-establishment subject is rebound" in item for item in _coordination_caller_authority_violations([alias_rebound_snapshot])
    )

    repeating_establishment = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot, window_seconds):\n"
        "        for _ in range(2):\n"
        "            snapshot.factory.run_coordination.acquire_run_leadership("
        "run_id=snapshot.run_id, worker_id=snapshot.worker_id, window_seconds=window_seconds, entry_point='resume')\n",
    )
    assert any(
        "authority-establishment call is runtime-repeating" in item
        for item in _coordination_caller_authority_violations([repeating_establishment])
    )

    stale_internal_token = _parse_source(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class RunCoordinationRepository:\n"
        "    def wrapper(self, token: CoordinationToken):\n"
        "        self.release_seat(token=stale)\n",
    )
    assert any(
        "internal .release_seat lacks exact current token" in item
        for item in _internal_coordination_authority_violations([stale_internal_token])
    )

    clock_free_acquire = next(
        node
        for node in ast.walk(
            _parse_source(
                "src/elspeth/engine/orchestrator/resume.py",
                "def f(repo, snapshot, window_seconds):\n"
                "    repo.acquire_run_leadership(run_id=snapshot.run_id, worker_id=snapshot.worker_id, "
                "window_seconds=window_seconds, entry_point='resume')\n",
            ).tree
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    assert _establishment_call_shape_violation("acquire_run_leadership", clock_free_acquire) is None
    clock_owned_elsewhere = ast.Call(
        func=clock_free_acquire.func,
        args=clock_free_acquire.args,
        keywords=(*clock_free_acquire.keywords, ast.keyword(arg="now", value=ast.Name(id="now"))),
    )
    assert _establishment_call_shape_violation("acquire_run_leadership", clock_owned_elsewhere) is None

    live_units = list(_production_units())
    live_index = next(index for index, candidate in enumerate(live_units) if candidate.path == _RUN_LIFECYCLE_PATH)
    live_lifecycle = live_units[live_index]
    worker_assignment = "        worker_id = leader_worker_id or mint_worker_id(run.run_id)\n"
    assert live_lifecycle.source.count(worker_assignment) == 1
    live_units[live_index] = _parse_source(
        live_lifecycle.path,
        live_lifecycle.source.replace(
            worker_assignment,
            worker_assignment + "        worker_id = attacker_worker_id\n",
        ),
    )
    assert any(
        "begin_run -> register_run_leader_on worker_id subject is rebound" in item for item in _begin_run_edge_violations(live_units)
    )


def test_reopen_authority_provenance_rejects_mutated_tokens_providers_dynamic_dispatch_and_subjects() -> None:
    token_attacks = _parse_source(
        "src/elspeth/engine/reopen_authority.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "def imported(factory, token: CoordinationToken):\n"
        "    from attacker import stale as token\n"
        "    factory.run_lifecycle.complete_run(token.run_id, status, coordination_token=token)\n"
        "def object_mutated(factory, token: CoordinationToken):\n"
        "    alias = token\n"
        "    object.__setattr__(alias, 'run_id', evil)\n"
        "    factory.run_lifecycle.complete_run(token.run_id, status, coordination_token=token)\n"
        "@factory.run_lifecycle.complete_run(run_id, status, coordination_token=stale)\n"
        "def decorated(token: CoordinationToken):\n"
        "    pass\n"
        "def defaulted(token: CoordinationToken, value=factory.run_lifecycle.complete_run(run_id, status, coordination_token=stale)):\n"
        "    pass\n",
    )
    caller_violations = _caller_authority_violations([token_attacks])
    assert len(caller_violations) == 4

    fence_mutation = _parse_source(
        "src/elspeth/core/landscape/reopen_fence.py",
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.scheduler import fencing\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "setattr(fencing, 'fenced_write', attacker)\n"
        "def write(*, coordination_token: CoordinationToken):\n"
        "    with fencing.fenced_write(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table).where(runs_table.c.run_id == coordination_token.run_id))\n",
    )
    fence_writer = next(node for node in ast.walk(fence_mutation.tree) if isinstance(node, ast.FunctionDef))
    assert "contexts=0" in (_function_fence_violation(fence_writer) or "")

    capability_mutation = _parse_source(
        "src/elspeth/engine/reopen_capability.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape import mutations\n"
        "setattr(mutations, 'LandscapeMutationCapability', attacker)\n"
        "def run(repo, token: CoordinationToken):\n"
        "    capability = mutations.LandscapeMutationCapability(repo, coordination_token=token)\n"
        "    capability.complete_run(token.run_id, status)\n",
    )
    assert _caller_authority_violations([capability_mutation]) or _mutation_callable_escapes([capability_mutation])

    dynamic = _parse_source(
        "src/elspeth/engine/reopen_dynamic.py",
        "import operator\n"
        "from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository\n"
        "def attack(factory, method):\n"
        "    fetch = getattr\n"
        "    fetch(factory.run_lifecycle, method)(payload)\n"
        "    operator.methodcaller(method, payload)(factory.run_lifecycle)\n"
        "    object.__getattribute__(factory.run_lifecycle, method)(payload)\n"
        "    vars(type(factory.run_lifecycle))[method](factory.run_lifecycle, payload)\n"
        "    RunLifecycleRepository.__dict__['complete_run'](factory.run_lifecycle, payload)\n"
        "def neutral(store, method):\n"
        "    getattr(store, method)(payload)\n"
        "    operator.methodcaller(method, payload)(store)\n"
        "    object.__getattribute__(store, method)(payload)\n",
    )
    dynamic_violations = _mutation_callable_escapes([dynamic])
    assert len(dynamic_violations) >= 5
    assert not any("neutral" in item for item in dynamic_violations)

    direct_subjects = _parse_source(
        "src/elspeth/core/landscape/reopen_subjects.py",
        "from sqlalchemy import update\n"
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def local(*, coordination_token: CoordinationToken):\n"
        "    run_id = evil\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table).where(runs_table.c.run_id == run_id))\n"
        "def arbitrary(foreign_id, *, coordination_token: CoordinationToken):\n"
        "    with fenced_leader_transaction(engine, token=coordination_token) as guarded:\n"
        "        guarded.execute(update(runs_table).where(runs_table.c.run_id == foreign_id))\n",
    )
    direct_nodes = {node.name: node for node in ast.walk(direct_subjects.tree) if isinstance(node, ast.FunctionDef)}
    assert "non-token run-column subject" in (_function_fence_violation(direct_nodes["local"]) or "")
    assert "non-token run-column subject" in (_function_fence_violation(direct_nodes["arbitrary"]) or "")

    internal = _parse_source(
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "class RunCoordinationRepository:\n"
        "    def record_fence_refusal(self, *, run_id, worker_id, token):\n"
        "        pass\n"
        "    def wrapper(self, token: CoordinationToken):\n"
        "        self.record_fence_refusal(run_id=evil_run, worker_id=evil_worker, token=token)\n",
    )
    internal_violations = _internal_coordination_authority_violations([internal])
    assert any("run_id is not exact token.run_id" in item for item in internal_violations)


def test_reopen_establishment_model_rejects_dead_and_mapping_rebound_subjects() -> None:
    dead = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot, window_seconds):\n"
        "        if False:\n"
        "            snapshot.factory.run_coordination.acquire_run_leadership("
        "run_id=snapshot.run_id, worker_id=snapshot.worker_id, window_seconds=window_seconds, entry_point='resume')\n",
    )
    assert any("statically unreachable" in item for item in _coordination_caller_authority_violations([dead]))

    mapping_rebound = _parse_source(
        "src/elspeth/engine/orchestrator/resume.py",
        "class ResumeCoordinator:\n"
        "    def _acquire_resume_leadership(self, snapshot, window_seconds):\n"
        "        snapshot.__dict__['run_id'] = evil\n"
        "        snapshot.factory.run_coordination.acquire_run_leadership("
        "run_id=snapshot.run_id, worker_id=snapshot.worker_id, window_seconds=window_seconds, entry_point='resume')\n",
    )
    assert any("subject is rebound" in item for item in _coordination_caller_authority_violations([mapping_rebound]))


def test_authority_establishment_exception_is_exact_and_non_release() -> None:
    assert _AUTHORITY_ESTABLISHMENT_EXCEPTIONS == (_FRESH_EPOCH_ONE_EXCEPTION,)
    assert tuple(item.classification for item in _AUTHORITY_ESTABLISHMENTS) == (
        "fresh-run-epoch-1-creation",
        "existing-run-leadership-claim",
        "follower-membership-admission",
    )
    assert sum(item.temporary for item in _AUTHORITY_ESTABLISHMENTS) == 1
    exception = _FRESH_EPOCH_ONE_EXCEPTION
    assert exception.classification == "fresh-run-epoch-1-creation"
    assert exception.caller_symbol == "RunLifecycleRepository.begin_run"
    assert exception.callee_symbol == "RunCoordinationRepository.register_run_leader_on"
    assert exception.sunset is not None
    assert "Task 8B" in exception.sunset and "non-release" in exception.sunset
    assert not any(
        "*" in value or value.endswith(".")
        for value in (exception.caller_path, exception.caller_symbol, exception.callee_path, exception.callee_symbol)
    )
    assert exception.write_counts == (
        ("run_attributions", "insert", 1),
        ("run_coordination", "insert", 1),
        ("run_coordination_events", "insert", 2),
        ("run_web_plugin_policy", "insert", 1),
        ("run_workers", "insert", 1),
        ("runs", "insert", 1),
    )

    units = _production_units()
    dml = scan_dml_identities(units)
    for establishment in _AUTHORITY_ESTABLISHMENTS:
        assert _establishment_live_write_counts(establishment, units, dml) == Counter(
            {(table, operation): count for table, operation, count in establishment.write_counts}
        )


def test_landscape_mutation_api_inventory_is_literal_complete_and_cardinality_one() -> None:
    units = _production_units()
    assert len(_MUTATION_APIS) == 90
    assert Counter(api.category for api in _MUTATION_APIS) == Counter(_EXPECTED_API_CATEGORY_COUNTS)
    assert len({(api.path, api.symbol) for api in _MUTATION_APIS}) == len(_MUTATION_APIS)

    definitions = _find_api_definitions(units)
    drift = [f"{path}:{symbol} definitions={len(nodes)}" for (path, symbol), nodes in definitions.items() if len(nodes) != 1]
    assert not drift, _format_violations("Landscape mutation API definition drift", drift)

    coordination_path = "src/elspeth/core/landscape/run_coordination_repository.py"
    index = _function_index(units)
    coordination_definitions = {
        symbol.rsplit(".", maxsplit=1)[-1]
        for path, symbol in index
        if path == coordination_path
        and symbol.startswith("RunCoordinationRepository.")
        and symbol.rsplit(".", maxsplit=1)[-1] in _COORDINATION_MUTATION_METHOD_NAMES
    }
    temporary_wrapper = {"register_run_leader"}
    assert coordination_definitions - temporary_wrapper == _COORDINATION_MUTATION_METHOD_NAMES - temporary_wrapper
    assert coordination_definitions <= _COORDINATION_MUTATION_METHOD_NAMES


def test_landscape_dml_identity_and_write_set_are_frozen() -> None:
    dml = scan_dml_identities(_production_units())
    actual_digest = _canonical_digest(dml)
    actual_write_set = frozenset((site.table, site.operation) for site in dml)
    assert (
        len(dml),
        actual_digest,
        actual_write_set,
    ) == (
        _EXPECTED_DML_COUNT,
        _EXPECTED_DML_INVENTORY_SHA256,
        _EXPECTED_DML_WRITE_SET,
    ), (
        "Landscape DML inventory drift. A DML identity was added, removed, moved, duplicated, or replaced.\n"
        f"expected count/digest={_EXPECTED_DML_COUNT}/{_EXPECTED_DML_INVENTORY_SHA256}\n"
        f"actual count/digest={len(dml)}/{actual_digest}\n"
        f"added write shapes={sorted(actual_write_set - _EXPECTED_DML_WRITE_SET)!r}\n"
        f"removed write shapes={sorted(_EXPECTED_DML_WRITE_SET - actual_write_set)!r}\n"
        + "\n".join(
            f"  {site.path}:{site.line} {site.symbol} {site.operation} {site.table} fp={site.fingerprint}#{site.ordinal}"
            for site in dml[:160]
        )
    )


def test_landscape_production_caller_set_is_frozen() -> None:
    units = _production_units()
    calls = scan_production_calls(units)
    actual_digest = _canonical_digest(calls)
    assert (len(calls), actual_digest) == (
        _EXPECTED_CALL_COUNT,
        _EXPECTED_PRODUCTION_CALLER_SHA256,
    ), (
        "Landscape mutation caller inventory drift. A caller was added, removed, moved, aliased, or replaced.\n"
        f"expected count/digest={_EXPECTED_CALL_COUNT}/{_EXPECTED_PRODUCTION_CALLER_SHA256}\n"
        f"actual count/digest={len(calls)}/{actual_digest}\n"
        + "\n".join(f"  {site.path}:{site.line} {site.symbol} {site.receiver}.{site.method}#{site.ordinal}" for site in calls[:260])
    )

    assert sum(call.method in {"register_candidate", "register_verified_candidate", "bind_winner"} for call in calls) == 3

    coordination_calls = scan_coordination_production_calls(units)
    assert (len(coordination_calls), _canonical_digest(coordination_calls)) == (
        _EXPECTED_COORDINATION_CALL_COUNT,
        _EXPECTED_COORDINATION_CALL_SHA256,
    ), (
        "Run-coordination/worker production caller inventory drift.\n"
        f"expected={_EXPECTED_COORDINATION_CALL_COUNT}/{_EXPECTED_COORDINATION_CALL_SHA256}\n"
        f"actual={len(coordination_calls)}/{_canonical_digest(coordination_calls)}"
    )

    internal_edges = scan_internal_landscape_wrapper_edges(units)
    assert (len(internal_edges), _canonical_digest(internal_edges)) == (
        _EXPECTED_INTERNAL_EDGE_COUNT,
        _EXPECTED_INTERNAL_EDGE_SHA256,
    ), (
        "Internal Landscape facade/subrepository edge inventory drift.\n"
        f"expected={_EXPECTED_INTERNAL_EDGE_COUNT}/{_EXPECTED_INTERNAL_EDGE_SHA256}\n"
        f"actual={len(internal_edges)}/{_canonical_digest(internal_edges)}"
    )

    violations = (*_caller_authority_violations(units), *_coordination_caller_authority_violations(units))
    assert not violations, _format_violations(
        "Every Landscape production caller must forward one exact token and exact token.run_id",
        violations,
    )


def test_every_landscape_mutation_api_requires_current_typed_authority() -> None:
    violations = _api_authority_violations(_production_units())
    assert not violations, _format_violations(
        "Every normal Landscape mutation API must require a non-optional current CoordinationToken",
        violations,
    )


def test_every_landscape_dml_transaction_is_full_token_fenced_first() -> None:
    units = _production_units()
    dml = scan_dml_identities(units)
    violations = _transaction_order_violations(units, dml)
    assert not violations, _format_violations(
        "Every Landscape DML owner must fence before payload SQL; raw-Connection helpers need one exact fenced caller",
        violations,
    )

    edges = _subordinate_helper_edges(units, dml)
    assert (len(edges), _canonical_digest(edges)) == (
        _EXPECTED_SUBORDINATE_EDGE_COUNT,
        _EXPECTED_SUBORDINATE_EDGE_SHA256,
    ), (
        "Landscape subordinate Connection-helper edge inventory drift.\n"
        f"expected={_EXPECTED_SUBORDINATE_EDGE_COUNT}/{_EXPECTED_SUBORDINATE_EDGE_SHA256}\n"
        f"actual={len(edges)}/{_canonical_digest(edges)}\n"
        + "\n".join(f"  {edge.helper_path}:{edge.helper_symbol} -> {edge.caller_path}:{edge.caller_symbol}" for edge in edges)
    )


def test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists() -> None:
    units = _production_units()
    violations = (
        *_mutation_callable_escapes(units),
        *_internal_coordination_authority_violations(units),
        *_dml_callable_escape_violations(units),
        *_unknown_or_raw_execution_violations(units),
        *_raw_write_surface_violations(units),
        *_cross_database_violations(units),
    )
    assert not violations, _format_violations("Landscape mutation authority escape", violations)


def test_epoch_one_creation_edge_is_the_only_temporary_authority_exception() -> None:
    violations = _begin_run_edge_violations(_production_units())
    assert not violations, _format_violations("Task 8B epoch-one creation exception drift", violations)
