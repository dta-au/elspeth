"""Fail-closed gate for Landscape database-clock authority.

Task 6 moves every lease, expiry, takeover, checkpoint, and stale-owner
decision into the Landscape database's clock domain.  This test intentionally
starts RED.  Its scanner is self-tested against common indirection tricks so a
process clock cannot be hidden behind an alias, wrapper, ``getattr``, injected
callable, or ``**kwargs`` forwarding.

Audit timestamps may remain forensic facts.  They may not be compared with an
authority deadline or forwarded as the time used by an authority decision.
Sessions and Landscape clocks are deliberately separate domains; neither
database's timestamp may cross the adapter boundary into the other.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[4]

# These verbs decide custody, liveness, expiry, takeover, or stale-owner
# eligibility.  A ``now`` supplied by their caller is therefore authority, not
# a harmless audit timestamp.
_CLOCK_AUTHORITY_VERBS = frozenset(
    {
        "_acquire_run_leadership_on",
        "_recover_expired_leases",
        "acquire_lease",
        "acquire_run_leadership",
        "admit_follower",
        "claim_pending_sink",
        "claim_preparation",
        "claim_ready",
        "claim_ready_row",
        "dead_non_leader_workers",
        "depart_worker",
        "evict_worker",
        "fenced_leader_transaction",
        "fenced_write",
        "heartbeat_lease",
        "live_leader",
        "peer_active_leases",
        "recover_expired_leases",
        "register_run_leader",
        "register_run_leader_on",
        "release_seat",
        "takeover_expired",
        "verify_and_extend_leader_fence",
        "worker_heartbeat",
    }
)

_SENSITIVE_SYMBOLS = _CLOCK_AUTHORITY_VERBS | {
    "CheckpointManager._fenced_or_plain_write",
    "CheckpointManager.delete_checkpoints",
    "FollowerProcessor._best_effort_depart",
    "FollowerProcessor._drain_loop",
    "FollowerProcessor.__init__",
    "JoinAdmissionService.join_run",
    "RunHeartbeatThread.__init__",
    "RunHeartbeatThread._beat_once",
    "SinkEffectFinalization._validate_effect_authority",
    "SinkEffectLifecycle.complete_member_result",
    "SinkEffectLifecycle.mark_response_lost",
    "check_run_status_resumable",
}

_PROCESS_CLOCKS = frozenset(
    {
        "date.today",
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcnow",
        "datetime.now",
        "datetime.today",
        "datetime.utcnow",
        "elspeth.core.landscape._helpers.now",
        "time.monotonic",
        "time.time",
    }
)
_FORENSIC_NAMES = frozenset({"created_at", "occurred_at", "recorded_at", "timestamp", "forensic_timestamp"})
_AUTHORITY_DEADLINES = frozenset(
    {
        "available_at",
        "barrier_blocked_at",
        "heartbeat_expires_at",
        "leader_heartbeat_expires_at",
        "lease_expires_at",
    }
)
_AUTHORITY_TABLE_NAMES = frozenset(
    {
        "run_coordination_table",
        "run_workers_table",
        "sink_effects_table",
        "token_work_items_table",
    }
)
_CALLER_CLOCK_MARKERS = ("clock", "cutoff", "deadline", "expire", "now", "time", "wall")
_DATABASE_CLOCK_MARKERS = ("current_timestamp", "database_now", "database_time", "transaction_timestamp")
_DURATION_MARKERS = ("budget", "duration", "grace", "interval", "seconds", "timeout", "ttl", "window")
_NON_CLOCK_CONTEXT_PARAMETERS = frozenset({"cls", "conn", "engine", "repo", "repository", "row", "self", "token"})
_IDENTITY_PARAMETER_MARKERS = ("context", "generation", "owner", "role", "token", "worker")
_NEUTRAL_ABSOLUTE_NAMES = frozenset({"epoch_seconds", "marker", "moment", "observed", "timeout_at"})
_AUTHORITY_SCOPE_PREFIXES = (
    "src/elspeth/core/checkpoint/",
    "src/elspeth/core/landscape/",
    "src/elspeth/engine/orchestrator/",
)
_CLOCK_BOUNDARY_DIGEST = "a8c603ffcfbb77610fb244c53e16130f1baac12be8a81f814753d87c0d1fcb19"


def _name_has_clock_marker(name: str) -> bool:
    lowered = name.lower()
    tokens = tuple(filter(None, re.split(r"[^a-z0-9]+|_", lowered)))
    return (
        lowered.endswith(("_at", "_timestamp"))
        or any(token in {"clock", "cutoff", "deadline", "expiry", "now", "timestamp", "wall"} for token in tokens)
        or any(token.startswith("expire") for token in tokens)
        or "epoch_seconds" in lowered
    )


def _is_positive_seconds_modifier(node: ast.expr, name: str = "window_seconds") -> bool:
    return (
        isinstance(node, ast.JoinedStr)
        and len(node.values) == 3
        and isinstance(node.values[0], ast.Constant)
        and node.values[0].value == "+"
        and isinstance(node.values[1], ast.FormattedValue)
        and isinstance(node.values[1].value, ast.Name)
        and node.values[1].value.id == name
        and isinstance(node.values[2], ast.Constant)
        and node.values[2].value == " seconds"
    )


@dataclass(frozen=True, order=True)
class ClockViolation:
    path: str
    symbol: str
    line: int
    kind: str
    detail: str


@dataclass(frozen=True, order=True)
class ClockBoundary:
    path: str
    symbol: str
    identity_fingerprint: str


@dataclass(frozen=True)
class _FunctionRecord:
    path: str
    symbol: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    called_names: frozenset[str]
    called_qualified: frozenset[str]
    return_called_names: frozenset[str]
    return_called_qualified: frozenset[str]
    imports: frozenset[tuple[str, str]]


# Closed identity set for the current authority surface.  Task 6 may change
# annotations and remove ``now``, but moving, duplicating, or adding an
# authority definition requires an explicit review of this gate.
_REVIEWED_CLOCK_BOUNDARY_IDENTITIES = frozenset(
    {
        ("src/elspeth/core/checkpoint/manager.py", "CheckpointManager._fenced_or_plain_write"),
        ("src/elspeth/core/checkpoint/manager.py", "CheckpointManager.create_checkpoint"),
        ("src/elspeth/core/checkpoint/manager.py", "CheckpointManager.delete_checkpoints"),
        ("src/elspeth/core/checkpoint/recovery.py", "RecoveryManager.can_resume"),
        ("src/elspeth/core/checkpoint/recovery.py", "RecoveryManager.get_resume_point"),
        ("src/elspeth/core/checkpoint/recovery.py", "check_run_status_resumable"),
        ("src/elspeth/core/landscape/data_flow/tokens.py", "RowTokenRepository.create_row_with_token"),
        ("src/elspeth/core/landscape/execution/sink_effect_finalization.py", "SinkEffectFinalization._finalize_on"),
        ("src/elspeth/core/landscape/execution/sink_effect_finalization.py", "SinkEffectFinalization._validate_effect_authority"),
        ("src/elspeth/core/landscape/execution/sink_effect_finalization.py", "SinkEffectFinalization.finalize"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.acquire_lease"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.claim_preparation"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.complete_member_result"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.complete_plan"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.heartbeat_lease"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.mark_response_lost"),
        ("src/elspeth/core/landscape/execution/sink_effect_lifecycle.py", "SinkEffectLifecycle.takeover_expired"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.acquire_lease"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.claim_preparation"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.heartbeat_lease"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.takeover_expired"),
        ("src/elspeth/core/landscape/execution/source_completion_recovery.py", "SourceCompletionReconciler.reconcile"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository._acquire_run_leadership_on"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository._insert_worker_row"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_run_leadership"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.admit_follower"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.dead_non_leader_workers"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.evict_worker"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.live_leader"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.register_run_leader"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.register_run_leader_on"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.release_seat"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.worker_heartbeat"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "fenced_leader_transaction"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "verify_and_extend_leader_fence"),
        ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.complete_run"),
        ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.finalize_run"),
        ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.update_run_status"),
        ("src/elspeth/core/landscape/scheduler/fencing.py", "fenced_write"),
        ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository._terminalize_consumed_barrier_rows"),
        ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository._transition_passthrough_pending_sink"),
        ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository.complete_barrier"),
        ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository.mark_blocked_barrier_pending_sink_many"),
        ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository.mark_blocked_barrier_terminal"),
        ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository.adopt_blocked_barrier_item"),
        ("src/elspeth/core/landscape/scheduler/branch_losses.py", "CoalesceBranchLossRepository.adopt_coalesce_branch_losses"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository._transition"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository._transition_on"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository._transition_with_ready_children"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_blocked"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_failed"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_failed_with_ready_children"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_pending_sink"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_pending_sink_terminal"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_pending_sink_terminal_many"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_pending_sink_with_ready_children"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_terminal"),
        ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository.mark_terminal_with_ready_children"),
        (
            "src/elspeth/core/landscape/scheduler/dispositions.py",
            "SchedulerDispositionRepository.terminalize_pending_sinks_with_terminal_outcomes",
        ),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository._recover_expired_leases"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.claim_pending_sink"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.claim_ready"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.claim_ready_row"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.heartbeat_lease"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.peer_active_leases"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.recover_expired_leases"),
        ("src/elspeth/core/landscape/scheduler/leases.py", "SchedulerLeaseRepository.recover_expired_leases_legacy_unfenced"),
        ("src/elspeth/core/landscape/scheduler/queue.py", "SchedulerQueueRepository.ingest_row_with_initial_claim"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.claim_pending_sink"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.claim_ready"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.heartbeat_lease"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.recover_expired_leases"),
        ("src/elspeth/engine/orchestrator/resume.py", "ResumeCoordinator.resume"),
    }
)

# This is the external authority API contract, not the discovery allowlist.
# Structural discovery below independently finds any function that compares or
# mutates custody/lease fields, then follows its local callers.  These entries
# merely keep delegation-only facades/protocols visible when their bodies have
# no direct SQL decision.
_REQUIRED_AUTHORITY_PUBLIC_SURFACE = frozenset(
    {
        ("src/elspeth/core/checkpoint/manager.py", "CheckpointManager._fenced_or_plain_write"),
        ("src/elspeth/core/checkpoint/manager.py", "CheckpointManager.delete_checkpoints"),
        ("src/elspeth/core/checkpoint/recovery.py", "check_run_status_resumable"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.acquire_lease"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.claim_preparation"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.heartbeat_lease"),
        ("src/elspeth/core/landscape/execution/sink_effects.py", "SinkEffectRepository.takeover_expired"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_run_leadership"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.admit_follower"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.live_leader"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.worker_heartbeat"),
        ("src/elspeth/core/landscape/scheduler/fencing.py", "fenced_write"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.claim_pending_sink"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.claim_ready"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.heartbeat_lease"),
        ("src/elspeth/core/landscape/scheduler_repository.py", "TokenSchedulerRepository.recover_expired_leases"),
    }
)


def _assigned_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.List, ast.Tuple)):
        return {name for item in node.elts for name in _assigned_names(item)}
    return set()


def _identifier_names(node: ast.AST) -> set[str]:
    return {
        candidate.attr if isinstance(candidate, ast.Attribute) else candidate.id
        for candidate in ast.walk(node)
        if isinstance(candidate, (ast.Attribute, ast.Name))
    }


def _mapping_alias_root(name: str, assignments: dict[str, ast.expr]) -> str:
    """Return the shared mapping object name behind a chain of simple aliases."""

    cursor = name
    seen: set[str] = set()
    while cursor not in seen:
        seen.add(cursor)
        value = assignments.get(cursor)
        if not isinstance(value, ast.Name) or value.id not in assignments:
            break
        cursor = value.id
    return cursor


def _scoped_nodes(node: ast.AST) -> tuple[ast.AST, ...]:
    nodes: list[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def generic_visit(self, candidate: ast.AST) -> None:
            nodes.append(candidate)
            super().generic_visit(candidate)

        def visit_FunctionDef(self, candidate: ast.FunctionDef) -> None:
            nodes.append(candidate)
            if candidate is node:
                super().generic_visit(candidate)

        def visit_AsyncFunctionDef(self, candidate: ast.AsyncFunctionDef) -> None:
            nodes.append(candidate)
            if candidate is node:
                super().generic_visit(candidate)

        def visit_Lambda(self, candidate: ast.Lambda) -> None:
            nodes.append(candidate)

    Visitor().visit(node)
    return tuple(nodes)


def _has_authority_ordering_comparison(
    node: ast.AST,
    imports: Iterable[tuple[str, str]] = (),
) -> bool:
    assignments = _local_assignments(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else {}
    assignments.update(
        {
            local: ast.Name(id=origin.rsplit(".", 1)[-1], ctx=ast.Load())
            for local, origin in imports
            if origin.rsplit(".", 1)[-1] in {"ge", "gt", "le", "lt", "text"}
        }
    )

    def comparison_uses_deadline(candidate: ast.Compare) -> bool:
        expressions = (candidate.left, *candidate.comparators)
        return bool(
            set().union(*(_identifier_names(_resolved_expression(expression, assignments)) for expression in expressions))
            & _AUTHORITY_DEADLINES
        )

    return any(
        (
            isinstance(candidate, ast.Compare)
            and any(isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for operator in candidate.ops)
            and comparison_uses_deadline(candidate)
        )
        or (isinstance(candidate, ast.Call) and _sqlalchemy_ordering_callable(candidate.func, assignments, candidate.args))
        for candidate in _scoped_nodes(node)
    )


def _local_assignments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    assignments: dict[str, ast.expr] = {}

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, candidate: ast.FunctionDef) -> None:
            if candidate is node:
                for statement in candidate.body:
                    self.visit(statement)

        def visit_AsyncFunctionDef(self, candidate: ast.AsyncFunctionDef) -> None:
            if candidate is node:
                for statement in candidate.body:
                    self.visit(statement)

        def visit_Assign(self, candidate: ast.Assign) -> None:
            for target in candidate.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = candidate.value
                elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    mapping_name = _mapping_alias_root(target.value.id, assignments)
                    target_key = _resolved_mapping_key(target.slice, assignments)
                    if target_key is None:
                        assignments[mapping_name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
                        continue
                    existing = _mapping_items(assignments.get(mapping_name, ast.Dict(keys=[], values=[])), assignments)
                    merged = dict(existing or ())
                    merged[target_key] = candidate.value
                    assignments[mapping_name] = ast.Dict(
                        keys=[ast.Constant(key) for key in merged],
                        values=list(merged.values()),
                    )
            self.generic_visit(candidate.value)

        def visit_AnnAssign(self, candidate: ast.AnnAssign) -> None:
            if isinstance(candidate.target, ast.Name) and candidate.value is not None:
                assignments[candidate.target.id] = candidate.value
                self.generic_visit(candidate.value)

        def visit_AugAssign(self, candidate: ast.AugAssign) -> None:
            if isinstance(candidate.op, ast.BitOr) and isinstance(candidate.target, ast.Name):
                mapping_name = _mapping_alias_root(candidate.target.id, assignments)
                existing = _mapping_items(assignments.get(mapping_name, ast.Dict(keys=[], values=[])), assignments)
                incoming = _mapping_items(candidate.value, assignments)
                if existing is not None and incoming is not None:
                    merged = dict(existing)
                    merged.update(incoming)
                    assignments[mapping_name] = ast.Dict(
                        keys=[ast.Constant(key) for key in merged],
                        values=list(merged.values()),
                    )
                else:
                    assignments[mapping_name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
            self.generic_visit(candidate.value)

        def visit_Expr(self, candidate: ast.Expr) -> None:
            call = candidate.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "update"
                and isinstance(call.func.value, ast.Name)
                and len(call.args) <= 1
            ):
                mapping_name = _mapping_alias_root(call.func.value.id, assignments)
                incoming = _mapping_items(call.args[0], assignments) if call.args else ()
                incoming_items = list(incoming or ())
                if incoming is not None:
                    for keyword in call.keywords:
                        if keyword.arg is not None:
                            incoming_items.append((keyword.arg, keyword.value))
                        else:
                            expanded = _mapping_items(keyword.value, assignments)
                            if expanded is None:
                                incoming = None
                                break
                            incoming_items.extend(expanded)
                existing = _mapping_items(assignments.get(mapping_name, ast.Dict(keys=[], values=[])), assignments)
                if incoming is not None and existing is not None:
                    merged = dict(existing)
                    merged.update(incoming_items)
                    assignments[mapping_name] = ast.Dict(
                        keys=[ast.Constant(key) for key in merged],
                        values=list(merged.values()),
                    )
            elif (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "setdefault"
                and isinstance(call.func.value, ast.Name)
                and len(call.args) in {1, 2}
                and not call.keywords
            ):
                mapping_name = _mapping_alias_root(call.func.value.id, assignments)
                key = _resolved_mapping_key(call.args[0], assignments)
                value = call.args[1] if len(call.args) == 2 else ast.Constant(None)
                existing = _mapping_items(assignments.get(mapping_name, ast.Dict(keys=[], values=[])), assignments)
                if key is not None and existing is not None:
                    merged = dict(existing)
                    merged.setdefault(key, value)
                    assignments[mapping_name] = ast.Dict(
                        keys=[ast.Constant(item_key) for item_key in merged],
                        values=list(merged.values()),
                    )
                else:
                    assignments[mapping_name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
            self.generic_visit(candidate)

    Visitor().visit(node)
    return assignments


def _mapping_items(
    node: ast.expr,
    assignments: dict[str, ast.expr],
    *,
    seen: frozenset[str] = frozenset(),
) -> tuple[tuple[str, ast.expr], ...] | None:
    if isinstance(node, ast.Name) and node.id not in seen:
        value = assignments.get(node.id)
        if value is not None:
            return _mapping_items(value, assignments, seen=seen | {node.id})
        return None
    if isinstance(node, ast.Dict):
        items: list[tuple[str, ast.expr]] = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                nested = _mapping_items(value, assignments, seen=seen)
                if nested is None:
                    return None
                items.extend(nested)
                continue
            resolved_key = _resolved_mapping_key(key, assignments)
            if resolved_key is None:
                return None
            items.append((resolved_key, value))
        return tuple(items)
    if isinstance(node, ast.Call) and _terminal_name(node.func) == "dict":
        if node.args:
            return None
        return tuple((keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg is not None)
    return None


def _deadline_values(call: ast.Call, assignments: dict[str, ast.expr]) -> tuple[ast.expr, ...]:
    values = [keyword.value for keyword in call.keywords if keyword.arg in _AUTHORITY_DEADLINES]
    for argument in call.args:
        items = _mapping_items(argument, assignments)
        if items is not None:
            values.extend(value for key, value in items if key in _AUTHORITY_DEADLINES)
    for keyword in call.keywords:
        if keyword.arg is None:
            items = _mapping_items(keyword.value, assignments)
            if items is not None:
                values.extend(value for key, value in items if key in _AUTHORITY_DEADLINES)
    return tuple(values)


def _targets_authority_table(node: ast.AST, assignments: dict[str, ast.expr] | None = None) -> bool:
    bindings = assignments or {}
    return any(
        isinstance(candidate, ast.Call)
        and _resolved_terminal(candidate.func, bindings) in {"insert", "update"}
        and candidate.args
        and _terminal_name(_resolved_expression(candidate.args[0], bindings)) in _AUTHORITY_TABLE_NAMES
        for candidate in ast.walk(node)
    )


def _resolved_expression(node: ast.expr, assignments: dict[str, ast.expr], *, seen: frozenset[str] = frozenset()) -> ast.expr:
    if isinstance(node, ast.Name) and node.id not in seen and node.id in assignments:
        return _resolved_expression(assignments[node.id], assignments, seen=seen | {node.id})
    return node


def _resolved_mapping_key(node: ast.expr, assignments: dict[str, ast.expr]) -> str | None:
    resolved = _resolved_expression(node, assignments)
    if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
        return resolved.value
    if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Add):
        left = _resolved_mapping_key(resolved.left, assignments)
        right = _resolved_mapping_key(resolved.right, assignments)
        return left + right if left is not None and right is not None else None
    if isinstance(resolved, ast.Attribute):
        return resolved.attr
    return None


def _normalized_callable(
    node: ast.expr,
    assignments: dict[str, ast.expr],
    *,
    seen: frozenset[str] = frozenset(),
) -> ast.expr:
    if isinstance(node, ast.Name) and node.id not in seen and node.id in assignments:
        return _normalized_callable(assignments[node.id], assignments, seen=seen | {node.id})
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in assignments:
        receiver = node.value
        receiver_seen: set[str] = set()
        while receiver.id not in receiver_seen and isinstance(assignments.get(receiver.id), ast.Name):
            receiver_seen.add(receiver.id)
            receiver = assignments[receiver.id]
        if receiver is not node.value:
            return ast.Attribute(value=receiver, attr=node.attr, ctx=ast.Load())
    if isinstance(node, ast.Subscript):
        key = _resolved_mapping_key(node.slice, assignments)
        container = _resolved_expression(node.value, assignments)
        if key is not None and isinstance(container, ast.Dict):
            items = _mapping_items(container, assignments)
            if items is not None and key in dict(items):
                return _normalized_callable(dict(items)[key], assignments, seen=seen)
        if key is not None and isinstance(container, ast.Call) and _terminal_name(container.func) == "vars" and container.args:
            return ast.Attribute(value=container.args[0], attr=key, ctx=ast.Load())
    if isinstance(node, ast.Call) and _terminal_name(node.func) in {"getattr", "__getattribute__"}:
        method_getattribute = isinstance(node.func, ast.Attribute) and node.func.attr == "__getattribute__"
        receiver = node.func.value if method_getattribute else node.args[0] if node.args else None
        key_node = node.args[0] if method_getattribute and node.args else node.args[1] if len(node.args) >= 2 else None
        attribute = _resolved_mapping_key(key_node, assignments) if key_node is not None else None
        if receiver is not None and attribute is not None:
            return ast.Attribute(value=receiver, attr=attribute, ctx=ast.Load())
    if isinstance(node, ast.Call) and _terminal_name(_normalized_callable(node.func, assignments, seen=seen)) == "partial" and node.args:
        return _normalized_callable(node.args[0], assignments, seen=seen)
    return node


def _bound_callable_arguments(node: ast.expr, assignments: dict[str, ast.expr]) -> tuple[ast.expr, ...]:
    resolved = _resolved_expression(node, assignments)
    if isinstance(resolved, ast.Call) and _terminal_name(_normalized_callable(resolved.func, assignments)) == "partial" and resolved.args:
        return (*_bound_callable_arguments(resolved.args[0], assignments), *resolved.args[1:])
    return ()


def _sqlalchemy_ordering_callable(
    node: ast.expr,
    assignments: dict[str, ast.expr],
    arguments: Sequence[ast.expr] = (),
) -> bool:
    def is_deadline(receiver: ast.expr) -> bool:
        return bool(_identifier_names(_resolved_expression(receiver, assignments)) & _AUTHORITY_DEADLINES)

    resolved = _normalized_callable(node, assignments)
    all_arguments = (*_bound_callable_arguments(node, assignments), *arguments)
    if isinstance(resolved, ast.Attribute) and resolved.attr in {"__ge__", "__gt__", "__le__", "__lt__", "between"}:
        return is_deadline(resolved.value)
    if (
        isinstance(resolved, ast.Attribute)
        and _attribute_chain(resolved) in {"operator.ge", "operator.gt", "operator.le", "operator.lt"}
        and all_arguments
    ):
        return is_deadline(all_arguments[0])
    if isinstance(resolved, ast.Call):
        factory = _normalized_callable(resolved.func, assignments)
        return (
            isinstance(factory, ast.Attribute)
            and factory.attr == "op"
            and is_deadline(factory.value)
            and len(resolved.args) == 1
            and isinstance(resolved.args[0], ast.Constant)
            and resolved.args[0].value in {"<", "<=", ">", ">="}
        )
    return False


def _static_sql_text(node: ast.expr, assignments: dict[str, ast.expr]) -> str | None:
    resolved = _resolved_expression(node, assignments)
    if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
        return resolved.value
    if isinstance(resolved, ast.JoinedStr):
        rendered: list[str] = []
        for part in resolved.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                rendered.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                formatted = _resolved_expression(part.value, assignments)
                if isinstance(formatted, ast.Constant) and isinstance(formatted.value, str):
                    rendered.append(formatted.value)
                else:
                    rendered.append(ast.unparse(part.value))
        return "".join(rendered)
    if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Add):
        left = _static_sql_text(resolved.left, assignments)
        right = _static_sql_text(resolved.right, assignments)
        return left + right if left is not None and right is not None else None
    if isinstance(resolved, ast.Call) and (_terminal_name(resolved.func) or "").endswith("text") and len(resolved.args) == 1:
        return _static_sql_text(resolved.args[0], assignments)
    if (
        isinstance(resolved, ast.Call)
        and isinstance(resolved.func, ast.Attribute)
        and resolved.func.attr == "join"
        and len(resolved.args) == 1
        and isinstance(resolved.args[0], (ast.List, ast.Tuple))
    ):
        separator = _static_sql_text(resolved.func.value, assignments)
        parts = [_static_sql_text(item, assignments) for item in resolved.args[0].elts]
        if separator is not None and all(part is not None for part in parts):
            return separator.join(part for part in parts if part is not None)
    return None


def _sql_code_only(statement: str) -> str:
    """Remove SQL literals/comments before recognizing authority syntax."""

    return re.sub(
        r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/",
        " ",
        statement,
        flags=re.DOTALL,
    )


def _resolved_terminal(node: ast.expr, assignments: dict[str, ast.expr]) -> str | None:
    return _terminal_name(_normalized_callable(node, assignments))


def _raw_sql_deadline_mutation(node: ast.Call, assignments: dict[str, ast.expr] | None = None) -> bool:
    bindings = assignments or {}
    terminal = _resolved_terminal(node.func, bindings)
    if terminal not in {"exec_driver_sql", "execute"} or not node.args:
        return False
    statement_node = _resolved_expression(node.args[0], bindings)
    statement = _static_sql_text(statement_node, bindings)
    if statement is None:
        return False
    rendered = _sql_code_only(statement).lower()
    dynamic_interpolation = isinstance(statement_node, ast.JoinedStr) and any(
        isinstance(part, ast.FormattedValue)
        and not (isinstance((resolved := _resolved_expression(part.value, bindings)), ast.Constant) and isinstance(resolved.value, str))
        for part in statement_node.values
    )
    authority_table = any(table.removesuffix("_table") in rendered for table in _AUTHORITY_TABLE_NAMES)
    write_mutation = bool(re.search(r"\b(?:insert|merge|replace|update|upsert)\b", rendered))
    deadline_decision = authority_table and (
        bool(re.search(r"\bdelete\b", rendered)) or bool(re.search(r"\bselect\b.*\bwhere\b", rendered, flags=re.DOTALL))
    )
    return (write_mutation or deadline_decision) and (
        any(deadline in rendered for deadline in _AUTHORITY_DEADLINES) or (dynamic_interpolation and authority_table)
    )


def _raw_sql_database_clock(node: ast.Call, assignments: dict[str, ast.expr]) -> bool:
    if not node.args:
        return False
    statement = _static_sql_text(node.args[0], assignments)
    return statement is not None and bool(re.search(r"\b(?:current_timestamp|transaction_timestamp)\b", _sql_code_only(statement).lower()))


def _is_sqlalchemy_statement_expression(node: ast.expr, assignments: dict[str, ast.expr]) -> bool:
    cursor = _resolved_expression(node, assignments)
    while isinstance(cursor, ast.Call) and isinstance(cursor.func, ast.Attribute) and isinstance(cursor.func.value, ast.Call):
        cursor = cursor.func.value
    return isinstance(cursor, ast.Call) and _resolved_terminal(cursor.func, assignments) in {
        "delete",
        "insert",
        "select",
        "update",
    }


def _has_authority_deadline_write(
    node: ast.AST,
    imports: Iterable[tuple[str, str]] = (),
) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    assignments = _local_assignments(node)
    assignments.update(
        {
            local: ast.Name(id=origin.rsplit(".", 1)[-1], ctx=ast.Load())
            for local, origin in imports
            if origin.rsplit(".", 1)[-1] in {"ge", "gt", "le", "lt", "text"}
        }
    )
    for candidate in _scoped_nodes(node):
        if not isinstance(candidate, ast.Call):
            continue
        if _raw_sql_deadline_mutation(candidate, assignments):
            return True
        if _resolved_terminal(candidate.func, assignments) in {"exec_driver_sql", "execute"} and candidate.args:
            statement = _static_sql_text(candidate.args[0], assignments)
            if statement is None and any(
                _name_has_clock_marker(name)
                for argument in (*candidate.args[1:], *(keyword.value for keyword in candidate.keywords))
                for name in _identifier_names(argument)
            ):
                return True
        if _terminal_name(candidate.func) != "values":
            continue
        if _deadline_values(candidate, assignments):
            return True
        unresolved_mapping = any(_mapping_items(argument, assignments) is None for argument in candidate.args) or any(
            keyword.arg is None and _mapping_items(keyword.value, assignments) is None for keyword in candidate.keywords
        )
        if unresolved_mapping and _targets_authority_table(candidate, assignments):
            return True
    return False


def _is_direct_authority_boundary(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    imports: Iterable[tuple[str, str]] = (),
) -> bool:
    """Recognize authority by what it decides/writes, never by its name."""

    return _has_authority_ordering_comparison(node, imports) or _has_authority_deadline_write(node, imports)


def _function_records(sources: dict[str, str]) -> tuple[_FunctionRecord, ...]:
    records: list[_FunctionRecord] = []
    for path, source in sorted(sources.items()):
        tree = ast.parse(source, filename=path)
        imports: dict[str, str] = {}
        for candidate in ast.walk(tree):
            if isinstance(candidate, ast.Import):
                for alias in candidate.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(candidate, ast.ImportFrom):
                imported_module = _relative_import_module(path, candidate)
                for alias in candidate.names:
                    imports[alias.asname or alias.name] = f"{imported_module}.{alias.name}"
        object_types: dict[str, str] = {}
        for candidate in ast.walk(tree):
            if not isinstance(candidate, ast.Assign) or not isinstance(candidate.value, ast.Call):
                continue
            object_type: str | None = None
            if isinstance(candidate.value.func, ast.Name) and candidate.value.func.id in imports:
                object_type = imports[candidate.value.func.id]
            elif (
                _terminal_name(candidate.value.func) == "import_module"
                and candidate.value.args
                and isinstance(candidate.value.args[0], ast.Constant)
                and isinstance(candidate.value.args[0].value, str)
            ):
                object_type = candidate.value.args[0].value
            if object_type is not None:
                for target in candidate.targets:
                    if isinstance(target, ast.Name):
                        object_types[target.id] = object_type
        module = _module_name(path)
        local_classes = {candidate.name for candidate in tree.body if isinstance(candidate, ast.ClassDef)}
        if module is not None:
            for candidate in ast.walk(tree):
                if (
                    isinstance(candidate, ast.Assign)
                    and isinstance(candidate.value, ast.Call)
                    and isinstance(candidate.value.func, ast.Name)
                    and candidate.value.func.id in local_classes
                ):
                    for target in candidate.targets:
                        if isinstance(target, ast.Name):
                            object_types[target.id] = f"{module}.{candidate.value.func.id}"
        callable_aliases: dict[str, str] = {}
        module_assignments = [candidate for candidate in tree.body if isinstance(candidate, (ast.Assign, ast.AnnAssign))]

        def callable_reference(
            value: ast.expr,
            source_imports: dict[str, str] = imports,
            source_aliases: dict[str, str] = callable_aliases,
        ) -> str | None:
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Call)
                and callable_reference(value.value.func) == "importlib.import_module"
                and value.value.args
                and isinstance(value.value.args[0], ast.Constant)
                and isinstance(value.value.args[0].value, str)
            ):
                return f"{value.value.args[0].value}.{value.attr}"
            chain = _attribute_chain(value)
            if chain is not None:
                head, *tail = chain.split(".")
                origin = source_imports.get(head) or source_aliases.get(head)
                if origin is not None:
                    return ".".join((origin, *tail))
            if isinstance(value, ast.Call) and value.args:
                called = callable_reference(value.func)
                if called == "functools.partial":
                    return callable_reference(value.args[0])
                if (
                    _terminal_name(value.func) == "getattr"
                    and len(value.args) >= 2
                    and isinstance(value.args[1], ast.Constant)
                    and isinstance(value.args[1].value, str)
                ):
                    receiver = callable_reference(value.args[0])
                    if receiver is not None:
                        return f"{receiver}.{value.args[1].value}"
            return None

        changed = True
        while changed:
            changed = False
            for candidate in module_assignments:
                targets = candidate.targets if isinstance(candidate, ast.Assign) else (candidate.target,)
                value = candidate.value
                if value is None or (reference := callable_reference(value)) is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and callable_aliases.get(target.id) != reference:
                        callable_aliases[target.id] = reference
                        changed = True

        instance_callable_aliases: dict[tuple[str, str], str] = {}
        for class_node in (candidate for candidate in tree.body if isinstance(candidate, ast.ClassDef)):
            class_assignments: dict[str, ast.expr] = {}
            for method in class_node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_assignments.update(_local_assignments(method))
            for candidate in ast.walk(class_node):
                targets: tuple[ast.expr, ...] = ()
                value: ast.expr | None = None
                if isinstance(candidate, ast.Assign):
                    targets = tuple(candidate.targets)
                    value = candidate.value
                elif isinstance(candidate, ast.AnnAssign):
                    targets = (candidate.target,)
                    value = candidate.value
                elif (
                    isinstance(candidate, ast.Call)
                    and _terminal_name(candidate.func) in {"setattr", "__setattr__"}
                    and len(candidate.args) >= 3
                    and isinstance(candidate.args[0], ast.Name)
                    and candidate.args[0].id == "self"
                ):
                    attribute = _resolved_mapping_key(candidate.args[1], class_assignments)
                    if attribute is None:
                        continue
                    targets = (ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()), attr=attribute, ctx=ast.Store()),)
                    value = candidate.args[2]
                if value is None or (reference := callable_reference(value)) is None:
                    continue
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and isinstance(target.value.value, ast.Name)
                        and target.value.value.id == "self"
                        and target.value.attr == "__dict__"
                        and (attribute := _resolved_mapping_key(target.slice, class_assignments)) is not None
                    ):
                        target = ast.Attribute(value=ast.Name(id="self", ctx=ast.Load()), attr=attribute, ctx=ast.Store())
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                        instance_callable_aliases[(class_node.name, target.attr)] = reference

        class Visitor(ast.NodeVisitor):
            def __init__(
                self,
                source_path: str,
                source_imports: dict[str, str],
                source_object_types: dict[str, str],
                source_callable_aliases: dict[str, str],
                source_instance_callable_aliases: dict[tuple[str, str], str],
                source_module: str | None,
                source_local_classes: set[str],
            ) -> None:
                self.source_path = source_path
                self.source_imports = source_imports
                self.source_object_types = source_object_types
                self.source_callable_aliases = source_callable_aliases
                self.source_instance_callable_aliases = source_instance_callable_aliases
                self.source_module = source_module
                self.source_local_classes = source_local_classes
                self.stack: list[str] = []

            def qualified_call(self, node: ast.expr) -> str | None:
                chain = _attribute_chain(node)
                if chain is None:
                    return None
                head, *tail = chain.split(".")
                imported = self.source_imports.get(head) or self.source_object_types.get(head) or self.source_callable_aliases.get(head)
                if imported is not None:
                    return ".".join((imported, *(tail or (["__call__"] if head in self.source_object_types else []))))
                if self.source_module is None:
                    return None
                if head == "self" and len(self.stack) > 1:
                    instance_origin = self.source_instance_callable_aliases.get((self.stack[0], tail[0])) if tail else None
                    if instance_origin is not None:
                        return ".".join((instance_origin, *tail[1:]))
                    return ".".join((self.source_module, *self.stack[:-1], *tail))
                if head in self.source_local_classes or not tail:
                    return ".".join((self.source_module, head, *tail))
                return None

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                self.stack.append(node.name)
                symbol = ".".join(self.stack)
                called = frozenset(
                    name
                    for candidate in ast.walk(node)
                    if isinstance(candidate, ast.Call)
                    if (name := _terminal_name(candidate.func)) is not None
                )
                assignments = _local_assignments(node)
                qualified = frozenset(
                    reference
                    for candidate in ast.walk(node)
                    if isinstance(candidate, ast.Call)
                    if (
                        reference := self.qualified_call(
                            assignments.get(candidate.func.id, candidate.func) if isinstance(candidate.func, ast.Name) else candidate.func
                        )
                    )
                    is not None
                )
                returned_names: set[str] = set()
                returned_qualified: set[str] = set()

                def collect_returned(expression: ast.AST, seen: frozenset[str] = frozenset()) -> None:
                    if isinstance(expression, ast.Name) and expression.id not in seen and expression.id in assignments:
                        collect_returned(assignments[expression.id], seen | {expression.id})
                    if isinstance(expression, ast.expr):
                        reference = self.qualified_call(expression)
                        if reference is not None:
                            returned_qualified.add(reference)
                    if isinstance(expression, ast.Call):
                        if isinstance(expression.func, ast.Name) and expression.func.id in assignments:
                            collect_returned(assignments[expression.func.id], seen | {expression.func.id})
                        terminal = _terminal_name(expression.func)
                        if terminal is not None:
                            returned_names.add(terminal)
                        called_reference = self.qualified_call(expression.func)
                        if called_reference is not None:
                            returned_qualified.add(called_reference)
                    for child in ast.iter_child_nodes(expression):
                        collect_returned(child, seen)

                class ReturnVisitor(ast.NodeVisitor):
                    def visit_FunctionDef(self, candidate: ast.FunctionDef) -> None:
                        if candidate is node:
                            self.generic_visit(candidate)

                    def visit_AsyncFunctionDef(self, candidate: ast.AsyncFunctionDef) -> None:
                        if candidate is node:
                            self.generic_visit(candidate)

                    def visit_Return(self, candidate: ast.Return) -> None:
                        if candidate.value is not None:
                            collect_returned(candidate.value)

                ReturnVisitor().visit(node)
                records.append(
                    _FunctionRecord(
                        self.source_path,
                        symbol,
                        node,
                        called,
                        qualified,
                        frozenset(returned_names),
                        frozenset(returned_qualified),
                        frozenset(self.source_imports.items()),
                    )
                )
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

        Visitor(path, imports, object_types, callable_aliases, instance_callable_aliases, module, local_classes).visit(tree)
    return tuple(records)


def _module_name(path: str) -> str | None:
    if not path.startswith("src/") or not path.endswith(".py"):
        return None
    module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return module.removesuffix(".__init__")


def _missing_required_boundaries(sources: dict[str, str]) -> frozenset[tuple[str, str]]:
    actual = {(record.path, record.symbol) for record in _function_records(sources)}
    required_in_scope = {identity for identity in _REQUIRED_AUTHORITY_PUBLIC_SURFACE if identity[0] in sources}
    return frozenset(required_in_scope - actual)


def _discover_authority_boundaries(sources: dict[str, str]) -> tuple[ClockBoundary, ...]:
    records = _function_records(sources)
    record_identities = {(record.path, record.symbol) for record in records}
    identities = {
        (record.path, record.symbol)
        for record in records
        if (not record.path.startswith("src/") or record.path.startswith(_AUTHORITY_SCOPE_PREFIXES))
        and _is_direct_authority_boundary(record.node, record.imports)
    }
    identities.update(identity for identity in _REQUIRED_AUTHORITY_PUBLIC_SURFACE if identity in record_identities)

    # Pull in delegation-only wrappers from authority implementation scopes.
    # The seed names come from structural evidence/public API, not an allowed
    # verb list, so a newly named wrapper cannot hide a decision.
    changed = True
    while changed:
        changed = False
        authority_references = {f"{module}.{symbol}" for path, symbol in identities if (module := _module_name(path)) is not None}
        for record in records:
            identity = (record.path, record.symbol)
            if identity in identities or not record.path.startswith(_AUTHORITY_SCOPE_PREFIXES):
                continue
            if record.called_qualified & authority_references:
                identities.add(identity)
                changed = True

    return tuple(
        ClockBoundary(path, symbol, hashlib.sha256(f"{path}:{symbol}".encode()).hexdigest()[:16]) for path, symbol in sorted(identities)
    )


def _record_returns_inline_database_clock(record: _FunctionRecord) -> bool:
    for returned in _record_return_values(record):
        for call in (child for child in _reachable_expression_nodes(returned) if isinstance(child, ast.Call)):
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "func"
                and call.func.attr in {"current_timestamp", "transaction_timestamp"}
                and not call.args
                and not call.keywords
                and dict(record.imports).get("func") in {None, "sqlalchemy.func"}
                and not any(
                    parameter.arg == "func"
                    for parameter in (*record.node.args.posonlyargs, *record.node.args.args, *record.node.args.kwonlyargs)
                )
            ) or (
                _terminal_name(call.func) == "read_landscape_transaction_time"
                and (dict(record.imports).get("read_landscape_transaction_time") or "").startswith("elspeth.core.landscape.")
            ):
                return True
    return False


def _function_return_values(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.expr, ...]:
    returned: list[ast.expr] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_Return(self, node: ast.Return) -> None:
            if node.value is not None:
                returned.append(node.value)

    Visitor().visit(function)
    return tuple(returned)


def _record_return_values(record: _FunctionRecord) -> tuple[ast.expr, ...]:
    return _function_return_values(record.node)


def _reachable_expression_nodes(node: ast.AST) -> Iterable[ast.AST]:
    yield node
    if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Constant):
        yield from _reachable_expression_nodes(node.body if node.test.value else node.orelse)
        return
    for child in ast.iter_child_nodes(node):
        yield from _reachable_expression_nodes(child)


def _clock_returning_references(
    sources: dict[str, str],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Summarize process, pure-database, and unresolved clock wrappers."""

    records = _function_records(sources)
    process_identities: set[tuple[str, str]] = set()
    database_candidates: set[tuple[str, str]] = set()
    unresolved_identities: set[tuple[str, str]] = set()

    def references(identities: set[tuple[str, str]]) -> set[str]:
        return {f"{module}.{symbol}" for path, symbol in identities if (module := _module_name(path)) is not None}

    object_aliases: dict[str, str] = {}
    for path, source in sources.items():
        module = _module_name(path)
        if module is None:
            continue
        tree = ast.parse(source, filename=path)
        local_classes = {candidate.name for candidate in tree.body if isinstance(candidate, ast.ClassDef)}
        for candidate in tree.body:
            if (
                isinstance(candidate, ast.Assign)
                and isinstance(candidate.value, ast.Call)
                and isinstance(candidate.value.func, ast.Name)
                and candidate.value.func.id in local_classes
            ):
                for target in candidate.targets:
                    if isinstance(target, ast.Name):
                        object_aliases[f"{module}.{target.id}"] = f"{module}.{candidate.value.func.id}"

    def canonical_references(record: _FunctionRecord) -> frozenset[str]:
        expanded = set(record.return_called_qualified)
        for reference in tuple(expanded):
            for alias, object_type in object_aliases.items():
                if reference == alias or reference.startswith(f"{alias}."):
                    suffix = reference.removeprefix(alias)
                    expanded.add(f"{object_type}{suffix or '.__call__'}")
        return frozenset(expanded)

    changed = True
    while changed:
        changed = False
        process_references = references(process_identities)
        database_references = references(database_candidates)
        for record in records:
            identity = (record.path, record.symbol)
            returned_references = canonical_references(record)
            if identity not in process_identities and returned_references & (_PROCESS_CLOCKS | process_references):
                process_identities.add(identity)
                changed = True
            if identity not in database_candidates and (
                _record_returns_inline_database_clock(record) or bool(returned_references & database_references)
            ):
                database_candidates.add(identity)
                changed = True

    transparent_calls = {"case", "cast", "choose", "coalesce", "func", "literal", "timedelta"}
    known_references = _PROCESS_CLOCKS | references(process_identities) | references(database_candidates)
    record_references = {f"{module}.{record.symbol}" for record in records if (module := _module_name(record.path)) is not None}
    for record in records:
        module = _module_name(record.path)
        parameter_names = {
            parameter.arg for parameter in (*record.node.args.posonlyargs, *record.node.args.args, *record.node.args.kwonlyargs)
        }

        def is_transparent_reference(reference: str, record_module: str | None = module) -> bool:
            terminal = reference.rsplit(".", 1)[-1]
            return terminal in transparent_calls and (
                (record_module is not None and reference == f"{record_module}.{terminal}") or reference.startswith("sqlalchemy.")
            )

        unresolved_references = {
            reference
            for reference in canonical_references(record)
            if reference not in known_references
            and reference not in {"sqlalchemy.func.current_timestamp", "sqlalchemy.func.transaction_timestamp"}
            and reference.rsplit(".", 1)[-1] not in parameter_names
            and not is_transparent_reference(reference)
        }
        unresolved_names = {
            name
            for name in record.return_called_names
            if name not in transparent_calls
            and name not in {"current_timestamp", "read_landscape_transaction_time", "transaction_timestamp"}
        }
        if unresolved_references or (
            unresolved_names and not canonical_references(record) and any(_name_has_clock_marker(name) for name in unresolved_names)
        ):
            unresolved_identities.add((record.path, record.symbol))

    changed = True
    while changed:
        changed = False
        unresolved_references = references(unresolved_identities)
        for record in records:
            identity = (record.path, record.symbol)
            if identity not in unresolved_identities and canonical_references(record) & (
                unresolved_references | (record_references - known_references)
            ):
                unresolved_identities.add(identity)
                changed = True

    database_identities = database_candidates - process_identities - unresolved_identities

    reexports: dict[str, str] = {}
    wildcard_reexports: list[tuple[str, str]] = []
    for path, source in sources.items():
        module = _module_name(path)
        if module is None:
            continue
        tree = ast.parse(source, filename=path)
        for candidate in tree.body:
            if not isinstance(candidate, ast.ImportFrom):
                continue
            origin = _relative_import_module(path, candidate)
            if any(alias.name == "*" for alias in candidate.names):
                wildcard_reexports.append((module, origin))
                continue
            for alias in candidate.names:
                reexports[f"{module}.{alias.asname or alias.name}"] = f"{origin}.{alias.name}"

    def with_reexports(base: set[str]) -> frozenset[str]:
        expanded = set(base)
        for reference in tuple(expanded):
            for alias, object_type in object_aliases.items():
                if reference == f"{object_type}.__call__":
                    expanded.add(alias)
                elif reference.startswith(f"{object_type}."):
                    expanded.add(f"{alias}.{reference.removeprefix(f'{object_type}.')}")
        changed = True
        while changed:
            changed = False
            for alias, origin in reexports.items():
                if origin in expanded and alias not in expanded:
                    expanded.add(alias)
                    changed = True
            for alias_module, origin_module in wildcard_reexports:
                for reference in tuple(expanded):
                    if reference.startswith(f"{origin_module}."):
                        suffix = reference.removeprefix(f"{origin_module}.")
                        if "." in suffix:
                            continue
                        alias = f"{alias_module}.{suffix}"
                        if alias not in expanded:
                            expanded.add(alias)
                            changed = True
        return frozenset(expanded)

    return (
        with_reexports(references(process_identities)),
        with_reexports(references(database_identities)),
        with_reexports(references(unresolved_identities)),
    )


def _clock_domain_returning_references(sources: dict[str, str]) -> tuple[frozenset[str], frozenset[str]]:
    records = _function_records(sources)
    sessions: set[tuple[str, str]] = set()
    landscape: set[tuple[str, str]] = set()

    def references(identities: set[tuple[str, str]]) -> set[str]:
        return {f"{module}.{symbol}" for path, symbol in identities if (module := _module_name(path)) is not None}

    changed = True
    while changed:
        changed = False
        session_references = references(sessions)
        landscape_references = references(landscape)
        for record in records:
            identity = (record.path, record.symbol)
            rendered_returns = " ".join(ast.unparse(value).lower() for value in _record_return_values(record))
            if identity not in sessions and (
                ("session" in rendered_returns and any(marker in rendered_returns for marker in _DATABASE_CLOCK_MARKERS))
                or bool(record.return_called_qualified & session_references)
            ):
                sessions.add(identity)
                changed = True
            if identity not in landscape and (
                ("landscape" in rendered_returns and any(marker in rendered_returns for marker in _DATABASE_CLOCK_MARKERS))
                or bool(record.return_called_qualified & landscape_references)
            ):
                landscape.add(identity)
                changed = True
    return frozenset(references(sessions)), frozenset(references(landscape))


def _terminal_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "__getattribute__"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    if (
        isinstance(node, ast.Call)
        and _terminal_name(node.func) in {"getattr", "__getattribute__"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def _attribute_chain(node: ast.expr) -> str | None:
    parts: list[str] = []
    cursor: ast.expr = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return None
    parts.append(cursor.id)
    return ".".join(reversed(parts))


def _looks_like_authority_receiver(node: ast.expr) -> bool:
    rendered = ast.unparse(node).lower()
    return any(marker in rendered for marker in ("checkpoint", "coordination", "effect", "landscape", "repo", "scheduler"))


class _ClockScanner(ast.NodeVisitor):
    def __init__(
        self,
        source: str,
        *,
        path: str,
        authority_terminals: frozenset[str] | None = None,
        external_process_callables: frozenset[str] = frozenset(),
        external_database_callables: frozenset[str] = frozenset(),
        external_unresolved_callables: frozenset[str] = frozenset(),
        external_sessions_callables: frozenset[str] = frozenset(),
        external_landscape_callables: frozenset[str] = frozenset(),
    ) -> None:
        self._source = source
        self._path = path
        self._tree = ast.parse(source, filename=path)
        self._imports: dict[str, str] = {}
        self._wildcard_imports: set[str] = set()
        self._object_types: dict[str, str] = {}
        self._module_assignments: dict[str, ast.expr] = {}
        self._instance_assignments: dict[str, ast.expr] = {}
        self._scope_assignments: dict[int, dict[str, ast.expr]] = {}
        self._assignment_stack: list[dict[str, ast.expr]] = []
        self._wrappers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self._class_wrappers: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self._node_classes: dict[int, str] = {}
        self._class_instance_assignments: dict[str, dict[str, ast.expr]] = {}
        self._flow_environments: dict[int, dict[str, ast.expr]] = {}
        self._symbol_stack: list[str] = []
        self._class_stack: list[str] = []
        self._parameter_stack: list[set[str]] = []
        self._all_parameter_stack: list[set[str]] = []
        self._provenance_cycle = False
        discovered = _discover_authority_boundaries({path: source})
        self._authority_symbols = {(boundary.path, boundary.symbol) for boundary in discovered}
        self._authority_terminals = frozenset(
            set(_CLOCK_AUTHORITY_VERBS)
            | set(authority_terminals or ())
            | {symbol.rsplit(".", 1)[-1] for _path, symbol in _REQUIRED_AUTHORITY_PUBLIC_SURFACE}
            | {boundary.symbol.rsplit(".", 1)[-1] for boundary in discovered}
        )
        self._external_process_callables = external_process_callables
        self._external_database_callables = external_database_callables
        self._external_unresolved_callables = external_unresolved_callables
        self._external_sessions_callables = external_sessions_callables
        self._external_landscape_callables = external_landscape_callables
        self.violations: list[ClockViolation] = []
        self._collect_bindings()
        self._build_flow_bindings()

    @staticmethod
    def _binding_key(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return ast.unparse(node)
        return None

    def _bind_assignment(self, bindings: dict[str, ast.expr], target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            for item_target, item_value in zip(target.elts, value.elts, strict=True):
                self._bind_assignment(bindings, item_target, item_value)
            return
        key = self._binding_key(target)
        if key is not None:
            bindings[key] = value

    def _collect_bindings(self) -> None:
        scanner = self

        class BindingVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.bindings = scanner._module_assignments
                self.class_depth = 0

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    scanner._imports[local] = alias.name

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                imported_module = _relative_import_module(scanner._path, node)
                for alias in node.names:
                    if alias.name == "*":
                        scanner._wildcard_imports.add(imported_module)
                    else:
                        local = alias.asname or alias.name
                        scanner._imports[local] = f"{imported_module}.{alias.name}"
                        if alias.name in {"ge", "gt", "le", "lt", "text"}:
                            scanner._module_assignments[local] = ast.Name(id=alias.name, ctx=ast.Load())

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    scanner._bind_assignment(self.bindings, target, node.value)
                    key = scanner._binding_key(target)
                    if key is not None and key.startswith("self."):
                        scanner._instance_assignments[key] = node.value
                    if self.class_depth == 0 and isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                        if isinstance(node.value.func, ast.Name) and node.value.func.id in scanner._imports:
                            scanner._object_types[target.id] = scanner._imports[node.value.func.id]
                        elif (
                            scanner._call_terminal(node.value.func) == "import_module"
                            and node.value.args
                            and isinstance(node.value.args[0], ast.Constant)
                            and isinstance(node.value.args[0].value, str)
                        ):
                            scanner._object_types[target.id] = node.value.args[0].value
                self.generic_visit(node.value)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if node.value is not None:
                    scanner._bind_assignment(self.bindings, node.target, node.value)
                    key = scanner._binding_key(node.target)
                    if key is not None and key.startswith("self."):
                        scanner._instance_assignments[key] = node.value
                    self.generic_visit(node.value)

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                if self.class_depth == 0:
                    scanner._wrappers[node.name] = node
                previous = self.bindings
                self.bindings = scanner._scope_assignments.setdefault(id(node), {})
                for statement in node.body:
                    self.visit(statement)
                self.bindings = previous

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.class_depth += 1
                self.generic_visit(node)
                self.class_depth -= 1

        BindingVisitor().visit(self._tree)

    @staticmethod
    def _union_expression(values: Iterable[ast.expr]) -> ast.expr | None:
        flattened: list[ast.expr] = []
        seen: set[str] = set()
        for value in values:
            candidates = value.elts if isinstance(value, ast.Tuple) and getattr(value, "_clock_union", False) else (value,)
            for candidate in candidates:
                rendered = ast.dump(candidate, include_attributes=False)
                if rendered not in seen:
                    seen.add(rendered)
                    flattened.append(candidate)
        if not flattened:
            return None
        if len(flattened) == 1:
            return flattened[0]
        union = ast.Tuple(elts=flattened, ctx=ast.Load())
        union._clock_union = True  # type: ignore[attr-defined]
        return union

    @classmethod
    def _merge_environments(cls, *environments: dict[str, ast.expr]) -> dict[str, ast.expr]:
        merged: dict[str, ast.expr] = {}
        for key in set().union(*(environment.keys() for environment in environments)):
            value = cls._union_expression(environment[key] for environment in environments if key in environment)
            if value is not None:
                merged[key] = value
        return merged

    def _record_expression_environment(self, node: ast.AST | None, environment: dict[str, ast.expr]) -> None:
        if node is None:
            return
        self._flow_environments[id(node)] = environment.copy()
        if isinstance(node, ast.NamedExpr):
            self._record_expression_environment(node.value, environment)
            self._bind_flow_target(node.target, node.value, environment)
            self._record_expression_environment(node.target, environment)
            return
        if (
            isinstance(node, ast.Call)
            and _terminal_name(node.func) in {"setattr", "__setattr__"}
            and len(node.args) >= 3
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
        ):
            for argument in node.args:
                self._record_expression_environment(argument, environment)
            attribute = _resolved_mapping_key(node.args[1], environment)
            if attribute is not None:
                environment[f"self.{attribute}"] = node.args[2]
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            self._record_expression_environment(child, environment)

    def _bind_flow_target(self, target: ast.expr, value: ast.expr, environment: dict[str, ast.expr]) -> None:
        if isinstance(target, ast.Name):
            environment[target.id] = value
            return
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
            environment[f"self.{target.attr}"] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            for item_target, item_value in zip(target.elts, value.elts, strict=False):
                self._bind_flow_target(item_target, item_value, environment)
            return
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            name = _mapping_alias_root(target.value.id, environment)
            target_key = _resolved_mapping_key(target.slice, environment)
            if target_key is None:
                environment[name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
                return
            existing = environment.get(name)
            items = _mapping_items(existing, {}) if existing is not None else ()
            if items is None:
                items = ()
            filtered = [(key, item_value) for key, item_value in items if key != target_key]
            filtered.append((target_key, value))
            environment[name] = ast.Dict(
                keys=[ast.Constant(key) for key, _item_value in filtered],
                values=[item_value for _key, item_value in filtered],
            )
            return
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Attribute)
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "self"
            and target.value.attr == "__dict__"
        ):
            attribute = _resolved_mapping_key(target.slice, environment)
            if attribute is not None:
                environment[f"self.{attribute}"] = value

    def _apply_flow_mapping_update(self, call: ast.Call, environment: dict[str, ast.expr]) -> bool:
        resolved_func = _normalized_callable(call.func, environment)
        if (
            not isinstance(resolved_func, ast.Attribute)
            or resolved_func.attr not in {"__setitem__", "setdefault", "update"}
            or not isinstance(resolved_func.value, ast.Name)
            or (resolved_func.attr == "update" and len(call.args) > 1)
        ):
            return False
        name = resolved_func.value.id
        if resolved_func.attr in {"__setitem__", "setdefault"}:
            if len(call.args) not in {1, 2} or call.keywords:
                environment[name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
                return True
            key = _resolved_mapping_key(call.args[0], environment)
            existing = _mapping_items(environment[name], {}) if name in environment else ()
            if key is None or existing is None:
                environment[name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
                return True
            merged = dict(existing)
            value = call.args[1] if len(call.args) == 2 else ast.Constant(None)
            if resolved_func.attr == "__setitem__":
                merged[key] = value
            else:
                merged.setdefault(key, value)
            environment[name] = ast.Dict(keys=[ast.Constant(item_key) for item_key in merged], values=list(merged.values()))
            return True
        incoming = _mapping_items(call.args[0], environment) if call.args else ()
        if incoming is None:
            environment[name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
            return True
        incoming_items = list(incoming)
        for keyword in call.keywords:
            if keyword.arg is not None:
                incoming_items.append((keyword.arg, keyword.value))
            else:
                expanded = _mapping_items(keyword.value, environment)
                if expanded is None:
                    environment[name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
                    return True
                incoming_items.extend(expanded)
        existing = _mapping_items(environment[name], {}) if name in environment else ()
        if existing is None:
            existing = ()
        merged = dict(existing)
        merged.update(incoming_items)
        environment[name] = ast.Dict(
            keys=[ast.Constant(key) for key in merged],
            values=list(merged.values()),
        )
        return True

    def _analyze_flow_block(self, statements: Sequence[ast.stmt], environment: dict[str, ast.expr]) -> dict[str, ast.expr]:
        current = environment.copy()
        for statement in statements:
            self._flow_environments[id(statement)] = current.copy()
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.Assign):
                self._record_expression_environment(statement.value, current)
                for target in statement.targets:
                    self._record_expression_environment(target, current)
                    self._bind_flow_target(target, statement.value, current)
                continue
            if isinstance(statement, ast.AnnAssign):
                self._record_expression_environment(statement.value, current)
                if statement.value is not None:
                    self._bind_flow_target(statement.target, statement.value, current)
                continue
            if isinstance(statement, ast.AugAssign):
                self._record_expression_environment(statement.value, current)
                if isinstance(statement.op, ast.BitOr) and isinstance(statement.target, ast.Name):
                    mapping_name = _mapping_alias_root(statement.target.id, current)
                    existing = _mapping_items(current.get(mapping_name, ast.Dict(keys=[], values=[])), current)
                    incoming = _mapping_items(statement.value, current)
                    if existing is None or incoming is None:
                        current[mapping_name] = ast.Name(id="__unknown_mapping__", ctx=ast.Load())
                    else:
                        merged = dict(existing)
                        merged.update(incoming)
                        current[mapping_name] = ast.Dict(
                            keys=[ast.Constant(key) for key in merged],
                            values=list(merged.values()),
                        )
                continue
            if isinstance(statement, ast.Expr):
                self._record_expression_environment(statement.value, current)
                if isinstance(statement.value, ast.Call):
                    self._apply_flow_mapping_update(statement.value, current)
                continue
            if isinstance(statement, ast.If):
                self._record_expression_environment(statement.test, current)
                body = self._analyze_flow_block(statement.body, current)
                alternate = self._analyze_flow_block(statement.orelse, current) if statement.orelse else current.copy()
                current = self._merge_environments(body, alternate)
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self._record_expression_environment(item.context_expr, current)
                    if item.optional_vars is not None:
                        self._bind_flow_target(item.optional_vars, item.context_expr, current)
                current = self._analyze_flow_block(statement.body, current)
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                self._record_expression_environment(getattr(statement, "iter", None), current)
                self._record_expression_environment(getattr(statement, "test", None), current)
                loop_environment = current.copy()
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    iterable = _resolved_expression(statement.iter, current)
                    if isinstance(iterable, (ast.List, ast.Tuple, ast.Set)):
                        value = self._union_expression(iterable.elts)
                    else:
                        value = ast.Name(id="__unknown_clock_provenance__", ctx=ast.Load())
                    if value is not None:
                        self._bind_flow_target(statement.target, value, loop_environment)
                body = self._analyze_flow_block(statement.body, loop_environment)
                alternate = self._analyze_flow_block(statement.orelse, current) if statement.orelse else current.copy()
                current = self._merge_environments(current, body, alternate)
                continue
            if isinstance(statement, ast.Match):
                self._record_expression_environment(statement.subject, current)
                paths: list[dict[str, ast.expr]] = [current.copy()]
                for case in statement.cases:
                    case_environment = current.copy()
                    capture_names = {
                        candidate.name
                        for candidate in ast.walk(case.pattern)
                        if isinstance(candidate, (ast.MatchAs, ast.MatchStar)) and candidate.name is not None
                    }
                    for name in capture_names:
                        case_environment[name] = statement.subject
                    self._record_expression_environment(case.guard, case_environment)
                    paths.append(self._analyze_flow_block(case.body, case_environment))
                current = self._merge_environments(*paths)
                continue
            if isinstance(statement, ast.Try):
                body = self._analyze_flow_block(statement.body, current)
                paths = [body]
                paths.extend(self._analyze_flow_block(handler.body, current) for handler in statement.handlers)
                if statement.orelse:
                    paths.append(self._analyze_flow_block(statement.orelse, body))
                current = self._merge_environments(*paths)
                if statement.finalbody:
                    current = self._analyze_flow_block(statement.finalbody, current)
                continue
            for child in ast.iter_child_nodes(statement):
                if isinstance(child, ast.expr):
                    self._record_expression_environment(child, current)
        return current

    def _build_flow_bindings(self) -> None:
        scanner = self

        class ClassVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.classes: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.classes.append(node.name)
                scanner._node_classes[id(node)] = ".".join(self.classes)
                self.generic_visit(node)
                self.classes.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                class_name = ".".join(self.classes)
                if class_name:
                    scanner._node_classes[id(node)] = class_name
                    scanner._class_wrappers[(class_name, node.name)] = node
                    class_bindings = scanner._class_instance_assignments.setdefault(class_name, {})
                    function_assignments = scanner._scope_assignments.get(id(node), {})
                    for candidate in ast.walk(node):
                        if isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                            targets = candidate.targets if isinstance(candidate, ast.Assign) else (candidate.target,)
                            value = candidate.value
                            if value is None:
                                continue
                            for target in targets:
                                key = scanner._binding_key(target)
                                if (
                                    key is None
                                    and isinstance(target, ast.Subscript)
                                    and isinstance(target.value, ast.Attribute)
                                    and isinstance(target.value.value, ast.Name)
                                    and target.value.value.id == "self"
                                    and target.value.attr == "__dict__"
                                    and (attribute := _resolved_mapping_key(target.slice, function_assignments)) is not None
                                ):
                                    key = f"self.{attribute}"
                                if key is not None and key.startswith("self."):
                                    union = scanner._union_expression((*([class_bindings[key]] if key in class_bindings else []), value))
                                    if union is not None:
                                        class_bindings[key] = union
                        elif (
                            isinstance(candidate, (ast.For, ast.AsyncFor))
                            and isinstance(candidate.target, ast.Attribute)
                            and isinstance(candidate.target.value, ast.Name)
                            and candidate.target.value.id == "self"
                            and isinstance(candidate.iter, (ast.List, ast.Tuple, ast.Set))
                        ):
                            key = f"self.{candidate.target.attr}"
                            value = scanner._union_expression(candidate.iter.elts)
                            if value is not None:
                                union = scanner._union_expression((*([class_bindings[key]] if key in class_bindings else []), value))
                                if union is not None:
                                    class_bindings[key] = union
                        elif (
                            isinstance(candidate, ast.Call)
                            and _terminal_name(candidate.func) in {"setattr", "__setattr__"}
                            and len(candidate.args) >= 3
                            and isinstance(candidate.args[0], ast.Name)
                            and candidate.args[0].id == "self"
                        ):
                            attribute = _resolved_mapping_key(candidate.args[1], function_assignments)
                            if attribute is None:
                                continue
                            key = f"self.{attribute}"
                            union = scanner._union_expression(
                                (*([class_bindings[key]] if key in class_bindings else []), candidate.args[2])
                            )
                            if union is not None:
                                class_bindings[key] = union
                self.generic_visit(node)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

        ClassVisitor().visit(self._tree)
        self._analyze_flow_block(self._tree.body, {})
        for function in (node for node in ast.walk(self._tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            class_name = self._node_classes.get(id(function), "")
            initial = self._flow_environments.get(id(function), {}).copy()
            initial.update(self._class_instance_assignments.get(class_name, {}))
            self._analyze_flow_block(function.body, initial)

    def _lookup_assignment(
        self,
        key: str,
        *,
        scope: int | None = None,
        at_node: ast.AST | None = None,
    ) -> ast.expr | None:
        if at_node is not None:
            value = self._flow_environments.get(id(at_node), {}).get(key)
            if value is not None:
                return value
        if scope is not None:
            value = self._scope_assignments.get(scope, {}).get(key)
            if value is not None:
                return value
        else:
            for bindings in reversed(self._assignment_stack):
                value = bindings.get(key)
                if value is not None:
                    return value
        class_name = ".".join(self._class_stack)
        if class_name:
            value = self._class_instance_assignments.get(class_name, {}).get(key)
            if value is not None:
                return value
        return self._module_assignments.get(key)

    def _current_assignments(self, node: ast.AST | None = None) -> dict[str, ast.expr]:
        if node is not None and id(node) in self._flow_environments:
            return self._flow_environments[id(node)].copy()
        assignments = {**self._module_assignments, **self._instance_assignments}
        for bindings in self._assignment_stack:
            assignments.update(bindings)
        return assignments

    def _qualified(self, node: ast.expr) -> str | None:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Call)
            and self._call_terminal(node.value.func) == "import_module"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str)
        ):
            return f"{node.value.args[0].value}.{node.attr}"
        chain = _attribute_chain(node)
        if chain is None:
            return None
        head, *tail = chain.split(".")
        imported = self._imports.get(head) or self._object_types.get(head) or head
        return ".".join((imported, *tail))

    def _external_qualified_candidates(self, node: ast.expr) -> frozenset[str]:
        candidates = {qualified for qualified in (self._qualified(node),) if qualified is not None}
        chain = _attribute_chain(node)
        if chain is not None and chain.split(".", 1)[0] not in self._imports:
            candidates.update(f"{module}.{chain}" for module in self._wildcard_imports)
        return frozenset(candidates)

    def _call_terminal(self, node: ast.expr, *, seen: frozenset[str] = frozenset(), scope: int | None = None) -> str | None:
        terminal = _terminal_name(node)
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self._lookup_assignment(node.id, scope=scope, at_node=node)
            if value is not None:
                return self._call_terminal(value, seen=seen | {node.id}, scope=scope)
        return terminal

    def _is_process_clock_callable(self, node: ast.expr, *, seen: frozenset[str] = frozenset(), scope: int | None = None) -> bool:
        if isinstance(node, ast.Tuple) and getattr(node, "_clock_union", False):
            return any(self._is_process_clock_callable(item, seen=seen, scope=scope) for item in node.elts)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant):
            container = self._lookup_assignment(node.value.id, scope=scope, at_node=node)
            if isinstance(container, (ast.List, ast.Tuple)) and isinstance(node.slice.value, int):
                try:
                    return self._is_process_clock_callable(container.elts[node.slice.value], seen=seen, scope=scope)
                except IndexError:
                    return False
        if self._external_qualified_candidates(node) & (_PROCESS_CLOCKS | self._external_process_callables):
            return True
        if isinstance(node, ast.Name):
            if node.id in seen:
                self._provenance_cycle = True
                return False
            imported = self._imports.get(node.id)
            if imported in _PROCESS_CLOCKS:
                return True
            value = self._lookup_assignment(node.id, scope=scope, at_node=node)
            if value is not None and self._is_process_clock_callable(value, seen=seen | {node.id}, scope=scope):
                return True
            wrapper = self._wrappers.get(node.id)
            if wrapper is not None:
                return any(
                    self._expression_uses_process_clock(candidate, seen=seen | {node.id}, scope=id(wrapper))
                    for candidate in _function_return_values(wrapper)
                )
        key = self._binding_key(node)
        if key is not None and key in seen:
            self._provenance_cycle = True
            return False
        if key is not None and key not in seen:
            value = self._lookup_assignment(key, scope=scope, at_node=node)
            if value is not None and self._is_process_clock_callable(value, seen=seen | {key}, scope=scope):
                return True
        terminal = _terminal_name(node)
        if isinstance(node, ast.Attribute) and terminal is not None and terminal in seen:
            self._provenance_cycle = True
            return False
        if isinstance(node, ast.Attribute) and terminal is not None and terminal not in seen:
            class_name = self._node_classes.get(id(node), ".".join(self._class_stack))
            wrapper = self._class_wrappers.get((class_name, terminal)) if class_name else self._wrappers.get(terminal)
            if wrapper is not None:
                return any(
                    self._expression_uses_process_clock(candidate, seen=seen | {terminal}, scope=id(wrapper))
                    for candidate in _function_return_values(wrapper)
                )
        if isinstance(node, ast.Lambda):
            return self._expression_uses_process_clock(node.body, seen=seen, scope=scope)
        if isinstance(node, ast.Call) and self._qualified(node.func) == "functools.partial" and node.args:
            return self._is_process_clock_callable(node.args[0], seen=seen, scope=scope)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
            receiver = self._qualified(node.args[0])
            attribute = node.args[1]
            clock_attribute = not isinstance(attribute, ast.Constant) or attribute.value in {
                "monotonic",
                "now",
                "time",
                "utcnow",
            }
            return receiver in {"datetime", "datetime.datetime", "time"} and clock_attribute
        if (
            isinstance(node, ast.Call)
            and self._qualified(node.func) in {"operator.attrgetter", "operator.methodcaller"}
            and node.args
            and (not isinstance(node.args[0], ast.Constant) or node.args[0].value in {"monotonic", "now", "time", "today", "utcnow"})
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in {"monotonic", "now", "time", "today", "utcnow"}
            and (
                (
                    isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "__dict__"
                    and self._qualified(node.func.value.value) in {"datetime", "datetime.datetime", "time"}
                )
                or (
                    isinstance(node.func.value, ast.Call)
                    and _terminal_name(node.func.value.func) == "vars"
                    and node.func.value.args
                    and self._qualified(node.func.value.args[0]) in {"datetime", "datetime.datetime", "time"}
                )
            )
        ):
            return True
        return (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Call)
            and _terminal_name(node.value.func) == "vars"
            and node.value.args
            and self._qualified(node.value.args[0]) in {"datetime", "datetime.datetime", "time"}
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in {"monotonic", "now", "time", "today", "utcnow"}
        )

    def _is_unresolved_clock_callable(self, node: ast.expr) -> bool:
        qualified_candidates = self._external_qualified_candidates(node)
        if qualified_candidates & self._external_unresolved_callables:
            return True
        if isinstance(node, ast.Name) and (wrapper := self._wrappers.get(node.id)) is not None:
            parameter_names = {parameter.arg for parameter in (*wrapper.args.posonlyargs, *wrapper.args.args, *wrapper.args.kwonlyargs)}
            returned_calls = [
                candidate
                for returned in _function_return_values(wrapper)
                for candidate in _reachable_expression_nodes(returned)
                if isinstance(candidate, ast.Call)
            ]
            if returned_calls and all(isinstance(call.func, ast.Name) and call.func.id in parameter_names for call in returned_calls):
                return False
            return bool(returned_calls)
        qualified = self._qualified(node)
        terminal = _terminal_name(node)
        return (
            qualified is not None
            and terminal is not None
            and _name_has_clock_marker(terminal)
            and qualified not in self._external_database_callables
            and qualified not in self._external_process_callables
            and (
                (isinstance(node, ast.Name) and node.id in self._imports)
                or (
                    isinstance(node, ast.Attribute)
                    and _attribute_chain(node) is not None
                    and _attribute_chain(node).split(".", 1)[0] in {*self._imports, *self._object_types}
                )
            )
        )

    def _is_database_clock_callable(
        self,
        node: ast.expr,
        *,
        seen: frozenset[str] = frozenset(),
        scope: int | None = None,
    ) -> bool:
        if isinstance(node, ast.Tuple) and getattr(node, "_clock_union", False):
            return all(self._is_database_clock_callable(item, seen=seen, scope=scope) for item in node.elts)
        if self._external_qualified_candidates(node) & self._external_database_callables:
            return True
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "func"
            and node.attr in {"current_timestamp", "transaction_timestamp"}
            and self._imports.get("func") in {None, "sqlalchemy.func"}
            and self._lookup_assignment("func", scope=scope, at_node=node) is None
            and not any("func" in parameters for parameters in self._all_parameter_stack)
        ):
            return True
        if isinstance(node, ast.Name):
            if node.id in seen:
                return False
            value = self._lookup_assignment(node.id, scope=scope, at_node=node)
            if value is not None and self._is_database_clock_callable(value, seen=seen | {node.id}, scope=scope):
                return True
            wrapper = self._wrappers.get(node.id)
            if wrapper is not None:
                return any(
                    self._expression_uses_database_clock(candidate, seen=seen | {node.id}, scope=id(wrapper))
                    for candidate in _function_return_values(wrapper)
                )
            if node.id == "read_landscape_transaction_time" and (self._imports.get(node.id) or "").startswith("elspeth.core.landscape."):
                return True
        terminal = _terminal_name(node)
        if isinstance(node, ast.Attribute) and terminal is not None and terminal not in seen:
            class_name = self._node_classes.get(id(node), ".".join(self._class_stack))
            wrapper = self._class_wrappers.get((class_name, terminal)) if class_name else None
            if wrapper is not None:
                return any(
                    self._expression_uses_database_clock(candidate, seen=seen | {terminal}, scope=id(wrapper))
                    for candidate in _function_return_values(wrapper)
                )
        return False

    def _expression_uses_database_clock(
        self,
        node: ast.AST,
        *,
        seen: frozenset[str] = frozenset(),
        scope: int | None = None,
    ) -> bool:
        if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Constant):
            return self._expression_uses_database_clock(node.body if node.test.value else node.orelse, seen=seen, scope=scope)
        if isinstance(node, ast.expr):
            key = self._binding_key(node)
            if key is not None and key not in seen:
                value = self._lookup_assignment(key, scope=scope, at_node=node)
                if value is not None and self._expression_uses_database_clock(value, seen=seen | {key}, scope=scope):
                    return True
        if isinstance(node, ast.Call) and self._is_database_clock_callable(node.func, seen=seen, scope=scope):
            return True
        return any(self._expression_uses_database_clock(child, seen=seen, scope=scope) for child in ast.iter_child_nodes(node))

    def _clock_sources(self, node: ast.AST, *, seen: frozenset[str] = frozenset(), scope: int | None = None) -> frozenset[str]:
        sources: set[str] = set()
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant):
            container = self._lookup_assignment(node.value.id, scope=scope, at_node=node)
            if isinstance(container, (ast.List, ast.Tuple)) and isinstance(node.slice.value, int):
                try:
                    sources.update(self._clock_sources(container.elts[node.slice.value], seen=seen, scope=scope))
                except IndexError:
                    sources.add("unresolved")
        if isinstance(node, ast.expr):
            key = self._binding_key(node)
            if key is not None and key in seen:
                self._provenance_cycle = True
            elif key is not None:
                value = self._lookup_assignment(key, scope=scope, at_node=node)
                if value is not None:
                    sources.update(self._clock_sources(value, seen=seen | {key}, scope=scope))

        if isinstance(node, ast.Name):
            lowered = node.id.lower()
            if node.id == "__unknown_clock_provenance__":
                sources.add("unresolved")
            if lowered in _FORENSIC_NAMES:
                sources.add("forensic")
            if any(node.id in params for params in self._parameter_stack):
                sources.add("caller")
            if self._is_process_clock_callable(node, seen=seen, scope=scope):
                sources.add("process")
        elif isinstance(node, ast.Attribute):
            lowered = node.attr.lower()
            if lowered in _FORENSIC_NAMES:
                sources.add("forensic")
            if self._is_process_clock_callable(node, seen=seen, scope=scope):
                sources.add("process")
            elif self._is_database_clock_callable(node, seen=seen, scope=scope):
                sources.add("database")
        elif isinstance(node, ast.Call):
            process_clock = self._is_process_clock_callable(node.func, scope=scope)
            if process_clock:
                sources.add("process")
            database_clock = not process_clock and self._is_database_clock_callable(node.func, scope=scope)
            if database_clock:
                sources.add("database")
            if not process_clock and not database_clock and self._is_unresolved_clock_callable(node.func):
                sources.add("unresolved")

        for child in ast.iter_child_nodes(node):
            sources.update(self._clock_sources(child, seen=seen, scope=scope))
        return frozenset(sources)

    def _expression_uses_process_clock(self, node: ast.AST, *, seen: frozenset[str] = frozenset(), scope: int | None = None) -> bool:
        if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Constant):
            return self._expression_uses_process_clock(node.body if node.test.value else node.orelse, seen=seen, scope=scope)
        if isinstance(node, ast.expr):
            key = self._binding_key(node)
            if key is not None and key not in seen:
                value = self._lookup_assignment(key, scope=scope, at_node=node)
                if value is not None and self._expression_uses_process_clock(value, seen=seen | {key}, scope=scope):
                    return True
        if isinstance(node, ast.Call) and self._is_process_clock_callable(node.func, seen=seen, scope=scope):
            return True
        return any(self._expression_uses_process_clock(child, seen=seen, scope=scope) for child in ast.iter_child_nodes(node))

    def _clock_domains(
        self,
        node: ast.expr,
        *,
        seen: frozenset[str] = frozenset(),
        scope: int | None = None,
    ) -> frozenset[str]:
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self._lookup_assignment(node.id, scope=scope, at_node=node)
            if value is not None:
                return self._clock_domains(value, seen=seen | {node.id}, scope=scope)
        if isinstance(node, ast.Call):
            called = (self._qualified(node.func) or self._call_terminal(node.func, scope=scope) or "").lower()
        else:
            called = (self._qualified(node) or _terminal_name(node) or "").lower()
        is_clock = any(marker in called for marker in ("clock", "database_now", "database_time", "transaction_time"))
        domains: set[str] = set()
        if is_clock and "session" in called:
            domains.add("sessions")
        if is_clock and "landscape" in called:
            domains.add("landscape")
        if isinstance(node, ast.Call):
            qualified = self._external_qualified_candidates(node.func)
            if qualified & self._external_sessions_callables:
                domains.add("sessions")
            if qualified & self._external_landscape_callables:
                domains.add("landscape")
            if isinstance(node.func, ast.Name) and node.func.id not in seen and (wrapper := self._wrappers.get(node.func.id)) is not None:
                domains.update(
                    domain
                    for returned in _function_return_values(wrapper)
                    for domain in self._clock_domains(returned, seen=seen | {node.func.id}, scope=id(wrapper))
                )
        domains.update(
            domain
            for child in ast.iter_child_nodes(node)
            if isinstance(child, ast.expr)
            for domain in self._clock_domains(child, seen=seen, scope=scope)
        )
        return frozenset(domains)

    def _clock_domain(self, node: ast.expr, *, seen: frozenset[str] = frozenset(), scope: int | None = None) -> str | None:
        domains = self._clock_domains(node, seen=seen, scope=scope)
        return next(iter(domains)) if len(domains) == 1 else None

    def _symbol(self) -> str:
        return ".".join((*self._class_stack, *self._symbol_stack)) or "<module>"

    def _sensitive(self) -> bool:
        return (
            (self._path, self._symbol()) in self._authority_symbols
            or (self._path, self._symbol()) in _REQUIRED_AUTHORITY_PUBLIC_SURFACE
            or (not self._path.startswith("src/") and self._symbol().rsplit(".", 1)[-1] in self._authority_terminals)
        )

    def _emit(self, node: ast.AST, kind: str, detail: str) -> None:
        self.violations.append(ClockViolation(self._path, self._symbol(), node.lineno, kind, detail))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._symbol_stack.append(node.name)
        self._assignment_stack.append(self._scope_assignments.get(id(node), {}))
        parameters = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        all_parameters = {parameter.arg for parameter in parameters}
        if node.args.vararg is not None:
            all_parameters.add(node.args.vararg.arg)
        if node.args.kwarg is not None:
            all_parameters.add(node.args.kwarg.arg)
        clock_parameters = {
            parameter.arg
            for parameter in parameters
            if parameter.arg.lower() not in _FORENSIC_NAMES
            and (
                not any(marker in parameter.arg.lower() for marker in _DURATION_MARKERS)
                or parameter.arg.lower().endswith(("_at", "_timestamp"))
                or "epoch_seconds" in parameter.arg.lower()
            )
            and (
                _name_has_clock_marker(parameter.arg)
                or (
                    parameter.annotation is not None
                    and any(marker in ast.unparse(parameter.annotation).lower() for marker in ("date", "datetime", "timestamp"))
                )
            )
        }
        self._all_parameter_stack.append(all_parameters)
        self._parameter_stack.append(clock_parameters)
        self.generic_visit(node)
        self._parameter_stack.pop()
        self._all_parameter_stack.pop()
        self._assignment_stack.pop()
        self._symbol_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _neutral_caller_parameters(self, node: ast.AST) -> set[str]:
        candidates = self._resolved_parameter_names(node)
        resolved_node = self._resolved_value_expression(node)
        return {
            name
            for name in candidates
            if name not in _NON_CLOCK_CONTEXT_PARAMETERS
            and name.lower() not in _FORENSIC_NAMES
            and not name.endswith("_id")
            and not self._duration_is_database_offset(resolved_node, name)
            and (
                "epoch_seconds" in name.lower()
                or name.lower().endswith(("_at", "_timestamp"))
                or (
                    not any(marker in name.lower() for marker in _IDENTITY_PARAMETER_MARKERS)
                    and not any(marker in name.lower() for marker in _DURATION_MARKERS)
                )
            )
        }

    def _resolved_value_expression(self, node: ast.AST, *, seen: frozenset[str] = frozenset()) -> ast.AST:
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self._lookup_assignment(node.id, at_node=node)
            if value is not None:
                return self._resolved_value_expression(value, seen=seen | {node.id})
        return node

    def _resolved_parameter_names(self, node: ast.AST, *, seen: frozenset[str] = frozenset()) -> set[str]:
        parameters = set().union(*self._all_parameter_stack) if self._all_parameter_stack else set()
        resolved: set[str] = set()
        if isinstance(node, ast.Name):
            if node.id in parameters:
                resolved.add(node.id)
            if node.id not in seen:
                value = self._lookup_assignment(node.id, at_node=node)
                if value is not None:
                    resolved.update(self._resolved_parameter_names(value, seen=seen | {node.id}))
        for child in ast.iter_child_nodes(node):
            resolved.update(self._resolved_parameter_names(child, seen=seen))
        return resolved

    @staticmethod
    def _duration_is_database_offset(node: ast.AST, name: str) -> bool:
        occurrences = [candidate for candidate in ast.walk(node) if isinstance(candidate, ast.Name) and candidate.id == name]
        if not occurrences:
            return False
        safe_occurrences: set[int] = set()
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.BinOp) or not isinstance(candidate.op, (ast.Add, ast.Sub)):
                continue
            left_names = _identifier_names(candidate.left)
            right_names = _identifier_names(candidate.right)
            left_rendered = ast.unparse(candidate.left).lower()
            right_rendered = ast.unparse(candidate.right).lower()
            if name in left_names and any(marker in right_rendered for marker in _DATABASE_CLOCK_MARKERS):
                safe_occurrences.update(id(item) for item in ast.walk(candidate.left) if isinstance(item, ast.Name) and item.id == name)
            if name in right_names and any(marker in left_rendered for marker in _DATABASE_CLOCK_MARKERS):
                safe_occurrences.update(id(item) for item in ast.walk(candidate.right) if isinstance(item, ast.Name) and item.id == name)
        for candidate in ast.walk(node):
            if (
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and isinstance(candidate.func.value, ast.Name)
                and candidate.func.value.id == "func"
                and candidate.func.attr == "datetime"
                and len(candidate.args) == 2
                and (
                    _is_positive_seconds_modifier(candidate.args[1], name)
                    or (
                        isinstance(candidate.args[1], ast.JoinedStr)
                        and len(candidate.args[1].values) == 3
                        and isinstance(candidate.args[1].values[0], ast.Constant)
                        and candidate.args[1].values[0].value == "+"
                        and isinstance(candidate.args[1].values[1], ast.FormattedValue)
                        and name in _identifier_names(candidate.args[1].values[1].value)
                        and isinstance(candidate.args[1].values[2], ast.Constant)
                        and candidate.args[1].values[2].value == " seconds"
                    )
                )
                and isinstance(candidate.args[0], ast.Call)
                and isinstance(candidate.args[0].func, ast.Attribute)
                and isinstance(candidate.args[0].func.value, ast.Name)
                and candidate.args[0].func.value.id == "func"
                and candidate.args[0].func.attr in {"current_timestamp", "transaction_timestamp"}
            ):
                safe_occurrences.update(id(item) for item in ast.walk(candidate.args[1]) if isinstance(item, ast.Name) and item.id == name)
        return all(id(occurrence) in safe_occurrences for occurrence in occurrences)

    def _forwarded_absolute_parameters(self, node: ast.AST) -> set[str]:
        parameters = set().union(*self._all_parameter_stack) if self._all_parameter_stack else set()
        return {
            name
            for name in self._resolved_parameter_names(node)
            if name in parameters
            and (name.lower() in _NEUTRAL_ABSOLUTE_NAMES or "epoch_seconds" in name.lower() or name.lower().endswith(("_at", "_timestamp")))
        }

    def _emit_authority_time_sources(
        self,
        node: ast.AST,
        *,
        detail: str,
        detect_neutral_caller: bool = True,
        database_authoritative: bool = False,
    ) -> None:
        self._provenance_cycle = False
        sources = set(self._clock_sources(node))
        if database_authoritative:
            sources.add("database")
        provenance_cycle = self._provenance_cycle
        if detect_neutral_caller and self._neutral_caller_parameters(node):
            sources.add("caller")
        for source in sorted(sources & {"caller", "forensic", "process"}):
            self._emit(node, f"{source}-clock-authority", detail)
        if "unresolved" in sources:
            self._emit(node, "unresolved-clock-provenance", detail)
        if "database" not in sources:
            self._emit(node, "missing-database-time", detail)
        if "sessions" in self._clock_domains(node):
            self._emit(node, "cross-database-clock-authority", "sessions -> landscape")
        if provenance_cycle:
            self._emit(node, "cyclic-clock-provenance", detail)

    def visit_Return(self, node: ast.Return) -> None:
        if self._sensitive() and node.value is not None:
            self._provenance_cycle = False
            sources = self._clock_sources(node.value)
            raw_clock_result = isinstance(node.value, ast.Name) or (
                isinstance(node.value, ast.Call) and self._is_process_clock_callable(node.value.func)
            )
            if raw_clock_result and "process" in sources:
                self._emit(node, "authority-returns-nondatabase-clock", ast.unparse(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        called = self._call_terminal(node.func)
        dynamic_getattr = (
            isinstance(node.func, ast.Call)
            and _terminal_name(node.func.func) in {"getattr", "__getattribute__"}
            and (
                (
                    _terminal_name(node.func.func) == "getattr"
                    and (len(node.func.args) < 2 or not isinstance(node.func.args[1], ast.Constant))
                )
                or (
                    _terminal_name(node.func.func) == "__getattribute__"
                    and (not node.func.args or not isinstance(node.func.args[0], ast.Constant))
                )
            )
        )
        if (
            dynamic_getattr
            and node.func.args
            and (_looks_like_authority_receiver(node.func.args[0]) or self._sensitive())
            and (node.args or node.keywords)
        ):
            self._emit(node, "dynamic-authority-getattr", ast.unparse(node.func))
            for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                for source in sorted(self._clock_sources(argument) & {"caller", "forensic", "process"}):
                    self._emit(argument, f"{source}-clock-authority", "dynamic getattr")
        if called in self._authority_terminals:
            for argument in node.args:
                if isinstance(argument, ast.Starred):
                    self._emit(argument, "dynamic-authority-args", called)
                    continue
                if "sessions" in self._clock_domains(argument):
                    self._emit(node, "cross-database-clock-forwarding", "sessions -> landscape")
                if self._clock_sources(argument) & {"caller", "forensic", "process"} or self._forwarded_absolute_parameters(argument):
                    self._emit(argument, "caller-clock-positional-forwarding", called)
            for keyword in node.keywords:
                if "sessions" in self._clock_domains(keyword.value):
                    self._emit(node, "cross-database-clock-forwarding", "sessions -> landscape")
                if keyword.arg is not None and (
                    self._clock_sources(keyword.value) & {"caller", "forensic", "process"}
                    or self._forwarded_absolute_parameters(keyword.value)
                ):
                    self._emit(keyword.value, "caller-clock-forwarding", called)
                elif keyword.arg is None and self._mapping_contains_clock(keyword.value):
                    self._emit(keyword.value, "caller-clock-kwargs-escape", called)
                elif keyword.arg is None and _mapping_items(keyword.value, self._current_assignments(keyword.value)) is None:
                    self._emit(keyword.value, "dynamic-authority-kwargs", called)

        if called == "values":
            assignments = self._current_assignments(node)
            for keyword in node.keywords:
                if keyword.arg in _AUTHORITY_DEADLINES and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
                    self._emit_authority_time_sources(keyword.value, detail=keyword.arg)
                elif keyword.arg is None:
                    items = _mapping_items(keyword.value, assignments)
                    if items is not None:
                        for key, value in items:
                            if key in _AUTHORITY_DEADLINES and not (isinstance(value, ast.Constant) and value.value is None):
                                self._emit_authority_time_sources(value, detail=key)
                    elif _targets_authority_table(node, assignments):
                        self._emit(keyword.value, "dynamic-authority-mapping", ast.unparse(keyword.value))
            for argument in node.args:
                items = _mapping_items(argument, assignments)
                if items is None:
                    if _targets_authority_table(node, assignments):
                        self._emit(argument, "dynamic-authority-mapping", ast.unparse(argument))
                    continue
                for key, value in items:
                    if key in _AUTHORITY_DEADLINES and not (isinstance(value, ast.Constant) and value.value is None):
                        self._emit_authority_time_sources(value, detail=key)

        assignments = self._current_assignments(node)
        if _sqlalchemy_ordering_callable(node.func, assignments, node.args):
            compared = ast.copy_location(
                ast.Tuple(elts=[*_bound_callable_arguments(node.func, assignments), *node.args], ctx=ast.Load()),
                node,
            )
            self._emit_authority_time_sources(compared, detail=ast.unparse(node))

        raw_terminal = _resolved_terminal(node.func, assignments)
        if _raw_sql_deadline_mutation(node, assignments):
            payload = ast.copy_location(
                ast.Tuple(elts=list(node.args[1:]) + [keyword.value for keyword in node.keywords], ctx=ast.Load()),
                node,
            )
            self._emit_authority_time_sources(
                payload,
                detail="raw SQL deadline mutation",
                database_authoritative=_raw_sql_database_clock(node, assignments),
            )
        elif (
            raw_terminal in {"exec_driver_sql", "execute"}
            and node.args
            and _static_sql_text(node.args[0], assignments) is None
            and not _is_sqlalchemy_statement_expression(node.args[0], assignments)
        ):
            payload = ast.copy_location(
                ast.Tuple(elts=list(node.args[1:]) + [keyword.value for keyword in node.keywords], ctx=ast.Load()),
                node,
            )
            payload_sources = self._clock_sources(payload)
            if self._sensitive() or payload_sources & {"caller", "forensic", "process", "unresolved"}:
                self._emit(node, "dynamic-authority-raw-sql", ast.unparse(node.args[0]))
                self._emit_authority_time_sources(payload, detail="dynamic raw SQL")

        if called in {"FollowerProcessor", "RunHeartbeatThread"}:
            for keyword in node.keywords:
                if keyword.arg == "now_fn":
                    self._emit(keyword.value, "injected-process-clock", called)

        called_text = ast.unparse(node.func).lower()
        target_domain = "sessions" if "session" in called_text else "landscape" if "landscape" in called_text else None
        if target_domain is not None:
            for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                for argument_domain in self._clock_domains(argument) - {target_domain}:
                    self._emit(node, "cross-database-clock-forwarding", f"{argument_domain} -> {target_domain}")
        self.generic_visit(node)

    def _mapping_contains_clock(self, node: ast.expr, *, seen: frozenset[str] = frozenset()) -> bool:
        if isinstance(node, ast.Dict):
            return any(
                isinstance(key, ast.Constant) and isinstance(key.value, str) and _name_has_clock_marker(key.value)
                for key in node.keys
                if key is not None
            ) or any(bool(self._clock_sources(value) & {"caller", "forensic", "process"}) for value in node.values)
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self._lookup_assignment(node.id, at_node=node)
            return value is not None and self._mapping_contains_clock(value, seen=seen | {node.id})
        if isinstance(node, ast.Call) and _terminal_name(node.func) == "dict":
            return any(keyword.arg is not None and _name_has_clock_marker(keyword.arg) for keyword in node.keywords)
        return False

    def visit_Compare(self, node: ast.Compare) -> None:
        assignments = self._current_assignments(node)
        expressions = (node.left, *node.comparators)
        names = set().union(*(_identifier_names(_resolved_expression(expression, assignments)) for expression in expressions))
        if names & _AUTHORITY_DEADLINES and any(isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for operator in node.ops):
            self._emit_authority_time_sources(node, detail=ast.unparse(node))
        domains = set().union(*(self._clock_domains(expression) for expression in (node.left, *node.comparators)))
        if len(domains) > 1:
            self._emit(node, "cross-database-clock-comparison", ast.unparse(node))
        self.generic_visit(node)

    def scan(self) -> tuple[ClockViolation, ...]:
        self.visit(self._tree)
        return tuple(sorted(set(self.violations)))


def _scan_source(source: str, *, path: str = "<mutation>") -> tuple[ClockViolation, ...]:
    return _ClockScanner(source, path=path).scan()


def _scan_sources(sources: dict[str, str]) -> tuple[ClockViolation, ...]:
    violations: list[ClockViolation] = []
    boundaries = _discover_authority_boundaries(sources)
    terminals = frozenset(boundary.symbol.rsplit(".", 1)[-1] for boundary in boundaries)
    process_callables, database_callables, unresolved_callables = _clock_returning_references(sources)
    sessions_callables, landscape_callables = _clock_domain_returning_references(sources)
    for relative, source in sources.items():
        violations.extend(
            _ClockScanner(
                source,
                path=relative,
                authority_terminals=terminals,
                external_process_callables=process_callables,
                external_database_callables=database_callables,
                external_unresolved_callables=unresolved_callables,
                external_sessions_callables=sessions_callables,
                external_landscape_callables=landscape_callables,
            ).scan()
        )
    return tuple(sorted(set(violations)))


def _scan_production(paths: Iterable[str] | None = None) -> tuple[ClockViolation, ...]:
    relative_paths = tuple(
        paths
        if paths is not None
        else (
            relative
            for source_file in sorted((_ROOT / "src/elspeth").rglob("*.py"))
            if (relative := source_file.relative_to(_ROOT).as_posix()).startswith(_AUTHORITY_SCOPE_PREFIXES)
        )
    )
    sources = {relative: (_ROOT / relative).read_text(encoding="utf-8") for relative in relative_paths}
    return _scan_sources(sources)


def _relative_import_module(path: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    module = _module_name(path) or ""
    package = module if path.endswith("/__init__.py") else module.rsplit(".", 1)[0]
    package_parts = package.split(".") if package else []
    keep = max(0, len(package_parts) - (node.level - 1))
    prefix = package_parts[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _sessions_import_violations(sources: dict[str, str]) -> tuple[str, ...]:
    forbidden_prefixes = ("elspeth.web.coordination", "elspeth.web.sessions")
    dependencies: dict[str, set[str]] = {}
    direct_details: dict[str, list[str]] = {}
    for relative, source in sorted(sources.items()):
        module_name = _module_name(relative)
        if module_name is None:
            continue
        tree = ast.parse(source, filename=relative)
        module_dependencies = dependencies.setdefault(module_name, set())
        details = direct_details.setdefault(module_name, [])
        import_module_aliases = {"__import__"}
        importlib_aliases = {"importlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _relative_import_module(relative, node)
                origins = {module, *(f"{module}.{alias.name}" for alias in node.names)}
                module_dependencies.update(origins)
                if node.module == "importlib":
                    import_module_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
                details.extend(f"{relative}:{node.lineno} {origin}" for origin in origins if origin.startswith(forbidden_prefixes))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_dependencies.add(alias.name)
                    if alias.name == "importlib":
                        importlib_aliases.add(alias.asname or alias.name)
                    if alias.name.startswith(forbidden_prefixes):
                        details.append(f"{relative}:{node.lineno} {alias.name}")
        assignments = _local_assignments(
            ast.FunctionDef(
                name="<module>",
                args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=tree.body,
                decorator_list=[],
            )
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            resolved_func = _normalized_callable(node.func, assignments)
            terminal = _terminal_name(resolved_func)
            qualified = _attribute_chain(resolved_func)
            is_loader = (
                terminal in import_module_aliases
                or terminal == "__import__"
                or (
                    qualified is not None
                    and qualified.rsplit(".", 1)[-1] == "import_module"
                    and qualified.split(".", 1)[0] in importlib_aliases
                )
            )
            loader_arguments = (*_bound_callable_arguments(node.func, assignments), *node.args)
            imported = _static_sql_text(loader_arguments[0], assignments) if loader_arguments else None
            if is_loader and imported is not None:
                if imported.startswith("."):
                    package_keyword = next((keyword.value for keyword in node.keywords if keyword.arg == "package"), None)
                    package = _static_sql_text(package_keyword, assignments) if package_keyword is not None else None
                    if package is not None:
                        level = len(imported) - len(imported.lstrip("."))
                        package_parts = package.split(".")
                        prefix = package_parts[: max(0, len(package_parts) - (level - 1))]
                        suffix = imported.lstrip(".")
                        imported = ".".join((*prefix, *(suffix.split(".") if suffix else ())))
                if terminal == "__import__":
                    fromlist = next((keyword.value for keyword in node.keywords if keyword.arg == "fromlist"), None)
                    if isinstance(fromlist, (ast.List, ast.Tuple)):
                        imported_names = [
                            item.value for item in fromlist.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        ]
                        if any(f"{imported}.{name}".startswith(forbidden_prefixes) for name in imported_names):
                            imported = next(
                                f"{imported}.{name}" for name in imported_names if f"{imported}.{name}".startswith(forbidden_prefixes)
                            )
                module_dependencies.add(imported)
                if imported.startswith(forbidden_prefixes):
                    details.append(f"{relative}:{node.lineno} {imported}")

    tainted = {module for module, details in direct_details.items() if details}
    changed = True
    while changed:
        changed = False
        for module, imported in dependencies.items():
            if module in tainted:
                continue
            if any(dependency == taint or dependency.startswith(f"{taint}.") for dependency in imported for taint in tainted):
                tainted.add(module)
                changed = True

    violations: list[str] = []
    for relative in sorted(sources):
        if not relative.startswith(("src/elspeth/core/landscape/", "src/elspeth/core/checkpoint/")):
            continue
        module = _module_name(relative)
        if module not in tainted:
            continue
        details = direct_details.get(module) or [f"{relative}:1 transitive Sessions clock reexport"]
        violations.extend(details)
    return tuple(violations)


def _clock_boundary_inventory() -> tuple[ClockBoundary, ...]:
    sources = {
        source_file.relative_to(_ROOT).as_posix(): source_file.read_text(encoding="utf-8")
        for source_file in sorted((_ROOT / "src/elspeth").rglob("*.py"))
    }
    return _discover_authority_boundaries(sources)


def _format_violations(violations: Sequence[ClockViolation]) -> str:
    return "\n".join(f"{item.path}:{item.line} {item.symbol}: {item.kind} ({item.detail})" for item in violations)


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    assert len(matches) == 1, f"expected exactly one {name}, found {len(matches)}"
    return matches[0]


def _connection_execute_calls(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "conn"
        and node.func.attr in {"exec_driver_sql", "execute"}
    ]


def _executable_statements(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    return [
        statement
        for statement in function.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str))
    ]


def _full_token_predicates(statement: ast.AST) -> set[tuple[str, str]]:
    predicates: set[tuple[str, str]] = set()
    candidates: tuple[ast.AST, ...]
    if isinstance(statement, ast.Call) and _terminal_name(statement.func) == "and_":
        candidates = tuple(statement.args)
    else:
        candidates = (statement,)
    for candidate in candidates:
        if not isinstance(candidate, ast.Compare) or len(candidate.ops) != 1 or not isinstance(candidate.ops[0], ast.Eq):
            continue
        operands = (candidate.left, candidate.comparators[0])
        chains = [_attribute_chain(operand) for operand in operands]
        if all(chain is not None for chain in chains):
            left_chain, right_chain = (chain for chain in chains if chain is not None)
            if left_chain.startswith("run_coordination_table.c.") and right_chain.startswith("token."):
                predicates.add((left_chain.rsplit(".", 1)[-1], right_chain.rsplit(".", 1)[-1]))
            if right_chain.startswith("run_coordination_table.c.") and left_chain.startswith("token."):
                predicates.add((right_chain.rsplit(".", 1)[-1], left_chain.rsplit(".", 1)[-1]))
    return predicates


def _statement_chain(statement: ast.expr) -> tuple[ast.Call | None, dict[str, list[ast.Call]]]:
    methods: dict[str, list[ast.Call]] = {}
    cursor = statement
    while isinstance(cursor, ast.Call) and isinstance(cursor.func, ast.Attribute):
        methods.setdefault(cursor.func.attr, []).append(cursor)
        cursor = cursor.func.value
    return (cursor if isinstance(cursor, ast.Call) else None), methods


def _deadline_expression_is_database_authoritative(
    expression: ast.expr,
    verify: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, bool]:
    def is_database_clock(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "func"
            and node.func.attr in {"current_timestamp", "transaction_timestamp"}
            and not node.args
            and not node.keywords
        )

    def is_duration(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id == "window_seconds") or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "timedelta"
            and not node.args
            and len(node.keywords) == 1
            and node.keywords[0].arg == "seconds"
            and isinstance(node.keywords[0].value, ast.Name)
            and node.keywords[0].value.id == "window_seconds"
        )

    def is_database_deadline(node: ast.expr) -> bool:
        if isinstance(node, ast.IfExp):
            return is_database_deadline(node.body) and is_database_deadline(node.orelse)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return (is_database_clock(node.left) and is_duration(node.right)) or (is_database_clock(node.right) and is_duration(node.left))
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "func"
            and node.func.attr == "datetime"
            and len(node.args) == 2
            and not node.keywords
            and is_database_clock(node.args[0])
            and _is_positive_seconds_modifier(node.args[1])
        )

    database_time = is_database_deadline(expression)
    rendered = ast.unparse(expression).lower()
    parameter_names = {
        parameter.arg
        for parameter in (*verify.args.posonlyargs, *verify.args.args, *verify.args.kwonlyargs)
        if parameter.arg not in _NON_CLOCK_CONTEXT_PARAMETERS
        and not parameter.arg.endswith("_id")
        and parameter.arg != "window_seconds"
        and (parameter.arg.lower() in _NEUTRAL_ABSOLUTE_NAMES or not any(marker in parameter.arg.lower() for marker in _DURATION_MARKERS))
    }
    caller_or_process = bool(_identifier_names(expression) & parameter_names) or any(
        process_clock in rendered for process_clock in _PROCESS_CLOCKS
    )
    return database_time, caller_or_process


def _first_fence_dependency_is_rebound(
    tree: ast.Module,
    name: str,
    scopes: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
) -> bool:
    def import_binds(candidate: ast.AST) -> bool:
        if isinstance(candidate, ast.Import):
            return any((alias.asname or alias.name.split(".")[0]) == name for alias in candidate.names)
        if isinstance(candidate, ast.ImportFrom):
            return any((alias.asname or alias.name) == name for alias in candidate.names)
        return False

    direct_module_imports = sum(import_binds(statement) for statement in tree.body)
    module_bindings = _local_assignments(
        ast.FunctionDef(
            name="<module>",
            args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=tree.body,
            decorator_list=[],
        )
    )

    def call_rebinds(call: ast.Call) -> bool:
        if _terminal_name(call.func) in {"eval", "exec"}:
            return True
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "update"
            and isinstance(call.func.value, ast.Call)
            and _terminal_name(call.func.value.func) in {"globals", "locals", "vars"}
        ):
            if any(keyword.arg == name for keyword in call.keywords):
                return True
            for argument in (*call.args, *(keyword.value for keyword in call.keywords if keyword.arg is None)):
                items = _mapping_items(argument, module_bindings)
                if items is None or any(key == name for key, _value in items):
                    return True
        if isinstance(call.func, ast.Attribute) and call.func.attr == "__setitem__" and isinstance(call.func.value, ast.Call):
            return (
                _terminal_name(call.func.value.func) in {"globals", "locals", "vars"}
                and len(call.args) >= 2
                and _resolved_mapping_key(call.args[0], module_bindings) == name
            )
        if _terminal_name(call.func) == "setitem" and len(call.args) >= 3 and isinstance(call.args[0], ast.Call):
            return (
                _terminal_name(call.args[0].func) in {"globals", "locals", "vars"}
                and _resolved_mapping_key(call.args[1], module_bindings) == name
            )
        return (
            _terminal_name(call.func) == "setattr"
            and len(call.args) >= 3
            and _resolved_mapping_key(call.args[1], module_bindings) == name
            and ("sys.modules" in ast.unparse(call.args[0]) or "__name__" in ast.unparse(call.args[0]))
        )

    def target_rebinds(target: ast.expr) -> bool:
        return name in _assigned_names(target) or (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Call)
            and isinstance(target.value.func, ast.Name)
            and target.value.func.id in {"globals", "locals", "vars"}
            and _resolved_mapping_key(target.slice, module_bindings) == name
        )

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and statement.name == name:
            return True
        targets: tuple[ast.expr, ...] = ()
        if isinstance(statement, ast.Assign):
            targets = tuple(statement.targets)
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            targets = (statement.target,)
        if any(target_rebinds(target) for target in targets):
            return True
        if import_binds(statement):
            if direct_module_imports > 1:
                return True
            continue
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
            import_binds(candidate) for candidate in ast.walk(statement)
        ):
            return True
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
            (isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Store) and candidate.id == name)
            or (isinstance(candidate, (ast.MatchAs, ast.MatchStar)) and candidate.name == name)
            for candidate in ast.walk(statement)
        ):
            return True
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
            call_rebinds(candidate) for candidate in ast.walk(statement) if isinstance(candidate, ast.Call)
        ):
            return True
    for scope in scopes:
        parameters = {
            argument.arg
            for argument in (
                *scope.args.posonlyargs,
                *scope.args.args,
                *scope.args.kwonlyargs,
                *((scope.args.vararg,) if scope.args.vararg is not None else ()),
                *((scope.args.kwarg,) if scope.args.kwarg is not None else ()),
            )
        }
        if (
            name in parameters
            or any(import_binds(candidate) for candidate in ast.walk(scope))
            or any(
                (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name)
                or (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name)
                or (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and target_rebinds(node))
                for node in ast.walk(scope)
            )
        ):
            return True
        if any(call_rebinds(candidate) for candidate in ast.walk(scope) if isinstance(candidate, ast.Call)):
            return True
    return False


def _first_fence_statement_is_effect_free(statement: ast.expr) -> bool:
    for call in (candidate for candidate in ast.walk(statement) if isinstance(candidate, ast.Call)):
        terminal = _terminal_name(call.func)
        if terminal in {"update", "where", "values"}:
            continue
        if (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "func"
            and call.func.attr in {"current_timestamp", "datetime", "transaction_timestamp"}
            and (call.func.attr == "datetime" or not call.args)
            and not call.keywords
        ):
            continue
        if (
            isinstance(call.func, ast.Name)
            and call.func.id == "timedelta"
            and not call.args
            and len(call.keywords) == 1
            and call.keywords[0].arg == "seconds"
            and isinstance(call.keywords[0].value, ast.Name)
            and call.keywords[0].value.id == "window_seconds"
        ):
            continue
        return False
    return True


def _first_fence_contract_violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    problems: list[str] = []
    import_origins: dict[str, str] = {}
    if any(isinstance(candidate, ast.ImportFrom) and any(alias.name == "*" for alias in candidate.names) for candidate in ast.walk(tree)):
        problems.append("wildcard-first-fence-import")
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.ImportFrom) and candidate.module:
            for alias in candidate.names:
                import_origins[alias.asname or alias.name] = f"{candidate.module}.{alias.name}"
        elif isinstance(candidate, ast.Import):
            for alias in candidate.names:
                import_origins[alias.asname or alias.name.split(".")[0]] = alias.name
    verify_matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "verify_and_extend_leader_fence"
    ]
    owner_matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "fenced_leader_transaction"
    ]
    if len(verify_matches) != 1:
        problems.append("verifier-definition-cardinality")
    if len(owner_matches) != 1:
        problems.append("transaction-owner-cardinality")
    if len(verify_matches) != 1 or len(owner_matches) != 1:
        return tuple(problems)
    verify = verify_matches[0]
    owner = owner_matches[0]
    parameter_names = {parameter.arg for parameter in (*verify.args.posonlyargs, *verify.args.args, *verify.args.kwonlyargs)}
    if any(_name_has_clock_marker(parameter) for parameter in parameter_names):
        problems.append("caller-time-parameter")
    if verify.decorator_list:
        problems.append("decorated-verifier")
    if isinstance(verify, ast.AsyncFunctionDef) or any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(verify)):
        problems.append("async-or-generator-verifier")
    if any(default is not None for function in (verify, owner) for default in (*function.args.defaults, *function.args.kw_defaults)):
        problems.append("effectful-fence-default")
    if owner.decorator_list and not (
        len(owner.decorator_list) == 1
        and isinstance(owner.decorator_list[0], ast.Name)
        and owner.decorator_list[0].id == "contextmanager"
        and import_origins.get("contextmanager") == "contextlib.contextmanager"
    ):
        problems.append("decorated-transaction-owner")

    shadowed = (
        "verify_and_extend_leader_fence"
        in {parameter.arg for parameter in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)}
        or any(
            (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "verify_and_extend_leader_fence")
            or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node is not owner
                and node.name == "verify_and_extend_leader_fence"
            )
            for node in ast.walk(owner)
        )
        or any(
            "verify_and_extend_leader_fence" in _assigned_names(target)
            for statement in tree.body
            for target in (
                tuple(statement.targets)
                if isinstance(statement, ast.Assign)
                else (statement.target,)
                if isinstance(statement, (ast.AnnAssign, ast.AugAssign))
                else ()
            )
        )
        or any(
            (
                isinstance(candidate, ast.ImportFrom)
                and any((alias.asname or alias.name) == "verify_and_extend_leader_fence" for alias in candidate.names)
            )
            or (
                isinstance(candidate, ast.Import)
                and any((alias.asname or alias.name.split(".")[0]) == "verify_and_extend_leader_fence" for alias in candidate.names)
            )
            for candidate in ast.walk(tree)
        )
    )
    if shadowed:
        problems.append("shadowed-verifier")
    dependencies = ["RunLeadershipLostError", "begin_write", "func", "run_coordination_table", "timedelta", "update"]
    if owner.decorator_list:
        dependencies.append("contextmanager")
    for dependency in dependencies:
        if _first_fence_dependency_is_rebound(tree, dependency, (verify, owner)):
            problems.append(f"shadowed-first-fence-dependency:{dependency}")
    expected_origins = {
        "RunLeadershipLostError": "elspeth.contracts.errors.RunLeadershipLostError",
        "begin_write": "elspeth.core.landscape.database.begin_write",
        "contextmanager": "contextlib.contextmanager",
        "func": "sqlalchemy.func",
        "run_coordination_table": "elspeth.core.landscape.schema.run_coordination_table",
        "timedelta": "datetime.timedelta",
        "update": "sqlalchemy.update",
    }
    for dependency, expected_origin in expected_origins.items():
        origin = import_origins.get(dependency)
        if origin is not None and origin != expected_origin:
            problems.append(f"untrusted-first-fence-dependency:{dependency}")

    transactions = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.With)
        and any(isinstance(item.context_expr, ast.Call) and _terminal_name(item.context_expr.func) == "begin_write" for item in node.items)
    ]
    if len(transactions) != 1:
        problems.append("transaction-owner-cardinality")
    else:
        transaction = transactions[0]

        def transaction_path(
            statements: Sequence[ast.stmt],
            *,
            container: ast.AST,
            field: str,
        ) -> list[tuple[Sequence[ast.stmt], int, ast.AST, str]] | None:
            for index, statement in enumerate(statements):
                if statement is transaction:
                    return [(statements, index, container, field)]
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                child_blocks: list[tuple[Sequence[ast.stmt], ast.AST, str]] = []
                for child_field in ("body", "orelse", "finalbody"):
                    child = getattr(statement, child_field, None)
                    if isinstance(child, list) and all(isinstance(item, ast.stmt) for item in child):
                        child_blocks.append((child, statement, child_field))
                if isinstance(statement, (ast.Try, ast.TryStar)):
                    child_blocks.extend((handler.body, handler, "body") for handler in statement.handlers)
                if isinstance(statement, ast.Match):
                    child_blocks.extend((case.body, case, "body") for case in statement.cases)
                for child, child_container, child_field in child_blocks:
                    nested = transaction_path(child, container=child_container, field=child_field)
                    if nested is not None:
                        return [(statements, index, container, field), *nested]
            return None

        path = transaction_path(owner.body, container=owner, field="body")
        unreachable_or_conditional = path is None
        if path is not None:
            for statements, index, container, field in path:
                preceding = statements[:index]
                if any(
                    isinstance(statement, (ast.Return, ast.Raise, ast.If, ast.For, ast.AsyncFor, ast.While, ast.Match))
                    for statement in preceding
                ):
                    unreachable_or_conditional = True
                if container is owner:
                    continue
                if isinstance(container, (ast.Try, ast.TryStar)) and field == "body":
                    continue
                unreachable_or_conditional = True
        if unreachable_or_conditional:
            problems.append("conditional-or-unreachable-transaction-owner")
        if any(
            isinstance(candidate, ast.If)
            and isinstance(candidate.test, ast.Constant)
            and not candidate.test.value
            and any(descendant is transaction for descendant in ast.walk(candidate))
            for candidate in ast.walk(owner)
        ):
            problems.append("unreachable-transaction-owner")
        transaction_nodes = {id(candidate) for candidate in ast.walk(transaction)}
        if any(
            id(candidate) not in transaction_nodes
            and isinstance(candidate, ast.Call)
            and _terminal_name(candidate.func) in {"begin", "execute"}
            for candidate in ast.walk(owner)
        ):
            problems.append("effect-before-or-outside-transaction")
        if len(transaction.items) != 1:
            problems.append("additional-transaction-context")
        item = transaction.items[0]
        transaction_connection = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else None
        context = item.context_expr
        exact_context = (
            isinstance(context, ast.Call)
            and isinstance(context.func, ast.Name)
            and context.func.id == "begin_write"
            and len(context.args) == 1
            and isinstance(context.args[0], ast.Name)
            and context.args[0].id == "engine"
            and not context.keywords
            and transaction_connection == "conn"
        )
        if not exact_context:
            problems.append("invalid-transaction-owner")
        if isinstance(context, ast.Call) and any(
            isinstance(candidate, ast.Call) and candidate is not context for argument in context.args for candidate in ast.walk(argument)
        ):
            problems.append("sql-in-transaction-context")
        if not transaction.body:
            problems.append("empty-transaction")
        else:
            first = transaction.body[0]
            valid_verifier_call = (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == "verify_and_extend_leader_fence"
            )
            if not valid_verifier_call:
                problems.append("helper-or-sql-before-fence")
            else:
                expected_keywords = {"token": "token", "verb": "verb", "window_seconds": "window_seconds"}
                actual_keywords = {keyword.arg: keyword.value for keyword in first.value.keywords if keyword.arg is not None}
                exact_call = (
                    transaction_connection is not None
                    and len(first.value.args) == 1
                    and isinstance(first.value.args[0], ast.Name)
                    and first.value.args[0].id == transaction_connection
                    and set(actual_keywords) == set(expected_keywords)
                    and all(
                        isinstance(actual_keywords[key], ast.Name) and actual_keywords[key].id == value
                        for key, value in expected_keywords.items()
                    )
                )
                if not exact_call:
                    problems.append("verifier-connection-mismatch")
            yields = [candidate for candidate in ast.walk(transaction) if isinstance(candidate, (ast.Yield, ast.YieldFrom))]
            owner_yields = [candidate for candidate in ast.walk(owner) if isinstance(candidate, (ast.Yield, ast.YieldFrom))]
            if not (
                len(yields) == 1
                and owner_yields == yields
                and isinstance(yields[0], ast.Yield)
                and isinstance(yields[0].value, ast.Name)
                and yields[0].value.id == "conn"
                and any(isinstance(statement, ast.Expr) and statement.value is yields[0] for statement in transaction.body)
            ):
                problems.append("invalid-transaction-yield")

    statements = _executable_statements(verify)
    if not statements:
        return (*problems, "empty-fence")
    first = statements[0]
    result_name: str | None = None
    execute: ast.Call | None = None
    if (
        isinstance(first, ast.Assign)
        and len(first.targets) == 1
        and isinstance(first.targets[0], ast.Name)
        and isinstance(first.value, ast.Call)
    ):
        result_name = first.targets[0].id
        execute = first.value
    if not (
        execute is not None
        and isinstance(execute.func, ast.Attribute)
        and isinstance(execute.func.value, ast.Name)
        and execute.func.value.id == "conn"
        and execute.func.attr == "execute"
        and len(execute.args) == 1
        and not execute.keywords
    ):
        problems.append("first-effect-not-direct-execute")
        return tuple(problems)

    statement = execute.args[0]
    root, methods = _statement_chain(statement)
    exact_root_update = (
        root is not None
        and isinstance(root.func, ast.Name)
        and root.func.id == "update"
        and len(root.args) == 1
        and isinstance(root.args[0], ast.Name)
        and root.args[0].id == "run_coordination_table"
        and not root.keywords
    )
    nested_sql = [
        candidate
        for candidate in ast.walk(statement)
        if isinstance(candidate, ast.Call) and _terminal_name(candidate.func) in {"delete", "insert", "select", "update"}
    ]
    if not exact_root_update or len(nested_sql) != 1 or nested_sql[0] is not root:
        problems.append("first-effect-not-seat-update")
    if not _first_fence_statement_is_effect_free(statement):
        problems.append("effectful-first-fence-expression")

    where_calls = methods.get("where", [])
    where_arguments = tuple(where_calls[0].args) if len(where_calls) == 1 and not where_calls[0].keywords else ()
    predicates = set().union(*(_full_token_predicates(argument) for argument in where_arguments)) if where_arguments else set()
    exact_predicates = len(where_arguments) == 3 and len(predicates) == 3
    for expected in (("run_id", "run_id"), ("leader_worker_id", "worker_id"), ("leader_epoch", "leader_epoch")):
        if expected not in predicates:
            problems.append(f"missing-full-token-predicate:{expected[0]}")
    if not exact_predicates:
        problems.append("nonexact-token-where")

    values_calls = methods.get("values", [])
    assignments = _local_assignments(verify)
    deadline_values = _deadline_values(values_calls[0], assignments) if len(values_calls) == 1 else ()
    if len(deadline_values) != 1:
        problems.append("missing-database-time-expression")
    else:
        database_time, caller_or_process = _deadline_expression_is_database_authoritative(deadline_values[0], verify)
        if not database_time:
            problems.append("missing-database-time-expression")
        if caller_or_process:
            problems.append("caller-or-process-deadline")

    if len(statements) < 2 or result_name is None or not isinstance(statements[1], ast.If):
        problems.append("missing-cardinality-one-refusal")
    else:
        refusal = statements[1]
        test = refusal.test
        exact_rowcount = (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.NotEq)
            and isinstance(test.left, ast.Attribute)
            and isinstance(test.left.value, ast.Name)
            and test.left.value.id == result_name
            and test.left.attr == "rowcount"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == 1
        )
        first_refusal_statement = next(
            (
                statement
                for statement in refusal.body
                if not (
                    isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)
                )
            ),
            None,
        )
        raises_lost = (
            isinstance(first_refusal_statement, ast.Raise)
            and isinstance(first_refusal_statement.exc, ast.Call)
            and _terminal_name(first_refusal_statement.exc.func) == "RunLeadershipLostError"
        )
        if not exact_rowcount or not raises_lost:
            problems.append("missing-cardinality-one-refusal")
    return tuple(problems)


_GOOD_FENCE_SOURCE = """
def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):
    result = conn.execute(
        update(run_coordination_table)
        .where(
            run_coordination_table.c.run_id == token.run_id,
            run_coordination_table.c.leader_worker_id == token.worker_id,
            run_coordination_table.c.leader_epoch == token.leader_epoch,
        )
        .values(
            leader_heartbeat_expires_at=func.current_timestamp() + window_seconds,
            updated_at=func.current_timestamp(),
        )
    )
    if result.rowcount != 1:
        raise RunLeadershipLostError()

def fenced_leader_transaction(engine, *, token, window_seconds, verb):
    with begin_write(engine) as conn:
        verify_and_extend_leader_fence(conn, token=token, window_seconds=window_seconds, verb=verb)
        yield conn
"""


def test_first_fence_shape_accepts_exact_positive_control() -> None:
    assert _first_fence_contract_violations(_GOOD_FENCE_SOURCE) == ()


def test_first_fence_shape_accepts_effect_free_dialect_specific_database_deadline_positive_control() -> None:
    source = _GOOD_FENCE_SOURCE.replace(
        "func.current_timestamp() + window_seconds",
        "(func.datetime(func.current_timestamp(), f'+{window_seconds} seconds') "
        "if conn.dialect.name == 'sqlite' else "
        "func.current_timestamp() + timedelta(seconds=window_seconds))",
        1,
    )
    assert _first_fence_contract_violations(source) == ()


def test_first_fence_shape_ignores_unrelated_function_local_dependency_names_positive_control() -> None:
    source = (
        _GOOD_FENCE_SOURCE
        + """
def unrelated(update, begin_write):
    func = object()
    run_coordination_table = object()
    return update, begin_write, func, run_coordination_table
"""
    )
    assert _first_fence_contract_violations(source) == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_GOOD_FENCE_SOURCE.replace("update(run_coordination_table)", "select(run_coordination_table)"), "first-effect-not-seat-update"),
        (
            _GOOD_FENCE_SOURCE.replace(
                "run_coordination_table.c.leader_worker_id == token.worker_id,",
                "other_table.c.leader_worker_id == token.worker_id,",
            ),
            "missing-full-token-predicate:leader_worker_id",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "    result = conn.execute(",
                "    if False:\n        result = conn.execute(",
                1,
            ),
            "first-effect-not-direct-execute",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("with begin_write(engine) as conn:", "with begin_write(engine) as conn, helper_sql():"),
            "additional-transaction-context",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "        verify_and_extend_leader_fence(conn, token=token, window_seconds=window_seconds, verb=verb)",
                "        helper_sql(conn)\n        verify_and_extend_leader_fence(conn, token=token, window_seconds=window_seconds, verb=verb)",
            ),
            "helper-or-sql-before-fence",
        ),
        (_GOOD_FENCE_SOURCE.replace("if result.rowcount != 1:", "if False:"), "missing-cardinality-one-refusal"),
        (_GOOD_FENCE_SOURCE.replace("func.current_timestamp()", "caller_now"), "missing-database-time-expression"),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
                "def verify_and_extend_leader_fence(conn, *, token, now, window_seconds, verb):",
            ),
            "caller-time-parameter",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "update(run_coordination_table)",
                "choose(select(run_coordination_table), update(run_coordination_table))",
                1,
            ),
            "first-effect-not-seat-update",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("with begin_write(engine) as conn:", "with begin_write(engine) as transaction_conn:"),
            "verifier-connection-mismatch",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "verify_and_extend_leader_fence(conn, token=token",
                "verify_and_extend_leader_fence(other_conn, token=token",
            ),
            "verifier-connection-mismatch",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "with begin_write(engine) as conn:",
                "with begin_write(engine, external_conn.execute(select(run_coordination_table))) as conn:",
            ),
            "sql-in-transaction-context",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def fenced_leader_transaction(engine, *, token, window_seconds, verb):",
                "def fenced_leader_transaction(engine, *, token, window_seconds, verb, verify_and_extend_leader_fence):",
            ),
            "shadowed-verifier",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
                "def verify_and_extend_leader_fence(conn, *, token, caller_now, window_seconds, verb):",
            ).replace(
                "leader_heartbeat_expires_at=func.current_timestamp() + window_seconds,",
                "leader_heartbeat_expires_at=caller_now,",
            ),
            "caller-or-process-deadline",
        ),
    ],
    ids=(
        "select-first",
        "partial-token",
        "nested-decoy",
        "extra-context",
        "helper-first",
        "no-rowcount",
        "no-db-time",
        "caller-now",
        "choose-select-with-update-decoy",
        "external-transaction-connection",
        "verifier-other-connection",
        "sql-in-begin-write-context",
        "injected-verifier",
        "database-time-unrelated-column-decoy",
    ),
)
def test_first_fence_shape_rejects_adversarial_bypasses(source: str, expected: str) -> None:
    assert expected in _first_fence_contract_violations(source)


def test_first_fence_rejects_token_decoys_outside_the_update_where_clause() -> None:
    source = """
def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):
    result = conn.execute(
        update(run_coordination_table)
        .where(run_coordination_table.c.run_id == attacker.run_id)
        .values(
            leader_heartbeat_expires_at=case(
                (
                    and_(
                        run_coordination_table.c.run_id == token.run_id,
                        run_coordination_table.c.leader_worker_id == token.worker_id,
                        run_coordination_table.c.leader_epoch == token.leader_epoch,
                    ),
                    func.current_timestamp() + window_seconds,
                ),
                else_=func.current_timestamp() + window_seconds,
            ),
            updated_at=func.current_timestamp(),
        )
    )
    if result.rowcount != 1:
        raise RunLeadershipLostError()

def fenced_leader_transaction(engine, *, token, window_seconds, verb):
    with begin_write(engine) as conn:
        verify_and_extend_leader_fence(conn, token=token, window_seconds=window_seconds, verb=verb)
        yield conn
"""
    violations = _first_fence_contract_violations(source)
    assert "missing-full-token-predicate:run_id" in violations
    assert "nonexact-token-where" in violations


def test_first_fence_rejects_missing_or_rebound_verifier_definition() -> None:
    missing = _GOOD_FENCE_SOURCE.replace("def verify_and_extend_leader_fence", "def deleted_verifier", 1)
    assert "verifier-definition-cardinality" in _first_fence_contract_violations(missing)

    rebound = _GOOD_FENCE_SOURCE + "\nverify_and_extend_leader_fence = attacker\n"
    assert "shadowed-verifier" in _first_fence_contract_violations(rebound)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            _GOOD_FENCE_SOURCE.replace("with begin_write(engine) as conn:", "with begin_write(probe()) as conn:"),
            "invalid-transaction-owner",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("func.current_timestamp() + window_seconds", "'current_timestamp' + window_seconds", 1),
            "missing-database-time-expression",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("func.current_timestamp() + window_seconds", "fake_current_timestamp() + window_seconds", 1),
            "missing-database-time-expression",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("        raise RunLeadershipLostError()", "        return\n        raise RunLeadershipLostError()"),
            "missing-cardinality-one-refusal",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("    )\n    if result.rowcount", "    , execution_options=probe())\n    if result.rowcount", 1),
            "first-effect-not-direct-execute",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
                "def verify_and_extend_leader_fence(conn, *, token, epoch_seconds, window_seconds, verb):",
            ).replace(
                "func.current_timestamp() + window_seconds",
                "coalesce(epoch_seconds, func.current_timestamp()) + window_seconds",
                1,
            ),
            "caller-or-process-deadline",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("with begin_write(engine) as conn:", "with begin_write(engine) as external_conn:").replace(
                "verify_and_extend_leader_fence(conn, token", "verify_and_extend_leader_fence(external_conn, token"
            ),
            "invalid-transaction-owner",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def fenced_leader_transaction(engine, *, token, window_seconds, verb):",
                "def fenced_leader_transaction(engine, *, token, window_seconds, verb, begin_write):",
            ),
            "shadowed-first-fence-dependency:begin_write",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
                "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb, update):",
            ),
            "shadowed-first-fence-dependency:update",
        ),
        (
            _GOOD_FENCE_SOURCE + "\nrun_coordination_table = attacker\n",
            "shadowed-first-fence-dependency:run_coordination_table",
        ),
        (
            _GOOD_FENCE_SOURCE + "\nfunc = attacker\n",
            "shadowed-first-fence-dependency:func",
        ),
        (
            "from attacker import func\n" + _GOOD_FENCE_SOURCE,
            "untrusted-first-fence-dependency:func",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("func.current_timestamp() + window_seconds", "func.current_timestamp()", 1),
            "missing-database-time-expression",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "func.current_timestamp() + window_seconds",
                "coalesce(literal('2099-01-01'), func.current_timestamp()) + window_seconds",
                1,
            ),
            "missing-database-time-expression",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("updated_at=func.current_timestamp()", "updated_at=probe()"),
            "effectful-first-fence-expression",
        ),
        (
            _GOOD_FENCE_SOURCE.replace("        yield conn", "        yield external_conn"),
            "invalid-transaction-yield",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
                "@attacker\ndef verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
            ),
            "decorated-verifier",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "token=token, window_seconds=window_seconds",
                "token=attacker, window_seconds=0",
            ),
            "verifier-connection-mismatch",
        ),
        (
            _GOOD_FENCE_SOURCE + "\nglobals()['update'] = attacker\n",
            "shadowed-first-fence-dependency:update",
        ),
        (
            _GOOD_FENCE_SOURCE.replace(
                "func.current_timestamp() + window_seconds",
                "func.datetime(func.current_timestamp(), f'-{window_seconds} years')",
                1,
            ),
            "missing-database-time-expression",
        ),
    ],
    ids=(
        "begin-write-probe",
        "database-name-string",
        "fake-current-timestamp",
        "unreachable-raise",
        "execute-keyword-side-effect",
        "epoch-seconds-coalesced",
        "external-connection-yield",
        "injected-begin-write",
        "rebound-update",
        "rebound-table",
        "rebound-sql-function-namespace",
        "untrusted-sql-function-import",
        "missing-window",
        "dead-database-fallback",
        "effectful-update-argument",
        "external-yield-only",
        "decorated-verifier",
        "wrong-verifier-token-and-window",
        "globals-update-rebind",
        "negative-years-modifier",
    ),
)
def test_first_fence_rejects_strict_contract_bypasses(source: str, expected: str) -> None:
    assert expected in _first_fence_contract_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\ndef live_leader(): return datetime.now()",
        "import datetime as dt\ndef live_leader(): return dt.datetime.now()",
        "from datetime import datetime\nclock = datetime.now\ndef live_leader(): return clock()",
        "from datetime import datetime\nclock = getattr(datetime, 'now')\ndef live_leader(): return clock()",
        "from datetime import datetime\ndef wall(): return datetime.now()\ndef live_leader(): return wall()",
        "from datetime import datetime\nclock = lambda: datetime.now()\ndef live_leader(): return clock()",
        "from elspeth.core.landscape._helpers import now as wall\ndef takeover_expired(): return wall()",
        "from datetime import datetime\ndef wall():\n    local = datetime.now\n    value = local()\n    return value\ndef live_leader(): return wall()",
        "import functools\nfrom datetime import datetime\nclock = functools.partial(datetime.now)\ndef live_leader(): return clock()",
        "from datetime import datetime\nclock, other = datetime.today, object\ndef live_leader(): return clock()",
        "from datetime import date as day\ndef live_leader(): return day.today()",
        "from datetime import datetime\nclass Authority:\n    def __init__(self): self.wall = datetime.now\n    def live_leader(self): return self.wall()",
        "from datetime import datetime\nclass Authority:\n    def wall(self): return datetime.now()\n    def live_leader(self): return self.wall()",
    ],
    ids=(
        "direct",
        "import-alias",
        "callable-alias",
        "getattr",
        "wrapper",
        "lambda",
        "helper-alias",
        "local-then-return",
        "partial",
        "tuple-alias",
        "date-today-alias",
        "instance-held-callable",
        "self-method-wrapper",
    ),
)
def test_scanner_rejects_process_clock_aliases_and_wrappers(source: str) -> None:
    assert any(violation.kind == "authority-returns-nondatabase-clock" for violation in _scan_source(source))


def test_structural_inventory_catches_newly_named_authority_and_wrapper() -> None:
    source = """
from datetime import datetime

def renew_worker_custody(conn):
    cutoff = datetime.now()
    return conn.execute(
        update(run_workers_table)
        .where(run_workers_table.c.heartbeat_expires_at < cutoff)
        .values(heartbeat_expires_at=cutoff)
    )

def refresh_membership(conn):
    return renew_worker_custody(conn)
"""
    boundaries = _discover_authority_boundaries({"src/elspeth/core/landscape/new_custody.py": source})
    identities = {(boundary.path, boundary.symbol) for boundary in boundaries}
    assert ("src/elspeth/core/landscape/new_custody.py", "renew_worker_custody") in identities
    assert ("src/elspeth/core/landscape/new_custody.py", "refresh_membership") in identities
    kinds = {violation.kind for violation in _scan_source(source, path="src/elspeth/core/landscape/new_custody.py")}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


@pytest.mark.parametrize(
    "body",
    [
        "conn.exec_driver_sql('UPDATE run_workers SET heartbeat_expires_at = ?', (datetime.now(),))",
        "conn.execute(update(run_workers_table).values({'heartbeat_expires_at': datetime.now()}))",
        "conn.execute(update(run_workers_table).values({run_workers_table.c.heartbeat_expires_at: datetime.now()}))",
        "payload = {'heartbeat_expires_at': datetime.now()}\n    conn.execute(update(run_workers_table).values(payload))",
        "payload = {}\n    payload['heartbeat_expires_at'] = datetime.now()\n    conn.execute(update(run_workers_table).values(**payload))",
        "payload = {}\n    payload[run_workers_table.c.heartbeat_expires_at] = datetime.now()\n    conn.execute(update(run_workers_table).values(payload))",
        "payload = {}\n    payload.update({'heartbeat_expires_at': datetime.now()})\n    conn.execute(update(run_workers_table).values(**payload))",
        "payload = {}\n    payload.update(heartbeat_expires_at=datetime.now())\n    conn.execute(update(run_workers_table).values(**payload))",
        "statement = 'UPDATE run_workers SET heartbeat_expires_at = :cutoff'\n    conn.exec_driver_sql(statement, {'cutoff': datetime.now()})",
        "prefix = 'UPDATE run_workers SET '\n    statement = prefix + 'heartbeat_expires_at = :cutoff'\n    conn.exec_driver_sql(statement, {'cutoff': datetime.now()})",
        "column = 'heartbeat_expires_at'\n    statement = f'UPDATE run_workers SET {column} = :cutoff'\n    conn.exec_driver_sql(statement, {'cutoff': datetime.now()})",
        "column = choose_column()\n    statement = f'UPDATE run_workers SET {column} = :cutoff'\n    conn.exec_driver_sql(statement, {'cutoff': datetime.now()})",
        "execute_raw = conn.exec_driver_sql\n    execute_raw('UPDATE run_workers SET heartbeat_expires_at = :cutoff', {'cutoff': datetime.now()})",
        "conn.execute(text('UPDATE run_workers SET heartbeat_expires_at = :cutoff'), {'cutoff': datetime.now()})",
    ],
    ids=(
        "raw-exec-driver-sql",
        "literal-mapping",
        "column-key-mapping",
        "mapping-variable",
        "mapping-subscript-assignment",
        "mapping-column-subscript-assignment",
        "mapping-update-and-kwargs",
        "mapping-update-keyword",
        "raw-sql-name-alias",
        "raw-sql-concatenation",
        "raw-sql-f-string",
        "raw-sql-dynamic-column-f-string",
        "aliased-exec-driver-sql",
        "text-update-and-params",
    ),
)
def test_structural_inventory_and_scanner_reject_raw_or_mapping_deadline_mutations(body: str) -> None:
    source = f"""
from datetime import datetime

def rotate_custody(conn):
    {body}
"""
    path = "src/elspeth/core/landscape/raw_or_mapping.py"
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: source})}
    assert (path, "rotate_custody") in identities
    kinds = {violation.kind for violation in _scan_source(source, path=path)}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_structural_inventory_and_scanner_fail_closed_on_unknown_authority_kwargs_mapping() -> None:
    path = "src/elspeth/core/landscape/dynamic_mapping.py"
    source = """
def rotate_custody(conn, payload):
    conn.execute(update(run_workers_table).values(**payload))
"""
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: source})}
    assert (path, "rotate_custody") in identities
    assert "dynamic-authority-mapping" in {violation.kind for violation in _scan_source(source, path=path)}


def test_raw_sql_select_with_updated_at_name_is_not_misclassified_positive_control() -> None:
    source = """
def inspect(conn):
    return conn.exec_driver_sql(
        'SELECT heartbeat_expires_at, updated_at FROM run_workers'
    )
"""
    assert _scan_source(source) == ()


def test_aliased_sqlalchemy_text_preserves_raw_authority_classification() -> None:
    path = "src/elspeth/core/landscape/aliased_text.py"
    unsafe = """
from datetime import datetime
from sqlalchemy import text as sql_text

def rotate_custody(conn):
    conn.execute(
        sql_text('UPDATE run_workers SET heartbeat_expires_at=:deadline'),
        {'deadline': datetime.now()},
    )
"""
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: unsafe})}
    assert (path, "rotate_custody") in identities
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(unsafe, path=path)}

    safe = """
from sqlalchemy import text as sql_text

def rotate_custody(conn):
    conn.execute(sql_text(
        'UPDATE run_workers SET heartbeat_expires_at=CURRENT_TIMESTAMP'
    ))
"""
    assert _scan_source(safe, path=path) == ()


def test_structural_inventory_follows_cross_file_authority_wrappers() -> None:
    authority_path = "src/elspeth/core/landscape/custody_impl.py"
    wrapper_path = "src/elspeth/core/landscape/custody_facade.py"
    sources = {
        authority_path: """
def rotate_custody(conn, observed):
    return conn.execute(update(run_workers_table).values(heartbeat_expires_at=observed))
""",
        wrapper_path: """
from elspeth.core.landscape.custody_impl import rotate_custody

def refresh_membership(conn, observed):
    return rotate_custody(conn, observed)
""",
    }
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries(sources)}
    assert (authority_path, "rotate_custody") in identities
    assert (wrapper_path, "refresh_membership") in identities
    wrapper_violations = [violation for violation in _scan_sources(sources) if violation.path == wrapper_path]
    assert "caller-clock-positional-forwarding" in {violation.kind for violation in wrapper_violations}


def test_structural_inventory_follows_cross_file_callable_alias_wrappers() -> None:
    authority_path = "src/elspeth/core/landscape/custody_impl.py"
    facade_path = "src/elspeth/core/landscape/custody_facade.py"
    caller_path = "src/elspeth/core/landscape/custody_entrypoint.py"
    sources = {
        authority_path: """
def rotate_custody(conn, observed):
    return conn.execute(update(run_workers_table).values(heartbeat_expires_at=observed))
""",
        facade_path: """
from elspeth.core.landscape.custody_impl import rotate_custody
delegated = rotate_custody

def refresh_membership(conn, observed):
    return delegated(conn, observed)
""",
        caller_path: """
from elspeth.core.landscape.custody_facade import refresh_membership as refresh

def custody_entrypoint(conn, observed):
    return refresh(conn, observed)
""",
    }
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries(sources)}
    assert {
        (authority_path, "rotate_custody"),
        (facade_path, "refresh_membership"),
        (caller_path, "custody_entrypoint"),
    } <= identities


def test_structural_inventory_follows_cross_file_object_method_wrappers() -> None:
    authority_path = "src/elspeth/core/landscape/custody_service.py"
    wrapper_path = "src/elspeth/core/landscape/custody_facade.py"
    sources = {
        authority_path: """
class CustodyService:
    def renew_custody(self, conn, observed):
        return conn.execute(update(run_workers_table).values(heartbeat_expires_at=observed))
""",
        wrapper_path: """
from elspeth.core.landscape.custody_service import CustodyService

service = CustodyService()

def refresh_membership(conn, observed):
    return service.renew_custody(conn, observed)
""",
    }
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries(sources)}
    assert (authority_path, "CustodyService.renew_custody") in identities
    assert (wrapper_path, "refresh_membership") in identities


def test_structural_inventory_keeps_same_named_methods_class_qualified_positive_control() -> None:
    path = "src/elspeth/core/landscape/custody.py"
    source = """
class Authority:
    def renew(self, conn, observed):
        conn.execute(update(run_workers_table).values(heartbeat_expires_at=observed))

class Harmless:
    def renew(self):
        return 'display-only'

    def wrapper(self):
        return self.renew()
"""
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: source})}
    assert (path, "Authority.renew") in identities
    assert (path, "Harmless.wrapper") not in identities


def test_required_authority_boundary_injection_cannot_mask_a_deleted_definition() -> None:
    path = "src/elspeth/core/checkpoint/manager.py"
    missing = _missing_required_boundaries({path: "def unrelated(): pass"})
    assert (path, "CheckpointManager._fenced_or_plain_write") in missing
    assert not _discover_authority_boundaries({path: "def unrelated(): pass"})


@pytest.mark.parametrize(
    "source",
    [
        "def call(repo, wall): repo.live_leader(run_id='r', now=wall)",
        "def call(repo, wall): fn = repo.worker_heartbeat; fn(worker_id='w', now=wall, window_seconds=1)",
        "def call(repo, wall): getattr(repo, 'acquire_run_leadership')(run_id='r', worker_id='w', now=wall, window_seconds=1)",
        "def call(repo, wall): kwargs = {'run_id': 'r', 'now': wall}; repo.live_leader(**kwargs)",
        "def call(repo, cutoff): alias = cutoff; repo.live_leader('r', alias)",
        "def call(repo, moment: datetime): repo.live_leader('r', moment)",
    ],
    ids=("direct", "callable-alias", "getattr", "kwargs", "positional-alias", "typed-positional"),
)
def test_scanner_rejects_caller_clock_forwarding_escapes(source: str) -> None:
    assert any(violation.kind.startswith("caller-clock") for violation in _scan_source(source))


def test_scanner_rejects_authority_star_args_and_neutral_getattr() -> None:
    star_args = "def call(repo, args): repo.live_leader(*args)"
    assert "dynamic-authority-args" in {violation.kind for violation in _scan_source(star_args)}

    neutral_getattr = """
def live_leader(target, method, observed):
    return getattr(target, method)(observed)
"""
    assert "dynamic-authority-getattr" in {violation.kind for violation in _scan_source(neutral_getattr)}


@pytest.mark.parametrize(
    "source",
    [
        """
def rotate_custody(conn, observed):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=choose(func.current_timestamp(), observed)
        )
    )
""",
        """
def rotate_custody(conn, marker):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce(marker, func.current_timestamp())
        )
    )
""",
    ],
    ids=("neutral-observed-choice", "caller-coalesce"),
)
def test_scanner_rejects_neutral_caller_fallbacks_even_with_database_time(source: str) -> None:
    kinds = {violation.kind for violation in _scan_source(source)}
    assert "caller-clock-authority" in kinds


def test_scanner_rejects_instance_process_clock_even_when_coalesced_with_database_time() -> None:
    source = """
from datetime import datetime

class Custody:
    def __init__(self):
        self.wall = datetime.now

    def rotate(self, conn):
        conn.execute(
            update(run_workers_table).values(
                heartbeat_expires_at=coalesce(self.wall(), func.current_timestamp())
            )
        )
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert "process-clock-authority" in kinds


def test_scanner_rejects_production_path_instance_held_process_clock() -> None:
    path = "src/elspeth/core/landscape/new_custody.py"
    source = """
from datetime import datetime

class Custody:
    def __init__(self):
        self.clock = datetime.now

    def rotate(self, conn):
        conn.execute(
            update(run_workers_table).values(
                heartbeat_expires_at=self.clock()
            )
        )
"""
    kinds = {violation.kind for violation in _scan_sources({path: source})}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


@pytest.mark.parametrize("unsafe_class_first", [True, False], ids=("unsafe-first", "unsafe-last"))
def test_scanner_keeps_same_named_method_provenance_class_specific(unsafe_class_first: bool) -> None:
    unsafe = """
class UnsafeCustody:
    def database_now(self):
        return datetime.now()

    def rotate(self, conn):
        conn.execute(update(run_workers_table).values(heartbeat_expires_at=self.database_now()))
"""
    safe = """
class SafeCustody:
    def database_now(self):
        return func.current_timestamp()
"""
    source = "from datetime import datetime\n" + (unsafe + safe if unsafe_class_first else safe + unsafe)
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_scanner_does_not_apply_later_rebinding_retroactively() -> None:
    source = """
from datetime import datetime

def rotate(conn):
    clock = datetime.now
    deadline = clock()
    clock = func.current_timestamp
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=deadline))
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "absolute_name",
    ["status", "epoch_seconds", "timeout_at", "observed", "worker_epoch_seconds", "owner_timeout_at"],
)
def test_scanner_resolves_absolute_parameter_aliases_at_deadline_sinks(absolute_name: str) -> None:
    source = f"""
def rotate(conn, {absolute_name}):
    deadline = coalesce({absolute_name}, func.current_timestamp())
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=deadline))
"""
    assert "caller-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize("absolute_name", ["observed", "epoch_seconds", "timeout_at"])
def test_scanner_resolves_explicit_temporal_aliases_forwarded_to_authority(absolute_name: str) -> None:
    source = f"""
def forward(repo, {absolute_name}):
    value = {absolute_name}
    repo.live_leader(value)
"""
    assert any(violation.kind.startswith("caller-clock") for violation in _scan_source(source))


def test_scanner_fails_closed_when_opposite_branches_bind_different_clock_domains() -> None:
    source = """
from datetime import datetime

def rotate(conn, trusted):
    if trusted:
        clock = func.current_timestamp
    else:
        clock = datetime.now
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=clock()))
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_scanner_classifies_cross_file_database_named_process_wrapper_by_provenance() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: "from datetime import datetime\ndef database_now(): return datetime.now()",
        caller_path: """
from elspeth.core.landscape.clock_helper import database_now

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_scanner_classifies_cross_file_module_callable_alias_by_provenance() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: "from datetime import datetime\nclock = datetime.now\ndef database_now(): return clock()",
        caller_path: """
from elspeth.core.landscape.clock_helper import database_now

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


@pytest.mark.parametrize(
    "alias_setup",
    [
        "base = datetime.now\nclock = base",
        "import functools\nclock = functools.partial(datetime.now)",
        "clock = getattr(datetime, 'now')",
    ],
    ids=("multi-hop", "partial", "getattr"),
)
def test_scanner_rejects_cross_file_process_aliases_masked_by_database_fallback(alias_setup: str) -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: f"""
from datetime import datetime
{alias_setup}

def database_now():
    return coalesce(clock(), func.current_timestamp())
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import database_now

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_scanner_classifies_cross_file_process_object_method_by_provenance() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
from datetime import datetime
class ClockService:
    def database_now(self):
        return datetime.now()
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import ClockService
service = ClockService()

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=service.database_now()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_scanner_allows_generic_local_and_cross_file_database_clock_wrappers_positive_control() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: "def trusted_clock(): return func.current_timestamp()",
        caller_path: """
from elspeth.core.landscape.clock_helper import trusted_clock

def local_clock():
    return func.current_timestamp()

def rotate(conn):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=choose(trusted_clock(), local_clock())
        )
    )
""",
    }
    assert _scan_sources(sources) == ()


def test_scanner_does_not_cross_contaminate_same_named_class_and_module_wrappers_positive_control() -> None:
    source = """
from datetime import datetime

def database_now():
    return func.current_timestamp()

class Unsafe:
    def database_now(self):
        return datetime.now()

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
"""
    assert _scan_source(source) == ()


def test_scanner_rejects_process_callable_passed_through_parameterized_wrapper() -> None:
    source = """
from datetime import datetime

def apply(clock):
    return clock()

def rotate(conn):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce(apply(datetime.now), func.current_timestamp())
        )
    )
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_scanner_allows_cross_file_database_wrapper_with_unreturned_forensic_process_time() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
from datetime import datetime

def database_now():
    recorded_at = datetime.now()
    record_forensics(recorded_at)
    return func.current_timestamp()
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import database_now

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
""",
    }
    assert _scan_sources(sources) == ()


@pytest.mark.parametrize(
    "source",
    [
        """
class Custody:
    def wall(self):
        return self.other()

    def other(self):
        return self.wall()

    def rotate(self, conn):
        conn.execute(
            update(run_workers_table).values(
                heartbeat_expires_at=coalesce(self.wall(), func.current_timestamp())
            )
        )
""",
        """
clock = other
other = clock

def rotate(conn):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce(clock(), func.current_timestamp())
        )
    )
""",
    ],
    ids=("cyclic-instance-methods", "cyclic-module-aliases"),
)
def test_scanner_provenance_cycles_terminate_and_fail_closed(source: str) -> None:
    kinds = {violation.kind for violation in _scan_source(source)}
    assert "cyclic-clock-provenance" in kinds


def test_scanner_allows_duration_forwarding_and_forensic_mapping_positive_controls() -> None:
    source = """
from datetime import datetime

def call(repo, timeout_seconds):
    repo.live_leader(window_seconds=timeout_seconds)

def rotate(conn, timeout_seconds):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=func.current_timestamp() + timeout_seconds
        )
    )

def rotate_with_alias(conn, timeout_seconds):
    deadline = func.current_timestamp() + timeout_seconds
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=deadline
        )
    )

def record_event(conn):
    audit = {}
    audit['recorded_at'] = datetime.now()
    conn.execute(insert(events).values(audit))
"""
    assert _scan_source(source) == ()


def test_scanner_allows_ordinary_authority_payload_parameters_positive_control() -> None:
    source = """
def forward(repo, row_index, data, source_row_index, quarantined, plan, claim, verb, runtime_config):
    repo.live_leader(row_index, data, source_row_index, quarantined, plan, claim, runtime_config, verb=verb)
"""
    assert _scan_source(source) == ()


@pytest.mark.parametrize("absolute_name", ["status", "epoch_seconds", "timeout_at"])
def test_scanner_rejects_neutrally_named_absolute_deadline_fallbacks(absolute_name: str) -> None:
    source = f"""
def rotate(conn, {absolute_name}):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce({absolute_name}, func.current_timestamp())
        )
    )
"""
    assert "caller-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize("fake_database_time", ["'current_timestamp'", "fake_current_timestamp()"])
def test_scanner_rejects_database_clock_name_decoys(fake_database_time: str) -> None:
    source = f"""
def rotate(conn):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at={fake_database_time}
        )
    )
"""
    assert "missing-database-time" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "comparison",
    [
        "run_workers_table.c.heartbeat_expires_at.op('<')(datetime.now())",
        "run_workers_table.c.heartbeat_expires_at.between(marker, func.current_timestamp())",
    ],
    ids=("sqlalchemy-op-process-clock", "sqlalchemy-between-caller-fallback"),
)
def test_scanner_rejects_sqlalchemy_operator_clock_escapes(comparison: str) -> None:
    source = f"""
from datetime import datetime

def rotate(conn, marker):
    return conn.execute(select(run_workers_table).where({comparison}))
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert kinds & {"caller-clock-authority", "process-clock-authority"}


@pytest.mark.parametrize(
    "binding",
    [
        "predicate = run_workers_table.c.heartbeat_expires_at.op('<')\n    expression = predicate(datetime.now())",
        "predicate = run_workers_table.c.heartbeat_expires_at.between\n    expression = predicate(marker, func.current_timestamp())",
    ],
    ids=("aliased-op", "aliased-between"),
)
def test_scanner_rejects_aliased_sqlalchemy_operator_clock_escapes(binding: str) -> None:
    source = f"""
from datetime import datetime

def rotate(conn, marker):
    {binding}
    return conn.execute(select(run_workers_table).where(expression))
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert kinds & {"caller-clock-authority", "process-clock-authority"}


def test_scanner_fails_closed_on_dynamic_getattr_and_kwargs() -> None:
    source = """
from datetime import datetime

def live_leader(repo, method_name, options):
    clock = getattr(datetime, method_name)
    cutoff = clock()
    getattr(repo, method_name)(now=cutoff)
    repo.live_leader(**options)
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert {"dynamic-authority-getattr", "dynamic-authority-kwargs"} <= kinds
    assert "process-clock-authority" in kinds


def test_scanner_allows_unrelated_dynamic_client_dispatch_positive_control() -> None:
    source = """
def call_cloud(client, method_name, kwargs):
    return getattr(client, method_name)(**kwargs)
"""
    assert _scan_source(source) == ()


def test_scanner_rejects_injected_or_forensic_authority_clocks() -> None:
    source = """
def build(repo, clock):
    RunHeartbeatThread(repo, token='t', now_fn=clock)

def inspect(row, recorded_at):
    return row.lease_expires_at < recorded_at
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert kinds == {"forensic-clock-authority", "injected-process-clock", "missing-database-time"}


def test_scanner_allows_database_clock_and_forensic_recording_positive_control() -> None:
    source = """
from datetime import datetime

def live_leader(conn, run_id):
    database_now = read_landscape_transaction_time(conn)
    return conn.execute(select(seat).where(seat.c.expires_at > database_now))

def record_event(conn):
    forensic_timestamp = datetime.now()
    conn.execute(insert(events).values(recorded_at=forensic_timestamp))
"""
    assert _scan_source(source) == ()


def test_scanner_allows_separate_unpassed_clock_domains_positive_control() -> None:
    source = """
def adapter(session_authority, landscape_authority):
    session_authority.verify()
    landscape_authority.verify()
"""
    assert _scan_source(source) == ()


def test_scanner_rejects_deliberately_divergent_clock_domains() -> None:
    source = """
def compare(conn):
    sessions_now = read_sessions_database_time(conn)
    landscape_now = read_landscape_transaction_time(conn)
    return sessions_now < landscape_now
"""
    assert {violation.kind for violation in _scan_source(source)} == {"cross-database-clock-comparison"}


@pytest.mark.parametrize(
    "source",
    [
        """
def call(landscape_authority, conn):
    sessions_now = read_sessions_database_time(conn)
    landscape_authority.verify(database_time=sessions_now)
""",
        """
def call(landscape_authority, session_store, conn):
    observed = session_store._database_now(conn)
    landscape_authority.verify_and_extend_leader_fence(observed)
""",
        """
from clocks import read_sessions_database_time as database_now

def call(landscape_authority, conn):
    observed = database_now(conn)
    landscape_authority.verify_and_extend_leader_fence(observed)
""",
        """
def call(authority, context):
    authority.verify_and_extend_leader_fence(
        coalesce(context.sessions_database_now(), func.current_timestamp())
    )
""",
        """
def call(authority, context, conn):
    authority.verify_and_extend_leader_fence(
        coalesce(
            context.sessions_database_now(),
            read_landscape_database_time(conn),
        )
    )
""",
    ],
    ids=(
        "named-helper",
        "receiver-preserved",
        "misleading-import-alias",
        "neutral-receiver-coalesced-context-clock",
        "neutral-receiver-mixed-domains",
    ),
)
def test_scanner_rejects_cross_domain_clock_forwarding(source: str) -> None:
    assert "cross-database-clock-forwarding" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "sources",
    [
        {"src/elspeth/core/landscape/new_helper.py": "from elspeth.web.coordination.repository import database_now"},
        {"src/elspeth/core/landscape/new_helper.py": "from ...web.sessions.repository import database_now"},
        {"src/elspeth/core/landscape/__init__.py": "from ...web.sessions.repository import database_now"},
        {"src/elspeth/core/landscape/__init__.py": "from elspeth.web.coordination.repository import database_now as clock"},
        {"src/elspeth/core/landscape/new_helper.py": "from elspeth.web import coordination"},
        {
            "src/elspeth/core/landscape/new_helper.py": (
                "from importlib import import_module\nclock = import_module('elspeth.web.sessions.repository')"
            )
        },
        {
            "src/elspeth/core/landscape/new_helper.py": (
                "import importlib\nload = importlib.import_module\nclock = load('elspeth.web.sessions.repository')"
            )
        },
        {"src/elspeth/core/landscape/new_helper.py": "clock = __import__('elspeth.web.sessions.repository')"},
        {
            "src/elspeth/core/landscape/new_helper.py": "from elspeth.core.shared_clock import database_now",
            "src/elspeth/core/shared_clock.py": "from elspeth.web.sessions.repository import database_now",
        },
    ],
    ids=(
        "new-helper",
        "relative-import",
        "relative-package-reexport",
        "package-reexport",
        "parent-package-reexport",
        "dynamic-import",
        "aliased-dynamic-import",
        "dunder-import",
        "transitive-shared-reexport",
    ),
)
def test_sessions_import_guard_scans_new_helpers_relative_imports_and_reexports(sources: dict[str, str]) -> None:
    assert _sessions_import_violations(sources)


def test_sessions_import_guard_allows_landscape_local_relative_import_positive_control() -> None:
    sources = {"src/elspeth/core/landscape/new_helper.py": "from .database import begin_read"}
    assert _sessions_import_violations(sources) == ()


def test_structural_inventory_discovers_sqlalchemy_operator_only_authority_boundary() -> None:
    path = "src/elspeth/core/landscape/new_custody.py"
    source = """
def retire(conn):
    conn.execute(
        update(run_workers_table)
        .where(
            run_workers_table.c.heartbeat_expires_at.between(
                func.current_timestamp(), func.current_timestamp()
            )
        )
        .values(status='dead')
    )
"""
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: source})}
    assert (path, "retire") in identities


@pytest.mark.parametrize(
    "assignment",
    ["self.clock = datetime.now", "self.clock = functools.partial(datetime.now)"],
    ids=("direct", "partial"),
)
def test_cross_file_stateful_process_wrapper_cannot_hide_behind_database_fallback(assignment: str) -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: f"""
import functools
from datetime import datetime

class ClockService:
    def __init__(self):
        {assignment}

    def database_now(self):
        return coalesce(self.clock(), func.current_timestamp())
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import ClockService
service = ClockService()

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=service.database_now()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_cross_file_unknown_and_cyclic_clock_wrappers_fail_closed() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
clock = other
other = clock

def database_now():
    return coalesce(clock(), func.current_timestamp())
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import database_now

def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
""",
    }
    assert "unresolved-clock-provenance" in {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}


@pytest.mark.parametrize(
    "setup",
    [
        "if (wall := datetime.now): pass",
        "for wall in [datetime.now]: pass",
        "match datetime.now:\n        case wall: pass",
    ],
    ids=("walrus", "for-target", "match-capture"),
)
def test_flow_sensitive_bindings_cannot_hide_process_clock(setup: str) -> None:
    source = f"""
from datetime import datetime

def rotate(conn):
    {setup}
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce(wall(), func.current_timestamp())
        )
    )
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_setattr_instance_clock_binding_cannot_hide_process_clock() -> None:
    source = """
from datetime import datetime

class Custody:
    def __init__(self):
        setattr(self, 'wall', datetime.now)

    def rotate(self, conn):
        conn.execute(
            update(run_workers_table).values(
                heartbeat_expires_at=coalesce(self.wall(), func.current_timestamp())
            )
        )
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def worker_heartbeat(conn, sql, observed): conn.exec_driver_sql(sql, {'x': observed})",
            "dynamic-authority-raw-sql",
        ),
        (
            """
def build_sql(): return 'UPDATE run_workers SET heartbeat_expires_at=:x'
def worker_heartbeat(conn, observed): conn.exec_driver_sql(build_sql(), {'x': observed})
""",
            "dynamic-authority-raw-sql",
        ),
        (
            """
def worker_heartbeat(conn, observed):
    conn.exec_driver_sql(
        'UPDATE run_workers SET heartbeat_expires_at=COALESCE(:x,CURRENT_TIMESTAMP)',
        {'x': observed},
    )
""",
            "caller-clock-authority",
        ),
        (
            """
def worker_heartbeat(conn):
    conn.exec_driver_sql(
        \"UPDATE run_workers SET heartbeat_expires_at=:x -- CURRENT_TIMESTAMP\",
        {'x': 1},
    )
""",
            "missing-database-time",
        ),
        (
            """
from datetime import datetime
def worker_heartbeat(conn):
    sql = ' '.join(['REPLACE INTO run_workers', '(heartbeat_expires_at)', 'VALUES (:x)'])
    conn.__getattribute__('exec_driver_sql')(sql, {'x': datetime.now()})
""",
            "process-clock-authority",
        ),
    ],
    ids=("unresolved", "helper-returned", "neutral-bind", "comment-decoy", "replace-join-dunder"),
)
def test_raw_sql_authority_paths_fail_closed(source: str, expected: str) -> None:
    assert expected in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "body",
    [
        "conn.execute(statement, {'heartbeat_expires_at': datetime.now()})",
        "conn.execute(make_statement(sql), {'heartbeat_expires_at': datetime.now()})",
    ],
    ids=("dynamic-execute", "constructed-dynamic-execute"),
)
def test_dynamic_sqlalchemy_authority_execute_fails_closed(body: str) -> None:
    path = "src/elspeth/core/landscape/dynamic_execute.py"
    source = f"""
from datetime import datetime
def worker_heartbeat(conn, statement, sql):
    {body}
"""
    identities = {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: source})}
    assert (path, "worker_heartbeat") in identities
    kinds = {violation.kind for violation in _scan_source(source, path=path)}
    assert {"dynamic-authority-raw-sql", "process-clock-authority"} <= kinds


@pytest.mark.parametrize(
    "mutation",
    [
        "column = 'heartbeat_expires_at'; payload[column] = datetime.now()",
        "payload |= {'heartbeat_expires_at': datetime.now()}",
        "payload.setdefault('heartbeat_expires_at', datetime.now())",
        "column = dynamic_key(); payload[column] = datetime.now()",
    ],
    ids=("aliased-key", "mapping-or", "setdefault", "dynamic-key"),
)
def test_mapping_mutations_cannot_hide_process_deadlines(mutation: str) -> None:
    source = f"""
from datetime import datetime

def rotate(conn):
    table = run_workers_table
    payload = {{}}
    {mutation}
    conn.execute(update(table).values(**payload))
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert kinds & {"dynamic-authority-mapping", "process-clock-authority"}


@pytest.mark.parametrize(
    "predicate",
    [
        "factory = deadline.op; pred = factory('<'); condition = pred(datetime.now())",
        "pred = getattr(deadline, 'between'); condition = pred(datetime.now(), datetime.now())",
    ],
    ids=("op-factory", "getattr-between"),
)
def test_aliased_sqlalchemy_ordering_operators_remain_authority_boundaries(predicate: str) -> None:
    source = f"""
from datetime import datetime

def retire(conn):
    deadline = run_workers_table.c.heartbeat_expires_at
    {predicate}
    conn.execute(update(run_workers_table).where(condition).values(status='dead'))
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


@pytest.mark.parametrize(
    "source",
    [
        "from attacker import *\n" + _GOOD_FENCE_SOURCE,
        "globals().update(begin_write=attacker)\n" + _GOOD_FENCE_SOURCE,
        "globals().update({'update': attacker})\n" + _GOOD_FENCE_SOURCE,
        _GOOD_FENCE_SOURCE.replace("def fenced_leader_transaction", "@attacker\ndef fenced_leader_transaction"),
        _GOOD_FENCE_SOURCE.replace("        yield conn", "        yield external_conn\n        yield conn"),
        _GOOD_FENCE_SOURCE.replace("        yield conn", "        yield conn\n        yield external_conn"),
    ],
    ids=("wildcard", "globals-keyword", "globals-mapping", "decorated-owner", "yield-before", "yield-after"),
)
def test_first_fence_rejects_dynamic_dependencies_and_external_control_paths(source: str) -> None:
    assert _first_fence_contract_violations(source)


def test_sessions_import_guard_resolves_relative_dynamic_import_package() -> None:
    sources = {
        "src/elspeth/core/landscape/new_helper.py": (
            "from importlib import import_module\nclock = import_module('.repository', package='elspeth.web.sessions')"
        )
    }
    assert _sessions_import_violations(sources)


@pytest.mark.parametrize("factory", ["operator.attrgetter('now')(datetime)", "vars(datetime)['now']"], ids=("attrgetter", "vars"))
def test_process_clock_higher_order_factories_are_rejected(factory: str) -> None:
    source = f"""
import operator
from datetime import datetime

def rotate(conn):
    clock = {factory}
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce(clock(), func.current_timestamp())
        )
    )
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_nested_unused_database_return_cannot_bless_attacker_clock_wrapper() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
def fake_database_time():
    def unused():
        return func.current_timestamp()
    return attacker_clock()
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import fake_database_time
def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=fake_database_time()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "unresolved-clock-provenance"} <= kinds


@pytest.mark.parametrize(
    "caller_import",
    [
        "from . import clock_helper\nclock = clock_helper.database_now",
        "from elspeth.core.landscape import database_now\nclock = database_now",
        "import importlib\nclock = importlib.import_module('elspeth.core.landscape.clock_helper').database_now",
    ],
    ids=("from-dot", "package-reexport", "dynamic-module"),
)
def test_cross_file_process_wrappers_survive_import_indirection(caller_import: str) -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: "from datetime import datetime\ndef database_now(): return datetime.now()",
        "src/elspeth/core/landscape/__init__.py": "from .clock_helper import database_now",
        caller_path: f"""
{caller_import}
def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=clock()))
""",
    }
    kinds = {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_sessions_clock_hidden_in_database_fallback_is_rejected_at_landscape_deadline() -> None:
    source = """
def rotate(conn, context):
    conn.execute(
        update(run_workers_table).values(
            heartbeat_expires_at=coalesce(
                context.sessions_database_now(),
                func.current_timestamp(),
            )
        )
    )
"""
    assert "cross-database-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "binding",
    [
        "name = 'wall'; setattr(self, name, datetime.now)",
        "self.__dict__['wall'] = datetime.now",
        "for self.wall in [datetime.now]: pass",
    ],
    ids=("aliased-setattr", "instance-dict", "for-instance-target"),
)
def test_dynamic_instance_state_cannot_hide_process_clock(binding: str) -> None:
    source = f"""
from datetime import datetime
class Custody:
    def bind(self):
        {binding}
    def rotate(self, conn):
        conn.execute(update(run_workers_table).values(
            heartbeat_expires_at=coalesce(self.wall(), func.current_timestamp())
        ))
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_match_star_clock_collection_is_resolved_at_subscript_call() -> None:
    source = """
from datetime import datetime
def rotate(conn):
    match [datetime.now]:
        case [*walls]:
            pass
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce(walls[0](), func.current_timestamp())
    ))
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "helper",
    [
        "def database_now(): return datetime(2099, 1, 1) if True else func.current_timestamp()",
        "def read_landscape_transaction_time(): return datetime(2099, 1, 1)",
        "func = attacker",
    ],
    ids=("dead-database-branch", "fake-reader", "shadowed-func"),
)
def test_fake_database_roots_are_not_trusted_by_terminal_name(helper: str) -> None:
    call = (
        "func.current_timestamp()"
        if helper == "func = attacker"
        else ("read_landscape_transaction_time()" if "read_landscape" in helper else "database_now()")
    )
    source = f"""
from datetime import datetime
{helper}
def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at={call}))
"""
    assert _scan_source(source)


def test_attacker_imported_transparent_name_cannot_bless_database_wrapper() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
from attacker import coalesce
def database_now(): return coalesce(func.current_timestamp())
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import database_now
def rotate(conn):
    conn.execute(update(run_workers_table).values(heartbeat_expires_at=database_now()))
""",
    }
    assert "unresolved-clock-provenance" in {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}


def test_wildcard_reexport_and_aliased_loader_preserve_process_provenance() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    package_path = "src/elspeth/core/landscape/__init__.py"
    wildcard_caller = "src/elspeth/core/landscape/wildcard_caller.py"
    loader_caller = "src/elspeth/core/landscape/loader_caller.py"
    sources = {
        helper_path: "from datetime import datetime\ndef database_now(): return datetime.now()",
        package_path: "from .clock_helper import *",
        wildcard_caller: """
from elspeth.core.landscape import *
def rotate(conn):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce(database_now(), func.current_timestamp())
    ))
""",
        loader_caller: """
import importlib
load = importlib.import_module
clock = load('elspeth.core.landscape.clock_helper').database_now
def rotate(conn):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce(clock(), func.current_timestamp())
    ))
""",
    }
    violations = _scan_sources(sources)
    for caller in (wildcard_caller, loader_caller):
        assert "process-clock-authority" in {violation.kind for violation in violations if violation.path == caller}


def test_cross_file_pure_database_helper_and_hof_are_fillable_positive_controls() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
from sqlalchemy import func
def database_now(): return func.current_timestamp()
def apply(clock): return clock()
""",
        caller_path: """
from sqlalchemy import func
from elspeth.core.landscape.clock_helper import apply, database_now
def rotate(conn):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=choose(database_now(), apply(func.current_timestamp))
    ))
""",
    }
    assert _scan_sources(sources) == ()


def test_exported_database_clock_object_is_fillable_positive_control() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    sources = {
        helper_path: """
from sqlalchemy import func
class Clock:
    def __call__(self): return func.current_timestamp()
source = Clock()
""",
        caller_path: """
from elspeth.core.landscape.clock_helper import source
def rotate(conn):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=source()
    ))
""",
    }
    assert _scan_sources(sources) == ()


@pytest.mark.parametrize("method", ["__call__", "read"], ids=("callable-object", "object-method"))
def test_exported_process_clock_objects_preserve_cross_file_provenance(method: str) -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    method_call = "source()" if method == "__call__" else "source.read()"
    sources = {
        helper_path: f"""
from datetime import datetime
class Clock:
    def {method}(self): return datetime.now()
source = Clock()
""",
        caller_path: f"""
from elspeth.core.landscape.clock_helper import source
def rotate(conn):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce({method_call}, func.current_timestamp())
    ))
""",
    }
    assert "process-clock-authority" in {violation.kind for violation in _scan_sources(sources) if violation.path == caller_path}


@pytest.mark.parametrize(
    "clock",
    [
        "operator.methodcaller('now')(datetime)",
        "datetime.__dict__.get('now')",
        "vars(datetime).get('now')",
        "operator.attrgetter(attribute)(datetime)",
    ],
    ids=("methodcaller", "dunder-dict-get", "vars-get", "dynamic-attrgetter"),
)
def test_additional_process_clock_hofs_are_rejected(clock: str) -> None:
    source = f"""
import operator
from datetime import datetime
def rotate(conn, attribute):
    clock = {clock}
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce(clock(), func.current_timestamp())
    ))
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


def test_with_target_preserves_process_clock_provenance() -> None:
    source = """
from datetime import datetime
def rotate(conn):
    with clock_context(datetime.now()) as observed:
        pass
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce(observed, func.current_timestamp())
    ))
"""
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "body",
    [
        "conn.exec_driver_sql('UPDATE \"run_workers\" SET \"heartbeat_expires_at\"=:x', {'x': datetime.now()})",
        "conn.exec_driver_sql('DELETE FROM run_workers WHERE heartbeat_expires_at < :x', {'x': datetime.now()})",
        "conn.exec_driver_sql('SELECT * FROM run_workers WHERE heartbeat_expires_at < :x', {'x': datetime.now()})",
        "conn.exec_driver_sql('CREATE TRIGGER expire_workers AFTER UPDATE ON run_workers BEGIN UPDATE run_workers SET heartbeat_expires_at=:x; END', {'x': datetime.now()})",
        "conn.exec_driver_sql('UPSERT INTO run_workers (heartbeat_expires_at) VALUES (:x)', {'x': datetime.now()})",
        "method = 'exec_' + 'driver_sql'; getattr(conn, method)('UPDATE run_workers SET heartbeat_expires_at=:x', {'x': datetime.now()})",
        "vars(conn)['exec_driver_sql']('UPDATE run_workers SET heartbeat_expires_at=:x', {'x': datetime.now()})",
    ],
    ids=(
        "quoted-identifiers",
        "delete-decision",
        "select-decision",
        "ddl-trigger",
        "standalone-upsert",
        "constructed-executor",
        "vars-executor",
    ),
)
def test_additional_raw_sql_authority_forms_are_rejected(body: str) -> None:
    source = f"from datetime import datetime\ndef rotate(conn):\n    {body}"
    assert "process-clock-authority" in {violation.kind for violation in _scan_source(source)}


@pytest.mark.parametrize(
    "mutation",
    [
        "merge = payload.update; merge({'heartbeat_expires_at': datetime.now()})",
        "put = payload.setdefault; put('heartbeat_expires_at', datetime.now())",
        "payload.__setitem__('heartbeat_expires_at', datetime.now())",
        "alias = payload; alias.update({'heartbeat_expires_at': datetime.now()})",
        "alias = payload; alias['heartbeat_expires_at'] = datetime.now()",
        "payload.update(extra)",
    ],
    ids=(
        "aliased-update",
        "aliased-setdefault",
        "dunder-setitem",
        "payload-method-alias",
        "payload-subscript-alias",
        "unresolved-poison",
    ),
)
def test_additional_mapping_mutations_fail_closed(mutation: str) -> None:
    source = f"""
from datetime import datetime
def rotate(conn, extra):
    table = run_workers_table
    payload = {{}}
    {mutation}
    conn.execute(update(table).values(**payload))
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert kinds & {"dynamic-authority-mapping", "process-clock-authority"}


@pytest.mark.parametrize(
    "predicate",
    [
        "pred = functools.partial(deadline.op('<'), datetime.now()); condition = pred()",
        "condition = deadline.__lt__(datetime.now())",
        "condition = operator.lt(deadline, datetime.now())",
        "predicates = {'expired': deadline.__lt__}; condition = predicates['expired'](datetime.now())",
        "condition = deadline < datetime.now()",
    ],
    ids=("partial-op", "dunder-lt", "operator-lt", "mapping-predicate", "direct-column-alias"),
)
def test_additional_operator_authority_forms_are_rejected(predicate: str) -> None:
    source = f"""
import functools
import operator
from datetime import datetime
def retire(conn):
    deadline = run_workers_table.c.heartbeat_expires_at
    {predicate}
    conn.execute(update(run_workers_table).where(condition).values(status='dead'))
"""
    kinds = {violation.kind for violation in _scan_source(source)}
    assert {"missing-database-time", "process-clock-authority"} <= kinds


def test_neutral_local_and_cross_file_wrappers_preserve_sessions_domain() -> None:
    helper_path = "src/elspeth/core/landscape/clock_helper.py"
    caller_path = "src/elspeth/core/landscape/custody.py"
    helper = "def neutral(context): return context.sessions_database_now()"
    caller = """
from elspeth.core.landscape.clock_helper import neutral
def rotate(conn, context):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=coalesce(neutral(context), func.current_timestamp())
    ))
"""
    assert "cross-database-clock-authority" in {
        violation.kind for violation in _scan_sources({helper_path: helper, caller_path: caller}) if violation.path == caller_path
    }
    assert "cross-database-clock-authority" in {
        violation.kind
        for violation in _scan_source(f"{helper}\n{caller.replace('from elspeth.core.landscape.clock_helper import neutral', '')}")
    }


@pytest.mark.parametrize(
    "loader",
    [
        "name = 'elspeth.web.' + 'sessions'; import_module(name)",
        "package = 'elspeth.web.sessions'; import_module('.repository', package=package)",
        "loader = getattr(importlib, 'import_module'); loader('elspeth.web.sessions')",
        "loader = functools.partial(import_module, 'elspeth.web.sessions'); loader()",
        "__import__('elspeth.web', fromlist=['sessions'])",
    ],
    ids=("constructed-name", "package-variable", "getattr-loader", "partial-loader", "dunder-fromlist"),
)
def test_sessions_import_guard_resolves_constructed_loaders(loader: str) -> None:
    source = f"import functools\nimport importlib\nfrom importlib import import_module\n{loader}"
    assert _sessions_import_violations({"src/elspeth/core/landscape/new_helper.py": source})


@pytest.mark.parametrize(
    "source",
    [
        "globals().__setitem__('update', attacker)\n" + _GOOD_FENCE_SOURCE,
        "key = 'update'; vars()[key] = attacker\n" + _GOOD_FENCE_SOURCE,
        "exec('update = attacker')\n" + _GOOD_FENCE_SOURCE,
        "from attacker import contextmanager\n"
        + _GOOD_FENCE_SOURCE.replace("def fenced_leader_transaction", "@contextmanager\ndef fenced_leader_transaction"),
        "import attacker\n"
        + _GOOD_FENCE_SOURCE.replace("def fenced_leader_transaction", "@attacker.contextmanager\ndef fenced_leader_transaction"),
        "from contextlib import contextmanager\n"
        + _GOOD_FENCE_SOURCE.replace("def fenced_leader_transaction", "@contextmanager\ndef fenced_leader_transaction")
        + "\ncontextmanager = attacker",
        "from attacker import RunLeadershipLostError\n" + _GOOD_FENCE_SOURCE,
    ],
    ids=("globals-setitem", "vars-key", "exec", "spoofed-contextmanager", "attribute-decorator", "late-rebind", "spoofed-error"),
)
def test_first_fence_rejects_namespace_and_decorator_spoofing(source: str) -> None:
    assert _first_fence_contract_violations(source)


def test_first_fence_accepts_trusted_contextmanager_decorator_positive_control() -> None:
    source = "from contextlib import contextmanager\n" + _GOOD_FENCE_SOURCE.replace(
        "def fenced_leader_transaction",
        "@contextmanager\ndef fenced_leader_transaction",
    )
    assert _first_fence_contract_violations(source) == ()


@pytest.mark.parametrize(
    "rebind",
    [
        "globals().update(**{'update': attacker})",
        "setattr(sys.modules[__name__], 'update', attacker)",
        "(update := attacker)",
        "for update in attackers:\n    pass",
        "match attacker:\n    case update:\n        pass",
    ],
    ids=("globals-kwargs", "sys-modules-setattr", "walrus", "for-target", "match-target"),
)
def test_first_fence_rejects_additional_module_namespace_rebindings(rebind: str) -> None:
    source = f"import sys\n{rebind}\n{_GOOD_FENCE_SOURCE}"
    assert "shadowed-first-fence-dependency:update" in _first_fence_contract_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        _GOOD_FENCE_SOURCE.replace(
            "def verify_and_extend_leader_fence(conn, *, token, window_seconds, verb):",
            "def verify_and_extend_leader_fence(conn, *, token, window_seconds=probe(), verb):",
        ),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    engine.begin()\n    with begin_write(engine) as conn:",
        ),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    attacker.execute(statement)\n    with begin_write(engine) as conn:",
        ),
        _GOOD_FENCE_SOURCE + "\nfrom attacker import update\n",
    ],
    ids=("poisoned-default", "engine-begin-before-owner", "attacker-execute-before-owner", "post-definition-import"),
)
def test_first_fence_rejects_poisoned_initialization_and_prior_effects(source: str) -> None:
    assert _first_fence_contract_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        _GOOD_FENCE_SOURCE.replace("        yield conn", "        if False:\n            yield conn"),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    if enabled:\n        with begin_write(engine) as conn:",
        )
        .replace("        verify_and_extend_leader_fence", "            verify_and_extend_leader_fence")
        .replace(
            "        yield conn",
            "            yield conn",
        ),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    for _ in []:\n        with begin_write(engine) as conn:",
        )
        .replace("        verify_and_extend_leader_fence", "            verify_and_extend_leader_fence")
        .replace(
            "        yield conn",
            "            yield conn",
        ),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    return\n    with begin_write(engine) as conn:",
        ),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    raise RuntimeError\n    with begin_write(engine) as conn:",
        ),
        _GOOD_FENCE_SOURCE.replace(
            "    with begin_write(engine) as conn:",
            "    if bypass:\n        return\n    with begin_write(engine) as conn:",
        ),
    ],
    ids=("dead-yield", "conditional-owner", "empty-loop-owner", "after-return", "after-raise", "bypass-return"),
)
def test_first_fence_requires_unconditional_reachable_transaction_and_yield(source: str) -> None:
    assert _first_fence_contract_violations(source)


def test_first_fence_rejects_late_verifier_import_rebinding() -> None:
    source = _GOOD_FENCE_SOURCE + "\nfrom attacker import verify_and_extend_leader_fence\n"
    assert "shadowed-verifier" in _first_fence_contract_violations(source)


def test_first_fence_requires_sync_nongenerator_verifier_and_reachable_owner() -> None:
    async_verify = _GOOD_FENCE_SOURCE.replace("def verify_and_extend_leader_fence", "async def verify_and_extend_leader_fence")
    generator_verify = _GOOD_FENCE_SOURCE.replace(
        "        raise RunLeadershipLostError()",
        "        raise RunLeadershipLostError()\n    yield conn",
    )
    dead_owner = _GOOD_FENCE_SOURCE.replace(
        "    with begin_write(engine) as conn:\n        verify_and_extend_leader_fence",
        "    if False:\n        with begin_write(engine) as conn:\n            verify_and_extend_leader_fence",
    ).replace("        yield conn", "            yield conn")
    for source in (async_verify, generator_verify, dead_owner):
        assert _first_fence_contract_violations(source)


def test_config_backed_database_duration_offsets_are_positive_controls() -> None:
    source = """
def rotate(conn, config):
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=func.current_timestamp()
        + timedelta(seconds=config.scheduler.lease_ttl_seconds)
    ))
    conn.execute(update(run_workers_table).values(
        heartbeat_expires_at=func.datetime(
            func.current_timestamp(), f'+{config.scheduler.lease_ttl_seconds} seconds'
        )
    ))
"""
    assert _scan_source(source) == ()


def test_resolved_safe_kwargs_are_positive_controls() -> None:
    source = """
def call(repo, runtime_config, timeout_seconds):
    kwargs = {'runtime_config': runtime_config, 'verb': 'heartbeat', 'window_seconds': timeout_seconds}
    repo.worker_heartbeat(**kwargs)
"""
    assert _scan_source(source) == ()


def test_nested_unused_authority_write_does_not_taint_outer_inventory_positive_control() -> None:
    path = "src/elspeth/core/landscape/harmless.py"
    source = """
def harmless():
    def unused(conn):
        conn.execute(update(run_workers_table).values(heartbeat_expires_at=func.current_timestamp()))
    return 'display-only'
"""
    assert (path, "harmless") not in {(boundary.path, boundary.symbol) for boundary in _discover_authority_boundaries({path: source})}


def test_clock_authority_definition_inventory_is_closed_and_stable() -> None:
    live = _clock_boundary_inventory()
    identities = frozenset((boundary.path, boundary.symbol) for boundary in live)
    assert identities == _REVIEWED_CLOCK_BOUNDARY_IDENTITIES, (
        "clock authority definition inventory drifted; review every move/addition/removal:\n"
        f"unexpected={sorted(identities - _REVIEWED_CLOCK_BOUNDARY_IDENTITIES)}\n"
        f"stale={sorted(_REVIEWED_CLOCK_BOUNDARY_IDENTITIES - identities)}"
    )
    digest = hashlib.sha256("\n".join(f"{boundary.path}:{boundary.symbol}" for boundary in live).encode()).hexdigest()
    assert digest == _CLOCK_BOUNDARY_DIGEST


def test_required_authority_boundary_definitions_exist_in_production() -> None:
    sources = {path: (_ROOT / path).read_text(encoding="utf-8") for path, _symbol in _REQUIRED_AUTHORITY_PUBLIC_SURFACE}
    missing = _missing_required_boundaries(sources)
    assert not missing, f"required authority boundary definitions were deleted: {sorted(missing)}"


def test_clock_inventory_covers_every_required_decision_family() -> None:
    identities = {(boundary.path, boundary.symbol) for boundary in _clock_boundary_inventory()}
    rendered = "\n".join(f"{path}:{symbol}" for path, symbol in sorted(identities))
    for family, markers in {
        "leadership": ("run_coordination_repository.py", "acquire_run_leadership"),
        "worker": ("run_coordination_repository.py", "worker_heartbeat"),
        "scheduler": ("scheduler/leases.py", "claim_ready"),
        "effect": ("sink_effect_lifecycle.py", "acquire_lease"),
        "checkpoint": ("checkpoint/manager.py", "CheckpointManager"),
        "takeover": ("run_coordination_repository.py", "_acquire_run_leadership_on"),
    }.items():
        assert all(marker in rendered for marker in markers), f"clock inventory omitted {family} decisions"


def test_landscape_authority_has_no_process_or_caller_clock_ingress() -> None:
    violations = tuple(violation for violation in _scan_production() if violation.kind != "missing-database-time")
    assert not violations, "Landscape authority still trusts caller/process time:\n" + _format_violations(violations)


def test_every_clock_sensitive_decision_uses_landscape_database_time() -> None:
    violations = tuple(violation for violation in _scan_production() if violation.kind == "missing-database-time")
    assert not violations, "Landscape decisions missing database time:\n" + _format_violations(violations)


def test_first_leader_fence_statement_uses_database_time_and_full_token() -> None:
    path = _ROOT / "src/elspeth/core/landscape/run_coordination_repository.py"
    violations = _first_fence_contract_violations(path.read_text(encoding="utf-8"))
    assert violations == (), "first Landscape fence contract violations: " + ", ".join(violations)


def test_divergent_sessions_and_landscape_clocks_never_cross_production_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from sqlalchemy import func, insert, select, update

    from elspeth.contracts import NodeType
    from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
    from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
    from elspeth.contracts.sink_effects import (
        SinkEffectInputKind,
        SinkEffectMemberCandidate,
        SinkEffectReservationRequest,
        SinkEffectRole,
    )
    from elspeth.core.checkpoint.manager import CheckpointManager
    from elspeth.core.checkpoint.recovery import NonResumableRunError
    from elspeth.core.landscape.execution.sink_effect_identity import (
        compute_pipeline_effect_identity,
        resolve_sink_effect_members,
    )
    from elspeth.core.landscape.run_coordination_repository import verify_and_extend_leader_fence
    from elspeth.core.landscape.schema import (
        run_coordination_table,
        run_workers_table,
        runs_table,
        sink_effects_table,
        token_work_items_table,
    )
    from tests.fixtures.landscape import make_factory, make_landscape_db, register_test_node

    sessions_database_now = datetime(2040, 1, 1, tzinfo=UTC)

    class ScalarResult:
        def scalar_one(self) -> datetime:
            return sessions_database_now

    class SessionsConnection:
        dialect = SimpleNamespace(name="sqlite")

        def exec_driver_sql(self, statement: str) -> ScalarResult:
            assert "CURRENT_TIMESTAMP" in statement.upper()
            return ScalarResult()

    sessions_module = importlib.import_module("elspeth.web.coordination.repository")
    sessions_repository = sessions_module._SessionOperationAuthorityRepository
    checkpoint_module = importlib.import_module("elspeth.core.checkpoint.manager")
    effect_module = importlib.import_module("elspeth.core.landscape.execution.sink_effect_lifecycle")
    observed_sessions_now = sessions_repository._database_now(SessionsConnection())
    assert observed_sessions_now == sessions_database_now

    def as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def database_now() -> datetime:
        with db.engine.connect() as connection:
            return as_utc(connection.execute(select(func.current_timestamp())).scalar_one())

    def call_with_legacy_now(method: Any, /, *args: Any, **kwargs: Any) -> Any:
        if "now" in inspect.signature(method).parameters:
            kwargs["now"] = observed_sessions_now
        return method(*args, **kwargs)

    divergences: list[str] = []

    def check_database_deadline(
        family: str,
        deadline: datetime,
        *,
        duration_seconds: float,
        landscape_before: datetime,
        landscape_after: datetime,
    ) -> None:
        decision_time = as_utc(deadline) - timedelta(seconds=duration_seconds)
        if not (as_utc(landscape_before) - timedelta(seconds=1) <= decision_time <= as_utc(landscape_after) + timedelta(seconds=1)):
            divergences.append(
                f"{family} used {decision_time.isoformat()} outside Landscape "
                f"[{as_utc(landscape_before).isoformat()}, {as_utc(landscape_after).isoformat()}]"
            )

    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id = "run-divergent-clock-families"
        leader_worker_id = "worker-landscape-clock"
        setup_now = database_now()
        with db.engine.begin() as connection:
            connection.execute(
                insert(runs_table).values(
                    run_id=run_id,
                    started_at=setup_now,
                    config_hash="config",
                    settings_json="{}",
                    canonical_version="v1",
                    status="running",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )

        token = call_with_legacy_now(
            factory.run_coordination.register_run_leader,
            run_id=run_id,
            worker_id=leader_worker_id,
            window_seconds=80.0,
        )
        source_node_id = register_test_node(
            factory.data_flow,
            run_id,
            "source-clock",
            node_type=NodeType.SOURCE,
            plugin_name="source",
        )

        with db.engine.begin() as connection:
            landscape_before = as_utc(connection.execute(select(func.current_timestamp())).scalar_one())
            call_with_legacy_now(
                verify_and_extend_leader_fence,
                connection,
                token=token,
                window_seconds=80.0,
                verb="divergent-leadership-clock-proof",
            )
            leadership_expiry = connection.execute(
                select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run_id)
            ).scalar_one()
            landscape_after = as_utc(connection.execute(select(func.current_timestamp())).scalar_one())
        check_database_deadline(
            "leadership",
            leadership_expiry,
            duration_seconds=80.0,
            landscape_before=landscape_before,
            landscape_after=landscape_after,
        )

        landscape_before = database_now()
        heartbeat = call_with_legacy_now(
            factory.run_coordination.worker_heartbeat,
            worker_id=leader_worker_id,
            window_seconds=80.0,
        )
        landscape_after = database_now()
        assert heartbeat.worker_active
        with db.engine.connect() as connection:
            worker_expiry = connection.execute(
                select(run_workers_table.c.heartbeat_expires_at).where(run_workers_table.c.worker_id == leader_worker_id)
            ).scalar_one()
        check_database_deadline(
            "worker",
            worker_expiry,
            duration_seconds=80.0,
            landscape_before=landscape_before,
            landscape_after=landscape_after,
        )

        class SessionsDateTime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return observed_sessions_now if tz is not None else observed_sessions_now.replace(tzinfo=None)

        monkeypatch.setattr(checkpoint_module, "datetime", SessionsDateTime)
        landscape_before = database_now()
        CheckpointManager(db).delete_checkpoints(run_id, coordination_token=token)
        landscape_after = database_now()
        with db.engine.connect() as connection:
            checkpoint_fence_expiry = connection.execute(
                select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run_id)
            ).scalar_one()
        check_database_deadline(
            "checkpoint",
            checkpoint_fence_expiry,
            duration_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            landscape_before=landscape_before,
            landscape_after=landscape_after,
        )

        row, scheduler_token = factory.data_flow.create_row_with_token(
            run_id,
            source_node_id,
            0,
            {"id": 1},
            source_row_index=0,
            ingest_sequence=0,
        )
        row_payload = factory.scheduler.serialize_row_payload(
            PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
        )
        work_item = factory.scheduler.enqueue_ready(
            run_id=run_id,
            token_id=scheduler_token.token_id,
            row_id=row.row_id,
            node_id=source_node_id,
            step_index=0,
            ingest_sequence=0,
            available_at=database_now() - timedelta(seconds=1),
            row_payload_json=row_payload,
        )
        landscape_before = database_now()
        claimed = call_with_legacy_now(
            factory.scheduler.claim_ready,
            run_id=run_id,
            lease_owner=leader_worker_id,
            lease_seconds=30,
        )
        landscape_after = database_now()
        assert claimed is not None
        with db.engine.connect() as connection:
            scheduler_expiry = connection.execute(
                select(token_work_items_table.c.lease_expires_at).where(token_work_items_table.c.work_item_id == work_item.work_item_id)
            ).scalar_one()
        check_database_deadline(
            "scheduler",
            scheduler_expiry,
            duration_seconds=30.0,
            landscape_before=landscape_before,
            landscape_after=landscape_after,
        )

        sink_node_id = register_test_node(
            factory.data_flow,
            run_id,
            "effect-sink-clock",
            node_type=NodeType.SINK,
            plugin_name="sink",
        )
        effect_payload = {"ordinal": 0}
        effect_row = factory.data_flow.create_row(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=1,
            data=effect_payload,
            source_row_index=1,
            ingest_sequence=1,
        )
        effect_token = factory.data_flow.create_token(effect_row.row_id)
        factory.execution.begin_node_state(
            token_id=effect_token.token_id,
            node_id=sink_node_id,
            run_id=run_id,
            step_index=0,
            input_data=effect_payload,
        )
        members = resolve_sink_effect_members(
            factory,
            (SinkEffectMemberCandidate(token_id=effect_token.token_id, row=effect_payload),),
        )
        canonical_members = tuple(
            replace(member, ordinal=ordinal, member_effect_id=None)
            for ordinal, member in enumerate(sorted(members, key=lambda member: member.ordinal))
        )
        effect_identity = compute_pipeline_effect_identity(
            run_id=run_id,
            sink_node_id=sink_node_id,
            role=SinkEffectRole.PRIMARY,
            sink_config={"name": "sink"},
            target_config={"path": "out.jsonl"},
            members=canonical_members,
        )
        reservation = SinkEffectReservationRequest(
            run_id=run_id,
            sink_node_id=sink_node_id,
            role=SinkEffectRole.PRIMARY,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            requested_target_hash=effect_identity.requested_target_hash,
            members=members,
            audit_export_snapshot_id=None,
            config_hash=effect_identity.config_hash,
            replacing_target=False,
            primary_effect_id=None,
        )
        effect = factory.execution.sink_effects.reserve(reservation).new_effect
        assert effect is not None
        monkeypatch.setattr(effect_module, "now", lambda: observed_sessions_now)
        landscape_before = database_now()
        effect_lease = factory.execution.sink_effects.claim_preparation(
            effect.effect_id,
            owner="effect-worker",
            ttl=timedelta(seconds=30),
        )
        landscape_after = database_now()
        with db.engine.connect() as connection:
            effect_expiry = connection.execute(
                select(sink_effects_table.c.lease_expires_at).where(sink_effects_table.c.effect_id == effect.effect_id)
            ).scalar_one()
        assert as_utc(effect_expiry) == as_utc(effect_lease.expires_at)
        check_database_deadline(
            "effect",
            effect_expiry,
            duration_seconds=30.0,
            landscape_before=landscape_before,
            landscape_after=landscape_after,
        )

        live_until = database_now() + timedelta(hours=1)
        with db.engine.begin() as connection:
            connection.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="failed"))
            connection.execute(
                update(run_coordination_table)
                .where(run_coordination_table.c.run_id == run_id)
                .values(leader_heartbeat_expires_at=live_until)
            )
        try:
            acquired = call_with_legacy_now(
                factory.run_coordination.acquire_run_leadership,
                run_id=run_id,
                worker_id="takeover-candidate",
                window_seconds=80.0,
            )
        except NonResumableRunError:
            pass
        else:
            divergences.append(f"takeover accepted Sessions clock and advanced Landscape seat to epoch {acquired.leader_epoch}")
    finally:
        db.close()

    assert not divergences, "cross-database clock authority reached Landscape decisions:\n" + "\n".join(divergences)


def test_landscape_core_does_not_import_sessions_clock_authority() -> None:
    source_files = sorted((_ROOT / "src/elspeth").rglob("*.py"))
    sources = {file.relative_to(_ROOT).as_posix(): file.read_text(encoding="utf-8") for file in source_files}
    forbidden = _sessions_import_violations(sources)
    assert forbidden == (), "Landscape imported Sessions clock authority:\n" + "\n".join(forbidden)


def test_no_cross_database_clock_name_crosses_authority_adapters() -> None:
    offenders: list[str] = []
    for source_file in sorted((_ROOT / "src/elspeth").rglob("*.py")):
        relative = source_file.relative_to(_ROOT).as_posix()
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = (_terminal_name(node.func) or "").lower()
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                value = ast.unparse(keyword.value).lower()
                if "landscape" in called and "session" in value and ("clock" in value or "database_now" in value):
                    offenders.append(f"{relative}:{node.lineno} Sessions -> Landscape")
                if "session" in called and "landscape" in value and ("clock" in value or "database_now" in value):
                    offenders.append(f"{relative}:{node.lineno} Landscape -> Sessions")
    assert offenders == [], "database clock crossed an authority adapter:\n" + "\n".join(offenders)
