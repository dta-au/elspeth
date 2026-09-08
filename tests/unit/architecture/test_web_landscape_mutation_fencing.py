"""Closed inventory and hard authority gates for Landscape mutations.

Every production caller, mutation API, transaction owner, and escape sweep
must have an empty violation set. Separate inventory pins bind the complete
production DML and caller sets, so a failing sweep cannot hide inventory drift.
Source-resolved proofs reject forged or mutable authority, callable escapes,
raw write surfaces, cross-database access, and transactions whose first
database effect does not establish the authority required by the verb.

TWO fences, one authority type each (ADR-030 D4, restored by the ADR-048
amendment of 2026-09-07).  A verb is LEADER-scoped or MEMBER-scoped, and the
scope decides both the exact concrete token type its signature must require and
which fence it must enter:

    LEADER  CoordinationToken       fenced_leader_transaction / fenced_write
    MEMBER  WorkerMembershipToken   fenced_member_transaction
    ITEM    WorkerMembershipToken   fenced_work_item_transaction + TokenWorkItem

The two are never interchangeable and no verb may accept both.  Crossing them
is rejected in either direction, because a leader verb that accidentally
accepted a follower's token would be unprovable -- the fail-open class this
gate exists to close, and the reason ADR-048's one-type-two-meanings option was
rejected.  Scope is keyed on the OWNING FILE as well as the method name: a
same-named method on another owned type must not inherit membership semantics.

``fenced_write`` is a thin WRAPPER over ``fenced_leader_transaction``; the tree
had one fence under two names before the membership fence landed, and a reader
of the trusted-fence set must not conclude two independent leader fences
pre-existed.

Pins and violation sweeps remain separate test ids and fail independently.
Diagnostics that truncate say so: never re-derive a pin from a list that
printed an elision notice.

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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from tests.helpers.tree_gate import iter_gate_files
from tests.unit.core.landscape.test_database_clock_authority import (
    _clock_captured_alias_is_mutated,
    _clock_lexical_scopes,
    _leader_deadline_sources_are_proven,
    _lexical_binding_sites,
    _worker_heartbeat_contract_violations,
    _worker_registration_contract_violations,
)

from elspeth_lints.core.ast_dump import stable_ast_dump


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
    # Qualified name of the fence actually entered.  Carried so a verb can be
    # held to the fence its OWN authority class names: admitting both fences
    # into one set without recording which was used would let a member fence
    # satisfy a leader verb, which is option (B) by accident.
    fence: str


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
            "create_row_with_token",
            "create_quarantine_row_with_token",
            "insert_row_with_token_on",
            "create_token",
            "fork_token",
            "coalesce_tokens",
            "finalize_coalesce_effect",
            "expand_token",
            "collect_tokens",
            "record_empty_expansion",
            "record_token_outcome",
            "record_token_outcome_leader",
            "register_node",
            "register_edge",
            "update_node_output_contract",
            "record_validation_error",
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
            "record_routing_event",
            "record_routing_events",
            "allocate_call_index",
            "record_call",
            "begin_operation",
            "complete_operation",
            "allocate_operation_call_index",
            "record_operation_call",
            "create_batch",
            "update_batch_status",
            "complete_batch",
            "complete_aggregation_result",
            "retry_batch",
        ),
    ),
    *_apis(
        _SCHEDULER_PATH,
        "TokenSchedulerRepository",
        "scheduler",
        (
            "enqueue_ready",
            "enqueue_ready_claimed",
            "ingest_row_with_initial_claim",
            "claim_ready",
            "claim_pending_sink",
            "recover_expired_leases",
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
            "adopt_group_losses",
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
    "data-flow": 17,
    "execution": 19,
    "scheduler": 23,
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
        ("run_coordination", "update", 1),
        ("run_coordination_events", "insert", 2),
        ("run_web_plugin_policy", "insert", 1),
        ("run_workers", "insert", 1),
        ("run_workers", "update", 1),
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
        ("run_coordination", "update", 2),
        ("run_coordination_events", "insert", 3),
        ("run_workers", "insert", 1),
        ("run_workers", "update", 2),
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
        ("run_workers", "update", 1),
    ),
    temporary=False,
    sunset=None,
)

_EXPORT_SEAT_ESTABLISHMENT = AuthorityEstablishmentException(
    classification="export-seat-claim",
    caller_path="src/elspeth/core/landscape/run_coordination_repository.py",
    caller_symbol="RunCoordinationRepository.acquire_export_leadership",
    callee_path="src/elspeth/core/landscape/run_coordination_repository.py",
    callee_symbol="RunCoordinationRepository._acquire_export_leadership_on",
    write_counts=(
        ("run_coordination", "update", 2),
        ("run_coordination_events", "insert", 3),
        ("run_workers", "insert", 1),
        ("run_workers", "update", 2),
    ),
    temporary=False,
    sunset=None,
)
_AUTHORITY_ESTABLISHMENTS = (
    _FRESH_EPOCH_ONE_EXCEPTION,
    _EXISTING_RUN_LEADERSHIP_ESTABLISHMENT,
    _FOLLOWER_MEMBERSHIP_ESTABLISHMENT,
    _EXPORT_SEAT_ESTABLISHMENT,
)
_AUTHORITY_ESTABLISHMENT_EXCEPTIONS = tuple(item for item in _AUTHORITY_ESTABLISHMENTS if item.temporary)
_EXACT_BEGIN_RUN_PRODUCTION_CALLERS = frozenset(
    {
        ("src/elspeth/engine/orchestrator/run_lifecycle.py", "RunLifecycleCoordinator.initialize_database_phase"),
        ("src/elspeth/web/_aws_ecs_acceptance/bedrock.py", "run_bedrock_guardrails_live"),
    }
)

_AUTHORITY_PARAMETER_NAMES = frozenset({"coordination_token", "member_token", "token"})
_FENCED_CONTEXT_NAMES = frozenset(
    {"fenced_leader_transaction", "fenced_member_transaction", "fenced_heartbeat_transaction", "fenced_item_transaction", "fenced_write"}
)

# ADR-030 D4 names THREE fences, and until the membership fence landed the tree
# had ONE wearing two names: ``fenced_write`` is a thin wrapper over
# ``fenced_leader_transaction``, so a reader of this set must not conclude two
# independent leader fences pre-existed.  ``fenced_member_transaction`` is the
# genuinely second fence (elspeth-43ddb79074).
_LEADER_FENCE_QUALIFIED = frozenset(
    {
        "elspeth.core.landscape.run_coordination_repository.fenced_leader_transaction",
        "elspeth.core.landscape.scheduler.fencing.fenced_write",
    }
)
_MEMBER_FENCE_QUALIFIED = frozenset(
    {
        "elspeth.core.landscape.run_coordination_repository.fenced_member_transaction",
        "elspeth.core.landscape.run_coordination_repository.fenced_heartbeat_transaction",
    }
)
_ITEM_FENCE_QUALIFIED = frozenset({"elspeth.core.landscape.item_fencing.fenced_item_transaction"})
_TRUSTED_FENCE_QUALIFIED = _LEADER_FENCE_QUALIFIED | _MEMBER_FENCE_QUALIFIED | _ITEM_FENCE_QUALIFIED

# ADR-030 D4's authority classes.  ONE exact concrete type per class, never a
# union, never Optional, and never both on one verb: a leader verb that
# accidentally accepted a follower's token would be unprovable, which is the
# fail-open class this programme exists to close (ADR-048 amendment, option A).
_LEADER_SCOPE = "leader"
_MEMBER_SCOPE = "member"
_ITEM_SCOPE = "item"
_WORK_ITEM_SCOPE = "claimed-work-item"
_AUTHORITY_QUALIFIED_BY_SCOPE = {
    _LEADER_SCOPE: "elspeth.contracts.coordination.CoordinationToken",
    _MEMBER_SCOPE: "elspeth.contracts.coordination.WorkerMembershipToken",
    _ITEM_SCOPE: "elspeth.contracts.coordination.WorkerMembershipToken",
    _WORK_ITEM_SCOPE: "elspeth.contracts.scheduler.TokenWorkItem",
}
_FENCE_QUALIFIED_BY_SCOPE = {
    _LEADER_SCOPE: _LEADER_FENCE_QUALIFIED,
    _MEMBER_SCOPE: _MEMBER_FENCE_QUALIFIED,
    _ITEM_SCOPE: _ITEM_FENCE_QUALIFIED,
}
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

# The MEMBER-scoped verbs (ADR-030 D4's second fence): a follower's own
# liveness and departure writes, whose authority is membership in
# ``run_workers``, not the leader epoch.  Everything else -- all 90 facade APIs
# and every other coordination verb -- is LEADER-scoped.
#
# ``release_seat`` is deliberately NOT here, and the reason is worth stating
# because the lane brief grouped it with the member verbs: the seat is a
# run-scoped row and its CAS ``WHERE`` is identical to the leader fence
# predicate (run_id, leader_worker_id, leader_epoch), so it is leader-scoped in
# the code and the ADR-048 amendment classifies it that way.  Where the brief
# and the code disagreed, the code won.
_RUN_COORDINATION_PATH = "src/elspeth/core/landscape/run_coordination_repository.py"
_MEMBER_SCOPED_METHOD_NAMES = frozenset(
    {
        "depart_worker",
        "worker_heartbeat",
        "record_heartbeat_degraded",
        # The fence machinery itself carries the member token and is scanned
        # like any other writer, so it must classify MEMBER too. Naming these
        # explicitly, rather than inferring scope from the annotation each
        # function happens to declare, is deliberate: inferring would let a
        # verb choose its own authority class, which is not a check.
        "fenced_member_transaction",
        "fenced_heartbeat_transaction",
        "verify_membership_fence",
    }
)

# ADR-048 amendment A3: declarations keyed by their exact owner path. Row
# audit writers additionally prove the claimed work item; scheduler transition
# verbs retain their own item CAS inside the membership-fenced transaction.
_MEMBER_METHODS_BY_PATH = {
    _RUN_COORDINATION_PATH: _MEMBER_SCOPED_METHOD_NAMES,
    _RUN_LIFECYCLE_PATH: frozenset({"record_readiness_check"}),
    _DATA_FLOW_PATH: frozenset({"expand_token", "record_empty_expansion", "update_node_output_contract"}),
    "src/elspeth/core/landscape/data_flow/tokens.py": frozenset({"expand_token", "record_empty_expansion"}),
    "src/elspeth/core/landscape/data_flow/graph.py": frozenset({"update_node_output_contract"}),
    _EXECUTION_PATH: frozenset({"begin_node_state", "complete_node_state", "record_routing_event"}),
    "src/elspeth/core/landscape/execution/node_states.py": frozenset({"begin_node_state", "complete_node_state", "record_routing_event"}),
    _SCHEDULER_PATH: frozenset(
        {
            "enqueue_ready",
            "enqueue_ready_claimed",
            "claim_ready",
            "heartbeat_lease",
            "mark_blocked",
            "mark_terminal",
            "mark_terminal_with_ready_children",
            "mark_failed",
            "mark_failed_with_ready_children",
            "mark_pending_sink",
            "mark_pending_sink_with_ready_children",
        }
    ),
    "src/elspeth/core/landscape/scheduler/queue.py": frozenset({"enqueue_ready", "enqueue_ready_claimed", "_enqueue_ready_claimed"}),
    "src/elspeth/core/landscape/scheduler/leases.py": frozenset({"claim_ready", "heartbeat_lease"}),
    "src/elspeth/core/landscape/scheduler/dispositions.py": frozenset(
        {
            "mark_blocked",
            "mark_terminal",
            "mark_terminal_with_ready_children",
            "mark_failed",
            "mark_failed_with_ready_children",
            "mark_pending_sink",
            "mark_pending_sink_with_ready_children",
            "_transition",
            "_transition_with_ready_children",
        }
    ),
    "src/elspeth/core/landscape/item_fencing.py": frozenset({"fenced_item_transaction"}),
}
_ITEM_METHODS_BY_PATH = {
    _DATA_FLOW_PATH: frozenset({"fork_token", "record_token_outcome", "record_transform_error"}),
    "src/elspeth/core/landscape/data_flow/tokens.py": frozenset({"fork_token"}),
    "src/elspeth/core/landscape/data_flow/outcomes.py": frozenset({"record_token_outcome"}),
    "src/elspeth/core/landscape/data_flow/errors.py": frozenset({"record_transform_error"}),
    _EXECUTION_PATH: frozenset({"allocate_call_index", "record_call", "record_routing_events"}),
    "src/elspeth/core/landscape/execution/calls.py": frozenset({"allocate_call_index", "record_call", "_record_call_payload_refs"}),
    "src/elspeth/core/landscape/execution/node_states.py": frozenset({"record_routing_events"}),
}


def _verb_authority_scope(path: str, method: str) -> str:
    """The ONE authority class a verb may accept (ADR-030 D4, ADR-048 §1 as amended).

    A single classifier serves both sweeps -- the 90-API sweep and the DML
    transaction sweep -- so a verb cannot be leader-scoped in one and
    member-scoped in the other.  It is a function rather than a column on
    ``_MUTATION_APIS`` because the coordination repository is NOT among those
    90 owners: the member verbs are enumerated in
    ``_COORDINATION_MUTATION_METHOD_NAMES``, and a scope column on the facade
    tuple could never have reached them.

    Keyed on the OWNING FILE as well as the name, never the name alone. This
    project has been bitten three times by rules that keyed on a method name
    across owned types (``begin_attempt``, ``heartbeat_lease``,
    ``update_run_status``), and the collision is not hypothetical here:
    ``_HeartbeatRepository.worker_heartbeat`` in the orchestrator is a
    same-named Protocol declaration. Name-only keying would classify any such
    definition MEMBER, and that is the DANGEROUS direction -- it would require
    a leader-scoped verb to carry a follower's token, the narrow form of the
    one-type-two-meanings hole ADR-048 rejected.
    """
    if path in _ITEM_METHODS_BY_PATH and method in _ITEM_METHODS_BY_PATH[path]:
        return _ITEM_SCOPE
    if path in _MEMBER_METHODS_BY_PATH and method in _MEMBER_METHODS_BY_PATH[path]:
        return _MEMBER_SCOPE
    return _LEADER_SCOPE


# Filled from the canonical scanners below.  These literals intentionally
# represent the pre-Task-6 surface; production migration may satisfy the
# authority tests without silently adding, deleting, moving, or replacing a
# write/caller identity.
#
# Re-derived for P4-D5 (elspeth-284f68c493) by running this file's scanners
# as a library over the fec6a4f32 pin tree and the landed tree; the current
# scanner reproduces every fec6a4f32 pin exactly, so each delta below is
# production change only (line-insensitive identity terms).
#
# DML 126 -> 139 (-16 +29): coalesce_branch_losses -> group_losses, three
# sites including the fenced adopt verb (a68ad6a2e); unified lineage adds the
# token_lineage_frames writer (_insert_lineage_frames) and group_records
# writers in collect_tokens / expand_token / fork_token /
# record_empty_expansion, and rotates every tokens / token_outcomes insert that
# dropped the tri-field lineage columns (879d007dd, d176c5d2c, 27414bbb0);
# aggregation result receipts add three inserts in complete_aggregation_result
# and nest complete_batch's UPDATE in _complete_on (8408eaf3b, 4e0781695);
# record_terminal_outcome_guarded and fail_open_effect_operations_for_run are
# new caller-fenced helpers (9ca934b7e); link_validation_error_to_row -> _on
# (67f6e1e02); fingerprint rotations in mark_pending_sink_terminal_many
# (49a7bb16c), _recover_expired_leases (55a8a94f4) and
# SinkEffectLifecycle.complete_plan (826d5e6ca). Every added identity carries
# its typed authority.
# MEMBER-FENCE (elspeth-43ddb79074, ADR-030 D4): 144 -> 145, +1 identity —
# verify_membership_fence's run_workers UPDATE, the membership fence's own
# first statement. The write set is UNCHANGED (added=[] removed=[]): run_workers
# already took an UPDATE through depart_worker and evict_worker, so this is a
# new construction of an existing write shape, not a new shape. Re-derived from
# this file's own printed output on the rebased tree, applied and run.
_EXPECTED_DML_COUNT = 150
# D8.1 (P4-D8 elspeth-43ddb79074): 6ca139a7… → 504d39e2…. Count 139 and the write set
# unchanged; twelve construction FINGERPRINTS moved because the constructions
# themselves were rewritten to fence first / execute once: the eleven
# RunLifecycleRepository writers (_complete_run_in update runs; record_preflight_results
# insert preflight_results; record_run_source insert+update run_sources;
# record_secret_resolutions insert secret_resolutions; record_source_field_resolution,
# set_export_failed_unless_completed, set_export_pending_unless_completed,
# set_export_status, update_run_status update runs; update_run_source_contract update
# run_sources) and OperationRepository.fail_open_effect_operations_for_run update
# operations (one executemany UPDATE instead of an UPDATE per locked row). Re-derived
# from the gate's printed output; no identity added, removed, moved or replaced.
# Then 139 -> 142 (504d39e2… → d51c3414…), same commit, the cleared run-coordination
# hunks: record_coordination_events insert run_coordination_events (the ONE
# executemany ledger write _complete_run_in's follower departures ride on) and
# _acquire_export_leadership_on update run_coordination + update run_workers (the
# export seat, the fourth pinned establishment; ADR-048 §4). Write set unchanged.
# Then d51c3414… → de37c3fe… (count 142, write set unchanged): the two
# run_coordination_events constructions share one ``_coordination_event_id``
# recipe and the batch rows carry no soft-mapping annotation (census 2734 held).
# Then 142 -> 144 (de37c3fe… → b8797993…, C6 stages 3-4 elspeth-0ff11aa42e,
# rebased onto 282936e27): SchedulerDispositionRepository._transition_on
# executes one inline UPDATE per owned disposition image (three sites) in
# place of the one mapping-driven UPDATE. Write set unchanged; re-derived from
# the gate's printed output on the rebased tree.
# Then b8797993… → a76a88f5… (count 144, write set unchanged, elspeth-ee18e446ff):
# BarrierJournalRepository.reset_adoption_marker_to_pending took a bare ``run_id``
# and ran on ``begin_write``; it now takes the coordination token and runs inside
# ``fenced_leader_transaction``, so its run_id predicate reads
# ``coordination_token.run_id``. That one AST change moves exactly one site's
# fingerprint, 37af8d10ee462eff → 8f538e40a9a91999 — the fence wrapper itself is
# excluded from the fingerprint by ``_semantic_dml_boundary``, so this records the
# statement change and not the fencing. Path, symbol, table, operation, ordinal
# and authority are all unchanged. Attributed by scanning base f83011bb7 and the
# merged tree with this same scanner: one site differs, no other.
# Then 144 -> 151 (a76a88f5… → b9ef22af…, P4-D8 SINKFX elspeth-43ddb79074), and the
# write set gains four shapes: sink_effects, sink_effect_members,
# sink_effect_streams and sink_effect_export_snapshots insert. NONE OF THESE IS A
# NEW DATABASE WRITE. Every one of them already ran; the generic
# ``_conflict_safe_insert(conn, table, values, index_elements)`` took its table as a
# caller-supplied parameter, so the scanner could not bind a table to the statement
# and counted the family as two "insert on caller-supplied table" ESCAPES instead of
# as classified DML. Each owner now issues its own dialect-specific conflict-safe
# INSERT against a named table, so the writes MOVED from the escape counter into this
# inventory — an unmasking, not an addition, which is why escapes fall by two across
# the same delta. Of the eleven added rows, nine are in the newly classified
# sink_effect_reservation.py; the other two are one-for-two consolidations in
# _finalize_on and complete_plan, where a per-ordinal member UPDATE loop became one
# executemany UPDATE (four rows removed, two added, and sink_effect_members/update
# keeps surviving rows, so NO shape is removed). Re-derived by the merge writer on the
# MERGED tree, not carried from the branch, and the merged value equals the branch
# value because the intervening tip delta touched no Python. The lane declared four
# added SHAPES and none removed; this scan measured eleven added and four removed
# ROWS: the same fact at two granularities, reconciled row by row before pinning.
# CKPT-SNAP (elspeth-43ddb79074, ADR-048 D8): b9ef22af… -> the value below, at COUNT
# 151 UNCHANGED and write shapes added and removed BOTH EMPTY. A balanced swap: the
# count is actively reassuring and wrong, and only the row list separates it from no
# change at all. Four rows move — two checkpoint constructions now take the run
# subject from the token attribute, and two audit-export inserts keep BYTE-IDENTICAL
# fingerprints and move on the owning symbol alone, register_verified_candidate ->
# _register_verified_on. The sentence above about the intervening tip delta touching
# no Python described the SINKFX landing and does NOT hold across this one, which
# adds a 204-line test file and edits ten src modules. Re-derived on the merged tree
# 79fefa4fe by RUNNING the gate, never by reasoning about rows, and agreed value for
# value by an independent derivation from a git archive of the same sha.
# Member-fence integration adds verify_membership_fence and changes the three
# heartbeat/departure DML fingerprints to use the membership token's subjects.
# The existing write-shape set is unchanged. Re-derived with scan_dml_identities.
# Actual INSERT RETURNING cardinality checks and the finalizer's locked follower
# roster change function fingerprints; the DML count and write-shape set stay unchanged.
# Fresh-time renewal and explicit registration finalizers add four DML sites;
# the approved helper extraction adds seven edges. Table/operation shapes and
# public caller inventories remain unchanged (measured from the live AST).
_EXPECTED_DML_INVENTORY_SHA256 = "c3b43f8fe38e43d916e79dc0dddc5c5a5e90bdb6a21df411be77039efe60c0f0"
_EXPECTED_DML_WRITE_SET: frozenset[tuple[str, str]] = frozenset(
    {
        ("aggregation_result_members", "insert"),
        ("aggregation_result_outputs", "insert"),
        ("aggregation_results", "insert"),
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
        ("coalesce_effect_members", "insert"),
        ("coalesce_effect_members", "update"),
        ("coalesce_effects", "insert"),
        ("coalesce_effects", "update"),
        ("edges", "insert"),
        ("group_losses", "insert"),
        ("group_losses", "update"),
        ("group_records", "insert"),
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
        ("sink_effect_export_snapshots", "insert"),
        ("sink_effect_members", "insert"),
        ("sink_effect_members", "update"),
        ("sink_effect_streams", "insert"),
        ("sink_effect_streams", "update"),
        ("sink_effects", "insert"),
        ("sink_effects", "update"),
        ("token_lineage_frames", "insert"),
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
# Measured by scripts/fencing_inventory.py against the complete production tree.
# The five inventory digests are separate from violation sweeps: a failed sweep
# must never prevent detection of added, removed, moved, or replaced identities.
# ADR-048 completion removes dead writers, adds explicit connection composition,
# and forwards exact member/leader/item authorities through every live caller.
_EXPECTED_CALL_COUNT = 274
_EXPECTED_PRODUCTION_CALLER_SHA256 = "92e579b2c4ee6a39a03689c9fd21f52bbfbbb5528876e3541106d97ddb547a6b"
_EXPECTED_SUBORDINATE_EDGE_COUNT = 136
_EXPECTED_SUBORDINATE_EDGE_SHA256 = "0561dddd71e704031510290def021516e75e0d0668316c25a6e659ad2702c6fe"
_EXPECTED_COORDINATION_CALL_COUNT = 23
_EXPECTED_COORDINATION_CALL_SHA256 = "214a5dba39b8edea91a2c101f247fd44c1b005765042622fe4f2217260f73090"
_EXPECTED_INTERNAL_EDGE_COUNT = 88
_EXPECTED_INTERNAL_EDGE_SHA256 = "fc1f6fe8a8d29cc56d7bdd8000d34d00c3136b4cb83d677c9b049d22f7ed89a8"


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


def _ancestors(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the enclosing nodes of ``node``, nearest first, up to the module."""

    current = getattr(node, "_landscape_parent", None)
    while current is not None:
        yield current
        current = getattr(current, "_landscape_parent", None)


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
    return tuple(_read_source(path, anchor=root) for path in iter_gate_files(root / "src" / "elspeth"))


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

    def iteration_source(self, name: str, use: ast.AST) -> tuple[ast.expr, ast.expr] | None:
        """Return ``(target, iterable)`` of the loop or comprehension binding ``name`` around ``use``.

        A loop variable has no assignment to resolve; its value is one element
        of the iterable.  Only a loop whose BODY encloses ``use`` binds it —
        a use inside the iterable expression itself is evaluated before the
        target exists.
        """

        ancestors: list[ast.AST] = []
        for current in _ancestors(use):
            if isinstance(current, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                break
            ancestors.append(current)
        lineage = {id(use), *(id(node) for node in ancestors)}
        for node in ancestors:
            if isinstance(node, (ast.For, ast.AsyncFor)) and id(node.iter) not in lineage and name in self._target_names(node.target):
                return node.target, node.iter
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                for generator in node.generators:
                    if id(generator.iter) not in lineage and name in self._target_names(generator.target):
                        return generator.target, generator.iter
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
        if isinstance(node, ast.Call) and _resolved_callable_name(node.func, self, use=node, seen=seen) == "import_module" and node.args:
            return _constant_string_value(node.args[0], self, use=node)
        if isinstance(node, ast.Name):
            if node.id in seen:
                return None
            binding = self.binding(node.id, use or node)
            if binding is not None:
                return self.qualified_name(binding, use=binding, seen=seen | {node.id})
            if self.is_local(node.id, use or node):
                return self._local_import(node.id, use or node)
            declarations = [
                child
                for child in self.unit.tree.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == node.id
            ]
            import_lines = [line for line, _value in self.imports.get((id(self.unit.tree), node.id), ())]
            if declarations and max(child.lineno for child in declarations) > max(import_lines, default=-1):
                module = self.unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
                return f"{module}.{node.id}"
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

    def resolve_statement(self, node: ast.expr, *, use: ast.AST, seen: frozenset[int] = frozenset()) -> ast.expr | None:
        if isinstance(node, ast.Name):
            binding = self.binding(node.id, use)
            # The cycle guard keys on the BINDING SITE, not on the name.
            # ``query = select(...)`` followed by ``query = query.where(...)``
            # re-binds one name to a refinement of its own earlier value; a
            # name-keyed guard reads that as a cycle and abandons the walk at
            # the ``.where(...)`` link, so a conditionally refined SELECT never
            # resolves to its ``select(...)`` root.  Binding-site identity keeps
            # a genuine ``a = b`` / ``b = a`` cycle terminating.
            if binding is None or id(binding) in seen:
                return None
            return self.resolve_statement(binding, use=binding, seen=seen | {id(binding)})
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # SQLAlchemy statement chaining: update(...).where(...).values(...)
            return self.resolve_statement(node.func.value, use=node, seen=seen) or node
        return node


@cache
def _resolver_for_unit(unit: SourceUnit) -> _Resolver:
    return _Resolver(unit)


def _table_identity(node: ast.AST, resolver: _Resolver, *, use: ast.AST) -> tuple[str, str] | None:
    """Return the defining module and table name of a SQLAlchemy table reference.

    A table NAME is not an identity.  Sessions (``elspeth.web.sessions.models``)
    and Landscape (``elspeth.core.landscape.schema``) both define a ``runs``
    table, so every cross-database decision keys on the module the table object
    is bound from, never on the trailing spelling.
    """

    dotted = resolver.qualified_name(node, use=use)
    if dotted is None:
        return None
    module, _, terminal = dotted.rpartition(".")
    if not terminal.endswith("_table"):
        return None
    return module, terminal.removesuffix("_table")


def _table_name(node: ast.AST, resolver: _Resolver, *, use: ast.AST) -> str | None:
    identity = _table_identity(node, resolver, use=use)
    return None if identity is None else identity[1]


def _dml_construction(call: ast.Call, resolver: _Resolver) -> tuple[ast.expr, str, ast.AST] | None:
    """Return a SQLAlchemy DML construction's table expression, operation and use site."""

    callable_node = resolver.resolve_callable(call.func, use=call)
    if isinstance(callable_node, ast.Call) and _call_name(callable_node) == "getattr" and len(callable_node.args) >= 2:
        operation_node = callable_node.args[1]
        if isinstance(operation_node, ast.Constant) and operation_node.value in {"insert", "update", "delete"}:
            return callable_node.args[0], str(operation_node.value), callable_node

    if (
        isinstance(callable_node, ast.Attribute)
        and callable_node.attr in {"insert", "update", "delete"}
        and _table_name(callable_node.value, resolver, use=callable_node) is not None
    ):
        return callable_node.value, callable_node.attr, callable_node

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
    return call.args[0], operation, call


def _dml_shape(call: ast.Call, resolver: _Resolver) -> tuple[str, str] | None:
    """Return a SQLAlchemy DML construction's exact table and operation."""

    construction = _dml_construction(call, resolver)
    if construction is None:
        return None
    table_node, operation, use = construction
    table = _table_name(table_node, resolver, use=use)
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

# PRAGMA <name> with no ``=`` reads the setting back.  ``foreign_keys``,
# ``journal_mode`` and ``user_version`` are read in this form by
# ``verify_sqlite_tier1_pragmas``, ``_sqlite_epoch_is_incompatible`` and
# ``_get_sqlite_schema_epoch``; ``_verify_sqlite_pragmas`` reads every name in
# ``_SQLITE_PRAGMA_INVARIANTS_*`` (``busy_timeout``, ``synchronous`` included)
# through one interpolated statement.  The assignment form is a separate
# decision (see ``_raw_sql_is_connection_configuration``).
_RAW_READ_PRAGMAS = frozenset(
    {
        "busy_timeout",
        "compile_options",
        "database_list",
        "foreign_key_list",
        "foreign_keys",
        "index_info",
        "index_list",
        "journal_mode",
        "synchronous",
        "table_info",
        "table_xinfo",
        "user_version",
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


def _resolved_callable_name(
    node: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[str] = frozenset(),
) -> str | None:
    # ``seen`` is ``qualified_name``'s cycle guard.  This helper sits on the
    # lap ``qualified_name`` (Call branch) -> here -> ``qualified_name``, so it
    # must carry the guard through both hops: a guard dropped on one path is
    # the same defect as no guard (elspeth-5a50d4b9f3).
    resolved = resolver.resolve_callable(node, use=use, seen=seen)
    if isinstance(resolved, ast.Call) and _call_name(resolved) == "getattr" and len(resolved.args) >= 2:
        name = _constant_string_value(resolved.args[1], resolver, use=resolved)
        if name is not None:
            return name
    qualified = resolver.qualified_name(resolved, use=use, seen=seen)
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


# PRAGMAs that configure a CONNECTION rather than write Landscape data, each
# admitted because a measured production site issues it: query_only,
# journal_mode, synchronous, foreign_keys and busy_timeout come from
# ``LandscapeDB._configure_sqlite`` / ``read_only_connection``; ``key`` is the
# SQLCipher passphrase in ``_create_sqlcipher_engine``.  ``user_version`` is
# NOT here: it stamps the schema epoch, which is a DDL write and stays
# reported.
_RAW_CONNECTION_CONFIGURATION_PRAGMAS = frozenset(
    {
        "busy_timeout",
        "foreign_keys",
        "journal_mode",
        "key",
        "query_only",
        "synchronous",
    }
)
_RAW_PRAGMA_ASSIGNMENT_RE = re.compile(r"^pragma\s+(?:[a-z_][a-z0-9_]*\.)?(?P<name>[a-z_][a-z0-9_]*)\s*=")
# ``PRAGMA user_version = N`` stamps the schema epoch: a schema-creation write
# that precedes any token (ADR-048 §8).  It is reported under its own label so
# the residue reads as the DDL obligation it is, never as a row write.
_SCHEMA_STAMP_PRAGMAS = frozenset({"user_version"})


def _raw_sql_write_detail(value: str) -> str:
    """Name the write class of a raw statement already known to be a write."""

    shape = _raw_dml_shape_from_text(value)
    if shape is not None:
        return f"{shape[1]} {shape[0]}"
    match = _RAW_PRAGMA_ASSIGNMENT_RE.match(_normalized_raw_sql(value).strip().lower())
    if match is not None and match.group("name") in _SCHEMA_STAMP_PRAGMAS:
        return f"DDL schema-stamp (PRAGMA {match.group('name')})"
    return "write/DDL"


def _raw_sql_is_connection_configuration(value: str) -> bool:
    """True for statements that configure the connection, not Landscape rows."""

    normalized = _normalized_raw_sql(value).rstrip(";").strip().lower()
    if normalized.startswith(("set transaction", "set session characteristics")):
        return True
    if re.fullmatch(r"set local (?:lock_timeout|statement_timeout) = '[0-9]+ms'", normalized):
        return True
    match = _RAW_PRAGMA_ASSIGNMENT_RE.match(normalized)
    return match is not None and match.group("name") in _RAW_CONNECTION_CONFIGURATION_PRAGMAS


def _raw_sql_is_single_configuration_statement(value: str) -> bool:
    """True for exactly one connection-configuration statement (no ``;`` to hide a second)."""

    return ";" not in value and _raw_sql_is_connection_configuration(value)


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


def _raw_sql_text_payload(node: ast.expr, resolver: _Resolver) -> ast.expr | None:
    """Unwrap ``text("…")`` so its payload can be classified at the executor."""

    if isinstance(node, ast.Call) and node.args and _resolved_callable_name(node.func, resolver, use=node) == "text":
        return node.args[0]
    return None


def _constant_string_candidates(
    node: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int] = frozenset(),
) -> frozenset[str] | None:
    """Return every string ``node`` can evaluate to, or ``None`` when that set is not finite and static.

    Beyond a plain constant, the only admitted shape is a loop variable drawn
    from a literal tuple/list (optionally chosen by a conditional expression)
    whose elements are constants or tuples of constants — the
    ``for pragma, expected in _SQLITE_PRAGMA_INVARIANTS_*`` form.  A parameter,
    an attribute, or a call returns ``None``: nothing is ever inferred from a
    name alone.
    """

    if id(node) in seen:
        return None
    next_seen = seen | {id(node)}
    direct = _constant_string_value(node, resolver, use=use)
    if direct is not None:
        return frozenset({direct})
    if isinstance(node, ast.IfExp):
        branches = [_constant_string_candidates(branch, resolver, use=node, seen=next_seen) for branch in (node.body, node.orelse)]
        return None if any(branch is None for branch in branches) else frozenset().union(*(branch or () for branch in branches))
    if isinstance(node, ast.Subscript):
        return _subscript_string_candidates(node, resolver, use=use, seen=next_seen)
    if isinstance(node, ast.Attribute):
        return _receiver_attribute_string_candidates(node, resolver, use=use, seen=next_seen)
    if not isinstance(node, ast.Name):
        return None
    if resolver.parameter(node.id, use) is not None:
        return None
    binding = resolver.binding(node.id, use)
    if binding is not None:
        return _constant_string_candidates(binding, resolver, use=binding, seen=next_seen)
    source = resolver.iteration_source(node.id, use)
    if source is None:
        return None
    target, iterable = source
    containers = _literal_container_candidates(iterable, resolver, use=use, seen=next_seen)
    if containers is None:
        return None
    position: int | None = None
    if isinstance(target, (ast.Tuple, ast.List)):
        slots = [element.id if isinstance(element, ast.Name) else None for element in target.elts]
        if node.id not in slots:
            return None
        position = slots.index(node.id)
    candidates: set[str] = set()
    for container in containers:
        for element in container.elts:
            member = element
            if position is not None:
                if not isinstance(element, (ast.Tuple, ast.List)) or position >= len(element.elts):
                    return None
                member = element.elts[position]
            values = _constant_string_candidates(member, resolver, use=member, seen=next_seen)
            if values is None:
                return None
            candidates.update(values)
    return frozenset(candidates)


def _literal_container_candidates(
    node: ast.expr,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int],
) -> tuple[ast.Tuple | ast.List, ...] | None:
    """Resolve an iterable expression to the literal tuples/lists it can be."""

    if id(node) in seen:
        return None
    next_seen = seen | {id(node)}
    if isinstance(node, (ast.Tuple, ast.List)):
        return (node,)
    if isinstance(node, ast.IfExp):
        branches = [_literal_container_candidates(branch, resolver, use=node, seen=next_seen) for branch in (node.body, node.orelse)]
        return None if any(branch is None for branch in branches) else tuple(item for branch in branches for item in (branch or ()))
    if isinstance(node, ast.Name) and resolver.parameter(node.id, use) is None:
        binding = resolver.binding(node.id, use)
        if binding is not None:
            return _literal_container_candidates(binding, resolver, use=binding, seen=next_seen)
    return None


def _subscript_string_candidates(
    node: ast.Subscript,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int],
) -> frozenset[str] | None:
    """Every string a subscript can select: the exact member for a constant key, else EVERY member.

    ``_DATABASE_CLOCK_SQL[engine.dialect.name]`` is admitted as the union of
    the dictionary's values, so one write value anywhere in the container
    keeps the site reported.  A non-literal container or a member that does
    not resolve returns ``None``.
    """

    exact = resolver.resolve_value(node, use=use)
    if exact is not node:
        return _constant_string_candidates(exact, resolver, use=exact, seen=seen)
    container = resolver.resolve_value(node.value, use=node)
    if isinstance(container, ast.Dict):
        # A ``**`` splat member is a Dict node, which the candidate resolver
        # refuses, so a splatted container fails closed without a guard here.
        members: Sequence[ast.expr] = container.values
    elif isinstance(container, (ast.Tuple, ast.List)):
        members = container.elts
    else:
        return None
    candidates: set[str] = set()
    for member in members:
        values = _constant_string_candidates(member, resolver, use=member, seen=seen)
        if values is None:
            return None
        candidates.update(values)
    return frozenset(candidates) if candidates else None


def _method_receiver(node: ast.AST) -> tuple[ast.ClassDef, str] | None:
    """Return ``(class, receiver name)`` when ``node`` sits directly in an instance method of a class."""

    owner = _owner_function(node)
    if owner is None:
        return None
    for ancestor in _ancestors(owner):
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return None
        if isinstance(ancestor, ast.ClassDef):
            break
    else:
        return None
    if any(_dotted_name(decorator) in {"staticmethod", "classmethod"} for decorator in owner.decorator_list):
        return None
    positional = (*owner.args.posonlyargs, *owner.args.args)
    return None if not positional else (ancestor, positional[0].arg)


def _receiver_attribute_string_candidates(
    node: ast.Attribute,
    resolver: _Resolver,
    *,
    use: ast.AST,
    seen: frozenset[int],
) -> frozenset[str] | None:
    """Every string ``self.<attr>`` can hold, when EVERY binding of it in the enclosing class is finite and static.

    The receiver must be the enclosing method's own instance parameter and
    the class must bind the attribute at least once; each binding must be a
    plain ``self.<attr> = …`` whose value resolves to constant candidates.  A
    tuple-unpacked or augmented binding, a binding on any other receiver, or
    a ``setattr`` call anywhere in the class returns ``None``: nothing is
    inferred from the attribute's name.
    """

    if not isinstance(node.value, ast.Name):
        return None
    located = _method_receiver(use)
    if located is None or node.value.id != located[1]:
        return None
    owner_class = located[0]
    candidates: set[str] = set()
    bindings = 0
    for member in ast.walk(owner_class):
        if isinstance(member, ast.Call) and _call_name(member) == "setattr":
            return None
        if isinstance(member, (ast.AugAssign, ast.AnnAssign)):
            targets: list[ast.expr] = [member.target]
        elif isinstance(member, ast.Assign):
            targets = list(member.targets)
        else:
            continue
        for target in targets:
            matches = [
                child
                for child in ast.walk(target)
                if isinstance(child, ast.Attribute) and child.attr == node.attr and isinstance(child.value, ast.Name)
            ]
            if not matches:
                continue
            binder = _method_receiver(member)
            if (
                not isinstance(member, ast.Assign)
                or target is not matches[0]
                or len(matches) != 1
                or binder is None
                or binder[0] is not owner_class
                or matches[0].value.id != binder[1]
            ):
                return None
            values = _constant_string_candidates(member.value, resolver, use=member.value, seen=seen)
            if values is None:
                return None
            candidates.update(values)
            bindings += 1
    return frozenset(candidates) if bindings else None


def _raw_sql_exact_texts(node: ast.expr, resolver: _Resolver, *, use: ast.AST) -> frozenset[str] | None:
    """Return every SQL text the statement can carry, when each is statically known in full.

    An f-string qualifies only when every interpolation has a finite constant
    candidate set (a DBAPI placeholder, a constant, or a loop variable over a
    literal table); anything else returns ``None`` so an accept decision can
    never be made on text the scanner has not seen in full.
    """

    payload = _raw_sql_text_payload(node, resolver)
    if payload is not None:
        return _raw_sql_exact_texts(payload, resolver, use=node)
    direct = _constant_string_value(node, resolver, use=use)
    if direct is not None:
        return frozenset({direct})
    if not isinstance(node, ast.JoinedStr):
        return _constant_string_candidates(node, resolver, use=use)
    texts: frozenset[str] = frozenset({""})
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            texts = frozenset(prefix + part.value for prefix in texts)
            continue
        if not isinstance(part, ast.FormattedValue) or part.format_spec is not None:
            return None
        interpolated = _constant_string_candidates(part.value, resolver, use=node)
        if interpolated is None:
            return None
        texts = frozenset(prefix + value for prefix in texts for value in interpolated)
    return texts


def _raw_sql_constant_skeleton(node: ast.expr, resolver: _Resolver, *, use: ast.AST) -> str | None:
    """Return the constant text of a statement, interpolations dropped.

    Only ever used to PROVE a write: dropping an interpolation cannot invent a
    write keyword, so a skeleton that matches one is a write whatever the
    interpolation carries.  It is never used to prove a read.
    """

    if _raw_sql_text_payload(node, resolver) is not None:
        # A ``text()`` payload is already classified by ``_raw_dml_shape`` at
        # the wrapper node itself; classifying it again here would report one
        # statement twice.
        return None
    direct = _constant_string_value(node, resolver, use=use)
    if direct is not None:
        return direct
    if not isinstance(node, ast.JoinedStr):
        return None
    parts = [part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)]
    return " ".join(parts) if parts else None


def _raw_dml_shape_from_text(value: str) -> tuple[str, str] | None:
    match = _RAW_DML_RE.search(_normalized_raw_sql(value))
    if match is None:
        return None
    raw_operation = match.group("operation").lower()
    operation = "insert" if raw_operation.startswith("insert") else "delete" if raw_operation.startswith("delete") else "update"
    return match.group("table").lower(), f"raw-{operation}"


def _raw_dml_shape(call: ast.Call, resolver: _Resolver) -> tuple[str, str] | None:
    value = _raw_sql_literal(call, resolver)
    return None if value is None else _raw_dml_shape_from_text(value)


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
    normalized = stable_ast_dump(_semantic_dml_boundary(node))
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
    return stable_ast_dump(node)


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
_TOKEN_CARRYING_CONTEXT_QUALIFIED = frozenset(
    {
        "elspeth.contracts.plugin_context.PluginContext",
        "elspeth.contracts.contexts.LifecycleContext",
        "elspeth.contracts.contexts.SourceContext",
        "elspeth.contracts.contexts.TransformContext",
        "elspeth.contracts.contexts.SinkContext",
    }
)
"""Context types a plugin may receive and forward through (ADR-048 §3).

Four of these are ``Protocol`` classes, so the annotation is NOT an authority
proof and is not used as one: admitting a ``ctx.record_*`` call DEFERS the proof
to whatever class actually forwards to Landscape, it never grants it. That
receiver's own forwarding call is scanned like any other and is admitted only by
``_context_attribute_token_is_carried_by_value``, which is keyed on a concrete
owned class at an exact path. A structural impostor therefore cannot launder an
unfenced write through here — it can only move where the proof is demanded.
"""

_TOKEN_CARRYING_CONTEXT_OWNER = ("src/elspeth/contracts/plugin_context.py", "PluginContext")
"""The one concrete class whose ``self``-attribute token is provable by value."""

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

_NON_LANDSCAPE_RECEIVER_OWNERS: frozenset[tuple[str, str]] = frozenset(
    {
        ("elspeth.web.sessions.protocol.SessionServiceProtocol", "update_run_status"),
        ("elspeth.web.composer.pipeline_planner._PlannerAttemptTrail", "begin_attempt"),
        ("elspeth.engine.orchestrator.ports.CoalesceCompletionPort", "mark_blocked_barrier_terminal"),
    }
)
"""(resolved owner, method) pairs whose NAME collides with a Landscape verb and which are not one.

``_mutation_callable_escapes`` is name-keyed and fail-closed: any attribute
spelled like a Landscape verb on a receiver it cannot prove is a Landscape
receiver becomes an ``unknown mutation receiver`` row.  That is the right
default — the ``LLMAuditParent`` indirection in the LLM providers is exactly
such a row and is REAL — but it also rows the Sessions service's own
``update_run_status`` and the composer planner's own attempt trail, neither of
which touches the Landscape.

Admission is keyed on the receiver's **resolved owner**, never on its name, and
never on the receiver resolving INTO an owned Landscape class.  An
UNRESOLVABLE receiver is not admitted; it stays a row.  A rule that admitted
every resolvable receiver would fail closed on the rows worth catching:
``LLMAuditParent`` resolves perfectly well and MUST keep rowing until D8.3
threads the token through it.

**Why the pair and not the owner alone.**  ``SessionServiceProtocol`` declares
85 methods.  Admitting the owner would silently admit every one of them that
ever collides with a Landscape verb name, including a future ``complete_run``.
Pinning the pair keeps the admission enumerable and bounds it to the two names
measured here.

**Why a ``Protocol`` annotation is sound HERE and is not an authority proof.**
ADR-032 forbids a Protocol as a security or dispatch control because an
impostor satisfies it structurally.  That argument is about granting authority.
This constant grants none: it only declines to raise a false ``unknown
receiver`` row, and it is reached ONLY after
``_looks_like_landscape_receiver`` has already failed to prove the receiver.
The inverted risk — a genuine Landscape write hiding behind a non-Landscape
annotation — is bounded by ``test_non_landscape_receiver_owners_are_pinned_to_the_tree``,
which re-derives every entry from the tree and asserts the owner is neither a
``_MUTATION_APIS`` owner nor a module under ``src/elspeth/core/landscape/``.
The blast radius is therefore exactly these two method names on these two
owners.
"""


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


def _self_attribute_owner_annotation(node: ast.Attribute, resolver: _Resolver, *, use: ast.AST) -> str | None:
    """Qualified annotation of ``self.<attr>`` when it is bound ONLY in ``__init__`` from one parameter.

    Mirrors the binding discipline of ``_context_attribute_token_is_carried_by_value``:
    a single ``__init__`` assignment from a plain annotated parameter that is never
    rebound, with no ``setattr`` anywhere in the class.  Any other shape — a second
    binding site, a rebinding, a computed value, a ``setattr`` — returns ``None`` and
    the caller keeps its row.
    """

    located = _method_receiver(use)
    if located is None or not isinstance(node.value, ast.Name) or node.value.id != located[1]:
        return None
    owner_class = located[0]
    annotations: set[str] = set()
    for member in ast.walk(owner_class):
        if isinstance(member, ast.Call) and _call_name(member) == "setattr":
            return None
        if not isinstance(member, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        targets = list(member.targets) if isinstance(member, ast.Assign) else [member.target]
        for target in targets:
            matches = [
                child
                for child in ast.walk(target)
                if isinstance(child, ast.Attribute) and child.attr == node.attr and isinstance(child.value, ast.Name)
            ]
            if not matches:
                continue
            binder = _method_receiver(member)
            if (
                not isinstance(member, ast.Assign)
                or target is not matches[0]
                or len(matches) != 1
                or binder is None
                or binder[0] is not owner_class
                or matches[0].value.id != binder[1]
            ):
                return None
            init = _owner_function(member)
            if init is None or init.name != "__init__" or not isinstance(member.value, ast.Name):
                return None
            parameter = next(
                (
                    argument
                    for argument in (*init.args.posonlyargs, *init.args.args, *init.args.kwonlyargs)
                    if argument.arg == member.value.id
                ),
                None,
            )
            if parameter is None or parameter.annotation is None or _parameter_rebound(init, parameter.arg):
                return None
            qualified = resolver.qualified_name(parameter.annotation, use=init)
            if qualified is None:
                return None
            annotations.add(qualified)
    return annotations.pop() if len(annotations) == 1 else None


def _resolved_non_landscape_receiver_owner(node: ast.AST, method: str, resolver: _Resolver, *, use: ast.AST) -> str | None:
    """Return the receiver's owner when it is a PINNED non-Landscape ``(owner, method)`` pair.

    Two receiver shapes resolve: a ``Name`` bound to a parameter of the enclosing
    function (``_Resolver.parameter`` walks out through nested scopes, which is how
    the planner's closure reaches its enclosing ``trail`` parameter), and a
    ``self.<attr>`` bound once in ``__init__``.  Everything else — a call result, a
    subscript, a module global, an unannotated parameter — is UNRESOLVABLE and
    returns ``None``, which keeps the row.  See ``_NON_LANDSCAPE_RECEIVER_OWNERS``.
    """

    qualified: str | None = None
    if isinstance(node, ast.Name):
        parameter = resolver.parameter(node.id, use)
        if parameter is None or parameter.annotation is None:
            return None
        qualified = resolver.qualified_name(parameter.annotation, use=use)
    elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        qualified = _self_attribute_owner_annotation(node, resolver, use=use)
    if qualified is None or (qualified, method) not in _NON_LANDSCAPE_RECEIVER_OWNERS:
        return None
    return qualified


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
    scope: str = _LEADER_SCOPE,
) -> bool:
    if not isinstance(node, ast.Name):
        return False
    arguments = (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)
    parameter = next((argument for argument in arguments if argument.arg == node.id), None)
    return (
        parameter is not None
        and _is_exact_scoped_authority_annotation(parameter.annotation, scope=scope, resolver=resolver, use=use)
        and _argument_default(owner, parameter.arg) is None
        and resolver.binding(node.id, use) is None
        and not _parameter_rebound(owner, node.id)
    )


def _admission_statement(node: ast.AST) -> ast.stmt | None:
    while not isinstance(node, ast.stmt):
        node = getattr(node, "_landscape_parent", None)
        if node is None:
            return None
    return node


def _admission_block(statement: ast.stmt) -> list[ast.stmt] | None:
    parent = getattr(statement, "_landscape_parent", None)
    if parent is None:
        return None
    for _field, value in ast.iter_fields(parent):
        if isinstance(value, list) and any(child is statement for child in value):
            return value
    return None


def _admission_unique_binding(name: str, owner: ast.FunctionDef | ast.AsyncFunctionDef, use: ast.AST) -> ast.expr | None:
    """One direct assignment dominating use in the same statement list.

    Deliberately refuses conditional/loop/handler assignments and rebinding.
    An assignment in a try body can prove later uses in that body: if its
    RHS raises, control cannot reach those uses. It cannot prove uses after
    the try statement, including when an exception handler swallows failure.
    """
    stores = [
        part
        for part in _walk_same_scope(owner)
        if isinstance(part, ast.Name) and part.id == name and isinstance(part.ctx, (ast.Store, ast.Del))
    ]
    if len(stores) != 1:
        return None
    statement = getattr(stores[0], "_landscape_parent", None)
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
        return None
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    if targets != [stores[0]]:
        return None
    use_statement = _admission_statement(use)
    block = _admission_block(statement)
    if block is None or use_statement is None or _admission_block(use_statement) is not block:
        return None
    if block.index(statement) >= block.index(use_statement):
        return None
    return statement.value


def _admission_owned_receiver(
    node: ast.expr,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    resolver: _Resolver,
    use: ast.AST,
    expected: str,
    seen: frozenset[int] = frozenset(),
) -> bool:
    """Resolve actual repository construction, never a receiver-name hint.

    The RecorderFactory entry points are owned repository-graph producers;
    the accepted projection is limited to their declared run_lifecycle field.
    """
    if id(node) in seen:
        return False
    seen = seen | {id(node)}
    if isinstance(node, ast.Name):
        value = _admission_unique_binding(node.id, owner, use)
        return value is not None and _admission_owned_receiver(value, owner, resolver, value, expected, seen)
    if isinstance(node, ast.Call):
        qualified = resolver.qualified_name(node.func, use=node)
        return qualified == expected and not _trusted_qualified_name_is_mutated(qualified, resolver=resolver, use=node)
    if not isinstance(node, ast.Attribute) or node.attr != "run_lifecycle":
        return False
    if expected != "elspeth.core.landscape.run_lifecycle_repository.RunLifecycleRepository":
        return False
    factory = "elspeth.core.landscape.factory.RecorderFactory"
    return any(
        _admission_owned_receiver(node.value, owner, resolver, use, qualified, seen) for qualified in (factory, factory + ".writable")
    )


def _authority_block_raises(statements: list[ast.stmt]) -> bool:
    if not statements:
        return False
    final = statements[-1]
    return isinstance(final, ast.Raise) or (
        isinstance(final, ast.If) and _authority_block_raises(final.body) and _authority_block_raises(final.orelse)
    )


def _authority_binding_origin(name: str, owner: ast.FunctionDef | ast.AsyncFunctionDef, use: ast.AST) -> ast.expr | None:
    """One unchanged binding on every path reaching this use.

    Uses may sit inside later nested statements. An assignment inside a try
    may reach a later statement only if every handler raises; conditional,
    iterative, and potentially suppressing context-manager assignments cannot
    certify a value outside their own body.
    """
    stores = [
        part
        for part in _walk_same_scope(owner)
        if isinstance(part, ast.Name) and part.id == name and isinstance(part.ctx, (ast.Store, ast.Del))
    ]
    if len(stores) != 1 or any(isinstance(part, (ast.Nonlocal, ast.Global)) and name in part.names for part in ast.walk(owner)):
        return None
    assignment = getattr(stores[0], "_landscape_parent", None)
    if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
        return None
    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
    if targets != [stores[0]]:
        return None
    use_statements: list[ast.stmt] = []
    current: ast.AST | None = use
    while current is not None and current is not owner:
        if isinstance(current, ast.stmt):
            use_statements.append(current)
        current = getattr(current, "_landscape_parent", None)
    if current is not owner:
        return None
    candidate: ast.stmt = assignment
    while True:
        block = _admission_block(candidate)
        if block is None:
            return None
        for statement in use_statements:
            if _admission_block(statement) is block and block.index(candidate) < block.index(statement):
                return assignment.value
        parent = getattr(candidate, "_landscape_parent", None)
        if not isinstance(parent, ast.Try) or block is not parent.body:
            return None
        if any(not _authority_block_raises(handler.body) for handler in parent.handlers):
            return None
        if any(isinstance(part, (ast.Return, ast.Break, ast.Continue)) for statement in parent.finalbody for part in ast.walk(statement)):
            return None
        candidate = parent


def _authority_value_mutated(name: str, owner: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Reject explicit mutation of an authority value, including frozen-object escapes."""
    aliases = {name}
    while True:
        previous = set(aliases)
        for part in ast.walk(owner):
            if isinstance(part, (ast.Assign, ast.AnnAssign)) and isinstance(part.value, ast.Name) and part.value.id in aliases:
                targets = part.targets if isinstance(part, ast.Assign) else [part.target]
                aliases.update(target.id for target in targets if isinstance(target, ast.Name))
        if aliases == previous:
            break

    def carried(value: ast.AST) -> bool:
        while isinstance(value, (ast.Attribute, ast.Subscript)):
            value = value.value
        return isinstance(value, ast.Name) and value.id in aliases

    for part in ast.walk(owner):
        if (
            isinstance(part, ast.Attribute)
            and carried(part.value)
            and (isinstance(part.ctx, (ast.Store, ast.Del)) or part.attr == "__dict__")
        ):
            return True
        if not isinstance(part, ast.Call):
            continue
        method = _call_name(part)
        if method in {"setattr", "__setattr__", "delattr", "__delattr__", "vars"}:
            if part.args and carried(part.args[0]):
                return True
            if isinstance(part.func, ast.Attribute) and carried(part.func.value):
                return True
    return False


def _fresh_epoch_one_creation_authority(
    node: ast.expr, owner: ast.FunctionDef | ast.AsyncFunctionDef, resolver: _Resolver, scope: str
) -> bool:
    """Prove the registered fresh leader identity, not an arbitrary constructor."""
    if scope != _LEADER_SCOPE or not isinstance(node, ast.Call) or node.args:
        return False
    constructor = "elspeth.contracts.coordination.CoordinationToken"
    if resolver.qualified_name(node.func, use=node) != constructor:
        return False
    if _trusted_qualified_name_is_mutated(constructor, resolver=resolver, use=node):
        return False
    if len(node.keywords) != 3 or {kw.arg for kw in node.keywords} != {"run_id", "worker_id", "leader_epoch"}:
        return False
    arguments = {kw.arg: kw.value for kw in node.keywords}
    epoch = arguments["leader_epoch"]
    if not isinstance(epoch, ast.Constant) or type(epoch.value) is not int or epoch.value != 1:
        return False
    run_id = arguments["run_id"]
    worker = arguments["worker_id"]
    if not isinstance(run_id, ast.Attribute) or run_id.attr != "run_id" or not isinstance(run_id.value, ast.Name):
        return False
    if not isinstance(worker, ast.Name):
        return False
    creation = _admission_unique_binding(run_id.value.id, owner, node)
    if not isinstance(creation, ast.Call) or not isinstance(creation.func, ast.Attribute) or creation.func.attr != "begin_run":
        return False
    if any(kw.arg is None for kw in creation.keywords):
        return False
    registered_workers = [kw.value for kw in creation.keywords if kw.arg == "leader_worker_id"]
    if len(registered_workers) != 1 or not isinstance(registered_workers[0], ast.Name) or registered_workers[0].id != worker.id:
        return False
    # One stable local recipe or an unchanged required input can name the
    # worker inserted by begin_run. No independent membership lookup/mint.
    parameter = resolver.parameter(worker.id, node)
    if parameter is not None:
        if _parameter_rebound(owner, worker.id):
            return False
    elif _admission_unique_binding(worker.id, owner, creation) is None:
        return False
    # Owned Run is frozen. Explicit escape hatches and attribute writes are
    # not compatible with proving its returned identity.
    for part in _walk_same_scope(owner):
        if isinstance(part, ast.Attribute) and (
            part.attr == "__dict__"
            or (part.attr in {"run_id", "begin_run", "run_lifecycle"} and isinstance(part.ctx, (ast.Store, ast.Del)))
        ):
            return False
        if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__", "vars"}:
            return False
    if any(isinstance(part, ast.Nonlocal) for part in ast.walk(owner)):
        return False
    return _admission_owned_receiver(
        creation.func.value,
        owner,
        resolver,
        creation,
        "elspeth.core.landscape.run_lifecycle_repository.RunLifecycleRepository",
    )


def _authority_return_body_is_visible(function: ast.FunctionDef | ast.AsyncFunctionDef, resolver: _Resolver) -> bool:
    """A decorator may replace the function whose source is being proved."""
    if not function.decorator_list:
        return True
    if len(function.decorator_list) != 1 or not isinstance(getattr(function, "_landscape_parent", None), ast.ClassDef):
        return False
    decorator = function.decorator_list[0]
    if not isinstance(decorator, ast.Name) or decorator.id not in {"classmethod", "staticmethod"}:
        return False
    if resolver.binding(decorator.id, function) is not None:
        return False
    return resolver.qualified_name(decorator, use=function) in {None, decorator.id, "builtins." + decorator.id}


def _authority_factory_return_contract(
    proof: _AuthorityProof, function: ast.FunctionDef | ast.AsyncFunctionDef, resolver: _Resolver, scope: str
) -> bool:
    """Check actual returned subjects of the three established factory APIs."""
    key = (resolver.unit.path, _symbol(function))
    expected_scope = {
        "RunCoordinationRepository.acquire_run_leadership": _LEADER_SCOPE,
        "RunCoordinationRepository.acquire_export_leadership": _LEADER_SCOPE,
        "RunCoordinationRepository.admit_follower": _MEMBER_SCOPE,
    }.get(key[1])
    if expected_scope == scope and key in _proven_coordination_deadline_writers(proof.units):
        return True
    path = "src/elspeth/core/landscape/run_coordination_repository.py"
    scopes = {
        "RunCoordinationRepository.acquire_run_leadership": _LEADER_SCOPE,
        "RunCoordinationRepository.acquire_export_leadership": _LEADER_SCOPE,
        "RunCoordinationRepository.admit_follower": _MEMBER_SCOPE,
    }
    if resolver.unit.path != path or scopes.get(_symbol(function)) != scope:
        return False
    if any(_parameter_rebound(function, name) for name in ("run_id", "worker_id")):
        return False
    returns = [part for part in _walk_same_scope(function) if isinstance(part, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Call):
        return False
    returned = returns[0].value
    if scope == _LEADER_SCOPE:
        helper_name = "_" + function.name + "_on"
        if not isinstance(returned.func, ast.Attribute) or _dotted_name(returned.func) != "self." + helper_name:
            return False
        if len(returned.args) != 1 or not isinstance(returned.args[0], ast.Name) or returned.args[0].id != "conn":
            return False
        params = ("run_id", "worker_id", "window_seconds")
        if function.name == "acquire_run_leadership":
            params += ("entry_point",)
        if {kw.arg for kw in returned.keywords} != set(params) or len(returned.keywords) != len(params):
            return False
        if any(not isinstance(kw.value, ast.Name) or kw.value.id != kw.arg for kw in returned.keywords):
            return False
        helper = proof.called_function(returned, resolver, returned)
        if helper is None:
            return False
        function, resolver = helper
        returns = [part for part in _walk_same_scope(function) if isinstance(part, ast.Return)]
        if len(returns) != 1 or not isinstance(returns[0].value, ast.Call):
            return False
        returned = returns[0].value
        if any(_parameter_rebound(function, name) for name in ("run_id", "worker_id")):
            return False
    expected = "elspeth.contracts.coordination." + ("CoordinationToken" if scope == _LEADER_SCOPE else "WorkerMembershipToken")
    if resolver.qualified_name(returned.func, use=returned) != expected or returned.args:
        return False
    subjects = {"run_id", "worker_id"}
    required = subjects | ({"leader_epoch"} if scope == _LEADER_SCOPE else set())
    if len(returned.keywords) != len(required) or {kw.arg for kw in returned.keywords} != required:
        return False
    payload = {kw.arg: kw.value for kw in returned.keywords}
    if any(not isinstance(payload[name], ast.Name) or payload[name].id != name for name in subjects):
        return False
    if scope == _LEADER_SCOPE:
        epoch = payload["leader_epoch"]
        if not isinstance(epoch, ast.Name):
            return False
        value = _admission_unique_binding(epoch.id, function, returned)
        expected_epoch = ast.parse("int(seat.leader_epoch) + 1", mode="eval").body
        if value is None or stable_ast_dump(value) != stable_ast_dump(expected_epoch):
            return False
    return True


def _authority_relay_block_terminates(statements: list[ast.stmt]) -> bool:
    if not statements:
        return False
    final = statements[-1]
    if isinstance(final, (ast.Return, ast.Raise)):
        return True
    return isinstance(final, ast.If) and _authority_relay_block_terminates(final.body) and _authority_relay_block_terminates(final.orelse)


def _receiver_owned_class(proof, qualified):
    exports = proof._receiver_exports
    visited = set()
    while qualified in exports and qualified not in visited:
        visited.add(qualified)
        qualified = exports[qualified]
    if qualified in visited:
        return None
    found = proof.class_for(qualified)
    return found if found is not None and _receiver_class_body_is_visible(proof, *found) else None


def _receiver_binding_dominates(value, use):
    binding = _admission_statement(value)
    if not isinstance(binding, (ast.Assign, ast.AnnAssign)):
        return False
    block = _admission_block(binding)
    current = _admission_statement(use)
    while current is not None:
        if _admission_block(current) is block:
            return block.index(binding) < block.index(current)
        current = _admission_statement(getattr(current, "_landscape_parent", None))
    return False


def _receiver_field(proof, cls, resolver, field, use, seen, allow_none=False, parameter_values=None):
    parameter_values = {} if parameter_values is None else parameter_values
    caller = _owner_function(use)
    if caller is not None:
        for part in ast.walk(caller):
            if (
                isinstance(part, ast.Attribute)
                and part.attr == field
                and isinstance(part.ctx, (ast.Store, ast.Del))
                and _owner_function(part).name != "__init__"
            ):
                return None
            if isinstance(part, ast.Attribute) and part.attr == "__dict__":
                return None
            if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__", "vars"}:
                return None
    # Resolve overriding __init__ before following a base property's field.
    initializer = proof.member(cls, resolver, "__init__")
    writes = []
    lineage = [(cls, resolver)]
    visited = set()
    while lineage:
        current, current_resolver = lineage.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        for declaration in current.body:
            if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef)) and declaration.name == field:
                return None
            if isinstance(declaration, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = declaration.targets if isinstance(declaration, ast.Assign) else [declaration.target]
                if any(isinstance(target, ast.Name) and target.id == field for target in targets) and (
                    not isinstance(declaration, ast.AnnAssign)
                    or (declaration.value is not None and (id(current), field) not in parameter_values)
                ):
                    return None
        for part in ast.walk(current):
            if isinstance(part, ast.Attribute) and part.attr == "__dict__":
                return None
            if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__", "vars"}:
                return None
            if not isinstance(part, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = part.targets if isinstance(part, ast.Assign) else [part.target]
            for target in targets:
                if not isinstance(target, ast.Attribute) or target.attr != field:
                    continue
                located = _method_receiver(part)
                owner = _owner_function(part)
                if (
                    isinstance(part, ast.AugAssign)
                    or part.value is None
                    or located is None
                    or not isinstance(target.value, ast.Name)
                    or target.value.id != located[1]
                    or owner is None
                    or owner.name != "__init__"
                ):
                    return None
                # An overriding initializer's field is authoritative. A base
                # initializer cannot certify it by annotation or matching name.
                if initializer is not None and owner is initializer[0]:
                    writes.append((part.value, current_resolver, part))
        for base in current.bases:
            qualified = current_resolver.qualified_name(base, use=current)
            if qualified == "object" or (
                isinstance(base, ast.Name) and base.id == "object" and current_resolver.binding("object", current) is None
            ):
                continue
            parent = _receiver_owned_class(proof, qualified)
            if parent is None:
                return None
            lineage.append(parent)
    if len(writes) == 1:
        value, value_resolver, assignment = writes[0]
        if isinstance(value, ast.IfExp):
            arms = (value.body, value.orelse)
            present = [arm for arm in arms if not (isinstance(arm, ast.Constant) and arm.value is None)]
            if not allow_none or len(present) != 1:
                return None
            value = present[0]
        return proof.receiver(value, value_resolver, assignment, seen, parameter_values)
    if writes:
        return None
    member = proof.member(cls, resolver, field)
    if member is not None and isinstance(member[0], ast.AnnAssign):
        actual = parameter_values.get((id(cls), field))
        if actual is not None:
            value, value_resolver, value_use = actual
            return proof.receiver(value, value_resolver, value_use, seen, parameter_values)
        if (id(cls), "<known-constructor>") in parameter_values:
            return None
        # Retain existing source-owned typed carrier field resolution. This
        # identifies the receiver class, never an authority token value.
        return _receiver_owned_class(proof, member[2].qualified_name(member[0].annotation, use=member[0]))
    return None


def _callback_statement_dominates(binding, use, owner):
    """A direct binding, or a binding in a try whose handlers all raise."""
    current = binding
    while True:
        block = _admission_block(current)
        if block is None:
            return False
        containing_use = next((stmt for stmt in block if _is_descendant(use, stmt)), None)
        if containing_use is not None:
            return block.index(current) < block.index(containing_use)
        parent = getattr(current, "_landscape_parent", None)
        if not isinstance(parent, ast.Try) or block is not parent.body:
            return False
        # No handler may fall through to the use with the binding absent.
        # Restrict to explicit raising tails, including the production handler.
        if any(not handler.body or not isinstance(handler.body[-1], ast.Raise) for handler in parent.handlers):
            return False
        if parent.orelse or parent.finalbody:
            return False
        current = parent
        if current is owner:
            return False


def _callback_reflective_consumer_escape(proof, owner):
    def targets_consumer(receiver, resolver, use):
        carrier = proof.receiver(receiver, resolver, use)
        if carrier is None:
            return False
        method = proof.member(*carrier, owner.name)
        return method is not None and method[0] is owner

    for unit in proof.units:
        if not unit.path.startswith("src/elspeth/"):
            continue
        resolver = _resolver_for_unit(unit)
        for part in ast.walk(unit.tree):
            receiver = None
            attribute = None
            if isinstance(part, ast.Call):
                operation = _resolved_callable_name(part.func, resolver, use=part)
                callable_origin = resolver.resolve_callable(part.func, use=part)
                if operation == "getattr" and len(part.args) >= 2:
                    receiver, attribute = part.args[:2]
                elif operation == "__getattribute__" and isinstance(callable_origin, ast.Attribute):
                    if len(part.args) >= 2:
                        receiver, attribute = part.args[:2]
                    elif len(part.args) == 1:
                        receiver, attribute = callable_origin.value, part.args[0]
                elif operation == "vars" and part.args:
                    # The returned dictionary exposes the consumer even if
                    # its key lookup occurs later behind opaque kwargs.
                    if targets_consumer(part.args[0], resolver, part):
                        return True
                elif isinstance(callable_origin, ast.Call) and part.args:
                    operator = callable_origin
                    if _resolved_callable_name(operator.func, resolver, use=operator) in {"attrgetter", "methodcaller"} and operator.args:
                        receiver, attribute = part.args[0], operator.args[0]
            elif isinstance(part, ast.Attribute) and part.attr == "__dict__":
                if targets_consumer(part.value, resolver, part):
                    return True
            if receiver is not None and attribute is not None and targets_consumer(receiver, resolver, part):
                attribute_name = _constant_string_value(attribute, resolver, use=part)
                if attribute_name is None or attribute_name == owner.name:
                    return True
    return False


def _receiver_class_body_is_visible(proof, cls, resolver, seen=frozenset()):
    if id(cls) in seen or cls.keywords:
        return False
    seen = seen | {id(cls)}
    if any(
        isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) and part.name in {"__new__", "__getattribute__", "__getattr__"}
        for part in cls.body
    ):
        return False
    if cls.decorator_list:
        if len(cls.decorator_list) != 1:
            return False
        decorator = cls.decorator_list[0]
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if resolver.qualified_name(target, use=cls) != "dataclasses.dataclass":
            return False
        if _trusted_qualified_name_is_mutated("dataclasses.dataclass", resolver=resolver, use=cls):
            return False
        if isinstance(decorator, ast.Call) and (
            decorator.args or any(kw.arg is None or not isinstance(kw.value, ast.Constant) for kw in decorator.keywords)
        ):
            return False
    for base in cls.bases:
        qualified = resolver.qualified_name(base, use=cls)
        if qualified in {"object", "builtins.object"}:
            continue
        parent = proof.class_for(qualified)
        if parent is None or not _receiver_class_body_is_visible(proof, *parent, seen):
            return False
        if any(isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) and part.name == "__init_subclass__" for part in parent[0].body):
            return False
    return True


def _receiver_call_parameters(function, call, resolver, use, inherited):
    """Bind actual inputs before interpreting any returned receiver expression."""
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
        return None
    values = dict(inherited)
    positional = [*function.args.posonlyargs, *function.args.args]
    is_static = (
        len(function.decorator_list) == 1
        and isinstance(function.decorator_list[0], ast.Name)
        and function.decorator_list[0].id == "staticmethod"
    )
    if isinstance(getattr(function, "_landscape_parent", None), ast.ClassDef) and positional and not is_static:
        first = positional.pop(0)
        if isinstance(call.func, ast.Attribute):
            values[(id(function), first.arg)] = call.func.value, resolver, use
    if len(call.args) > len(positional):
        return None
    for index, parameter in enumerate(positional):
        actuals = [kw.value for kw in call.keywords if kw.arg == parameter.arg]
        if index < len(call.args):
            actuals.append(call.args[index])
        if len(actuals) > 1:
            return None
        if actuals:
            values[(id(function), parameter.arg)] = actuals[0], resolver, use
        elif (default := _argument_default(function, parameter.arg)) is not None:
            values[(id(function), parameter.arg)] = default, _resolver_for_node(function), function
        else:
            values[(id(function), parameter.arg)] = ast.Constant(value=None), resolver, use
    for parameter in function.args.kwonlyargs:
        actuals = [kw.value for kw in call.keywords if kw.arg == parameter.arg]
        if len(actuals) > 1:
            return None
        if actuals:
            values[(id(function), parameter.arg)] = actuals[0], resolver, use
        elif (default := _argument_default(function, parameter.arg)) is not None:
            values[(id(function), parameter.arg)] = default, _resolver_for_node(function), function
        else:
            values[(id(function), parameter.arg)] = ast.Constant(value=None), resolver, use
    return values


def _receiver_dataclass_inputs(cls, class_resolver, call, resolver, use, inherited):
    """Bind fields of an inspected generated constructor to this invocation."""
    if cls.bases or len(cls.decorator_list) != 1:
        return None
    decorator = cls.decorator_list[0]
    decorator_func = decorator.func if isinstance(decorator, ast.Call) else decorator
    if class_resolver.qualified_name(decorator_func, use=cls) != "dataclasses.dataclass":
        return None
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(keyword.arg is None for keyword in call.keywords):
        return None
    fields = [part for part in cls.body if isinstance(part, ast.AnnAssign) and isinstance(part.target, ast.Name)]
    for field in fields:
        annotation = field.annotation.value if isinstance(field.annotation, ast.Subscript) else field.annotation
        if isinstance(annotation, ast.Constant) or class_resolver.qualified_name(annotation, use=field) in {
            "typing.ClassVar",
            "dataclasses.InitVar",
        }:
            return None
        if isinstance(field.value, ast.Call) and class_resolver.qualified_name(field.value.func, use=field.value) == "dataclasses.field":
            # init=False, per-field kw_only, and default_factory alter generated
            # constructor semantics. They need an explicit proof before use.
            return None
    keyword_only = isinstance(decorator, ast.Call) and any(
        keyword.arg == "kw_only" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        for keyword in decorator.keywords
    )
    if len(call.args) > len(fields) or (keyword_only and call.args):
        return None
    if any(keyword.arg not in {field.target.id for field in fields} for keyword in call.keywords):
        return None
    values = dict(inherited)
    values[(id(cls), "<known-constructor>")] = ast.Constant(value=None), resolver, use
    for index, field in enumerate(fields):
        arguments = [keyword.value for keyword in call.keywords if keyword.arg == field.target.id]
        if not keyword_only and index < len(call.args):
            arguments.append(call.args[index])
        if len(arguments) > 1:
            return None
        if arguments:
            values[(id(cls), field.target.id)] = arguments[0], resolver, use
        elif field.value is not None:
            values[(id(cls), field.target.id)] = field.value, class_resolver, field
        else:
            values[(id(cls), field.target.id)] = ast.Constant(value=None), resolver, use
    return values


def _receiver_constructor_environment(proof, expression, resolver, use, parameter_values, seen=frozenset()):
    """Follow the actual construction supplying a projected instance field."""
    if id(expression) in seen:
        return None
    seen = seen | {id(expression)}
    if isinstance(expression, ast.Name):
        owner = _owner_function(use)
        actual = parameter_values.get((id(owner), expression.id))
        if actual is not None:
            return _receiver_constructor_environment(proof, *actual, parameter_values, seen)
        binding = resolver.binding(expression.id, use)
        if binding is not None and _receiver_binding_dominates(binding, use):
            return _receiver_constructor_environment(proof, binding, resolver, binding, parameter_values, seen)
        return None
    if not isinstance(expression, ast.Call):
        return None
    cls = _receiver_owned_class(proof, resolver.qualified_name(expression.func, use=use))
    if cls is not None:
        initializer = proof.member(*cls, "__init__")
        if initializer is None or not isinstance(initializer[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            fields = _receiver_dataclass_inputs(*cls, expression, resolver, use, parameter_values)
            if fields is not None:
                return fields
            values = dict(parameter_values)
            values[(id(cls[0]), "<known-constructor>")] = ast.Constant(value=None), resolver, use
            return values
        return _receiver_call_parameters(initializer[0], expression, resolver, use, parameter_values)
    producer = proof.called_function(expression, resolver, use, parameter_values)
    if producer is None:
        return None
    returns = [part for part in _walk_same_scope(producer[0]) if isinstance(part, ast.Return)]
    if len(returns) != 1 or returns[0].value is None or not _authority_relay_block_terminates(producer[0].body):
        return None
    substituted = _receiver_call_parameters(producer[0], expression, resolver, use, parameter_values)
    if substituted is None:
        return None
    return _receiver_constructor_environment(proof, returns[0].value, producer[1], returns[0], substituted, seen)


def _receiver_callable_scopes(use):
    roots = []
    current = use
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            roots.append(current)
        elif isinstance(current, ast.Module):
            roots.extend(part for part in current.body if not isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        current = getattr(current, "_landscape_parent", None)
    return roots


def _receiver_callable_code_is_visible(function, use):
    roots = [*_receiver_callable_scopes(use), *_receiver_callable_scopes(function)]
    for root in roots:
        for part in ast.walk(root):
            if isinstance(part, ast.Attribute) and part.attr in {"__code__", "__defaults__", "__kwdefaults__", "__globals__"}:
                return False
    return True


def _receiver_method_is_unmodified(proof, cls, class_resolver, method, use):
    """Refuse visible replacement, including reflective writes and aliases.

    The check is deliberately conservative within the invocation's enclosing
    scopes and the receiver class hierarchy: a same-name write on an unresolved
    alias cannot certify that the receiver retains its source method.
    """
    roots = _receiver_callable_scopes(use)
    lineage = [(cls, class_resolver)]
    seen = set()
    while lineage:
        current_class, current_resolver = lineage.pop()
        if id(current_class) in seen:
            continue
        seen.add(id(current_class))
        roots.append(current_class)
        bindings = []
        for declaration in current_class.body:
            if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if declaration.name == method:
                    bindings.append(declaration)
                continue
            bindings.extend(
                part
                for part in ast.walk(declaration)
                if isinstance(part, ast.Name) and part.id == method and isinstance(part.ctx, (ast.Store, ast.Del))
            )
        if len(bindings) > 1 or any(not isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) for part in bindings):
            return False
        # Include module mutations of a class even when its consumer imported
        # it from another source unit.
        module = current_class
        while not isinstance(module, ast.Module):
            module = getattr(module, "_landscape_parent", None)
            if module is None:
                return False
        roots.extend(part for part in module.body if not isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        for base in current_class.bases:
            if isinstance(base, ast.Name) and base.id == "object" and current_resolver.binding("object", current_class) is None:
                continue
            parent = _receiver_owned_class(proof, current_resolver.qualified_name(base, use=current_class))
            if parent is None:
                return False
            lineage.append(parent)
    for root in roots:
        for part in ast.walk(root):
            if isinstance(part, ast.Attribute):
                if part.attr in {method, "__class__"} and isinstance(part.ctx, (ast.Store, ast.Del)):
                    return False
                # Exposing a dictionary permits a later write through another
                # binding; refuse the exposure, not only immediate subscripts.
                if part.attr in {"__dict__", "__code__", "__defaults__", "__kwdefaults__", "__globals__"}:
                    return False
                if part.attr in {"__setattr__", "__delattr__"}:
                    parent = getattr(part, "_landscape_parent", None)
                    if not isinstance(parent, ast.Call) or parent.func is not part:
                        return False
            if isinstance(part, ast.Call):
                name = _call_name(part)
                qualified = _resolver_for_node(part).qualified_name(part.func, use=part)
                if qualified in {"builtins.setattr", "builtins.delattr", "builtins.vars", "builtins.locals"}:
                    name = qualified.rsplit(".", 1)[-1]
                if name in {"vars", "locals"}:
                    return False
                if name in {"setattr", "__setattr__", "delattr", "__delattr__"} and (
                    len(part.args) < 2 or not isinstance(part.args[1], ast.Constant) or part.args[1].value in {method, "__class__"}
                ):
                    return False
            # Aliases of the reflective builtin must be refused as well.
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load) and part.id in {"setattr", "delattr", "vars"}:
                parent = getattr(part, "_landscape_parent", None)
                if not isinstance(parent, ast.Call) or parent.func is not part:
                    return False
    return True


class _AuthorityProof:
    """Resolve owned authority carriers and inspect their actual getter bodies.

    Names alone never grant authority: a getter must return its constructor-
    supplied nominal field after a concrete fail-closed check. Inherited methods
    resolve through the source hierarchy, including overrides.
    """

    def __init__(self, units: tuple[SourceUnit, ...]) -> None:
        self.units = units
        self._receiver_exports = {**_owned_reexports(units), **_lazy_owned_reexports(units)}
        self._callback_suppliers_cache = {}
        self._callback_function_mutation_cache = {}
        self.classes = {
            f"{unit.path.removeprefix('src/').removesuffix('.py').replace('/', '.')}.{node.name}": (node, _resolver_for_unit(unit))
            for unit in units
            if unit.path.startswith("src/elspeth/")
            for node in unit.tree.body
            if isinstance(node, ast.ClassDef)
        }

    def class_for(self, qualified: str | None) -> tuple[ast.ClassDef, _Resolver] | None:
        aliases = {
            "elspeth.contracts.PluginContext": "elspeth.contracts.plugin_context.PluginContext",
            "elspeth.contracts.contexts.PluginContext": "elspeth.contracts.plugin_context.PluginContext",
        }
        return self.classes.get(aliases.get(qualified, qualified)) if qualified is not None else None

    def called_function(self, call, resolver, use, parameter_values=None):
        if isinstance(call.func, ast.Attribute):
            owner = self.receiver(call.func.value, resolver, use, parameter_values=parameter_values)
            if owner is not None:
                if not _receiver_method_is_unmodified(self, *owner, call.func.attr, use):
                    return None
                member = self.member(*owner, call.func.attr)
                if member is not None and isinstance(member[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not _authority_return_body_is_visible(member[0], member[2]) or not _receiver_callable_code_is_visible(
                        member[0], use
                    ):
                        return None
                    return member[0], member[2]
        qualified = resolver.qualified_name(call.func, use=use)
        if qualified is None:
            return None
        path = _module_path_from_qualified(qualified)
        function = _function_index(self.units).get((path, qualified.rsplit(".", maxsplit=1)[-1])) if path is not None else None
        if function is None:
            return None
        function_resolver = _resolver_for_node(function)
        if not _authority_return_body_is_visible(function, function_resolver) or not _receiver_callable_code_is_visible(function, use):
            return None
        if "." in qualified and _trusted_qualified_name_is_mutated(qualified, resolver=resolver, use=use):
            return None
        return function, function_resolver

    def _callback_function_unmodified(self, function):
        if id(function) in self._callback_function_mutation_cache:
            return self._callback_function_mutation_cache[id(function)]
        valid = True
        located = _method_receiver(function)
        for unit in self.units:
            if not unit.path.startswith("src/elspeth/"):
                continue
            for part in ast.walk(unit.tree):
                if isinstance(part, ast.Attribute) and part.attr == function.name and isinstance(part.ctx, (ast.Store, ast.Del)):
                    valid = False
                if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__"} and len(part.args) >= 2:
                    if isinstance(part.args[1], ast.Constant) and part.args[1].value == function.name:
                        valid = False
                    target = self.receiver(part.args[0], _resolver_for_unit(unit), part)
                    if target is not None and located is not None and target[0] is located[0]:
                        valid = False
                if isinstance(part, ast.Subscript) and isinstance(part.ctx, (ast.Store, ast.Del)):
                    container = part.value
                    reflective = isinstance(container, ast.Attribute) and container.attr == "__dict__"
                    reflective = reflective or (isinstance(container, ast.Call) and _call_name(container) == "vars")
                    if reflective and isinstance(part.slice, ast.Constant) and part.slice.value == function.name:
                        valid = False
        self._callback_function_mutation_cache[id(function)] = valid
        return valid

    def _closed_callback_suppliers(self, owner, parameter, resolver):
        key = (id(owner), parameter.arg)
        if key not in self._callback_suppliers_cache:
            self._callback_suppliers_cache[key] = self._inspect_callback_suppliers(owner, parameter, resolver)
        return self._callback_suppliers_cache[key]

    def _inspect_callback_suppliers(self, owner, parameter, resolver):
        if _callback_reflective_consumer_escape(self, owner):
            return ()
        # This is the audited callable interface, not an authority grant. Every
        # source supplier and every returned payload must still prove its body.
        if resolver.qualified_name(parameter.annotation, use=owner) != "elspeth.engine.orchestrator.run_lifecycle.InitializeDatabasePhase":
            return ()
        if owner.decorator_list or _argument_default(owner, parameter.arg) is not None or _parameter_rebound(owner, parameter.arg):
            return ()
        if any(isinstance(part, (ast.Nonlocal, ast.Global)) for part in ast.walk(owner)):
            return ()
        for part in ast.walk(owner):
            if isinstance(part, ast.Name) and part.id == parameter.arg and isinstance(part.ctx, ast.Load):
                parent = getattr(part, "_landscape_parent", None)
                if not isinstance(parent, ast.Call) or parent.func is not part or _owner_function(parent) is not owner:
                    return ()  # Alias, container storage, return, or closure escape.
        suppliers = []
        for unit in self.units:
            if not unit.path.startswith("src/elspeth/"):
                continue
            source_resolver = _resolver_for_unit(unit)
            for part in ast.walk(unit.tree):
                if isinstance(part, ast.Call):
                    arguments = [kw.value for kw in part.keywords if kw.arg == parameter.arg]
                    if not arguments and _resolved_callable_name(part.func, source_resolver, use=part) != owner.name:
                        continue
                    target = self.called_function(part, source_resolver, part)
                    if arguments and (target is None or target[0] is not owner):
                        return ()  # Unknown receiver/caller cannot supply this seam.
                    if target is None or target[0] is not owner:
                        continue
                    if len(arguments) != 1 or any(kw.arg is None for kw in part.keywords):
                        return ()
                    supplier = arguments[0]
                    # Callable is supplied directly, never via a local alias,
                    # conditional, partial, opaque getter, or external import.
                    if not isinstance(supplier, (ast.Attribute, ast.Name)):
                        return ()
                    supplier_call = ast.Call(func=supplier, args=[], keywords=[])
                    implementation = self.called_function(supplier_call, source_resolver, supplier)
                    if implementation is None or implementation[0].decorator_list:
                        return ()
                    caller = _owner_function(part)
                    if caller is None:
                        return ()
                    if isinstance(supplier, ast.Attribute):
                        receiver_name = _dotted_name(supplier.value)
                        if receiver_name and _subject_rebound(caller, receiver_name):
                            return ()
                        if any(
                            isinstance(node, ast.Attribute) and node.attr == supplier.attr and isinstance(node.ctx, (ast.Store, ast.Del))
                            for node in ast.walk(unit.tree)
                        ):
                            return ()
                    elif _subject_rebound(caller, supplier.id):
                        return ()
                    suppliers.append(implementation)
                elif isinstance(part, (ast.Attribute, ast.Name)) and isinstance(part.ctx, ast.Load):
                    # A reference to the consumer method that escapes direct
                    # invocation leaves its caller set open and is rejected.
                    parent = getattr(part, "_landscape_parent", None)
                    if isinstance(parent, ast.Call) and parent.func is part:
                        continue
                    if _resolved_callable_name(part, source_resolver, use=part) != owner.name:
                        continue
                    target = self.called_function(ast.Call(func=part, args=[], keywords=[]), source_resolver, part)
                    if target is not None and target[0] is owner:
                        return ()
        # Inspect source writes to every supplier method and the consumer.
        # Refuse reflective mutation as well as ordinary attribute replacement.
        protected = {owner.name, *(function.name for function, _ in suppliers)}
        protected_classes = {
            id(_method_receiver(function)[0])
            for function in (owner, *(function for function, _ in suppliers))
            if _method_receiver(function) is not None
        }
        for unit in self.units:
            source_resolver = _resolver_for_unit(unit)
            for part in ast.walk(unit.tree):
                if isinstance(part, ast.Attribute) and part.attr in protected and isinstance(part.ctx, (ast.Store, ast.Del)):
                    receiver_class = self.receiver(part.value, source_resolver, part)
                    if receiver_class is not None and id(receiver_class[0]) in protected_classes:
                        return ()
                if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__"}:
                    receiver_class = self.receiver(part.args[0], source_resolver, part) if part.args else None
                    if receiver_class is not None and id(receiver_class[0]) in protected_classes:
                        return ()
        return tuple(suppliers)

    def _tuple_payload(self, function, resolver, index, arity, scope, seen):
        if id(function) in seen or function.decorator_list or not _authority_relay_block_terminates(function.body):
            return False
        if not self._callback_function_unmodified(function):
            return False
        if any(isinstance(part, (ast.Nonlocal, ast.Global)) for part in ast.walk(function)):
            return False
        seen = seen | {id(function)}
        returned = [part for part in _walk_same_scope(function) if isinstance(part, ast.Return)]
        if not returned:
            return False
        for statement in returned:
            value = statement.value
            if isinstance(value, ast.Call):
                producer = self.called_function(value, resolver, value)
                if producer is None or not self._tuple_payload(*producer, index, arity, scope, seen):
                    return False
            elif isinstance(value, ast.Tuple) and len(value.elts) == arity:
                payload = value.elts[index]
                payload_use = statement
                aliases = set()
                while isinstance(payload, ast.Name):
                    if payload.id in aliases or resolver.parameter(payload.id, payload_use) is not None:
                        return False
                    aliases.add(payload.id)
                    if _subject_rebound(function, payload.id):
                        return False
                    payload = _authority_binding_origin(payload.id, function, payload_use)
                    if payload is None:
                        return False
                    payload_use = payload
                # This callback is the fresh initialization seam. Only the
                # actual begin_run-bound creation recipe grants authority.
                # Generic expression() accepts required nominal parameters;
                # using it here without invocation substitution is unsound.
                if not _fresh_epoch_one_creation_authority(payload, function, resolver, scope):
                    return False
            else:
                return False
        return True

    def _unpacked_callback_authority(self, node, owner, resolver, use, scope, seen):
        if not isinstance(node, ast.Name) or _subject_rebound(owner, node.id):
            return False
        stores = [
            part
            for part in _walk_same_scope(owner)
            if isinstance(part, ast.Name) and part.id == node.id and isinstance(part.ctx, (ast.Store, ast.Del))
        ]
        if len(stores) != 1:
            return False
        target = getattr(stores[0], "_landscape_parent", None)
        statement = getattr(target, "_landscape_parent", None)
        if not isinstance(target, ast.Tuple) or not all(isinstance(item, ast.Name) for item in target.elts):
            return False
        if not isinstance(statement, ast.Assign) or statement.targets != [target] or not isinstance(statement.value, ast.Call):
            return False
        if not _callback_statement_dominates(statement, use, owner):
            return False
        invocation = statement.value
        if not isinstance(invocation.func, ast.Name) or any(kw.arg is None for kw in invocation.keywords):
            return False
        parameter = resolver.parameter(invocation.func.id, invocation)
        if parameter is None:
            return False
        suppliers = self._closed_callback_suppliers(owner, parameter, resolver)
        index = target.elts.index(stores[0])
        return bool(suppliers) and all(self._tuple_payload(*supplier, index, len(target.elts), scope, seen) for supplier in suppliers)

    def returned_authority(
        self,
        call: ast.Call,
        resolver: _Resolver,
        use: ast.AST,
        scope: str,
        seen: frozenset[int] = frozenset(),
        parameter_values: dict[tuple[int, str], tuple[ast.expr, _Resolver, ast.AST]] | None = None,
    ) -> bool:
        """Follow source return values; annotations alone never grant authority.

        Every explicit return must prove the requested scope. Bare/None returns,
        forged constructors, unknown calls, and cycles fail closed. A returned
        argument is substituted with this invocation's actual argument before
        authority proof, so an annotated identity function cannot bless a mint.
        """
        if any(kw.arg is None for kw in call.keywords):
            return False
        producer = self.called_function(call, resolver, use, parameter_values)
        if producer is None:
            return False
        function, producer_resolver = producer
        if id(function) in seen:
            return False
        seen = seen | {id(call), id(function)}
        substitutions = dict(parameter_values or {})
        positional = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args) if arg.arg not in {"self", "cls"}]
        for parameter in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs):
            arguments = [kw.value for kw in call.keywords if kw.arg == parameter.arg]
            if parameter.arg in positional and positional.index(parameter.arg) < len(call.args):
                arguments.append(call.args[positional.index(parameter.arg)])
            if len(arguments) > 1:
                return False
            if len(arguments) == 1:
                actual, actual_resolver, actual_use = arguments[0], resolver, call
                visited_parameters: set[tuple[int, str]] = set()
                visited_aliases: set[int] = set()
                while isinstance(actual, ast.Name):
                    actual_owner = _owner_function(actual_use)
                    key = (id(actual_owner), actual.id)
                    if key not in substitutions:
                        binding = actual_resolver.binding(actual.id, actual_use)
                        if binding is None:
                            break
                        if (
                            actual_owner is None
                            or id(binding) in visited_aliases
                            or _authority_binding_origin(actual.id, actual_owner, actual_use) is not binding
                        ):
                            return False
                        visited_aliases.add(id(binding))
                        actual, actual_use = binding, binding
                        continue
                    if key in visited_parameters or actual_owner is None or _parameter_rebound(actual_owner, actual.id):
                        return False
                    visited_parameters.add(key)
                    actual, actual_resolver, actual_use = substitutions[key]
                substitutions[(id(function), parameter.arg)] = actual, actual_resolver, actual_use
        if _authority_factory_return_contract(self, function, producer_resolver, scope):
            # Subject-admission recipes are independently checked by the
            # coordination caller sweep. They must not be conflated with value
            # provenance (the export arm has a distinct admission contract).
            return True
        # A corrupted known factory must never fall through as an ordinary relay.
        if producer_resolver.unit.path == "src/elspeth/core/landscape/run_coordination_repository.py":
            return False
        if not _authority_relay_block_terminates(function.body):
            return False
        returns = [part for part in _walk_same_scope(function) if isinstance(part, ast.Return)]
        if not returns:
            return False
        for statement in returns:
            value = statement.value
            if value is None:
                return False
            if isinstance(value, ast.Name):
                parameter = producer_resolver.parameter(value.id, statement)
                if parameter is not None:
                    if _parameter_rebound(function, parameter.arg):
                        return False
                    actual_value = substitutions.get((id(function), parameter.arg))
                    if actual_value is None:
                        return False
                    actual, actual_resolver, actual_use = actual_value
                    caller = _owner_function(actual_use)
                    if caller is None or not self.expression(actual, caller, actual_resolver, actual_use, scope, seen):
                        return False
                    continue
                value = _admission_unique_binding(value.id, function, statement)
                if value is None:
                    return False
            if not isinstance(value, ast.Call) or not self.returned_authority(value, producer_resolver, value, scope, seen, substitutions):
                return False
        return True

    def returned_field(self, field: ast.Attribute, resolver: _Resolver, use: ast.AST, scope: str, seen: frozenset[int]) -> bool:
        """Prove the actual constructor payload returned in a frozen carrier.

        The field annotation never grants authority. Every return must construct
        the same inspected frozen class with one explicit authority value; a
        producer parameter is substituted with this caller's actual argument.
        """
        invocation = resolver.resolve_value(field.value, use=use)
        if not isinstance(invocation, ast.Call) or id(invocation) in seen:
            return False
        producer = self.called_function(invocation, resolver, invocation)
        if producer is None:
            return False
        caller = _owner_function(use)
        if caller is None:
            return False
        for part in ast.walk(caller):
            if isinstance(part, ast.Attribute) and isinstance(part.ctx, (ast.Store, ast.Del)) and part.attr == field.attr:
                return False
            if isinstance(part, ast.Attribute) and part.attr == "__dict__":
                return False
            if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__", "vars"}:
                return False
        function, producer_resolver = producer
        returned = [part for part in _walk_same_scope(function) if isinstance(part, ast.Return)]
        if not returned:
            return False
        classes: set[int] = set()
        for statement in returned:
            value = statement.value
            if not isinstance(value, ast.Call) or any(keyword.arg is None for keyword in value.keywords):
                return False
            carrier = self.class_for(producer_resolver.qualified_name(value.func, use=value))
            if carrier is None or not self.field_origin(*carrier, field.attr, scope)[0]:
                return False
            if len(carrier[0].decorator_list) != 1 or carrier[0].bases or carrier[0].keywords:
                return False
            if any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name in {field.attr, "__getattribute__", "__getattr__", "__init__", "__new__", "__setattr__"}
                for member in carrier[0].body
            ):
                return False
            if any(
                (isinstance(part, ast.Attribute) and part.attr == "__dict__")
                or (isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__", "vars"})
                for part in ast.walk(carrier[0])
            ):
                return False
            if not any(
                isinstance(decorator, ast.Call)
                and carrier[1].qualified_name(decorator.func, use=carrier[0]) == "dataclasses.dataclass"
                and any(
                    keyword.arg == "frozen" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                    for keyword in decorator.keywords
                )
                for decorator in carrier[0].decorator_list
            ):
                return False
            classes.add(id(carrier[0]))
            payloads = [keyword.value for keyword in value.keywords if keyword.arg == field.attr]
            if len(payloads) != 1:
                return False
            payload = producer_resolver.resolve_value(payloads[0], use=value)
            parameter = producer_resolver.parameter(payload.id, payload) if isinstance(payload, ast.Name) else None
            if parameter is not None:
                arguments = [keyword.value for keyword in invocation.keywords if keyword.arg == parameter.arg]
                positional = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args) if arg.arg not in {"self", "cls"}]
                if parameter.arg in positional and positional.index(parameter.arg) < len(invocation.args):
                    arguments.append(invocation.args[positional.index(parameter.arg)])
                caller = _owner_function(use)
                if len(arguments) != 1 or caller is None or any(keyword.arg is None for keyword in invocation.keywords):
                    return False
                if not self.expression(arguments[0], caller, resolver, invocation, scope, seen | {id(invocation)}):
                    return False
            elif not self.expression(payload, function, producer_resolver, payload, scope, seen | {id(invocation)}):
                return False
        return len(classes) == 1

    def member(
        self, cls: ast.ClassDef, resolver: _Resolver, name: str, seen: frozenset[int] = frozenset()
    ) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.AnnAssign, ast.ClassDef, _Resolver] | None:
        if id(cls) in seen:
            return None
        matches = [
            child
            for child in cls.body
            if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name)
            or (isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.target.id == name)
        ]
        if matches:
            return (matches[0], cls, resolver) if len(matches) == 1 else None
        inherited = [
            found
            for base in cls.bases
            if (parent := self.class_for(resolver.qualified_name(base, use=cls))) is not None
            and (found := self.member(*parent, name, seen | {id(cls)})) is not None
        ]
        return inherited[0] if len(inherited) == 1 else None

    def receiver(self, expression, resolver, use, seen=frozenset(), parameter_values=None):
        if id(expression) in seen:
            return None
        seen = seen | {id(expression)}
        parameter_values = {} if parameter_values is None else parameter_values
        if isinstance(expression, ast.Name):
            owner = _owner_function(use)
            actual = parameter_values.get((id(owner), expression.id))
            if actual is not None:
                if owner is None or _parameter_rebound(owner, expression.id):
                    return None
                value, actual_resolver, actual_use = actual
                return self.receiver(value, actual_resolver, actual_use, seen, parameter_values)
            located = _method_receiver(use)
            if located is not None and expression.id == located[1]:
                return (located[0], resolver) if _receiver_class_body_is_visible(self, located[0], resolver) else None
            binding = resolver.binding(expression.id, use)
            if binding is not None:
                defining = _owner_function(binding)
                if defining is not None:
                    writes = [
                        part
                        for part in ast.walk(defining)
                        if isinstance(part, ast.Name) and part.id == expression.id and isinstance(part.ctx, (ast.Store, ast.Del))
                    ]
                    if len(writes) != 1 or not _receiver_binding_dominates(binding, use):
                        return None
                return self.receiver(binding, resolver, binding, seen, parameter_values)
            parameter = resolver.parameter(expression.id, use)
            if parameter is not None and parameter.annotation is not None:
                return _receiver_owned_class(self, resolver.qualified_name(parameter.annotation, use=use))
            return _receiver_owned_class(self, resolver.qualified_name(expression, use=use))
        if isinstance(expression, ast.Call):
            located = _method_receiver(use)
            owner = _owner_function(use)
            if located is None and owner is not None:
                parent = getattr(owner, "_landscape_parent", None)
                positional = (*owner.args.posonlyargs, *owner.args.args)
                if isinstance(parent, ast.ClassDef) and positional:
                    located = parent, positional[0].arg
            if (
                located is not None
                and owner is not None
                and isinstance(expression.func, ast.Name)
                and expression.func.id == located[1]
                and len(owner.decorator_list) == 1
                and isinstance(owner.decorator_list[0], ast.Name)
                and owner.decorator_list[0].id == "classmethod"
                and resolver.qualified_name(owner.decorator_list[0], use=owner) in {None, "classmethod", "builtins.classmethod"}
                and not _parameter_rebound(owner, located[1])
            ):
                return (located[0], resolver) if _receiver_class_body_is_visible(self, located[0], resolver) else None
            qualified = resolver.qualified_name(expression.func, use=expression)
            if (
                qualified is not None
                and "." in qualified
                and _trusted_qualified_name_is_mutated(qualified, resolver=resolver, use=expression)
            ):
                return None
            constructed = _receiver_owned_class(self, qualified)
            if constructed is not None:
                return constructed
            function = self.called_function(expression, resolver, expression, parameter_values)
            if function is None or id(function[0]) in seen:
                return None
            if not _authority_return_body_is_visible(*function):
                return None
            returns = [part for part in _walk_same_scope(function[0]) if isinstance(part, ast.Return)]
            if not returns or not _authority_relay_block_terminates(function[0].body):
                return None
            substitutions = _receiver_call_parameters(function[0], expression, resolver, use, parameter_values)
            if substitutions is None:
                return None
            # Required producer parameters must never fall back to their declared
            # class when this invocation omitted their actual value.
            parameter_names = {arg.arg for arg in (*function[0].args.posonlyargs, *function[0].args.args, *function[0].args.kwonlyargs)}
            if any(
                isinstance(part.value, ast.Name)
                and part.value.id in parameter_names
                and (id(function[0]), part.value.id) not in substitutions
                for part in returns
            ):
                return None
            resolved = [
                self.receiver(part.value, function[1], part, seen | {id(function[0])}, substitutions) if part.value is not None else None
                for part in returns
            ]
            if any(item is None for item in resolved):
                return None
            return resolved[0] if len({id(item[0]) for item in resolved}) == 1 else None
        if not isinstance(expression, ast.Attribute):
            return None
        parent = self.receiver(expression.value, resolver, use, seen, parameter_values)
        if parent is None:
            return None
        cls, class_resolver = parent
        constructor_values = _receiver_constructor_environment(self, expression.value, resolver, use, parameter_values)
        if constructor_values is not None:
            parameter_values = constructor_values
        # A concrete instance's source-visible field/property replacement makes
        # method lookup ambiguous; never use the declared class through it.
        owner = _owner_function(use)
        if owner is not None:
            for part in ast.walk(owner):
                if isinstance(part, ast.Attribute) and part.attr == "__dict__":
                    return None
                if (
                    isinstance(part, ast.Attribute)
                    and part.attr == expression.attr
                    and isinstance(part.ctx, (ast.Store, ast.Del))
                    and _owner_function(part).name != "__init__"
                ):
                    return None
                if isinstance(part, ast.Call) and _call_name(part) in {"setattr", "__setattr__", "vars"}:
                    return None
        member = self.member(cls, class_resolver, expression.attr)
        if member is not None and isinstance(member[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            method, _declaring_class, method_resolver = member
            if len(method.decorator_list) != 1:
                return None
            decorator = method.decorator_list[0]
            if not isinstance(decorator, ast.Name) or decorator.id != "property" or method_resolver.binding("property", method) is not None:
                return None
            if method_resolver.qualified_name(decorator, use=method) not in {None, "property", "builtins.property"}:
                return None
            returned = [part for part in _walk_same_scope(method) if isinstance(part, ast.Return)]
            located = _method_receiver(method)
            if len(returned) != 1 or located is None or not _authority_relay_block_terminates(method.body):
                return None
            value = returned[0].value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == located[1]:
                allow_none = any(_is_fail_closed_none_guard(statement, located[1], value.attr) for statement in method.body[:-1])
                return _receiver_field(self, cls, class_resolver, value.attr, use, seen | {id(method)}, allow_none, parameter_values)
            return self.receiver(value, method_resolver, returned[0], seen | {id(method)}, parameter_values) if value is not None else None
        return _receiver_field(self, cls, class_resolver, expression.attr, use, seen, parameter_values=parameter_values)

    @staticmethod
    def annotation(annotation: ast.expr | None, resolver: _Resolver, use: ast.AST, scope: str) -> tuple[bool, bool]:
        optional = isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr)
        if optional:
            arms = (annotation.left, annotation.right)
            values = [arm for arm in arms if not (isinstance(arm, ast.Constant) and arm.value is None)]
            if len(values) != 1:
                return False, False
            annotation = values[0]
        return _is_exact_scoped_authority_annotation(annotation, scope=scope, resolver=resolver, use=use), optional

    def field_origin(self, cls: ast.ClassDef, resolver: _Resolver, field: str, scope: str) -> tuple[bool, bool]:
        writes: list[tuple[ast.Assign | ast.AnnAssign, ast.expr]] = []
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and _call_name(node) in {"setattr", "__setattr__"}:
                return False, False
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == field:
                    located = _method_receiver(node)
                    if (
                        isinstance(node, ast.AugAssign)
                        or located is None
                        or located[0] is not cls
                        or not isinstance(target.value, ast.Name)
                        or target.value.id != located[1]
                        or _owner_function(node) is None
                        or node.value is None
                    ):
                        return False, False
                    writes.append((node, node.value))
        if not writes:
            member = self.member(cls, resolver, field)
            if member is None or not isinstance(member[0], ast.AnnAssign):
                return False, False
            frozen = any(
                isinstance(decorator, ast.Call)
                and resolver.qualified_name(decorator.func, use=cls) == "dataclasses.dataclass"
                and any(
                    keyword.arg == "frozen" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                    for keyword in decorator.keywords
                )
                for decorator in cls.decorator_list
            )
            return self.annotation(member[0].annotation, member[2], member[0], scope) if frozen else (False, False)
        results = [self.assignment_origin(assignment, value, resolver, scope) for assignment, value in writes]
        return all(result[0] for result in results), any(result[1] for result in results)

    def assignment_origin(
        self, assignment: ast.Assign | ast.AnnAssign, value: ast.expr, resolver: _Resolver, scope: str
    ) -> tuple[bool, bool]:
        if isinstance(value, ast.Constant) and value.value is None:
            # Clearing a carrier cannot create authority. The nominal getter
            # remains mandatory when any write can clear the field.
            return True, True
        init = _owner_function(assignment)
        if init is None or not isinstance(value, ast.Name):
            return False, False
        parameter = next((arg for arg in (*init.args.args, *init.args.kwonlyargs) if arg.arg == value.id), None)
        if parameter is None:
            return False, False
        default = _argument_default(init, parameter.arg)
        if default is not None and not (isinstance(default, ast.Constant) and default.value is None):
            return False, False
        valid, optional = self.annotation(parameter.annotation, resolver, init, scope)
        if not valid:
            return False, False
        stores = [
            node
            for node in _walk_same_scope(init)
            if isinstance(node, ast.Name) and node.id == value.id and isinstance(node.ctx, (ast.Store, ast.Del))
        ]
        rebindings: list[ast.Assign] = []
        for target in stores:
            parent = getattr(target, "_landscape_parent", None)
            if not isinstance(parent, ast.Assign) or parent.targets != [target]:
                return False, False
            rebindings.append(parent)
        for binding in rebindings:
            # A leader's membership projection preserves the admitted identity;
            # it does not construct a token or look up mutable repository state.
            if (
                scope not in {_MEMBER_SCOPE, _ITEM_SCOPE}
                or not isinstance(binding.value, ast.Attribute)
                or binding.value.attr != "membership"
            ):
                return False, False
            projected = binding.value.value
            if not isinstance(projected, ast.Name):
                return False, False
            leader = next((arg for arg in (*init.args.args, *init.args.kwonlyargs) if arg.arg == projected.id), None)
            if leader is None or not self.annotation(leader.annotation, resolver, init, _LEADER_SCOPE)[0]:
                return False, False
            leader_default = _argument_default(init, leader.arg)
            if (
                leader_default is not None and not (isinstance(leader_default, ast.Constant) and leader_default.value is None)
            ) or _parameter_rebound(init, leader.arg):
                return False, False
        return True, optional

    def getter(self, call: ast.Call, resolver: _Resolver, use: ast.AST, scope: str) -> bool:
        if not isinstance(call.func, ast.Attribute) or call.args or call.keywords:
            return False
        carrier = self.receiver(call.func.value, resolver, use)
        if carrier is None:
            return False
        found = self.member(*carrier, call.func.attr)
        if found is None or not isinstance(found[0], ast.FunctionDef):
            return False
        method, cls, method_resolver = found
        located = _method_receiver(method)
        if located is None:
            return False
        authority_fields: set[str] = set()
        if self.annotation(method.returns, method_resolver, method, scope) != (True, False):
            return False
        body = [
            stmt
            for stmt in method.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str))
        ]
        if len(body) == 3 and scope in {_MEMBER_SCOPE, _ITEM_SCOPE}:
            projection = body[0]
            if not isinstance(projection, ast.If) or projection.orelse or len(projection.body) != 1:
                return False
            check = projection.test
            projected = projection.body[0]
            if not (
                isinstance(check, ast.Call)
                and isinstance(check.func, ast.Name)
                and check.func.id == "isinstance"
                and len(check.args) == 2
                and not check.keywords
                and isinstance(check.args[0], ast.Attribute)
                and isinstance(check.args[0].value, ast.Name)
                and check.args[0].value.id == located[1]
                and self.annotation(check.args[1], method_resolver, method, _LEADER_SCOPE) == (True, False)
                and method_resolver.qualified_name(check.func, use=check) == "isinstance"
                and not method_resolver.is_local("isinstance", check)
                and isinstance(projected, ast.Return)
                and isinstance(projected.value, ast.Attribute)
                and projected.value.attr == "membership"
                and stable_ast_dump(projected.value.value) == stable_ast_dump(check.args[0])
                and self.field_origin(cls, method_resolver, check.args[0].attr, _LEADER_SCOPE)[0]
            ):
                return False
            authority_fields.add(check.args[0].attr)
            body = body[1:]
        if len(body) != 2 or not isinstance(body[0], ast.If) or not isinstance(body[1], ast.Return):
            return False
        guard, returned = body
        if not guard.body or not all(isinstance(stmt, ast.Raise) for stmt in guard.body) or guard.orelse:
            return False
        if not isinstance(returned.value, ast.Attribute) or not isinstance(returned.value.value, ast.Name):
            return False
        if returned.value.value.id != located[1]:
            return False
        authority_fields.add(returned.value.attr)
        test = guard.test
        if not (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Call)
            and isinstance(test.operand.func, ast.Name)
            and test.operand.func.id == "isinstance"
            and len(test.operand.args) == 2
            and not test.operand.keywords
            and stable_ast_dump(test.operand.args[0]) == stable_ast_dump(returned.value)
            and self.annotation(test.operand.args[1], method_resolver, method, scope) == (True, False)
            and not method_resolver.is_local("isinstance", test)
            and method_resolver.qualified_name(test.operand.func, use=test) == "isinstance"
        ):
            return False
        pending_classes = [carrier]
        seen_classes: set[int] = set()
        while pending_classes:
            current_class, current_resolver = pending_classes.pop()
            if id(current_class) in seen_classes:
                continue
            seen_classes.add(id(current_class))
            if any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name in authority_fields
                for member in current_class.body
            ):
                return False
            if current_class is not cls:
                for part in ast.walk(current_class):
                    if isinstance(part, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = part.targets if isinstance(part, ast.Assign) else [part.target]
                        if any(
                            (isinstance(target, ast.Attribute) and target.attr in authority_fields)
                            or (isinstance(target, ast.Name) and target.id in authority_fields)
                            for target in targets
                        ):
                            return False
            for base in current_class.bases:
                qualified = current_resolver.qualified_name(base, use=current_class)
                if qualified == "object":
                    continue
                parent = self.class_for(qualified)
                if parent is None:
                    return False
                pending_classes.append(parent)
        owner = _owner_function(use)
        if owner is not None:
            receiver = resolver.resolve_value(call.func.value, use=use)
            for node in _walk_same_scope(owner):
                if isinstance(node, ast.Attribute) and (
                    (isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in authority_fields) or node.attr == "__dict__"
                ):
                    mutated = resolver.resolve_value(node.value, use=node)
                    if stable_ast_dump(mutated) == stable_ast_dump(receiver):
                        return False
                if isinstance(node, ast.Call) and _call_name(node) in {"setattr", "__setattr__", "vars"} and node.args:
                    mutated = resolver.resolve_value(node.args[0], use=node)
                    if stable_ast_dump(mutated) == stable_ast_dump(receiver):
                        return False
        return self.field_origin(cls, method_resolver, returned.value.attr, scope)[0]

    def expression(
        self,
        node: ast.expr,
        owner: ast.FunctionDef | ast.AsyncFunctionDef,
        resolver: _Resolver,
        use: ast.AST,
        scope: str,
        seen: frozenset[int] = frozenset(),
    ) -> bool:
        if isinstance(node, ast.Name) and _authority_value_mutated(node.id, owner):
            return False
        if id(node) not in seen and self._unpacked_callback_authority(node, owner, resolver, use, scope, seen):
            return True
        if id(node) in seen:
            return False
        seen = seen | {id(node)}
        if _token_expression_is_explicit(node, owner, resolver=resolver, use=use, scope=scope):
            return True
        if isinstance(node, ast.Name):
            binding = resolver.binding(node.id, use)
            if binding is None:
                return False
            binding_owner = _owner_function(binding)
            if binding_owner is not None and binding_owner is not owner:
                # A closure captures the defining binding. Inspect that owner,
                # and reject writes through any nested closure before following
                # its origin; an inner annotation cannot certify a capture.
                stores = [
                    part
                    for part in ast.walk(binding_owner)
                    if isinstance(part, ast.Name) and part.id == node.id and isinstance(part.ctx, (ast.Store, ast.Del))
                ]
                if len(stores) != 1 or any(
                    isinstance(part, (ast.Nonlocal, ast.Global)) and node.id in part.names for part in ast.walk(binding_owner)
                ):
                    return False
                declaration: ast.AST = owner
                while _owner_function(getattr(declaration, "_landscape_parent", declaration)) is not binding_owner:
                    parent = getattr(declaration, "_landscape_parent", None)
                    if parent is None:
                        return False
                    declaration = parent
                if _authority_binding_origin(node.id, binding_owner, declaration) is not binding:
                    return False
                return self.expression(binding, binding_owner, resolver, binding, scope, seen)
            if _authority_binding_origin(node.id, owner, use) is not binding:
                return False
            return self.expression(binding, owner, resolver, binding, scope, seen)
        if isinstance(node, ast.Call):
            if scope == _WORK_ITEM_SCOPE and _claimed_work_item_return(self, node, resolver, use, seen - {id(node)}):
                return True
            if _fresh_epoch_one_creation_authority(node, owner, resolver, scope):
                return True
            return self.getter(node, resolver, use, scope) or self.returned_authority(node, resolver, use, scope, seen - {id(node)})
        if isinstance(node, ast.Attribute):
            if node.attr == "membership" and scope in {_MEMBER_SCOPE, _ITEM_SCOPE}:
                return self.expression(node.value, owner, resolver, use, _LEADER_SCOPE, seen)
            located = _method_receiver(use)
            if located is not None and isinstance(node.value, ast.Name) and node.value.id == located[1]:
                valid, optional = self.field_origin(located[0], resolver, node.attr, scope)
                if valid and not optional:
                    return True
            return self.returned_field(node, resolver, use, scope, seen)
        return False


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
    return isinstance(run_id, ast.Attribute) and run_id.attr == "run_id" and stable_ast_dump(run_id.value) == stable_ast_dump(token)


def _is_token_carrying_context_receiver(node: ast.AST, resolver: _Resolver, *, use: ast.AST) -> bool:
    """``ctx`` is a parameter of the enclosing function annotated with an ELSPETH-owned context type."""

    if not isinstance(node, ast.Name):
        return False
    parameter = resolver.parameter(node.id, use)
    if parameter is None or parameter.annotation is None:
        return False
    return resolver.qualified_name(parameter.annotation, use=use) in _TOKEN_CARRYING_CONTEXT_QUALIFIED


def _is_fail_closed_none_guard(statement: ast.stmt, receiver: str, attribute: str) -> bool:
    """``if self.<attr> is None: raise`` (possibly as one arm of an ``or``)."""

    if not isinstance(statement, ast.If) or not statement.body or not isinstance(statement.body[0], ast.Raise):
        return False

    def matches(test: ast.expr) -> bool:
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            return any(matches(value) for value in test.values)
        return (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.left, ast.Attribute)
            and test.left.attr == attribute
            and isinstance(test.left.value, ast.Name)
            and test.left.value.id == receiver
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        )

    return matches(statement.test)


def _context_attribute_token_is_carried_by_value(
    token: ast.expr,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    resolver: _Resolver,
    use: ast.Call,
) -> bool:
    """``self.<attr>`` bound ONLY from an exact ``__init__`` token parameter, never minted, and None-guarded."""

    if not isinstance(token, ast.Attribute) or not isinstance(token.value, ast.Name):
        return False
    located = _method_receiver(use)
    if located is None or token.value.id != located[1] or resolver.unit.path != _TOKEN_CARRYING_CONTEXT_OWNER[0]:
        return False
    owner_class = located[0]
    if owner_class.name != _TOKEN_CARRYING_CONTEXT_OWNER[1]:
        return False
    bindings = 0
    for member in ast.walk(owner_class):
        if isinstance(member, ast.Call) and _call_name(member) == "setattr":
            return False
        if not isinstance(member, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        targets = list(member.targets) if isinstance(member, ast.Assign) else [member.target]
        for target in targets:
            matches = [
                child
                for child in ast.walk(target)
                if isinstance(child, ast.Attribute) and child.attr == token.attr and isinstance(child.value, ast.Name)
            ]
            if not matches:
                continue
            binder = _method_receiver(member)
            if (
                not isinstance(member, ast.Assign)
                or target is not matches[0]
                or len(matches) != 1
                or binder is None
                or binder[0] is not owner_class
                or matches[0].value.id != binder[1]
            ):
                return False
            init = _owner_function(member)
            if init is None or init.name != "__init__" or not isinstance(member.value, ast.Name):
                return False
            parameter = next(
                (
                    argument
                    for argument in (*init.args.posonlyargs, *init.args.args, *init.args.kwonlyargs)
                    if argument.arg == member.value.id
                ),
                None,
            )
            if parameter is None or _parameter_rebound(init, parameter.arg):
                return False
            annotation = parameter.annotation
            if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
                # ``CoordinationToken | None``: the executor-less context carries nothing.
                arms = [annotation.left, annotation.right]
                annotation = next((arm for arm in arms if not (isinstance(arm, ast.Constant) and arm.value is None)), None)
                if len(arms) != 2 or not any(isinstance(arm, ast.Constant) and arm.value is None for arm in arms):
                    return False
            if not _is_exact_coordination_token_annotation(annotation, resolver=resolver, use=init):
                return False
            bindings += 1
    if bindings == 0:
        return False
    # Fail-closed: the forwarding call must sit below an ``if self.<attr> is None: raise`` in its own function.
    return any(statement.lineno < use.lineno and _is_fail_closed_none_guard(statement, located[1], token.attr) for statement in owner.body)


def _caller_authority_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    definitions = _find_api_definitions(unit_list)
    proof = _AuthorityProof(unit_list)
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
            if _resolved_non_landscape_receiver_owner(call.func.value, call.func.attr, resolver, use=call) is not None:
                continue
            if _proven_mutation_forwarder(call.func.value, call.func.attr, resolver, call, proof):
                continue
            candidates = [api for api in _MUTATION_APIS if api.method == call.func.attr]
            receiver = proof.receiver(call.func.value, resolver, call)
            if receiver is not None:
                owned_candidates = [api for api in candidates if api.path == receiver[1].unit.path and api.owner == receiver[0].name]
                if owned_candidates:
                    candidates = owned_candidates
            scopes = {_verb_authority_scope(api.path, api.method) for api in candidates}
            # Receiver collisions cannot silently select the less restrictive
            # authority. Every resolved API candidate must agree.
            if len(scopes) != 1:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} ambiguous API authority scope")
                continue
            scope = next(iter(scopes))
            owner = _owner_function(call)
            if owner is None:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} call has no lexical owner")
                continue
            if any(keyword.arg is None for keyword in call.keywords):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} forwards authority through **kwargs")
                continue
            if call.func.attr in _PLUGIN_CONTEXT_METHODS and _is_token_carrying_context_receiver(call.func.value, resolver, use=call):
                # A plugin never holds a token (ADR-048 §3). It forwards through
                # its context, and the context's own forwarding call — scanned
                # below on ``self.landscape`` — is where authority is proven.
                continue
            token_keywords_by_value = [keyword.value for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
            if (
                scope == _LEADER_SCOPE
                and len(token_keywords_by_value) == 1
                and _context_attribute_token_is_carried_by_value(token_keywords_by_value[0], owner, resolver=resolver, use=call)
            ):
                continue
            capability_token = _proven_token_bound_capability_token(call.func.value, resolver=resolver, use=call)
            if capability_token is not None and scope == _LEADER_SCOPE:
                if any(keyword.arg in _AUTHORITY_PARAMETER_NAMES for keyword in call.keywords):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} overrides token-bound capability authority")
                    continue
                run_id = _run_id_argument(call, candidates, definitions)
                requires_run_id = any(_api_run_id_position(api, definitions) is not None for api in candidates)
                if requires_run_id and run_id is not None and not _is_exact_token_run_id(run_id, capability_token):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} run_id is not capability token.run_id")
                continue
            token_keywords = [keyword for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
            if len(token_keywords) != 1 or not proof.expression(token_keywords[0].value, owner, resolver, call, scope):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} lacks one exact current token")
                continue
            if scope == _ITEM_SCOPE:
                claims = [keyword.value for keyword in call.keywords if keyword.arg == "work_item"]
                if len(claims) != 1 or not proof.expression(claims[0], owner, resolver, call, _WORK_ITEM_SCOPE):
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} lacks one exact claimed work item")
                    continue
            run_id = _run_id_argument(call, candidates, definitions)
            requires_run_id = any(_api_run_id_position(api, definitions) is not None for api in candidates)
            if requires_run_id and (run_id is None or not _is_exact_token_run_id(run_id, token_keywords[0].value)):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} run_id is not exact token.run_id")
    return tuple(violations)


_EXACT_ESTABLISHMENT_CALLERS = {
    "acquire_run_leadership": frozenset(
        {
            ("src/elspeth/engine/orchestrator/resume.py", "ResumeCoordinator._acquire_resume_leadership"),
            ("src/elspeth/web/app.py", "_acquire_orphaned_run_leadership"),
        }
    ),
    "admit_follower": frozenset(
        {
            ("src/elspeth/engine/orchestrator/join_admission.py", "JoinAdmissionService.join_run"),
        }
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


def _establishment_origin(
    value: ast.expr,
    *,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
) -> ast.expr | None:
    """Resolve a local only through one direct, dominating assignment."""
    if not isinstance(value, ast.Name):
        return value
    parameters = {argument.arg for argument in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)}
    if value.id in parameters:
        return None if _subject_rebound(owner, value.id) else value
    writes = [
        node
        for node in _walk_same_scope(owner)
        if isinstance(node, ast.Name) and node.id == value.id and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    if not writes:
        # The caller below must still resolve this as an owned import. It is
        # not sufficient for a bare unbound name to look like a constant.
        return value
    if len(writes) != 1 or _subject_rebound(owner, value.id):
        return None
    assignment = next(
        (
            statement
            for statement in owner.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and _is_descendant(writes[0], statement)
        ),
        None,
    )
    if assignment is None or assignment.value is None:
        return None
    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name) or targets[0].id != value.id:
        return None
    call_statement = next((statement for statement in owner.body if _is_descendant(call, statement)), None)
    if call_statement is None or owner.body.index(assignment) >= owner.body.index(call_statement):
        return None
    # One local recipe is enough for the current production shapes. Following
    # an arbitrary alias graph would need its own dominance/mutation proof.
    return assignment.value


def _establishment_owned_unary_call(expression: ast.expr, qualified: str, argument: ast.expr, resolver: _Resolver) -> bool:
    return (
        isinstance(expression, ast.Call)
        and resolver.qualified_name(expression.func, use=expression) == qualified
        and len(expression.args) == 1
        and not expression.keywords
        and stable_ast_dump(expression.args[0]) == stable_ast_dump(argument)
    )


def _establishment_window_is_proven(
    expression: ast.expr,
    *,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    resolver: _Resolver,
    call: ast.Call,
) -> bool:
    constant = "elspeth.contracts.coordination.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS"
    value = _establishment_origin(expression, owner=owner, call=call)
    if value is None:
        return False
    if resolver.qualified_name(value, use=value) == constant:
        return True
    parameter = next(
        (argument for argument in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs) if argument.arg == "window_seconds"),
        None,
    )
    if parameter is None or _subject_rebound(owner, "window_seconds"):
        return False
    if isinstance(value, ast.Name) and value.id == "window_seconds":
        # Preserve historical direct required-window synthetic tests. Never
        # certify forwarding an optional default None without its fallback.
        return _argument_default(owner, "window_seconds") is None
    if not isinstance(value, ast.IfExp):
        return False
    expected_test = ast.parse("window_seconds is not None", mode="eval").body
    return (
        stable_ast_dump(value.test) == stable_ast_dump(expected_test)
        and isinstance(value.body, ast.Name)
        and value.body.id == "window_seconds"
        and resolver.qualified_name(value.orelse, use=value.orelse) == constant
    )


def _establishment_call_shape_violation(method: str, call: ast.Call) -> str | None:
    arguments = _exact_keyword_arguments(call)
    required = {
        "acquire_run_leadership": {"run_id", "worker_id", "window_seconds", "entry_point"},
        "admit_follower": {"run_id", "worker_id", "config_hash", "window_seconds"},
    }[method]
    if arguments is None or not required <= arguments.keys() or set(arguments) - required - {"now"}:
        return f"{method} must use one explicit complete keyword-bound authority subject"
    # Existing clock-compatibility unit tests construct a new ast.Call around
    # a parsed callee. Use that callee's attached scope for the same proof.
    owner = _owner_function(call) or _owner_function(call.func)
    if owner is None:
        return "authority establishment has no inspected owner"
    resolver = _resolver_for_node(owner)
    parameters = {argument.arg for argument in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)}
    if not _establishment_window_is_proven(arguments["window_seconds"], owner=owner, resolver=resolver, call=call):
        return "authority-establishment window is not a required parameter or exact owned default/fallback"
    if (
        method == "acquire_run_leadership"
        and isinstance(arguments["entry_point"], ast.Constant)
        and arguments["entry_point"].value == "resume"
    ):
        if not (
            "snapshot" in parameters
            and _dotted_name(arguments["run_id"]) == "snapshot.run_id"
            and _dotted_name(arguments["worker_id"]) == "snapshot.worker_id"
            and not _subject_rebound(owner, "snapshot")
        ):
            return "acquire_run_leadership CAS subject is not one exact resume snapshot"
        return None
    if not (
        "run_id" in parameters
        and isinstance(arguments["run_id"], ast.Name)
        and arguments["run_id"].id == "run_id"
        and not _subject_rebound(owner, "run_id")
    ):
        return "authority-establishment run is not the unchanged exact run parameter"
    worker = _establishment_origin(arguments["worker_id"], owner=owner, call=call)
    if worker is None or not _establishment_owned_unary_call(
        worker, "elspeth.contracts.coordination.mint_worker_id", arguments["run_id"], resolver
    ):
        return "authority-establishment worker is not owned mint_worker_id for the exact run"
    if method == "acquire_run_leadership":
        if not (
            (resolver.unit.path, _symbol(owner)) == ("src/elspeth/web/app.py", "_acquire_orphaned_run_leadership")
            and isinstance(arguments["entry_point"], ast.Constant)
            and arguments["entry_point"].value == "orphan-finalize"
        ):
            return "orphan leadership acquisition is not the exact admitted helper"
        return None
    config_hash = _establishment_origin(arguments["config_hash"], owner=owner, call=call)
    if not (
        "settings" in parameters
        and not _subject_rebound(owner, "settings")
        and isinstance(config_hash, ast.Call)
        and resolver.qualified_name(config_hash.func, use=config_hash) == "elspeth.core.canonical.stable_hash"
        and len(config_hash.args) == 1
        and not config_hash.keywords
        and _establishment_owned_unary_call(
            config_hash.args[0], "elspeth.core.config.resolve_config", ast.Name(id="settings", ctx=ast.Load()), resolver
        )
    ):
        return "follower config hash is not owned stable_hash(resolve_config(settings))"
    return None


def _heartbeat_degraded_evidence_call_violation(unit: SourceUnit, call: ast.Call, proof: _AuthorityProof) -> str | None:
    """An exact forensic edge; this proves identity, not current DB liveness."""
    if (unit.path, _symbol(call)) != ("src/elspeth/engine/orchestrator/heartbeat.py", "RunHeartbeatThread._emit_degraded"):
        return "unexpected heartbeat_degraded evidence caller"
    owner = _owner_function(call)
    arguments = _exact_keyword_arguments(call)
    if owner is None or arguments is None or set(arguments) != {"member_token", "failures", "now"}:
        return "heartbeat_degraded requires exact token, failure count, and forensic time arguments"
    resolver = _resolver_for_unit(unit)
    if _dotted_name(arguments["member_token"]) != "self._token" or not proof.expression(
        arguments["member_token"], owner, resolver, call, _MEMBER_SCOPE
    ):
        return "heartbeat_degraded identity is not the thread's proven membership value"
    if _dotted_name(arguments["failures"]) != "self._consecutive_busy":
        return "heartbeat_degraded failure count is not the thread's actual counter"
    timestamp = arguments["now"]
    if not isinstance(timestamp, ast.Call) or _dotted_name(timestamp.func) != "self._now_fn" or timestamp.args or timestamp.keywords:
        return "heartbeat_degraded time is not the thread's forensic clock"
    if _has_repeating_ancestor(call, stop=owner) or _is_statically_dead(call, stop=owner):
        return "heartbeat_degraded evidence call is repeating or unreachable"
    return None


def _exact_token_subject(node: ast.expr, token: ast.expr, field: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == field and stable_ast_dump(node.value) == stable_ast_dump(token)


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
    proof = _AuthorityProof(unit_list)
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
            if call.func.attr == "record_heartbeat_degraded":
                evidence_violation = _heartbeat_degraded_evidence_call_violation(unit, call, proof)
                if evidence_violation is not None:
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} {evidence_violation}")
                continue
            if expected_establishment is not None:
                establishment_calls[call.func.attr].append((unit, call))
                if identity not in expected_establishment:
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
            scope = _verb_authority_scope(_RUN_COORDINATION_PATH, call.func.attr)
            capability_token = _proven_token_bound_capability_token(call.func.value, resolver=resolver, use=call)
            if (
                scope == _LEADER_SCOPE
                and capability_token is not None
                and not any(keyword.arg in _AUTHORITY_PARAMETER_NAMES for keyword in call.keywords)
            ):
                subject_violation = _coordination_subject_violation(call, capability_token, coordination_definitions)
                if subject_violation is not None:
                    violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} {subject_violation}")
                continue
            token_keywords = [keyword for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
            if len(token_keywords) != 1 or not proof.expression(token_keywords[0].value, owner, resolver, call, scope):
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} .{call.func.attr} lacks one exact current authority")
                continue
            subject_violation = _coordination_subject_violation(call, token_keywords[0].value, coordination_definitions)
            if subject_violation is not None:
                violations.append(f"{unit.path}:{call.lineno} {_symbol(call)} {subject_violation}")
    for method, calls in establishment_calls.items():
        counts = Counter((unit.path, _symbol(call)) for unit, call in calls)
        for identity in _EXACT_ESTABLISHMENT_CALLERS[method]:
            if counts[identity] != 1:
                violations.append(f"{method} {identity} approved authority-establishment calls={counts[identity]} expected=1")
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


def _is_exact_scoped_authority_annotation(
    annotation: ast.expr | None,
    *,
    scope: str,
    resolver: _Resolver | None = None,
    use: ast.AST | None = None,
) -> bool:
    """Exactly ONE concrete authority type, chosen by the verb's scope class.

    Deliberately NOT a widening of the leader predicate.  Admitting either type
    everywhere would let a leader verb accept a follower's token -- one type
    with two meanings, ADR-048's rejected option (B), unprovable by
    construction.  The scope is decided by the verb, and the annotation must
    match that scope's single type: no union, no ``| None``, no string
    annotation.
    """
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return False
    dotted = resolver.qualified_name(annotation, use=use or annotation) if resolver is not None else _dotted_name(annotation)
    return dotted == _AUTHORITY_QUALIFIED_BY_SCOPE[scope]


def _is_exact_coordination_token_annotation(
    annotation: ast.expr | None,
    *,
    resolver: _Resolver | None = None,
    use: ast.AST | None = None,
) -> bool:
    """The LEADER-scoped predicate.  Unchanged in meaning; every existing caller keeps it."""
    return _is_exact_scoped_authority_annotation(annotation, scope=_LEADER_SCOPE, resolver=resolver, use=use)


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
        scope = _verb_authority_scope(api.path, api.method)
        if not _is_exact_scoped_authority_annotation(
            parameter.annotation,
            scope=scope,
            resolver=resolvers.get(api.path),
            use=node,
        ):
            violations.append(
                f"{api.path}:{api.symbol} token annotation is not {_AUTHORITY_QUALIFIED_BY_SCOPE[scope].rsplit('.', maxsplit=1)[-1]}"
            )
        if parameter not in node.args.kwonlyargs:
            violations.append(f"{api.path}:{api.symbol} token is not keyword-only")
        if _argument_default(node, parameter.arg) is not None:
            violations.append(f"{api.path}:{api.symbol} token is optional/defaulted")
        if _parameter_rebound(node, parameter.arg):
            violations.append(f"{api.path}:{api.symbol} token parameter is rebound")
        if scope == _ITEM_SCOPE and not _has_exact_work_item_parameter(node, resolvers[api.path]):
            violations.append(f"{api.path}:{api.symbol} requires exact non-defaulted keyword-only TokenWorkItem")
    return tuple(violations)


def _has_exact_work_item_parameter(node: ast.FunctionDef | ast.AsyncFunctionDef, resolver: _Resolver) -> bool:
    matches = [argument for argument in node.args.kwonlyargs if argument.arg == "work_item"]
    return (
        len(matches) == 1
        and matches[0].annotation is not None
        and resolver.qualified_name(matches[0].annotation, use=node) == "elspeth.contracts.scheduler.TokenWorkItem"
        and _argument_default(node, "work_item") is None
        and not _parameter_rebound(node, "work_item")
    )


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


def _proven_mutation_forwarder(receiver: ast.expr, method_name: str, resolver: _Resolver, use: ast.AST, proof: _AuthorityProof) -> bool:
    """A wrapper is admitted only after proving every actual inner audit call.

    This is a source proof, not a method-name or class allowlist. Empty stubs,
    forged authority, unknown receivers, overrides and untyped carriers remain
    unproven. The inner facade calls also remain in the production inventory.
    """
    carrier = proof.receiver(receiver, resolver, use)
    if carrier is None or carrier[1].unit.path.startswith("src/elspeth/core/landscape/"):
        return False
    found = proof.member(*carrier, method_name)
    if found is None or not isinstance(found[0], ast.FunctionDef):
        return False
    method, _cls, method_resolver = found
    calls = [
        node
        for node in _walk_same_scope(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _ALL_MUTATION_METHOD_NAMES
    ]
    if not calls:
        return False
    for call in calls:
        if not _looks_like_landscape_receiver(call.func.value, call.func.attr, resolver=method_resolver, use=call):
            return False
        candidates = [api for api in _MUTATION_APIS if api.method == call.func.attr]
        scopes = {_verb_authority_scope(api.path, api.method) for api in candidates}
        if len(scopes) != 1:
            return False
        scope = next(iter(scopes))
        values = [keyword.value for keyword in call.keywords if keyword.arg in _AUTHORITY_PARAMETER_NAMES]
        if any(keyword.arg is None for keyword in call.keywords) or len(values) != 1:
            return False
        if not proof.expression(values[0], method, method_resolver, call, scope):
            return False
        if scope == _ITEM_SCOPE:
            claims = [keyword.value for keyword in call.keywords if keyword.arg == "work_item"]
            if len(claims) != 1 or not proof.expression(claims[0], method, method_resolver, call, _WORK_ITEM_SCOPE):
                return False
    return True


def _mutation_callable_escapes(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    units = tuple(units)
    proof = _AuthorityProof(units)
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
                # A receiver whose RESOLVED OWNER is a pinned non-Landscape type is a
                # method-name collision, not an unknown receiver.  Keyed on the owner,
                # never the name; an unresolvable receiver falls through and rows.
                if _resolved_non_landscape_receiver_owner(node.value, node.attr, resolver, use=node) is not None:
                    continue
                if _proven_mutation_forwarder(node.value, node.attr, resolver, node, proof):
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


# SQLAlchemy exposes the dialect-specific ``INSERT ... ON CONFLICT`` builder
# under the same terminal name in every dialect package, so a module that needs
# both must alias at least one of them: the alias is forced by the library, not
# a way of hiding a DML constructor.  Admission is decided on the import's
# RESOLVED ORIGIN — the dialect module the binding comes from — and the alias
# must be that origin's dialect-qualified spelling, so a same-spelled alias over
# any other origin stays an escape.
_DIALECT_DML_ORIGIN = re.compile(r"^sqlalchemy\.dialects\.(?P<dialect>[a-z_][a-z0-9_]*)\.(?P<operation>insert|update|delete)$")


def _is_canonical_dialect_dml_binding(origin: str, asname: str) -> bool:
    match = _DIALECT_DML_ORIGIN.match(origin)
    return match is not None and asname == f"{match.group('dialect')}_{match.group('operation')}"


def _dml_callable_escape_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    violations: list[str] = []
    for unit in units:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.ImportFrom):
                module = resolver._absolute_import_module(node)
                for alias in node.names:
                    if alias.asname is None or not (alias.name in {"insert", "update", "delete"} or alias.name.endswith("_insert")):
                        continue
                    origin = f"{module}.{alias.name}" if module else alias.name
                    if _is_canonical_dialect_dml_binding(origin, alias.asname):
                        continue
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
    seen: frozenset[int] = frozenset(),
) -> bool:
    if isinstance(statement, ast.Name):
        binding = resolver.binding(statement.id, use)
        if binding is None:
            return False
        # Keyed on the BINDING SITE, not the name: ``query = query.where(...)``
        # refines one name from its own earlier value and is not a cycle.  A
        # genuine revisit of the same binding still answers True (fail closed).
        if id(binding) in seen:
            return True
        return _statement_contains_dml(binding, resolver, use=binding, seen=seen | {id(binding)})
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
    qualified = resolver.qualified_name(resolver.resolve_callable(resolved.func, use=resolved), use=resolved)
    if name in {"select", "exists"}:
        return qualified in {
            f"{module}.{name}" for module in ("sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression", "sqlalchemy.sql.selectable")
        }
    if name == "text" and resolved.args:
        if qualified not in {"sqlalchemy.text", "sqlalchemy.sql.text", "sqlalchemy.sql.expression.text"}:
            return False
        value = resolved.args[0]
        return isinstance(value, ast.Constant) and isinstance(value.value, str) and _raw_sql_is_proven_read(value.value)
    # A compound select is admitted by RESOLVED ORIGIN: SQLAlchemy's
    # ``union``/``intersect``/``except_`` only compose selectables, so the
    # construction cannot carry a write, but a project callable of the same
    # spelling proves nothing.
    return qualified in _SQLALCHEMY_COMPOUND_READ_ORIGINS


_SQLALCHEMY_COMPOUND_READ_ORIGINS = frozenset(
    {
        f"{module}.{operation}"
        for module in ("sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression")
        for operation in ("union", "union_all", "intersect", "intersect_all", "except_", "except_all")
    }
)


def _helper_return_class(statement: ast.Call, resolver: _Resolver, unit: SourceUnit, *, use: ast.AST) -> str | None:
    """Classify a statement built by a helper defined in the same unit.

    Returns ``"dml"`` when every ``return`` of the helper is a SQLAlchemy DML
    construction, ``"read"`` when every one is a proven read, and ``None``
    otherwise — a helper mixing the two, returning ``None``, or defined in
    another module is not classified on the strength of its name.
    """

    function = _same_unit_helper(statement.func, resolver, unit, use=use)
    if function is None:
        return None
    classes: set[str | None] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return):
            continue
        if _lexical_scope(node) is not function:
            continue
        value = node.value
        if value is None:
            return None
        resolved = resolver.resolve_statement(value, use=node)
        if isinstance(resolved, ast.Call) and (
            _dml_shape(resolved, resolver) is not None or _dml_operation_without_table(resolved, resolver, use=node) is not None
        ):
            classes.add("dml")
        elif _is_proven_read(value, resolver, use=node):
            classes.add("read")
        else:
            return None
    return next(iter(classes)) if len(classes) == 1 else None


def _same_unit_helper(
    func: ast.expr, resolver: _Resolver, unit: SourceUnit, *, use: ast.AST
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    index = _function_index_for_units((unit,))
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
        owner = next((node for node in _ancestors(use) if isinstance(node, ast.ClassDef)), None)
        if owner is None:
            return None
        return index.get((unit.path, f"{_symbol(owner)}.{func.attr}"))
    if isinstance(func, ast.Name) and resolver.parameter(func.id, use) is None and not resolver.is_local(func.id, use):
        qualified = resolver.qualified_name(func, use=use)
        if qualified is None or _module_path_from_qualified(qualified) != unit.path:
            return None
        return index.get((unit.path, func.id))
    return None


_SQLALCHEMY_CORE_DML_ORIGINS = frozenset(
    {
        f"{module}.{operation}"
        for module in ("sqlalchemy", "sqlalchemy.sql", "sqlalchemy.sql.expression")
        for operation in ("insert", "update", "delete")
    }
)


def _dml_operation_without_table(statement: ast.Call, resolver: _Resolver, *, use: ast.AST) -> str | None:
    """Return the verb of a DML construction whose table cannot be named.

    Admission is by RESOLVED ORIGIN — a SQLAlchemy core or dialect DML
    constructor — so a same-spelled project callable is never relabelled as
    DML on the strength of its spelling.
    """

    callable_node = resolver.resolve_callable(statement.func, use=statement)
    qualified = resolver.qualified_name(callable_node, use=use)
    if qualified is None:
        return None
    if qualified in _SQLALCHEMY_CORE_DML_ORIGINS:
        return qualified.rsplit(".", maxsplit=1)[-1]
    match = _DIALECT_DML_ORIGIN.match(qualified)
    return None if match is None else match.group("operation")


def _admitted_non_run_raw_write(node: ast.Call, resolver: _Resolver) -> bool:
    """ADR-048 A5's exact schema stamp and temporary outbox acknowledgement."""
    key = (resolver.unit.path, _symbol(node))
    if key == ("src/elspeth/core/landscape/database.py", "LandscapeDB._set_sqlite_schema_epoch"):
        expected = ast.parse('conn.exec_driver_sql(f"PRAGMA user_version = {int(epoch)}")').body[0].value
        owner = _owner_function(node)
        if stable_ast_dump(node) != stable_ast_dump(expected) or owner is None or _parameter_rebound(owner, "epoch"):
            return False
        return any(
            isinstance(parent, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and resolver.qualified_name(item.context_expr.func, use=item.context_expr) == _BEGIN_WRITE_QUALIFIED
                and isinstance(item.optional_vars, ast.Name)
                and item.optional_vars.id == "conn"
                for item in parent.items
            )
            for parent in _ancestors(node)
        )
    if key == ("src/elspeth/core/landscape/journal.py", "LandscapeJournal._drain_committed_outbox"):
        expected = (
            ast.parse(
                'cursor.execute(f"DELETE FROM sidecar_journal_outbox WHERE sequence = {placeholder} AND journal_owner = {placeholder}", (sequence, self._owner_key))'
            )
            .body[0]
            .value
        )
        if stable_ast_dump(node) != stable_ast_dump(expected):
            return False
        owner = _owner_function(node)
        if owner is None:
            return False
        stores = [
            child
            for child in _walk_same_scope(owner)
            if isinstance(child, ast.Name) and child.id == "placeholder" and isinstance(child.ctx, (ast.Store, ast.Del))
        ]
        values: set[str] = set()
        for store in stores:
            assignment = getattr(store, "_landscape_parent", None)
            if not isinstance(assignment, ast.Assign) or assignment.targets != [store] or not isinstance(assignment.value, ast.Constant):
                return False
            if assignment.value.value not in {"?", "%s"}:
                return False
            values.add(assignment.value.value)
        return values == {"?", "%s"} and len(stores) == 2
    return False


def _read_only_statement_relay(node: ast.Call, resolver: _Resolver) -> bool:
    """Admit only the three physical read-only DatabaseOps entry points."""
    if resolver.unit.path != "src/elspeth/core/landscape/_database_ops.py" or _symbol(node) not in {
        "ReadOnlyDatabaseOps.execute_fetchone",
        "ReadOnlyDatabaseOps.execute_fetchall",
        "ReadOnlyDatabaseOps.execute_fetchall_many",
    }:
        return False
    owner = _owner_function(node)
    if owner is None:
        return False
    contexts = [
        (child, item)
        for child in _walk_same_scope(owner)
        if isinstance(child, ast.With)
        for item in child.items
        if isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "read_only_connection"
        and _dotted_name(item.context_expr.func.value) == "self._db"
        and not item.context_expr.args
        and not item.context_expr.keywords
    ]
    if len(contexts) != 1 or not isinstance(contexts[0][1].optional_vars, ast.Name):
        return False
    connection = contexts[0][1].optional_vars.id
    stores = [
        child
        for child in _walk_same_scope(owner)
        if isinstance(child, ast.Name) and child.id == connection and isinstance(child.ctx, (ast.Store, ast.Del))
    ]
    if stores != [contexts[0][1].optional_vars]:
        return False
    return _is_descendant(node, contexts[0][0]) and _payload_uses_exact_connection(node, connection)


def _unknown_or_raw_execution_violations(units: Iterable[SourceUnit]) -> tuple[str, ...]:
    unit_list = tuple(units)
    dml = scan_dml_identities(unit_list)
    read_helpers = _proven_read_statement_helpers(unit_list)
    helper_keys = _subordinate_helper_keys(_function_index(unit_list), dml, _owned_reexports(unit_list), read_helpers)
    helper_violations = _transaction_order_violations(unit_list, dml)
    proven_helpers = {key for key in helper_keys if not any(row.startswith(f"{key[0]}:{key[1]} ") for row in helper_violations)}
    violations: list[str] = []
    for unit in unit_list:
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            continue
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Call):
                continue
            if _admitted_non_run_raw_write(node, resolver):
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
            statement = node.args[0]
            # A ``text()`` payload with a raw DML shape was already reported
            # at the ``text()`` node by the ``_raw_dml_shape`` branch above;
            # reporting it again here would count one statement twice.
            if isinstance(statement, ast.Call) and _raw_dml_shape(statement, resolver) is not None:
                continue
            # A literal handed straight to a DBAPI cursor or driver never
            # reaches ``_raw_dml_shape`` (that only reads ``text()`` and
            # ``exec_driver_sql()`` callables), so classify the SQL here.  The
            # WRITE decision uses the constant skeleton — dropping an
            # interpolation cannot invent a write keyword — while every ACCEPT
            # decision demands the exact text.
            skeleton = _raw_sql_constant_skeleton(statement, resolver, use=node)
            exact = _raw_sql_exact_texts(statement, resolver, use=node)
            # A DBAPI ``execute`` runs ONE statement, so a configuration PRAGMA
            # whose text carries no ``;`` cannot also carry a row write.
            configuration = skeleton is not None and _raw_sql_is_single_configuration_statement(skeleton)
            # The WRITE decision reads every exact candidate, else the skeleton;
            # among several write candidates the most specific label wins.
            written = sorted(
                {
                    _raw_sql_write_detail(text)
                    for text in (exact or ())
                    if _raw_sql_is_write(text) and not _raw_sql_is_single_configuration_statement(text)
                },
                key=lambda detail: (detail == "write/DDL", detail),
            )
            if not written and exact is None and skeleton is not None and _raw_sql_is_write(skeleton) and not configuration:
                written = [_raw_sql_write_detail(skeleton)]
            if written:
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} raw SQL {written[0]} is forbidden")
                continue
            if configuration:
                continue
            if exact is not None and all(
                _raw_sql_is_proven_read(text) or _raw_sql_is_transaction_control(text) or _raw_sql_is_single_configuration_statement(text)
                for text in exact
            ):
                continue
            if name == "exec_driver_sql":
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} unknown exec_driver_sql effect")
                continue
            resolved = resolver.resolve_statement(statement, use=node)
            if isinstance(resolved, ast.Call) and _dml_shape(resolved, resolver) is not None:
                continue
            if _is_proven_read(statement, resolver, use=node):
                continue
            # A statement built by a same-unit helper is classified by the
            # helper's ``return`` expressions: its DML constructions are
            # inventoried where they are written and its call edge is a
            # subordinate edge of the fencing gate, so the execute site is
            # admitted exactly as a direct construction would be.
            if isinstance(resolved, ast.Call) and _helper_return_class(resolved, resolver, unit, use=node) is not None:
                continue
            # Reclassify, never suppress: a site whose class IS provable is
            # reported by its class so the residue reads as a real fencing
            # obligation rather than scanner noise.
            operation = None if not isinstance(resolved, ast.Call) else _dml_operation_without_table(resolved, resolver, use=node)
            if operation is not None:
                table_expression = _dotted_name(resolved.args[0]) if isinstance(resolved, ast.Call) and resolved.args else None
                subject = "an unresolved table" if table_expression is None else f"caller-supplied table {table_expression!r}"
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} DML {operation} on {subject}")
                continue
            parameter = _relayed_statement_parameter(statement, resolver, use=node)
            if parameter is not None:
                if (unit.path, _symbol(node)) in proven_helpers | read_helpers or _read_only_statement_relay(node, resolver):
                    continue
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} relays caller-supplied statement parameter {parameter!r}")
                continue
            violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} unclassified .{name} statement")
    return tuple(violations)


def _relayed_statement_parameter(statement: ast.expr, resolver: _Resolver, *, use: ast.AST) -> str | None:
    """Name the parameter a relayed statement arrives through, directly or as a loop element."""

    if not isinstance(statement, ast.Name):
        return None
    parameter = resolver.parameter(statement.id, use)
    if parameter is not None:
        return parameter.arg
    source = resolver.iteration_source(statement.id, use)
    if source is None:
        return None
    _target, iterable = source
    if isinstance(iterable, ast.Name):
        iterated = resolver.parameter(iterable.id, use)
        return None if iterated is None else iterated.arg
    return None


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


# A table NAME is not an identity.  The Sessions database owns its own ``runs``
# table, so ``update(runs_table)`` in ``src/elspeth/web/`` means the Landscape
# runs table or the Sessions one depending only on which module defines the
# ``Table`` object the statement binds to.  These are the non-Landscape schema
# modules whose tables are proven to belong to another engine; anything the
# scanner cannot bind to one of them keeps failing closed on the name.
_NON_LANDSCAPE_SCHEMA_MODULES = ("elspeth.web.sessions.",)


def _dml_table_metadata_module(statement: ast.expr | None, resolver: _Resolver) -> str | None:
    """Return the module defining the ``Table`` a DML construction binds to.

    ``None`` when the statement is not a DML construction, or when its table
    expression cannot be resolved to a ``*_table`` object with a defining
    module — an unresolved binding is never treated as proof of another
    database, so the caller keeps failing closed on the table name.
    """

    if not isinstance(statement, ast.Call):
        return None
    construction = _dml_construction(statement, resolver)
    if construction is None:
        return None
    table_node, _operation, use = construction
    identity = _table_identity(table_node, resolver, use=use)
    return None if identity is None or not identity[0] else identity[0]


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
                # A text carried by a name, a subscript of a literal container,
                # or the method's own instance attribute is admitted only when
                # EVERY candidate resolves and every one is a proven read.
                exact = _raw_sql_exact_texts(node.args[0], resolver, use=node)
                if any(_raw_sql_is_write(text) for text in (exact or ())):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside raw SQL write/DDL")
                elif exact is None or not all(_raw_sql_is_proven_read(text) for text in exact):
                    violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside unknown raw SQL effect")
                continue
            if shape is None and raw_sql is not None and _raw_sql_is_write(raw_sql):
                violations.append(f"{unit.path}:{node.lineno} {_symbol(node)} outside raw SQL write/DDL")
                continue
            statement = resolver.resolve_statement(node.args[0], use=node)
            if shape is None and isinstance(statement, ast.Call):
                shape = _dml_shape(statement, resolver) or _raw_dml_shape(statement, resolver)
            metadata_module = _dml_table_metadata_module(statement, resolver)
            if metadata_module is not None and metadata_module.startswith(_NON_LANDSCAPE_SCHEMA_MODULES):
                # Proven other-engine metadata: the statement binds to a Table
                # this repository defines outside the Landscape schema.
                continue
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
    # The fence names come from ``_FENCED_CONTEXT_NAMES`` rather than being
    # spelled again here.  They were duplicated, and the duplicate silently
    # went stale when the membership fence landed: a fence this set does not
    # know is not counted as a database effect, so the verb's FIRST effect
    # looked like its payload and every member-fenced verb reported "fence is
    # not the transaction owner's first database effect" while being correctly
    # fenced. One list, one source of truth.
    effect_names = {
        "begin_write",
        "execute",
        "execute_insert",
        "execute_update",
        "exec_driver_sql",
        "scalar",
        "write_connection",
        *_CONNECTION_STATEMENT_HELPER_NAMES,
        *_FENCED_CONTEXT_NAMES,
    }
    resolver = _resolver_for_node(node)
    return tuple(
        sorted(
            (
                child
                for child in _walk_same_scope(node)
                if isinstance(child, ast.Call)
                and (
                    _resolved_callable_name(child.func, resolver, use=child) in effect_names
                    or resolver.qualified_name(child.func, use=child)
                    in {
                        "elspeth.core.landscape.database_clock.read_landscape_decision_time",
                        "elspeth.core.landscape.database_clock.read_landscape_transaction_time",
                    }
                    or _indirect_dml_execution(child, resolver)[0]
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
                result.append(FencedContext(child, item.context_expr, connection, qualified))
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


_CONNECTION_STATEMENT_HELPER_NAMES = frozenset({"execute_insert_on", "execute_update_on"})
_PAYLOAD_EFFECT_NAMES = (
    frozenset({"execute", "execute_insert", "execute_update", "exec_driver_sql", "scalar"}) | _CONNECTION_STATEMENT_HELPER_NAMES
)


def _is_database_ops_statement_helper(call: ast.Call, resolver: _Resolver) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in _CONNECTION_STATEMENT_HELPER_NAMES:
        return False
    receiver = call.func.value
    qualified = resolver.qualified_name(receiver, use=call)
    if isinstance(receiver, ast.Attribute):
        qualified = _self_attribute_owner_annotation(receiver, resolver, use=call)
    elif isinstance(receiver, ast.Name) and (parameter := resolver.parameter(receiver.id, call)) is not None:
        qualified = resolver.qualified_name(parameter.annotation, use=call) if parameter.annotation is not None else None
    return qualified == "elspeth.core.landscape._database_ops.DatabaseOps"


def _payload_uses_exact_connection(call: ast.Call, connection: str) -> bool:
    resolver = _resolver_for_node(call)
    name = _resolved_callable_name(call.func, resolver, use=call)
    if _is_database_ops_statement_helper(call, resolver) and call.args:
        return isinstance(call.args[0], ast.Name) and call.args[0].id == connection
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
    if _is_database_ops_statement_helper(call, resolver) and len(call.args) >= 2:
        return call.args[1]
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
    resolver = _resolver_for_node(node)

    def token_subject(subject: ast.expr) -> bool:
        if isinstance(subject, ast.Name) and subject.id not in argument_names:
            stores = [
                part
                for part in _walk_same_scope(node)
                if isinstance(part, ast.Name) and part.id == subject.id and isinstance(part.ctx, (ast.Store, ast.Del))
            ]
            if len(stores) != 1:
                return False
            assignment = next((part for part in _ancestors(stores[0]) if isinstance(part, ast.stmt)), None)
            if not isinstance(assignment, ast.Assign) or assignment not in node.body or assignment.lineno >= subject.lineno:
                return False
        resolved = resolver.resolve_value(subject, use=subject)
        return _exact_token_run_id_expression(resolved, token_parameter)

    for construction, root in _dml_subject_roots(node):
        for subject in _run_column_subjects(root):
            if token_subject(subject):
                continue
            if isinstance(subject, ast.Name) and subject.id == "run_id" and subject.id in argument_names:
                continue
            return f"DML construction line {construction.lineno} uses non-token run-column subject"
        for child in ast.walk(root):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id.endswith("run_id"):
                if token_subject(child):
                    continue
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
    scope = _verb_authority_scope(resolver.unit.path, node.name)
    expected_type = _AUTHORITY_QUALIFIED_BY_SCOPE[scope].rsplit(".", maxsplit=1)[-1]
    if not _is_exact_scoped_authority_annotation(parameter.annotation, scope=scope, resolver=resolver, use=node):
        return f"{scope}-scoped verb's token annotation is not {expected_type}"
    if _argument_default(node, parameter.arg) is not None:
        return "token is optional/defaulted"
    if _parameter_rebound(node, parameter.arg):
        return "token parameter is rebound"
    contexts = _fenced_contexts(node)
    if len(contexts) != 1:
        return f"fenced transaction contexts={len(contexts)} expected=1"
    context = contexts[0]
    # The fence must be the one this verb's authority class names.  A member
    # fence on a leader verb (or the reverse) is admitted by neither: each
    # class proves a different thing, and crossing them proves nothing.
    if context.fence not in _FENCE_QUALIFIED_BY_SCOPE[scope]:
        return f"{scope}-scoped verb is fenced by {context.fence.rsplit('.', maxsplit=1)[-1]}"
    if context.connection is None:
        return "fenced transaction must bind one exact connection name"
    if len(context.owner.items) != 1:
        return "full-token fence must be the sole context manager"
    if not _exact_token_keyword(context.call, parameter):
        return "fenced transaction does not receive the exact unaliased token parameter"
    if scope == _ITEM_SCOPE:
        if not _has_exact_work_item_parameter(node, resolver):
            return "item-scoped verb requires the exact claimed TokenWorkItem parameter"
        item_arguments = [keyword.value for keyword in context.call.keywords if keyword.arg == "work_item"]
        if len(item_arguments) != 1 or not isinstance(item_arguments[0], ast.Name) or item_arguments[0].id != "work_item":
            return "item fence does not receive the exact claimed work_item parameter"
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
        and not (
            effect.args
            and not _statement_contains_dml(effect.args[0], resolver, use=effect)
            and (
                _is_proven_read(effect.args[0], resolver, use=effect)
                or (
                    isinstance(resolved_payload := resolver.resolve_statement(effect.args[0], use=effect), ast.Call)
                    and _helper_return_class(resolved_payload, resolver, resolver.unit, use=effect) == "read"
                )
            )
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


@cache
def _owned_reexports(units: tuple[SourceUnit, ...]) -> dict[str, str]:
    """Resolve package exports from their actual import declarations, never spelling."""
    result: dict[str, str] = {}
    for unit in units:
        if not unit.path.startswith("src/elspeth/") or not unit.path.endswith("/__init__.py"):
            continue
        module = unit.path.removeprefix("src/").removesuffix("/__init__.py").replace("/", ".")
        bindings: dict[str, list[str | None]] = {}
        for node in unit.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
                for alias in node.names:
                    if alias.name != "*":
                        bindings.setdefault(alias.asname or alias.name, []).append(f"{node.module}.{alias.name}")
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            bindings.setdefault(name.id, []).append(None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.setdefault(node.name, []).append(None)
        for name, values in bindings.items():
            if len(values) == 1 and values[0] is not None:
                result[f"{module}.{name}"] = values[0]
    return result


def _helper_receiver_owner(receiver: ast.expr, resolver: _Resolver, use: ast.AST) -> str | None:
    """Resolve one exact repository receiver from its source construction or annotation."""
    if isinstance(receiver, ast.Call):
        return resolver.qualified_name(receiver.func, use=use)
    if isinstance(receiver, ast.Name):
        binding = resolver.binding(receiver.id, use)
        if binding is not None and binding is not receiver:
            return _helper_receiver_owner(binding, resolver, binding)
        parameter = resolver.parameter(receiver.id, use)
        if parameter is not None and parameter.annotation is not None:
            annotation = parameter.annotation
            if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
                arms = [arm for arm in (annotation.left, annotation.right) if not (isinstance(arm, ast.Constant) and arm.value is None)]
                if len(arms) != 1:
                    return None
                annotation = arms[0]
            return resolver.qualified_name(annotation, use=use)
        return resolver.qualified_name(receiver, use=use)
    if not isinstance(receiver, ast.Attribute) or not isinstance(receiver.value, ast.Name):
        return None
    located = _method_receiver(use)
    if located is None or receiver.value.id != located[1]:
        parent_owner = _helper_receiver_owner(receiver.value, resolver, use)
        if parent_owner is not None and _module_path_from_qualified(parent_owner) == resolver.unit.path:
            owner_name = parent_owner.rsplit(".", maxsplit=1)[-1]
            fields = [
                field
                for owner in resolver.unit.tree.body
                if isinstance(owner, ast.ClassDef) and owner.name == owner_name
                for field in owner.body
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name) and field.target.id == receiver.attr
            ]
            if len(fields) == 1:
                return resolver.qualified_name(fields[0].annotation, use=fields[0])
        return None
    assignments = [
        node
        for node in ast.walk(located[0])
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == located[1]
            and target.attr == receiver.attr
            for target in node.targets
        )
    ]
    if len(assignments) != 1:
        return None
    assignment = assignments[0]
    owner = _owner_function(assignment)
    if owner is None or owner.name != "__init__":
        return None
    if isinstance(assignment.value, ast.Call):
        return resolver.qualified_name(assignment.value.func, use=assignment)
    if isinstance(assignment.value, ast.Name):
        return _helper_receiver_owner(assignment.value, resolver, assignment)
    return None


def _resolve_helper_candidates(
    call: ast.Call,
    unit: SourceUnit,
    helper_keys: set[tuple[str, str]],
    resolver: _Resolver,
    *,
    helper_keys_by_terminal: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
    reexports: Mapping[str, str] | None = None,
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
        if isinstance(callable_node, ast.Attribute):
            receiver_owner = _helper_receiver_owner(callable_node.value, resolver, call)
            seen_exports: set[str] = set()
            while reexports is not None and receiver_owner in reexports and receiver_owner not in seen_exports:
                seen_exports.add(receiver_owner)
                receiver_owner = reexports[receiver_owner]
            if receiver_owner is not None and "." in receiver_owner:
                if receiver_owner.startswith("elspeth."):
                    owner_path = _module_path_from_qualified(receiver_owner)
                    owner_name = receiver_owner.rsplit(".", maxsplit=1)[-1]
                    result.update(key for key in candidates if key == (owner_path, f"{owner_name}.{terminal}"))
                continue
            if isinstance(callable_node.value, ast.Name):
                binding = resolver.binding(callable_node.value.id, call)
                if isinstance(binding, ast.IfExp):
                    owners = [
                        resolver.qualified_name(branch.func, use=branch)
                        if isinstance(branch, ast.Call)
                        else _helper_receiver_owner(branch, resolver, branch)
                        for branch in (binding.body, binding.orelse)
                    ]
                    if all(owner is not None and "." in owner for owner in owners):
                        for owner in owners:
                            assert owner is not None
                            owner_path = _module_path_from_qualified(owner)
                            owner_name = owner.rsplit(".", maxsplit=1)[-1]
                            result.update(key for key in candidates if key == (owner_path, f"{owner_name}.{terminal}"))
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
        if not unit.path.startswith("src/elspeth/core/landscape/") and unit.path != _CHECKPOINT_PATH:
            # Outside the repository package, common names such as record()
            # require source-resolved ownership; spelling is not an edge.
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


@cache
def _proven_read_statement_helpers(units: tuple[SourceUnit, ...]) -> set[tuple[str, str]]:
    """Prove every caller's query is read-only before classifying a query relay.

    Select annotations alone are insufficient: a Select may contain a writable
    CTE. Rebinding, transformed payloads, missing callers and any unproven
    argument leave the helper in the write-relay proof graph.
    """
    index = _function_index(units)
    candidates: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for key, node in index.items():
        if not key[0].startswith("src/elspeth/core/landscape/"):
            continue
        resolver = _resolver_for_node(node)
        parameter = next((arg for arg in (*node.args.args, *node.args.kwonlyargs) if arg.arg == "query"), None)
        if parameter is None or _parameter_rebound(node, "query"):
            continue
        annotation = parameter.annotation.value if isinstance(parameter.annotation, ast.Subscript) else parameter.annotation
        if annotation is None or resolver.qualified_name(annotation, use=node) not in {
            "sqlalchemy.Select",
            "sqlalchemy.sql.Select",
            "sqlalchemy.sql.selectable.Select",
        }:
            continue
        executions = [
            call
            for call in _walk_same_scope(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "execute"
        ]
        if executions and all(
            len(call.args) == 1 and isinstance(call.args[0], ast.Name) and call.args[0].id == "query" for call in executions
        ):
            candidates[key] = node
    keys = set(candidates)
    callers: dict[tuple[str, str], list[tuple[ast.Call, _Resolver]]] = {key: [] for key in keys}
    reexports = _owned_reexports(units)
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for call in ast.walk(unit.tree):
            if not isinstance(call, ast.Call):
                continue
            resolved = _resolve_helper_candidates(call, unit, keys, resolver, reexports=reexports)
            for candidate in resolved:
                callers[candidate].append((call, resolver))
    proven: set[tuple[str, str]] = set()
    for key, calls in callers.items():
        if not calls:
            continue
        helper = candidates[key]
        positional = [arg.arg for arg in (*helper.args.posonlyargs, *helper.args.args) if arg.arg != "self"]
        position = positional.index("query") if "query" in positional else None
        for call, resolver in calls:
            arguments = [keyword.value for keyword in call.keywords if keyword.arg == "query"]
            if position is not None and position < len(call.args):
                arguments.append(call.args[position])
            if (
                len(arguments) != 1
                or any(keyword.arg is None for keyword in call.keywords)
                or not _is_proven_read(arguments[0], resolver, use=call)
            ):
                break
            query = resolver.resolve_value(arguments[0], use=call)
            if any(
                isinstance(part, ast.Call) and _resolved_callable_name(part.func, resolver, use=part) == "text" for part in ast.walk(query)
            ):
                break
        else:
            proven.add(key)
    return proven


def _subordinate_helper_keys(
    index: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef],
    dml: Sequence[DmlIdentity],
    reexports: Mapping[str, str] | None = None,
    read_helpers: set[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    dml_helpers = {
        (site.path, site.symbol)
        for site in dml
        if (node := index.get((site.path, site.symbol))) is not None
        and any(argument.arg == "conn" for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
    }
    relay_helpers: set[tuple[str, str]] = set()
    for key, node in index.items():
        if read_helpers is not None and key in read_helpers:
            continue
        if not key[0].startswith("src/elspeth/core/landscape/"):
            continue
        if not any(argument.arg == "conn" for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)):
            continue
        resolver = _resolver_for_node(node)
        for call in _walk_same_scope(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "conn"
                and call.func.attr == "execute"
                and call.args
                and _relayed_statement_parameter(call.args[0], resolver, use=call) is not None
            ):
                relay_helpers.add(key)
    helpers = dml_helpers | relay_helpers
    # Connection-only relays may delegate without constructing or executing a
    # statement themselves. Include every such predecessor in the proof graph;
    # admission still recursively checks ALL of its callers, exact connection
    # forwarding, run subjects, and absence of a new transaction.
    candidates = {
        key: node
        for key, node in index.items()
        if key[0].startswith("src/elspeth/core/landscape/")
        and any(argument.arg == "conn" for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
    }
    changed = True
    while changed:
        changed = False
        terminals = _helper_key_terminal_index(helpers)
        for key, node in candidates.items():
            if key in helpers:
                continue
            resolver = _resolver_for_node(node)
            if any(
                isinstance(call, ast.Call)
                and _resolve_helper_candidates(
                    call, resolver.unit, helpers, resolver, helper_keys_by_terminal=terminals, reexports=reexports
                )
                for call in _walk_same_scope(node)
            ):
                helpers.add(key)
                changed = True
    return helpers


def _subordinate_helper_edges(
    units: Iterable[SourceUnit],
    dml: Sequence[DmlIdentity],
) -> tuple[SubordinateHelperEdge, ...]:
    unit_list = tuple(units)
    index = _function_index(unit_list)
    reexports = _owned_reexports(unit_list)
    helper_keys = _subordinate_helper_keys(index, dml, reexports, _proven_read_statement_helpers(unit_list))
    helper_keys_by_terminal = _helper_key_terminal_index(helper_keys)
    raw: list[SubordinateHelperEdge] = []
    for unit in unit_list:
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
                reexports=reexports,
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
    reexports = _owned_reexports(unit_list)
    helper_keys = _subordinate_helper_keys(index, dml, reexports, _proven_read_statement_helpers(unit_list))
    helper_keys_by_terminal = _helper_key_terminal_index(helper_keys)
    helper_terminals = helper_keys_by_terminal.keys()
    violations: list[str] = []
    for unit in unit_list:
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
                reexports=reexports,
            )
            if not candidates and isinstance(call.func, ast.Attribute):
                receiver_owner = _helper_receiver_owner(call.func.value, resolver, call)
                if receiver_owner is not None and "." in receiver_owner:
                    # An explicitly resolved different owner is a same-named
                    # method, not an ambiguous call to a Landscape helper.
                    continue
                if isinstance(call.func.value, ast.Name):
                    binding = resolver.binding(call.func.value.id, call)
                    if isinstance(binding, ast.IfExp) and all(
                        (owner := _helper_receiver_owner(branch, resolver, branch)) is not None and "." in owner
                        for branch in (binding.body, binding.orelse)
                    ):
                        continue
                if (
                    not unit.path.startswith("src/elspeth/core/landscape/")
                    and unit.path != _CHECKPOINT_PATH
                    and not _looks_like_any_landscape_receiver(call.func.value, resolver=resolver, use=call)
                ):
                    continue
            if (
                not candidates
                and isinstance(call.func, ast.Name)
                and resolver.parameter(call.func.id, call) is not None
                and not unit.path.startswith("src/elspeth/core/landscape/")
                and unit.path != _CHECKPOINT_PATH
            ):
                # A caller-supplied callable is not a reference to an imported
                # or owned Landscape function merely because names coincide.
                continue
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


_NON_RUN_DML_WRITERS = {
    ("src/elspeth/core/landscape/auth_audit_repository.py", "AuthAuditRepository._insert_auth_events"): (
        "auth_events",
        "insert",
        "permanent: ADR-048 A5 non-run authentication audit",
    ),
    ("src/elspeth/core/landscape/journal.py", "LandscapeJournal._before_commit"): (
        "sidecar_journal_outbox",
        "insert",
        "temporary: ADR-048 A5 sidecar mirror; remove with Task 8B",
    ),
}


def _heartbeat_fence_implementation_violations(units: tuple[SourceUnit, ...]) -> tuple[str, ...]:
    """The heartbeat context is trusted only in its seat-before-member form."""
    qualified = "elspeth.core.landscape.run_coordination_repository.fenced_heartbeat_transaction"
    declarations = [
        node
        for unit in units
        if unit.path == _RUN_COORDINATION_PATH
        for node in unit.tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "fenced_heartbeat_transaction"
    ]
    used = any(
        isinstance(node, ast.Call) and _resolver_for_unit(unit).qualified_name(node.func, use=node) == qualified
        for unit in units
        for node in ast.walk(unit.tree)
    )
    if not declarations and not used:
        return ()
    if len(declarations) != 1:
        return ("heartbeat fence requires exactly one inspected implementation",)
    node = declarations[0]
    expected = ast.parse(
        textwrap.dedent("""\
        if not isinstance(member_token, WorkerMembershipToken):
            raise TypeError("worker heartbeat requires a WorkerMembershipToken")
        with begin_write(engine) as conn:
            _bound_heartbeat_statement_waits(conn)
            locked_seat = conn.execute(
                select(run_coordination_table.c.run_id).where(run_coordination_table.c.run_id == member_token.run_id).with_for_update()
            ).one_or_none()
            verify_membership_fence(conn, member_token=member_token, verb=verb)
            if locked_seat is None:
                raise AuditIntegrityError(f"Run {member_token.run_id!r} has registered membership but no coordination seat")
            yield conn
    """)
    )
    body = [
        stmt
        for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str))
    ]
    resolver = _resolver_for_node(node)
    if (
        stable_ast_dump(ast.Module(body=body, type_ignores=[])) != stable_ast_dump(expected)
        or len(node.decorator_list) != 1
        or resolver.qualified_name(node.decorator_list[0], use=node) != "contextlib.contextmanager"
        or resolver.qualified_name(ast.Name(id="verify_membership_fence", ctx=ast.Load()), use=node)
        != "elspeth.core.landscape.run_coordination_repository.verify_membership_fence"
    ):
        return ("heartbeat fence implementation no longer proves seat lock, membership check, and exact connection before yield",)
    return ()


def _non_run_writer_context(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.AST, str] | None:
    resolver = _resolver_for_node(node)
    key = (resolver.unit.path, _symbol(node))
    if key not in _NON_RUN_DML_WRITERS:
        return None
    if key[1] == "LandscapeJournal._before_commit":
        parameter = next((arg for arg in node.args.args if arg.arg == "conn"), None)
        if parameter is None or resolver.qualified_name(parameter.annotation, use=node) != "sqlalchemy.engine.Connection":
            return None
        if any(
            isinstance(child, ast.Name) and child.id == "conn" and isinstance(child.ctx, (ast.Store, ast.Del))
            for child in _walk_same_scope(node)
        ):
            return None
        return node, "conn"
    contexts = [
        (child, item)
        for child in _walk_same_scope(node)
        if isinstance(child, ast.With)
        for item in child.items
        if isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "write_connection"
        and _helper_receiver_owner(item.context_expr.func.value, resolver, item.context_expr)
        == "elspeth.core.landscape.database.LandscapeDB"
        and not item.context_expr.args
        and not item.context_expr.keywords
    ]
    if len(contexts) != 1 or not isinstance(contexts[0][1].optional_vars, ast.Name):
        return None
    return contexts[0][0], contexts[0][1].optional_vars.id


def _non_run_writer_violation(node: ast.FunctionDef | ast.AsyncFunctionDef, dml: Sequence[DmlIdentity]) -> str | None:
    key = (_resolver_for_node(node).unit.path, _symbol(node))
    table, operation, _rationale = _NON_RUN_DML_WRITERS[key]
    actual = Counter((site.table, site.operation) for site in dml if (site.path, site.symbol) == key)
    if actual != Counter({(table, operation): 1}):
        return "non-run writer write set differs from its exact admitted table and operation"
    context = _non_run_writer_context(node)
    if context is None:
        return "non-run writer lacks its exact transaction connection"
    binding = _dml_execution_binding_violation(node, context[1])
    if binding is not None:
        return binding
    for call in _database_effect_calls(node):
        name = _resolved_callable_name(call.func, _resolver_for_node(node), use=call)
        if name in _PAYLOAD_EFFECT_NAMES and (not _is_descendant(call, context[0]) or not _payload_uses_exact_connection(call, context[1])):
            return "non-run writer payload escapes its exact transaction connection"
    return None


# Closed executable-source binding for the reviewed registry and journal
# dependency graph. This proves equality to the implementation validated by
# real database controls; it does not replace issuer lock/caller proofs.
# Rebinding requires semantic review and applicable mutation/runtime controls.
_REVIEWED_REGISTRY_MODULES = {
    # Nominal identities, outer-transaction ownership, exact issued expiry,
    # reserve arithmetic, no renewal DML/callbacks, and physical rollback.
    "src/elspeth/core/landscape/lease_deadlines.py": "44de6eafb463b54cdb75087ca45a41eb4259bec4122a3eff44a1045af7c3f009",
    # Separate fresh database sample, dialect-specific shape/UTC validation.
    "src/elspeth/core/landscape/database_clock.py": "e9c23dc544de396b4dab3421dba030908898eda27da4685b82c7b042ec909e65",
    # Guard registration after serialization/outbox INSERT; precommit failure
    # cleanup, transitive serialization helpers, and postcommit drain ordering.
    "src/elspeth/core/landscape/journal.py": "96162bebbae30b8eaf5d26a2b9ae1ce48101e0cbc5983468af8824ab958e4ddc",
    # Installation in both engine constructors and bare-engine begin_write;
    # transaction ownership and engine/Connection event setup are also bound.
    "src/elspeth/core/landscape/database.py": "a41206e20766c46a54831b529f229b395060fd1588b923b2240d248d99786d5a",
}


def _registry_executable_ast(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            del node.body[0]
    return stable_ast_dump(tree)


def _issued_deadline_registry_source_is_proven(sources: dict[str, str]) -> bool:
    for path, reviewed in _REVIEWED_REGISTRY_MODULES.items():
        if path not in sources:
            return False
        try:
            canonical = _registry_executable_ast(sources[path])
        except SyntaxError:
            return False
        if hashlib.sha256(canonical.encode()).hexdigest() != reviewed:
            return False
    return True


_DEADLINE_FIELDS = {
    "run_coordination": ("leader_heartbeat_expires_at", "LEADER", ("run_id", "leader_worker_id", "leader_epoch")),
    "run_workers": ("heartbeat_expires_at", "WORKER", ("run_id", "worker_id")),
    "token_work_items": ("lease_expires_at", "ITEM", ("work_item_id",)),
    "sink_effects": ("lease_expires_at", "SINK_EFFECT", ("effect_id",)),
}
_FRESH_CLOCK = "elspeth.core.landscape.database_clock.read_landscape_decision_time"
_DEADLINE_API = "elspeth.core.landscape.lease_deadlines."


def _deadline_api_is_visible(qualified: str, resolver: _Resolver, use: ast.AST) -> bool:
    if _trusted_qualified_name_is_mutated(qualified, resolver=resolver, use=use):
        return False
    owner = _owner_function(use)
    if owner is not None and not _receiver_callable_code_is_visible(owner, use):
        return False
    for part in ast.walk(resolver.unit.tree):
        if (
            isinstance(part, ast.Attribute)
            and isinstance(part.ctx, (ast.Store, ast.Del))
            and (resolver.qualified_name(part, use=part) == qualified or resolver.qualified_name(part.value, use=part) == qualified)
        ):
            return False
    return True


def _deadline_binding(expression: ast.expr, resolver: _Resolver, use: ast.AST) -> ast.expr:
    """Follow only unchanged, dominating local assignments, never a last-store guess."""
    seen: set[int] = set()
    while isinstance(expression, ast.Name) and id(expression) not in seen:
        seen.add(id(expression))
        owner = _owner_function(use)
        binding = None if owner is None else _authority_binding_origin(expression.id, owner, use)
        if not isinstance(binding, ast.expr):
            break
        expression, use = binding, binding
    return expression


def _deadline_term(expression: ast.expr, resolver: _Resolver, use: ast.AST, parameters: dict[str, str] | None = None) -> str:
    """Stable expression identity; imports normalize, unsafe mutable aliases do not."""
    if isinstance(expression, ast.Name) and parameters is not None and expression.id in parameters:
        return parameters[expression.id]
    expression = _deadline_binding(expression, resolver, use)
    if isinstance(expression, ast.Name):
        return resolver.qualified_name(expression, use=expression) or expression.id
    if isinstance(expression, ast.Constant):
        return repr(expression.value)
    if isinstance(expression, ast.Attribute):
        if isinstance(expression.value, ast.Name) and parameters is not None:
            projected = parameters.get(f"{expression.value.id}.{expression.attr}")
            if projected is not None:
                return projected
        value = _deadline_binding(expression.value, resolver, expression)
        if isinstance(value, ast.Call) and resolver.qualified_name(value.func, use=value) in {
            "elspeth.contracts.coordination.CoordinationToken",
            "elspeth.contracts.coordination.WorkerMembershipToken",
        }:
            fields = [keyword.value for keyword in value.keywords if keyword.arg == expression.attr]
            if len(fields) == 1 and not value.args and all(keyword.arg is not None for keyword in value.keywords):
                return _deadline_term(fields[0], resolver, value, parameters)
        return f"{_deadline_term(expression.value, resolver, expression, parameters)}.{expression.attr}"
    if isinstance(expression, ast.Subscript):
        return f"{_deadline_term(expression.value, resolver, expression, parameters)}[{_deadline_term(expression.slice, resolver, expression, parameters)}]"
    if isinstance(expression, ast.Call):
        arguments = [_deadline_term(item, resolver, expression, parameters) for item in expression.args]
        arguments.extend(f"{item.arg}={_deadline_term(item.value, resolver, expression, parameters)}" for item in expression.keywords)
        rendered = f"{_deadline_term(expression.func, resolver, expression, parameters)}({','.join(arguments)})"
        if resolver.qualified_name(expression.func, use=expression) == _FRESH_CLOCK:
            return f"{rendered}@{expression.lineno}:{expression.col_offset}"
        return rendered
    if isinstance(expression, ast.BinOp):
        return f"({_deadline_term(expression.left, resolver, expression, parameters)} {type(expression.op).__name__} {_deadline_term(expression.right, resolver, expression, parameters)})"
    return ast.dump(expression, include_attributes=False)


def _deadline_duration_seconds(duration: ast.expr, resolver: _Resolver) -> str:
    duration = _deadline_binding(duration, resolver, duration)
    if (
        isinstance(duration, ast.Call)
        and resolver.qualified_name(duration.func, use=duration) == "datetime.timedelta"
        and not duration.args
        and len(duration.keywords) == 1
        and duration.keywords[0].arg == "seconds"
    ):
        return _deadline_term(duration.keywords[0].value, resolver, duration)
    return f"{_deadline_term(duration, resolver, duration)}.total_seconds()"


def _deadline_column(expression: ast.expr, resolver: _Resolver, table: str) -> str | None:
    if (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Attribute)
        and expression.value.attr == "c"
        and _table_name(expression.value.value, resolver, use=expression) == table
    ):
        return expression.attr
    return None


def _deadline_write_identity(write: ast.Call, resolver: _Resolver, table: str) -> dict[str, ast.expr]:
    """Extract conjunctive exact-row predicates or INSERT's explicit identity values."""
    identity: dict[str, ast.expr] = {}
    examined: set[int] = set()

    def predicates(node: ast.expr) -> None:
        if id(node) in examined:
            return
        examined.add(id(node))
        if isinstance(node, ast.Name):
            # ``clauses = and_(clauses, extra)`` reads the prior assignment,
            # not its own multiline RHS merely because that starts above the
            # nested Name's line number. Source order follows statements.
            statement = _admission_statement(node)
            binding = resolver.binding(node.id, use=statement or node)
            if binding is not None:
                predicates(binding)
        elif isinstance(node, ast.Call) and resolver.qualified_name(node.func, use=node) in {"sqlalchemy.and_", "sqlalchemy.sql.and_"}:
            for argument in node.args:
                predicates(argument)
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            column = _deadline_column(node.left, resolver, table)
            if column is not None:
                identity[column] = node.comparators[0]

    current: ast.expr = write
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        if current.func.attr == "where":
            for argument in current.args:
                predicates(argument)
        current = current.func.value
    shape = _dml_shape(current, resolver) if isinstance(current, ast.Call) else None
    if shape is not None:
        # Issuance keys identify the row AFTER the successful statement. A
        # takeover legitimately changes worker/epoch in its SET values.
        values = _deadline_write_values(write, resolver)
        if values is not None:
            identity.update(values)
    return identity


def _deadline_write_values(write: ast.Call, resolver: _Resolver) -> dict[str, ast.expr] | None:
    values: dict[str, ast.expr] = {}

    def mapping(expression: ast.expr) -> bool:
        expression = _deadline_binding(expression, resolver, expression)
        if not isinstance(expression, ast.Dict):
            return False
        for key, value in zip(expression.keys, expression.values, strict=True):
            if key is None:
                if not mapping(value):
                    return False
            elif isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value not in values:
                values[key.value] = value
            else:
                return False
        return True

    if len(write.args) > 1 or (write.args and not mapping(write.args[0])):
        return None
    for keyword in write.keywords:
        if keyword.arg is None:
            if not mapping(keyword.value):
                return None
        elif keyword.arg in values:
            return None
        else:
            values[keyword.arg] = keyword.value
    return values


def _deadline_key_terms(
    key: ast.expr,
    resolver: _Resolver,
    proof: _AuthorityProof,
    parameters: dict[str, str] | None = None,
    seen: frozenset[int] = frozenset(),
) -> tuple[str, tuple[str, ...]] | None:
    """Inline only an actual pure owned key constructor, with its caller's values."""
    if not isinstance(key, ast.Call) or id(key) in seen:
        return None
    if resolver.qualified_name(key.func, use=key) == _DEADLINE_API + "DeadlineKey":
        if not _deadline_api_is_visible(_DEADLINE_API + "DeadlineKey", resolver, key):
            return None
        if len(key.args) != 2 or key.keywords or not isinstance(key.args[1], ast.Tuple):
            return None
        kind = resolver.qualified_name(key.args[0], use=key)
        if kind is None or not kind.startswith(_DEADLINE_API + "DeadlineKind."):
            return None
        if not _deadline_api_is_visible(kind, resolver, key):
            return None
        return kind.rsplit(".", 1)[-1], tuple(_deadline_term(item, resolver, key, parameters) for item in key.args[1].elts)
    called = proof.called_function(key, resolver, key)
    if called is None:
        return None
    function, helper_resolver = called
    body = [
        statement
        for statement in function.body
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return None
    arguments = [*function.args.posonlyargs, *function.args.args]
    if function.args.vararg is not None or function.args.kwarg is not None or len(key.args) > len(arguments):
        return None
    supplied = {argument.arg: value for argument, value in zip(arguments, key.args, strict=False)}
    for keyword in key.keywords:
        if keyword.arg is None or keyword.arg in supplied:
            return None
        supplied[keyword.arg] = keyword.value
    if set(supplied) != {argument.arg for argument in (*arguments, *function.args.kwonlyargs)}:
        return None
    substitutions = {name: _deadline_term(value, resolver, key, parameters) for name, value in supplied.items()}
    # Project token fields before passing the pure helper environment. The
    # key's token may be the just-created immutable admission result.
    for name, value in supplied.items():
        resolved = _deadline_binding(value, resolver, key)
        if isinstance(resolved, ast.Call) and resolver.qualified_name(resolved.func, use=resolved) in {
            "elspeth.contracts.coordination.CoordinationToken",
            "elspeth.contracts.coordination.WorkerMembershipToken",
        }:
            for field in resolved.keywords:
                if field.arg is not None:
                    substitutions[f"{name}.{field.arg}"] = _deadline_term(field.value, resolver, resolved, parameters)
    return _deadline_key_terms(body[0].value, helper_resolver, proof, substitutions, seen | {id(key)})


def _deadline_registration_shape(
    call: ast.Call, resolver: _Resolver, proof: _AuthorityProof
) -> tuple[str, tuple[str, ...], str, str, str] | None:
    if resolver.qualified_name(call.func, use=call) != _DEADLINE_API + "record_issued_deadline" or len(call.args) != 1:
        return None
    if not _deadline_api_is_visible(_DEADLINE_API + "record_issued_deadline", resolver, call):
        return None
    keywords = {item.arg: item.value for item in call.keywords}
    if set(keywords) != {"key", "expires_at", "window_seconds"}:
        return None
    key = _deadline_key_terms(keywords["key"], resolver, proof)
    if key is None:
        return None
    return (
        *key,
        _deadline_term(call.args[0], resolver, call),
        _deadline_term(keywords["expires_at"], resolver, call),
        _deadline_term(keywords["window_seconds"], resolver, call),
    )


def _deadline_registration_covers_success(write: ast.Call, registration: ast.Call) -> bool:
    """Every successful-write path registers before normal transaction exit.

    Evaluate only literal flags and this exact UPDATE result's rowcount. An
    unknown branch is checked on both arms; returning before registration is
    never treated as rollback. This also proves the scheduler's explicit
    ``lease_lost`` flag without granting conditional registrations generally.
    """
    statement = _admission_statement(write)
    block = None if statement is None else _admission_block(statement)
    if block is None:
        return False
    result_name = (
        statement.targets[0].id
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)
        else None
    )
    owner = _owner_function(write)
    if (
        result_name is not None
        and owner is not None
        and any(
            isinstance(part, ast.Name)
            and part.id == result_name
            and isinstance(part.ctx, (ast.Store, ast.Del))
            and part is not statement.targets[0]
            and part.lineno > statement.lineno
            for part in _walk_same_scope(owner)
        )
    ):
        return False
    flags: dict[str, bool] = {}
    predecessors: list[ast.stmt] = []
    continuation = list(block[block.index(statement) + 1 :])
    current = statement
    while current is not None:
        current_block = _admission_block(current)
        if current_block is None:
            break
        predecessors = [*current_block[: current_block.index(current)], *predecessors]
        parent = getattr(current, "_landscape_parent", None)
        if isinstance(parent, ast.If):
            parent_block = _admission_block(parent)
            if parent_block is None:
                return False
            continuation.extend(parent_block[parent_block.index(parent) + 1 :])
        if not isinstance(parent, (ast.If, ast.With, ast.Try)):
            break
        current = parent
    for previous in predecessors:
        if isinstance(previous, ast.Assign) and len(previous.targets) == 1 and isinstance(previous.targets[0], ast.Name):
            if isinstance(previous.value, ast.Constant) and isinstance(previous.value.value, bool):
                flags[previous.targets[0].id] = previous.value.value
            else:
                flags.pop(previous.targets[0].id, None)
        elif isinstance(previous, (ast.AnnAssign, ast.AugAssign)) and isinstance(previous.target, ast.Name):
            flags.pop(previous.target.id, None)
    owner = _owner_function(write)
    if owner is not None:
        for part in ast.walk(owner):
            if isinstance(part, (ast.Nonlocal, ast.Global)):
                for name in part.names:
                    flags.pop(name, None)

    def condition(expression: ast.expr, known: dict[str, bool]) -> bool | None:
        if isinstance(expression, ast.Constant) and isinstance(expression.value, bool):
            return expression.value
        if isinstance(expression, ast.Name):
            return known.get(expression.id)
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
            value = condition(expression.operand, known)
            return None if value is None else not value
        if (
            result_name is not None
            and isinstance(expression, ast.Compare)
            and len(expression.ops) == 1
            and isinstance(expression.left, ast.Attribute)
            and expression.left.attr == "rowcount"
            and isinstance(expression.left.value, ast.Name)
            and expression.left.value.id == result_name
            and isinstance(expression.comparators[0], ast.Constant)
            and type(expression.comparators[0].value) is int
        ):
            expected = expression.comparators[0].value
            if isinstance(expression.ops[0], ast.Eq):
                return expected == 1
            if isinstance(expression.ops[0], ast.NotEq):
                return expected != 1
        return None

    def covers(statements: list[ast.stmt], known: dict[str, bool]) -> bool:
        if not statements:
            return False
        head, *tail = statements
        if isinstance(head, ast.Expr) and head.value is registration:
            return True
        if isinstance(head, ast.Raise):
            for ancestor in _ancestors(head):
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    break
                if (
                    isinstance(ancestor, ast.Try)
                    and any(_is_descendant(head, statement) for statement in ancestor.body)
                    and any(not _authority_block_raises(handler.body) for handler in ancestor.handlers)
                ):
                    return False
            return True
        if isinstance(head, (ast.Return, ast.Break, ast.Continue, ast.Try, ast.With, ast.For, ast.While)):
            return False
        if isinstance(head, ast.If):
            verdict = condition(head.test, known)
            branches = [head.body] if verdict is True else [head.orelse] if verdict is False else [head.body, head.orelse]
            return all(covers([*branch, *tail], dict(known)) for branch in branches)
        if isinstance(head, ast.Assign) and len(head.targets) == 1 and isinstance(head.targets[0], ast.Name):
            value = condition(head.value, known)
            if value is None:
                known.pop(head.targets[0].id, None)
            else:
                known[head.targets[0].id] = value
        elif isinstance(head, (ast.AnnAssign, ast.AugAssign)) and isinstance(head.target, ast.Name):
            known.pop(head.target.id, None)
        for part in ast.walk(head):
            if isinstance(part, ast.NamedExpr) and isinstance(part.target, ast.Name):
                known.pop(part.target.id, None)
        return covers(tail, known)

    return covers(continuation, flags)


def _deadline_lock_predecessors(use, function):
    """Earlier statements which dominate this use, excluding branch decoys."""
    levels = []
    current = _admission_statement(use)
    while current is not None and current is not function:
        block = _admission_block(current)
        if block is None:
            return ()
        levels.append(tuple(block[: block.index(current)]))
        parent = getattr(current, "_landscape_parent", None)
        if parent is function:
            break
        current = _admission_statement(parent)
    return tuple(statement for level in reversed(levels) for statement in level)


def _deadline_lock_builtin(node, name, resolver, use):
    return (
        isinstance(node, ast.Name)
        and node.id == name
        and resolver.binding(name, use) is None
        and resolver.parameter(name, use) is None
        and not resolver.is_local(name, use)
        and resolver.qualified_name(node, use=use) in {name, f"builtins.{name}"}
    )


def _deadline_lock_unalias(node, resolver, use, parameters, seen=frozenset()):
    """Return an expression together with its original lexical use site."""
    identity = (id(node), id(use))
    if identity in seen:
        return None
    seen = seen | {identity}
    if not isinstance(node, ast.Name):
        return node, resolver, use
    owner = _owner_function(use)
    value = resolver.binding(node.id, use)
    for part in _walk_same_scope(owner) if owner is not None else ():
        if not isinstance(part, (ast.AugAssign, ast.NamedExpr, ast.Delete)):
            continue
        if getattr(part, "lineno", 0) >= getattr(use, "lineno", 0):
            continue
        if value is not None and getattr(part, "lineno", 0) < getattr(value, "lineno", 0):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == node.id and isinstance(target.ctx, (ast.Store, ast.Del))
            for target in ast.walk(part)
        ):
            return None
    if value is not None:
        if not _receiver_binding_dominates(value, use):
            return None
        return _deadline_lock_unalias(value, resolver, value, parameters, seen)
    supplied = parameters.get((id(owner), node.id))
    if supplied is not None:
        actual, actual_resolver, actual_use = supplied
        return _deadline_lock_unalias(actual, actual_resolver, actual_use, parameters, seen)
    # A conditional, loop, walrus, augmented, or deleted binding cannot be
    # mistaken for the original parameter/with-bound connection.
    for part in _walk_same_scope(owner) if owner is not None else ():
        if isinstance(part, ast.Name) and part.id == node.id and isinstance(part.ctx, (ast.Store, ast.Del)):
            if getattr(part, "lineno", 0) >= getattr(use, "lineno", 0):
                continue
            parent = getattr(part, "_landscape_parent", None)
            if isinstance(parent, ast.withitem) and parent.optional_vars is part:
                continue
            return None
    return node, resolver, use


def _deadline_completed_select(node, resolver, use, parameters):
    """Resolve consumed conn.execute(SELECT), never an unexecuted query value."""
    resolved = _deadline_lock_unalias(node, resolver, use, parameters)
    if resolved is None:
        return None
    node, resolver, use = resolved
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in {"all", "fetchall", "fetchone", "first", "one", "one_or_none", "scalar_one", "scalar_one_or_none"}:
        return None
    if node.args or node.keywords:
        return None
    current = node.func.value
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute) and current.func.attr in {"mappings", "scalars"}:
        if current.args or current.keywords:
            return None
        current = current.func.value
    if not isinstance(current, ast.Call) or not isinstance(current.func, ast.Attribute) or current.func.attr != "execute":
        return None
    if len(current.args) != 1 or current.keywords:
        return None
    query = _deadline_lock_unalias(current.args[0], resolver, current, parameters)
    if query is None:
        return None
    query_node, query_resolver, query_use = query
    methods = []
    while isinstance(query_node, ast.Call) and isinstance(query_node.func, ast.Attribute):
        if query_node.func.attr not in {"where", "order_by", "with_for_update", "select_from", "limit", "offset"}:
            return None  # No CTE, limit, offset, UNION, stream options or opaque SQL.
        methods.append(query_node)
        query_node = query_node.func.value
    if not isinstance(query_node, ast.Call) or query_resolver.qualified_name(query_node.func, use=query_node) not in {
        "sqlalchemy.select",
        "sqlalchemy.sql.select",
        "sqlalchemy.sql.expression.select",
    }:
        return None
    return current.func.value, resolver, current, query_node, methods, query_resolver, query_use


def _deadline_lock_conjuncts(node, resolver):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd):
        return (*_deadline_lock_conjuncts(node.left, resolver), *_deadline_lock_conjuncts(node.right, resolver))
    if isinstance(node, ast.Call) and resolver.qualified_name(node.func, use=node) in {"sqlalchemy.and_", "sqlalchemy.sql.and_"}:
        if node.keywords:
            return ()
        return tuple(atom for argument in node.args for atom in _deadline_lock_conjuncts(argument, resolver))
    # Do not walk into OR, arbitrary calls, NOT, subqueries or conditional terms.
    return (node,)


def _deadline_lock_column(node, resolver, use):
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute) or node.value.attr != "c":
        return None
    table = _table_identity(node.value.value, resolver, use=use)
    return None if table is None else (table, node.attr)


def _deadline_lock_unrelated_label(node, fields):
    # Extra computed SELECT columns cannot change target lock identity. Their
    # explicit SQL result label must not shadow any key projected from the row.
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "label"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value not in fields
    )


def _deadline_lock_term(node, resolver, use, parameters, seen=frozenset()):
    identity = (id(node), id(use))
    if identity in seen:
        return None
    seen = seen | {identity}
    resolved = _deadline_lock_unalias(node, resolver, use, parameters)
    if resolved is None:
        return None
    node, resolver, use = resolved
    if isinstance(node, ast.Name):
        return ("binding", id(_owner_function(use)), node.id)
    if isinstance(node, ast.Constant):
        return ("constant", type(node.value).__name__, repr(node.value))
    if isinstance(node, ast.Call):
        # A row materialized once by an assignment has a distinct evaluation
        # identity; separate query calls never collapse just because SQL matches.
        parent = getattr(node, "_landscape_parent", None)
        if (
            isinstance(parent, (ast.Assign, ast.AnnAssign))
            and parent.value is node
            and _deadline_completed_select(node, resolver, use, parameters) is not None
        ):
            return ("materialized-row", id(node))
        return None
    if isinstance(node, ast.Attribute):
        base, field = node.value, node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        base, field = node.value, node.slice.value
    else:
        return None
    # An exact key projected from a locked row is the key the SELECT matched,
    # not a new identity named 'row'. This handles row = locked_row safely.
    projection = _deadline_completed_select(base, resolver, use, parameters)
    if projection is not None:
        _conn, _cr, _cu, selected_root, methods, qr, qu = projection
        projection_tables = []
        for argument in selected_root.args:
            column = _deadline_lock_column(argument, qr, qu)
            selected_table = column[0] if column is not None else _table_identity(argument, qr, use=qu)
            if selected_table is None:
                if _deadline_lock_unrelated_label(argument, {field}):
                    continue
                return None
            projection_tables.append(selected_table)
        if len(set(projection_tables)) != 1:
            return None
        subjects = []
        for method in methods:
            if method.func.attr != "where":
                continue
            for predicate in method.args:
                for atom in _deadline_lock_conjuncts(predicate, qr):
                    if isinstance(atom, ast.Compare) and len(atom.ops) == 1 and isinstance(atom.ops[0], ast.Eq):
                        for column, subject in ((atom.left, atom.comparators[0]), (atom.comparators[0], atom.left)):
                            identity_column = _deadline_lock_column(column, qr, atom)
                            if identity_column == (projection_tables[0], field):
                                subjects.append(_deadline_lock_term(subject, qr, atom, parameters, seen))
        if len(subjects) == 1:
            return subjects[0]
        if subjects:
            return None
        # No key equality on an optimistic SELECT: retain that exact row's
        # evaluation identity for the later locking SELECT's key predicate.
    parent = _deadline_lock_term(base, resolver, use, parameters, seen)
    return None if parent is None else ("field", parent, field)


def _deadline_lock_set_contains(node, expected, resolver, use, parameters, proof, seen=frozenset()):
    """Prove inclusion in explicit collections, including sorted({key,...}-{None})."""
    if id(node) in seen:
        return False
    seen = seen | {id(node)}
    resolved = _deadline_lock_unalias(node, resolver, use, parameters)
    if resolved is None:
        return False
    node, resolver, use = resolved
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_deadline_lock_term(value, resolver, use, parameters) == expected for value in node.elts)
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
        and _deadline_lock_builtin(node.func, "sorted", resolver, use)
    ):
        return _deadline_lock_set_contains(node.args[0], expected, resolver, use, parameters, proof, seen)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and isinstance(node.right, ast.Set):
        # Removing None is safe for an exact key only when its non-nullness is
        # proven by the caller's supplied key or an owned fail-closed guard.
        removed = [_deadline_lock_term(value, resolver, use, parameters) for value in node.right.elts]
        if any(value is None or value == expected for value in removed):
            return False
        if removed != [("constant", "NoneType", "None")]:
            return False
        if not _deadline_lock_nonnull_key(expected, resolver, use, parameters, proof):
            return False
        return _deadline_lock_set_contains(node.left, expected, resolver, use, parameters, proof, seen)
    return False


def _deadline_guard_refuses_none(test, expected, resolver, parameters):
    # Positive Boolean evidence only: one true OR arm suffices; every AND
    # arm must be true. An unknown call cannot manufacture non-null evidence.
    if isinstance(test, ast.BoolOp):
        values = [_deadline_guard_refuses_none(part, expected, resolver, parameters) for part in test.values]
        return any(values) if isinstance(test.op, ast.Or) else all(values)
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    right = test.comparators[0]
    if isinstance(test.ops[0], ast.Is) and isinstance(right, ast.Constant) and right.value is None:
        return _deadline_lock_term(test.left, resolver, test, parameters) == expected
    # Actual _require_hash uses `type(value) is not str or ...`. This proves
    # refusal of None from its body, not from the function spelling or annotation.
    left = test.left
    return (
        isinstance(test.ops[0], ast.IsNot)
        and isinstance(left, ast.Call)
        and len(left.args) == 1
        and not left.keywords
        and _deadline_lock_builtin(left.func, "type", resolver, test)
        and _deadline_lock_builtin(right, "str", resolver, test)
        and _deadline_lock_term(left.args[0], resolver, test, parameters) == expected
    )


def _deadline_lock_nonnull_key(expected, resolver, use, parameters, proof):
    owner = _owner_function(use)
    for statement in _deadline_lock_predecessors(use, owner):
        candidates = [(statement, resolver, parameters)]
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            found = proof.called_function(call, resolver, call, parameter_values=parameters)
            if found is not None:
                callee, callee_resolver = found
                bound = _receiver_call_parameters(callee, call, resolver, call, parameters)
                # Only a straight-line, synchronous guard before any early
                # return or side effect is admitted. Actual hash guard is one If.
                if bound is not None and not isinstance(callee, ast.AsyncFunctionDef):
                    body = callee.body
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        body = body[1:]
                    if body and isinstance(body[0], ast.If):
                        candidates.append((body[0], callee_resolver, bound))
        for guard, guard_resolver, guard_parameters in candidates:
            if (
                isinstance(guard, ast.If)
                and not guard.orelse
                and _authority_block_raises(guard.body)
                and _deadline_guard_refuses_none(guard.test, expected, guard_resolver, guard_parameters)
            ):
                return True
    return False


def _deadline_select_locks_target(value, resolver, use, parameters, proof, *, expected_connection, table, expected_keys):
    parsed = _deadline_completed_select(value, resolver, use, parameters)
    if parsed is None:
        return False
    conn, conn_resolver, conn_use, root, methods, query_resolver, query_use = parsed
    if _deadline_lock_term(conn, conn_resolver, conn_use, parameters) != expected_connection:
        return False
    if any(method.func.attr in {"limit", "offset"} for method in methods):
        return False
    locks = [method for method in methods if method.func.attr == "with_for_update"]
    if len(locks) != 1 or locks[0].args:
        return False
    for keyword in locks[0].keywords:
        if keyword.arg == "of":
            if _table_identity(keyword.value, query_resolver, use=keyword.value) != table:
                return False
        elif keyword.arg not in {"read", "key_share", "nowait", "skip_locked"} or not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        ):
            return False
    selected = []
    for argument in root.args:
        column = _deadline_lock_column(argument, query_resolver, query_use)
        identity = column[0] if column is not None else _table_identity(argument, query_resolver, use=query_use)
        if identity is None:
            if _deadline_lock_unrelated_label(argument, set(expected_keys)):
                continue
            return False
        selected.append(identity)
    if table not in selected or any(identity != table for identity in selected):
        return False
    if root.keywords:
        return False
    for method in methods:
        if method.func.attr == "select_from":
            if method.keywords or not method.args or any(_table_identity(arg, query_resolver, use=method) != table for arg in method.args):
                return False
        elif method.func.attr in {"where", "order_by"} and method.keywords:
            return False
    proven = set()
    for method in methods:
        if method.func.attr != "where":
            continue
        for predicate in method.args:
            atoms = _deadline_lock_conjuncts(predicate, query_resolver)
            if not atoms:
                return False
            for atom in atoms:
                matched = None
                if isinstance(atom, ast.Compare) and len(atom.ops) == 1 and isinstance(atom.ops[0], ast.Eq):
                    for column, subject in ((atom.left, atom.comparators[0]), (atom.comparators[0], atom.left)):
                        identity = _deadline_lock_column(column, query_resolver, atom)
                        if (
                            identity is not None
                            and identity[0] == table
                            and identity[1] in expected_keys
                            and (_deadline_lock_term(subject, query_resolver, atom, parameters) == expected_keys[identity[1]])
                        ):
                            matched = identity[1]
                elif (
                    isinstance(atom, ast.Call)
                    and isinstance(atom.func, ast.Attribute)
                    and atom.func.attr == "in_"
                    and len(atom.args) == 1
                    and not atom.keywords
                ):
                    identity = _deadline_lock_column(atom.func.value, query_resolver, atom)
                    if (
                        identity is not None
                        and identity[0] == table
                        and identity[1] in expected_keys
                        and _deadline_lock_set_contains(atom.args[0], expected_keys[identity[1]], query_resolver, atom, parameters, proof)
                    ):
                        matched = identity[1]
                # A correct key conjunct does not cancel an additional false,
                # foreign-key or state predicate: that SELECT may lock no row.
                if matched is None:
                    return False
                proven.add(matched)
    return proven == set(expected_keys)


def _deadline_lock_carries_connection(value, resolver, use, parameters, expected_connection, seen=frozenset()):
    resolved = _deadline_lock_unalias(value, resolver, use, parameters)
    if resolved is None:
        # An unresolved name can conceal an augmented/deleted connection alias.
        return isinstance(value, ast.Name)
    value, resolver, use = resolved
    if id(value) in seen:
        return True
    seen = seen | {id(value)}
    if _deadline_lock_term(value, resolver, use, parameters) == expected_connection:
        return True
    if isinstance(value, (ast.Attribute, ast.Subscript)):
        return _deadline_lock_carries_connection(value.value, resolver, use, parameters, expected_connection, seen)
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return any(_deadline_lock_carries_connection(part, resolver, use, parameters, expected_connection, seen) for part in value.elts)
    if isinstance(value, ast.Dict):
        return any(
            _deadline_lock_carries_connection(part, resolver, use, parameters, expected_connection, seen)
            for part in (*value.keys, *value.values)
            if part is not None
        )
    if isinstance(value, ast.IfExp):
        return any(
            _deadline_lock_carries_connection(part, resolver, use, parameters, expected_connection, seen)
            for part in (value.body, value.orelse)
        )
    # Call arguments are checked separately. A returned PID or materialized row
    # does not carry Connection merely because its producing call used one.
    return False


def _deadline_lock_execute_is_select(call, resolver, parameters):
    # Preserve ordinary SELECT helper reads, without trusting execute's name
    # as evidence that an opaque SQL payload cannot commit/rollback.
    if len(call.args) != 1 or call.keywords:
        return False
    resolved = _deadline_lock_unalias(call.args[0], resolver, call, parameters)
    if resolved is None:
        return False
    query, query_resolver, _use = resolved
    while isinstance(query, ast.Call) and isinstance(query.func, ast.Attribute):
        if query.func.attr not in {"where", "order_by", "with_for_update", "select_from", "limit", "offset"}:
            return False
        query = query.func.value
    return isinstance(query, ast.Call) and query_resolver.qualified_name(query.func, use=query) in {
        "sqlalchemy.select",
        "sqlalchemy.sql.select",
        "sqlalchemy.sql.expression.select",
    }


def _deadline_lock_call_preserves_transaction(call, resolver, proof, parameters, expected_connection, seen):
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    receiver_is_connection = receiver is not None and _deadline_lock_term(receiver, resolver, call, parameters) == expected_connection
    if receiver_is_connection:
        if call.func.attr == "execute":
            return _deadline_lock_execute_is_select(call, resolver, parameters)
        if call.func.attr == "exec_driver_sql":
            # The actual observer helper issues this fixed read. Raw arbitrary
            # SQL, multiple statements and search-path function names are refused.
            return (
                len(call.args) == 1
                and not call.keywords
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                and " ".join(call.args[0].value.split()).upper() == "SELECT PG_BACKEND_PID()"
            )
        return False  # commit/rollback/close/invalidate and unknown methods.
    supplied = [call.func, *call.args, *(keyword.value for keyword in call.keywords)]
    if receiver is not None:
        supplied.append(receiver)
    carries = any(_deadline_lock_carries_connection(value, resolver, call, parameters, expected_connection) for value in supplied)
    if not carries:
        return True
    if _deadline_lock_builtin(call.func, "id", resolver, call) and len(call.args) == 1 and not call.keywords:
        return True  # builtin id cannot invoke callbacks or release a transaction.
    found = proof.called_function(call, resolver, call, parameter_values=parameters)
    if found is None:
        return False  # Unknown callback carrying this connection can release it.
    callee, callee_resolver = found
    if id(callee) in seen or isinstance(callee, ast.AsyncFunctionDef):
        return False
    bound = _receiver_call_parameters(callee, call, resolver, call, parameters)
    if bound is None:
        return False
    return _deadline_lock_scope_preserves_transaction(callee, callee_resolver, proof, bound, expected_connection, seen | {id(callee)})


def _deadline_lock_scope_preserves_transaction(scope, resolver, proof, parameters, expected_connection, seen):
    for part in _walk_same_scope(scope):
        if isinstance(part, ast.Call) and not _deadline_lock_call_preserves_transaction(
            part, resolver, proof, parameters, expected_connection, seen
        ):
            return False
        if isinstance(part, (ast.With, ast.AsyncWith)):
            for item in part.items:
                if _deadline_lock_carries_connection(item.context_expr, resolver, item, parameters, expected_connection):
                    return False  # Context exit can release the supplied connection.
        if isinstance(part, (ast.Assign, ast.AnnAssign)):
            value = part.value
            targets = part.targets if isinstance(part, ast.Assign) else [part.target]
            if (
                value is not None
                and _deadline_lock_carries_connection(value, resolver, part, parameters, expected_connection)
                and any(not isinstance(target, ast.Name) for target in targets)
            ):
                return False  # Do not allow escape into a receiver/container.
        if (
            isinstance(part, ast.Return)
            and part.value is not None
            and _deadline_lock_carries_connection(part.value, resolver, part, parameters, expected_connection)
        ):
            return False  # An unmodelled returned capability can hide later release.
    return True


def _deadline_target_lock_before(use, function, resolver, proof, parameters, *, expected_connection, table, expected_keys, seen):
    if id(function) in seen:
        return False
    seen = seen | {id(function)}
    for statement in reversed(_deadline_lock_predecessors(use, function)):
        # Inspect transitive helper effects and fail closed on unknown calls
        # receiving this connection, even when the call is nested in a branch.
        if not _deadline_lock_scope_preserves_transaction(statement, resolver, proof, parameters, expected_connection, frozenset()):
            return False
        if not isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return)) or statement.value is None:
            continue
        value = statement.value
        if _deadline_select_locks_target(
            value, resolver, value, parameters, proof, expected_connection=expected_connection, table=table, expected_keys=expected_keys
        ):
            return True
        if not isinstance(value, ast.Call):
            continue
        found = proof.called_function(value, resolver, value, parameter_values=parameters)
        if found is None:
            continue
        callee, callee_resolver = found
        if not callee_resolver.unit.path.startswith("src/elspeth/core/landscape/") or isinstance(callee, ast.AsyncFunctionDef):
            continue
        # This is an ordinary completed call, never a deferred generator.
        if any(isinstance(part, (ast.Yield, ast.YieldFrom, ast.Await)) for part in _walk_same_scope(callee)):
            continue
        bound = _receiver_call_parameters(callee, value, resolver, value, parameters)
        if bound is None:
            continue
        returns = [part for part in _walk_same_scope(callee) if isinstance(part, ast.Return)]
        if not returns or not _authority_relay_block_terminates(callee.body):
            continue  # Conservative: an implicit-return helper needs its own CFG proof.
        if all(
            _deadline_target_lock_before(
                ret,
                callee,
                callee_resolver,
                proof,
                bound,
                expected_connection=expected_connection,
                table=table,
                expected_keys=expected_keys,
                seen=seen,
            )
            or (
                ret.value is not None
                and _deadline_select_locks_target(
                    ret.value,
                    callee_resolver,
                    ret,
                    bound,
                    proof,
                    expected_connection=expected_connection,
                    table=table,
                    expected_keys=expected_keys,
                )
            )
            for ret in returns
        ):
            return True
    return False


def _target_lock_precedes_fresh_sample(sample, *, function, resolver, proof, connection, table, key_values):
    """Prove completed source-owned exclusive target lock before this sample.

    table: exact (metadata_module, table_name), never a suffix/name hint.
    key_values: every required column mapped to its caller expression at sample.
    No authority is granted to a helper based on its name.
    """
    expected_connection = _deadline_lock_term(connection, resolver, sample, {})
    expected_keys = {key: _deadline_lock_term(value, resolver, sample, {}) for key, value in key_values.items()}
    if expected_connection is None or not expected_keys or any(value is None for value in expected_keys.values()):
        return False
    return _deadline_target_lock_before(
        sample,
        function,
        resolver,
        proof,
        {},
        expected_connection=expected_connection,
        table=table,
        expected_keys=expected_keys,
        seen=frozenset(),
    )


def _deadline_executed_writes(function, resolver):
    """Enumerate execution payloads as well as fluent values; never skip bulk parameters."""
    for call in _walk_same_scope(function):
        if not isinstance(call, ast.Call) or not call.args:
            continue
        callee = resolver.resolve_callable(call.func, use=call)
        if not isinstance(callee, ast.Attribute) or callee.attr != "execute":
            continue
        statement = _deadline_binding(call.args[0], resolver, call)
        roots = [item for item in ast.walk(statement) if isinstance(item, ast.Call) and _dml_shape(item, resolver) is not None]
        if len(roots) != 1:
            continue
        construction = _dml_construction(roots[0], resolver)
        assert construction is not None
        table = _table_identity(construction[0], resolver, use=construction[2])
        if table is None or table[0] != "elspeth.core.landscape.schema" or table[1] not in _DEADLINE_FIELDS:
            continue
        if construction[1] == "delete":
            continue
        value_calls = [
            item
            for item in ast.walk(statement)
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "values"
        ]
        write = value_calls[0] if len(value_calls) == 1 else statement
        values = _deadline_write_values(value_calls[0], resolver) if len(value_calls) == 1 else {} if not value_calls else None
        parameters = [*call.args[1:], *(keyword.value for keyword in call.keywords if keyword.arg == "parameters")]
        if len(parameters) > 1:
            yield write, call, callee.value, table[1], None
            continue
        if not parameters:
            yield write, call, callee.value, table[1], values
            continue
        payload = _deadline_binding(parameters[0], resolver, call)
        rows = payload.elts if isinstance(payload, (ast.List, ast.Tuple)) else [payload]
        for row in rows:
            parameter_call = ast.Call(func=ast.Name(id="parameters", ctx=ast.Load()), args=[row], keywords=[])
            supplied = _deadline_write_values(parameter_call, resolver)
            if values is None or supplied is None or values.keys() & supplied.keys():
                yield write, call, callee.value, table[1], None
            else:
                yield write, call, callee.value, table[1], {**values, **supplied}


def _deadline_decision_uses_one_sample(write, execute, sample, write_values, table, resolver, function):
    for stamp in {"updated_at", "lease_heartbeat_at", "registered_at"} & write_values.keys():
        if _deadline_binding(write_values[stamp], resolver, execute) is not sample:
            return False
    seen: set[int] = set()
    comparisons: list[ast.Compare] = []

    def visit(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ast.Name):
            origin = resolver.binding(node.id, use=_admission_statement(node) or node)
            if origin is not None:
                visit(origin)
        elif isinstance(node, ast.Compare):
            comparisons.append(node)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child)

    # UPDATE eligibility includes predicates bound through an explicit local
    # WHERE expression. Read-only candidate discovery is deliberately excluded.
    current = write
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        if current.func.attr == "where":
            for argument in current.args:
                visit(argument)
        current = current.func.value
    for call in _walk_same_scope(function):
        if (
            isinstance(call, ast.Call)
            and resolver.qualified_name(call.func, use=call) == "elspeth.core.landscape.execution.sink_effect_lifecycle.lease_is_live"
        ):
            for argument in call.args:
                visit(argument)
    temporal = {"available_at", "lease_expires_at", "leader_heartbeat_expires_at", "heartbeat_expires_at"}
    for comparison in comparisons:
        if len(comparison.ops) != 1:
            return False
        for column, value in ((comparison.left, comparison.comparators[0]), (comparison.comparators[0], comparison.left)):
            if _deadline_column(column, resolver, table) in temporal and _deadline_binding(value, resolver, comparison) is not sample:
                return False
    return True


# Conjunct this mutation/escape proof with the separate
# exact implementation recipes. It never grants authority by namespace alone.

_DEADLINE_REGISTRY_NAMESPACES = frozenset(
    {
        "elspeth.core.landscape.lease_deadlines",
        "elspeth.core.landscape.database_clock",
        "elspeth.core.landscape.journal",
        "elspeth.core.landscape.database",
    }
)


_DEADLINE_FINALIZATION_DEPENDENCIES = frozenset(
    {
        "elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository",
        "elspeth.core.landscape.run_lifecycle_repository.RunLifecycleRepository",
        "elspeth.core.landscape.run_coordination_repository.record_coordination_event",
    }
)


def _deadline_dependency_mutation_violations(units, protected_names: frozenset[str]) -> tuple[str, ...]:
    return _deadline_dependency_mutation_violations_for_units(tuple(units), protected_names)


@cache
def _deadline_dependency_mutation_violations_for_units(units: tuple[SourceUnit, ...], protected_names: frozenset[str]) -> tuple[str, ...]:
    """Reject writes, reflection or opaque escapes of reviewed dependencies.

    Names may be exact callable/class identities or complete audited module
    roots. Passing the four registry module roots binds their variables and
    transitive callable objects as well as the public registration function.
    Source implementation equality is a separate prerequisite; this helper
    checks the all-production-module mutation edges that equality cannot see.
    """

    def protected(qualified):
        return qualified is not None and any(qualified == name or qualified.startswith(name + ".") for name in protected_names)

    def namespace(qualified):
        # A package or class dictionary can replace a protected descendant.
        return qualified is not None and any(qualified == name or name.startswith(qualified + ".") for name in protected_names)

    reviewed_callables = {
        unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + _symbol(node)
        for unit in units
        if unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".") in protected_names
        for node in ast.walk(unit.tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    problems = set()
    for unit in units:
        resolver = _resolver_for_unit(unit)
        module = unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
        reviewed_module = module in protected_names
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                qualified = resolver.qualified_name(node, use=node)
                if protected(qualified):
                    problems.add(f"deadline-dependency attribute replacement: {unit.path}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "__dict__",
                "__code__",
                "__defaults__",
                "__kwdefaults__",
                "__globals__",
                "__closure__",
            }:
                receiver = resolver.qualified_name(node.value, use=node)
                if protected(receiver) or namespace(receiver):
                    problems.add(f"deadline-dependency mutable metadata exposure: {unit.path}:{node.lineno}")
            if not isinstance(node, ast.Call):
                # Container/conditional laundering makes the receiver's later
                # identity opaque. Current reviewed external callers need none.
                parent = getattr(node, "_landscape_parent", None)
                exception_types = isinstance(parent, ast.ExceptHandler) and parent.type is node
                if not reviewed_module and not exception_types and isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple, ast.IfExp)):
                    pending = list(ast.iter_child_nodes(node))
                    while pending:
                        child = pending.pop()
                        child_name = resolver.qualified_name(child, use=child) if isinstance(child, (ast.Name, ast.Attribute)) else None
                        if namespace(child_name) or child_name in reviewed_callables:
                            problems.add(f"deadline-dependency container/choice escape: {unit.path}:{node.lineno}")
                            break
                        if isinstance(child, (ast.Dict, ast.List, ast.Set, ast.Tuple, ast.IfExp)):
                            pending.extend(ast.iter_child_nodes(child))
                continue
            method = _resolved_callable_name(node.func, resolver, use=node)
            function = resolver.qualified_name(node.func, use=node)
            if method in {"getattr", "setattr", "delattr", "__setattr__", "__delattr__"}:
                pairs = []
                if len(node.args) >= 2:
                    pairs.append((node.args[0], node.args[1]))
                if method in {"__setattr__", "__delattr__"} and isinstance(node.func, ast.Attribute) and node.args:
                    pairs.append((node.func.value, node.args[0]))
                for receiver, attribute_node in pairs:
                    receiver_name = resolver.qualified_name(receiver, use=node)
                    attribute = _constant_string_value(attribute_node, resolver, use=node)
                    target = None if receiver_name is None or attribute is None else receiver_name + "." + attribute
                    if protected(target) or (attribute is None and (protected(receiver_name) or namespace(receiver_name))):
                        problems.add(f"deadline-dependency reflected access/replacement: {unit.path}:{node.lineno}")
            if method == "vars" and node.args:
                receiver = resolver.qualified_name(node.args[0], use=node)
                if protected(receiver) or namespace(receiver):
                    problems.add(f"deadline-dependency namespace dictionary exposure: {unit.path}:{node.lineno}")
            if reviewed_module or function in reviewed_callables:
                # These exact module bodies/callees are separately verified by
                # the registry implementation predicate. Do not infer that an
                # unreviewed callback is safe merely because it is source-owned.
                continue
            for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                argument_name = resolver.qualified_name(argument, use=node)
                if protected(argument_name) or namespace(argument_name):
                    problems.add(f"deadline-dependency opaque callback escape: {unit.path}:{node.lineno}")
    return tuple(sorted(problems))


_COORDINATION_FINALIZATION_RECIPES = {
    ("src/elspeth/core/landscape/run_coordination_repository.py", "record_coordination_event"): """
def record_coordination_event(conn: Connection, *, run_id: str, event_type: str, worker_id: str, leader_epoch: int | None, recorded_at: datetime, context: Mapping[str, object] | None=None) -> None:
    context_json = canonical_json({} if context is None else dict(context))
    conn.execute(insert(run_coordination_events_table).values(event_id=_coordination_event_id(run_id=run_id, event_type=event_type, worker_id=worker_id, leader_epoch=leader_epoch, recorded_at=recorded_at, context_json=context_json), run_id=run_id, event_type=event_type, worker_id=worker_id, leader_epoch=leader_epoch, recorded_at=recorded_at, context_json=context_json))
""",
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.register_run_leader_on"): """
def register_run_leader_on(self, conn: Connection, *, run_id: str, worker_id: str, window_seconds: float, entry_point: str='run') -> CoordinationToken:
    database_now = read_landscape_decision_time(conn)
    expires = database_now + timedelta(seconds=window_seconds)
    conn.execute(insert(run_coordination_table).values(run_id=run_id, leader_worker_id=worker_id, leader_epoch=1, leader_heartbeat_expires_at=expires, updated_at=database_now))
    token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=1)
    record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)
    self._insert_worker_row(conn, run_id=run_id, worker_id=worker_id, role='leader', window_seconds=window_seconds, entry_point=entry_point, database_now=database_now)
    record_coordination_event(conn, run_id=run_id, event_type='worker_register', worker_id=worker_id, leader_epoch=1, recorded_at=database_now, context={'role': 'leader', 'entry_point': entry_point})
    record_coordination_event(conn, run_id=run_id, event_type='leader_acquire', worker_id=worker_id, leader_epoch=1, recorded_at=database_now, context={'entry_point': entry_point})
    return token
""",
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_run_leadership"): """
def acquire_run_leadership(self, *, run_id: str, worker_id: str, window_seconds: float, entry_point: str='resume') -> CoordinationToken:
    try:
        with begin_write(self._engine) as conn:
            token = self._acquire_run_leadership_on(conn, run_id=run_id, worker_id=worker_id, window_seconds=window_seconds, entry_point=entry_point)
            self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)
        return token
    except OperationalError as exc:
        if not _is_database_locked(exc):
            raise
        raise WriteLockHeldError(run_id=run_id, workers=self._read_registered_workers(run_id)) from exc
""",
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository._acquire_run_leadership_on"): """
def _acquire_run_leadership_on(self, conn: Connection, *, run_id: str, worker_id: str, window_seconds: float, entry_point: str) -> CoordinationToken:
    seat = conn.execute(select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_epoch, run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run_id).with_for_update()).one_or_none()
    if seat is None:
        raise AuditIntegrityError(f'Run {run_id!r} has no run_coordination seat row; at schema epoch 21 begin_run creates it atomically with the run. The audit DB is corrupt or was written by incompatible code.')
    database_now = read_landscape_decision_time(conn)
    prior_worker: str | None = seat.leader_worker_id
    expires = database_now + timedelta(seconds=window_seconds)
    run_status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one_or_none()
    if run_status in _IMMUTABLE_SUCCESS_RUN_STATUSES:
        status_enum = RunStatus(run_status)
        raise AuditIntegrityError(f"Cannot acquire run leadership: cannot transition run {run_id} from {status_enum.name} ({status_enum.value!r}) to 'running'. Successful terminal runs are immutable. FAILED/INTERRUPTED runs can be resumed via seat takeover.")
    cas = conn.execute(update(run_coordination_table).where(run_coordination_table.c.run_id == run_id, run_coordination_table.c.leader_worker_id.is_(None) | (run_coordination_table.c.leader_heartbeat_expires_at < database_now)).values(leader_worker_id=worker_id, leader_epoch=run_coordination_table.c.leader_epoch + 1, leader_heartbeat_expires_at=expires, updated_at=database_now))
    if cas.rowcount != 1:
        from elspeth.core.checkpoint.recovery import NonResumableRunError
        held_expiry = seat.leader_heartbeat_expires_at
        expiry_text = 'unknown' if held_expiry is None else _utc(held_expiry).isoformat()
        raise NonResumableRunError(run_id, f'run leadership is held by {prior_worker!r} (seat expires {expiry_text})')
    new_epoch = int(seat.leader_epoch) + 1
    token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=new_epoch)
    record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)
    conn.execute(update(runs_table).where(runs_table.c.run_id == run_id, runs_table.c.status.in_((*_TAKEOVER_FLIPPABLE_RUN_STATUSES, RunStatus.RUNNING.value))).values(status=RunStatus.RUNNING.value, completed_at=None, reproducibility_grade=None))
    if prior_worker is not None and prior_worker != worker_id:
        evicted = conn.execute(update(run_workers_table).where(run_workers_table.c.worker_id == prior_worker, run_workers_table.c.status == 'active').values(status='evicted', evicted_at=database_now, evicted_by_worker_id=worker_id))
        if evicted.rowcount == 1:
            record_coordination_event(conn, run_id=run_id, event_type='worker_evict', worker_id=prior_worker, leader_epoch=new_epoch, recorded_at=database_now, context={'evicted_by_worker_id': worker_id, 'reason': 'deposed_leader_takeover'})
    self._insert_worker_row(conn, run_id=run_id, worker_id=worker_id, role='leader', window_seconds=window_seconds, entry_point=entry_point, database_now=database_now)
    record_coordination_event(conn, run_id=run_id, event_type='worker_register', worker_id=worker_id, leader_epoch=new_epoch, recorded_at=database_now, context={'role': 'leader', 'entry_point': entry_point})
    record_coordination_event(conn, run_id=run_id, event_type='leader_acquire', worker_id=worker_id, leader_epoch=new_epoch, recorded_at=database_now, context={'entry_point': entry_point, 'deposed_leader_worker_id': prior_worker})
    return token
""",
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_export_leadership"): """
def acquire_export_leadership(self, *, run_id: str, worker_id: str, window_seconds: float) -> CoordinationToken:
    try:
        with begin_write(self._engine) as conn:
            token = self._acquire_export_leadership_on(conn, run_id=run_id, worker_id=worker_id, window_seconds=window_seconds)
            self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)
        return token
    except OperationalError as exc:
        if not _is_database_locked(exc):
            raise
        raise WriteLockHeldError(run_id=run_id, workers=self._read_registered_workers(run_id)) from exc
""",
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository._acquire_export_leadership_on"): """
def _acquire_export_leadership_on(self, conn: Connection, *, run_id: str, worker_id: str, window_seconds: float) -> CoordinationToken:
    seat = conn.execute(select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_epoch, run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run_id).with_for_update()).one_or_none()
    if seat is None:
        raise AuditIntegrityError(f'Run {run_id!r} has no run_coordination seat row; at schema epoch 21 begin_run creates it atomically with the run. The audit DB is corrupt or was written by incompatible code.')
    database_now = read_landscape_decision_time(conn)
    run_status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one_or_none()
    if run_status == RunStatus.RUNNING.value:
        from elspeth.core.checkpoint.recovery import NonResumableRunError
        raise NonResumableRunError(run_id, 'run is not terminal; its running leader owns finalization')
    if run_status not in _EXPORT_SEAT_RUN_STATUSES:
        raise AuditIntegrityError(f"Cannot acquire export leadership: run {run_id} is {run_status!r}, not terminal. An audit export is re-driven only for a finalized run; a RUNNING run is its leader's.")
    prior_worker: str | None = seat.leader_worker_id
    expires = database_now + timedelta(seconds=window_seconds)
    cas = conn.execute(update(run_coordination_table).where(run_coordination_table.c.run_id == run_id, run_coordination_table.c.leader_worker_id.is_(None) | (run_coordination_table.c.leader_heartbeat_expires_at < database_now)).values(leader_worker_id=worker_id, leader_epoch=run_coordination_table.c.leader_epoch + 1, leader_heartbeat_expires_at=expires, updated_at=database_now))
    if cas.rowcount != 1:
        from elspeth.core.checkpoint.recovery import NonResumableRunError
        held_expiry = seat.leader_heartbeat_expires_at
        expiry_text = 'unknown' if held_expiry is None else _utc(held_expiry).isoformat()
        raise NonResumableRunError(run_id, f'run leadership is held by {prior_worker!r} (seat expires {expiry_text})')
    new_epoch = int(seat.leader_epoch) + 1
    token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=new_epoch)
    record_issued_deadline(conn, key=_leader_deadline_key(token), expires_at=expires, window_seconds=window_seconds)
    if prior_worker is not None and prior_worker != worker_id:
        evicted = conn.execute(update(run_workers_table).where(run_workers_table.c.worker_id == prior_worker, run_workers_table.c.status == 'active').values(status='evicted', evicted_at=database_now, evicted_by_worker_id=worker_id))
        if evicted.rowcount == 1:
            record_coordination_event(conn, run_id=run_id, event_type='worker_evict', worker_id=prior_worker, leader_epoch=new_epoch, recorded_at=database_now, context={'evicted_by_worker_id': worker_id, 'reason': 'deposed_leader_export_takeover'})
    self._insert_worker_row(conn, run_id=run_id, worker_id=worker_id, role='leader', window_seconds=window_seconds, entry_point='export', database_now=database_now)
    record_coordination_event(conn, run_id=run_id, event_type='worker_register', worker_id=worker_id, leader_epoch=new_epoch, recorded_at=database_now, context={'role': 'leader', 'entry_point': 'export'})
    record_coordination_event(conn, run_id=run_id, event_type='leader_acquire', worker_id=worker_id, leader_epoch=new_epoch, recorded_at=database_now, context={'entry_point': 'export', 'deposed_leader_worker_id': prior_worker})
    return token
""",
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.admit_follower"): """
def admit_follower(self, *, run_id: str, worker_id: str, config_hash: str, window_seconds: float) -> WorkerMembershipToken:
    with begin_write(self._engine) as conn:
        conn.execute(select(run_coordination_table.c.run_id).where(run_coordination_table.c.run_id == run_id).with_for_update()).one_or_none()
        database_now = read_landscape_decision_time(conn)
        run = conn.execute(select(runs_table.c.status, runs_table.c.config_hash).where(runs_table.c.run_id == run_id)).one_or_none()
        if run is None:
            raise JoinRefusedError(run_id, 'run not found')
        if run.status != RunStatus.RUNNING.value:
            raise JoinRefusedError(run_id, f'run status is {run.status!r} — ' + ('a terminal run cannot be joined' if run.status == RunStatus.COMPLETED.value else 'use `elspeth resume`'))
        if run.config_hash != config_hash:
            raise JoinRefusedError(run_id, f"resolved settings hash {config_hash!r} does not match the run's config_hash {run.config_hash!r}; a joiner must run the identical pipeline")
        seat = conn.execute(select(run_coordination_table.c.leader_worker_id, (run_coordination_table.c.leader_heartbeat_expires_at >= database_now).label('seat_live')).where(run_coordination_table.c.run_id == run_id)).one_or_none()
        seat_live = seat is not None and seat.leader_worker_id is not None and bool(seat.seat_live)
        if not seat_live:
            raise JoinRefusedError(run_id, 'no live leader — use `elspeth resume` to take the seat')
        self._insert_worker_row(conn, run_id=run_id, worker_id=worker_id, role='follower', window_seconds=window_seconds, entry_point='join', database_now=database_now)
        record_coordination_event(conn, run_id=run_id, event_type='worker_register', worker_id=worker_id, leader_epoch=None, recorded_at=database_now, context={'role': 'follower', 'entry_point': 'join'})
        self._finalize_follower_admission_on(conn, run_id=run_id, worker_id=worker_id, window_seconds=window_seconds)
    return WorkerMembershipToken(run_id=run_id, worker_id=worker_id)
""",
    ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository._coordination_repo"): """
@property
def _coordination_repo(self) -> RunCoordinationRepository:
    if self._run_coordination is None:
        self._run_coordination = RunCoordinationRepository(self._db.engine)
    return self._run_coordination
""",
    ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.begin_run"): """
def begin_run(self, config: Mapping[str, Any], canonical_version: str, *, run_id: str | None=None, reproducibility_grade: ReproducibilityGrade | None=None, status: RunStatus=RunStatus.RUNNING, source_schema_json: str | None=None, initiated_by_user_id: str | None=None, auth_provider_type: str | None=None, openrouter_catalog_sha256: str, openrouter_catalog_source: str, leader_worker_id: str | None=None, web_plugin_policy_evidence: WebPluginPolicyEvidence | None=None) -> Run:
    if status == RunStatus.COMPLETED:
        raise AuditIntegrityError('begin_run() cannot create a COMPLETED run. Use complete_run() so completed_at is recorded in the audit trail.')
    validate_run_attribution(initiated_by_user_id=initiated_by_user_id, auth_provider_type=auth_provider_type)
    _validate_openrouter_catalog_snapshot(sha256=openrouter_catalog_sha256, source=openrouter_catalog_source)
    if web_plugin_policy_evidence is not None and (not isinstance(web_plugin_policy_evidence, WebPluginPolicyEvidence)):
        raise AuditIntegrityError('web_plugin_policy_evidence must be a WebPluginPolicyEvidence value')
    run_id = run_id or generate_id()
    settings_json = canonical_json(config)
    config_hash = stable_hash(config)
    timestamp = now()
    runtime_val_manifest_json = _frozen_runtime_val_manifest_json()
    run = Run(run_id=run_id, started_at=timestamp, config_hash=config_hash, settings_json=settings_json, canonical_version=canonical_version, status=status, reproducibility_grade=reproducibility_grade)
    worker_id = leader_worker_id or mint_worker_id(run.run_id)
    coordination = self._coordination_repo
    try:
        with self._db.write_connection() as conn:
            conn.execute(runs_table.insert().values(run_id=run.run_id, started_at=run.started_at, config_hash=run.config_hash, settings_json=run.settings_json, canonical_version=run.canonical_version, status=run.status.value, reproducibility_grade=run.reproducibility_grade, source_schema_json=source_schema_json, runtime_val_manifest_json=runtime_val_manifest_json, llm_call_count=None, seeded_from_cache=False, cache_key=None, openrouter_catalog_sha256=openrouter_catalog_sha256, openrouter_catalog_source=openrouter_catalog_source))
            if initiated_by_user_id is not None and auth_provider_type is not None:
                conn.execute(run_attributions_table.insert().values(run_id=run.run_id, recorded_at=timestamp, initiated_by_user_id=initiated_by_user_id, auth_provider_type=auth_provider_type))
            if web_plugin_policy_evidence is not None:
                self._insert_web_plugin_policy_evidence(conn, run_id=run.run_id, evidence=web_plugin_policy_evidence)
            leader_token = coordination.register_run_leader_on(conn, run_id=run.run_id, worker_id=worker_id, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, entry_point='run')
            coordination._finalize_leader_registration_on(conn, token=leader_token, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS)
    except SQLAlchemyError as exc:
        raise LandscapeRecordError(f'begin_run — database rejected audit write: {type(exc).__name__}: {exc}') from exc
    return run
""",
}

_COORDINATION_FINALIZATION_DEPENDENCIES = {
    ("src/elspeth/core/landscape/run_coordination_repository.py", "record_coordination_event"): {
        "Connection": "sqlalchemy.engine.Connection",
        "datetime": "datetime.datetime",
        "canonical_json": "elspeth.core.canonical.canonical_json",
        "insert": "sqlalchemy.insert",
        "run_coordination_events_table": "elspeth.core.landscape.schema.run_coordination_events_table",
        "_coordination_event_id": "elspeth.core.landscape.run_coordination_repository._coordination_event_id",
    },
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.register_run_leader_on"): {
        "CoordinationToken": "elspeth.contracts.coordination.CoordinationToken",
        "Connection": "sqlalchemy.engine.Connection",
        "read_landscape_decision_time": "elspeth.core.landscape.database_clock.read_landscape_decision_time",
        "record_issued_deadline": "elspeth.core.landscape.lease_deadlines.record_issued_deadline",
        "record_coordination_event": "elspeth.core.landscape.run_coordination_repository.record_coordination_event",
        "timedelta": "datetime.timedelta",
        "_leader_deadline_key": "elspeth.core.landscape.run_coordination_repository._leader_deadline_key",
        "insert": "sqlalchemy.insert",
        "run_coordination_table": "elspeth.core.landscape.schema.run_coordination_table",
    },
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_run_leadership"): {
        "CoordinationToken": "elspeth.contracts.coordination.CoordinationToken",
        "OperationalError": "sqlalchemy.exc.OperationalError",
        "begin_write": "elspeth.core.landscape.database.begin_write",
        "WriteLockHeldError": "elspeth.contracts.errors.WriteLockHeldError",
        "_is_database_locked": "elspeth.core.landscape.run_coordination_repository._is_database_locked",
    },
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository._acquire_run_leadership_on"): {
        "CoordinationToken": "elspeth.contracts.coordination.CoordinationToken",
        "Connection": "sqlalchemy.engine.Connection",
        "read_landscape_decision_time": "elspeth.core.landscape.database_clock.read_landscape_decision_time",
        "record_issued_deadline": "elspeth.core.landscape.lease_deadlines.record_issued_deadline",
        "record_coordination_event": "elspeth.core.landscape.run_coordination_repository.record_coordination_event",
        "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
        "timedelta": "datetime.timedelta",
        "RunStatus": "elspeth.contracts.enums.RunStatus",
        "NonResumableRunError": "elspeth.core.checkpoint.recovery.NonResumableRunError",
        "_leader_deadline_key": "elspeth.core.landscape.run_coordination_repository._leader_deadline_key",
        "_utc": "elspeth.core.landscape.run_coordination_repository._utc",
        "select": "sqlalchemy.select",
        "update": "sqlalchemy.update",
        "run_coordination_table": "elspeth.core.landscape.schema.run_coordination_table",
        "runs_table": "elspeth.core.landscape.schema.runs_table",
        "run_workers_table": "elspeth.core.landscape.schema.run_workers_table",
    },
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_export_leadership"): {
        "CoordinationToken": "elspeth.contracts.coordination.CoordinationToken",
        "OperationalError": "sqlalchemy.exc.OperationalError",
        "begin_write": "elspeth.core.landscape.database.begin_write",
        "WriteLockHeldError": "elspeth.contracts.errors.WriteLockHeldError",
        "_is_database_locked": "elspeth.core.landscape.run_coordination_repository._is_database_locked",
    },
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository._acquire_export_leadership_on"): {
        "CoordinationToken": "elspeth.contracts.coordination.CoordinationToken",
        "Connection": "sqlalchemy.engine.Connection",
        "read_landscape_decision_time": "elspeth.core.landscape.database_clock.read_landscape_decision_time",
        "record_issued_deadline": "elspeth.core.landscape.lease_deadlines.record_issued_deadline",
        "record_coordination_event": "elspeth.core.landscape.run_coordination_repository.record_coordination_event",
        "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
        "NonResumableRunError": "elspeth.core.checkpoint.recovery.NonResumableRunError",
        "timedelta": "datetime.timedelta",
        "RunStatus": "elspeth.contracts.enums.RunStatus",
        "_leader_deadline_key": "elspeth.core.landscape.run_coordination_repository._leader_deadline_key",
        "_utc": "elspeth.core.landscape.run_coordination_repository._utc",
        "select": "sqlalchemy.select",
        "update": "sqlalchemy.update",
        "run_coordination_table": "elspeth.core.landscape.schema.run_coordination_table",
        "runs_table": "elspeth.core.landscape.schema.runs_table",
        "run_workers_table": "elspeth.core.landscape.schema.run_workers_table",
    },
    ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.admit_follower"): {
        "WorkerMembershipToken": "elspeth.contracts.coordination.WorkerMembershipToken",
        "begin_write": "elspeth.core.landscape.database.begin_write",
        "read_landscape_decision_time": "elspeth.core.landscape.database_clock.read_landscape_decision_time",
        "record_coordination_event": "elspeth.core.landscape.run_coordination_repository.record_coordination_event",
        "JoinRefusedError": "elspeth.contracts.errors.JoinRefusedError",
        "RunStatus": "elspeth.contracts.enums.RunStatus",
        "select": "sqlalchemy.select",
        "runs_table": "elspeth.core.landscape.schema.runs_table",
        "run_coordination_table": "elspeth.core.landscape.schema.run_coordination_table",
    },
    ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository._coordination_repo"): {
        "RunCoordinationRepository": "elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository"
    },
    ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.begin_run"): {
        "Run": "elspeth.contracts.Run",
        "RunStatus": "elspeth.contracts.RunStatus",
        "validate_run_attribution": "elspeth.core.landscape.run_lifecycle_repository.validate_run_attribution",
        "_validate_openrouter_catalog_snapshot": "elspeth.core.landscape.run_lifecycle_repository._validate_openrouter_catalog_snapshot",
        "canonical_json": "elspeth.core.canonical.canonical_json",
        "stable_hash": "elspeth.core.canonical.stable_hash",
        "now": "elspeth.core.landscape._helpers.now",
        "_frozen_runtime_val_manifest_json": "elspeth.core.landscape.run_lifecycle_repository._frozen_runtime_val_manifest_json",
        "SQLAlchemyError": "sqlalchemy.exc.SQLAlchemyError",
        "ReproducibilityGrade": "elspeth.contracts.ReproducibilityGrade",
        "WebPluginPolicyEvidence": "elspeth.contracts.plugin_policy_audit.WebPluginPolicyEvidence",
        "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
        "generate_id": "elspeth.core.ids.generate_id",
        "mint_worker_id": "elspeth.contracts.coordination.mint_worker_id",
        "LandscapeRecordError": "elspeth.core.landscape.errors.LandscapeRecordError",
        "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS": "elspeth.contracts.coordination.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS",
        "runs_table": "elspeth.core.landscape.schema.runs_table",
        "run_attributions_table": "elspeth.core.landscape.schema.run_attributions_table",
    },
}

_COORDINATION_FINALIZATION_RECIPES[("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.__init__")] = """
def __init__(self, db: LandscapeDB, ops: DatabaseOps, run_loader: RunLoader) -> None:
    self._db = db
    self._ops = ops
    self._run_loader = run_loader
    self._run_coordination: RunCoordinationRepository | None = None
    self._token_outcomes: TokenOutcomeRepository | None = None
    self._operation_repository: OperationRepository | None = None
"""
_COORDINATION_FINALIZATION_DEPENDENCIES[("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.__init__")] = {}

_COORDINATION_FINALIZATION_CALLERS = {
    "_finalize_leader_registration_on": {
        ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.begin_run"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_run_leadership"),
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_export_leadership"),
    },
    "_finalize_follower_admission_on": {
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.admit_follower"),
    },
    "register_run_leader_on": {
        ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.begin_run"),
    },
    "_acquire_run_leadership_on": {
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_run_leadership"),
    },
    "_acquire_export_leadership_on": {
        ("src/elspeth/core/landscape/run_coordination_repository.py", "RunCoordinationRepository.acquire_export_leadership"),
    },
}


def _finalization_recipe_dump(function):
    # Comments/initial documentation are not authority behavior. Everything
    # executable, including defaults/decorators/returns, remains in the recipe.
    value = ast.parse(ast.unparse(function)).body[0]
    if (
        value.body
        and isinstance(value.body[0], ast.Expr)
        and isinstance(value.body[0].value, ast.Constant)
        and isinstance(value.body[0].value.value, str)
    ):
        value.body.pop(0)
    return stable_ast_dump(value)


def _finalization_begin_run_receiver_is_owned(call, resolver, proof, functions):
    # The generic resolver does not currently model this lazy property. Prove
    # the exact visible creation/None-initialization graph, without granting an
    # arbitrary receiver merely because its local variable is 'coordination'.
    lifecycle_path = "src/elspeth/core/landscape/run_lifecycle_repository.py"
    if resolver.unit.path != lifecycle_path or _symbol(call) != "RunLifecycleRepository.begin_run":
        return False
    for symbol in ("RunLifecycleRepository.begin_run", "RunLifecycleRepository._coordination_repo", "RunLifecycleRepository.__init__"):
        key = (lifecycle_path, symbol)
        found = functions.get(key, ())
        if len(found) != 1 or _finalization_recipe_dump(found[0][0]) != _finalization_recipe_dump(
            ast.parse(_COORDINATION_FINALIZATION_RECIPES[key]).body[0]
        ):
            return False
    owner = proof.class_for("elspeth.core.landscape.run_lifecycle_repository.RunLifecycleRepository")
    target = proof.class_for("elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository")
    if owner is None or target is None or owner[0].bases or owner[0].decorator_list or target[0].bases or target[0].decorator_list:
        return False
    if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name) or call.func.value.id != "coordination":
        return False
    if not _receiver_class_body_is_visible(proof, *owner) or not _receiver_class_body_is_visible(proof, *target):
        return False
    if not _receiver_method_is_unmodified(proof, *owner, "_coordination_repo", call) or not _receiver_method_is_unmodified(
        proof, *target, call.func.attr, call
    ):
        return False
    allowed_stores = {
        (lifecycle_path, "RunLifecycleRepository.__init__"),
        (lifecycle_path, "RunLifecycleRepository._coordination_repo"),
    }
    fields = {"_run_coordination", "_coordination_repo"}
    for unit in proof.units:
        unit_resolver = _resolver_for_unit(unit)
        for part in ast.walk(unit.tree):
            if isinstance(part, ast.Attribute) and part.attr in fields and isinstance(part.ctx, (ast.Store, ast.Del)):
                # A same-spelled field on a distinct, unrelated nominal class
                # is not a mutation of RunLifecycleRepository's lazy receiver.
                function = _owner_function(part)
                declaring = next((ancestor for ancestor in _ancestors(part) if isinstance(ancestor, ast.ClassDef)), None)
                parameters = () if function is None else (*function.args.posonlyargs, *function.args.args)
                unrelated_self = (
                    declaring is not None
                    and declaring is not owner[0]
                    and not declaring.bases
                    and not declaring.keywords
                    and not declaring.decorator_list
                    and function is not None
                    and function.name == "__init__"
                    and not function.decorator_list
                    and parameters
                    and isinstance(part.value, ast.Name)
                    and part.value.id == parameters[0].arg
                    and not any(
                        isinstance(binding, ast.Name) and binding.id == parameters[0].arg and isinstance(binding.ctx, (ast.Store, ast.Del))
                        for binding in _walk_same_scope(function)
                    )
                )
                if unrelated_self:
                    continue
                if part.attr == "_coordination_repo" or (unit.path, _symbol(part)) not in allowed_stores:
                    return False
            if (
                isinstance(part, ast.Subscript)
                and isinstance(part.ctx, (ast.Store, ast.Del))
                and isinstance(part.value, ast.Attribute)
                and part.value.attr == "__dict__"
                and _constant_string_value(part.slice, unit_resolver, use=part) in fields
            ):
                return False
            if (
                isinstance(part, ast.Call)
                and _call_name(part) in {"setattr", "delattr", "__setattr__", "__delattr__"}
                and len(part.args) >= 2
                and any(_constant_string_value(argument, unit_resolver, use=part) in fields for argument in part.args[:2])
            ):
                return False
    return True


def _deadline_finalization_caller_violations(units) -> tuple[str, ...]:
    return _deadline_finalization_caller_violations_for_units(tuple(units))


def _finalization_graph_is_present(units: tuple[SourceUnit, ...]) -> bool:
    """Require the closed graph for admission capabilities, not a module path.

    Evidence-only snippets may share the real module path. They receive no
    deadline-writer exemption: that grant separately requires every recipe.
    Definitions keep missing-call mutations relevant even after call removal.
    """
    protected = set(_COORDINATION_FINALIZATION_CALLERS)
    callers = set().union(*_COORDINATION_FINALIZATION_CALLERS.values())
    classes = {
        "elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository",
        "elspeth.core.landscape.run_lifecycle_repository.RunLifecycleRepository",
    }
    proof = _AuthorityProof(units)
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in protected or (unit.path, _symbol(node)) in callers
            ):
                return True
            if isinstance(node, ast.Attribute) and node.attr in protected:
                return True
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            if _resolved_callable_name(node.func, resolver, use=node) not in {
                "getattr",
                "setattr",
                "delattr",
                "__setattr__",
                "__delattr__",
            }:
                continue
            if any(_constant_string_value(arg, resolver, use=node) in protected for arg in node.args[:2]):
                return True
            receiver = proof.receiver(node.args[0], resolver, node)
            if resolver.qualified_name(node.args[0], use=node) in classes or (
                receiver is not None
                and receiver[1].unit.path == "src/elspeth/core/landscape/run_coordination_repository.py"
                and receiver[0].name == "RunCoordinationRepository"
            ):
                return True
    return False


@cache
def _deadline_finalization_caller_violations_for_units(units: tuple[SourceUnit, ...]) -> tuple[str, ...]:
    """Prove the closed admission graph before granting inherited row locks.

    Paired finalization follows one initial seat INSERT or a locked-seat CAS
    and one new worker INSERT, on the original connection. Each owning body
    finishes with that exact returned token's finalizer. Follower admission
    takes its seat lock before inserting membership and finalizes at its tail.

    These auditable AST recipes deliberately reject new compositions until
    reviewed. The separate clock and registry predicates remain required by
    _proven_coordination_deadline_writers before any generic-gate delegation.
    """
    units = tuple(units)
    coordination_path = "src/elspeth/core/landscape/run_coordination_repository.py"
    protected = set(_COORDINATION_FINALIZATION_CALLERS)
    relevant = _finalization_graph_is_present(units)
    if not relevant:
        return ()
    problems = []
    proof = _AuthorityProof(units)
    functions = {}
    for unit in units:
        for node in ast.walk(unit.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault((unit.path, _symbol(node)), []).append((node, _resolver_for_unit(unit)))
    for key, recipe in _COORDINATION_FINALIZATION_RECIPES.items():
        found = functions.get(key, ())
        if len(found) != 1 or isinstance(found[0][0], ast.AsyncFunctionDef):
            problems.append(f"deadline-finalization missing/ambiguous recipe owner: {key}")
            continue
        function, resolver = found[0]
        expected = ast.parse(textwrap.dedent(recipe)).body[0]
        if _finalization_recipe_dump(function) != _finalization_recipe_dump(expected):
            problems.append(f"deadline-finalization caller/admission recipe changed: {key}")
        for name, qualified in _COORDINATION_FINALIZATION_DEPENDENCIES[key].items():
            references = [
                part for part in ast.walk(function) if isinstance(part, ast.Name) and part.id == name and isinstance(part.ctx, ast.Load)
            ]
            if not references or any(
                resolver.qualified_name(part, use=part) != qualified or not _deadline_api_is_visible(qualified, resolver, part)
                for part in references
            ):
                problems.append(f"deadline-finalization dependency changed: {key}:{name}")
        for name in {"int", "bool", "dict", "str", "property"}:
            references = [
                part for part in ast.walk(function) if isinstance(part, ast.Name) and part.id == name and isinstance(part.ctx, ast.Load)
            ]
            if any(not _deadline_lock_builtin(part, name, resolver, part) for part in references):
                problems.append(f"deadline-finalization builtin rebound: {key}:{name}")
    observed = {name: set() for name in protected}
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if not isinstance(node, ast.Attribute) or node.attr not in protected:
                continue
            parent = getattr(node, "_landscape_parent", None)
            # Closed direct-call set: extraction into an alias, reflection,
            # or a forwarding helper is an unproved additional authority edge.
            if not isinstance(parent, ast.Call) or parent.func is not node:
                problems.append(f"deadline-finalization indirect/altered reference: {unit.path}:{node.lineno}")
                continue
            owner = _owner_function(parent)
            key = (unit.path, _symbol(owner)) if owner is not None else (unit.path, "<module>")
            if key not in _COORDINATION_FINALIZATION_CALLERS[node.attr] or key in observed[node.attr]:
                problems.append(f"deadline-finalization extra/repeated caller: {unit.path}:{node.lineno}")
                continue
            observed[node.attr].add(key)
            target = proof.called_function(parent, resolver, parent)
            exact_target = target is not None and (target[1].unit.path, _symbol(target[0])) == (
                coordination_path,
                "RunCoordinationRepository." + node.attr,
            )
            if not exact_target and not _finalization_begin_run_receiver_is_owned(parent, resolver, proof, functions):
                problems.append(f"deadline-finalization unowned receiver: {unit.path}:{node.lineno}")
    # Also reject reflective references, which do not contain an Attribute
    # spelling but can expose either finalizer or its admission capability.
    protected_qualified = {
        path.removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + symbol
        for path, symbol in _COORDINATION_FINALIZATION_RECIPES
    } | {"elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository." + name for name in protected}
    protected_classes = {
        "elspeth.core.landscape.run_coordination_repository.RunCoordinationRepository",
        "elspeth.core.landscape.run_lifecycle_repository.RunLifecycleRepository",
    }
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.Attribute) and (
                isinstance(node.ctx, (ast.Store, ast.Del)) or node.attr in {"__dict__", "__code__", "__globals__"}
            ):
                qualified = resolver.qualified_name(node, use=node)
                receiver_qualified = resolver.qualified_name(node.value, use=node)
                if (
                    isinstance(node.ctx, (ast.Store, ast.Del))
                    and qualified is not None
                    and any(qualified == name or qualified.startswith(name + ".") for name in protected_qualified)
                ):
                    problems.append(f"deadline-finalization external callable mutation: {unit.path}:{node.lineno}")
                if node.attr in {"__dict__", "__code__", "__globals__"} and receiver_qualified in protected_classes | protected_qualified:
                    problems.append(f"deadline-finalization callable dictionary/code exposure: {unit.path}:{node.lineno}")
            callable_name = _resolved_callable_name(node.func, resolver, use=node) if isinstance(node, ast.Call) else None
            if (
                isinstance(node, ast.Call)
                and callable_name in {"getattr", "setattr", "delattr", "__setattr__", "__delattr__"}
                and len(node.args) >= 2
            ):
                attribute = _constant_string_value(node.args[1], resolver, use=node)
                receiver = proof.receiver(node.args[0], resolver, node)
                owned_receiver = (
                    receiver is not None and receiver[1].unit.path == coordination_path and receiver[0].name == "RunCoordinationRepository"
                )
                owned_class = resolver.qualified_name(node.args[0], use=node) in protected_classes
                reflected_protected = any(_constant_string_value(argument, resolver, use=node) in protected for argument in node.args[:2])
                if reflected_protected or (attribute is None and (owned_receiver or owned_class)):
                    problems.append(f"deadline-finalization reflected reference: {unit.path}:{node.lineno}")
    for name, expected in _COORDINATION_FINALIZATION_CALLERS.items():
        if observed[name] != expected:
            problems.append(f"deadline-finalization caller set incomplete: {name}")
    return tuple(sorted(set(problems)))


@cache
def _proven_coordination_deadline_writers(units: tuple[SourceUnit, ...]) -> frozenset[tuple[str, str]]:
    """Only the exact graph and implementation recipes bypass generic issuance.

    The registry binding independently pins the reviewed commit guard.
    Missing/changed graph, implementation or registry means no granted symbols.
    """
    units = tuple(units)
    sources = {unit.path: unit.source for unit in units}
    coordination_path = "src/elspeth/core/landscape/run_coordination_repository.py"
    source = sources.get(coordination_path)
    if source is None or _deadline_finalization_caller_violations(units):
        return frozenset()
    if _deadline_dependency_mutation_violations(units, _DEADLINE_REGISTRY_NAMESPACES | _DEADLINE_FINALIZATION_DEPENDENCIES):
        return frozenset()
    if not _leader_deadline_sources_are_proven(sources) or not _issued_deadline_registry_source_is_proven(sources):
        return frozenset()
    if (
        _worker_registration_contract_violations(source)
        or _worker_heartbeat_contract_violations(source)
        or _heartbeat_fence_implementation_violations(units)
    ):
        return frozenset()
    symbols = {
        "RunCoordinationRepository.register_run_leader_on",
        "RunCoordinationRepository._acquire_run_leadership_on",
        "RunCoordinationRepository._acquire_export_leadership_on",
        "RunCoordinationRepository.acquire_run_leadership",
        "RunCoordinationRepository.acquire_export_leadership",
        "RunCoordinationRepository.admit_follower",
        "RunCoordinationRepository._insert_worker_row",
        "RunCoordinationRepository._finalize_leader_registration_on",
        "RunCoordinationRepository._finalize_follower_admission_on",
        "RunCoordinationRepository.worker_heartbeat",
        "_renew_leader_deadline_on",
    }
    return frozenset(
        {(coordination_path, symbol) for symbol in symbols}
        | {
            ("src/elspeth/core/landscape/run_lifecycle_repository.py", "RunLifecycleRepository.begin_run"),
        }
    )


# Permanent controls: actual owned recipes are the baseline; mutations
# alter only the named AST/source seam and must never receive delegation.


@pytest.fixture(scope="module")
def finalization_source_units():
    return tuple(unit for unit in _production_units() if unit.path.startswith("src/elspeth/core/landscape/"))


def _finalization_mutate(units, path, old, new):
    result = []
    changed = False
    for unit in units:
        if unit.path == path:
            assert old in unit.source
            result.append(_parse_source(path, unit.source.replace(old, new, 1)))
            changed = True
        else:
            result.append(unit)
    assert changed
    return tuple(result)


def test_finalization_current_closed_caller_graph(finalization_source_units):
    findings = _deadline_finalization_caller_violations(finalization_source_units)
    assert findings == (), findings


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)",
            "self._finalize_leader_registration_on(other_conn, token=token, window_seconds=window_seconds)",
        ),
        (
            "self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)",
            "self._finalize_leader_registration_on(conn, token=foreign_token, window_seconds=window_seconds)",
        ),
        (
            "self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)",
            "self._finalize_leader_registration_on(conn, token=token, window_seconds=window_seconds)\n                conn.execute(after_finalization)",
        ),
        ("            return token\n        except OperationalError", "            return foreign_token\n        except OperationalError"),
        (
            "self._finalize_follower_admission_on(conn, run_id=run_id, worker_id=worker_id, window_seconds=window_seconds)",
            "self._finalize_follower_admission_on(conn, run_id=run_id, worker_id=foreign_worker, window_seconds=window_seconds)",
        ),
        (
            "return WorkerMembershipToken(run_id=run_id, worker_id=worker_id)",
            "return WorkerMembershipToken(run_id=run_id, worker_id=foreign_worker)",
        ),
        (
            ".with_for_update()\n        ).one_or_none()\n        if seat is None:",
            ".with_for_update(read=True)\n        ).one_or_none()\n        if seat is None:",
        ),
        (
            "token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=1)",
            "token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=9)",
        ),
        (
            "        return token\n\n    def acquire_run_leadership",
            "        conn.rollback()\n        return token\n\n    def acquire_run_leadership",
        ),
        (
            "    context_json = canonical_json({} if context is None else dict(context))",
            "    getattr(conn, 'rollback')()\n    context_json = canonical_json({} if context is None else dict(context))",
        ),
        (
            "from elspeth.core.landscape.database import Tier1Engine, begin_write, verify_sqlite_tier1_pragmas",
            "from foreign.database import Tier1Engine, begin_write, verify_sqlite_tier1_pragmas",
        ),
    ],
)
def test_finalization_refuses_coordination_graph_mutants(finalization_source_units, old, new):
    mutated = _finalization_mutate(finalization_source_units, "src/elspeth/core/landscape/run_coordination_repository.py", old, new)
    assert _deadline_finalization_caller_violations(mutated)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "coordination._finalize_leader_registration_on(conn, token=leader_token, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS)",
            "coordination._finalize_leader_registration_on(conn, token=leader_token, window_seconds=1)",
        ),
        (
            "                leader_token = coordination.register_run_leader_on(",
            "                coordination._finalize_leader_registration_on(conn, token=foreign_token, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS)\n                leader_token = coordination.register_run_leader_on(",
        ),
        ("with self._db.write_connection() as conn:", "with foreign_transaction() as conn:"),
        ("        return self._run_coordination", "        return foreign_repository"),
    ],
)
def test_finalization_refuses_begin_run_graph_mutants(finalization_source_units, old, new):
    mutated = _finalization_mutate(finalization_source_units, "src/elspeth/core/landscape/run_lifecycle_repository.py", old, new)
    assert _deadline_finalization_caller_violations(mutated)


@pytest.mark.parametrize(
    "extra",
    [
        "def extra(repo, conn, token):\n    repo._finalize_leader_registration_on(conn, token=token, window_seconds=60)\n",
        "def extra(repo, conn, token):\n    relay = repo._finalize_leader_registration_on\n    relay(conn, token=token, window_seconds=60)\n",
        "def extra(repo, conn, token):\n    getattr(repo, '_finalize_leader_registration_on')(conn, token=token, window_seconds=60)\n",
    ],
)
def test_finalization_refuses_extra_and_laundered_callers(finalization_source_units, extra):
    units = (*finalization_source_units, _parse_source("src/elspeth/extra_deadline_caller.py", extra))
    assert _deadline_finalization_caller_violations(units)


@pytest.mark.parametrize(
    "extra",
    [
        "def corrupt(obj, foreign):\n    obj.__dict__['_run_coordination'] = foreign\n",
        "def corrupt(obj, foreign):\n    object.__setattr__(obj, '_run_coordination', foreign)\n",
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\nRunCoordinationRepository._finalize_leader_registration_on = foreign\n",
        "def extra(repo, conn, token):\n    name = '_finalize_' + 'leader_registration_on'\n    getattr(repo, name)(conn, token=token, window_seconds=60)\n",
    ],
)
def test_finalization_refuses_property_and_reflective_mutations(finalization_source_units, extra):
    units = (*finalization_source_units, _parse_source("src/elspeth/finalization_receiver_mutation.py", extra))
    assert _deadline_finalization_caller_violations(units)


def test_finalization_current_writer_delegation_and_factory_returns(finalization_source_units):
    proven = _proven_coordination_deadline_writers(finalization_source_units)
    expected = {
        "RunCoordinationRepository.register_run_leader_on",
        "RunCoordinationRepository._acquire_run_leadership_on",
        "RunCoordinationRepository._acquire_export_leadership_on",
        "RunCoordinationRepository.acquire_run_leadership",
        "RunCoordinationRepository.acquire_export_leadership",
        "RunCoordinationRepository.admit_follower",
        "RunCoordinationRepository._insert_worker_row",
        "RunCoordinationRepository._finalize_leader_registration_on",
        "RunCoordinationRepository._finalize_follower_admission_on",
        "RunCoordinationRepository.worker_heartbeat",
        "_renew_leader_deadline_on",
        "RunLifecycleRepository.begin_run",
    }
    assert {symbol for _path, symbol in proven} == expected, proven
    proof = _AuthorityProof(finalization_source_units)
    for unit in finalization_source_units:
        if unit.path != "src/elspeth/core/landscape/run_coordination_repository.py":
            continue
        for function in ast.walk(unit.tree):
            if isinstance(function, ast.FunctionDef) and function.name in {
                "acquire_run_leadership",
                "acquire_export_leadership",
                "admit_follower",
            }:
                scope = _MEMBER_SCOPE if function.name == "admit_follower" else _LEADER_SCOPE
                assert _authority_factory_return_contract(proof, function, _resolver_for_unit(unit), scope)


def test_finalization_registry_mutation_removes_every_writer_grant(finalization_source_units):
    units = _finalization_mutate(
        finalization_source_units,
        "src/elspeth/core/landscape/lease_deadlines.py",
        "    install_deadline_guard(conn.engine)",
        "    conn.rollback()\n    install_deadline_guard(conn.engine)",
    )
    assert not _proven_coordination_deadline_writers(units)


@pytest.mark.parametrize(
    "extra",
    [
        "class Mutator:\n    @staticmethod\n    def corrupt(obj, foreign):\n        obj._run_coordination = foreign\n",
        "class Mutator:\n    def __init__(self, victim, foreign):\n        self = victim\n        self._run_coordination = foreign\n",
    ],
)
def test_finalization_refuses_foreign_parameter_masquerading_as_constructor_self(finalization_source_units, extra):
    units = (*finalization_source_units, _parse_source("src/elspeth/core/landscape/receiver_corruption.py", extra))
    assert _deadline_finalization_caller_violations(units)


def test_finalization_complete_production_graph_and_writer_grants():
    units = _production_units()
    findings = _deadline_finalization_caller_violations(units)
    assert not findings, findings
    assert len(_proven_coordination_deadline_writers(units)) == 12


@pytest.mark.parametrize(
    "extra",
    [
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\ntype.__setattr__(RunCoordinationRepository, '_finalize_leader_registration_on', foreign)\n",
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\nfrom builtins import setattr as write\nwrite(RunCoordinationRepository, '_finalize_leader_registration_on', foreign)\n",
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\nRunCoordinationRepository.__dict__['_finalize_leader_registration_on'].__code__ = foreign.__code__\n",
        "from elspeth.core.landscape.run_coordination_repository import record_coordination_event as record\nrecord.__code__ = foreign.__code__\n",
    ],
)
def test_finalization_refuses_external_dependency_code_replacement(finalization_source_units, extra):
    units = (*finalization_source_units, _parse_source("src/elspeth/core/landscape/dependency_mutation.py", extra))
    assert _deadline_finalization_caller_violations(units)


# Reviewed NULL-deadline mapping graph. Exact recipes intentionally fail closed on body drift.
_NULL_DEADLINE_RECIPES = {}

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/execution/sink_effect_reservation.py", "_effect_row_values")] = r"""
def _effect_row_values(conn: Connection, request: SinkEffectReservationRequest, identity: _EffectIdentity, *, stream_id: str | None, stream_sequence: int | None, predecessor_effect_id: str | None) -> dict[str, object]:
    timestamp = read_landscape_transaction_time(conn)
    primary_effect_ids = {member.primary_effect_id for member in identity.members if member.primary_effect_id is not None}
    common_primary_effect_id = next(iter(primary_effect_ids)) if len(primary_effect_ids) == 1 else None
    return {'effect_id': identity.effect_id, 'run_id': request.run_id, 'sink_node_id': request.sink_node_id, 'role': request.role.value, 'state': SinkEffectState.RESERVED.value, 'protocol_version': SINK_EFFECT_PROTOCOL_VERSION, 'input_kind': request.input_kind.value, 'required_member_ordinal': 0 if request.input_kind is SinkEffectInputKind.PIPELINE_MEMBERS else None, 'required_snapshot_slot': 0 if request.input_kind is SinkEffectInputKind.AUDIT_EXPORT_SNAPSHOT else None, 'config_hash': request.config_hash, 'membership_or_manifest_hash': identity.membership_or_manifest_hash, 'group_payload_hash': identity.group_payload_hash, 'artifact_id': identity.artifact_id, 'artifact_idempotency_key': identity.artifact_idempotency_key, 'target_json': _EMPTY_TARGET_JSON, 'inspection_mode': None, 'inspection_attempt_id': None, 'plan_json': None, 'plan_hash': None, 'descriptor_mode': None, 'expected_descriptor_hash': None, 'precondition_hash': None, 'prepared_at': None, 'lease_owner': None, 'generation': 0, 'lease_expires_at': None, 'lease_heartbeat_at': None, 'reconcile_kind': None, 'reconcile_evidence_hash': None, 'result_descriptor_hash': None, 'publication_performed': None, 'publication_evidence_kind': None, 'primary_effect_id': common_primary_effect_id, 'stream_id': stream_id, 'stream_sequence': stream_sequence, 'predecessor_effect_id': predecessor_effect_id, 'created_at': timestamp, 'updated_at': timestamp, 'finalized_at': None}
"""

_NULL_DEADLINE_RECIPES[
    ("src/elspeth/core/landscape/execution/sink_effect_reservation.py", "SinkEffectReservation._insert_or_compare_effect")
] = r"""
def _insert_or_compare_effect(self, conn: Connection, request: SinkEffectReservationRequest, identity: _EffectIdentity, *, stream_id: str | None, stream_sequence: int | None, predecessor_effect_id: str | None, coordination_token: CoordinationToken) -> tuple[bool, SinkEffect]:
    if request.run_id != coordination_token.run_id:
        raise ValueError(f"sink effect reservation names run {request.run_id!r}, not the coordination token's run")
    values = _effect_row_values(conn, request, identity, stream_id=stream_id, stream_sequence=stream_sequence, predecessor_effect_id=predecessor_effect_id)
    if conn.dialect.name == 'sqlite':
        inserted = conn.execute(sqlite_insert(sink_effects_table).values(**values).on_conflict_do_nothing(index_elements=['effect_id']).returning(sink_effects_table.c.effect_id)).fetchone() is not None
    elif conn.dialect.name == 'postgresql':
        inserted = conn.execute(postgresql_insert(sink_effects_table).values(**values).on_conflict_do_nothing(index_elements=['effect_id']).returning(sink_effects_table.c.effect_id)).fetchone() is not None
    else:
        raise _unsupported_backend(conn)
    row = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == identity.effect_id).with_for_update()).fetchone()
    if row is None:
        raise ValueError('sink effect winner disappeared')
    immutable_fields = ((row.run_id, request.run_id), (row.sink_node_id, request.sink_node_id), (row.role, request.role.value), (row.protocol_version, SINK_EFFECT_PROTOCOL_VERSION), (row.input_kind, request.input_kind.value), (row.required_member_ordinal, values['required_member_ordinal']), (row.required_snapshot_slot, values['required_snapshot_slot']), (row.config_hash, request.config_hash), (row.membership_or_manifest_hash, identity.membership_or_manifest_hash), (row.group_payload_hash, identity.group_payload_hash), (row.artifact_id, identity.artifact_id), (row.artifact_idempotency_key, identity.artifact_idempotency_key), (row.primary_effect_id, values['primary_effect_id']), (row.stream_id, values['stream_id']), (row.stream_sequence, values['stream_sequence']), (row.predecessor_effect_id, values['predecessor_effect_id']))
    if any((observed != expected for observed, expected in immutable_fields)):
        raise ValueError('sink effect identity winner is divergent')
    if inserted and row.target_json != _EMPTY_TARGET_JSON:
        raise ValueError('new sink effect did not preserve its empty target sentinel')
    return (inserted, self._effect_loader.load(row))
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/work_items.py", "ready_work_item_values")] = r"""
def ready_work_item_values(*, run_id: str, token_id: str, row_id: str, node_id: str | None, step_index: int, ingest_sequence: int, row_payload_json: str, available_at: datetime, attempt: int, queue_key: str | None, barrier_key: str | None, on_success_sink: str | None, join_group_id: str | None, lineage_path: tuple[LineageFrame, ...], coalesce_node_id: str | None, coalesce_name: str | None, row_union_name: str | None=None, collector_name: str | None=None) -> dict[str, object]:
    return {'work_item_id': work_item_id(run_id, token_id, node_id, attempt), 'run_id': run_id, 'token_id': token_id, 'row_id': row_id, 'node_id': node_id, 'step_index': step_index, 'ingest_sequence': ingest_sequence, 'row_payload_json': row_payload_json, 'status': TokenWorkStatus.READY.value, 'queue_key': queue_key, 'barrier_key': barrier_key, 'on_success_sink': on_success_sink, 'pending_sink_name': None, 'pending_outcome': None, 'pending_path': None, 'pending_error_hash': None, 'pending_error_message': None, 'join_group_id': join_group_id, 'lineage_path_json': lineage_path_to_json(lineage_path), 'coalesce_node_id': coalesce_node_id, 'coalesce_name': coalesce_name, 'row_union_name': row_union_name, 'collector_name': collector_name, 'attempt': attempt, 'lease_owner': None, 'lease_expires_at': None, 'available_at': available_at, 'created_at': available_at, 'updated_at': available_at}
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/work_items.py", "_validate_replay")] = r"""
def _validate_replay(values: dict[str, object], existing: Mapping[str, object], *, operation: str) -> None:
    comparable_fields = ('work_item_id', 'run_id', 'token_id', 'row_id', 'node_id', 'step_index', 'ingest_sequence', 'row_payload_json', 'queue_key', 'barrier_key', 'on_success_sink', 'pending_sink_name', 'pending_outcome', 'pending_path', 'pending_error_hash', 'pending_error_message', 'join_group_id', 'lineage_path_json', 'coalesce_node_id', 'coalesce_name', 'row_union_name', 'collector_name', 'attempt')
    mismatches = {field_name: {'expected': mismatch_diagnostic_value(field_name, values[field_name]), 'actual': mismatch_diagnostic_value(field_name, existing[field_name])} for field_name in comparable_fields if existing[field_name] != values[field_name]}
    if mismatches:
        raise LandscapeRecordError(f'Scheduler {operation} found incompatible existing work item for {work_item_identity(values)}: {mismatches!r}')
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/work_items.py", "insert_work_item_idempotent")] = r"""
def insert_work_item_idempotent(conn: Connection, *, values: dict[str, object], operation: str) -> bool:
    return bool(insert_work_items_idempotent(conn, values=[values], operation=operation))
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/work_items.py", "insert_work_items_idempotent")] = r"""
def insert_work_items_idempotent(conn: Connection, *, values: list[dict[str, object]], operation: str) -> frozenset[str]:
    if not values:
        return frozenset()
    by_id: dict[str, dict[str, object]] = {}
    for value in values:
        identity = str(value['work_item_id'])
        if identity in by_id:
            _validate_replay(value, by_id[identity], operation=operation)
        else:
            by_id[identity] = value
    try:
        if conn.dialect.name == 'sqlite':
            inserted = conn.execute(sqlite_insert(token_work_items_table).on_conflict_do_nothing(index_elements=['work_item_id']).returning(token_work_items_table.c.work_item_id), list(by_id.values())).scalars().all()
        elif conn.dialect.name == 'postgresql':
            inserted = conn.execute(postgresql_insert(token_work_items_table).on_conflict_do_nothing(index_elements=['work_item_id']).returning(token_work_items_table.c.work_item_id), list(by_id.values())).scalars().all()
        else:
            raise NotImplementedError(f'Scheduler idempotent enqueue unsupported dialect {conn.dialect.name!r}')
    except SQLAlchemyError as exc:
        raise LandscapeRecordError(f'Scheduler {operation} failed; database rejected audit write') from exc
    if len(inserted) != len(set(inserted)) or not set(inserted).issubset(by_id):
        raise LandscapeRecordError(f'Scheduler {operation} returned unexpected work_item_id')
    existing_rows = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id.in_(tuple(by_id)))).mappings().all()
    if len(existing_rows) != len(by_id):
        raise LandscapeRecordError(f'Scheduler {operation} failed; no matching row could be read back')
    for row in existing_rows:
        if row['work_item_id'] not in inserted:
            _validate_replay(by_id[row['work_item_id']], dict(row), operation=operation)
    return frozenset(inserted)
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/work_items.py", "insert_work_items")] = r"""
def insert_work_items(conn: Connection, *, values: list[dict[str, object]], operation: str) -> None:
    if not values:
        return
    expected = [value['work_item_id'] for value in values]
    try:
        inserted = conn.execute(token_work_items_table.insert().returning(token_work_items_table.c.work_item_id), values).scalars().all()
    except SQLAlchemyError as exc:
        raise LandscapeRecordError(f'Scheduler {operation} failed; database rejected audit write') from exc
    if len(inserted) != len(expected) or frozenset(inserted) != frozenset(expected):
        raise LandscapeRecordError(f'Scheduler {operation} returned an unexpected work item identity set')
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/queue.py", "SchedulerQueueRepository.enqueue_ready")] = r"""
def enqueue_ready(self, *, member_token: WorkerMembershipToken, token_id: str, row_id: str, node_id: str | None, step_index: int, ingest_sequence: int, row_payload_json: str, attempt: int=1, queue_key: str | None=None, barrier_key: str | None=None, on_success_sink: str | None=None, join_group_id: str | None=None, lineage_path: tuple[LineageFrame, ...]=(), coalesce_node_id: str | None=None, coalesce_name: str | None=None, row_union_name: str | None=None, collector_name: str | None=None) -> TokenWorkItem:
    run_id = member_token.run_id
    work_item_id = make_work_item_id(run_id, token_id, node_id, attempt)
    with fenced_member_transaction(self._engine, member_token=member_token, verb='enqueue_ready') as conn:
        available_at = read_landscape_transaction_time(conn)
        values = ready_work_item_values(run_id=run_id, token_id=token_id, row_id=row_id, node_id=node_id, step_index=step_index, ingest_sequence=ingest_sequence, row_payload_json=row_payload_json, available_at=available_at, attempt=attempt, queue_key=queue_key, barrier_key=barrier_key, on_success_sink=on_success_sink, join_group_id=join_group_id, lineage_path=lineage_path, coalesce_node_id=coalesce_node_id, coalesce_name=coalesce_name, row_union_name=row_union_name, collector_name=collector_name)
        validate_work_item_references(conn, run_id=run_id, token_id=token_id, row_id=row_id, ingest_sequence=ingest_sequence, node_id=node_id, coalesce_node_id=coalesce_node_id)
        inserted = insert_work_item_idempotent(conn, values=values, operation='enqueue READY scheduler work')
        if inserted:
            self._events.record(conn, event_type=SchedulerEventType.ENQUEUE, run_id=run_id, token_id=token_id, work_item_id=work_item_id, node_id=node_id, from_status=None, to_status=TokenWorkStatus.READY, from_lease_owner=None, to_lease_owner=None, from_attempt=None, to_attempt=attempt, recorded_at=available_at)
        row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
    return item_from_mapping(row)
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/queue.py", "SchedulerQueueRepository.enqueue_ready_claimed_on")] = r"""
def enqueue_ready_claimed_on(self, conn: Connection, *, run_id: str, token_id: str, row_id: str, node_id: str | None, step_index: int, ingest_sequence: int, row_payload_json: str, lease_owner: str, lease_seconds: int, attempt: int=1, queue_key: str | None=None, barrier_key: str | None=None, on_success_sink: str | None=None, join_group_id: str | None=None, lineage_path: tuple[LineageFrame, ...]=(), coalesce_node_id: str | None=None, coalesce_name: str | None=None, row_union_name: str | None=None, collector_name: str | None=None, worker_id: str | None=None) -> RowMapping:
    work_item_id = make_work_item_id(run_id, token_id, node_id, attempt)
    available_at = read_landscape_transaction_time(conn)
    values = ready_work_item_values(run_id=run_id, token_id=token_id, row_id=row_id, node_id=node_id, step_index=step_index, ingest_sequence=ingest_sequence, row_payload_json=row_payload_json, available_at=available_at, attempt=attempt, queue_key=queue_key, barrier_key=barrier_key, on_success_sink=on_success_sink, join_group_id=join_group_id, lineage_path=lineage_path, coalesce_node_id=coalesce_node_id, coalesce_name=coalesce_name, row_union_name=row_union_name, collector_name=collector_name)
    if worker_id is not None:
        fence_holds = conn.execute(select(active_worker_fence_clause(worker_id=worker_id, run_id=run_id))).scalar()
        if not fence_holds:
            raise RunWorkerEvictedError(worker_id=worker_id, run_id=run_id)
    validate_work_item_references(conn, run_id=run_id, token_id=token_id, row_id=row_id, ingest_sequence=ingest_sequence, node_id=node_id, coalesce_node_id=coalesce_node_id)
    inserted = insert_work_item_idempotent(conn, values=values, operation='enqueue and claim READY scheduler work')
    if inserted:
        self._events.record(conn, event_type=SchedulerEventType.ENQUEUE, run_id=run_id, token_id=token_id, work_item_id=work_item_id, node_id=node_id, from_status=None, to_status=TokenWorkStatus.READY, from_lease_owner=None, to_lease_owner=None, from_attempt=None, to_attempt=attempt, recorded_at=available_at)
    row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
    if row['status'] == TokenWorkStatus.READY.value:
        claimed = self._leases.claim_ready_row(conn, row=row, run_id=run_id, lease_owner=lease_owner, lease_seconds=lease_seconds, strict_membership_fenced=worker_id is not None)
        if claimed is not None:
            row = claimed
    return row
"""

_NULL_DEADLINE_RECIPES[
    ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository._transition_with_ready_children")
] = r"""
def _transition_with_ready_children(self, *, work_item_id: str, emitted_ready: Sequence[BarrierEmission], status: TokenWorkStatus, image: DispositionImage, expected_lease_owner: str, group_losses: tuple[GroupLossSpec, ...], member_token: WorkerMembershipToken, require_complete_pending_sink_bundle: bool=False) -> tuple[TokenWorkItem, tuple[TokenWorkItem, ...]]:
    if not emitted_ready:
        raise ValueError('atomic child disposition requires at least one READY emission')
    if expected_lease_owner != member_token.worker_id:
        raise ValueError('disposition lease owner must match membership token')
    with fenced_member_transaction(self._engine, member_token=member_token, verb='_transition_with_ready_children') as conn:
        children = [self._prepare_ready_emission_on(conn, parent_work_item_id=work_item_id, run_id=member_token.run_id, emission=emission) for emission in emitted_ready]
        inserted_ids = insert_work_items_idempotent(conn, values=[values for values, _event in children], operation=f'atomic child enqueue for parent work_item_id={work_item_id!r}')
        events_by_id = {event.work_item_id: event for _values, event in children}
        self._events.record_many(conn, records=[event for identity, event in events_by_id.items() if identity in inserted_ids])
        persisted_children = conn.execute(select(token_work_items_table).where(token_work_items_table.c.run_id == member_token.run_id, token_work_items_table.c.work_item_id.in_(tuple(events_by_id)))).mappings().all()
        rows_by_id = {row['work_item_id']: row for row in persisted_children}
        child_rows = tuple((rows_by_id[event.work_item_id] for _values, event in children))
        parent_row = self._transition_on(conn, work_item_id=work_item_id, status=status, image=image, expected_lease_owner=expected_lease_owner, group_losses=group_losses, member_token=member_token, require_complete_pending_sink_bundle=require_complete_pending_sink_bundle)
    return (item_from_mapping(parent_row), tuple((item_from_mapping(row) for row in child_rows)))
"""

_NULL_DEADLINE_RECIPES[
    ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository._prepare_ready_emission_on")
] = r"""
def _prepare_ready_emission_on(self, conn: Connection, *, parent_work_item_id: str, run_id: str, emission: BarrierEmission) -> tuple[dict[str, object], SchedulerEventRecord]:
    if emission.row_id is None or emission.step_index is None or emission.ingest_sequence is None:
        raise AuditIntegrityError(f'Atomic child enqueue for parent work_item_id={parent_work_item_id!r} token_id={emission.token_id!r} requires row_id, step_index and ingest_sequence.')
    validate_work_item_references(conn, run_id=run_id, token_id=emission.token_id, row_id=emission.row_id, ingest_sequence=emission.ingest_sequence, node_id=emission.node_id, coalesce_node_id=emission.coalesce_node_id)
    available_at = read_landscape_transaction_time(conn)
    values = ready_work_item_values(run_id=run_id, token_id=emission.token_id, row_id=emission.row_id, node_id=emission.node_id, step_index=emission.step_index, ingest_sequence=emission.ingest_sequence, row_payload_json=emission.row_payload_json, available_at=available_at, attempt=emission.attempt, queue_key=emission.queue_key, barrier_key=emission.barrier_key, on_success_sink=emission.on_success_sink, join_group_id=emission.join_group_id, lineage_path=emission.lineage_path, coalesce_node_id=emission.coalesce_node_id, coalesce_name=emission.coalesce_name, row_union_name=emission.row_union_name, collector_name=emission.collector_name)
    event = SchedulerEventRecord(event_type=SchedulerEventType.ENQUEUE, run_id=run_id, token_id=emission.token_id, work_item_id=str(values['work_item_id']), node_id=emission.node_id, from_status=None, to_status=TokenWorkStatus.READY, from_lease_owner=None, to_lease_owner=None, from_attempt=None, to_attempt=emission.attempt, recorded_at=available_at)
    return (values, event)
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository.complete_barrier")] = r"""
def complete_barrier(self, *, barrier_key: str, consumed_token_ids: Sequence[str], emitted_pending_sink: Sequence[BarrierEmission], emitted_ready: Sequence[BarrierEmission], require_exhaustive_release: bool=True, scope_row_id: str | None=None, intake_snapshot_token_ids: frozenset[str] | None=None, release_context: Mapping[str, object] | None=None, coordination_token: CoordinationToken, pending_sink_lease_owner: str | None=None, group_losses: Sequence[GroupLossSpec]=(), terminal_outcomes: Sequence[BarrierTerminalOutcomeSpec]=()) -> int:
    require_coordination_token(coordination_token, verb='complete_barrier')
    run_id = coordination_token.run_id
    if scope_row_id is not None and (not require_exhaustive_release):
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received scope_row_id={scope_row_id!r} with require_exhaustive_release=False; row scoping narrows the exhaustiveness universe and is meaningless on the legacy partial-release arm.')
    if intake_snapshot_token_ids is not None and (not require_exhaustive_release):
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received intake_snapshot_token_ids with require_exhaustive_release=False; the snapshot narrows the exhaustiveness universe and is meaningless on the legacy partial-release arm.')
    consumed = tuple(consumed_token_ids)
    consumed_set = frozenset(consumed)
    if len(consumed_set) != len(consumed):
        duplicates = sorted((token_id for token_id in consumed_set if consumed.count(token_id) > 1))
        raise AuditIntegrityError(f'Scheduler barrier terminalization received duplicate live token_ids for run_id={run_id!r} barrier_key={barrier_key!r}: {duplicates!r}')
    pending_token_ids = tuple((emission.token_id for emission in emitted_pending_sink))
    if len(frozenset(pending_token_ids)) != len(pending_token_ids):
        duplicates = sorted((token_id for token_id in frozenset(pending_token_ids) if pending_token_ids.count(token_id) > 1))
        raise AuditIntegrityError(f'Scheduler barrier completion received duplicate pending-sink emissions for run_id={run_id!r} barrier_key={barrier_key!r}: {duplicates!r}')
    ready_token_ids = tuple((emission.token_id for emission in emitted_ready))
    if len(frozenset(ready_token_ids)) != len(ready_token_ids):
        duplicates = sorted((token_id for token_id in frozenset(ready_token_ids) if ready_token_ids.count(token_id) > 1))
        raise AuditIntegrityError(f'Scheduler barrier completion received duplicate ready emissions for run_id={run_id!r} barrier_key={barrier_key!r}: {duplicates!r}')
    terminal_outcome_token_ids = tuple((spec.token_id for spec in terminal_outcomes))
    if len(frozenset(terminal_outcome_token_ids)) != len(terminal_outcome_token_ids):
        raise AuditIntegrityError(f'Scheduler barrier completion received duplicate terminal outcomes for run_id={run_id!r} barrier_key={barrier_key!r}.')
    terminal_outcomes_outside_consumed = frozenset(terminal_outcome_token_ids) - consumed_set
    if terminal_outcomes_outside_consumed:
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received terminal outcomes outside its consumed set: {sorted(terminal_outcomes_outside_consumed)!r}.')
    pending_overlap = consumed_set & frozenset(pending_token_ids)
    if pending_overlap:
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received token_ids both consumed and emitted to pending-sink: {sorted(pending_overlap)!r}; a BLOCKED row cannot be terminalized and handed off in the same completion.')
    for emission in emitted_pending_sink:
        if emission.sink_name is None or emission.outcome is None or emission.path is None:
            raise AuditIntegrityError(f'Scheduler barrier completion pending-sink emission for run_id={run_id!r} barrier_key={barrier_key!r} token_id={emission.token_id!r} requires sink_name, outcome and path.')
    if intake_snapshot_token_ids is not None:
        consumed_outside_snapshot = consumed_set - intake_snapshot_token_ids
        if consumed_outside_snapshot:
            raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} consumed token(s) outside its own intake snapshot: {sorted(consumed_outside_snapshot)!r}; the flush caller may only consume tokens it durably adopted into this firing group (ADR-030 §E.3).')
    if scope_row_id is not None:
        for emission in (*emitted_pending_sink, *emitted_ready):
            if emission.row_id is not None and emission.row_id != scope_row_id:
                raise AuditIntegrityError(f"Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received emission token_id={emission.token_id!r} with row_id={emission.row_id!r} outside the scoped pending group scope_row_id={scope_row_id!r}; a scoped completion must not emit into another row's group.")
    emission_context: dict[str, object] = {'barrier_key': barrier_key}
    if require_exhaustive_release:
        emission_context['consumed_count'] = len(consumed_set)
    blocked_predicates = [token_work_items_table.c.run_id == coordination_token.run_id, token_work_items_table.c.barrier_key == barrier_key, token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value]
    if scope_row_id is not None:
        blocked_predicates.append(token_work_items_table.c.row_id == scope_row_id)
    with fenced_write(self._engine, coordination_token=coordination_token, verb='complete_barrier') as conn:
        database_now = read_landscape_transaction_time(conn)
        if terminal_outcome_token_ids:
            locked_tokens = conn.execute(select(tokens_table.c.token_id, tokens_table.c.run_id).where(tokens_table.c.token_id.in_(sorted(terminal_outcome_token_ids))).order_by(tokens_table.c.token_id).with_for_update(of=tokens_table)).all()
            if {(str(row.token_id), str(row.run_id)) for row in locked_tokens} != {(token_id, run_id) for token_id in terminal_outcome_token_ids}:
                raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received a terminal outcome for a missing or foreign token.')
            existing_terminal_rows = conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.run_id == coordination_token.run_id).where(token_outcomes_table.c.token_id.in_(terminal_outcome_token_ids)).where(token_outcomes_table.c.completed == 1)).all()
            if existing_terminal_rows:
                raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} would duplicate terminal outcomes for token_ids={sorted((str(row.token_id) for row in existing_terminal_rows))!r}.')
        blocked_rows = conn.execute(select(token_work_items_table.c.work_item_id, token_work_items_table.c.token_id, token_work_items_table.c.node_id, token_work_items_table.c.attempt, token_work_items_table.c.lease_owner, token_work_items_table.c.lease_expires_at).where(*blocked_predicates).order_by(token_work_items_table.c.ingest_sequence, token_work_items_table.c.step_index, token_work_items_table.c.work_item_id)).mappings().all()
        blocked_by_token: dict[str, list[RowMapping]] = {}
        for row in blocked_rows:
            blocked_by_token.setdefault(row['token_id'], []).append(row)
        durable_token_ids = frozenset(blocked_by_token)
        scope_note = '' if scope_row_id is None else f' scope_row_id={scope_row_id!r}.'
        missing_token_ids = consumed_set - durable_token_ids
        if missing_token_ids:
            matching_count = len(consumed_set & durable_token_ids)
            raise AuditIntegrityError(f'Scheduler barrier terminalization mismatch for run_id={run_id!r} barrier_key={barrier_key!r}: live consumed {len(consumed_set)} token(s), but durable BLOCKED rows contained {matching_count} matching token(s) out of {len(durable_token_ids)} blocked token(s). missing token_ids={sorted(missing_token_ids)!r}; durable token_ids={sorted(durable_token_ids)!r}.{scope_note}')
        if intake_snapshot_token_ids is not None:
            unknown_snapshot_token_ids = intake_snapshot_token_ids - durable_token_ids
            if unknown_snapshot_token_ids:
                if scope_row_id is not None:
                    cross_group_rows = conn.execute(select(token_work_items_table.c.token_id, token_work_items_table.c.row_id).where(token_work_items_table.c.run_id == coordination_token.run_id).where(token_work_items_table.c.barrier_key == barrier_key).where(token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value).where(token_work_items_table.c.token_id.in_(sorted(unknown_snapshot_token_ids)))).all()
                    cross_group = {row.token_id: row.row_id for row in cross_group_rows if row.row_id != scope_row_id}
                    if cross_group:
                        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} scope_row_id={scope_row_id!r} received intake snapshot token(s) whose durable BLOCKED rows belong to a DIFFERENT row group: {dict(sorted(cross_group.items()))!r}; the flush caller built its firing-group snapshot across row groups (ADR-030 §E.3 scope validation).')
                raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received intake snapshot token(s) with no durable BLOCKED row under the barrier: {sorted(unknown_snapshot_token_ids)!r}; the leader believes in a token the journal does not hold.{scope_note}')
        passthrough_emissions: list[BarrierEmission] = []
        fresh_emissions: list[BarrierEmission] = []
        for emission in emitted_pending_sink:
            matching_rows = blocked_by_token[emission.token_id] if emission.token_id in blocked_by_token else []
            if not matching_rows:
                if require_exhaustive_release:
                    fresh_emissions.append(emission)
                    continue
                raise AuditIntegrityError(f'Scheduler barrier pending-sink handoff for run_id={run_id!r} barrier_key={barrier_key!r} is missing token_id={emission.token_id!r}; refusing partial sink handoff.')
            if len(matching_rows) != 1:
                raise AuditIntegrityError(f'Scheduler barrier pending-sink handoff for run_id={run_id!r} barrier_key={barrier_key!r} token_id={emission.token_id!r} found {len(matching_rows)} matching rows; expected exactly one.')
            passthrough_emissions.append(emission)
        if require_exhaustive_release:
            handed_off_token_ids = frozenset((emission.token_id for emission in passthrough_emissions))
            if intake_snapshot_token_ids is not None:
                handed_off_outside_snapshot = handed_off_token_ids - intake_snapshot_token_ids
                if handed_off_outside_snapshot:
                    raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} handed off buffered token(s) outside its own intake snapshot: {sorted(handed_off_outside_snapshot)!r}; the flush caller may only hand off tokens it durably adopted into this firing group (ADR-030 §E.3).')
                required_token_ids = durable_token_ids & intake_snapshot_token_ids
                late_arrival_token_ids = durable_token_ids - intake_snapshot_token_ids
                if late_arrival_token_ids:
                    emission_context['late_arrival_token_ids'] = sorted(late_arrival_token_ids)
            else:
                required_token_ids = durable_token_ids
            uncovered_token_ids = required_token_ids - consumed_set - handed_off_token_ids
            if uncovered_token_ids:
                raise AuditIntegrityError(f'Scheduler barrier completion mismatch for run_id={run_id!r} barrier_key={barrier_key!r}: durable BLOCKED rows hold {len(uncovered_token_ids)} token(s) neither consumed nor handed off; the completion would orphan them. uncovered token_ids={sorted(uncovered_token_ids)!r}; consumed token_ids={sorted(consumed_set)!r}; handoff token_ids={sorted(handed_off_token_ids)!r}.{scope_note}')
        terminalized = self._terminalize_consumed_barrier_rows(conn, run_id=coordination_token.run_id, barrier_key=barrier_key, consumed=consumed, blocked_by_token=blocked_by_token, database_now=database_now, release_context=release_context)
        record_terminal_outcomes_guarded(conn, run_id=coordination_token.run_id, outcomes=terminal_outcomes, recorded_at=database_now)
        self._transition_passthrough_pending_sink(conn, run_id=coordination_token.run_id, barrier_key=barrier_key, blocked_rows=blocked_rows, passthrough_emissions=passthrough_emissions, emission_context=emission_context, database_now=database_now, parked_lease_owner=pending_sink_lease_owner)
        pending = [self._prepare_fresh_pending_sink_emission(conn, run_id=coordination_token.run_id, barrier_key=barrier_key, emission=emission, emission_context=emission_context, database_now=database_now, parked_lease_owner=pending_sink_lease_owner) for emission in fresh_emissions]
        ready = [self._prepare_ready_emission(conn, run_id=coordination_token.run_id, barrier_key=barrier_key, emission=emission, emission_context=emission_context, database_now=database_now, claim_order_at=database_now - timedelta(microseconds=len(emitted_ready) - emission_index - 1)) for emission_index, emission in enumerate(emitted_ready)]
        insert_work_items(conn, values=[values for values, _event in (*pending, *ready)], operation='barrier-completion emissions')
        self._events.record_many(conn, records=[event for _values, event in (*pending, *ready)])
        record_group_losses(conn, run_id=coordination_token.run_id, specs=group_losses, recorded_by=coordination_token.worker_id, now=database_now)
    return terminalized
"""

_NULL_DEADLINE_RECIPES[
    ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository._prepare_fresh_pending_sink_emission")
] = r"""
def _prepare_fresh_pending_sink_emission(self, conn: Connection, *, run_id: str, barrier_key: str, emission: BarrierEmission, emission_context: Mapping[str, object], database_now: datetime, parked_lease_owner: str | None=None) -> tuple[dict[str, object], SchedulerEventRecord]:
    if emission.node_id is not None:
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} received fresh pending-sink emission token_id={emission.token_id!r} with node_id={emission.node_id!r}; fresh sink-bound emissions live on the node_id-NULL terminal lane.')
    if emission.row_id is None or emission.step_index is None or emission.ingest_sequence is None:
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} fresh pending-sink emission token_id={emission.token_id!r} requires row_id, step_index and ingest_sequence; the inserted journal row must be a complete resume cursor.')
    validate_work_item_references(conn, run_id=run_id, token_id=emission.token_id, row_id=emission.row_id, ingest_sequence=emission.ingest_sequence, node_id=None, coalesce_node_id=emission.coalesce_node_id)
    work_item_id = make_work_item_id(run_id, emission.token_id, None, emission.attempt)
    values: dict[str, object] = {'work_item_id': work_item_id, 'run_id': run_id, 'token_id': emission.token_id, 'row_id': emission.row_id, 'node_id': None, 'step_index': emission.step_index, 'ingest_sequence': emission.ingest_sequence, 'row_payload_json': emission.row_payload_json, 'status': TokenWorkStatus.PENDING_SINK.value, 'queue_key': emission.queue_key, 'barrier_key': emission.barrier_key, 'on_success_sink': emission.on_success_sink, 'pending_sink_name': emission.sink_name, 'pending_outcome': emission.outcome, 'pending_path': emission.path, 'pending_error_hash': emission.error_hash, 'pending_error_message': emission.error_message, 'join_group_id': emission.join_group_id, 'lineage_path_json': lineage_path_to_json(emission.lineage_path), 'coalesce_node_id': emission.coalesce_node_id, 'coalesce_name': emission.coalesce_name, 'row_union_name': emission.row_union_name, 'collector_name': emission.collector_name, 'attempt': emission.attempt, 'lease_owner': parked_lease_owner, 'lease_expires_at': None, 'available_at': database_now, 'created_at': database_now, 'updated_at': database_now}
    event = SchedulerEventRecord(event_type=SchedulerEventType.MARK_PENDING_SINK, run_id=run_id, token_id=emission.token_id, work_item_id=work_item_id, node_id=None, from_status=None, to_status=TokenWorkStatus.PENDING_SINK, from_lease_owner=None, to_lease_owner=parked_lease_owner, from_attempt=None, to_attempt=emission.attempt, recorded_at=database_now, context=emission_context)
    return (values, event)
"""

_NULL_DEADLINE_RECIPES[("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository._prepare_ready_emission")] = r"""
def _prepare_ready_emission(self, conn: Connection, *, run_id: str, barrier_key: str, emission: BarrierEmission, emission_context: Mapping[str, object], database_now: datetime, claim_order_at: datetime) -> tuple[dict[str, object], SchedulerEventRecord]:
    if emission.row_id is None or emission.step_index is None or emission.ingest_sequence is None:
        raise AuditIntegrityError(f'Scheduler barrier completion for run_id={run_id!r} barrier_key={barrier_key!r} ready emission token_id={emission.token_id!r} requires row_id, step_index and ingest_sequence; the inserted journal row must be a complete resume cursor.')
    validate_work_item_references(conn, run_id=run_id, token_id=emission.token_id, row_id=emission.row_id, ingest_sequence=emission.ingest_sequence, node_id=emission.node_id, coalesce_node_id=emission.coalesce_node_id)
    values = ready_work_item_values(run_id=run_id, token_id=emission.token_id, row_id=emission.row_id, node_id=emission.node_id, step_index=emission.step_index, ingest_sequence=emission.ingest_sequence, row_payload_json=emission.row_payload_json, available_at=read_landscape_transaction_time(conn), attempt=emission.attempt, queue_key=emission.queue_key, barrier_key=emission.barrier_key, on_success_sink=emission.on_success_sink, join_group_id=emission.join_group_id, lineage_path=emission.lineage_path, coalesce_node_id=emission.coalesce_node_id, coalesce_name=emission.coalesce_name, row_union_name=emission.row_union_name, collector_name=emission.collector_name)
    values['created_at'] = claim_order_at
    event = SchedulerEventRecord(event_type=SchedulerEventType.ENQUEUE, run_id=run_id, token_id=emission.token_id, work_item_id=str(values['work_item_id']), node_id=emission.node_id, from_status=None, to_status=TokenWorkStatus.READY, from_lease_owner=None, to_lease_owner=None, from_attempt=None, to_attempt=emission.attempt, recorded_at=database_now, context=emission_context)
    return (values, event)
"""

_NULL_DEADLINE_IMPORT_RECIPES = {
    "src/elspeth/core/landscape/execution/sink_effect_reservation.py": (
        "ImportFrom(module='__future__', names=[alias(name='annotations')], level=0)",
        "Import(names=[alias(name='re')])",
        "ImportFrom(module='collections.abc', names=[alias(name='Mapping'), alias(name='Sequence')], level=0)",
        "ImportFrom(module='dataclasses', names=[alias(name='dataclass'), alias(name='replace')], level=0)",
        "ImportFrom(module='hashlib', names=[alias(name='sha256')], level=0)",
        "ImportFrom(module='typing', names=[alias(name='Any'), alias(name='Final')], level=0)",
        "ImportFrom(module='sqlalchemy', names=[alias(name='Row'), alias(name='select')], level=0)",
        "ImportFrom(module='sqlalchemy.dialects.postgresql', names=[alias(name='insert', asname='postgresql_insert')], level=0)",
        "ImportFrom(module='sqlalchemy.dialects.sqlite', names=[alias(name='insert', asname='sqlite_insert')], level=0)",
        "ImportFrom(module='sqlalchemy.engine', names=[alias(name='Connection')], level=0)",
        "ImportFrom(module='sqlalchemy.exc', names=[alias(name='IntegrityError')], level=0)",
        "ImportFrom(module='elspeth.contracts.audit', names=[alias(name='SinkEffect')], level=0)",
        "ImportFrom(module='elspeth.contracts.audit_export', names=[alias(name='C'), alias(name='H'), alias(name='final_manifest_identity_payload'), alias(name='hash_final_manifest_identity_payload')], level=0)",
        "ImportFrom(module='elspeth.contracts.coordination', names=[alias(name='DEFAULT_RUN_LIVENESS_WINDOW_SECONDS'), alias(name='CoordinationToken')], level=0)",
        "ImportFrom(module='elspeth.contracts.enums', names=[alias(name='NodeStateStatus'), alias(name='RunStatus')], level=0)",
        "ImportFrom(module='elspeth.contracts.freeze', names=[alias(name='freeze_fields')], level=0)",
        "ImportFrom(module='elspeth.contracts.hashing', names=[alias(name='canonical_json')], level=0)",
        "ImportFrom(module='elspeth.contracts.sink_effects', names=[alias(name='SINK_EFFECT_PROTOCOL_VERSION'), alias(name='AuditExportSignedManifestInput'), alias(name='AuditExportSigningMode'), alias(name='SinkEffectInputKind'), alias(name='SinkEffectMember'), alias(name='SinkEffectReservationRequest'), alias(name='SinkEffectState')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database', names=[alias(name='LandscapeDB')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database_clock', names=[alias(name='read_landscape_transaction_time')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.model_loaders', names=[alias(name='SinkEffectLoader')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.run_coordination_repository', names=[alias(name='fenced_leader_transaction')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.schema', names=[alias(name='audit_export_snapshots_table'), alias(name='node_states_table'), alias(name='operations_table'), alias(name='rows_table'), alias(name='runs_table'), alias(name='sink_effect_export_snapshots_table'), alias(name='sink_effect_members_table'), alias(name='sink_effect_streams_table'), alias(name='sink_effects_table'), alias(name='tokens_table')], level=0)",
    ),
    "src/elspeth/core/landscape/scheduler/work_items.py": (
        "ImportFrom(module='__future__', names=[alias(name='annotations')], level=0)",
        "Import(names=[alias(name='hashlib')])",
        "ImportFrom(module='collections.abc', names=[alias(name='Mapping')], level=0)",
        "ImportFrom(module='datetime', names=[alias(name='UTC'), alias(name='datetime')], level=0)",
        "ImportFrom(module='sqlalchemy', names=[alias(name='select')], level=0)",
        "ImportFrom(module='sqlalchemy.dialects.postgresql', names=[alias(name='insert', asname='postgresql_insert')], level=0)",
        "ImportFrom(module='sqlalchemy.dialects.sqlite', names=[alias(name='insert', asname='sqlite_insert')], level=0)",
        "ImportFrom(module='sqlalchemy.engine', names=[alias(name='Connection'), alias(name='RowMapping')], level=0)",
        "ImportFrom(module='sqlalchemy.exc', names=[alias(name='SQLAlchemyError')], level=0)",
        "ImportFrom(module='elspeth.contracts.errors', names=[alias(name='AuditIntegrityError')], level=0)",
        "ImportFrom(module='elspeth.contracts.identity', names=[alias(name='LineageFrame'), alias(name='lineage_path_from_json'), alias(name='lineage_path_to_json')], level=0)",
        "ImportFrom(module='elspeth.contracts.scheduler', names=[alias(name='TokenWorkItem'), alias(name='TokenWorkStatus')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.errors', names=[alias(name='LandscapeRecordError')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.schema', names=[alias(name='nodes_table'), alias(name='rows_table'), alias(name='token_work_items_table'), alias(name='tokens_table')], level=0)",
    ),
    "src/elspeth/core/landscape/scheduler/queue.py": (
        "ImportFrom(module='__future__', names=[alias(name='annotations')], level=0)",
        "ImportFrom(module='typing', names=[alias(name='TYPE_CHECKING')], level=0)",
        "ImportFrom(module='sqlalchemy', names=[alias(name='select')], level=0)",
        "ImportFrom(module='sqlalchemy.engine', names=[alias(name='Connection'), alias(name='RowMapping')], level=0)",
        "ImportFrom(module='elspeth.contracts.coordination', names=[alias(name='DEFAULT_RUN_LIVENESS_WINDOW_SECONDS'), alias(name='CoordinationToken'), alias(name='WorkerMembershipToken')], level=0)",
        "ImportFrom(module='elspeth.contracts.errors', names=[alias(name='AuditIntegrityError'), alias(name='RunWorkerEvictedError')], level=0)",
        "ImportFrom(module='elspeth.contracts.identity', names=[alias(name='LineageFrame')], level=0)",
        "ImportFrom(module='elspeth.contracts.scheduler', names=[alias(name='SchedulerEventType'), alias(name='SourceIngestSpec'), alias(name='TokenWorkItem'), alias(name='TokenWorkStatus')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database', names=[alias(name='Tier1Engine')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database_clock', names=[alias(name='read_landscape_transaction_time')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.run_coordination_repository', names=[alias(name='fenced_leader_transaction'), alias(name='fenced_member_transaction')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.events', names=[alias(name='SchedulerEventStore')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.leases', names=[alias(name='SchedulerLeaseRepository')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.work_items', names=[alias(name='insert_work_item_idempotent'), alias(name='item_from_mapping'), alias(name='ready_work_item_values'), alias(name='validate_work_item_references')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.work_items', names=[alias(name='work_item_id', asname='make_work_item_id')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.schema', names=[alias(name='active_worker_fence_clause'), alias(name='token_work_items_table')], level=0)",
    ),
    "src/elspeth/core/landscape/scheduler/dispositions.py": (
        "ImportFrom(module='__future__', names=[alias(name='annotations')], level=0)",
        "ImportFrom(module='collections.abc', names=[alias(name='Sequence')], level=0)",
        "ImportFrom(module='dataclasses', names=[alias(name='dataclass')], level=0)",
        "ImportFrom(module='typing', names=[alias(name='ClassVar')], level=0)",
        "ImportFrom(module='sqlalchemy', names=[alias(name='and_'), alias(name='case'), alias(name='select'), alias(name='update')], level=0)",
        "ImportFrom(module='sqlalchemy.engine', names=[alias(name='Connection'), alias(name='RowMapping')], level=0)",
        "ImportFrom(module='elspeth.contracts.coordination', names=[alias(name='DEFAULT_RUN_LIVENESS_WINDOW_SECONDS'), alias(name='CoordinationToken'), alias(name='WorkerMembershipToken')], level=0)",
        "ImportFrom(module='elspeth.contracts.errors', names=[alias(name='AuditIntegrityError')], level=0)",
        "ImportFrom(module='elspeth.contracts.scheduler', names=[alias(name='BarrierEmission'), alias(name='GroupLossSpec'), alias(name='SchedulerEventType'), alias(name='TokenWorkItem'), alias(name='TokenWorkStatus')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database', names=[alias(name='Tier1Engine')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database_clock', names=[alias(name='read_landscape_transaction_time')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.run_coordination_repository', names=[alias(name='fenced_leader_transaction'), alias(name='fenced_member_transaction')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.events', names=[alias(name='SchedulerEventRecord'), alias(name='SchedulerEventStore')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.fencing', names=[alias(name='fenced_write')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.group_losses', names=[alias(name='record_group_losses')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.payload_codec', names=[alias(name='scrubbed_row_payload_json')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.work_items', names=[alias(name='insert_work_items_idempotent'), alias(name='item_from_mapping'), alias(name='ready_work_item_values'), alias(name='validate_work_item_references')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.schema', names=[alias(name='pending_sink_bundle_clause'), alias(name='token_outcomes_table'), alias(name='token_work_items_table')], level=0)",
    ),
    "src/elspeth/core/landscape/scheduler/barrier.py": (
        "ImportFrom(module='__future__', names=[alias(name='annotations')], level=0)",
        "ImportFrom(module='collections.abc', names=[alias(name='Mapping'), alias(name='Sequence')], level=0)",
        "ImportFrom(module='dataclasses', names=[alias(name='dataclass')], level=0)",
        "ImportFrom(module='datetime', names=[alias(name='UTC'), alias(name='datetime'), alias(name='timedelta')], level=0)",
        "ImportFrom(module='sqlalchemy', names=[alias(name='case'), alias(name='func'), alias(name='select'), alias(name='update')], level=0)",
        "ImportFrom(module='sqlalchemy.engine', names=[alias(name='Connection'), alias(name='RowMapping')], level=0)",
        "ImportFrom(module='elspeth.contracts.coordination', names=[alias(name='DEFAULT_RUN_LIVENESS_WINDOW_SECONDS'), alias(name='CoordinationToken')], level=0)",
        "ImportFrom(module='elspeth.contracts.errors', names=[alias(name='AuditIntegrityError')], level=0)",
        "ImportFrom(module='elspeth.contracts.identity', names=[alias(name='lineage_path_to_json')], level=0)",
        "ImportFrom(module='elspeth.contracts.scheduler', names=[alias(name='BarrierEmission'), alias(name='BarrierTerminalOutcomeSpec'), alias(name='BatchMembershipSpec'), alias(name='BlockedPendingSinkHandoff'), alias(name='BufferedOutcomeSpec'), alias(name='GroupLossSpec'), alias(name='SchedulerEventType'), alias(name='TokenWorkItem'), alias(name='TokenWorkStatus')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.data_flow.outcomes', names=[alias(name='record_buffered_outcome_guarded'), alias(name='record_terminal_outcomes_guarded')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database', names=[alias(name='Tier1Engine')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.database_clock', names=[alias(name='read_landscape_transaction_time')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.execution.batches', names=[alias(name='add_batch_member_guarded')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.run_coordination_repository', names=[alias(name='fenced_leader_transaction')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.events', names=[alias(name='SchedulerEventRecord'), alias(name='SchedulerEventStore')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.fencing', names=[alias(name='fenced_write'), alias(name='require_coordination_token')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.group_losses', names=[alias(name='record_group_losses')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.payload_codec', names=[alias(name='scrubbed_row_payload_json')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.work_items', names=[alias(name='insert_work_items'), alias(name='item_from_mapping'), alias(name='ready_work_item_values'), alias(name='validate_work_item_references')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.scheduler.work_items', names=[alias(name='work_item_id', asname='make_work_item_id')], level=0)",
        "ImportFrom(module='elspeth.core.landscape.schema', names=[alias(name='blocked_barrier_hold_clause'), alias(name='token_outcomes_table'), alias(name='token_work_items_table'), alias(name='tokens_table')], level=0)",
    ),
}
_NULL_WORK_ITEMS_PATH = "src/elspeth/core/landscape/scheduler/work_items.py"
_NULL_EFFECT_PATH = "src/elspeth/core/landscape/execution/sink_effect_reservation.py"
_NULL_INSERT_TARGETS = frozenset(
    {
        (_NULL_EFFECT_PATH, "SinkEffectReservation._insert_or_compare_effect"),
        (_NULL_WORK_ITEMS_PATH, "insert_work_items_idempotent"),
        (_NULL_WORK_ITEMS_PATH, "insert_work_items"),
    }
)
_NULL_VALUE_CALLERS = {
    "insert_work_item_idempotent": frozenset(
        {
            ("src/elspeth/core/landscape/scheduler/queue.py", "SchedulerQueueRepository.enqueue_ready"),
            ("src/elspeth/core/landscape/scheduler/queue.py", "SchedulerQueueRepository.enqueue_ready_claimed_on"),
        }
    ),
    "insert_work_items_idempotent": frozenset(
        {
            (_NULL_WORK_ITEMS_PATH, "insert_work_item_idempotent"),
            ("src/elspeth/core/landscape/scheduler/dispositions.py", "SchedulerDispositionRepository._transition_with_ready_children"),
        }
    ),
    "insert_work_items": frozenset(
        {
            ("src/elspeth/core/landscape/scheduler/barrier.py", "BarrierJournalRepository.complete_barrier"),
        }
    ),
}


def _null_recipe_shape(function):
    clone = ast.parse(ast.unparse(function)).body[0]
    if (
        clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(clone.body[0].value, ast.Constant)
        and isinstance(clone.body[0].value.value, str)
    ):
        clone.body.pop(0)
    return ast.dump(clone, include_attributes=False)


def _null_recipe_is_visible(function, resolver, proof):
    imported = [node for node in resolver.unit.tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    if tuple(ast.dump(node, include_attributes=False) for node in imported) != _NULL_DEADLINE_IMPORT_RECIPES[resolver.unit.path]:
        return False
    module_bindings = _lexical_binding_sites(resolver.unit.tree)
    for declaration in imported:
        for alias in declaration.names:
            name = alias.asname or (alias.name.split(".")[0] if isinstance(declaration, ast.Import) else alias.name)
            if module_bindings.get(name) != [declaration]:
                return False
            if any(_lexical_binding_sites(scope).get(name) for scope in _clock_lexical_scopes(resolver.unit.tree)[1:]):
                return False
    if not _authority_return_body_is_visible(function, resolver):
        return False
    if not _receiver_callable_code_is_visible(function, function) or not proof._callback_function_unmodified(function):
        return False
    parent = next(_ancestors(function), None)
    if isinstance(parent, ast.ClassDef) and not _receiver_class_body_is_visible(proof, parent, resolver):
        return False
    local_names = set(_lexical_binding_sites(function))
    protected = {
        n.func.id for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id not in local_names
    }
    protected.add(function.name)
    if isinstance(parent, ast.ClassDef):
        protected.add(parent.name)
    if _clock_captured_alias_is_mutated(resolver.unit.tree, protected):
        return False
    # Global calls that construct or copy the INSERT payload must keep their
    # imported/builtin binding. A body recipe alone cannot prove rebinding.
    builtin_calls = {"list", "dict", "bool", "str", "set", "frozenset", "len", "any", "next", "iter", "enumerate"}
    module_bindings = _lexical_binding_sites(resolver.unit.tree)
    for name in protected & builtin_calls:
        if module_bindings.get(name):
            return False
        if any(_lexical_binding_sites(scope).get(name) for scope in _clock_lexical_scopes(resolver.unit.tree)[1:]):
            return False
    for call in (part for part in ast.walk(function) if isinstance(part, ast.Call) and isinstance(part.func, ast.Name)):
        origin = resolver.qualified_name(call.func, use=call)
        if (
            origin is not None
            and "." in origin
            and origin.rsplit(".", 1)[-1] == call.func.id
            and _trusted_qualified_name_is_mutated(origin, resolver=resolver, use=call)
        ):
            return False
    return True


def _null_recipe_graph_is_closed(units, proof, required):
    index = _function_index(units)
    functions = {}
    terminals = {symbol.rsplit(".", 1)[-1]: (path, symbol) for path, symbol in required}
    for key in required:
        function = index.get(key)
        if function is None:
            return False
        resolver = _resolver_for_node(function)
        expected = ast.parse(_NULL_DEADLINE_RECIPES[key]).body[0]
        if _null_recipe_shape(function) != _null_recipe_shape(expected) or not _null_recipe_is_visible(function, resolver, proof):
            return False
        if function.name in {"_effect_row_values", "ready_work_item_values", "_prepare_fresh_pending_sink_emission"}:
            literal_rows = [
                part
                for part in ast.walk(function)
                if isinstance(part, ast.Dict)
                and any(isinstance(key, ast.Constant) and key.value == "lease_expires_at" for key in part.keys)
            ]
            if len(literal_rows) != 1:
                return False
            row = literal_rows[0]
            if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in row.keys):
                return False
            deadlines = [value for key, value in zip(row.keys, row.values, strict=True) if key.value == "lease_expires_at"]
            if len(deadlines) != 1 or not isinstance(deadlines[0], ast.Constant) or deadlines[0].value is not None:
                return False
        functions[key] = function
    qualified_targets = {path[4:-3].replace("/", ".") + "." + symbol for path, symbol in required}
    owned_modules = {path[4:-3].replace("/", ".") for path, _symbol_name in required}
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for part in ast.walk(unit.tree):
            if isinstance(part, (ast.Name, ast.Attribute)):
                qualified = resolver.qualified_name(part, use=part)
                if qualified in qualified_targets:
                    parent = next(_ancestors(part), None)
                    if not isinstance(part.ctx, ast.Load) or not isinstance(parent, ast.Call) or parent.func is not part:
                        return False
                if isinstance(part, ast.Attribute) and part.attr in terminals and isinstance(part.ctx, (ast.Store, ast.Del)):
                    return False
                if (
                    isinstance(part, ast.Attribute)
                    and part.attr == "__dict__"
                    and resolver.qualified_name(part.value, use=part) in owned_modules
                ):
                    return False
            if isinstance(part, ast.Call):
                operation = _resolved_callable_name(part.func, resolver, use=part)
                if operation in {"getattr", "setattr", "__getattribute__", "__setattr__", "vars"} and part.args:
                    if resolver.qualified_name(part.args[0], use=part) in owned_modules | qualified_targets:
                        return False
                    if len(part.args) > 1 and _constant_string_value(part.args[1], resolver, use=part) in terminals:
                        return False
    # Every edge between reviewed recipes resolves the actual owned body,
    # including instance methods and aliases of imported module functions.
    for function in functions.values():
        resolver = _resolver_for_node(function)
        for call in (part for part in ast.walk(function) if isinstance(part, ast.Call)):
            terminal = _resolved_callable_name(call.func, resolver, use=call)
            if terminal not in terminals:
                continue
            target = proof.called_function(call, resolver, call)
            if target is None or target[0] is not functions[terminals[terminal]]:
                return False
    return True


def _null_insert_value_callers_are_closed(units, proof, functions):
    observed = {name: set() for name in _NULL_VALUE_CALLERS}
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for part in ast.walk(unit.tree):
            if isinstance(part, ast.Call):
                terminal = _resolved_callable_name(part.func, resolver, use=part)
                if terminal in _NULL_VALUE_CALLERS:
                    target = proof.called_function(part, resolver, part)
                    expected = functions.get((_NULL_WORK_ITEMS_PATH, terminal))
                    caller = _owner_function(part)
                    if target is None or target[0] is not expected or caller is None:
                        return False
                    identity = (unit.path, _symbol(caller))
                    if identity not in _NULL_VALUE_CALLERS[terminal] or identity in observed[terminal]:
                        return False
                    if len([kw for kw in part.keywords if kw.arg == "values"]) != 1 or any(kw.arg is None for kw in part.keywords):
                        return False
                    observed[terminal].add(identity)
                # Reflective access to the module leaves the set of callers
                # open, even when the requested attribute is computed later.
                if terminal in {"getattr", "__getattribute__", "vars"} and part.args:
                    receiver = part.args[0]
                    origin = resolver.qualified_name(receiver, use=part)
                    if origin == "elspeth.core.landscape.scheduler.work_items":
                        return False
            elif isinstance(part, (ast.Name, ast.Attribute)) and isinstance(part.ctx, ast.Load):
                terminal = _resolved_callable_name(part, resolver, use=part)
                if terminal not in _NULL_VALUE_CALLERS:
                    continue
                parent = next(_ancestors(part), None)
                if not isinstance(parent, ast.Call) or parent.func is not part:
                    return False
    return all(observed[name] == expected for name, expected in _NULL_VALUE_CALLERS.items())


@cache
def _non_issuing_deadline_insert_keys(units):
    proof = _AuthorityProof(units)
    index = _function_index(units)
    admitted = set()
    effects = frozenset(key for key in _NULL_DEADLINE_RECIPES if key[0] == _NULL_EFFECT_PATH)
    scheduler = frozenset(_NULL_DEADLINE_RECIPES) - effects
    if _null_recipe_graph_is_closed(units, proof, effects):
        admitted.add((_NULL_EFFECT_PATH, "SinkEffectReservation._insert_or_compare_effect"))
    if _null_recipe_graph_is_closed(units, proof, scheduler) and _null_insert_value_callers_are_closed(units, proof, index):
        admitted.update({(_NULL_WORK_ITEMS_PATH, "insert_work_items_idempotent"), (_NULL_WORK_ITEMS_PATH, "insert_work_items")})
    return frozenset(admitted)


def _non_issuing_deadline_insert_is_proven(function, resolver, units):
    """Prove the five dialect INSERT sites store NULL, never a lease grant.

    Full executable recipes establish the fresh dictionary and preserve its
    mapping through the two actual batching paths and scalar relay. All actual
    suppliers of an INSERT helper's values parameter must resolve to those
    reviewed bodies. No optional values input or function-name exemption is
    sufficient to admit an unresolved mapping.
    """
    key = (resolver.unit.path, _symbol(function))
    return key in _NULL_INSERT_TARGETS and key in _non_issuing_deadline_insert_keys(tuple(units))


_NULL_TEST_QUEUE_PATH = "src/elspeth/core/landscape/scheduler/queue.py"
_NULL_TEST_BARRIER_PATH = "src/elspeth/core/landscape/scheduler/barrier.py"
_NULL_TEST_DISPOSITION_PATH = "src/elspeth/core/landscape/scheduler/dispositions.py"


def _null_deadline_test_sources():
    return {path: (_repo_root() / path).read_text() for path in _NULL_DEADLINE_IMPORT_RECIPES}


def _null_deadline_test_admitted(sources):
    units = tuple((_parse_source(path, source) for path, source in sources.items()))
    return _non_issuing_deadline_insert_keys(units)


def test_non_issuing_deadline_five_actual_insert_sites():
    units = tuple((_parse_source(path, source) for path, source in _null_deadline_test_sources().items()))
    assert _non_issuing_deadline_insert_keys(units) == _NULL_INSERT_TARGETS
    index = _function_index(units)
    writes = []
    for key in _NULL_INSERT_TARGETS:
        f = index[key]
        resolver = _resolver_for_node(f)
        assert _non_issuing_deadline_insert_is_proven(f, resolver, units)
        writes.extend(_deadline_executed_writes(f, resolver))
    assert len(writes) == 5


@pytest.mark.parametrize(
    "path,old,new",
    [
        (_NULL_WORK_ITEMS_PATH, '"lease_expires_at": None', '"lease_expires_at": available_at'),
        (_NULL_EFFECT_PATH, '"lease_expires_at": None', '"lease_expires_at": timestamp'),
        (_NULL_TEST_BARRIER_PATH, '"lease_expires_at": None', '"lease_expires_at": database_now'),
        (
            _NULL_TEST_QUEUE_PATH,
            'inserted = insert_work_item_idempotent(conn, values=values, operation="enqueue READY scheduler work")',
            'values["lease_expires_at"] = available_at\n            inserted = insert_work_item_idempotent(conn, values=values, operation="enqueue READY scheduler work")',
        ),
        (_NULL_WORK_ITEMS_PATH, "values=[values], operation=operation", "values=[foreign], operation=operation"),
        (_NULL_WORK_ITEMS_PATH, "list(by_id.values()),", '[{**row, "lease_expires_at": foreign} for row in by_id.values()],'),
        (
            _NULL_TEST_DISPOSITION_PATH,
            "values=[values for values, _event in children]",
            'values=[{**values, "lease_expires_at": foreign} for values, _event in children]',
        ),
        (
            _NULL_TEST_BARRIER_PATH,
            "values=[values for values, _event in (*pending, *ready)]",
            'values=[{**values, "lease_expires_at": foreign} for values, _event in (*pending, *ready)]',
        ),
        (
            _NULL_WORK_ITEMS_PATH,
            "from sqlalchemy.dialects.sqlite import insert as sqlite_insert",
            "from foreign import insert as sqlite_insert",
        ),
        (_NULL_EFFECT_PATH, "def _effect_row_values(", "@foreign\ndef _effect_row_values("),
    ],
)
def test_non_issuing_deadline_mapping_drift_is_refused(path, old, new):
    sources = dict(_null_deadline_test_sources())
    assert old in sources[path]
    sources[path] = sources[path].replace(old, new)
    expected_effect = (_NULL_EFFECT_PATH, "SinkEffectReservation._insert_or_compare_effect")
    result = _null_deadline_test_admitted(sources)
    if path == _NULL_EFFECT_PATH:
        assert expected_effect not in result
    else:
        assert not {(_NULL_WORK_ITEMS_PATH, "insert_work_items"), (_NULL_WORK_ITEMS_PATH, "insert_work_items_idempotent")} & result


@pytest.mark.parametrize(
    "extra",
    [
        'from elspeth.core.landscape.scheduler.work_items import insert_work_items\ndef extra(conn,values):\n    insert_work_items(conn,values=values,operation="extra")',
        'from elspeth.core.landscape.scheduler.work_items import insert_work_items as writer\nalias=writer\ndef extra(conn,values):\n    alias(conn,values=values,operation="extra")',
        'from elspeth.core.landscape.scheduler import work_items\ndef extra(conn,values,name):\n    getattr(work_items,name)(conn,values=values,operation="extra")',
        "from elspeth.core.landscape.scheduler.work_items import ready_work_item_values as builder\nbuilder.__code__=foreign.__code__",
        "from elspeth.core.landscape.scheduler.work_items import ready_work_item_values as builder\nmutate(builder)",
    ],
)
def test_non_issuing_deadline_external_caller_or_escape_is_refused(extra):
    sources = dict(_null_deadline_test_sources())
    sources["src/elspeth/core/landscape/extra.py"] = extra
    assert not {
        (_NULL_WORK_ITEMS_PATH, "insert_work_items"),
        (_NULL_WORK_ITEMS_PATH, "insert_work_items_idempotent"),
    } & _null_deadline_test_admitted(sources)


def _namespace_fixture(extra):
    return (_parse_source("src/elspeth/namespace_consumer.py", "import elspeth.core.landscape.lease_deadlines as d\n" + extra),)


def test_deadline_namespace_direct_audited_calls_are_not_mutations():
    units = _namespace_fixture("d.record_issued_deadline(conn, key=key, expires_at=expires, window_seconds=60)\n")
    assert not _deadline_dependency_mutation_violations(units, _DEADLINE_REGISTRY_NAMESPACES)


@pytest.mark.parametrize(
    "extra",
    [
        "d.record_issued_deadline = foreign\n",
        "alias = d\nalias.record_issued_deadline = foreign\n",
        "record = d.record_issued_deadline\nrecord.__code__ = foreign.__code__\n",
        "setattr(d, 'record_issued_deadline', foreign)\n",
        "from builtins import setattr as change\nchange(d, 'record_issued_deadline', foreign)\n",
        "object.__setattr__(d, 'record_issued_deadline', foreign)\n",
        "d.__setattr__('record_issued_deadline', foreign)\n",
        "d.__dict__['record_issued_deadline'] = foreign\n",
        "vars(d)['record_issued_deadline'] = foreign\n",
        "setattr(d, unknown_name, foreign)\n",
        "name = 'record_' + 'issued_deadline'\ngetattr(d, name)(conn, key=key)\n",
        "def corrupt(namespace):\n    namespace.record_issued_deadline = foreign\ncorrupt(d)\n",
        "callback(d.record_issued_deadline)\n",
        "packed = {'namespace': d}\n",
        "d._STATE_KEY = 'foreign-state'\n",
    ],
)
def test_deadline_namespace_external_mutations_and_escapes_are_refused(extra):
    assert _deadline_dependency_mutation_violations(_namespace_fixture(extra), _DEADLINE_REGISTRY_NAMESPACES)


def test_deadline_namespace_whole_production_baseline():
    findings = _deadline_dependency_mutation_violations(_production_units(), _DEADLINE_REGISTRY_NAMESPACES)
    assert not findings, findings


@pytest.mark.parametrize(
    "extra",
    [
        "from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository\nRunLifecycleRepository.__getattribute__ = foreign\n",
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\nsetattr(RunCoordinationRepository, '__getattr__', foreign)\n",
        "import elspeth.core.landscape.run_lifecycle_repository as lifecycle\ncallback(lifecycle)\n",
    ],
)
def test_deadline_namespace_external_repository_lookup_replacement(extra):
    protected = _DEADLINE_REGISTRY_NAMESPACES | _DEADLINE_FINALIZATION_DEPENDENCIES
    units = (_parse_source("src/elspeth/namespace_lookup_mutation.py", extra),)
    assert _deadline_dependency_mutation_violations(units, protected)


def test_deadline_namespace_complete_dependency_graph_baseline():
    protected = _DEADLINE_REGISTRY_NAMESPACES | _DEADLINE_FINALIZATION_DEPENDENCIES
    findings = _deadline_dependency_mutation_violations(_production_units(), protected)
    assert not findings, findings


def _deadline_issuance_violations(units: tuple[SourceUnit, ...]) -> tuple[str, ...]:
    """Prove each fresh lease stamp is locked and registered with its actual recipe.

    The clock gate proves the helper's dialect implementation. This gate proves
    the caller's separate ordering and obligation identity; recognizing an
    imported clock alone cannot establish either fact.
    """
    violations = list(_deadline_dependency_mutation_violations(units, _DEADLINE_REGISTRY_NAMESPACES | _DEADLINE_FINALIZATION_DEPENDENCIES))
    proof = _AuthorityProof(units)
    proven_coordination_writers = _proven_coordination_deadline_writers(units)
    proven_null_insert_writers = _non_issuing_deadline_insert_keys(units)
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for function in ast.walk(unit.tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (unit.path, _symbol(function)) in proven_coordination_writers:
                continue
            calls = [item for item in _walk_same_scope(function) if isinstance(item, ast.Call)]
            # Inventory the deadline sink, not a recognizable producer. A
            # wrapper, transaction clock, or missing registration must never
            # cause the entire writer to disappear from this proof.
            registrations = [(call, _deadline_registration_shape(call, resolver, proof)) for call in calls]
            for write, execute, connection, table, write_values in _deadline_executed_writes(function, resolver):
                field, kind, identity_columns = _DEADLINE_FIELDS[table]
                if write_values is None:
                    if (unit.path, _symbol(function)) in proven_null_insert_writers:
                        continue
                    violations.append(f"{unit.path}:{function.name}:{write.lineno}: deadline table has unresolved write values")
                    continue
                if field not in write_values:
                    continue
                value = write_values[field]
                expiry = _deadline_binding(value, resolver, write)
                if isinstance(expiry, ast.Constant) and expiry.value is None:
                    continue
                issue = f"{unit.path}:{function.name}:{write.lineno}: deadline issuance"
                if not isinstance(expiry, ast.BinOp) or not isinstance(expiry.op, ast.Add):
                    violations.append(issue + " lacks a fresh sample plus an explicit duration")
                    continue
                sample = _deadline_binding(expiry.left, resolver, expiry)
                if (
                    not isinstance(sample, ast.Call)
                    or resolver.qualified_name(sample.func, use=sample) != _FRESH_CLOCK
                    or len(sample.args) != 1
                    or sample.keywords
                    or not _deadline_api_is_visible(_FRESH_CLOCK, resolver, sample)
                ):
                    violations.append(issue + " lacks its own fresh database sample")
                    continue
                identity = _deadline_write_identity(write, resolver, table)
                identity.update(write_values)
                expected_identity = tuple(
                    _deadline_term(identity[column], resolver, write) for column in identity_columns if column in identity
                )
                if kind == "LEADER" and len(expected_identity) == 3:
                    expected_identity = (*expected_identity[:2], f"str({expected_identity[2]})")
                expected = (
                    kind,
                    expected_identity,
                    _deadline_term(connection, resolver, execute),
                    _deadline_term(value, resolver, write),
                    _deadline_duration_seconds(expiry.right, resolver),
                )
                matches = [
                    call
                    for call, actual in registrations
                    if actual == expected and call.lineno > write.lineno and _deadline_registration_covers_success(execute, call)
                ]
                if len(expected_identity) != len(identity_columns) or len(matches) != 1:
                    violations.append(issue + " does not register its exact row, connection, expiry and window")
                if _deadline_term(sample.args[0], resolver, sample) != _deadline_term(connection, resolver, execute):
                    violations.append(issue + " samples a different connection")
                if not _deadline_decision_uses_one_sample(write, execute, sample, write_values, table, resolver, function):
                    violations.append(issue + " splits eligibility or publication stamps from its issuing sample")
                lock_keys = {column: identity[column] for column in identity_columns if column in identity}
                if table == "run_coordination":
                    lock_keys = {column: identity[column] for column in ("run_id",) if column in identity}
                if table == "token_work_items" and "run_id" in identity:
                    lock_keys["run_id"] = identity["run_id"]
                if not _target_lock_precedes_fresh_sample(
                    sample,
                    function=function,
                    resolver=resolver,
                    proof=proof,
                    connection=connection,
                    table=("elspeth.core.landscape.schema", table),
                    key_values=lock_keys,
                ):
                    violations.append(issue + " is not preceded by its completed exclusive target lock")
    return tuple(violations)


def _pre_admission_helper_effect_violations(units: tuple[SourceUnit, ...]) -> tuple[str, ...]:
    """Do not hide clock reads before admission.

    ADR-048 orders statements within each authority-owning transaction.
    Independent identity/reference discovery is not that transaction's first
    statement; independently fenced callees retain their own admission proof.
    The complete DML/connection sweep separately checks every mutation owner.
    """
    proof = _AuthorityProof(units)
    memo: dict[int, bool] = {}

    def executes_database(call, resolver, seen):
        if resolver.qualified_name(call.func, use=call) in {
            _FRESH_CLOCK,
            "elspeth.core.landscape.database_clock.read_landscape_transaction_time",
        }:
            return True
        called = proof.called_function(call, resolver, call)
        if called is None:
            return False
        function, helper_resolver = called
        if _fenced_contexts(function) and _function_fence_violation(function) is None:
            # The ordinary owner check proves this callee's own transaction.
            # Its pre-admission helper calls are checked when this sweep visits
            # that function, so delegation cannot conceal an early clock.
            return False
        if id(function) in memo:
            return memo[id(function)]
        if id(function) in seen:
            return False
        result = any(
            executes_database(part, helper_resolver, seen | {id(function)})
            for part in _walk_same_scope(function)
            if isinstance(part, ast.Call)
        )
        if result:
            memo[id(function)] = True
        return result

    problems = []
    for unit in units:
        resolver = _resolver_for_unit(unit)
        for function in ast.walk(unit.tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            contexts = _fenced_contexts(function)
            if len(contexts) != 1:
                continue  # The ordinary fence-shape proof refuses this separately.
            context = contexts[0].owner
            admission = contexts[0].call
            for call in _walk_same_scope(function):
                if not isinstance(call, ast.Call) or call is admission:
                    continue
                if call.lineno >= context.lineno and not _is_descendant(call, admission):
                    continue
                if executes_database(call, resolver, frozenset()):
                    problems.append(f"{unit.path}:{_symbol(function)} database helper executes before authority admission")
    return tuple(problems)


def _journal_outbox_sources_are_proven(units: tuple[SourceUnit, ...], dml: Sequence[DmlIdentity]) -> bool:
    """Bind the extracted non-run outbox writer to its reviewed commit listener."""
    sources = {unit.path: unit.source for unit in units}
    if not _issued_deadline_registry_source_is_proven(sources):
        return False
    if _deadline_dependency_mutation_violations(units, _DEADLINE_REGISTRY_NAMESPACES):
        return False
    path = "src/elspeth/core/landscape/journal.py"
    helper = (path, "LandscapeJournal._prepare_commit")
    for unit in units:
        if unit.path == path:
            continue  # This complete module is bound by the registry recipe.
        resolver = _resolver_for_unit(unit)
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.Attribute) and node.attr == "_prepare_commit":
                return False
            if isinstance(node, ast.Name) and node.id == "_prepare_commit":
                return False
            if isinstance(node, ast.expr) and _constant_string_value(node, resolver, use=node) == "_prepare_commit":
                return False
    callers = {
        (edge.caller_path, edge.caller_symbol)
        for edge in _subordinate_helper_edges(units, dml)
        if (edge.helper_path, edge.helper_symbol) == helper
    }
    return callers == {(path, "LandscapeJournal._before_commit")}


@pytest.mark.parametrize(
    "fault", [None, "other_caller", "unresolved_caller", "reflected_caller", "rebound_connection", "missing_rollback", "early_guard"]
)
def test_journal_outbox_extraction_requires_reviewed_body_event_order_and_closed_caller(fault):
    sources = {path: (_repo_root() / path).read_text() for path in _REVIEWED_REGISTRY_MODULES}
    journal = "src/elspeth/core/landscape/journal.py"
    if fault == "other_caller":
        sources["src/elspeth/extra_journal_caller.py"] = (
            "from elspeth.core.landscape.journal import LandscapeJournal\n"
            "def invoke(journal: LandscapeJournal, conn):\n    journal._prepare_commit(conn)\n"
        )
    elif fault == "unresolved_caller":
        sources["src/elspeth/extra_journal_caller.py"] = "def invoke(journal, conn):\n    journal._prepare_commit(conn)\n"
    elif fault == "reflected_caller":
        sources["src/elspeth/extra_journal_caller.py"] = "def invoke(journal, conn):\n    getattr(journal, '_prepare_' + 'commit')(conn)\n"
    elif fault == "rebound_connection":
        sources[journal] = sources[journal].replace(
            "all_records = self._take_buffered_records(conn)", "conn = foreign\n        all_records = self._take_buffered_records(conn)"
        )
    elif fault == "missing_rollback":
        sources[journal] = sources[journal].replace("rollback_failed_commit(conn)", "pass")
    elif fault == "early_guard":
        sources[journal] = sources[journal].replace("install_deadline_guard(engine, after_journal=True)", "install_deadline_guard(engine)")
    units = tuple(_parse_source(path, source) for path, source in sources.items())
    assert _journal_outbox_sources_are_proven(units, scan_dml_identities(units)) is (fault is None)


def _transaction_order_violations(
    units: Iterable[SourceUnit],
    dml: Sequence[DmlIdentity],
) -> tuple[str, ...]:
    unit_list = tuple(units)
    journal_sources_proven = _journal_outbox_sources_are_proven(unit_list, dml)
    deadline_sources_proven = (
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "_renew_leader_deadline_on",
    ) in _proven_coordination_deadline_writers(unit_list)
    index = _function_index(unit_list)
    dml_symbols = {(site.path, site.symbol) for site in dml}
    # Each fence's OWN first statement. These are the fence, not a payload
    # writer reached through one, so they cannot be required to run inside a
    # fence without demanding that a fence fence itself. One entry per fence,
    # so a third fence has to be added here deliberately rather than inherited.
    exact_establishment_symbols = (
        {(item.caller_path, item.caller_symbol) for item in _AUTHORITY_ESTABLISHMENTS}
        | {(item.callee_path, item.callee_symbol) for item in _AUTHORITY_ESTABLISHMENTS}
        | {
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "verify_and_extend_leader_fence",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "verify_membership_fence",
            ),
        }
    )
    edges = _subordinate_helper_edges(unit_list, dml)
    edges_by_helper: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for edge in edges:
        edges_by_helper.setdefault((edge.helper_path, edge.helper_symbol), set()).add((edge.caller_path, edge.caller_symbol))
    reexports = _owned_reexports(unit_list)
    helper_keys = _subordinate_helper_keys(index, dml, reexports, _proven_read_statement_helpers(unit_list))
    unit_by_path = {unit.path: unit for unit in unit_list}
    violations: list[str] = [
        *_subordinate_helper_resolution_violations(unit_list, dml),
        *_heartbeat_fence_implementation_violations(unit_list),
        *_deadline_issuance_violations(unit_list),
        *_deadline_finalization_caller_violations(unit_list),
        *_pre_admission_helper_effect_violations(unit_list),
    ]
    admitted: dict[tuple[str, str], str | None] = {}

    def helper_calls_in(caller: ast.FunctionDef | ast.AsyncFunctionDef, caller_path: str, helper_key: tuple[str, str]) -> list[ast.Call]:
        caller_resolver = _resolver_for_unit(unit_by_path[caller_path])
        return [
            child
            for child in _walk_same_scope(caller)
            if isinstance(child, ast.Call)
            and _resolve_helper_candidates(child, unit_by_path[caller_path], {helper_key}, caller_resolver, reexports=reexports)
            == (helper_key,)
        ]

    def helper_side_violation(helper_key: tuple[str, str]) -> str | None:
        """Checks that depend on the helper alone, whoever calls it."""

        path = helper_key[0]
        helper = index[helper_key]
        helper_resolver = _resolver_for_unit(unit_by_path[path])
        if helper_calls_in(helper, path, helper_key):
            return "subordinate helper recursively invokes itself"
        if _parameter_rebound(helper, "conn"):
            return "subordinate conn parameter is rebound"
        binding_violation = _dml_execution_binding_violation(helper, "conn")
        if binding_violation is not None:
            return binding_violation
        helper_effects = [
            effect
            for effect in _database_effect_calls(helper)
            if _resolved_callable_name(effect.func, helper_resolver, use=effect) in _PAYLOAD_EFFECT_NAMES
        ]
        if any(not _payload_uses_exact_connection(effect, "conn") for effect in helper_effects):
            return "subordinate DML does not use the exact helper connection"
        return None

    def connection_arguments(call: ast.Call, helper: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
        positional_parameters = [argument.arg for argument in (*helper.args.posonlyargs, *helper.args.args) if argument.arg != "self"]
        values: list[ast.expr] = [keyword.value for keyword in call.keywords if keyword.arg == "conn"]
        if "conn" in positional_parameters and positional_parameters.index("conn") < len(call.args):
            values.append(call.args[positional_parameters.index("conn")])
        return values

    def exact_connection_argument(call: ast.Call, helper: ast.FunctionDef | ast.AsyncFunctionDef, connection: str | None) -> bool:
        values = connection_arguments(call, helper)
        return (
            len(values) == 1
            and isinstance(values[0], ast.Name)
            and values[0].id == connection
            and not any(keyword.arg is None for keyword in call.keywords)
        )

    def run_parameter_values(call: ast.Call, helper: ast.FunctionDef | ast.AsyncFunctionDef, run_parameter: str) -> list[ast.expr]:
        positional_parameters = [argument.arg for argument in (*helper.args.posonlyargs, *helper.args.args) if argument.arg != "self"]
        values = [keyword.value for keyword in call.keywords if keyword.arg == run_parameter]
        if run_parameter in positional_parameters:
            position = positional_parameters.index(run_parameter)
            if position < len(call.args):
                values.append(call.args[position])
        return values

    def run_subjects_bound(
        call: ast.Call,
        helper: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        bound_to: Callable[[ast.expr], bool],
    ) -> bool:
        """Every run-named parameter the helper's DML uses must be passed as a proven subject."""

        helper_arguments = [
            argument.arg for argument in (*helper.args.posonlyargs, *helper.args.args, *helper.args.kwonlyargs) if argument.arg != "self"
        ]
        for run_parameter in _dml_named_run_subjects(helper):
            if run_parameter not in helper_arguments or _parameter_rebound(helper, run_parameter):
                return False
            values = run_parameter_values(call, helper, run_parameter)
            if len(values) != 1 or not bound_to(values[0]):
                return False
        return True

    def call_site_violation(calls: list[ast.Call], *, transaction_scope: ast.AST, caller: ast.AST) -> str | None:
        if len(calls) != 1:
            return f"call sites={len(calls)} expected=1"
        if any(not _is_descendant(call, transaction_scope) for call in calls):
            return "subordinate call escapes its caller's fenced transaction"
        if any(_has_repeating_ancestor(call, stop=transaction_scope) for call in calls):
            return "subordinate call is nested in a runtime-repeating construct"
        if any(_is_statically_dead(call, stop=caller) for call in calls):
            return "subordinate call is statically unreachable"
        return None

    def evidence_edge_violation(helper_key: tuple[str, str], caller_key: tuple[str, str]) -> str | None:
        """The pinned unfenced-evidence edge: a proven-deposed writer's refusal row."""

        edge = _FENCE_REFUSAL_EVIDENCE_EDGE
        if caller_key in dml_symbols:
            return f"{edge.classification} caller constructs DML of its own"
        caller = index[caller_key]
        caller_resolver = _resolver_for_unit(unit_by_path[caller_key[0]])
        transactions = [
            (child, item)
            for child in _walk_same_scope(caller)
            if isinstance(child, (ast.With, ast.AsyncWith))
            for item in child.items
            if isinstance(item.context_expr, ast.Call)
            and caller_resolver.qualified_name(item.context_expr.func, use=item.context_expr) == _BEGIN_WRITE_QUALIFIED
        ]
        if len(transactions) != 1 or not isinstance(transactions[0][1].optional_vars, ast.Name):
            return f"{edge.classification} caller must open exactly one begin_write transaction bound to a name"
        transaction, item = transactions[0]
        connection = item.optional_vars.id
        calls = helper_calls_in(caller, caller_key[0], helper_key)
        site_violation = call_site_violation(calls, transaction_scope=transaction, caller=caller)
        if site_violation is not None:
            return f"{edge.classification} {site_violation}"
        if not exact_connection_argument(calls[0], index[helper_key], connection):
            return f"{edge.classification} subordinate call does not receive the exact begin_write connection"
        actual = Counter((site.table, site.operation.removeprefix("raw-")) for site in dml if (site.path, site.symbol) == helper_key)
        expected = Counter({(table, operation): count for table, operation, count in edge.write_counts})
        if actual != expected:
            return f"{edge.classification} write counts drifted: expected={sorted(expected.items())!r} actual={sorted(actual.items())!r}"
        return None

    def fenced_owner_edge_violation(helper_key: tuple[str, str], caller_key: tuple[str, str]) -> str | None:
        caller = index[caller_key]
        caller_violation = _function_fence_violation(caller)
        if caller_violation is not None:
            return f"is not fenced: {caller_violation}"
        fenced_context = _fenced_contexts(caller)[0]
        helper = index[helper_key]
        calls = helper_calls_in(caller, caller_key[0], helper_key)
        site_violation = call_site_violation(calls, transaction_scope=fenced_context.owner, caller=caller)
        if site_violation is not None:
            return site_violation
        if any(not exact_connection_argument(call, helper, fenced_context.connection) for call in calls):
            return "subordinate call does not receive the exact caller-owned conn"
        caller_token = _authority_parameter(caller)

        def bound_to_caller_token(value: ast.expr) -> bool:
            return caller_token is not None and _exact_token_run_id_expression(value, caller_token)

        if any(not run_subjects_bound(call, helper, bound_to=bound_to_caller_token) for call in calls):
            return "subordinate run subject is not exact caller token.run_id"
        return None

    def helper_caller_edge_violation(
        helper_key: tuple[str, str], caller_key: tuple[str, str], stack: frozenset[tuple[str, str]]
    ) -> str | None:
        """A conn-helper calling another conn-helper: the caller must itself be admitted, on its own conn."""

        caller = index[caller_key]
        helper = index[helper_key]
        caller_reason = helper_admission(caller_key, stack)
        if caller_reason is not None:
            return f"is reached through an unadmitted subordinate helper: {caller_reason}"
        calls = helper_calls_in(caller, caller_key[0], helper_key)
        site_violation = call_site_violation(calls, transaction_scope=caller, caller=caller)
        if site_violation is not None:
            return site_violation
        if any(not exact_connection_argument(call, helper, "conn") for call in calls):
            return "subordinate call does not receive the exact caller-owned conn"
        caller_run_parameters = {
            argument.arg
            for argument in (*caller.args.posonlyargs, *caller.args.args, *caller.args.kwonlyargs)
            if argument.arg.endswith("run_id") and not _parameter_rebound(caller, argument.arg)
        }

        def bound_to_caller_parameter(value: ast.expr) -> bool:
            return isinstance(value, ast.Name) and value.id in caller_run_parameters

        if any(not run_subjects_bound(call, helper, bound_to=bound_to_caller_parameter) for call in calls):
            return "subordinate run subject is not exact caller token.run_id"
        return None

    def edge_violation(helper_key: tuple[str, str], caller_key: tuple[str, str], stack: frozenset[tuple[str, str]]) -> str | None:
        if caller_key not in index:
            return "is unresolved"
        coordination_path = "src/elspeth/core/landscape/run_coordination_repository.py"
        if helper_key == (coordination_path, "_renew_leader_deadline_on") and caller_key in {
            (coordination_path, "verify_and_extend_leader_fence"),
            (coordination_path, "fenced_leader_transaction"),
        }:
            # The independent clock proof checks executable bodies, exact
            # token/connection forwarding, admission before sampling and the
            # conditional success tail. Other callers still traverse the
            # ordinary all-callers authority proof below.
            return None if deadline_sources_proven else "has no proven admission/renewal/finalization source contract"
        if caller_key in _NON_RUN_DML_WRITERS:
            caller = index[caller_key]
            reason = _non_run_writer_violation(caller, dml)
            if reason is not None:
                return reason
            context = _non_run_writer_context(caller)
            assert context is not None
            calls = helper_calls_in(caller, caller_key[0], helper_key)
            reason = call_site_violation(calls, transaction_scope=context[0], caller=caller)
            if reason is not None:
                return reason
            if any(not exact_connection_argument(call, index[helper_key], context[1]) for call in calls):
                return "non-run writer helper is passed a different connection"
            return None
        if any(helper_key in family and caller_key in family for family in _ESTABLISHMENT_HELPER_SYMBOLS.values()):
            # Both endpoints sit inside one authority-establishment helper
            # graph: every write on the edge is pinned per table by that
            # establishment's exact write counts (_begin_run_edge_violations).
            if (
                helper_key[1]
                in {
                    "RunCoordinationRepository._finalize_leader_registration_on",
                    "RunCoordinationRepository._finalize_follower_admission_on",
                }
                and not deadline_sources_proven
            ):
                return "has no proven deadline registration finalizer source contract"
            return None
        if (caller_key, helper_key) == (
            (_FENCE_REFUSAL_EVIDENCE_EDGE.caller_path, _FENCE_REFUSAL_EVIDENCE_EDGE.caller_symbol),
            (_FENCE_REFUSAL_EVIDENCE_EDGE.helper_path, _FENCE_REFUSAL_EVIDENCE_EDGE.helper_symbol),
        ):
            return evidence_edge_violation(helper_key, caller_key)
        if caller_key in helper_keys:
            return helper_caller_edge_violation(helper_key, caller_key, stack)
        return fenced_owner_edge_violation(helper_key, caller_key)

    def helper_admission(helper_key: tuple[str, str], stack: frozenset[tuple[str, str]]) -> str | None:
        """None when the helper's DML is proven to execute only inside fenced transactions, else the first reason."""

        if helper_key in admitted:
            return admitted[helper_key]
        if helper_key in stack:
            return "subordinate helper chain is cyclic"
        reason = helper_side_violation(helper_key)
        if reason is None:
            callers = sorted(edges_by_helper.get(helper_key, set()))
            if not callers:
                reason = "subordinate raw-Connection helper callers=0 expected>=1"
            for caller_key in callers:
                if reason is not None:
                    break
                caller_reason = edge_violation(helper_key, caller_key, stack | {helper_key})
                if caller_reason is not None:
                    reason = f"subordinate caller {caller_key[0]}:{caller_key[1]} {caller_reason}"
        admitted[helper_key] = reason
        return reason

    for path, symbol in sorted(dml_symbols | helper_keys):
        if (
            journal_sources_proven
            and path == "src/elspeth/core/landscape/journal.py"
            and symbol in {"LandscapeJournal._before_commit", "LandscapeJournal._prepare_commit"}
        ):
            # Only exact reviewed listener/preparer bodies, dependencies and
            # their closed helper edge admit the non-run outbox INSERT.
            continue
        if (path, symbol) in exact_establishment_symbols:
            continue
        node = index.get((path, symbol))
        if node is None:
            violations.append(f"{path}:{symbol} DML owner definition missing")
            continue
        if (path, symbol) in _NON_RUN_DML_WRITERS:
            reason = _non_run_writer_violation(node, dml)
            if reason is not None:
                violations.append(f"{path}:{symbol} {reason}")
            continue
        if (path, symbol) in helper_keys:
            # Every caller edge of a raw-Connection helper must be fenced —
            # a fenced owner passing its exact fenced connection, an admitted
            # helper passing its own exact ``conn`` (transitively), an
            # authority-establishment graph whose writes are pinned, or the
            # one pinned unfenced-evidence edge.  Cardinality is not the
            # property; every execution being inside a proven fence is.
            reason = helper_admission((path, symbol), frozenset())
            if reason is not None:
                violations.append(f"{path}:{symbol} {reason}")
            continue
        violation = _function_fence_violation(node)
        if violation is not None:
            violations.append(f"{path}:{symbol} {violation}")
    return tuple(violations)


@dataclass(frozen=True, slots=True)
class UnfencedEvidenceEdge:
    """One pinned caller->helper edge whose write is EVIDENCE of lost authority, so it cannot be fenced."""

    classification: str
    caller_path: str
    caller_symbol: str
    helper_path: str
    helper_symbol: str
    write_counts: tuple[tuple[str, str, int], ...]
    rationale: str


_BEGIN_WRITE_QUALIFIED = "elspeth.core.landscape.database.begin_write"
_FENCE_REFUSAL_EVIDENCE_EDGE = UnfencedEvidenceEdge(
    classification="fence-refusal-evidence",
    caller_path="src/elspeth/core/landscape/run_coordination_repository.py",
    caller_symbol="_record_best_effort_event",
    helper_path="src/elspeth/core/landscape/run_coordination_repository.py",
    helper_symbol="record_coordination_event",
    write_counts=(("run_coordination_events", "insert", 1),),
    rationale=(
        "ADR-030 §A.2: the fence_refusal / heartbeat_degraded row is written by a writer the fence has JUST "
        "proven holds no authority, on a fresh connection after the refused transaction rolled back; it is "
        "best-effort by design and is the forensic trace that a fence WAS refused."
    ),
)


_ESTABLISHMENT_HELPER_SYMBOLS: dict[str, frozenset[tuple[str, str]]] = {
    "fresh-run-epoch-1-creation": frozenset(
        {
            (_RUN_LIFECYCLE_PATH, "RunLifecycleRepository.begin_run"),
            (_RUN_LIFECYCLE_PATH, "RunLifecycleRepository._insert_web_plugin_policy_evidence"),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._finalize_leader_registration_on",
            ),
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
                "RunCoordinationRepository._finalize_leader_registration_on",
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
    "export-seat-claim": frozenset(
        {
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository.acquire_export_leadership",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._acquire_export_leadership_on",
            ),
            (
                "src/elspeth/core/landscape/run_coordination_repository.py",
                "RunCoordinationRepository._finalize_leader_registration_on",
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
                "RunCoordinationRepository._finalize_follower_admission_on",
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


def _elision_notice(total: int, shown: int, noun: str) -> str:
    """The line that makes a truncated diagnostic say so.

    A diagnostic that silently drops rows is worse than a short one: this gate's
    own instruction is to RE-DERIVE A PIN FROM ITS PRINTED OUTPUT, and a reader
    who counts a truncated list gets a wrong answer with nothing to signal it.
    An elision in evidence handed to someone else is not neutral -- it is a
    choice about what they may conclude, and it is invisible exactly where they
    would need to notice it.
    """
    if total <= shown:
        return ""
    return f"\n  … {total - shown} further {noun} NOT SHOWN ({total} total). Do NOT re-derive a pin from this truncated list."


def _format_violations(title: str, violations: Sequence[str]) -> str:
    shown = 120
    return (
        title
        + f" ({len(violations)}):\n"
        + "\n".join(f"  {item}" for item in violations[:shown])
        + _elision_notice(len(violations), shown, "violations")
    )


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


@pytest.mark.parametrize(
    ("replacement", "admitted"),
    [
        (None, True),
        (("return self.member_token", "return WorkerMembershipToken(run_id='r', worker_id='w')"), False),
        (("if not isinstance(self.member_token, WorkerMembershipToken):", "if self.member_token is None:"), False),
        (("raise RuntimeError('missing')", "pass"), False),
        (("self.member_token = member_token", "self.member_token = WorkerMembershipToken(run_id='r', worker_id='w')"), False),
        (("member_token: WorkerMembershipToken | None", "member_token: object"), False),
        (("member_token: WorkerMembershipToken | None", "member_token: CoordinationToken | None"), False),
        (
            (
                "    def require_member_token",
                "    def retarget(self, value):\n        self.member_token = value\n    def require_member_token",
            ),
            False,
        ),
        (
            (
                "    def require_member_token",
                "    def retarget(self, value: WorkerMembershipToken):\n        self.member_token = value\n    def require_member_token",
            ),
            True,
        ),
        (("alias = ctx.require_member_token()", "alias = WorkerMembershipToken(run_id='r', worker_id='w')"), False),
        (("alias = ctx.require_member_token()", "ctx = impostor\n    alias = ctx.require_member_token()"), False),
        (("class Carrier:", "isinstance = lambda *args: True\nclass Carrier:"), False),
        (
            (
                "self.member_token = member_token",
                "member_token: WorkerMembershipToken = WorkerMembershipToken(run_id='r', worker_id='w')\n        self.member_token = member_token",
            ),
            False,
        ),
        (("self.member_token = member_token", "member_token += forged\n        self.member_token = member_token"), False),
        (
            (
                "alias = ctx.require_member_token()",
                "ctx.member_token = WorkerMembershipToken(run_id='r', worker_id='w')\n    alias = ctx.require_member_token()",
            ),
            False,
        ),
        (
            (
                "alias = ctx.require_member_token()",
                "other = ctx\n    other.member_token = WorkerMembershipToken(run_id='r', worker_id='w')\n    alias = ctx.require_member_token()",
            ),
            False,
        ),
        (("alias = ctx.require_member_token()", "setattr(ctx, 'member_token', forged)\n    alias = ctx.require_member_token()"), False),
    ],
)
def test_authority_getter_proof_checks_nominal_guard_field_origin_and_mutation(replacement: tuple[str, str] | None, admitted: bool) -> None:
    source = textwrap.dedent("""\
        from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
        from elspeth.core.landscape.execution_repository import ExecutionRepository
        class Carrier:
            def __init__(self, member_token: WorkerMembershipToken | None):
                self.member_token = member_token
            def require_member_token(self) -> WorkerMembershipToken:
                if not isinstance(self.member_token, WorkerMembershipToken):
                    raise RuntimeError('missing')
                return self.member_token
        def forward(ctx: Carrier, execution: ExecutionRepository):
            alias = ctx.require_member_token()
            execution.begin_node_state(member_token=alias)
    """)
    if replacement is not None:
        source = source.replace(*replacement)
    unit = _parse_source("src/elspeth/engine/proven_carrier.py", source)
    assert (_caller_authority_violations([unit]) == ()) is admitted


@pytest.mark.parametrize("override", ["", "return forged", "self.member_token = forged\n        return self.member_token"])
def test_authority_getter_proof_resolves_inheritance_and_rejects_overrides(override: str) -> None:
    base = _parse_source(
        "src/elspeth/engine/carrier_base.py",
        textwrap.dedent("""\
        from elspeth.contracts.coordination import WorkerMembershipToken
        class Base:
            def __init__(self, member_token: WorkerMembershipToken | None):
                self.member_token = member_token
            def require_member_token(self) -> WorkerMembershipToken:
                if not isinstance(self.member_token, WorkerMembershipToken):
                    raise RuntimeError('missing')
                return self.member_token
    """),
    )
    body = "    pass\n" if not override else f"    def require_member_token(self) -> WorkerMembershipToken:\n        {override}\n"
    child = _parse_source(
        "src/elspeth/engine/carrier_child.py",
        "from elspeth.engine.carrier_base import Base\n"
        "from elspeth.contracts.coordination import WorkerMembershipToken\n"
        "from elspeth.core.landscape.execution_repository import ExecutionRepository\n"
        "class Child(Base):\n" + body + "def forward(ctx: Child, execution: ExecutionRepository):\n"
        "    execution.begin_node_state(member_token=ctx.require_member_token())\n",
    )
    assert (_caller_authority_violations([base, child]) == ()) is (not override)


@pytest.mark.parametrize(
    ("replacement", "admitted"),
    [
        (None, True),
        (("return self.leader.membership", "return WorkerMembershipToken(run_id='r', worker_id='w')"), False),
        (("if isinstance(self.leader, CoordinationToken):", "if self.leader is not None:"), False),
        (("self.leader = leader", "self.leader = CoordinationToken(run_id='r', leader_worker_id='w', leadership_epoch=1)"), False),
        (("return self.member", "return self.leader"), False),
        (("raise RuntimeError('missing')", "return forged"), False),
    ],
)
def test_authority_getter_proof_checks_leader_membership_projection(replacement: tuple[str, str] | None, admitted: bool) -> None:
    source = textwrap.dedent("""\
        from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
        from elspeth.core.landscape.execution_repository import ExecutionRepository
        class Carrier:
            def __init__(self, leader: CoordinationToken | None, member: WorkerMembershipToken | None):
                self.leader = leader
                self.member = member
            def require_member_token(self) -> WorkerMembershipToken:
                if isinstance(self.leader, CoordinationToken):
                    return self.leader.membership
                if not isinstance(self.member, WorkerMembershipToken):
                    raise RuntimeError('missing')
                return self.member
        def forward(ctx: Carrier, execution: ExecutionRepository):
            execution.begin_node_state(member_token=ctx.require_member_token())
    """)
    if replacement is not None:
        source = source.replace(*replacement)
    unit = _parse_source("src/elspeth/engine/projected_carrier.py", source)
    assert (_caller_authority_violations([unit]) == ()) is admitted


@pytest.mark.parametrize(
    ("body", "admitted"),
    [
        ("self.execution.begin_node_state(member_token=self.member)", True),
        ("pass", False),
        ("self.execution.begin_node_state(member_token=forged)", False),
        ("unknown.begin_node_state(member_token=self.member)", False),
        ("self.execution.begin_node_state(member_token=self.member, **extra)", False),
        ("self.execution.begin_node_state(member_token=self.member)\n        self.execution.begin_node_state(member_token=forged)", False),
    ],
)
def test_mutation_forwarder_proof_checks_every_inner_call(body: str, admitted: bool) -> None:
    source = (
        "from elspeth.contracts.coordination import WorkerMembershipToken\n"
        "from elspeth.core.landscape.execution_repository import ExecutionRepository\n"
        "class Wrapper:\n"
        "    def __init__(self, execution: ExecutionRepository, member: WorkerMembershipToken):\n"
        "        self.execution = execution\n"
        "        self.member = member\n"
        "    def begin_node_state(self):\n"
        f"        {body}\n"
        "def forward(wrapper: Wrapper):\n"
        "    wrapper.begin_node_state()\n"
    )
    unit = _parse_source("src/elspeth/engine/proven_forwarder.py", source)
    call = next(node for node in ast.walk(unit.tree) if isinstance(node, ast.Call) and _symbol(node) == "forward")
    assert isinstance(call.func, ast.Attribute)
    assert _proven_mutation_forwarder(call.func.value, call.func.attr, _resolver_for_unit(unit), call, _AuthorityProof((unit,))) is admitted


def _d8_read_relay_unit(caller_source: str, *, rebound_helper: bool = False) -> SourceUnit:
    helper_body = "    query = select(runs_table.c.run_id)\n" if rebound_helper else ""
    return _parse_source(
        "src/elspeth/core/landscape/read_relay.py",
        "from sqlalchemy import Select, select, text, update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def read_rows(conn, query: Select):\n"
        "    if not isinstance(query, Select):\n"
        "        raise TypeError('Select required')\n" + helper_body + "    return conn.execute(query).all()\n" + caller_source,
    )


def test_d8_read_relay_requires_all_callers_to_supply_proven_selects() -> None:
    unit = _d8_read_relay_unit(
        "def first(conn):\n"
        "    return read_rows(conn, select(runs_table.c.run_id).with_for_update())\n"
        "def second(conn):\n"
        "    query = select(runs_table.c.run_id).where(runs_table.c.status == 'running')\n"
        "    return read_rows(conn, query=query)\n"
    )
    assert _proven_read_statement_helpers((unit,)) == {(unit.path, "read_rows")}


@pytest.mark.parametrize(
    "caller_source",
    [
        pytest.param(
            "def caller(conn):\n"
            "    changed = update(runs_table).values(status='failed').returning(runs_table.c.run_id).cte()\n"
            "    return read_rows(conn, select(runs_table.c.run_id).add_cte(changed))\n",
            id="writable-cte",
        ),
        pytest.param(
            "def caller(conn):\n    return read_rows(conn, text('SELECT run_id FROM runs'))\n",
            id="raw-text-even-when-select-shaped",
        ),
        pytest.param(
            "def caller(conn):\n    return read_rows(conn, select(text('run_id')).select_from(runs_table))\n",
            id="raw-text-inside-select-tree",
        ),
        pytest.param(
            "def select(*args):\n"
            "    return update(runs_table).values(status='failed')\n"
            "def caller(conn):\n"
            "    return read_rows(conn, select(runs_table.c.run_id))\n",
            id="rebound-select-constructor",
        ),
        pytest.param(
            "def caller(conn, query):\n    return read_rows(conn, query)\n",
            id="unknown-parameter",
        ),
        pytest.param("", id="no-callers"),
        pytest.param(
            "def caller(conn, supplied):\n"
            "    query = select(runs_table.c.run_id)\n"
            "    query = supplied\n"
            "    return read_rows(conn, query)\n",
            id="caller-rebinding",
        ),
        pytest.param(
            "def first(conn):\n"
            "    return read_rows(conn, select(runs_table.c.run_id))\n"
            "def second(conn, supplied):\n"
            "    return read_rows(conn, supplied)\n",
            id="second-caller-is-unproven",
        ),
    ],
)
def test_d8_read_relay_refuses_unproven_callsite_queries(caller_source: str) -> None:
    unit = _d8_read_relay_unit(caller_source)
    assert (unit.path, "read_rows") not in _proven_read_statement_helpers((unit,))


def test_d8_read_relay_refuses_rebinding_inside_helper() -> None:
    unit = _d8_read_relay_unit(
        "def caller(conn):\n    return read_rows(conn, select(runs_table.c.run_id))\n",
        rebound_helper=True,
    )
    assert (unit.path, "read_rows") not in _proven_read_statement_helpers((unit,))


def _d8_reexport_read_units(export_source: str) -> tuple[SourceUnit, ...]:
    provider = _parse_source(
        "src/elspeth/core/landscape/read_provider.py",
        "from sqlalchemy import Select\n"
        "class Reader:\n"
        "    def read_rows(self, conn, query: Select):\n"
        "        if not isinstance(query, Select):\n"
        "            raise TypeError('Select required')\n"
        "        return conn.execute(query).all()\n",
    )
    package = _parse_source("src/elspeth/core/landscape/read_package/__init__.py", export_source)
    caller = _parse_source(
        "src/elspeth/core/landscape/read_caller.py",
        "from sqlalchemy import select\n"
        "from elspeth.core.landscape.read_package import Reader\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "class Facade:\n"
        "    def __init__(self):\n"
        "        self.reader = Reader()\n"
        "    def read(self, conn):\n"
        "        return self.reader.read_rows(conn, select(runs_table.c.run_id))\n",
    )
    return provider, package, caller


def test_d8_read_relay_resolves_exact_public_package_class_reexport() -> None:
    units = _d8_reexport_read_units("from elspeth.core.landscape.read_provider import Reader\n")
    assert _owned_reexports(units) == {"elspeth.core.landscape.read_package.Reader": "elspeth.core.landscape.read_provider.Reader"}
    assert _proven_read_statement_helpers(units) == {(units[0].path, "Reader.read_rows")}


@pytest.mark.parametrize(
    "export_source",
    [
        pytest.param("class Reader:\n    pass\n", id="package-local-impostor"),
        pytest.param("from elspeth.core.landscape.impostor import Reader\n", id="different-owned-module"),
        pytest.param("from foreign_reader import Reader\n", id="foreign-module-impostor"),
        pytest.param(
            "from elspeth.core.landscape.read_provider import Reader\nReader = replacement\n",
            id="export-rebound-after-import",
        ),
    ],
)
def test_d8_read_relay_refuses_impostor_public_package_export(export_source: str) -> None:
    units = _d8_reexport_read_units(export_source)
    assert _owned_reexports(units).get("elspeth.core.landscape.read_package.Reader") != ("elspeth.core.landscape.read_provider.Reader")
    assert (units[0].path, "Reader.read_rows") not in _proven_read_statement_helpers(units)


def _heartbeat_guard_source() -> str:
    return textwrap.dedent("""\
        from contextlib import contextmanager
        from sqlalchemy import select
        from elspeth.contracts.coordination import WorkerMembershipToken
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.core.landscape.database import begin_write
        from elspeth.core.landscape.schema import run_coordination_table, runs_table
        from elspeth.core.landscape.run_coordination_repository import verify_membership_fence

        @contextmanager
        def fenced_heartbeat_transaction(engine, *, member_token: WorkerMembershipToken, verb: str):
            if not isinstance(member_token, WorkerMembershipToken):
                raise TypeError("worker heartbeat requires a WorkerMembershipToken")
            with begin_write(engine) as conn:
                _bound_heartbeat_statement_waits(conn)
                locked_seat = conn.execute(
                    select(run_coordination_table.c.run_id).where(run_coordination_table.c.run_id == member_token.run_id).with_for_update()
                ).one_or_none()
                verify_membership_fence(conn, member_token=member_token, verb=verb)
                if locked_seat is None:
                    raise AuditIntegrityError(f"Run {member_token.run_id!r} has registered membership but no coordination seat")
                yield conn
        """)


def test_heartbeat_guard_admits_the_exact_seat_then_member_implementation() -> None:
    unit = _parse_source(_RUN_COORDINATION_PATH, _heartbeat_guard_source())
    assert _heartbeat_fence_implementation_violations((unit,)) == ()
    assert _heartbeat_fence_implementation_violations(()) == ()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("        verify_membership_fence(conn, member_token=member_token, verb=verb)\n", ""),
        (
            "        locked_seat = conn.execute(\n            select(run_coordination_table.c.run_id).where(run_coordination_table.c.run_id == member_token.run_id).with_for_update()\n        ).one_or_none()\n",
            "",
        ),
        (".with_for_update()", ""),
        ("member_token=member_token, verb=verb)", "member_token=other_token, verb=verb)"),
        ("yield conn", "yield other_conn"),
        ("yield conn", "conn.execute(runs_table.delete())\n        yield conn"),
        ("_bound_heartbeat_statement_waits(conn)", "pass"),
    ],
    ids=["removed-verifier", "removed-seat", "removed-seat-lock", "wrong-member", "wrong-yield", "extra-dml", "removed-wait-bound"],
)
def test_heartbeat_guard_rejects_implementation_drift(before: str, after: str) -> None:
    source = _heartbeat_guard_source()
    assert source.count(before) == 1
    unit = _parse_source(_RUN_COORDINATION_PATH, source.replace(before, after))
    assert _heartbeat_fence_implementation_violations((unit,)) == (
        "heartbeat fence implementation no longer proves seat lock, membership check, and exact connection before yield",
    )


def test_heartbeat_guard_rejects_membership_verification_before_the_seat_lock() -> None:
    source = _heartbeat_guard_source()
    verifier = "        verify_membership_fence(conn, member_token=member_token, verb=verb)\n"
    source = source.replace(verifier, "").replace("        locked_seat =", verifier + "        locked_seat =")
    unit = _parse_source(_RUN_COORDINATION_PATH, source)
    assert _heartbeat_fence_implementation_violations((unit,))


def test_heartbeat_guard_rejects_an_uninspected_or_duplicate_implementation() -> None:
    caller = _parse_source(
        "src/elspeth/core/landscape/heartbeat_caller.py",
        "from elspeth.core.landscape.run_coordination_repository import fenced_heartbeat_transaction\n"
        "def heartbeat(engine, member_token):\n"
        "    with fenced_heartbeat_transaction(engine, member_token=member_token, verb='heartbeat'):\n"
        "        pass\n",
    )
    implementation = _parse_source(_RUN_COORDINATION_PATH, _heartbeat_guard_source())
    expected = ("heartbeat fence requires exactly one inspected implementation",)
    assert _heartbeat_fence_implementation_violations((caller,)) == expected
    assert _heartbeat_fence_implementation_violations((implementation, implementation, caller)) == expected


def _auth_writer_guard_source(statement: str) -> SourceUnit:
    return _parse_source(
        "src/elspeth/core/landscape/auth_audit_repository.py",
        textwrap.dedent("""\
            from elspeth.core.landscape.database import LandscapeDB
            from elspeth.core.landscape.schema import auth_events_table, runs_table
            class AuthAuditRepository:
                def __init__(self, db: LandscapeDB):
                    self._db = db
                def _insert_auth_events(self, values):
                    with self._db.write_connection() as conn:
        """)
        + textwrap.indent(statement, "            ")
        + "\n",
    )


def _non_run_guard_reason(unit: SourceUnit, symbol: str) -> str | None:
    node = next(node for node in ast.walk(unit.tree) if isinstance(node, ast.FunctionDef) and _symbol(node) == symbol)
    return _non_run_writer_violation(node, scan_dml_identities((unit,)))


def test_auth_event_guard_admits_the_exact_insert_on_its_database_connection() -> None:
    unit = _auth_writer_guard_source("conn.execute(auth_events_table.insert(), values)")
    assert _non_run_guard_reason(unit, "AuthAuditRepository._insert_auth_events") is None


@pytest.mark.parametrize(
    "statement",
    [
        "conn.execute(runs_table.insert(), values)",
        "other.execute(auth_events_table.insert(), values)",
        "conn.execute(auth_events_table.update(), values)",
        "conn.execute(auth_events_table.delete())",
        "conn.execute(auth_events_table.insert(), values)\nconn.execute(runs_table.delete())",
        "conn.execute(auth_events_table.insert(), values)\nconn.execute(auth_events_table.insert(), values)",
    ],
    ids=["wrong-table", "wrong-connection", "new-update", "new-delete", "extra-table", "duplicate-insert"],
)
def test_auth_event_guard_rejects_write_set_or_connection_drift(statement: str) -> None:
    unit = _auth_writer_guard_source(statement)
    assert _non_run_guard_reason(unit, "AuthAuditRepository._insert_auth_events") is not None


def test_auth_event_guard_rejects_payload_execution_outside_its_transaction() -> None:
    unit = _auth_writer_guard_source("pass")
    escaped = _parse_source(unit.path, unit.source + "        conn.execute(auth_events_table.insert(), values)\n")
    assert _non_run_guard_reason(escaped, "AuthAuditRepository._insert_auth_events") is not None


@pytest.mark.parametrize(
    ("annotation", "statement", "admitted"),
    [
        ("Connection", "conn.execute(sidecar_journal_outbox_table.insert(), values)", True),
        ("Connection", "conn.execute(auth_events_table.insert(), values)", False),
        ("Connection", "other.execute(sidecar_journal_outbox_table.insert(), values)", False),
        ("Connection", "conn.execute(sidecar_journal_outbox_table.delete())", False),
        ("object", "conn.execute(sidecar_journal_outbox_table.insert(), values)", False),
        ("Connection", "conn = other\nconn.execute(sidecar_journal_outbox_table.insert(), values)", False),
    ],
    ids=["exact-outbox-insert", "wrong-table", "wrong-connection", "wrong-operation", "untyped-connection", "rebound-connection"],
)
def test_sidecar_insert_guard_is_confined_to_its_typed_connection(annotation: str, statement: str, admitted: bool) -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/journal.py",
        "from sqlalchemy.engine import Connection\n"
        "from elspeth.core.landscape.schema import sidecar_journal_outbox_table, auth_events_table\n"
        "class LandscapeJournal:\n"
        f"    def _before_commit(self, conn: {annotation}, values):\n" + textwrap.indent(statement, "        ") + "\n",
    )
    assert (_non_run_guard_reason(unit, "LandscapeJournal._before_commit") is None) is admitted


def _sidecar_ack_guard_source() -> str:
    return textwrap.dedent("""\
        class LandscapeJournal:
            def _drain_committed_outbox(self, dbapi_connection, sequence):
                cursor = dbapi_connection.cursor()
                if self._dialect_name == "sqlite":
                    placeholder = "?"
                elif self._dialect_name == "postgresql":
                    placeholder = "%s"
                else:
                    raise RuntimeError("unsupported dialect")
                cursor.execute(f"DELETE FROM sidecar_journal_outbox WHERE sequence = {placeholder} AND journal_owner = {placeholder}", (sequence, self._owner_key))
        """)


def _sidecar_ack_guard_admitted(source: str, *, path: str = "src/elspeth/core/landscape/journal.py") -> bool:
    unit = _parse_source(path, source)
    call = next(
        node
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
    )
    return _admitted_non_run_raw_write(call, _resolver_for_unit(unit))


def test_sidecar_ack_guard_admits_only_the_exact_owner_scoped_parameterized_delete() -> None:
    source = _sidecar_ack_guard_source()
    assert _sidecar_ack_guard_admitted(source)
    assert not _sidecar_ack_guard_admitted(source, path="src/elspeth/core/landscape/not_journal.py")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("DELETE FROM sidecar_journal_outbox", "DELETE FROM auth_events"),
        (" AND journal_owner = {placeholder}", ""),
        ("(sequence, self._owner_key)", "(sequence, other_owner)"),
        ("cursor.execute(", "other_cursor.execute("),
        ("sequence = {placeholder}", "sequence = {sequence}"),
        ('placeholder = "?"', "placeholder = request_placeholder"),
        ('placeholder = "%s"', 'placeholder = "?"'),
        ('placeholder = "%s"', 'placeholder = "%s"\n            placeholder = "%s"'),
        ("def _drain_committed_outbox(", "def _other_writer("),
    ],
    ids=[
        "wrong-table",
        "missing-owner-predicate",
        "wrong-owner-parameter",
        "wrong-cursor",
        "interpolated-sequence",
        "dynamic-placeholder",
        "missing-postgresql-placeholder",
        "extra-placeholder-write",
        "wrong-method",
    ],
)
def test_sidecar_ack_guard_rejects_exemption_shape_drift(before: str, after: str) -> None:
    source = _sidecar_ack_guard_source()
    assert source.count(before) == 1
    assert not _sidecar_ack_guard_admitted(source.replace(before, after))


@pytest.mark.parametrize(
    ("statement", "admitted"),
    [
        ('conn.exec_driver_sql(f"PRAGMA user_version = {int(epoch)}")', True),
        ('conn.exec_driver_sql(f"PRAGMA user_version = {epoch}")', False),
        ('other.exec_driver_sql(f"PRAGMA user_version = {int(epoch)}")', False),
        ('conn.exec_driver_sql(f"PRAGMA schema_version = {int(epoch)}")', False),
    ],
)
def test_schema_stamp_guard_preserves_the_integer_conversion_and_exact_connection(statement: str, admitted: bool) -> None:
    unit = _parse_source(
        "src/elspeth/core/landscape/database.py",
        "from elspeth.core.landscape.database import begin_write\n"
        "class LandscapeDB:\n"
        "    def _set_sqlite_schema_epoch(self, engine, epoch):\n"
        "        with begin_write(engine) as conn:\n" + textwrap.indent(statement, "            ") + "\n",
    )
    call = next(node for node in ast.walk(unit.tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute))
    assert _admitted_non_run_raw_write(call, _resolver_for_unit(unit)) is admitted


def _establishment_test_call(source: str, method: str, *, path: str = "src/elspeth/engine/orchestrator/join_admission.py") -> ast.Call:
    unit = _parse_source(path, textwrap.dedent(source))
    return next(
        node
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method
    )


def _production_join_shape() -> str:
    return textwrap.dedent("""\
        from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, mint_worker_id
        from elspeth.core.canonical import stable_hash
        from elspeth.core.config import resolve_config
        class JoinAdmissionService:
            def join_run(self, factory, run_id, settings, *, window_seconds: float | None = None):
                _window = window_seconds if window_seconds is not None else DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
                joiner_config_hash = stable_hash(resolve_config(settings))
                worker_id = mint_worker_id(run_id)
                return factory.run_coordination.admit_follower(run_id=run_id, worker_id=worker_id, config_hash=joiner_config_hash, window_seconds=_window)
        """)


def test_establishment_recognizes_real_join_recipes_and_owned_import_aliases() -> None:
    source = _production_join_shape()
    assert _establishment_call_shape_violation("admit_follower", _establishment_test_call(source, "admit_follower")) is None
    aliases = source.replace("import stable_hash", "import stable_hash as hash_settings").replace("= stable_hash(", "= hash_settings(")
    assert _establishment_call_shape_violation("admit_follower", _establishment_test_call(aliases, "admit_follower")) is None


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("mint_worker_id(run_id)", "mint_worker_id(other_run_id)"),
        ("resolve_config(settings)", "resolve_config(other_settings)"),
        ("elspeth.core.canonical import stable_hash", "untrusted.hashing import stable_hash"),
        ("elspeth.core.config import resolve_config", "untrusted.config import resolve_config"),
        (
            "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, mint_worker_id",
            "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS\nfrom untrusted.ids import mint_worker_id",
        ),
        (
            "window_seconds if window_seconds is not None else DEFAULT_RUN_LIVENESS_WINDOW_SECONDS",
            "window_seconds or DEFAULT_RUN_LIVENESS_WINDOW_SECONDS",
        ),
        (
            "window_seconds if window_seconds is not None else DEFAULT_RUN_LIVENESS_WINDOW_SECONDS",
            "window_seconds if window_seconds else DEFAULT_RUN_LIVENESS_WINDOW_SECONDS",
        ),
        ("window_seconds if window_seconds is not None else DEFAULT_RUN_LIVENESS_WINDOW_SECONDS", "0"),
        ("joiner_config_hash = stable_hash(resolve_config(settings))", "joiner_config_hash = supplied_hash"),
        ("worker_id = mint_worker_id(run_id)", "if conditional:\n            worker_id = mint_worker_id(run_id)"),
        ("worker_id = mint_worker_id(run_id)", "worker_id = mint_worker_id(run_id)\n        worker_id = attacker_worker"),
        ("_window = window_seconds", "settings = other_settings\n        _window = window_seconds"),
        ("return factory.run_coordination", "run_id = other_run_id\n        return factory.run_coordination"),
    ],
    ids=[
        "foreign-run-mint",
        "foreign-settings-hash",
        "foreign-hash-import",
        "foreign-resolver-import",
        "foreign-mint-import",
        "truthiness-default-swallows-zero",
        "truthiness-test-swallows-zero",
        "literal-zero",
        "supplied-hash",
        "non-dominating-worker",
        "worker-rebound",
        "settings-rebound",
        "run-rebound",
    ],
)
def test_establishment_rejects_follower_recipe_or_subject_drift(before: str, after: str) -> None:
    source = _production_join_shape()
    assert source.count(before) == 1
    assert (
        _establishment_call_shape_violation("admit_follower", _establishment_test_call(source.replace(before, after), "admit_follower"))
        is not None
    )


@pytest.mark.parametrize("window", ["window_seconds", "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS"])
def test_establishment_preserves_required_resume_window_and_owned_default(window: str) -> None:
    source = (
        "from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS\n"
        "def f(repo, snapshot, window_seconds):\n"
        "    return repo.acquire_run_leadership(run_id=snapshot.run_id, worker_id=snapshot.worker_id, "
        f"window_seconds={window}, entry_point='resume')\n"
    )
    call = _establishment_test_call(source, "acquire_run_leadership", path="src/elspeth/engine/orchestrator/resume.py")
    assert _establishment_call_shape_violation("acquire_run_leadership", call) is None
    with_clock = ast.Call(func=call.func, args=call.args, keywords=[*call.keywords, ast.keyword(arg="now", value=ast.Name(id="now"))])
    assert _establishment_call_shape_violation("acquire_run_leadership", with_clock) is None


def test_establishment_rejects_shadowed_default_and_mismatched_resume_snapshot() -> None:
    source = (
        "from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS\n"
        "def f(repo, snapshot, other):\n"
        "    return repo.acquire_run_leadership(run_id=snapshot.run_id, worker_id=snapshot.worker_id, "
        "window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, entry_point='resume')\n"
    )
    for mutated in (
        source.replace("worker_id=snapshot.worker_id", "worker_id=other.worker_id"),
        source.replace("    return repo", "    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = 0\n    return repo"),
        source.replace("elspeth.contracts.coordination import", "untrusted.constants import"),
    ):
        call = _establishment_test_call(mutated, "acquire_run_leadership", path="src/elspeth/engine/orchestrator/resume.py")
        assert _establishment_call_shape_violation("acquire_run_leadership", call) is not None


def test_establishment_orphan_extraction_is_one_named_helper_with_one_run_bound_mint() -> None:
    source = (
        "from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, mint_worker_id\n"
        "def _acquire_orphaned_run_leadership(repositories, *, run_id):\n"
        "    return repositories.run_coordination.acquire_run_leadership(run_id=run_id, worker_id=mint_worker_id(run_id), "
        "window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, entry_point='orphan-finalize')\n"
    )
    call = _establishment_test_call(source, "acquire_run_leadership", path="src/elspeth/web/app.py")
    assert _establishment_call_shape_violation("acquire_run_leadership", call) is None
    for mutated in (
        source.replace("mint_worker_id(run_id)", "mint_worker_id(other)"),
        source.replace("def _acquire_orphaned_run_leadership", "def other_helper"),
    ):
        call = _establishment_test_call(mutated, "acquire_run_leadership", path="src/elspeth/web/app.py")
        assert _establishment_call_shape_violation("acquire_run_leadership", call) is not None


def _heartbeat_degraded_evidence_shape() -> str:
    return textwrap.dedent("""\
        from elspeth.contracts.coordination import WorkerMembershipToken
        class RunHeartbeatThread:
            def __init__(self, repo, member_token: WorkerMembershipToken):
                self._repo = repo
                self._token = member_token
            def _emit_degraded(self):
                self._repo.record_heartbeat_degraded(member_token=self._token, failures=self._consecutive_busy, now=self._now_fn())
        """)


@pytest.mark.parametrize(
    ("before", "after", "admitted"),
    [
        ("member_token=self._token", "member_token=self._token", True),
        ("member_token=self._token", "member_token=other_token", False),
        ("failures=self._consecutive_busy", "failures=0", False),
        ("now=self._now_fn()", "now=other_clock()", False),
        ("member_token=self._token", "run_id=self._token.run_id, worker_id=self._token.worker_id", False),
    ],
    ids=["exact-forensic-membership", "arbitrary-identity", "invented-counter", "invented-clock", "ambient-scalar-identities"],
)
def test_heartbeat_degraded_evidence_keeps_identity_separate_from_liveness_fencing(before: str, after: str, admitted: bool) -> None:
    unit = _parse_source("src/elspeth/engine/orchestrator/heartbeat.py", _heartbeat_degraded_evidence_shape().replace(before, after))
    call = next(
        node
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "record_heartbeat_degraded"
    )
    assert (_heartbeat_degraded_evidence_call_violation(unit, call, _AuthorityProof((unit,))) is None) is admitted


def test_subordinate_helper_graph_checks_external_production_callers_by_source_owner() -> None:
    helper = _parse_source(
        "src/elspeth/core/landscape/external_helper.py",
        "from sqlalchemy import insert\n"
        "from sqlalchemy.engine import Connection\n"
        "from elspeth.core.landscape.schema import tokens_table\n"
        "class Writer:\n"
        "    def insert_on(self, conn: Connection, *, run_id: str):\n"
        "        conn.execute(insert(tokens_table).values(run_id=run_id))\n",
    )
    caller = _parse_source(
        "src/elspeth/engine/external_helper_caller.py",
        "from elspeth.core.landscape.external_helper import Writer\n"
        "def unfenced(writer: Writer, conn):\n"
        "    writer.insert_on(conn, run_id='foreign')\n",
    )
    units = (helper, caller)
    dml = scan_dml_identities(units)
    edges = _subordinate_helper_edges(units, dml)
    assert [(edge.caller_path, edge.caller_symbol) for edge in edges] == [(caller.path, "unfenced")]
    assert any("unfenced" in violation and "not fenced" in violation for violation in _transaction_order_violations(units, dml))

    unrelated = _parse_source(
        "src/elspeth/engine/unrelated_recorder.py",
        "class Other:\n"
        "    def insert_on(self, value):\n"
        "        return value\n"
        "def unrelated(other: Other):\n"
        "    other.insert_on('display only')\n",
    )
    expanded = (*units, unrelated)
    assert _subordinate_helper_edges(expanded, dml) == edges
    assert _subordinate_helper_resolution_violations(expanded, dml) == ()


@pytest.mark.parametrize(
    "fault",
    [None, "optional_none", "minted_default", "dictionary_mutation", "foreign_projection", "leader_mutation"],
)
def test_carried_authority_rejects_default_mints_and_projected_field_substitution(fault: str | None) -> None:
    source = textwrap.dedent("""
        from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
        from elspeth.core.landscape.execution_repository import ExecutionRepository
        class Carrier:
            def __init__(self, leader: CoordinationToken | None, member: WorkerMembershipToken | None):
                self.leader = leader
                self.member = member
            def require_member_token(self) -> WorkerMembershipToken:
                if isinstance(self.leader, CoordinationToken):
                    return self.leader.membership
                if not isinstance(self.member, WorkerMembershipToken):
                    raise RuntimeError('missing')
                return self.member
        def forward(ctx: Carrier, execution: ExecutionRepository):
            execution.begin_node_state(member_token=ctx.require_member_token())
    """)
    if fault == "optional_none":
        source = source.replace(
            "leader: CoordinationToken | None, member: WorkerMembershipToken | None",
            "leader: CoordinationToken | None = None, member: WorkerMembershipToken | None = None",
        )
    elif fault == "minted_default":
        source = source.replace(
            "member: WorkerMembershipToken | None)",
            "member: WorkerMembershipToken | None = WorkerMembershipToken(run_id='forged', worker_id='forged'))",
        )
    elif fault == "dictionary_mutation":
        source = source.replace(
            "    execution.begin_node_state",
            "    ctx.__dict__['member'] = WorkerMembershipToken(run_id='forged', worker_id='forged')\n    execution.begin_node_state",
        )
    elif fault == "foreign_projection":
        source = source.replace("if isinstance(self.leader,", "if isinstance(foreign.leader,").replace(
            "return self.leader.membership", "return foreign.leader.membership"
        )
    elif fault == "leader_mutation":
        source = source.replace(
            "    execution.begin_node_state",
            "    ctx.leader = CoordinationToken(run_id='forged', leader_worker_id='forged', leadership_epoch=1)\n    execution.begin_node_state",
        )
    unit = _parse_source("src/elspeth/engine/adversarial_carrier.py", source)
    assert bool(_caller_authority_violations((unit,))) is (fault not in {None, "optional_none"})


@pytest.mark.parametrize(
    ("replacement", "admitted"),
    [
        (None, True),
        (("frozen=True", "frozen=False"), False),
        (("return Carrier(member_token=member_token)", "return forged"), False),
        (
            ("return Carrier(member_token=member_token)", "return Carrier(member_token=WorkerMembershipToken(run_id='r', worker_id='w'))"),
            False,
        ),
        (("carrier = build(member_token)", "carrier = build(WorkerMembershipToken(run_id='r', worker_id='w'))"), False),
        (("carrier = build(member_token)", "carrier = build(**unproven)"), False),
        (("return Carrier(member_token=member_token)", "return Carrier()"), False),
    ],
)
def test_returned_authority_field_proves_frozen_constructor_and_actual_argument(
    replacement: tuple[str, str] | None, admitted: bool
) -> None:
    source = textwrap.dedent("""\
        from dataclasses import dataclass
        from elspeth.contracts.coordination import WorkerMembershipToken
        from elspeth.core.landscape.execution_repository import ExecutionRepository
        @dataclass(frozen=True)
        class Carrier:
            member_token: WorkerMembershipToken
        def build(member_token: WorkerMembershipToken) -> Carrier:
            return Carrier(member_token=member_token)
        def forward(execution: ExecutionRepository, member_token: WorkerMembershipToken):
            carrier = build(member_token)
            execution.begin_node_state(member_token=carrier.member_token)
    """)
    if replacement is not None:
        source = source.replace(*replacement)
    unit = _parse_source("src/elspeth/engine/returned_carrier.py", source)
    assert (_caller_authority_violations((unit,)) == ()) is admitted


@pytest.mark.parametrize(
    ("fault", "admitted"),
    [
        ("valid_repository", True),
        ("valid_factory", True),
        ("valid_writable_factory", True),
        ("foreign_run", False),
        ("foreign_worker", False),
        ("worker_rebound", False),
        ("run_rebound", False),
        ("epoch_two", False),
        ("epoch_bool", False),
        ("unknown_receiver", False),
        ("annotated_receiver_only", False),
        ("fake_import", False),
        ("missing_admission", False),
        ("swallowed_admission", False),
        ("conditional_admission", False),
        ("method_rebound", False),
        ("result_mutated", False),
        ("identity_setattr", False),
        ("nonlocal_worker", False),
        ("expanded_keywords", False),
    ],
)
def test_fresh_epoch_one_admission_subject_proof(fault, admitted):
    imports = (
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository\n"
        "from elspeth.core.landscape.factory import RecorderFactory\n"
    )
    receiver = "repo"
    statements = ["worker = 'fresh-worker'", "repo = RunLifecycleRepository(db)"]
    if fault == "valid_factory":
        statements[1] = "repo = RecorderFactory(db)"
        receiver += ".run_lifecycle"
    elif fault == "valid_writable_factory":
        statements[1] = "repo = RecorderFactory.writable(db)"
        receiver += ".run_lifecycle"
    elif fault == "unknown_receiver":
        statements[1] = "repo = unknown(db)"
    elif fault == "annotated_receiver_only":
        statements[1] = "repo: RunLifecycleRepository = unknown(db)"
    elif fault == "fake_import":
        imports = imports.replace("elspeth.core.landscape.run_lifecycle_repository", "attacker")
    if fault == "method_rebound":
        statements.append("repo.begin_run = unknown")
    begin = f"run = {receiver}.begin_run(config={{}}, canonical_version='v1', leader_worker_id=worker)"
    if fault == "swallowed_admission":
        statements.append("try:\n        " + begin + "\n    except Exception:\n        pass")
    elif fault == "conditional_admission":
        statements.append("if db:\n        " + begin)
    elif fault != "missing_admission":
        statements.append(begin)
    run = "run.run_id"
    worker = "worker"
    epoch = "1"
    if fault == "foreign_run":
        run = "other.run_id"
    elif fault == "foreign_worker":
        worker = "other_worker"
    elif fault == "worker_rebound":
        statements.append("worker = 'other-worker'")
    elif fault == "run_rebound":
        statements.append("run = other")
    elif fault == "epoch_two":
        epoch = "2"
    elif fault == "epoch_bool":
        epoch = "True"
    elif fault == "result_mutated":
        statements.append("run.run_id = other.run_id")
    elif fault == "identity_setattr":
        statements.append("setattr(run, 'run_id', other.run_id)")
    elif fault == "nonlocal_worker":
        statements.append("def rebind():\n        nonlocal worker\n        worker = 'other-worker'")
    kwargs = f"run_id={run}, worker_id={worker}, leader_epoch={epoch}"
    if fault == "expanded_keywords":
        kwargs += ", **extra"
    statements.append(f"token = CoordinationToken({kwargs})")
    source = imports + "def caller(db, other, other_worker, extra):\n    " + "\n    ".join(statements) + "\n"
    unit = _parse_source("src/elspeth/web/admission_probe.py", source)
    constructor = next(node for node in ast.walk(unit.tree) if isinstance(node, ast.Call) and _call_name(node) == "CoordinationToken")
    owner = _owner_function(constructor)
    assert owner is not None
    assert _fresh_epoch_one_creation_authority(constructor, owner, _resolver_for_unit(unit), _LEADER_SCOPE) is admitted


@pytest.mark.parametrize("fault", [None, "rebind", "nonlocal", "conditional", "swallowed"])
def test_captured_fresh_authority_requires_one_dominating_unchanged_binding(fault: str | None) -> None:
    source = textwrap.dedent("""\
        from elspeth.contracts.coordination import CoordinationToken
        from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository
        def outer(db):
            worker = 'fresh-worker'
            repo = RunLifecycleRepository(db)
            run = repo.begin_run(config={}, canonical_version='v1', leader_worker_id=worker)
            token = CoordinationToken(run_id=run.run_id, worker_id=worker, leader_epoch=1)
            member = token.membership
            def callback():
                return member
            return callback
    """)
    if fault == "rebind":
        source = source.replace("    return callback", "    member = forged\n    return callback")
    elif fault == "nonlocal":
        source = source.replace("        return member", "        nonlocal member\n        member = forged\n        return member")
    elif fault == "conditional":
        source = source.replace("    member = token.membership", "    if db:\n        member = token.membership")
    elif fault == "swallowed":
        source = source.replace(
            "    member = token.membership", "    try:\n        member = token.membership\n    except Exception:\n        pass"
        )
    unit = _parse_source("src/elspeth/web/captured_authority.py", source)
    callback = next(part for part in ast.walk(unit.tree) if isinstance(part, ast.FunctionDef) and part.name == "callback")
    returned = next(part for part in callback.body if isinstance(part, ast.Return))
    assert returned.value is not None
    assert _AuthorityProof((unit,)).expression(returned.value, callback, _resolver_for_unit(unit), returned, _MEMBER_SCOPE) is (
        fault is None
    )


def test_ambiguous_relay_call_invalidates_every_candidate_read_proof():
    path = "src/elspeth/core/landscape/read_relay_review.py"
    source = textwrap.dedent("""
        from sqlalchemy import Select, select
        from elspeth.core.landscape.schema import runs_table
        def read_rows(conn, query: Select):
            return conn.execute(query).all()
        def read_other(conn, query: Select):
            return conn.execute(query).all()
        def safe_rows(conn):
            return read_rows(conn, select(runs_table.c.run_id))
        def safe_other(conn):
            return read_other(conn, select(runs_table.c.run_id))
    """)
    proven = _proven_read_statement_helpers((_parse_source(path, source),))
    assert proven == {(path, "read_rows"), (path, "read_other")}
    source += textwrap.dedent("""
        def unsafe(conn, supplied, flag):
            fn = read_rows if flag else read_other
            return fn(conn, supplied)
    """)
    assert _proven_read_statement_helpers((_parse_source(path, source),)) == set()


def test_external_rebound_receiver_uses_assignment_before_foreign_annotation():
    helper = _parse_source(
        "src/elspeth/core/landscape/review_writer.py",
        textwrap.dedent("""
        from sqlalchemy import insert
        from sqlalchemy.engine import Connection
        from elspeth.contracts.coordination import CoordinationToken
        from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
        from elspeth.core.landscape.schema import run_coordination_events_table
        class Writer:
            def insert_on(self, conn: Connection, *, run_id: str):
                conn.execute(insert(run_coordination_events_table).values(run_id=run_id))
        def safe(writer: Writer, *, coordination_token: CoordinationToken):
            with fenced_leader_transaction(engine, token=coordination_token) as conn:
                writer.insert_on(conn, run_id=coordination_token.run_id)
    """),
    )
    assert _transaction_order_violations((helper,), scan_dml_identities((helper,))) == ()
    caller_path = "src/elspeth/engine/review_rebound.py"
    caller = _parse_source(
        caller_path,
        textwrap.dedent("""
        from elspeth.core.landscape.review_writer import Writer
        from foreign import Other
        def unfenced(writer: Other, conn):
            writer = Writer()
            writer.insert_on(conn, run_id='foreign')
    """),
    )
    units = (helper, caller)
    dml = scan_dml_identities(units)
    assert (caller_path, "unfenced") in {(edge.caller_path, edge.caller_symbol) for edge in _subordinate_helper_edges(units, dml)}
    violations = _transaction_order_violations(units, dml)
    assert any("unfenced is not fenced" in violation for violation in violations)


def test_inherited_getter_rejects_subclass_authority_field_descriptor():
    path = "src/elspeth/engine/adversarial_carrier.py"
    source = textwrap.dedent("""
        from elspeth.contracts.coordination import WorkerMembershipToken
        from elspeth.core.landscape.execution_repository import ExecutionRepository
        class Carrier:
            def __init__(self, member_token: WorkerMembershipToken | None):
                self.member_token = member_token
            def require_member_token(self) -> WorkerMembershipToken:
                if not isinstance(self.member_token, WorkerMembershipToken):
                    raise RuntimeError('missing')
                return self.member_token
        class Child(Carrier):
            pass
        def forward(ctx: Child, execution: ExecutionRepository):
            execution.begin_node_state(member_token=ctx.require_member_token())
    """)
    assert _caller_authority_violations((_parse_source(path, source),)) == ()
    source = source.replace(
        "class Child(Carrier):\n    pass",
        textwrap.dedent("""
        class Child(Carrier):
            @property
            def member_token(self):
                return WorkerMembershipToken(run_id='forged', worker_id='forged')
            @member_token.setter
            def member_token(self, value):
                pass
    """).strip(),
    )
    assert _caller_authority_violations((_parse_source(path, source),))


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


def test_caller_authority_admits_a_context_that_carries_the_token_by_value_and_nothing_else() -> None:
    """ADR-048 §3: a plugin never holds a token; its context forwards the executor's by value.

    (a) ``ctx.record_*`` is admitted only when ``ctx`` is a parameter annotated with an
    ELSPETH-owned context type and the method is a PluginContext forwarder; (b) inside
    ``PluginContext`` the forwarding call is admitted only when ``self.coordination_token``
    is bound solely in ``__init__`` from an exact token parameter, never minted or
    rebound, with a fail-closed ``is None`` guard above the call.

    Arm (a) is a DEFERRAL, not a grant: four of the admitted annotations are
    ``Protocol`` classes, so the ``bypassing_plugin`` and ``foreign_class`` arms
    below pin that the proof obligation lands on the concrete forwarder instead
    of evaporating.
    """

    plugin = _parse_source(
        "src/elspeth/plugins/sources/probe.py",
        textwrap.dedent(
            """\
            from elspeth.contracts.contexts import SourceContext

            def load(ctx: SourceContext, row):
                ctx.record_validation_error(row=row, error="bad")
            """
        ),
    )
    assert _caller_authority_violations([plugin]) == ()

    untyped_plugin = _parse_source(plugin.path, plugin.source.replace("ctx: SourceContext", "ctx"))
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([untyped_plugin]))

    bypassing_plugin = _parse_source(
        plugin.path, plugin.source.replace("ctx.record_validation_error(", "ctx.landscape.record_validation_error(")
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([bypassing_plugin]))

    context = _parse_source(
        _TOKEN_CARRYING_CONTEXT_OWNER[0],
        textwrap.dedent(
            """\
            from elspeth.contracts.coordination import CoordinationToken

            class PluginContext:
                def __init__(self, landscape, coordination_token: CoordinationToken | None = None):
                    self.landscape = landscape
                    self.coordination_token = coordination_token

                def record_validation_error(self, *, row, error):
                    if self.landscape is None or self.coordination_token is None:
                        raise RuntimeError("no authority")
                    return self.landscape.record_validation_error(row=row, error=error, coordination_token=self.coordination_token)
            """
        ),
    )
    assert _caller_authority_violations([context]) == ()

    minted = _parse_source(
        context.path,
        context.source.replace(
            "coordination_token=self.coordination_token)",
            'coordination_token=CoordinationToken(run_id="r", worker_id="w", leader_epoch=1))',
        ),
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([minted]))

    rebound = _parse_source(
        context.path,
        context.source.replace(
            "    def record_validation_error(self, *, row, error):\n",
            "    def retarget(self, token):\n        self.coordination_token = token\n\n    def record_validation_error(self, *, row, error):\n",
        ),
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([rebound]))

    unguarded = _parse_source(
        context.path,
        context.source.replace(
            '        if self.landscape is None or self.coordination_token is None:\n            raise RuntimeError("no authority")\n',
            "",
        ),
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([unguarded]))

    foreign_class = _parse_source(context.path, context.source.replace("class PluginContext:", "class OtherContext:"))
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([foreign_class]))

    untyped_init = _parse_source(
        context.path, context.source.replace("coordination_token: CoordinationToken | None = None", "coordination_token=None")
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([untyped_init]))

    # The deferral in (a) is bounded to the forwarders PluginContext actually
    # defines. A mutation API it does NOT forward has no second proof site, so
    # admitting it on the strength of the annotation alone would lose the write.
    # The parameter is named ``audit`` deliberately: that is an ``execution``
    # category receiver marker, so the call clears the receiver heuristic on its
    # NAME and reaches the admission arm. A parameter called ``ctx`` would be
    # turned away one step earlier and would prove nothing about this arm.
    assert "begin_operation" in _MUTATION_METHOD_NAMES and "begin_operation" not in _PLUGIN_CONTEXT_METHODS
    assert "audit" in _CATEGORY_RECEIVER_MARKERS["execution"]
    non_forwarder = _parse_source(
        plugin.path,
        plugin.source.replace("ctx: SourceContext", "audit: SourceContext").replace(
            'ctx.record_validation_error(row=row, error="bad")', "audit.begin_operation(row=row)"
        ),
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([non_forwarder]))

    # ...and bounded to the owned context types. Any other annotation, including
    # a sibling Protocol from the same module, proves nothing about forwarding.
    foreign_annotation = _parse_source(
        plugin.path,
        plugin.source.replace("import SourceContext", "import LimiterProtocol").replace("ctx: SourceContext", "ctx: LimiterProtocol"),
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([foreign_annotation]))

    # A rebinder whose parameter is correctly typed still breaks carry-by-value:
    # only ``__init__`` may bind the attribute, or the token is no longer the one
    # the executor handed in. The untyped ``rebound`` arm above cannot see this,
    # because its annotation check fires first.
    annotated_rebinder = _parse_source(
        context.path,
        context.source.replace(
            "    def record_validation_error(self, *, row, error):\n",
            "    def retarget(self, token: CoordinationToken):\n        self.coordination_token = token\n\n"
            "    def record_validation_error(self, *, row, error):\n",
        ),
    )
    assert any("lacks one exact current token" in item for item in _caller_authority_violations([annotated_rebinder]))


def test_non_landscape_receiver_owners_are_pinned_to_the_tree() -> None:
    """Every admitted ``(owner, method)`` pair is re-derived from the tree, not asserted.

    This is what bounds the ``Protocol`` exposure documented on
    ``_NON_LANDSCAPE_RECEIVER_OWNERS``.  An entry survives only while the owner
    really exists, really declares the method, is NOT a Landscape mutation owner,
    and does not live under ``src/elspeth/core/landscape/``.  Move the class,
    rename the method, or point an entry at a Landscape type and this fails.
    """

    units = {unit.path: unit for unit in _production_units()}
    landscape_owners = {(api.path, api.owner) for api in _MUTATION_APIS}

    assert _NON_LANDSCAPE_RECEIVER_OWNERS, "the admission table must not be empty"
    for qualified, method in sorted(_NON_LANDSCAPE_RECEIVER_OWNERS):
        module, _, owner = qualified.rpartition(".")
        path = f"src/{module.replace('.', '/')}.py"

        assert path in units, f"{qualified}: no production unit at {path}"
        assert not path.startswith("src/elspeth/core/landscape/"), f"{qualified}: a Landscape module is never a non-Landscape owner"
        assert (path, owner) not in landscape_owners, f"{qualified}: is a _MUTATION_APIS owner and cannot be admitted"

        # The pair is only ever reached for a name the escape scanner rows.
        assert method in _ALL_MUTATION_METHOD_NAMES, f"{qualified}.{method}: not a Landscape verb name, so the entry is dead"

        declaration = next(
            (node for node in units[path].tree.body if isinstance(node, ast.ClassDef) and node.name == owner),
            None,
        )
        assert declaration is not None, f"{qualified}: no top-level class {owner} in {path}"
        declared = {member.name for member in declaration.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert method in declared, f"{qualified}: {owner} does not declare {method}; the collision claim is stale"


def test_unknown_receiver_admission_is_keyed_on_the_resolved_non_landscape_owner() -> None:
    """A method-name collision is admitted by OWNER; anything unresolvable keeps its row.

    Precision here means naming the owner, not demanding resolution.  The rejected
    alternative — admitting every receiver that resolves — fails closed on exactly
    the rows worth catching: ``LLMAuditParent`` resolves cleanly and is a REAL
    unknown-receiver row that D8.3 must thread.
    """

    # (a) A parameter annotated with a pinned non-Landscape owner, reached from a
    # nested closure the way the planner's attempt trail is.
    planner = _parse_source(
        "src/elspeth/web/composer/pipeline_planner.py",
        textwrap.dedent(
            """\
            class _PlannerAttemptTrail:
                def begin_attempt(self, **fields): ...

            def _plan_pipeline_inner(*, trail: _PlannerAttemptTrail):
                def begin_response_attempt():
                    trail.begin_attempt(planner_call_ordinal=1)
                return begin_response_attempt
            """
        ),
    )
    assert not any("unknown mutation receiver .begin_attempt" in item for item in _mutation_callable_escapes([planner]))

    # (b) ``self.<attr>`` bound once in ``__init__`` from an annotated parameter.
    service = _parse_source(
        "src/elspeth/web/execution/service.py",
        textwrap.dedent(
            """\
            from elspeth.web.sessions.protocol import SessionServiceProtocol

            class ExecutionServiceImpl:
                def __init__(self, *, session_service: SessionServiceProtocol):
                    self._session_service = session_service

                def _fail(self, run_uuid):
                    self._session_service.update_run_status(run_uuid, status="failed")
            """
        ),
    )
    assert not any("unknown mutation receiver .update_run_status" in item for item in _mutation_callable_escapes([service]))

    # The admission is keyed on the PAIR. ``SessionServiceProtocol`` declares 85
    # methods; admitting the owner would carry every future name collision with it.
    other_method = _parse_source(service.path, service.source.replace("update_run_status", "complete_run"))
    assert any("unknown mutation receiver .complete_run" in item for item in _mutation_callable_escapes([other_method]))

    # An UNRESOLVABLE receiver stays a row even when it is NAMED like an admitted
    # one and calls a real Landscape verb. This is the arm that separates an
    # owner-keyed rule from a name-keyed one.
    unresolvable_name = _parse_source(
        planner.path,
        textwrap.dedent(
            """\
            def build(factory):
                trail = factory.make()
                trail.begin_node_state(node="n")
            """
        ),
    )
    assert any("unknown mutation receiver .begin_node_state" in item for item in _mutation_callable_escapes([unresolvable_name]))

    unresolvable_attribute = _parse_source(
        service.path,
        textwrap.dedent(
            """\
            class ExecutionServiceImpl:
                def __init__(self, factory):
                    self._session_service = factory.make()

                def _fail(self, run_uuid):
                    self._session_service.update_run_status(run_uuid, status="failed")
            """
        ),
    )
    assert any("unknown mutation receiver .update_run_status" in item for item in _mutation_callable_escapes([unresolvable_attribute]))

    # An unannotated parameter proves nothing, whatever it is called.
    untyped = _parse_source(planner.path, planner.source.replace("trail: _PlannerAttemptTrail", "trail"))
    assert any("unknown mutation receiver .begin_attempt" in item for item in _mutation_callable_escapes([untyped]))

    # A foreign owned type is not admitted merely because it resolves. This is the
    # ``LLMAuditParent`` shape, and it must keep rowing until D8.3 threads it.
    audit_parent = _parse_source(
        "src/elspeth/plugins/transforms/llm/providers/gateway.py",
        textwrap.dedent(
            """\
            from elspeth.plugins.transforms.llm.provider import LLMAuditParent

            def _record(audit_parent: LLMAuditParent, recorder):
                audit_parent.allocate_call_index(recorder)
            """
        ),
    )
    assert any("unknown mutation receiver .allocate_call_index" in item for item in _mutation_callable_escapes([audit_parent]))

    # A receiver resolving INTO an owned Landscape class is never admitted here.
    landscape_receiver = _parse_source(
        service.path,
        textwrap.dedent(
            """\
            from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository

            def run(store: RunLifecycleRepository, run_uuid):
                store.update_run_status(run_uuid, status="failed")
            """
        ),
    )
    resolver = _resolver_for_unit(landscape_receiver)
    receivers = [node for node in ast.walk(landscape_receiver.tree) if isinstance(node, ast.Attribute) and node.attr == "update_run_status"]
    assert len(receivers) == 1
    assert _resolved_non_landscape_receiver_owner(receivers[0].value, "update_run_status", resolver, use=receivers[0]) is None
    assert _caller_authority_violations([landscape_receiver])

    # Rebinding the attribute outside ``__init__``, or a ``setattr`` anywhere in the
    # class, breaks the binding proof and restores the row.
    rebound = _parse_source(
        service.path,
        service.source.replace(
            "    def _fail(self, run_uuid):\n",
            "    def retarget(self, other):\n        self._session_service = other\n\n    def _fail(self, run_uuid):\n",
        ),
    )
    assert any("unknown mutation receiver .update_run_status" in item for item in _mutation_callable_escapes([rebound]))

    setattr_class = _parse_source(
        service.path,
        service.source.replace(
            "    def _fail(self, run_uuid):\n",
            '    def retarget(self, other):\n        setattr(self, "_session_service", other)\n\n    def _fail(self, run_uuid):\n',
        ),
    )
    assert any("unknown mutation receiver .update_run_status" in item for item in _mutation_callable_escapes([setattr_class]))


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


def test_outside_landscape_dml_keys_on_the_table_metadata_module_not_the_table_name() -> None:
    """P4-D8 defect 1: Sessions and Landscape both own a ``runs`` table."""

    sessions_runs = _parse_source(
        "src/elspeth/web/coordination/repository.py",
        "from sqlalchemy import insert, update\n"
        "from elspeth.web.sessions.models import runs_table\n"
        "def write(conn):\n"
        "    conn.execute(insert(runs_table).values(id='r'))\n"
        "    conn.execute(update(runs_table).values(status='failed'))\n",
    )
    assert _raw_write_surface_violations([sessions_runs]) == ()

    landscape_runs = _parse_source(
        "src/elspeth/web/composer/tutorial_service.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(conn):\n"
        "    conn.execute(update(runs_table).values(status='failed'))\n",
    )
    assert any("outside Landscape DML update runs" in item for item in _raw_write_surface_violations([landscape_runs]))

    # An unproven origin is NOT proof of another database: the table-name test
    # remains the fail-closed default for anything the scanner cannot bind.
    unproven_origin = _parse_source(
        "src/elspeth/web/reexported_table.py",
        "from sqlalchemy import update\n"
        "from elspeth.web.schema_reexport import runs_table\n"
        "def write(conn):\n"
        "    conn.execute(update(runs_table).values(status='failed'))\n",
    )
    assert any("outside Landscape DML update runs" in item for item in _raw_write_surface_violations([unproven_origin]))

    bound_method_form = _parse_source(
        "src/elspeth/web/coordination/bound_method.py",
        "from elspeth.web.sessions.models import runs_table\n"
        "def write(conn):\n"
        "    conn.execute(runs_table.update().values(status='failed'))\n",
    )
    assert _raw_write_surface_violations([bound_method_form]) == ()


def test_aliased_dml_imports_are_admitted_by_resolved_origin_not_by_spelling() -> None:
    """P4-D8 defect 3: a dialect DML import cannot be written without an alias."""

    dialect_bindings = _parse_source(
        "src/elspeth/core/landscape/execution/dialect_inserts.py",
        "from sqlalchemy.dialects.postgresql import insert as postgresql_insert\n"
        "from sqlalchemy.dialects.sqlite import insert as sqlite_insert\n",
    )
    assert _dml_callable_escape_violations([dialect_bindings]) == ()

    non_canonical_alias = _parse_source(
        "src/elspeth/core/landscape/execution/non_canonical.py",
        "from sqlalchemy.dialects.sqlite import insert as fast_insert\n",
    )
    assert any("aliased DML import insert as fast_insert" in item for item in _dml_callable_escape_violations([non_canonical_alias]))

    core_origin_wearing_a_dialect_name = _parse_source(
        "src/elspeth/core/landscape/execution/core_origin.py",
        "from sqlalchemy import insert as postgresql_insert\n",
    )
    assert any(
        "aliased DML import insert as postgresql_insert" in item
        for item in _dml_callable_escape_violations([core_origin_wearing_a_dialect_name])
    )

    project_origin = _parse_source(
        "src/elspeth/core/landscape/execution/project_origin.py",
        "from elspeth.core.landscape.helpers import insert as sqlite_insert\n",
    )
    assert any("aliased DML import insert as sqlite_insert" in item for item in _dml_callable_escape_violations([project_origin]))

    relative_origin = _parse_source(
        "src/elspeth/core/landscape/execution/relative_origin.py",
        "from .helpers import insert as sqlite_insert\n",
    )
    assert any("aliased DML import insert as sqlite_insert" in item for item in _dml_callable_escape_violations([relative_origin]))

    core_alias = _parse_source(
        "src/elspeth/core/landscape/execution/core_alias.py",
        "from sqlalchemy import update as mutate\n",
    )
    assert any("aliased DML import update as mutate" in item for item in _dml_callable_escape_violations([core_alias]))


def test_execute_statement_classifier_reports_reads_raw_writes_and_caller_supplied_tables() -> None:
    """P4-D8 defect 2: every ``.execute`` site is classified, never left unknown."""

    refined_read = _parse_source(
        "src/elspeth/core/landscape/refined_read.py",
        "from sqlalchemy import select\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def read(conn, only_failed):\n"
        "    query = select(runs_table)\n"
        "    if only_failed:\n"
        "        query = query.where(runs_table.c.status == 'failed')\n"
        "    return conn.execute(query).fetchall()\n",
    )
    assert _unknown_or_raw_execution_violations([refined_read]) == ()

    refined_write = _parse_source(
        "src/elspeth/core/landscape/refined_write.py",
        "from sqlalchemy import update\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(conn, guarded):\n"
        "    statement = update(runs_table)\n"
        "    if guarded:\n"
        "        statement = statement.where(runs_table.c.status == 'running')\n"
        "    conn.execute(statement.values(status='failed'))\n",
    )
    assert _unknown_or_raw_execution_violations([refined_write]) == ()

    raw_cursor_delete = _parse_source(
        "src/elspeth/core/landscape/outbox_drain.py",
        "def drain(cursor, sequence):\n"
        "    placeholder = '?'\n"
        "    cursor.execute(f'DELETE FROM sidecar_journal_outbox WHERE sequence = {placeholder}', (sequence,))\n",
    )
    assert any(
        "raw SQL raw-delete sidecar_journal_outbox is forbidden" in item
        for item in _unknown_or_raw_execution_violations([raw_cursor_delete])
    )

    raw_cursor_read = _parse_source(
        "src/elspeth/core/landscape/outbox_read.py",
        "def read(cursor, owner):\n"
        "    placeholder = '%s'\n"
        "    cursor.execute(f'SELECT sequence FROM sidecar_journal_outbox WHERE journal_owner = {placeholder}', (owner,))\n"
        "    cursor.execute('BEGIN IMMEDIATE')\n",
    )
    assert _unknown_or_raw_execution_violations([raw_cursor_read]) == ()

    connection_configuration = _parse_source(
        "src/elspeth/core/landscape/configure.py",
        "from sqlalchemy import text\n"
        "def configure(cursor, conn):\n"
        "    cursor.execute('PRAGMA journal_mode=WAL')\n"
        "    cursor.execute('PRAGMA foreign_keys=ON')\n"
        "    conn.execute(text('SET TRANSACTION READ ONLY'))\n"
        "    conn.execute(text('PRAGMA query_only = ON'))\n",
    )
    assert _unknown_or_raw_execution_violations([connection_configuration]) == ()

    schema_epoch_stamp = _parse_source(
        "src/elspeth/core/landscape/epoch.py",
        "def stamp(conn, epoch):\n    conn.exec_driver_sql(f'PRAGMA user_version = {int(epoch)}')\n",
    )
    assert any(
        "raw SQL DDL schema-stamp (PRAGMA user_version) is forbidden" in item
        for item in _unknown_or_raw_execution_violations([schema_epoch_stamp])
    )

    epoch_read = _parse_source(
        "src/elspeth/core/landscape/epoch_read.py",
        "def read(conn):\n    return conn.exec_driver_sql('PRAGMA user_version').scalar_one()\n",
    )
    assert _unknown_or_raw_execution_violations([epoch_read]) == ()

    # A ``text()`` write is reported exactly once: a DML shape at the
    # ``text()`` node, a DDL statement (no DML shape) at the execute site.
    text_writes = _parse_source(
        "src/elspeth/core/landscape/text_writes.py",
        "from sqlalchemy import text\n"
        "def write(conn):\n"
        "    conn.execute(text('DELETE FROM runs WHERE run_id = :run_id'), {'run_id': 'r'})\n"
        "    conn.execute(text('DROP TABLE runs'))\n",
    )
    assert sorted(_unknown_or_raw_execution_violations([text_writes])) == [
        "src/elspeth/core/landscape/text_writes.py:3 write raw SQL raw-delete runs is forbidden",
        "src/elspeth/core/landscape/text_writes.py:4 write raw SQL write/DDL is forbidden",
    ]

    # An interpolated PRAGMA name is exact SQL when the loop variable ranges
    # over a literal table of constants; drawn from a parameter it stays unknown.
    tabled_pragma_read = _parse_source(
        "src/elspeth/core/landscape/pragma_table.py",
        "_FILE = (('journal_mode', 'wal'), ('busy_timeout', '5000'))\n"
        "_MEMORY = (('journal_mode', 'memory'), ('synchronous', '1'))\n"
        "def verify(conn, is_memory):\n"
        "    invariants = _MEMORY if is_memory else _FILE\n"
        "    for pragma, _expected in invariants:\n"
        "        conn.exec_driver_sql(f'PRAGMA {pragma}').scalar_one_or_none()\n",
    )
    assert _unknown_or_raw_execution_violations([tabled_pragma_read]) == ()
    parameter_pragma = _parse_source(
        "src/elspeth/core/landscape/pragma_parameter.py",
        "def verify(conn, pragmas):\n    for pragma in pragmas:\n        conn.exec_driver_sql(f'PRAGMA {pragma}')\n",
    )
    assert any("unknown exec_driver_sql effect" in item for item in _unknown_or_raw_execution_violations([parameter_pragma]))
    tabled_pragma_write = _parse_source(
        "src/elspeth/core/landscape/pragma_table_write.py",
        "_NAMES = ('journal_mode', 'user_version')\n"
        "def stamp(conn):\n"
        "    for pragma in _NAMES:\n"
        "        conn.exec_driver_sql(f'PRAGMA {pragma} = 1')\n",
    )
    assert any(
        "raw SQL DDL schema-stamp (PRAGMA user_version) is forbidden" in item
        for item in _unknown_or_raw_execution_violations([tabled_pragma_write])
    )

    caller_supplied_table = _parse_source(
        "src/elspeth/core/landscape/execution/conflict_safe.py",
        "from sqlalchemy.dialects.sqlite import insert as sqlite_insert\n"
        "def write(conn, table, values):\n"
        "    statement = sqlite_insert(table).values(**values).on_conflict_do_nothing()\n"
        "    return conn.execute(statement).fetchone()\n",
    )
    assert any(
        "DML insert on caller-supplied table 'table'" in item for item in _unknown_or_raw_execution_violations([caller_supplied_table])
    )

    statement_relay = _parse_source(
        "src/elspeth/core/landscape/relay.py",
        "def execute_insert(conn, stmt):\n    return conn.execute(stmt)\n",
    )
    assert any(
        "relays caller-supplied statement parameter 'stmt'" in item for item in _unknown_or_raw_execution_violations([statement_relay])
    )
    # A loop or comprehension element of a parameter is the same relay, named
    # by the parameter it came through.
    loop_relay = _parse_source(
        "src/elspeth/core/landscape/loop_relay.py",
        "def execute_all(conn, statements, queries):\n"
        "    for stmt in statements:\n"
        "        conn.execute(stmt)\n"
        "    return [conn.execute(query).fetchall() for query in queries]\n",
    )
    loop_relay_violations = _unknown_or_raw_execution_violations([loop_relay])
    assert any("relays caller-supplied statement parameter 'statements'" in item for item in loop_relay_violations)
    assert any("relays caller-supplied statement parameter 'queries'" in item for item in loop_relay_violations)
    assert not any("unclassified" in item for item in loop_relay_violations)

    # A same-unit helper classifies by its ``return`` expressions: all DML or
    # all proven reads are admitted, anything else stays unclassified.
    helper_returns = _parse_source(
        "src/elspeth/core/landscape/helpers_unit.py",
        "from sqlalchemy import select, union\n"
        "from sqlalchemy.dialects.postgresql import insert as postgresql_insert\n"
        "from sqlalchemy.dialects.sqlite import insert as sqlite_insert\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def _selects(run_id):\n"
        "    return (select(runs_table.c.run_id), select(runs_table.c.status))\n"
        "def _compound(run_id):\n"
        "    return union(*_selects(run_id))\n"
        "class Repository:\n"
        "    @staticmethod\n"
        "    def _upsert(conn, values):\n"
        "        if conn.dialect.name == 'sqlite':\n"
        "            return sqlite_insert(runs_table).values(**values).on_conflict_do_nothing()\n"
        "        return postgresql_insert(runs_table).values(**values).on_conflict_do_nothing()\n"
        "    @staticmethod\n"
        "    def _decision(run_id):\n"
        "        return select(runs_table).where(runs_table.c.run_id == run_id)\n"
        "    @staticmethod\n"
        "    def _mixed(conn, run_id):\n"
        "        if conn.dialect.name == 'sqlite':\n"
        "            return select(runs_table)\n"
        "        return sqlite_insert(runs_table)\n"
        "    def write(self, conn, values, run_id):\n"
        "        conn.execute(self._upsert(conn, values).returning(runs_table.c.run_id))\n"
        "        conn.execute(self._decision(run_id)).fetchall()\n"
        "        conn.execute(_compound(run_id)).fetchall()\n"
        "        conn.execute(self._mixed(conn, run_id))\n",
    )
    helper_violations = _unknown_or_raw_execution_violations([helper_returns])
    assert [item for item in helper_violations if "unclassified" in item] == [
        "src/elspeth/core/landscape/helpers_unit.py:27 Repository.write unclassified .execute statement"
    ]

    # A compound select is a read by RESOLVED ORIGIN, not by spelling.
    project_union = _parse_source(
        "src/elspeth/core/landscape/project_union.py",
        "from elspeth.core.landscape.helpers import union\ndef read(conn, parts):\n    return conn.execute(union(*parts)).fetchall()\n",
    )
    assert any("unclassified .execute statement" in item for item in _unknown_or_raw_execution_violations([project_union]))

    # Spelling alone never buys a DML label: only a resolved SQLAlchemy origin does.
    project_callable = _parse_source(
        "src/elspeth/core/landscape/project_callable.py",
        "from elspeth.core.landscape.helpers import insert\ndef write(conn, table):\n    return conn.execute(insert(table))\n",
    )
    assert any("unclassified .execute statement" in item for item in _unknown_or_raw_execution_violations([project_callable]))


def test_statement_walkers_key_their_cycle_guard_on_the_binding_site_not_the_name() -> None:
    """P4-D8 defect 2: a name re-bound to a refinement of itself is not a cycle.

    ``_statement_contains_dml`` answers True on a genuine revisit (fail closed),
    so a NAME-keyed guard read ``stmt = stmt.values(...)`` as DML by accident
    and ``state_id = state_id or ...`` inside a SELECT's WHERE as DML by error.
    Keyed on the binding site, the insert is proven through its own chain and
    the incidental scalar rebinding no longer poisons the read.
    """

    unit = _parse_source(
        "src/elspeth/core/landscape/rebinding.py",
        "from sqlalchemy import insert, select\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(conn, values):\n"
        "    stmt = insert(runs_table)\n"
        "    stmt = stmt.values(**values)\n"
        "    conn.execute(stmt)\n"
        "def read(conn, state_id):\n"
        "    state_id = state_id or generate_id()\n"
        "    query = select(runs_table)\n"
        "    query = query.where(runs_table.c.run_id == state_id)\n"
        "    conn.execute(query).fetchall()\n",
    )
    resolver = _resolver_for_unit(unit)
    executes = {
        _symbol(node): node
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and _call_name(node) == "execute" and _symbol(node) in {"write", "read"}
    }
    assert set(executes) == {"write", "read"}
    write_call, read_call = executes["write"], executes["read"]
    assert _statement_contains_dml(write_call.args[0], resolver, use=write_call)
    assert not _is_proven_read(write_call.args[0], resolver, use=write_call)
    resolved_write = resolver.resolve_statement(write_call.args[0], use=write_call)
    assert isinstance(resolved_write, ast.Call) and _dml_shape(resolved_write, resolver) == ("runs", "insert")
    assert not _statement_contains_dml(read_call.args[0], resolver, use=read_call)
    assert _is_proven_read(read_call.args[0], resolver, use=read_call)
    assert _unknown_or_raw_execution_violations([unit]) == ()

    # A GENUINE revisit still answers True.  Inside a parenthesised rebinding
    # the use of ``stmt`` sits below the assignment's own line, so it binds
    # to the assignment being evaluated: the binding site is revisited and
    # the walker fails closed instead of proving a read it has not seen.
    cyclic = _parse_source(
        "src/elspeth/core/landscape/cyclic.py",
        "from sqlalchemy import insert\n"
        "from elspeth.core.landscape.schema import runs_table\n"
        "def write(conn, values):\n"
        "    stmt = insert(runs_table)\n"
        "    stmt = (\n"
        "        stmt\n"
        "        .values(**values)\n"
        "    )\n"
        "    conn.execute(stmt)\n",
    )
    cyclic_resolver = _resolver_for_unit(cyclic)
    cyclic_call = next(node for node in ast.walk(cyclic.tree) if isinstance(node, ast.Call) and _call_name(node) == "execute")
    assert _statement_contains_dml(cyclic_call.args[0], cyclic_resolver, use=cyclic_call)


def test_resolver_cycle_guard_survives_the_callable_name_hop() -> None:
    """elspeth-5a50d4b9f3: ``seen`` must be carried through ``_resolved_callable_name``.

    A name re-bound to a multi-line chain over its own earlier value binds, for
    a use INSIDE that chain, to the very assignment being evaluated (the use's
    line is below the assignment's line), so the value graph is genuinely
    cyclic.  ``qualified_name`` guards that with ``seen``; the guard was
    dropped at the Call-branch hop into ``_resolved_callable_name`` and again
    on the way back into ``qualified_name``, so every lap reset it and the
    walk recursed without bound.  The fixture is the shape of
    ``RepositoryIdentityAuthority.list_relationships``.
    """

    unit = _parse_source(
        "src/elspeth/web/coordination/rebound_chain.py",
        "from sqlalchemy import or_, select\n"
        "from elspeth.web.sessions.models import identity_relationships_table as table\n"
        "def list_relationships(conn, limit, offset):\n"
        "    statement = select(table)\n"
        "    statement = statement.where(or_(table.c.a == 'x', table.c.b == 'y'))\n"
        "    statement = statement.where(table.c.revoked_at.is_(None))\n"
        "    statement = (\n"
        "        statement.order_by(table.c.created_at.desc(), table.c.id.asc())\n"
        "        .limit(limit)\n"
        "        .offset(offset)\n"
        "    )\n"
        "    return conn.execute(statement).fetchall()\n",
    )
    resolver = _resolver_for_unit(unit)
    chain_calls = [
        node
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"order_by", "limit", "offset"}
    ]
    assert len(chain_calls) == 3
    for call in chain_calls:
        # Each hop terminates instead of recursing; the chain has no
        # qualified name because its root re-binds to itself.
        assert _resolved_callable_name(call.func, resolver, use=call) == call.func.attr
        assert resolver.qualified_name(call, use=call) is None
    assert _mutation_callable_escapes([unit]) == ()


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
    # The UPDATE inside the CTE is a raw write on ``runs``: reported by that
    # class, exactly once (the ``text()`` node owns the report), never as a
    # read and never left unclassified.
    assert violations == ("src/elspeth/core/landscape/writable_cte.py:3 bypass raw SQL raw-update runs is forbidden",)

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


def test_raw_execution_admits_a_self_attribute_bound_to_a_constant_read_dictionary() -> None:
    """ADR-047 clock idiom: ``conn.exec_driver_sql(self._clock_sql)`` is admitted by resolution, never by name.

    The attribute is admitted only when EVERY binding of it in the enclosing
    class resolves to a finite set of constant texts and every text is a
    proven read.  One write candidate, one parameter-fed binding, a rebinding
    in another method, a ``setattr`` anywhere in the class, a tuple-unpacked
    binding, a foreign receiver, or a class that never binds the attribute
    keeps the site reported.
    """

    def clock_authority(*, values: str, init_binding: str, read_receiver: str = "self", extra_member: str = "") -> SourceUnit:
        body = (
            "_DATABASE_CLOCK_SQL = {\n"
            f"    {values}\n"
            "}\n"
            "\n"
            "class Authority:\n"
            "    def __init__(self, engine, clock_sql):\n"
            "        self._engine = engine\n"
            f"        {init_binding}\n"
            "\n"
            "    def now(self, other):\n"
            "        with self._engine.begin() as conn:\n"
            f"            return conn.exec_driver_sql({read_receiver}._clock_sql).scalar_one()\n"
            f"{extra_member}"
        )
        return _parse_source("src/elspeth/web/coordination/clock_authority.py", body)

    read_values = "'postgresql': 'SELECT clock_timestamp()', 'sqlite': 'SELECT CURRENT_TIMESTAMP',"
    dialect_binding = "self._clock_sql = _DATABASE_CLOCK_SQL[engine.dialect.name]"

    admitted = clock_authority(values=read_values, init_binding=dialect_binding)
    assert _raw_write_surface_violations([admitted]) == ()

    constant_key = clock_authority(
        values="'postgresql': 'DELETE FROM identities', 'sqlite': 'SELECT CURRENT_TIMESTAMP',",
        init_binding="self._clock_sql = _DATABASE_CLOCK_SQL['sqlite']",
    )
    assert _raw_write_surface_violations([constant_key]) == ()

    write_candidate = clock_authority(
        values="'postgresql': 'SELECT clock_timestamp()', 'sqlite': 'DELETE FROM identities',",
        init_binding=dialect_binding,
    )
    write_violations = _raw_write_surface_violations([write_candidate])
    assert len(write_violations) == 1
    assert "outside raw SQL write/DDL" in write_violations[0]

    def unknown_rows(unit: SourceUnit) -> list[str]:
        violations = _raw_write_surface_violations([unit])
        return [item for item in violations if "outside unknown raw SQL effect" in item]

    parameter_fed = clock_authority(values=read_values, init_binding="self._clock_sql = clock_sql")
    assert len(unknown_rows(parameter_fed)) == 1

    rebound_elsewhere = clock_authority(
        values=read_values,
        init_binding=dialect_binding,
        extra_member="\n    def retarget(self, sql):\n        self._clock_sql = sql\n",
    )
    assert len(unknown_rows(rebound_elsewhere)) == 1

    setattr_in_class = clock_authority(
        values=read_values,
        init_binding=dialect_binding,
        extra_member="\n    def retarget(self, name, sql):\n        setattr(self, name, sql)\n",
    )
    assert len(unknown_rows(setattr_in_class)) == 1

    tuple_unpacked = clock_authority(
        values=read_values,
        init_binding="self._engine, self._clock_sql = engine, _DATABASE_CLOCK_SQL[engine.dialect.name]",
    )
    assert len(unknown_rows(tuple_unpacked)) == 1

    foreign_receiver = clock_authority(values=read_values, init_binding=dialect_binding, read_receiver="other")
    assert len(unknown_rows(foreign_receiver)) == 1

    never_bound = clock_authority(values=read_values, init_binding="self._other = _DATABASE_CLOCK_SQL[engine.dialect.name]")
    assert len(unknown_rows(never_bound)) == 1

    transaction_control = clock_authority(
        values="'postgresql': 'SELECT clock_timestamp()', 'sqlite': 'BEGIN IMMEDIATE',",
        init_binding=dialect_binding,
    )
    assert _raw_write_surface_violations([transaction_control]) == tuple(unknown_rows(transaction_control))
    assert len(unknown_rows(transaction_control)) == 1

    splat_dictionary = _parse_source(
        "src/elspeth/web/coordination/splat_clock_authority.py",
        "_BASE = {'sqlite': 'SELECT CURRENT_TIMESTAMP'}\n"
        "_DATABASE_CLOCK_SQL = {**_BASE, 'postgresql': 'SELECT clock_timestamp()'}\n"
        "class Authority:\n"
        "    def __init__(self, engine):\n"
        "        self._engine = engine\n"
        "        self._clock_sql = _DATABASE_CLOCK_SQL[engine.dialect.name]\n"
        "    def now(self):\n"
        "        with self._engine.begin() as conn:\n"
        "            return conn.exec_driver_sql(self._clock_sql).scalar_one()\n",
    )
    assert len(unknown_rows(splat_dictionary)) == 1


@pytest.mark.parametrize("clock_name", ["read_landscape_decision_time", "sample_time"])
def test_fresh_landscape_sample_is_a_database_effect_before_authority_admission(clock_name: str) -> None:
    source = textwrap.dedent(
        f"""\
        from elspeth.contracts.coordination import CoordinationToken
        from elspeth.core.landscape.database_clock import read_landscape_decision_time as {clock_name}
        from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction

        def change(conn, *, coordination_token: CoordinationToken):
            sampled = {clock_name}(conn)
            with fenced_leader_transaction(engine, token=coordination_token) as conn:
                pass
        """
    )
    unit = _parse_source("src/elspeth/core/landscape/clock_sample.py", source)
    function = next(node for node in unit.tree.body if isinstance(node, ast.FunctionDef))
    assert "first database effect" in (_function_fence_violation(function) or "")

    admitted = _parse_source(
        unit.path,
        source.replace(f"    sampled = {clock_name}(conn)\n", "").replace("        pass", f"        sampled = {clock_name}(conn)"),
    )
    function = next(node for node in admitted.tree.body if isinstance(node, ast.FunctionDef))
    assert _function_fence_violation(function) is None


def _target_lock_fixture(body, *, extra="", table_name="token_work_items", keys=("work_item_id", "run_id")):
    source = (
        "from sqlalchemy import select\n"
        "from elspeth.core.landscape.schema import token_work_items_table, sink_effects_table\n"
        "from elspeth.core.landscape.database_clock import read_landscape_decision_time\n"
        + extra
        + "\n"
        + "def issue(conn, other_conn, work_item_id, run_id, effect_id, predecessor, row, flag):\n"
        + textwrap.indent(textwrap.dedent(body).strip() + "\n", "    ")
    )
    unit = _parse_source("src/elspeth/core/landscape/lock_fixture.py", source)
    function = next(part for part in unit.tree.body if isinstance(part, ast.FunctionDef) and part.name == "issue")
    sample = next(
        part
        for part in ast.walk(function)
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and part.func.id == "read_landscape_decision_time"
    )
    subjects = next(
        part for part in ast.walk(function) if isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and part.func.id == "subjects"
    )
    return _target_lock_precedes_fresh_sample(
        sample,
        function=function,
        resolver=_resolver_for_unit(unit),
        proof=_AuthorityProof((unit,)),
        connection=sample.args[0],
        table=("elspeth.core.landscape.schema", table_name),
        key_values=dict(zip(keys, subjects.args, strict=True)),
    )


_TARGET_LOCK_DIRECT = """
conn.execute(select(token_work_items_table).where(
    token_work_items_table.c.work_item_id == work_item_id,
    token_work_items_table.c.run_id == run_id,
).with_for_update(of=token_work_items_table)).mappings().all()
now = read_landscape_decision_time(conn)
subjects(work_item_id, run_id)
"""


def test_target_lock_direct_and_alias_positive():
    assert _target_lock_fixture(_TARGET_LOCK_DIRECT)
    alias = _TARGET_LOCK_DIRECT.replace("conn.execute", "target_conn.execute")
    assert _target_lock_fixture("target_conn = conn\n" + alias)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("conn.execute", "other_conn.execute"),
        (".with_for_update(of=token_work_items_table)", ".with_for_update(read=True)"),
        (".with_for_update(of=token_work_items_table)", ".with_for_update(skip_locked=True)"),
        (".with_for_update(of=token_work_items_table)", ""),
        (".mappings().all()", ""),
        ("work_item_id == work_item_id", "work_item_id == 'another-item'"),
        ("run_id == run_id", "run_id == 'another-run'"),
        ("select(token_work_items_table)", "select(token_work_items_table).select_from(sink_effects_table)"),
        ("select(token_work_items_table)", "select(token_work_items_table).add_cte(opaque_write)"),
        ("now = read_landscape_decision_time(conn)", "conn.rollback()\nnow = read_landscape_decision_time(conn)"),
        ("subjects(work_item_id, run_id)", "subjects('another-item', run_id)"),
    ],
)
def test_target_lock_direct_adversaries(before, after):
    assert not _target_lock_fixture(_TARGET_LOCK_DIRECT.replace(before, after))


def test_target_lock_order_branch_and_import_impostors():
    lock, after = _TARGET_LOCK_DIRECT.split("now =", 1)
    assert not _target_lock_fixture("now =" + after + lock)
    assert not _target_lock_fixture("if flag:\n" + textwrap.indent(lock.strip() + "\n", "    ") + "now =" + after)
    assert not _target_lock_fixture(_TARGET_LOCK_DIRECT, extra="from foreign.schema import token_work_items_table\n")
    assert not _target_lock_fixture(_TARGET_LOCK_DIRECT, extra="def select(*args):\n    return impostor\n")


_TARGET_LOCK_HELPER = """
def not_a_magic_lock_name(conn, work_item_id, run_id):
    conn.execute(select(token_work_items_table).where(
        token_work_items_table.c.work_item_id == work_item_id,
        token_work_items_table.c.run_id == run_id,
    ).with_for_update()).fetchall()
    return None
"""
_TARGET_LOCK_HELPER_CALL = """
not_a_magic_lock_name(conn, work_item_id, run_id)
now = read_landscape_decision_time(conn)
subjects(work_item_id, run_id)
"""


def test_target_lock_resolves_owned_helper_body():
    assert _target_lock_fixture(_TARGET_LOCK_HELPER_CALL, extra=_TARGET_LOCK_HELPER)
    assert not _target_lock_fixture(
        _TARGET_LOCK_HELPER_CALL, extra=_TARGET_LOCK_HELPER.replace("    conn.execute", "    return None\n    conn.execute")
    )
    assert not _target_lock_fixture(
        _TARGET_LOCK_HELPER_CALL, extra=_TARGET_LOCK_HELPER.replace("    return None", "    conn.commit()\n    return None")
    )
    assert not _target_lock_fixture(
        _TARGET_LOCK_HELPER_CALL, extra="def not_a_magic_lock_name(conn, work_item_id, run_id):\n    return None\n"
    )
    assert not _target_lock_fixture(_TARGET_LOCK_HELPER_CALL, extra=_TARGET_LOCK_HELPER + "\nnot_a_magic_lock_name = foreign_lock\n")


def test_target_lock_row_projection_is_the_original_key():
    body = """
locked_row = conn.execute(select(token_work_items_table).where(
    token_work_items_table.c.work_item_id == row["work_item_id"],
    token_work_items_table.c.run_id == run_id,
).with_for_update()).mappings().one_or_none()
if locked_row is None:
    return None
row = locked_row
now = read_landscape_decision_time(conn)
subjects(row["work_item_id"], run_id)
"""
    assert _target_lock_fixture(body)
    assert not _target_lock_fixture(body.replace("row = locked_row", "row = predecessor"))
    assert not _target_lock_fixture(body.replace("token_work_items_table.c.work_item_id ==", "sink_effects_table.c.work_item_id =="))


def test_target_lock_effect_set_requires_source_proven_nonnull():
    guard = """
def validate_key(value, name):
    if type(value) is not str or invalid_digest(value):
        raise ValueError(name)
"""
    body = """
validate_key(effect_id, "effect_id")
effect_ids = sorted({effect_id, predecessor} - {None})
conn.execute(select(sink_effects_table).where(
    sink_effects_table.c.effect_id.in_(effect_ids)
).order_by(sink_effects_table.c.effect_id).with_for_update()).fetchall()
now = read_landscape_decision_time(conn)
subjects(effect_id)
"""
    assert _target_lock_fixture(body, extra=guard, table_name="sink_effects", keys=("effect_id",))
    assert not _target_lock_fixture(
        body, extra=guard.replace("        raise ValueError(name)", "        return None"), table_name="sink_effects", keys=("effect_id",)
    )
    assert not _target_lock_fixture(
        body.replace("{effect_id, predecessor}", "{predecessor}"), extra=guard, table_name="sink_effects", keys=("effect_id",)
    )
    assert not _target_lock_fixture(body, extra=guard + "\nsorted = foreign_sorted\n", table_name="sink_effects", keys=("effect_id",))


def test_target_lock_extra_projection_cannot_shadow_keys():
    extra = "select(token_work_items_table, bundle.label('_pending_sink_bundle_complete'))"
    assert _target_lock_fixture(_TARGET_LOCK_DIRECT.replace("select(token_work_items_table)", extra))
    shadow = "select(token_work_items_table, bundle.label('work_item_id'))"
    assert not _target_lock_fixture(_TARGET_LOCK_DIRECT.replace("select(token_work_items_table)", shadow))


@pytest.mark.parametrize("change", ["work_item_id += foreign", "del work_item_id", "if (work_item_id := foreign):\n    pass"])
def test_target_lock_refuses_intervening_subject_mutation(change):
    body = "subject = work_item_id\n" + _TARGET_LOCK_DIRECT.replace("== work_item_id", "== subject").replace("now =", change + "\nnow =")
    assert not _target_lock_fixture(body)


def test_target_lock_refuses_augmented_connection_alias():
    body = "target_conn = conn\n" + _TARGET_LOCK_DIRECT.replace("conn.execute", "target_conn.execute").replace(
        "now =", "target_conn += other_conn\nnow ="
    ).replace("read_landscape_decision_time(conn)", "read_landscape_decision_time(target_conn)")
    assert not _target_lock_fixture(body)


@pytest.mark.parametrize(
    "predicate", ["False", "token_work_items_table.c.status == 'never-written'", "token_work_items_table.c.work_item_id == 'different-id'"]
)
def test_target_lock_refuses_additional_narrowing_conjunct(predicate):
    assert not _target_lock_fixture(
        _TARGET_LOCK_DIRECT.replace(
            "    token_work_items_table.c.run_id == run_id,", "    token_work_items_table.c.run_id == run_id,\n    " + predicate + ","
        )
    )


@pytest.mark.parametrize(
    "extra, action",
    [
        ("def release(conn):\n    conn.rollback()\n", "release(conn)"),
        ("def inner(conn):\n    conn.rollback()\ndef outer(conn):\n    inner(conn)\n", "outer(conn)"),
        ("", "callback(conn)"),
        ("", "callback(connection=conn)"),
        ("", "callback({'connection': conn})"),
        ("", "conn.exec_driver_sql('COMMIT')"),
        ("", "conn.connection.rollback()"),
        ("", "if flag:\n    callback(conn)"),
    ],
)
def test_target_lock_refuses_connection_release_and_unknown_escape(extra, action):
    assert not _target_lock_fixture(_TARGET_LOCK_DIRECT.replace("now =", action + "\nnow ="), extra=extra)


def test_target_lock_preserves_source_owned_read_helper():
    extra = "def inspect(conn):\n    return conn.execute(select(token_work_items_table.c.run_id)).fetchall()\n"
    assert _target_lock_fixture(_TARGET_LOCK_DIRECT.replace("now =", "inspect(conn)\nnow ="), extra=extra)


def test_target_lock_refuses_release_inside_the_lock_helper():
    helper = (
        _TARGET_LOCK_HELPER.replace("    return None", "    release(conn)\n    return None") + "\ndef release(conn):\n    conn.rollback()\n"
    )
    assert not _target_lock_fixture(_TARGET_LOCK_HELPER_CALL, extra=helper)


@pytest.mark.parametrize(
    "action", ["release = conn.rollback\nrelease()", "release = conn.close\nrelease()", "carried = conn\ncallback(carried)"]
)
def test_target_lock_refuses_connection_callable_alias_escape(action):
    assert not _target_lock_fixture(_TARGET_LOCK_DIRECT.replace("now =", action + "\nnow ="))


@pytest.mark.parametrize(
    ("module", "before", "after"),
    [
        ("lease_deadlines", "if not isinstance(state, _TransactionDeadlines):", "if False:"),
        ("lease_deadlines", "if state.transaction is not conn.get_transaction():", "if False:"),
        ("lease_deadlines", "if not isinstance(key, DeadlineKey):", "if False:"),
        (
            "lease_deadlines",
            "_IssuedDeadline(expires_at.astimezone(UTC), reserve)",
            "_IssuedDeadline(expires_at.astimezone(UTC) + timedelta(seconds=1), reserve)",
        ),
        ("lease_deadlines", "deadline.expires_at - sampled_at <= deadline.reserve", "False"),
        (
            "lease_deadlines",
            "sampled_at = read_landscape_decision_time(conn)",
            'conn.exec_driver_sql("UPDATE token_work_items SET lease_expires_at = NULL")\n        sampled_at = read_landscape_decision_time(conn)',
        ),
        (
            "lease_deadlines",
            "sampled_at = read_landscape_decision_time(conn)",
            "conn.commit()\n        sampled_at = read_landscape_decision_time(conn)",
        ),
        (
            "lease_deadlines",
            "sampled_at = read_landscape_decision_time(conn)",
            'conn.info["callback"](conn)\n        sampled_at = read_landscape_decision_time(conn)',
        ),
        ("lease_deadlines", "conn.connection.rollback()", "pass"),
        ("lease_deadlines", "state.issued.pop(key, None)", "state.issued.clear()"),
        ("database_clock", "func.clock_timestamp()", "func.current_timestamp()"),
        ("journal", "install_deadline_guard(engine, after_journal=True)", "install_deadline_guard(engine)"),
        ("journal", "rollback_failed_commit(conn)", "pass"),
        (
            "journal",
            "def _serialize_record(record: JournalRecord) -> str:",
            "def _serialize_record(record: JournalRecord) -> str:\n        unknown_callback(record)",
        ),
        ("database", "install_deadline_guard(self._engine)", "pass"),
        (
            "lease_deadlines",
            "_INSTALL_LOCK = Lock()",
            "_INSTALL_LOCK = Lock()\nread_landscape_decision_time = lambda conn: datetime.now(UTC)",
        ),
    ],
)
def test_issued_deadline_registry_dependency_binding_rejects_semantic_mutations(module, before, after):
    sources = {path: (_repo_root() / path).read_text() for path in _REVIEWED_REGISTRY_MODULES}
    assert _issued_deadline_registry_source_is_proven(sources)
    target = f"src/elspeth/core/landscape/{module}.py"
    assert before in sources[target]
    changed = {**sources, target: sources[target].replace(before, after, 1)}
    assert not _issued_deadline_registry_source_is_proven(changed)


def test_issued_deadline_registry_dependency_binding_requires_complete_source_but_ignores_comments():
    sources = {path: (_repo_root() / path).read_text() for path in _REVIEWED_REGISTRY_MODULES}
    assert _issued_deadline_registry_source_is_proven(sources)
    assert _issued_deadline_registry_source_is_proven({path: source + "\n# documentation only\n" for path, source in sources.items()})
    for missing in sources:
        assert not _issued_deadline_registry_source_is_proven({path: source for path, source in sources.items() if path != missing})
    assert not _issued_deadline_registry_source_is_proven({**sources, next(iter(sources)): "def ("})


@pytest.mark.parametrize("placement", ["before", "argument"])
def test_source_owned_clock_wrapper_cannot_execute_before_fence_admission(placement):
    baseline = _deadline_issuance_fixture()
    source = baseline.source + "\ndef read_clock(conn):\n    return read_landscape_decision_time(conn)\n"
    if placement == "before":
        source = source.replace(
            "    with fenced_member_transaction", "    observed = read_clock(other_conn)\n    with fenced_member_transaction"
        )
    else:
        source = source.replace("verb='heartbeat_lease'", "verb=read_clock(other_conn)")
    mutant = _parse_source(baseline.path, source)
    assert _pre_admission_helper_effect_violations((baseline,)) == ()
    assert _pre_admission_helper_effect_violations((mutant,))


@pytest.mark.parametrize("variant", ["own_fence", "own_early_clock", "independent_reference_read", "fake_fence"])
def test_pre_admission_effects_respect_transaction_ownership(variant):
    source = """from sqlalchemy import select, update
from sqlalchemy.engine import Connection
from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.core.landscape.run_coordination_repository import fenced_member_transaction
from elspeth.core.landscape.database_clock import read_landscape_decision_time
from elspeth.core.landscape.schema import token_work_items_table
def delegated(member_token: WorkerMembershipToken):
    with fenced_member_transaction(engine, member_token=member_token, verb='delegated') as conn:
        observed = read_landscape_decision_time(conn)
        conn.execute(update(token_work_items_table).values(updated_at=observed))
def outer(member_token: WorkerMembershipToken):
    delegated(member_token)
    with fenced_member_transaction(engine, member_token=member_token, verb='outer') as conn:
        conn.execute(update(token_work_items_table).values(status='READY'))
"""
    if variant == "own_early_clock":
        source = source.replace(
            "    with fenced_member_transaction(engine, member_token=member_token, verb='delegated')",
            "    early = read_landscape_decision_time(other_conn)\n    with fenced_member_transaction(engine, member_token=member_token, verb='delegated')",
        )
    elif variant == "independent_reference_read":
        source = source.replace(
            "    with fenced_member_transaction(engine, member_token=member_token, verb='delegated') as conn:\n        observed = read_landscape_decision_time(conn)\n        conn.execute(update(token_work_items_table).values(updated_at=observed))",
            "    with separate_db.read_only_connection() as snapshot:\n        return snapshot.execute(select(token_work_items_table.c.work_item_id))",
        )
    elif variant == "fake_fence":
        source = source.replace("verb='delegated') as conn:", "verb='delegated') as other_conn:")
    source = (
        source.replace("WorkerMembershipToken", "CoordinationToken")
        .replace("member_token", "token")
        .replace("fenced_member_transaction", "fenced_leader_transaction")
    )
    source = source.replace(
        "update(token_work_items_table).values",
        "update(token_work_items_table).where(token_work_items_table.c.run_id == token.run_id).values",
    )
    unit = _parse_source("src/elspeth/transaction_discovery.py", source)
    findings = _pre_admission_helper_effect_violations((unit,))
    assert bool(findings) is (variant in {"own_early_clock", "fake_fence"}), findings


@pytest.mark.parametrize("argument", ["", "conn=None", "conn=connection"])
def test_pre_admission_clock_rule_does_not_reclassify_optional_reference_queries(argument):
    source = f"""from sqlalchemy import select, update
from sqlalchemy.engine import Connection
from elspeth.contracts.coordination import CoordinationToken
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import token_work_items_table
def reference(*, conn: Connection | None = None):
    if conn is not None:
        return conn.execute(select(token_work_items_table.c.run_id))
    return separate_ops.execute_fetchone(select(token_work_items_table.c.run_id))
def outer(token: CoordinationToken):
    reference({argument})
    with fenced_leader_transaction(engine, token=token, verb='outer') as conn:
        conn.execute(update(token_work_items_table).where(token_work_items_table.c.run_id == token.run_id).values(status='READY'))
"""
    unit = _parse_source("src/elspeth/optional_reference.py", source)
    assert _pre_admission_helper_effect_violations((unit,)) == ()
    # The independent first-statement rule still refuses direct SQL before
    # this owner's admission. Reference discovery cannot relax that rule.
    changed = _parse_source(unit.path, source.replace(f"    reference({argument})", "    conn.execute(select(token_work_items_table))"))
    owner = next(node for node in changed.tree.body if isinstance(node, ast.FunctionDef) and node.name == "outer")
    assert _function_fence_violation(owner) == "full-token fence is not the transaction owner's first database effect"


def _deadline_fixture_units(unit: SourceUnit) -> tuple[SourceUnit, ...]:
    """Include the actual source dependencies required by the namespace proof."""
    dependencies = (
        "src/elspeth/core/landscape/lease_deadlines.py",
        "src/elspeth/core/landscape/database_clock.py",
    )
    return (unit, *(_parse_source(path, (_repo_root() / path).read_text()) for path in dependencies))


def _deadline_issuance_fixture() -> SourceUnit:
    return _parse_source(
        "src/elspeth/core/landscape/scheduler/leases.py",
        textwrap.dedent(
            """\
            from datetime import timedelta
            from sqlalchemy import select, update
            from elspeth.contracts.coordination import WorkerMembershipToken
            from elspeth.core.landscape.database_clock import read_landscape_decision_time
            from elspeth.core.landscape.lease_deadlines import DeadlineKey, DeadlineKind, record_issued_deadline
            from elspeth.core.landscape.run_coordination_repository import fenced_member_transaction
            from elspeth.core.landscape.schema import token_work_items_table

            def heartbeat_lease(*, member_token: WorkerMembershipToken, work_item_id, lease_seconds):
                with fenced_member_transaction(engine, member_token=member_token, verb='heartbeat_lease') as conn:
                    conn.execute(select(token_work_items_table.c.work_item_id).where(
                        token_work_items_table.c.work_item_id == work_item_id,
                        token_work_items_table.c.run_id == member_token.run_id,
                    ).with_for_update()).fetchall()
                    database_now = read_landscape_decision_time(conn)
                    expires_at = database_now + timedelta(seconds=lease_seconds)
                    result = conn.execute(update(token_work_items_table).where(
                        token_work_items_table.c.work_item_id == work_item_id,
                        token_work_items_table.c.run_id == member_token.run_id,
                    ).values(lease_expires_at=expires_at, updated_at=database_now))
                    if result.rowcount != 1:
                        raise RuntimeError('lease refused')
                    record_issued_deadline(conn,
                        key=DeadlineKey(DeadlineKind.ITEM, (work_item_id,)),
                        expires_at=expires_at, window_seconds=lease_seconds)
            """
        ),
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("database_now = read_landscape_decision_time(conn)", "database_now = read_landscape_decision_time(other_conn)"),
        (".with_for_update()", ""),
        (".with_for_update()", ".with_for_update(read=True)"),
        ("expires_at=expires_at, window_seconds=lease_seconds", "expires_at=database_now, window_seconds=lease_seconds"),
        ("DeadlineKind.ITEM, (work_item_id,)", "DeadlineKind.ITEM, ('foreign-item',)"),
        ("record_issued_deadline(conn,", "record_issued_deadline(other_conn,"),
        ("expires_at=expires_at, window_seconds=lease_seconds", "expires_at=expires_at, window_seconds=999"),
        ("DeadlineKind.ITEM, (work_item_id,)", "DeadlineKind.SINK_EFFECT, (work_item_id,)"),
        ("        record_issued_deadline(conn,", "        if unknown_condition:\n            record_issued_deadline(conn,"),
        ("        record_issued_deadline(conn,", "        return None\n        record_issued_deadline(conn,"),
        (
            ".values(lease_expires_at=expires_at, updated_at=database_now)",
            ".values(**{'lease_expires_at': database_now, 'updated_at': database_now})",
        ),
        (".values(lease_expires_at=expires_at, updated_at=database_now)", ".values(**supplied_values)"),
    ],
)
def test_deadline_issuance_requires_locked_sample_and_exact_registration(before: str, after: str) -> None:
    baseline = _deadline_issuance_fixture()
    baseline_units = _deadline_fixture_units(baseline)
    assert _transaction_order_violations(baseline_units, scan_dml_identities(baseline_units)) == ()
    assert before in baseline.source
    mutant = _parse_source(baseline.path, baseline.source.replace(before, after))
    mutant_units = _deadline_fixture_units(mutant)
    assert any("deadline" in finding for finding in _transaction_order_violations(mutant_units, scan_dml_identities(mutant_units)))


def test_deadline_issuance_rejects_sampling_before_the_target_lock() -> None:
    baseline = _deadline_issuance_fixture()
    before_lock = baseline.source.replace("        database_now = read_landscape_decision_time(conn)\n", "").replace(
        "        conn.execute(select", "        database_now = read_landscape_decision_time(conn)\n        conn.execute(select"
    )
    mutant = _parse_source(baseline.path, before_lock)
    mutant_units = _deadline_fixture_units(mutant)
    assert any("deadline" in finding for finding in _transaction_order_violations(mutant_units, scan_dml_identities(mutant_units)))


@pytest.mark.parametrize(
    "variant",
    [
        "flag_augassign",
        "rebound_result",
        "wrapper_without_registration",
        "transaction_without_registration",
        "mapping_without_registration",
        "executemany_without_registration",
        "split_stamp",
        "prelock_eligibility",
        "foreign_key_factory",
        "caught_registration_skip",
    ],
)
def test_deadline_registration_rejects_reviewed_provenance_and_control_flow_bypasses(variant):
    baseline = _deadline_issuance_fixture()
    registration = "        record_issued_deadline(conn,"
    missing = baseline.source.split(registration)[0]
    changes = {
        "flag_augassign": baseline.source.replace(
            registration,
            "        required = True\n        required &= unknown_condition\n        if required:\n            record_issued_deadline(conn,",
        ),
        "rebound_result": baseline.source.replace(
            registration, "        result = foreign\n        if result.rowcount == 1:\n            record_issued_deadline(conn,"
        ),
        "wrapper_without_registration": missing.replace("database_now = read_landscape_decision_time(conn)", "database_now = fresh(conn)")
        + "\ndef fresh(conn):\n    return read_landscape_decision_time(conn)\n",
        "transaction_without_registration": missing.replace("read_landscape_decision_time", "read_landscape_transaction_time"),
        "mapping_without_registration": missing.replace(
            ".values(lease_expires_at=expires_at, updated_at=database_now))",
            ", {'lease_expires_at': expires_at, 'updated_at': database_now})",
        ),
        "executemany_without_registration": missing.replace(
            ".values(lease_expires_at=expires_at, updated_at=database_now))",
            ", [{'lease_expires_at': expires_at, 'updated_at': database_now}])",
        ),
        "split_stamp": baseline.source.replace("updated_at=database_now", "updated_at=read_landscape_decision_time(conn)"),
        "prelock_eligibility": baseline.source.replace(
            "        conn.execute(select", "        eligibility_now = read_landscape_decision_time(conn)\n        conn.execute(select"
        ).replace(
            ".values(lease_expires_at", ".where(token_work_items_table.c.lease_expires_at > eligibility_now).values(lease_expires_at"
        ),
        "foreign_key_factory": baseline.source.replace(
            "DeadlineKey(DeadlineKind.ITEM, (work_item_id,))", "key_for(Foreign(work_item_id=work_item_id))"
        )
        + "\ndef key_for(token):\n    return DeadlineKey(DeadlineKind.ITEM, (token.work_item_id,))\n",
        "caught_registration_skip": "",
    }
    prefix, body = baseline.source.split("        database_now =", 1)
    changes["caught_registration_skip"] = (
        prefix
        + "        try:\n"
        + textwrap.indent(
            ("        database_now =" + body).replace(registration, "        raise RuntimeError('skip')\n" + registration), "    "
        )
        + "        except RuntimeError:\n            pass\n"
    )
    assert _deadline_issuance_violations(_deadline_fixture_units(baseline)) == ()
    source = changes[variant]
    assert source != baseline.source
    mutant = _parse_source(baseline.path, source)
    assert _deadline_issuance_violations(_deadline_fixture_units(mutant))


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
    assert not any("helpers.py:helper" in item for item in _transaction_order_violations([helpers], dml))

    one_unfenced = _parse_source(
        helpers.path,
        helpers.source.replace(
            "def second(*, coordination_token: CoordinationToken):\n    with fenced_leader_transaction(engine, token=coordination_token) as conn:",
            "def second(*, coordination_token: CoordinationToken):\n    with begin_write(engine) as conn:",
        ),
    )
    one_unfenced_violations = _transaction_order_violations([one_unfenced], scan_dml_identities([one_unfenced]))
    assert any(
        "helpers.py:helper subordinate caller src/elspeth/core/landscape/helpers.py:second is not fenced" in item
        for item in one_unfenced_violations
    )

    no_callers = _parse_source(helpers.path, helpers.source.split("def first")[0])
    assert any("callers=0 expected>=1" in item for item in _transaction_order_violations([no_callers], scan_dml_identities([no_callers])))

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


def test_each_verb_class_accepts_exactly_one_authority_type_and_its_own_fence() -> None:
    """ADR-030 D4 / ADR-048 amendment: one concrete type per verb class, and no crossing.

    ADR-048 §1 originally required the leader token on EVERY mutation API,
    which overreached D4's three-fence split: a follower writes its own
    liveness and departure rows and holds no epoch. The fix is a SECOND owned
    type, never one type with two meanings -- option (B) was rejected because a
    leader verb accidentally accepting a follower's token would be unprovable,
    which is the fail-open class this gate exists to close.

    So the cross arms below are the point of the test, not decoration: they are
    what distinguishes this rule from simply widening the annotation predicate
    to accept either type.
    """
    source = (
        "from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken\n"
        "from elspeth.core.landscape.run_coordination_repository import (\n"
        "    fenced_leader_transaction,\n"
        "    fenced_member_transaction,\n"
        ")\n"
        "from elspeth.core.landscape.schema import run_workers_table\n"
        "from sqlalchemy import update\n"
        "\n"
        "def depart_worker(*, member_token: WorkerMembershipToken):\n"
        "    with fenced_member_transaction(engine, member_token=member_token, verb='depart_worker') as conn:\n"
        "        conn.execute(update(run_workers_table).where(run_workers_table.c.run_id == member_token.run_id))\n"
    )
    # The fixture is parsed AT the coordination repository's own path, because
    # member scope is keyed on the owning file. A fixture at any other path
    # classifies leader-scoped, which is itself the guard working.
    admitted = _parse_source(_RUN_COORDINATION_PATH, source)
    member_verb = next(node for node in ast.walk(admitted.tree) if isinstance(node, ast.FunctionDef))
    assert _function_fence_violation(member_verb) is None

    # A member verb fenced by the LEADER fence. The membership fence is what
    # proves this worker is still 'active'; the leader fence proves an epoch
    # the member does not hold, so it cannot stand in.
    leader_fence_on_member = _parse_source(
        admitted.path,
        source.replace(
            "fenced_member_transaction(engine, member_token=member_token, verb='depart_worker')",
            "fenced_leader_transaction(engine, token=member_token)",
        ),
    )
    crossed = next(node for node in ast.walk(leader_fence_on_member.tree) if isinstance(node, ast.FunctionDef))
    assert "member-scoped verb is fenced by fenced_leader_transaction" in (_function_fence_violation(crossed) or "")

    # The mirror: a LEADER verb fenced by the membership fence.
    member_fence_on_leader = _parse_source(
        admitted.path,
        source.replace(
            "def depart_worker(*, member_token: WorkerMembershipToken):", "def complete_run(*, member_token: WorkerMembershipToken):"
        ),
    )
    leader_verb = next(node for node in ast.walk(member_fence_on_leader.tree) if isinstance(node, ast.FunctionDef))
    assert "leader-scoped verb's token annotation is not CoordinationToken" in (_function_fence_violation(leader_verb) or "")

    # A leader verb carrying the leader token but entering the member fence:
    # the annotation is right, so only the FENCE check can catch this one.
    leader_token_member_fence = _parse_source(
        admitted.path,
        source.replace(
            "def depart_worker(*, member_token: WorkerMembershipToken):", "def complete_run(*, coordination_token: CoordinationToken):"
        )
        .replace("member_token=member_token", "member_token=coordination_token")
        .replace("member_token.run_id", "coordination_token.run_id"),
    )
    leader_crossed = next(node for node in ast.walk(leader_token_member_fence.tree) if isinstance(node, ast.FunctionDef))
    assert "leader-scoped verb is fenced by fenced_member_transaction" in (_function_fence_violation(leader_crossed) or "")

    # Optional authority is not authority, for either class.
    optional_member = _parse_source(
        admitted.path, source.replace("member_token: WorkerMembershipToken", "member_token: WorkerMembershipToken | None")
    )
    optional_verb = next(node for node in ast.walk(optional_member.tree) if isinstance(node, ast.FunctionDef))
    assert "member-scoped verb's token annotation is not WorkerMembershipToken" in (_function_fence_violation(optional_verb) or "")

    # A member verb annotated with the LEADER type: the same rule read the
    # other way, and the one that would pass if the predicate were widened to
    # accept either type instead of being keyed on the verb's scope.
    wrong_type_member = _parse_source(
        admitted.path, source.replace("member_token: WorkerMembershipToken", "member_token: CoordinationToken")
    )
    wrong_type_verb = next(node for node in ast.walk(wrong_type_member.tree) if isinstance(node, ast.FunctionDef))
    assert "member-scoped verb's token annotation is not WorkerMembershipToken" in (_function_fence_violation(wrong_type_verb) or "")

    # Scope is keyed on the OWNING FILE. Byte-identical source at any other
    # path is leader-scoped, so a same-named method on another owned type
    # cannot inherit membership semantics by its name alone. This is the arm
    # that distinguishes owner-keying from name-keying, and the orchestrator's
    # _HeartbeatRepository.worker_heartbeat Protocol is the live collision that
    # makes it load-bearing rather than defensive.
    foreign_owner = _parse_source("src/elspeth/core/landscape/execution_repository.py", source)
    foreign_verb = next(node for node in ast.walk(foreign_owner.tree) if isinstance(node, ast.FunctionDef))
    assert "leader-scoped verb's token annotation is not CoordinationToken" in (_function_fence_violation(foreign_verb) or "")

    # A CALLER may never mint its own membership. admit_follower returns the
    # token, so a legitimate mint site exists and a caller that constructs one
    # inline is manufacturing authority rather than forwarding it. The leader
    # type already had this witness; without the arm below the member type
    # would have relied on the caller rule being type-agnostic rather than on
    # anything having checked.
    caller_source = (
        "from elspeth.contracts.coordination import WorkerMembershipToken\n"
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\n"
        "\n"
        "def leave(repo: RunCoordinationRepository, *, member_token: WorkerMembershipToken):\n"
        "    repo.depart_worker(member_token=member_token)\n"
    )
    forwarding_caller = _parse_source("src/elspeth/engine/orchestrator/follower.py", caller_source)
    minting_caller = _parse_source(
        forwarding_caller.path,
        caller_source.replace(
            "repo.depart_worker(member_token=member_token)",
            "repo.depart_worker(member_token=WorkerMembershipToken(run_id='r', worker_id='w'))",
        ),
    )
    minted_rows = [item for item in _coordination_caller_authority_violations([minting_caller]) if "leave .depart_worker" in item]
    assert minted_rows == ["src/elspeth/engine/orchestrator/follower.py:5 leave .depart_worker lacks one exact current authority"]

    # Forwarding a required member parameter is authority; constructing one
    # at the call site is not. The two cases must produce different verdicts.
    forwarded_rows = [item for item in _coordination_caller_authority_violations([forwarding_caller]) if "leave .depart_worker" in item]
    assert forwarded_rows == []


@pytest.mark.parametrize(
    ("annotation", "method", "default", "rebind", "admitted"),
    [
        ("WorkerMembershipToken", "depart_worker", "", "", True),
        ("WorkerMembershipToken", "worker_heartbeat", "", "", True),
        ("CoordinationToken", "depart_worker", "", "", False),
        ("WorkerMembershipToken", "release_seat", "", "", False),
        ("WorkerMembershipToken | None", "depart_worker", "", "", False),
        ("WorkerMembershipToken", "depart_worker", " = None", "", False),
        ("WorkerMembershipToken", "depart_worker", "", "    member_token = replacement\n", False),
    ],
)
def test_coordination_caller_matches_the_verbs_authority_scope(
    annotation: str, method: str, default: str, rebind: str, admitted: bool
) -> None:
    keyword = "token" if method == "release_seat" else "member_token"
    unit = _parse_source(
        "src/elspeth/engine/orchestrator/follower.py",
        "from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken\n"
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\n"
        f"def caller(repo: RunCoordinationRepository, *, member_token: {annotation}{default}):\n"
        f"{rebind}"
        f"    repo.{method}({keyword}=member_token)\n",
    )
    rows = [item for item in _coordination_caller_authority_violations([unit]) if f"caller .{method}" in item]
    assert (rows == []) is admitted


def test_leader_bound_capability_does_not_grant_member_authority() -> None:
    source = (
        "from elspeth.contracts.coordination import CoordinationToken\n"
        "from elspeth.core.landscape.mutations import LandscapeMutationCapability\n"
        "def leave(repo, coordination_token: CoordinationToken):\n"
        "    capability = LandscapeMutationCapability(repo, coordination_token=coordination_token)\n"
        "    capability.depart_worker()\n"
    )
    unit = _parse_source("src/elspeth/engine/member_bound.py", source)
    rows = [item for item in _coordination_caller_authority_violations([unit]) if "leave .depart_worker" in item]
    assert rows == ["src/elspeth/engine/member_bound.py:5 leave .depart_worker lacks one exact current authority"]

    leader = _parse_source(unit.path, source.replace("depart_worker", "release_seat"))
    assert [item for item in _coordination_caller_authority_violations([leader]) if "leave .release_seat" in item] == []


def test_shared_subordinate_helper_is_admitted_only_when_every_caller_edge_is_fenced() -> None:
    """ADR-048 D8.8 closure: a raw-Connection helper is admitted per EDGE, never by caller count.

    A helper reached through another helper is admitted only when that
    helper is itself admitted on its own ``conn`` and forwards its own
    run-named parameter; a cyclic chain, a chain rooted in an unfenced
    owner, and a run subject that is not the caller's own parameter all
    stay red.  The one unfenced edge is the pinned fence-refusal evidence
    row, admitted only in its exact shape.
    """

    chain = _parse_source(
        "src/elspeth/core/landscape/chain.py",
        textwrap.dedent(
            """\
            from sqlalchemy import insert, update
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
            from elspeth.core.landscape.schema import runs_table, run_coordination_events_table

            def leaf(conn, *, run_id):
                conn.execute(insert(run_coordination_events_table).values(run_id=run_id))

            def middle(conn, *, run_id):
                conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status="failed"))
                leaf(conn, run_id=run_id)

            def owner(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as conn:
                    middle(conn, run_id=coordination_token.run_id)

            def sibling(*, coordination_token: CoordinationToken):
                with fenced_leader_transaction(engine, token=coordination_token) as conn:
                    leaf(conn, run_id=coordination_token.run_id)
            """
        ),
    )
    assert _transaction_order_violations([chain], scan_dml_identities([chain])) == ()

    unfenced_root = _parse_source(
        chain.path,
        chain.source.replace(
            "def owner(*, coordination_token: CoordinationToken):\n    with fenced_leader_transaction(engine, token=coordination_token) as conn:",
            "def owner(*, coordination_token: CoordinationToken):\n    with begin_write(engine) as conn:",
        ),
    )
    unfenced_violations = _transaction_order_violations([unfenced_root], scan_dml_identities([unfenced_root]))
    assert any(
        "chain.py:middle subordinate caller src/elspeth/core/landscape/chain.py:owner is not fenced" in item for item in unfenced_violations
    )
    assert any(
        "chain.py:leaf subordinate caller src/elspeth/core/landscape/chain.py:middle is reached through an unadmitted subordinate helper"
        in item
        for item in unfenced_violations
    )

    foreign_subject = _parse_source(
        chain.path, chain.source.replace("    leaf(conn, run_id=run_id)", "    leaf(conn, run_id=other_run_id)")
    )
    foreign_violations = _transaction_order_violations([foreign_subject], scan_dml_identities([foreign_subject]))
    assert any(
        "chain.py:leaf subordinate caller src/elspeth/core/landscape/chain.py:middle subordinate run subject" in item
        for item in foreign_violations
    )

    cyclic = _parse_source(
        chain.path,
        chain.source.replace(
            "    conn.execute(insert(run_coordination_events_table).values(run_id=run_id))\n",
            "    conn.execute(insert(run_coordination_events_table).values(run_id=run_id))\n    if again:\n        middle(conn, run_id=run_id)\n",
        ),
    )
    cyclic_violations = _transaction_order_violations([cyclic], scan_dml_identities([cyclic]))
    assert any("chain is cyclic" in item for item in cyclic_violations)

    evidence = _parse_source(
        _FENCE_REFUSAL_EVIDENCE_EDGE.caller_path,
        textwrap.dedent(
            """\
            from sqlalchemy import insert
            from elspeth.core.landscape.database import begin_write
            from elspeth.core.landscape.schema import run_coordination_events_table

            def record_coordination_event(conn, *, run_id):
                conn.execute(insert(run_coordination_events_table).values(run_id=run_id))

            def _record_best_effort_event(engine, *, run_id):
                with begin_write(engine) as conn:
                    record_coordination_event(conn, run_id=run_id)
            """
        ),
    )
    assert _transaction_order_violations([evidence], scan_dml_identities([evidence])) == ()

    evidence_with_own_dml = _parse_source(
        evidence.path,
        evidence.source.replace(
            "        record_coordination_event(conn, run_id=run_id)\n",
            "        record_coordination_event(conn, run_id=run_id)\n        conn.execute(insert(run_coordination_events_table).values(run_id=run_id))\n",
        ),
    )
    assert any(
        "fence-refusal-evidence caller constructs DML of its own" in item
        for item in _transaction_order_violations([evidence_with_own_dml], scan_dml_identities([evidence_with_own_dml]))
    )

    evidence_twice = _parse_source(
        evidence.path,
        evidence.source.replace(
            "        record_coordination_event(conn, run_id=run_id)\n",
            "        record_coordination_event(conn, run_id=run_id)\n        record_coordination_event(conn, run_id=run_id)\n",
        ),
    )
    assert any(
        "fence-refusal-evidence call sites=2 expected=1" in item
        for item in _transaction_order_violations([evidence_twice], scan_dml_identities([evidence_twice]))
    )

    evidence_elsewhere = _parse_source("src/elspeth/core/landscape/elsewhere.py", evidence.source)
    assert any(
        "elsewhere.py:_record_best_effort_event is not fenced" in item
        for item in _transaction_order_violations([evidence_elsewhere], scan_dml_identities([evidence_elsewhere]))
    )


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


def test_export_write_without_the_export_seat_is_still_refused() -> None:
    """ADR-048 §4 control for the export-seat establishment: the seat, not a minted token, is the authority."""

    minted = _parse_source(
        "src/elspeth/engine/orchestrator/export_shortcut.py",
        textwrap.dedent(
            """\
            from elspeth.contracts import ExportStatus
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.factory import RecorderFactory

            def resume_audit_export(db, run_id, worker_id):
                factory = RecorderFactory(db)
                coordination_token = CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=1)
                factory.run_lifecycle.set_export_status(ExportStatus.COMPLETED, coordination_token=coordination_token)
            """
        ),
    )
    assert any("set_export_status lacks one exact current token" in item for item in _caller_authority_violations([minted]))

    seated = _parse_source(
        minted.path,
        textwrap.dedent(
            """\
            from elspeth.contracts import ExportStatus
            from elspeth.contracts.coordination import CoordinationToken
            from elspeth.core.landscape.factory import RecorderFactory

            def resume_audit_export(db, run_id, worker_id):
                factory = RecorderFactory(db)
                coordination_token = factory.run_coordination.acquire_export_leadership(run_id=run_id, worker_id=worker_id, window_seconds=80.0)
                _led(factory, coordination_token=coordination_token)

            def _led(factory: RecorderFactory, *, coordination_token: CoordinationToken):
                factory.run_lifecycle.set_export_status(ExportStatus.COMPLETED, coordination_token=coordination_token)
            """
        ),
    )
    assert not any("set_export_status" in item for item in _caller_authority_violations([seated]))


def test_authority_establishment_exception_is_exact_and_non_release() -> None:
    assert _AUTHORITY_ESTABLISHMENT_EXCEPTIONS == (_FRESH_EPOCH_ONE_EXCEPTION,)
    assert tuple(item.classification for item in _AUTHORITY_ESTABLISHMENTS) == (
        "fresh-run-epoch-1-creation",
        "existing-run-leadership-claim",
        "follower-membership-admission",
        "export-seat-claim",
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
        ("run_coordination", "update", 1),
        ("run_coordination_events", "insert", 2),
        ("run_web_plugin_policy", "insert", 1),
        ("run_workers", "insert", 1),
        ("run_workers", "update", 1),
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
    assert len(_MUTATION_APIS) == 88
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

    # Member scope is keyed on the OWNING FILE, never the bare method name, and
    # the collision that makes that necessary is REAL rather than hypothetical:
    # the orchestrator declares a same-named ``worker_heartbeat`` on its
    # repository Protocol. Under name-only keying that definition would
    # classify MEMBER, which is the dangerous direction -- a leader-scoped verb
    # required to carry a follower's token. This asserts the collision still
    # exists (so the guard is not silently protecting nothing) AND that scope
    # resolution is unmoved by it.
    module_level_fences = {"fenced_member_transaction", "fenced_heartbeat_transaction", "verify_membership_fence"}
    assert _MEMBER_SCOPED_METHOD_NAMES.issubset(_COORDINATION_MUTATION_METHOD_NAMES | module_level_fences)
    foreign_definitions = sorted(
        f"{path}:{symbol}"
        for path, symbol in index
        if path != coordination_path and symbol.rsplit(".", maxsplit=1)[-1] in _MEMBER_SCOPED_METHOD_NAMES
    )
    assert foreign_definitions == [
        "src/elspeth/engine/orchestrator/heartbeat.py:_HeartbeatRepository.record_heartbeat_degraded",
        "src/elspeth/engine/orchestrator/heartbeat.py:_HeartbeatRepository.worker_heartbeat",
    ]
    for path, symbol in index:
        method = symbol.rsplit(".", maxsplit=1)[-1]
        if method not in _MEMBER_SCOPED_METHOD_NAMES:
            continue
        expected = _MEMBER_SCOPE if path == coordination_path else _LEADER_SCOPE
        assert _verb_authority_scope(path, method) == expected, f"{path}:{symbol} resolved the wrong authority scope"


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
        + _elision_notice(len(dml), 160, "DML identities")
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
        + _elision_notice(len(calls), 260, "callers")
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


def test_every_landscape_production_caller_forwards_exact_authority() -> None:
    """The caller SWEEP, split out of ``test_landscape_production_caller_set_is_frozen``.

    Split because the four pin assertions above used to sit in front of this
    sweep, and the sweep has been red for the life of the ADR-048 burn-down.
    A red test stops at its first failure, so every assertion behind it was
    DORMANT — and dormancy is indistinguishable from passing in a suite
    summary. That is the fail-open-guard shape reproduced in the gate's own
    structure: a check that exists but cannot run. Two of the pins had in fact
    drifted while masked and nothing could say so.

    Nothing is loosened by the split: same scanners, same data, same
    thresholds. The only change is that the pins can now fail on their own
    evidence while this sweep burns down.
    """
    units = _production_units()
    violations = (*_caller_authority_violations(units), *_coordination_caller_authority_violations(units))
    assert not violations, _format_violations(
        "Every Landscape production caller must forward exact authority for its scope",
        violations,
    )


def test_dataclass_generated_constructor_field_semantics() -> None:
    base = textwrap.dedent("""
        from dataclasses import dataclass, InitVar, field
        from typing import ClassVar
        from elspeth.contracts.coordination import CoordinationToken
        class Good:
            def get(self, token: CoordinationToken):
                return token
        class Evil:
            def get(self, token: CoordinationToken):
                return CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77)
        @dataclass(frozen=True)
        class Holder:
            repo: Good = Good()
        def consume(token: CoordinationToken):
            alias = Evil()
            holder = Holder()
            return holder.repo.get(token)
        """)
    cases = {
        "default_good": base,
        "alias_evil": base.replace("holder = Holder()", "holder = Holder(alias)"),
        "classvar_positional_shift": base.replace(
            "    repo: Good = Good()", "    ignored: ClassVar[Good] = Good()\n    repo: Good = Good()"
        ).replace("holder = Holder()", "holder = Holder(Evil())"),
        "initvar_not_stored": base.replace("repo: Good = Good()", "repo: InitVar[Good] = Evil()").replace(
            "holder = Holder()", "holder = Holder(Good())"
        ),
        "string_classvar": base.replace(
            "    repo: Good = Good()", "    ignored: 'ClassVar[Good]' = Good()\n    repo: Good = Good()"
        ).replace("holder = Holder()", "holder = Holder(Evil())"),
        "non_constructor_field": base.replace("repo: Good = Good()", "repo: Good = field(init=False, default=Evil())"),
        "field_factory": base.replace("repo: Good = Good()", "repo: Good = field(default_factory=Evil)"),
        "field_keyword_only": base.replace("repo: Good = Good()", "repo: Good = field(kw_only=True, default=Evil())"),
    }
    for name, source in cases.items():
        unit = _parse_source("src/elspeth/web/dataclass_review.py", source)
        owner = next(node for node in unit.tree.body if isinstance(node, ast.FunctionDef))
        statement = owner.body[-1]
        assert isinstance(statement, ast.Return)
        call = statement.value
        assert isinstance(call, ast.Call)
        admitted = _AuthorityProof((unit,)).returned_authority(call, _resolver_for_unit(unit), call, _LEADER_SCOPE)
        assert admitted is (name == "default_good"), name


def test_every_landscape_mutation_api_requires_current_typed_authority() -> None:
    violations = _api_authority_violations(_production_units())
    assert not violations, _format_violations(
        "Every normal Landscape mutation API must require non-optional exact authority for its scope",
        violations,
    )


def test_every_landscape_dml_transaction_is_full_token_fenced_first() -> None:
    units = _production_units()
    dml = scan_dml_identities(units)

    violations = _transaction_order_violations(units, dml)
    assert not violations, _format_violations(
        "Every Landscape DML owner must fence before payload SQL; Connection helpers need proven fenced callers",
        violations,
    )


def test_landscape_subordinate_helper_edge_set_is_frozen() -> None:
    """The subordinate-edge PIN, split out of the transaction sweep above.

    Same reason as the caller-set split: this pin sat behind a sweep that has
    been red throughout the burn-down, so it could not run and its drift was
    invisible. It is the pin the split exists to make runnable again.
    """
    units = _production_units()
    dml = scan_dml_identities(units)
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


@pytest.mark.parametrize(
    ("fault", "admitted"),
    [
        ("direct_relay", True),
        ("local_relay", True),
        ("two_hop_relay", True),
        ("forged_return", False),
        ("annotated_forged_return", False),
        ("forged_parameter_return", False),
        ("unknown_receiver", False),
        ("unknown_call", False),
        ("swallowed_acquire", False),
        ("conditional_fallthrough", False),
        ("bare_return", False),
        ("mixed_returns", False),
        ("rebound_result", False),
        ("cyclic_relay", False),
        ("factory_forged_run", False),
        ("factory_forged_worker", False),
        ("factory_forged_epoch", False),
    ],
)
def test_source_resolved_returned_authority(fault, admitted):
    core_path = "src/elspeth/core/landscape/run_coordination_repository.py"
    core_source = (_repo_root() / core_path).read_text()
    if fault.startswith("factory_forged_"):
        function = next(
            node
            for node in ast.walk(ast.parse(core_source))
            if isinstance(node, ast.FunctionDef) and node.name == "_acquire_run_leadership_on"
        )
        body = ast.get_source_segment(core_source, function)
        assert body is not None
        constructor = "CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=new_epoch)"
        assert body.count(constructor) == 1
        field, replacement = {
            "factory_forged_run": ("run_id=run_id", "run_id='foreign'"),
            "factory_forged_worker": ("worker_id=worker_id", "worker_id='foreign'"),
            "factory_forged_epoch": ("leader_epoch=new_epoch", "leader_epoch=1"),
        }[fault]
        changed = body.replace(constructor, constructor.replace(field, replacement))
        assert changed != body
        assert core_source.count(body) == 1
        core_source = core_source.replace(body, changed)
    imports = (
        "from elspeth.contracts.coordination import CoordinationToken, mint_worker_id, DEFAULT_RUN_LIVENESS_WINDOW_SECONDS\n"
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\n"
    )
    acquisition = (
        "repo.acquire_run_leadership(run_id=run_id, worker_id=worker_id, "
        "window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, entry_point='resume')"
    )
    annotation = " -> CoordinationToken" if fault == "annotated_forged_return" else ""
    statements = ["worker_id = mint_worker_id(run_id)", "return " + acquisition]
    if fault in {"forged_return", "annotated_forged_return"}:
        statements[-1] = "return CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=77)"
    elif fault == "local_relay":
        statements[-1:] = ["token = " + acquisition, "return token"]
    elif fault == "rebound_result":
        statements[-1:] = ["token = " + acquisition, "token = unknown()", "return token"]
    elif fault == "unknown_receiver":
        statements.insert(0, "repo = unknown()")
    elif fault == "unknown_call":
        statements[-1] = "return unknown()"
    elif fault == "swallowed_acquire":
        statements[-1:] = ["try:\n        token = " + acquisition + "\n    except Exception:\n        pass", "return token"]
    elif fault == "conditional_fallthrough":
        statements[-1] = "if run_id:\n        return " + acquisition
    elif fault == "bare_return":
        statements[-1] = "return"
    elif fault == "mixed_returns":
        statements.insert(1, "if run_id:\n        return unknown()")
    elif fault == "cyclic_relay":
        statements[-1] = "return relay(repo, run_id)"
    producer_source = (
        imports + f"def relay(repo: RunCoordinationRepository, run_id: str){annotation}:\n    " + "\n    ".join(statements) + "\n"
    )
    consumer_call = "relay(repo, run_id)"
    if fault == "two_hop_relay":
        producer_source += "\ndef public(repo: RunCoordinationRepository, run_id: str):\n    return relay(repo, run_id)\n"
        consumer_call = "public(repo, run_id)"
    elif fault == "forged_parameter_return":
        producer_source = imports + "def relay(token: CoordinationToken):\n    return token\n"
        consumer_call = "relay(CoordinationToken(run_id=run_id, worker_id='foreign', leader_epoch=1))"
    consumer_source = imports + "from elspeth.web.authority_relay import relay" + (", public" if fault == "two_hop_relay" else "") + "\n"
    consumer_source += "def consume(repo: RunCoordinationRepository, run_id: str):\n    return " + consumer_call + "\n"
    core = _parse_source(core_path, core_source)
    producer = _parse_source("src/elspeth/web/authority_relay.py", producer_source)
    consumer = _parse_source("src/elspeth/web/authority_consumer.py", consumer_source)
    # The real acquisition wrapper returns its token only after finalization;
    # include the owned admission, clock, registry and lifecycle dependencies.
    dependencies = tuple(
        unit for unit in _production_units() if unit.path.startswith("src/elspeth/core/landscape/") and unit.path != core_path
    )
    proof = _AuthorityProof((*dependencies, core, producer, consumer))
    call = next(part.value for part in ast.walk(consumer.tree) if isinstance(part, ast.Return))
    assert isinstance(call, ast.Call)
    assert proof.returned_authority(call, _resolver_for_unit(consumer), call, _LEADER_SCOPE) is admitted


@pytest.mark.parametrize(
    ("fault", "admitted"),
    [
        ("constructor_field", True),
        ("guarded_optional_field", True),
        ("inherited_property", True),
        ("extra_property_decorator", False),
        ("private_descriptor_override", False),
        ("unknown_base", False),
        ("forged_initializer", False),
        ("overridden_initializer", False),
        ("mutable_property_field", False),
        ("unguarded_optional_field", False),
        ("instance_field_write", False),
        ("nested_instance_field_write", False),
        ("vars_mutation", False),
        ("dict_mutation", False),
        ("constructor_rebound", False),
        ("foreign_property_import", False),
        ("conditional_constructor", False),
    ],
)
def test_authority_receiver_source_origins(fault, admitted):
    imports = "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\n"
    recipe = "RunCoordinationRepository(db)"
    guard = ""
    extra_decorator = ""
    extra_method = ""
    extra_class = ""
    instance = "Holder"
    mutate = ""
    if fault in {"guarded_optional_field", "unguarded_optional_field"}:
        recipe = "None if db is None else RunCoordinationRepository(db)"
        if fault == "guarded_optional_field":
            guard = "        if self._repo is None:\n            raise RuntimeError('unavailable')\n"
    elif fault == "extra_property_decorator":
        extra_decorator = "    @unknown\n"
    elif fault == "forged_initializer":
        recipe = "unknown(db)"
    elif fault == "mutable_property_field":
        extra_method = "    def replace(self, db):\n        self._repo = unknown(db)\n"
    elif fault in {"private_descriptor_override", "inherited_property", "overridden_initializer"}:
        instance = "Derived"
        extra_class = "class Derived(Holder):\n    pass\n"
        if fault == "private_descriptor_override":
            extra_class = "class Derived(Holder):\n    @property\n    def _repo(self):\n        return unknown()\n"
        elif fault == "overridden_initializer":
            extra_class = "class Derived(Holder):\n    def __init__(self, db):\n        self._repo = unknown(db)\n"
    elif fault == "instance_field_write":
        mutate = "    obj._repo = unknown(db)\n"
    elif fault == "nested_instance_field_write":
        mutate = "    def replace():\n        obj._repo = unknown(db)\n"
    elif fault == "vars_mutation":
        mutate = "    vars(obj)['_repo'] = unknown(db)\n"
    elif fault == "dict_mutation":
        mutate = "    obj.__dict__['_repo'] = unknown(db)\n"
    elif fault == "constructor_rebound":
        imports += "RunCoordinationRepository = unknown\n"
    elif fault == "foreign_property_import":
        imports += "from attacker import property\n"
    base = "(UnknownBase)" if fault == "unknown_base" else ""
    source = imports + f"class Holder{base}:\n    def __init__(self, db):\n        self._repo = {recipe}\n"
    source += extra_decorator + "    @property\n    def coordination(self):\n" + guard + "        return self._repo\n" + extra_method
    source += extra_class
    source += f"def consume(db):\n    obj = {instance}(db)\n" + mutate + "    return obj.coordination\n"
    if fault == "conditional_constructor":
        source = source.replace("    obj = Holder(db)", "    if db:\n        obj = Holder(db)")
    path = "src/elspeth/core/landscape/run_coordination_repository.py"
    core = _parse_source(path, (_repo_root() / path).read_text())
    unit = _parse_source("src/elspeth/web/receiver_probe.py", source)
    proof = _AuthorityProof((core, unit))
    function = next(part for part in unit.tree.body if isinstance(part, ast.FunctionDef) and part.name == "consume")
    expression = function.body[-1].value
    assert expression is not None
    assert (proof.receiver(expression, _resolver_for_unit(unit), expression) is not None) is admitted


def test_returned_carrier_and_twohop_authority_negative_controls():
    base = textwrap.dedent("""
    from dataclasses import dataclass
    from elspeth.contracts.coordination import WorkerMembershipToken
    from elspeth.core.landscape.execution_repository import ExecutionRepository
    @dataclass(frozen=True)
    class Carrier:
        member_token: WorkerMembershipToken
    def build(member_token: WorkerMembershipToken) -> Carrier:
        return Carrier(member_token=member_token)
    def forward(execution: ExecutionRepository, member_token: WorkerMembershipToken):
        carrier = build(member_token)
        execution.begin_node_state(member_token=carrier.member_token)
    """)
    faults = {
        "baseline": base,
        "decorated_producer": base.replace(
            "def build(",
            "def replace(fn):\n    return lambda *args: Carrier(member_token=WorkerMembershipToken(run_id='forged', worker_id='forged'))\n@replace\ndef build(",
        ),
        "decorated_carrier": base.replace("@dataclass", "def replace(cls):\n    return lambda **kwargs: forged\n@replace\n@dataclass"),
        "descriptor_interception": base.replace(
            "    member_token: WorkerMembershipToken",
            "    member_token: WorkerMembershipToken\n    def __getattribute__(self, name):\n        return WorkerMembershipToken(run_id='forged', worker_id='forged')",
        ),
        "mutated_result_dict": base.replace(
            "    execution.begin_node_state",
            "    carrier.__dict__['member_token'] = WorkerMembershipToken(run_id='forged', worker_id='forged')\n    execution.begin_node_state",
        ),
        "conditional_rebind": base.replace(
            "    return Carrier(",
            "    if condition:\n        member_token = WorkerMembershipToken(run_id='forged', worker_id='forged')\n    return Carrier(",
        ),
    }
    for name, source in faults.items():
        unit = _parse_source("src/elspeth/engine/returned_carrier_review.py", source)
        assert (not _caller_authority_violations((unit,))) is (name == "baseline"), name

    relay = textwrap.dedent("""
    from elspeth.contracts.coordination import CoordinationToken
    def identity(token: CoordinationToken):
        return token
    def relay(token: CoordinationToken):
        return identity(token)
    def consume(token: CoordinationToken):
        return relay(CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77))
    """)
    for name, source in [
        ("twohop_forged", relay),
        ("twohop_valid", relay.replace("CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77)", "token")),
    ]:
        unit = _parse_source("src/elspeth/web/relay_review.py", source)
        consume = next(n for n in unit.tree.body if isinstance(n, ast.FunctionDef) and n.name == "consume")
        call = consume.body[0].value
        assert _AuthorityProof((unit,)).returned_authority(call, _resolver_for_unit(unit), call, _LEADER_SCOPE) is (
            name == "twohop_valid"
        ), name


def test_fresh_authority_consumption_requires_dominating_binding():
    base = textwrap.dedent("""
    from elspeth.contracts.coordination import CoordinationToken
    from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository
    def consume(db, token: CoordinationToken):
        worker = 'fresh-worker'
        repo = RunLifecycleRepository(db)
        run = repo.begin_run(config={}, canonical_version='v1', leader_worker_id=worker)
        token = CoordinationToken(run_id=run.run_id, worker_id=worker, leader_epoch=1)
        return token
    """)
    fresh = "    worker = 'fresh-worker'\n    repo = RunLifecycleRepository(db)\n    run = repo.begin_run(config={}, canonical_version='v1', leader_worker_id=worker)\n    token = CoordinationToken(run_id=run.run_id, worker_id=worker, leader_epoch=1)"
    mint = "CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=99)"
    mutants = {
        "baseline": base,
        "swallowed_fresh_default": base.replace("token: CoordinationToken)", f"token: CoordinationToken = {mint})").replace(
            fresh, "    try:\n" + textwrap.indent(fresh, "    ") + "\n    except Exception:\n        pass"
        ),
        "conditional_fresh_default": base.replace("token: CoordinationToken)", f"token: CoordinationToken = {mint})").replace(
            fresh, "    if db:\n" + textwrap.indent(fresh, "    ")
        ),
        "swallowed_fresh_local": base.replace(
            fresh, f"    token = {mint}\n    try:\n" + textwrap.indent(fresh, "    ") + "\n    except Exception:\n        pass"
        ),
    }
    for name, source in mutants.items():
        unit = _parse_source("src/elspeth/web/fresh_review.py", source)
        function = next(n for n in unit.tree.body if isinstance(n, ast.FunctionDef))
        returned = function.body[-1]
        assert _AuthorityProof((unit,)).expression(returned.value, function, _resolver_for_unit(unit), returned, _LEADER_SCOPE) is (
            name == "baseline"
        ), name


def _lazy_owned_reexports(units: tuple[SourceUnit, ...]) -> dict[str, str]:
    """Prove canonical source-owned lazy exports; reject unrecognized behavior."""

    def module_name(path: str) -> str:
        return path.removeprefix("src/").removesuffix("/__init__.py").removesuffix(".py").replace("/", ".")

    owned_modules = {module_name(unit.path) for unit in units if unit.path.startswith("src/elspeth/")}

    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("unproven lazy export")

    def named(node: ast.AST, name: str) -> bool:
        return isinstance(node, ast.Name) and node.id == name

    def parse_package(unit: SourceUnit) -> dict[str, str]:
        bindings: set[str] = set()
        imports: dict[str, str] = {}
        export_sets: dict[str, tuple[str, ...]] = {}
        functions: list[ast.FunctionDef] = []

        def bind(name: str) -> None:
            require(name not in bindings)
            bindings.add(name)

        for statement in unit.tree.body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue
            if isinstance(statement, ast.ImportFrom):
                require(statement.level == 0 and statement.module is not None)
                require(statement.module in {"__future__", "importlib", "typing"} or statement.module.startswith("elspeth."))
                for alias in statement.names:
                    require(alias.name != "*")
                    name = alias.asname or alias.name
                    bind(name)
                    imports[name] = f"{statement.module}.{alias.name}"
            elif isinstance(statement, ast.If):
                require(named(statement.test, "TYPE_CHECKING") and imports.get("TYPE_CHECKING") == "typing.TYPE_CHECKING")
                require(not statement.orelse and all(isinstance(child, ast.ImportFrom) for child in statement.body))
            elif isinstance(statement, ast.Assign):
                require(len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name))
                name = statement.targets[0].id
                bind(name)
                require(isinstance(statement.value, ast.Set) or (name == "__all__" and isinstance(statement.value, ast.List)))
                require(all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in statement.value.elts))
                values = tuple(item.value for item in statement.value.elts)
                require(len(values) == len(set(values)))
                if name != "__all__":
                    export_sets[name] = values
            elif isinstance(statement, ast.FunctionDef):
                require(statement.name == "__getattr__")
                bind(statement.name)
                functions.append(statement)
            else:
                raise ValueError("unproven package statement")

        require(not ({"getattr", "globals", "AttributeError"} & bindings))
        require(len(functions) == 1)
        function = functions[0]
        require(not function.decorator_list)
        require(len(function.args.args) == 1 and not function.args.posonlyargs and not function.args.kwonlyargs)
        require(function.args.vararg is None and function.args.kwarg is None and not function.args.defaults)
        parameter = function.args.args[0].arg
        require(parameter not in {"getattr", "globals", "AttributeError"})

        def refusal(statement: ast.stmt) -> bool:
            return (
                isinstance(statement, ast.Raise)
                and statement.cause is None
                and isinstance(statement.exc, ast.Call)
                and named(statement.exc.func, "AttributeError")
                and not statement.exc.keywords
                and not any(isinstance(child, ast.Call) for argument in statement.exc.args for child in ast.walk(argument))
            )

        def cache_and_return(statements: list[ast.stmt], value_name: str) -> bool:
            if len(statements) != 2 or not isinstance(statements[0], ast.Assign) or not isinstance(statements[1], ast.Return):
                return False
            assignment, returned = statements
            if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Subscript):
                return False
            target = assignment.targets[0]
            return (
                isinstance(target.value, ast.Call)
                and named(target.value.func, "globals")
                and not target.value.args
                and not target.value.keywords
                and named(target.slice, parameter)
                and named(assignment.value, value_name)
                and named(returned.value, value_name)
            )

        def branch(conditional: ast.If) -> tuple[tuple[str, ...], str, str]:
            comparison = conditional.test
            require(isinstance(comparison, ast.Compare) and named(comparison.left, parameter))
            require(len(comparison.ops) == 1 and isinstance(comparison.ops[0], ast.In) and len(comparison.comparators) == 1)
            set_name = comparison.comparators[0]
            require(isinstance(set_name, ast.Name) and set_name.id in export_sets)
            require(bool(conditional.body) and isinstance(conditional.body[0], ast.Assign))
            assignment = conditional.body[0]
            require(len(assignment.targets) == 1 and isinstance(assignment.targets[0], ast.Name))
            value_name = assignment.targets[0].id
            require(value_name not in {parameter, "getattr", "globals", "AttributeError"} and value_name not in imports)
            lookup = assignment.value
            require(isinstance(lookup, ast.Call) and named(lookup.func, "getattr") and len(lookup.args) == 2 and not lookup.keywords)
            require(named(lookup.args[1], parameter))
            imported = lookup.args[0]
            require(isinstance(imported, ast.Call) and isinstance(imported.func, ast.Name))
            require(imports.get(imported.func.id) == "importlib.import_module" and imported.func.id != parameter)
            require(len(imported.args) == 1 and not imported.keywords)
            target = imported.args[0]
            require(isinstance(target, ast.Constant) and isinstance(target.value, str))
            require(target.value.startswith("elspeth.") and target.value in owned_modules)
            return export_sets[set_name.id], target.value, value_name

        body = function.body
        require(bool(body) and isinstance(body[0], ast.If))
        result: dict[str, str] = {}

        def add(names: tuple[str, ...], target: str) -> None:
            for name in names:
                # An existing module binding bypasses __getattr__ entirely.
                require(name not in bindings and name not in result)
                result[name] = f"{target}.{name}"

        if len(body) == 2 and refusal(body[1]):
            conditional = body[0]
            require(not conditional.orelse)
            names, target, value_name = branch(conditional)
            require(cache_and_return(conditional.body[1:], value_name))
            add(names, target)
        else:
            require(len(body) == 3)
            conditional = body[0]
            value_names: set[str] = set()
            while True:
                names, target, value_name = branch(conditional)
                require(len(conditional.body) == 1 and len(conditional.orelse) == 1)
                value_names.add(value_name)
                add(names, target)
                tail = conditional.orelse[0]
                if isinstance(tail, ast.If):
                    conditional = tail
                    continue
                require(refusal(tail))
                break
            require(len(value_names) == 1 and cache_and_return(body[1:], next(iter(value_names))))
        return {f"{module_name(unit.path)}.{name}": target for name, target in result.items()}

    result: dict[str, str] = {}
    for unit in units:
        if unit.path.startswith("src/elspeth/") and unit.path.endswith("/__init__.py"):
            try:
                result.update(parse_package(unit))
            except ValueError:
                continue
    acyclic: dict[str, str] = {}
    for exported, target in result.items():
        seen = {exported}
        current = target
        while current in result and current not in seen:
            seen.add(current)
            current = result[current]
        if current not in seen:
            acyclic[exported] = target
    return acyclic


def _d8_lazy_package_source(target: str) -> str:
    return (
        "from importlib import import_module\n"
        "_EXPORTS = {'Orchestrator'}\n"
        "def __getattr__(name):\n"
        "    if name in _EXPORTS:\n"
        f"        value = getattr(import_module({target!r}), name)\n"
        "        globals()[name] = value\n"
        "        return value\n"
        "    raise AttributeError(name)\n"
    )


def test_d8_lazy_owned_export_resolves_two_source_proven_hops() -> None:
    units = (
        _parse_source("src/elspeth/demo/__init__.py", _d8_lazy_package_source("elspeth.demo.runtime")),
        _parse_source("src/elspeth/demo/runtime/__init__.py", _d8_lazy_package_source("elspeth.demo.runtime.core")),
        _parse_source("src/elspeth/demo/runtime/core.py", "class Orchestrator:\n    pass\n"),
    )
    assert _lazy_owned_reexports(units) == {
        "elspeth.demo.Orchestrator": "elspeth.demo.runtime.Orchestrator",
        "elspeth.demo.runtime.Orchestrator": "elspeth.demo.runtime.core.Orchestrator",
    }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("def __getattr__", "@decorate\ndef __getattr__"),
        ("_EXPORTS = {'Orchestrator'}", "_EXPORTS = {'Orchestrator'}\n_EXPORTS.add('Impostor')"),
        ("_EXPORTS = {'Orchestrator'}", "if condition:\n    _EXPORTS = {'Orchestrator'}"),
        ("elspeth.demo.core", "foreign.demo.core"),
        ("from importlib import import_module", "from foreign import import_module"),
        ("import_module('elspeth.demo.core')", "import_module(target)"),
        ("), name)", "), 'Impostor')"),
        ("globals()[name]", "globals()['Impostor']"),
        ("return value", "return replacement"),
        ("        globals()[name] = value", "        value = replacement\n        globals()[name] = value"),
        ("_EXPORTS = {'Orchestrator'}", "_EXPORTS = {'Orchestrator'}\ngetattr = replacement"),
        ("_EXPORTS = {'Orchestrator'}", "_EXPORTS = {'Orchestrator'}\nOrchestrator = {'already_bound'}"),
    ],
)
def test_d8_lazy_owned_export_refuses_unproven_dispatch(before: str, after: str) -> None:
    source = _d8_lazy_package_source("elspeth.demo.core")
    assert before in source
    units = (
        _parse_source("src/elspeth/demo/__init__.py", source.replace(before, after)),
        _parse_source("src/elspeth/demo/core.py", "class Orchestrator:\n    pass\n"),
    )
    assert _lazy_owned_reexports(units) == {}


def test_d8_lazy_owned_export_refuses_cycles_and_paths_into_cycles() -> None:
    units = tuple(
        _parse_source(f"src/elspeth/{name}/__init__.py", _d8_lazy_package_source(f"elspeth.{target}"))
        for name, target in (("a", "b"), ("b", "a"), ("entry", "a"))
    )
    assert _lazy_owned_reexports(units) == {}


def test_d8_actual_cli_orchestrator_export_has_two_owned_lazy_hops() -> None:
    root = _repo_root()
    paths = (
        "src/elspeth/engine/__init__.py",
        "src/elspeth/engine/orchestrator/__init__.py",
        "src/elspeth/engine/orchestrator/core.py",
        "src/elspeth/engine/orchestrator/types.py",
        "src/elspeth/engine/orchestrator/plugin_types.py",
    )
    units = tuple(_read_source(root / path, anchor=root) for path in paths)
    exports = _lazy_owned_reexports(units)
    assert exports["elspeth.engine.Orchestrator"] == "elspeth.engine.orchestrator.Orchestrator"
    assert exports[exports["elspeth.engine.Orchestrator"]] == "elspeth.engine.orchestrator.core.Orchestrator"
    cli = _read_source(root / "src/elspeth/cli.py", anchor=root)
    constructor = next(
        node
        for node in ast.walk(cli.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_Orchestrator"
        and _symbol(node) == "_orchestrator_context"
    )
    qualified = _resolver_for_unit(cli).qualified_name(constructor.func, use=constructor)
    assert qualified == "elspeth.engine.Orchestrator"
    assert exports[exports[qualified]] == "elspeth.engine.orchestrator.core.Orchestrator"
    assert any(isinstance(node, ast.ClassDef) and node.name == "Orchestrator" for node in units[2].tree.body)


def _callback_fixture_units(*, producer_edit=None, consumer_edit=None, caller_edit=None, extra_source=""):
    producer = """from typing import Protocol
from elspeth.contracts.coordination import CoordinationToken, mint_worker_id
from elspeth.core.landscape.factory import RecorderFactory
class InitializeDatabasePhase(Protocol):
    def __call__(self): ...
class RunLifecycleCoordinator:
    def initialize_database_phase(self, db, run_id):
        try:
            factory = RecorderFactory(db)
            worker_id = mint_worker_id(run_id)
            run = factory.run_lifecycle.begin_run(run_id=run_id, leader_worker_id=worker_id)
            token = CoordinationToken(run_id=run.run_id, worker_id=worker_id, leader_epoch=1)
        except Exception:
            raise
        return factory, run, token
    def run(self, *, initialize_database_phase: InitializeDatabasePhase):
        factory, run, coordination_token = initialize_database_phase(db, run_id)
        factory.run_coordination.release_seat(token=coordination_token)
"""
    caller = """from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator
class Orchestrator:
    def __init__(self):
        self._run_lifecycle = RunLifecycleCoordinator()
    def _initialize_database_phase(self, db, run_id):
        return self._run_lifecycle.initialize_database_phase(db, run_id)
    def run(self):
        return self._run_lifecycle.run(initialize_database_phase=self._initialize_database_phase)
"""
    if producer_edit:
        producer = producer_edit(producer)
    if consumer_edit:
        producer = consumer_edit(producer)
    if caller_edit:
        caller = caller_edit(caller)
    units = [
        _parse_source("src/elspeth/engine/orchestrator/run_lifecycle.py", producer),
        _parse_source("src/elspeth/engine/orchestrator/core.py", caller),
    ]
    if extra_source:
        units.append(_parse_source("src/elspeth/extra_caller.py", extra_source))
    return tuple(units)


def _callback_fixture_admitted(units):
    unit = units[0]
    call = next(
        part
        for part in ast.walk(unit.tree)
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute) and part.func.attr == "release_seat"
    )
    owner = _owner_function(call)
    return _AuthorityProof(units).expression(call.keywords[0].value, owner, _resolver_for_unit(unit), call, _LEADER_SCOPE)


def test_callback_return_tuple_actual_source_recipe():
    assert _callback_fixture_admitted(_callback_fixture_units())


@pytest.mark.parametrize(
    "before,after",
    [
        ("leader_epoch=1", "leader_epoch=2"),
        ("worker_id=worker_id, leader_epoch=1", "worker_id='foreign', leader_epoch=1"),
        ("return factory, run, token", "return factory, token, run"),
        ("return factory, run, token", "return factory, run, unknown()"),
        ("return factory, run, token", "return factory, run"),
        ("except Exception:\n            raise", "except Exception:\n            pass"),
        ("except Exception:\n            raise", "except Exception:\n            raise\n        finally:\n            token = unknown()"),
        (
            "except Exception:\n            raise",
            "except Exception:\n            raise\n        finally:\n            return factory, run, unknown()",
        ),
        ("return factory, run, token", "if condition:\n            return factory, run, unknown()\n        return factory, run, token"),
        ("    def initialize_database_phase", "    @wrapper\n    def initialize_database_phase"),
    ],
)
def test_callback_tuple_producer_corruption_refused(before, after):
    assert not _callback_fixture_admitted(_callback_fixture_units(producer_edit=lambda source: source.replace(before, after)))


@pytest.mark.parametrize(
    "before,after",
    [
        ("initialize_database_phase: InitializeDatabasePhase", "initialize_database_phase: InitializeDatabasePhase = default_callback"),
        (
            "        factory, run, coordination_token =",
            "        initialize_database_phase = unknown\n        factory, run, coordination_token =",
        ),
        (
            "        factory.run_coordination.release_seat",
            "        coordination_token = unknown()\n        factory.run_coordination.release_seat",
        ),
        (
            "        factory, run, coordination_token =",
            "        leak(initialize_database_phase)\n        factory, run, coordination_token =",
        ),
        (
            "        factory, run, coordination_token =",
            "        def escaped():\n            return initialize_database_phase()\n        factory, run, coordination_token =",
        ),
        ("        factory, run, coordination_token =", "        if condition:\n            factory, run, coordination_token ="),
    ],
)
def test_callback_consumer_rebinding_and_escape_refused(before, after):
    assert not _callback_fixture_admitted(_callback_fixture_units(consumer_edit=lambda source: source.replace(before, after)))


@pytest.mark.parametrize(
    "before,after",
    [
        ("initialize_database_phase=self._initialize_database_phase", "initialize_database_phase=unknown"),
        ("initialize_database_phase=self._initialize_database_phase", "**parameters"),
        ("    def _initialize_database_phase", "    @wrapper\n    def _initialize_database_phase"),
        (
            "        return self._run_lifecycle.run(",
            "        self._initialize_database_phase = unknown\n        return self._run_lifecycle.run(",
        ),
        (
            "        return self._run_lifecycle.run(",
            "        setattr(self, '_initialize_database_phase', unknown)\n        return self._run_lifecycle.run(",
        ),
        ("        return self._run_lifecycle.run(", "        callback = self._run_lifecycle.run\n        return self._run_lifecycle.run("),
        ("        return self._run_lifecycle.run(initialize_database_phase=self._initialize_database_phase)", "        return None"),
        ("return self._run_lifecycle.initialize_database_phase(db, run_id)", "return forged_factory()"),
        (
            "return self._run_lifecycle.initialize_database_phase(db, run_id)",
            "self._run_lifecycle.initialize_database_phase = unknown\n        return self._run_lifecycle.initialize_database_phase(db, run_id)",
        ),
    ],
)
def test_callback_supplier_unknown_wrapped_replaced_or_escaped_refused(before, after):
    assert not _callback_fixture_admitted(_callback_fixture_units(caller_edit=lambda source: source.replace(before, after)))


@pytest.mark.parametrize(
    "source",
    [
        "def unknown_caller(repo, callback):\n    repo.run(initialize_database_phase=callback)\n",
        "from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator\ndef extra(repo: RunLifecycleCoordinator):\n    repo.run(initialize_database_phase=unknown)\n",
        "from elspeth.engine.orchestrator.core import Orchestrator\nOrchestrator._initialize_database_phase = unknown\n",
        "from elspeth.engine.orchestrator.core import Orchestrator\nsetattr(Orchestrator, '_initialize_database_phase', unknown)\n",
        "from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator\nRunLifecycleCoordinator.initialize_database_phase = unknown\n",
        "from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator\nRunLifecycleCoordinator.__dict__['initialize_database_phase'] = unknown\n",
        "from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator\nvars(RunLifecycleCoordinator)['initialize_database_phase'] = unknown\n",
        "from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator\nsetattr(RunLifecycleCoordinator, field_name, unknown)\n",
    ],
)
def test_callback_all_source_callers_and_class_replacements_checked(source):
    assert not _callback_fixture_admitted(_callback_fixture_units(extra_source=source))


"""Draft value provenance only; transaction/CAS admission stays in the existing gate."""


def _work_item_codec_contract(proof, function, resolver):
    if function.decorator_list:
        return False
    parameters = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    if len(parameters) != 1 or function.args.vararg or function.args.kwarg:
        return False
    row_name = parameters[0].arg
    body = [
        s for s in function.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
    ]
    if len(body) != 4 or not isinstance(body[-1], ast.Return) or not isinstance(body[-1].value, ast.Call):
        return False
    constructor = body[-1].value
    if resolver.qualified_name(constructor.func, use=constructor) != "elspeth.contracts.scheduler.TokenWorkItem" or constructor.args:
        return False
    carrier = proof.class_for("elspeth.contracts.scheduler.TokenWorkItem")
    if carrier is None:
        return False
    cls, class_resolver = carrier
    if cls.bases or len(cls.decorator_list) != 1:
        return False
    decorator = cls.decorator_list[0]
    if not isinstance(decorator, ast.Call) or class_resolver.qualified_name(decorator.func, use=cls) != "dataclasses.dataclass":
        return False
    if not any(k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value is True for k in decorator.keywords):
        return False
    if any(
        not isinstance(s, ast.AnnAssign)
        and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
        for s in cls.body
    ):
        return False
    fields = {s.target.id for s in cls.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    if len(constructor.keywords) != len(fields) or {k.arg for k in constructor.keywords} != fields:
        return False
    # This is a deliberately narrow codec grammar, not a function-name grant.
    # Every identity/lease field is copied from the input row. The only writes
    # to the detached mapping normalize timestamps without changing their instant.
    expected_prefix = ast.parse(f"""
data = dict({row_name})
for key in ("available_at", "created_at", "updated_at", "lease_expires_at", "barrier_blocked_at"):
    value = data[key]
    if type(value) is datetime and value.tzinfo is None:
        data[key] = value.replace(tzinfo=UTC)
try:
    lineage_path = lineage_path_from_json(data["lineage_path_json"])
except ValueError as exc:
    raise AuditIntegrityError(f"Corrupt token_work_items.lineage_path_json for work_item_id={{data['work_item_id']!r}}: {{exc}}") from exc
""").body
    if any(stable_ast_dump(actual) != stable_ast_dump(expected) for actual, expected in zip(body[:-1], expected_prefix, strict=True)):
        return False
    expected_names = {
        "dict": "dict",
        "type": "type",
        "datetime": "datetime.datetime",
        "UTC": "datetime.UTC",
        "ValueError": "ValueError",
        "AuditIntegrityError": "elspeth.contracts.errors.AuditIntegrityError",
        "lineage_path_from_json": "elspeth.contracts.identity.lineage_path_from_json",
    }
    for node in _walk_same_scope(function):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in expected_names
            and resolver.qualified_name(node, use=node) != expected_names[node.id]
        ):
            return False
    for keyword in constructor.keywords:
        if keyword.arg == "status":
            value = keyword.value
            if not isinstance(value, ast.Call) or value.keywords or len(value.args) != 1:
                return False
            if resolver.qualified_name(value.func, use=value) != "elspeth.contracts.scheduler.TokenWorkStatus":
                return False
            value = value.args[0]
        elif keyword.arg == "lineage_path":
            if not isinstance(keyword.value, ast.Name) or keyword.value.id != "lineage_path":
                return False
            continue
        else:
            value = keyword.value
        expected = ast.Subscript(value=ast.Name(id="data", ctx=ast.Load()), slice=ast.Constant(keyword.arg), ctx=ast.Load())
        if stable_ast_dump(value) != stable_ast_dump(expected):
            return False
    return True


def _claimed_name_assignments(name, function):
    assignments = []
    for node in _walk_same_scope(function):
        if not isinstance(node, ast.Name) or node.id != name or not isinstance(node.ctx, (ast.Store, ast.Del)):
            continue
        parent = next(_ancestors(node), None)
        if not isinstance(parent, (ast.Assign, ast.AnnAssign)) or parent.value is None:
            return None
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        if targets != [node]:
            return None
        assignments.append(parent)
    return assignments


def _claimed_non_none(name, use):
    expected_null = ast.parse(f"{name} is None", mode="eval").body
    expected_present = ast.parse(f"{name} is not None", mode="eval").body
    descendant = use
    for ancestor in _ancestors(use):
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            break
        if (
            isinstance(ancestor, ast.If)
            and stable_ast_dump(ancestor.test) == stable_ast_dump(expected_present)
            and descendant in ancestor.body
        ):
            return True
        descendant = ancestor
    statement = _admission_statement(use)
    block = _admission_block(statement) if statement is not None else None
    if block is not None:
        for previous in block[: block.index(statement)]:
            if (
                isinstance(previous, ast.If)
                and stable_ast_dump(previous.test) == stable_ast_dump(expected_null)
                and _authority_relay_block_terminates(previous.body)
            ):
                return True
    return False


def _claimed_sql_row(node, resolver, connection):
    """A complete durable row fetched on the supplied connection; no annotation inference."""
    if not isinstance(node, ast.Call) or node.args or node.keywords or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in {"one", "one_or_none"}:
        return None
    nullable = node.func.attr == "one_or_none"
    mappings = node.func.value
    if (
        not isinstance(mappings, ast.Call)
        or mappings.args
        or mappings.keywords
        or not isinstance(mappings.func, ast.Attribute)
        or mappings.func.attr != "mappings"
    ):
        return None
    execute = mappings.func.value
    if (
        not isinstance(execute, ast.Call)
        or len(execute.args) != 1
        or execute.keywords
        or not isinstance(execute.func, ast.Attribute)
        or execute.func.attr != "execute"
    ):
        return None
    if not isinstance(execute.func.value, ast.Name) or execute.func.value.id != connection:
        return None
    statement = execute.args[0]
    while (
        isinstance(statement, ast.Call)
        and isinstance(statement.func, ast.Attribute)
        and statement.func.attr == "where"
        and not statement.keywords
    ):
        statement = statement.func.value
    if not isinstance(statement, ast.Call) or len(statement.args) != 1 or statement.keywords:
        return None
    if resolver.qualified_name(statement.func, use=statement) != "sqlalchemy.select":
        return None
    if resolver.qualified_name(statement.args[0], use=statement) != "elspeth.core.landscape.schema.token_work_items_table":
        return None
    return nullable


class _ClaimedRowReturnProof:
    def __init__(self, authority):
        self.authority = authority

    def value(self, node, function, resolver, use, connection, seen=frozenset()):
        """Return (proven durable row, may be None), checking every possible binding."""
        key = (id(node), id(function))
        if key in seen:
            return False, False
        seen = seen | {key}
        if isinstance(node, ast.Constant) and node.value is None:
            return True, True
        if isinstance(node, ast.Name):
            bindings = _claimed_name_assignments(node.id, function)
            if not bindings:
                return False, False
            # The first source binding must dominate this use. Later writes are
            # allowed only under positive None guards, as in row = claimed.
            first = bindings[0]
            if not _receiver_binding_dominates(first.value, use):
                return False, False
            nullable = False
            for binding in bindings:
                if binding.lineno >= use.lineno:
                    return False, False
                valid, optional = self.value(binding.value, function, resolver, binding.value, connection, seen)
                if not valid:
                    return False, False
                if isinstance(binding.value, ast.Name) and _claimed_non_none(binding.value.id, binding):
                    optional = False
                nullable |= optional
            if _claimed_non_none(node.id, use):
                nullable = False
            return True, nullable
        if not isinstance(node, ast.Call):
            return False, False
        nullable = _claimed_sql_row(node, resolver, connection)
        if nullable is not None:
            return True, nullable
        if any(keyword.arg is None for keyword in node.keywords):
            return False, False
        producer = self.authority.called_function(node, resolver, use)
        if producer is None:
            return False, False
        callee, callee_resolver = producer
        if callee.decorator_list:
            return False, False
        parameters = [a for a in (*callee.args.posonlyargs, *callee.args.args) if a.arg not in {"self", "cls"}]
        if not parameters or not node.args or not isinstance(node.args[0], ast.Name) or node.args[0].id != connection:
            return False, False
        callee_conn = parameters[0].arg
        if _parameter_rebound(callee, callee_conn):
            return False, False
        if not _authority_relay_block_terminates(callee.body):
            return False, False
        returns = [part for part in _walk_same_scope(callee) if isinstance(part, ast.Return)]
        if not returns:
            return False, False
        nullable = False
        for returned in returns:
            if returned.value is None:
                return False, False
            valid, optional = self.value(returned.value, callee, callee_resolver, returned, callee_conn, seen)
            if not valid:
                return False, False
            nullable |= optional
        return True, nullable


def _claimed_work_item_return(self, call, resolver, use, seen=frozenset()):
    if id(call) in seen or any(keyword.arg is None for keyword in call.keywords):
        return False
    producer = self.called_function(call, resolver, use)
    if producer is None:
        return False
    function, source_resolver = producer
    if function.decorator_list:
        return False
    if id(function) in seen or not _authority_relay_block_terminates(function.body):
        return False
    seen = seen | {id(call), id(function)}
    returns = [part for part in _walk_same_scope(function) if isinstance(part, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Call):
        return False
    returned = returns[0].value
    codec = self.called_function(returned, source_resolver, returned)
    if codec is None:
        return False
    if not _work_item_codec_contract(self, *codec):
        return _claimed_work_item_return(self, returned, source_resolver, returned, seen)
    if len(returned.args) != 1 or returned.keywords or not isinstance(returned.args[0], ast.Name):
        return False
    row = returned.args[0]
    assignments = _claimed_name_assignments(row.id, function)
    if assignments is None or len(assignments) != 1:
        return False
    assignment = assignments[0]
    if not isinstance(assignment.value, ast.Call) or assignment.lineno >= returned.lineno:
        return False
    scopes = [ancestor for ancestor in _ancestors(assignment) if isinstance(ancestor, ast.With)]
    if len(scopes) != 1 or len(scopes[0].items) != 1:
        return False
    scope = scopes[0].items[0]
    if not isinstance(scope.context_expr, ast.Call) or not isinstance(scope.optional_vars, ast.Name):
        return False
    if (
        source_resolver.qualified_name(scope.context_expr.func, use=scope.context_expr)
        != "elspeth.core.landscape.run_coordination_repository.fenced_member_transaction"
    ):
        return False
    connection = scope.optional_vars.id
    stores = [
        part
        for part in _walk_same_scope(function)
        if isinstance(part, ast.Name) and part.id == connection and isinstance(part.ctx, (ast.Store, ast.Del))
    ]
    if stores != [scope.optional_vars]:
        return False
    if any(isinstance(ancestor, (ast.For, ast.While, ast.Try)) for ancestor in _ancestors(assignment) if ancestor is not function):
        return False
    # Codec arguments may only come from the proved helper on this fence's conn.
    valid, nullable = _ClaimedRowReturnProof(self).value(assignment.value, function, source_resolver, assignment.value, connection)
    return valid and not nullable


"""Permanent controls to copy beside the architecture gate after integration."""


_CLAIMED_ITEM_FIXTURE_PATHS = (
    "src/elspeth/contracts/scheduler.py",
    "src/elspeth/core/landscape/scheduler_repository.py",
    "src/elspeth/core/landscape/scheduler/__init__.py",
    "src/elspeth/core/landscape/scheduler/queue.py",
    "src/elspeth/core/landscape/scheduler/leases.py",
    "src/elspeth/core/landscape/scheduler/work_items.py",
)


def _claimed_control_source_change(sources, path, function_name, before, after):
    source = sources[path]
    tree = ast.parse(source)
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function_name)
    body = ast.get_source_segment(source, function)
    assert body is not None and body.count(before) == 1
    sources[path] = source.replace(body, body.replace(before, after), 1)


@pytest.mark.parametrize(
    ("fault", "admitted"),
    [
        ("actual_source_chain", True),
        ("renamed_wrapper", True),
        ("renamed_codec", True),
        ("forged_item", False),
        ("unknown_receiver", False),
        ("impostor_receiver", False),
        ("caller_rebind", False),
        ("caller_annotation", False),
        ("altered_return", False),
        ("unknown_row", False),
        ("row_rebind", False),
        ("helper_return_forged", False),
        ("helper_row_unknown", False),
        ("codec_run_forged", False),
        ("codec_copy_forged", False),
        ("codec_mutation", False),
        ("claim_return_forged", False),
        ("connection_rebind", False),
        ("unknown_decorator", False),
        ("sql_other_table", False),
        ("sql_projection", False),
        ("nullable_unguarded", False),
        ("constructor_mutator", False),
    ],
)
def test_claimed_work_item_source_return_controls(fault, admitted):
    sources = {path: (_repo_root() / path).read_text() for path in _CLAIMED_ITEM_FIXTURE_PATHS}
    queue = "src/elspeth/core/landscape/scheduler/queue.py"
    codec = "src/elspeth/core/landscape/scheduler/work_items.py"
    leases = "src/elspeth/core/landscape/scheduler/leases.py"
    expression = 'scheduler.enqueue_ready_claimed(member_token=member_token, token_id="token", row_id="row", node_id=None, step_index=0, ingest_sequence=0, row_payload_json="{}", lease_owner=member_token.worker_id, lease_seconds=30)'
    annotation = "TokenSchedulerRepository"
    prefix = ""
    rebind = ""
    declaration = "item"
    pre_call = ""
    if fault == "forged_item":
        expression = 'TokenWorkItem(run_id="forged")'
    elif fault == "unknown_receiver":
        pre_call = "    scheduler = unknown()\n"
    elif fault == "impostor_receiver":
        prefix = 'class Impostor:\n    def enqueue_ready_claimed(self, **kwargs):\n        return TokenWorkItem(run_id="forged")\n'
        annotation = "Impostor"
    elif fault == "caller_rebind":
        rebind = '    item = TokenWorkItem(run_id="forged")\n'
    elif fault == "caller_annotation":
        declaration = "item: TokenWorkItem"
        expression = "unknown()"
    elif fault == "altered_return":
        _claimed_control_source_change(
            sources, queue, "_enqueue_ready_claimed", "return item_from_mapping(row)", 'return TokenWorkItem(run_id="forged")'
        )
    elif fault == "unknown_row":
        _claimed_control_source_change(
            sources, queue, "_enqueue_ready_claimed", "return item_from_mapping(row)", "return item_from_mapping(unknown())"
        )
    elif fault == "row_rebind":
        _claimed_control_source_change(
            sources,
            queue,
            "_enqueue_ready_claimed",
            "return item_from_mapping(row)",
            "row = unknown()\n        return item_from_mapping(row)",
        )
    elif fault == "helper_return_forged":
        _claimed_control_source_change(sources, queue, "enqueue_ready_claimed_on", "return row", 'return {"work_item_id": "forged"}')
    elif fault == "helper_row_unknown":
        _claimed_control_source_change(
            sources,
            queue,
            "enqueue_ready_claimed_on",
            "row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()",
            "row = unknown()",
        )
    elif fault == "codec_run_forged":
        _claimed_control_source_change(sources, codec, "item_from_mapping", 'run_id=data["run_id"]', 'run_id="forged"')
    elif fault == "codec_copy_forged":
        _claimed_control_source_change(sources, codec, "item_from_mapping", "data = dict(row)", 'data = {"run_id": "forged"}')
    elif fault == "codec_mutation":
        _claimed_control_source_change(
            sources, codec, "item_from_mapping", "data = dict(row)", 'data = dict(row)\n    data["token_id"] = "forged"'
        )
    elif fault == "claim_return_forged":
        _claimed_control_source_change(sources, leases, "claim_ready_row", "return claimed", 'return {"work_item_id": "forged"}')
    elif fault == "connection_rebind":
        _claimed_control_source_change(
            sources,
            queue,
            "_enqueue_ready_claimed",
            "row = self.enqueue_ready_claimed_on(",
            "conn = unknown()\n            row = self.enqueue_ready_claimed_on(",
        )
    elif fault == "unknown_decorator":
        sources[queue] = sources[queue].replace("    def _enqueue_ready_claimed(", "    @unknown\n    def _enqueue_ready_claimed(")
    elif fault == "sql_other_table":
        _claimed_control_source_change(
            sources,
            leases,
            "claim_ready_row",
            "claimed = (\n            conn.execute(\n                select(token_work_items_table)",
            "claimed = (\n            conn.execute(\n                select(tokens_table)",
        )
    elif fault == "sql_projection":
        _claimed_control_source_change(
            sources,
            leases,
            "claim_ready_row",
            "claimed = (\n            conn.execute(\n                select(token_work_items_table)",
            "claimed = (\n            conn.execute(\n                select(token_work_items_table.c.work_item_id)",
        )
    elif fault == "nullable_unguarded":
        _claimed_control_source_change(
            sources, queue, "enqueue_ready_claimed_on", "if claimed is not None:\n                row = claimed", "row = claimed"
        )
    elif fault == "constructor_mutator":
        contract = "src/elspeth/contracts/scheduler.py"
        sources[contract] = sources[contract].replace(
            "    barrier_adopted_epoch: int | None = None",
            '    barrier_adopted_epoch: int | None = None\n\n    def __post_init__(self):\n        object.__setattr__(self, "token_id", "forged")',
            1,
        )
    elif fault == "renamed_wrapper":
        for path in (queue, "src/elspeth/core/landscape/scheduler_repository.py"):
            sources[path] = sources[path].replace("enqueue_ready_claimed(", "produce_owned_cursor(")
        expression = expression.replace("enqueue_ready_claimed(", "produce_owned_cursor(")
    elif fault == "renamed_codec":
        for path in (queue, codec):
            sources[path] = sources[path].replace("item_from_mapping", "hydrate_owned_cursor")
    caller = _parse_source(
        "src/elspeth/web/claimed_item_probe.py",
        "from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository\n"
        "from elspeth.contracts.coordination import WorkerMembershipToken\n"
        "from elspeth.contracts.scheduler import TokenWorkItem\n"
        + prefix
        + f"def consume(scheduler: {annotation}, member_token: WorkerMembershipToken):\n"
        + pre_call
        + f"    {declaration} = {expression}\n"
        + rebind
        + "    consume_item(work_item=item)\n",
    )
    units = (*tuple(_parse_source(path, source) for path, source in sources.items()), caller)
    proof = _AuthorityProof(units)
    owner = next(n for n in caller.tree.body if isinstance(n, ast.FunctionDef))
    use = owner.body[-1].value
    value = use.keywords[0].value
    assert proof.expression(value, owner, _resolver_for_unit(caller), use, _WORK_ITEM_SCOPE) is admitted


def test_actual_ecs_claimed_work_item_source_return():
    paths = (*_CLAIMED_ITEM_FIXTURE_PATHS, "src/elspeth/core/landscape/factory.py", "src/elspeth/web/_aws_ecs_acceptance/bedrock.py")
    units = tuple(_parse_source(path, (_repo_root() / path).read_text()) for path in paths)
    proof = _AuthorityProof(units)
    caller = units[-1]
    checked = []
    for use in ast.walk(caller.tree):
        if not isinstance(use, ast.Call):
            continue
        for keyword in use.keywords:
            if keyword.arg == "work_item" and isinstance(keyword.value, ast.Name) and keyword.value.id == "work_item":
                owner = _owner_function(use)
                assert owner is not None
                assert proof.expression(keyword.value, owner, _resolver_for_unit(caller), use, _WORK_ITEM_SCOPE)
                checked.append(use)
    assert checked


def _parameter_return_recipe(source, recipe):
    start = source.index("    def initialize_database_phase(")
    end = source.index("    def run(", start)
    return source[:start] + "    def initialize_database_phase(self, db: CoordinationToken, run_id):\n" + recipe + source[end:]


def test_fresh_callback_control_and_alias_to_fresh_remain_admitted():
    assert _callback_fixture_admitted(_callback_fixture_units())
    assert _callback_fixture_admitted(
        _callback_fixture_units(
            producer_edit=lambda source: source.replace(
                "return factory, run, token", "alias = token\n        second = alias\n        return factory, run, second"
            )
        )
    )


@pytest.mark.parametrize(
    "recipe",
    [
        "        return None, None, db\n",
        "        alias = db\n        return None, None, alias\n",
        "        first = db\n        second = first\n        return None, None, second\n",
        "        alias = identity(db)\n        return None, None, alias\n",
        "        alias = db.authority\n        return None, None, alias\n",
    ],
)
def test_callback_payload_parameter_alias_does_not_bless_unproven_invocation(recipe):
    assert not _callback_fixture_admitted(_callback_fixture_units(producer_edit=lambda source: _parameter_return_recipe(source, recipe)))


@pytest.mark.parametrize(
    "lookup",
    [
        "fn = getattr(c, 'run')",
        "fetch = getattr\n    fn = fetch(c, 'run')",
        "method_name = 'run'\n    fn = getattr(c, method_name)",
        "fn = getattr(c, method_name)",
        "fn = object.__getattribute__(c, 'run')",
        "fn = c.__getattribute__('run')",
        "fetch = object.__getattribute__\n    fn = fetch(c, 'run')",
        "fetch = c.__getattribute__\n    fn = fetch('run')",
        "from operator import attrgetter\n    fn = attrgetter('run')(c)",
        "from operator import methodcaller\n    fn = methodcaller('run')(c)",
        "from operator import attrgetter\n    fetch = attrgetter('run')\n    fn = fetch(c)",
        "fn = vars(c)['run']",
        "fn = c.__dict__['run']",
    ],
)
def test_reflective_source_resolved_callback_consumer_escape_is_refused(lookup):
    source = (
        "from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator\n"
        "def unknown(c: RunLifecycleCoordinator, supplied):\n    " + lookup + "\n"
        "    kwargs = {'initialize_database_phase': supplied}\n    return fn(**kwargs)\n"
    )
    assert not _callback_fixture_admitted(_callback_fixture_units(extra_source=source))


def test_reflective_other_receiver_and_other_member_are_not_same_consumer():
    source = """from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator
class Other:
    def run(self): pass
def unrelated(c: Other):
    return getattr(c, 'run')()
def other_member(c: RunLifecycleCoordinator):
    return getattr(c, 'initialize_database_phase')
"""
    assert _callback_fixture_admitted(_callback_fixture_units(extra_source=source))


def test_actual_six_release_calls_still_have_proven_authority():
    units = tuple(
        _parse_source(
            f"src/elspeth/engine/orchestrator/{name}.py", (_repo_root() / f"src/elspeth/engine/orchestrator/{name}.py").read_text()
        )
        for name in ("core", "run_lifecycle")
    )
    unit = units[1]
    calls = [
        part
        for part in ast.walk(unit.tree)
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute) and part.func.attr == "release_seat"
    ]
    assert len(calls) == 6
    proof = _AuthorityProof(units)
    for call in calls:
        token = next(keyword.value for keyword in call.keywords if keyword.arg == "token")
        assert proof.expression(token, _owner_function(call), _resolver_for_unit(unit), call, _LEADER_SCOPE)


def test_returned_carrier_and_twohop_authority_extended_controls():
    base = textwrap.dedent("""
    from dataclasses import dataclass
    from elspeth.contracts.coordination import WorkerMembershipToken
    from elspeth.core.landscape.execution_repository import ExecutionRepository
    @dataclass(frozen=True)
    class Carrier:
        member_token: WorkerMembershipToken
    def build(member_token: WorkerMembershipToken) -> Carrier:
        return Carrier(member_token=member_token)
    def forward(execution: ExecutionRepository, member_token: WorkerMembershipToken):
        carrier = build(member_token)
        execution.begin_node_state(member_token=carrier.member_token)
    """)
    faults = {
        "baseline": base,
        "decorated_producer": base.replace(
            "def build(",
            "def replace(fn):\n    return lambda *args: Carrier(member_token=WorkerMembershipToken(run_id='forged', worker_id='forged'))\n@replace\ndef build(",
        ),
        "decorated_carrier": base.replace("@dataclass", "def replace(cls):\n    return lambda **kwargs: forged\n@replace\n@dataclass"),
        "descriptor_interception": base.replace(
            "    member_token: WorkerMembershipToken",
            "    member_token: WorkerMembershipToken\n    def __getattribute__(self, name):\n        return WorkerMembershipToken(run_id='forged', worker_id='forged')",
        ),
        "mutated_result_dict": base.replace(
            "    execution.begin_node_state",
            "    carrier.__dict__['member_token'] = WorkerMembershipToken(run_id='forged', worker_id='forged')\n    execution.begin_node_state",
        ),
        "post_init_dict": base.replace(
            "    member_token: WorkerMembershipToken",
            "    member_token: WorkerMembershipToken\n    def __post_init__(self):\n        self.__dict__['member_token'] = WorkerMembershipToken(run_id='forged', worker_id='forged')",
        ),
        "conditional_rebind": base.replace(
            "    return Carrier(",
            "    if condition:\n        member_token = WorkerMembershipToken(run_id='forged', worker_id='forged')\n    return Carrier(",
        ),
    }
    for name, source in faults.items():
        unit = _parse_source("src/elspeth/engine/returned_carrier_review.py", source)
        assert (not _caller_authority_violations((unit,))) is (name == "baseline"), name

    relay = textwrap.dedent("""
    from elspeth.contracts.coordination import CoordinationToken
    def identity(token: CoordinationToken):
        return token
    def relay(token: CoordinationToken):
        return identity(token)
    def consume(token: CoordinationToken):
        return relay(CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77))
    """)
    for name, source in [
        ("twohop_forged", relay),
        ("twohop_alias_forged", relay.replace("    return identity(token)", "    alias = token\n    return identity(alias)")),
        ("twohop_valid", relay.replace("CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77)", "token")),
    ]:
        unit = _parse_source("src/elspeth/web/relay_review.py", source)
        consume = next(n for n in unit.tree.body if isinstance(n, ast.FunctionDef) and n.name == "consume")
        call = consume.body[0].value
        assert _AuthorityProof((unit,)).returned_authority(call, _resolver_for_unit(unit), call, _LEADER_SCOPE) is (
            name == "twohop_valid"
        ), name


@pytest.mark.parametrize(
    ("construction", "default", "admitted"),
    [
        ("Holder(Good())", "", True),
        ("Holder(repo=Good())", "", True),
        ("Holder(Evil())", "", False),
        ("Holder(repo=Evil())", "", False),
        ("Holder(alias)", "", False),
        ("Holder()", "", False),
        ("Holder()", " = Good()", True),
        ("Holder()", " = Evil()", False),
    ],
)
def test_known_dataclass_receiver_uses_actual_constructor_field(construction: str, default: str, admitted: bool) -> None:
    source = (
        textwrap.dedent("""\
        from dataclasses import dataclass
        from elspeth.contracts.coordination import CoordinationToken
        class Good:
            def get(self, token: CoordinationToken):
                return token
        class Evil:
            def get(self, token: CoordinationToken):
                return CoordinationToken(run_id='foreign', worker_id='forged', leader_epoch=77)
        @dataclass(frozen=True)
        class Holder:
            repo: Good
        def consume(token: CoordinationToken):
            alias = Evil()
            holder = CONSTRUCTION
            return holder.repo.get(token)
    """)
        .replace("CONSTRUCTION", construction)
        .replace("repo: Good\n", f"repo: Good{default}\n")
    )
    unit = _parse_source("src/elspeth/web/dataclass_receiver_probe.py", source)
    owner = next(part for part in unit.tree.body if isinstance(part, ast.FunctionDef))
    call = owner.body[-1].value
    assert isinstance(call, ast.Call)
    assert _AuthorityProof((unit,)).returned_authority(call, _resolver_for_unit(unit), call, _LEADER_SCOPE) is admitted


def test_callback_claim_and_lazy_export_independent_controls():
    def alias_supplier(source):
        start = source.index("    def initialize_database_phase(")
        end = source.index("    def run(", start)
        return (
            source[:start]
            + (
                "    def initialize_database_phase(self, db: CoordinationToken, run_id):\n"
                "        alias = db\n"
                "        return None, None, alias\n"
            )
            + source[end:]
        )

    assert _callback_fixture_admitted(_callback_fixture_units())
    assert not _callback_fixture_admitted(_callback_fixture_units(producer_edit=alias_supplier))
    assert not _callback_fixture_admitted(
        _callback_fixture_units(
            extra_source=textwrap.dedent("""
    from elspeth.engine.orchestrator.run_lifecycle import RunLifecycleCoordinator
    def unknown(c: RunLifecycleCoordinator, supplied):
        fn = getattr(c, 'run')
        kwargs = {'initialize_database_phase': supplied}
        return fn(**kwargs)
    """)
        )
    )

    sources = {path: (_repo_root() / path).read_text() for path in _CLAIMED_ITEM_FIXTURE_PATHS}
    base = textwrap.dedent("""\
    from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
    from elspeth.contracts.coordination import WorkerMembershipToken
    def consume(scheduler: TokenSchedulerRepository, member_token: WorkerMembershipToken):
        item = scheduler.enqueue_ready_claimed(member_token=member_token, token_id="token", row_id="row", node_id=None, step_index=0, ingest_sequence=0, row_payload_json="{}", lease_owner=member_token.worker_id, lease_seconds=30)
        consume_item(work_item=item)
    """)
    for name, source in [
        ("claimed_baseline", base),
        (
            "claimed_mutated_result",
            base.replace("    consume_item", '    object.__setattr__(item, "token_id", "forged")\n    consume_item'),
        ),
    ]:
        caller = _parse_source("src/elspeth/web/claimed_review.py", source)
        units = (*tuple(_parse_source(path, src) for path, src in sources.items()), caller)
        owner = next(n for n in caller.tree.body if isinstance(n, ast.FunctionDef))
        use = owner.body[-1].value
        assert _AuthorityProof(units).expression(use.keywords[0].value, owner, _resolver_for_unit(caller), use, _WORK_ITEM_SCOPE) is (
            name == "claimed_baseline"
        ), name

    package = _d8_lazy_package_source("elspeth.demo.core")
    for name, source in [
        ("lazy_baseline", package),
        (
            "lazy_builtin_shadow",
            package.replace("    if name in _EXPORTS:", "    if name in _EXPORTS:").replace(
                "        value = getattr", "        globals = replacement\n        value = getattr"
            ),
        ),
    ]:
        units = (
            _parse_source("src/elspeth/demo/__init__.py", source),
            _parse_source("src/elspeth/demo/core.py", "class Orchestrator:\n    pass\n"),
        )
        assert bool(_lazy_owned_reexports(units)) is (name == "lazy_baseline"), name


@pytest.mark.parametrize(
    ("fault", "admitted"),
    [
        ("baseline", True),
        ("keyword_baseline", True),
        ("relay_baseline", True),
        ("dataclass_baseline", True),
        ("instance_baseline", True),
        ("actual_evil", False),
        ("keyword_evil", False),
        ("relay_evil", False),
        ("returned_alias_evil", False),
        ("static_actual_evil", False),
        ("decorated_class", False),
        ("decorated_base", False),
        ("custom_metaclass", False),
        ("custom_new", False),
        ("custom_getattribute", False),
        ("method_write", False),
        ("method_alias_write", False),
        ("nested_method_write", False),
        ("class_method_write", False),
        ("initializer_method_write", False),
        ("class_body_method_write", False),
        ("dict_write", False),
        ("dict_alias_write", False),
        ("vars_write", False),
        ("setattr_write", False),
        ("setattr_alias_write", False),
        ("setattr_import_alias_write", False),
        ("object_setattr_alias_write", False),
        ("delattr_write", False),
        ("constructor_field_baseline", True),
        ("constructor_field_evil", False),
        ("constructor_alias_evil", False),
        ("constructor_property_baseline", True),
        ("constructor_property_evil", False),
        ("receiver_method_code_write", False),
        ("relay_code_write", False),
        ("method_relay_baseline", True),
        ("method_relay_evil", False),
        ("function_code_write", False),
    ],
)
def test_receiver_invocation_matches_visible_method(fault, admitted):
    source = """
from elspeth.contracts.coordination import CoordinationToken
class Good:
    def get(self, token: CoordinationToken):
        return token
class Evil:
    def get(self, token: CoordinationToken):
        return CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77)
def identity(repo: Good):
    return repo
def relay(repo: Good):
    return identity(repo)
def consume(token: CoordinationToken):
    return identity(Good()).get(token)
"""
    if fault in {"keyword_baseline", "keyword_evil"}:
        source = source.replace("identity(Good()).get", "identity(repo=Good()).get")
    elif fault in {"relay_baseline", "relay_evil"}:
        source = source.replace("identity(Good()).get", "relay(Good()).get")
    elif fault == "returned_alias_evil":
        source = source.replace("    return repo", "    alias = repo\n    return alias")
    elif fault == "static_actual_evil":
        source = source.replace(
            "def identity(repo: Good):\n    return repo",
            "class Factory:\n    @staticmethod\n    def identity(repo: Good):\n        return repo",
        )
        source = source.replace("return identity(Good()).get", "return Factory.identity(Good()).get")
    elif fault == "dataclass_baseline":
        source = "from dataclasses import dataclass\n" + source.replace("class Good:", "@dataclass\nclass Good:")
    elif fault == "decorated_class":
        source = source.replace("class Good:", "def replace(cls):\n    return Evil\n@replace\nclass Good:")
    elif fault == "decorated_base":
        source = source.replace("class Good:", "def replace(cls):\n    return Evil\n@replace\nclass Base:\n    pass\nclass Good(Base):")
    elif fault == "custom_metaclass":
        source = source.replace("class Good:", "class Good(metaclass=foreign):")
    elif fault == "custom_new":
        source = source.replace("class Good:", "class Good:\n    def __new__(cls):\n        return Evil()")
    elif fault == "custom_getattribute":
        source = source.replace("class Good:", "class Good:\n    def __getattribute__(self, name):\n        return Evil().get")
    if fault.endswith("evil"):
        source = source.replace("Good()).get", "Evil()).get")
    if fault.startswith("method_relay_"):
        source += "def delegate(repo: Good, token: CoordinationToken):\n    return repo.get(token)\n"
        source = source.replace("identity(Good()).get(token)", "delegate(Good(), token)").replace(
            "identity(Evil()).get(token)", "delegate(Evil(), token)"
        )
    if fault == "function_code_write":
        source += "def relay_authority(token: CoordinationToken):\n    return token\n"
        source = source.replace(
            "    return identity(Good()).get(token)", "    relay_authority.__code__ = Evil.get.__code__\n    return relay_authority(token)"
        )
    if fault.startswith("constructor_"):
        source += "class Holder:\n    def __init__(self, repo: Good):\n        self.repo = repo\n"
        source = source.replace("identity(Good()).get", "Holder(Good()).repo.get").replace(
            "identity(Evil()).get", "Holder(Evil()).repo.get"
        )
        if fault == "constructor_alias_evil":
            source = source.replace("        self.repo = repo", "        alias = repo\n        self.repo = alias")
        if fault.startswith("constructor_property_"):
            source = source.replace(
                "        self.repo = repo", "        self._repo = repo\n    @property\n    def repo(self):\n        return self._repo"
            )
    mutations = {
        "instance_baseline": "",
        "method_write": "    repo.get = Evil().get\n",
        "method_alias_write": "    alias = repo\n    alias.get = Evil().get\n",
        "nested_method_write": "    def replace():\n        repo.get = Evil().get\n    replace()\n",
        "class_method_write": "    Good.get = Evil.get\n",
        "initializer_method_write": "",
        "class_body_method_write": "",
        "dict_write": "    repo.__dict__['get'] = Evil().get\n",
        "dict_alias_write": "    values = repo.__dict__\n    values['get'] = Evil().get\n",
        "vars_write": "    vars(repo)['get'] = Evil().get\n",
        "setattr_write": "    setattr(repo, 'get', Evil().get)\n",
        "setattr_alias_write": "    patch = setattr\n    patch(repo, 'get', Evil().get)\n",
        "setattr_import_alias_write": "    from builtins import setattr as patch\n    patch(repo, 'get', Evil().get)\n",
        "object_setattr_alias_write": "    patch = object.__setattr__\n    patch(repo, 'get', Evil().get)\n",
        "delattr_write": "    delattr(repo, 'get')\n",
        "receiver_method_code_write": "    repo.get.__func__.__code__ = Evil.get.__code__\n",
        "relay_code_write": "    identity.__code__ = Evil.get.__code__\n",
    }
    if fault in mutations:
        source = source.replace(
            "    return identity(Good()).get(token)", "    repo = Good()\n" + mutations[fault] + "    return repo.get(token)"
        )
    if fault == "initializer_method_write":
        source = source.replace("class Good:", "class Good:\n    def __init__(self):\n        self.get = Evil().get")
    elif fault == "class_body_method_write":
        source = source.replace("        return token\nclass Evil:", "        return token\n    get = foreign\nclass Evil:")
    unit = _parse_source("src/elspeth/web/receiver_review.py", source)
    consume = next(part for part in unit.tree.body if isinstance(part, ast.FunctionDef) and part.name == "consume")
    call = consume.body[-1].value
    assert isinstance(call, ast.Call)
    assert _AuthorityProof((unit,)).returned_authority(call, _resolver_for_unit(unit), call, _LEADER_SCOPE) is admitted


def test_receiver_body_lookup_proves_runtime_subject_and_method():
    base = textwrap.dedent("""
    from elspeth.contracts.coordination import CoordinationToken
    class Good:
        def get(self, token: CoordinationToken):
            return token
    class Evil:
        def get(self, token: CoordinationToken):
            return CoordinationToken(run_id='forged', worker_id='forged', leader_epoch=77)
    def identity(repo: Good):
        return repo
    def consume(token: CoordinationToken):
        return identity(Good()).get(token)
    """)
    faults = {
        "baseline": base,
        "receiver_actual_substitution": base.replace("identity(Good()).get", "identity(Evil()).get"),
        "receiver_decorated_class": base.replace(
            "class Good:", "def replace(cls):\n    return lambda: Evil()\n@replace\nclass Good:"
        ).replace("identity(Good()).get", "Good().get"),
        "receiver_method_replaced": base.replace(
            "    return identity(Good()).get(token)", "    repo = Good()\n    repo.get = Evil().get\n    return repo.get(token)"
        ),
    }
    for name, source in faults.items():
        unit = _parse_source("src/elspeth/web/receiver_review.py", source)
        consume = next(n for n in unit.tree.body if isinstance(n, ast.FunctionDef) and n.name == "consume")
        call = consume.body[-1].value
        assert _AuthorityProof((unit,)).returned_authority(call, _resolver_for_unit(unit), call, _LEADER_SCOPE) is (name == "baseline"), (
            name
        )


@pytest.mark.parametrize(
    "path",
    [
        "src/elspeth/core/landscape/run_coordination_repository.py",
        "src/elspeth/core/landscape/run_lifecycle_repository.py",
    ],
)
def test_finalization_graph_irrelevant_evidence_has_no_writer_grant(path):
    units = (_parse_source(path, "def record_coordination_event(conn, *, run_id):\n    pass\n"),)
    assert _deadline_finalization_caller_violations(units) == ()
    assert _proven_coordination_deadline_writers(units) == frozenset()


@pytest.mark.parametrize(
    "source",
    [
        "def use(repo):\n    repo._finalize_leader_registration_on(conn, token, window_seconds=30)\n",
        "def use(repo):\n    alias = repo._finalize_follower_admission_on\n",
        "def use(repo):\n    return getattr(repo, '_finalize_' + 'leader_registration_on')\n",
        "from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository\ndef use(name):\n    return getattr(RunCoordinationRepository, name)\n",
        "class RunCoordinationRepository:\n    def _acquire_run_leadership_on(self):\n        pass\n",
        "class RunCoordinationRepository:\n    def acquire_run_leadership(self):\n        return None\n",
        "class RunCoordinationRepository:\n    def acquire_export_leadership(self):\n        return None\n",
        "class RunCoordinationRepository:\n    def admit_follower(self):\n        return None\n",
    ],
)
def test_finalization_graph_partial_capability_requires_closed_dependencies(source):
    units = (_parse_source("src/elspeth/core/landscape/run_coordination_repository.py", source),)
    assert _deadline_finalization_caller_violations(units)
    assert not _proven_coordination_deadline_writers(units)


def test_finalization_graph_initial_creation_without_finalizer_stays_relevant():
    unit = _parse_source(
        "src/elspeth/core/landscape/run_lifecycle_repository.py",
        "class RunLifecycleRepository:\n    def begin_run(self):\n        return None\n",
    )
    assert _deadline_finalization_caller_violations((unit,))


@pytest.mark.parametrize(
    "method",
    [
        "_finalize_leader_registration_on",
        "_finalize_follower_admission_on",
    ],
)
def test_finalization_graph_actual_missing_finalizer_calls_are_rejected(method):
    units = tuple(unit for unit in _production_units() if unit.path.startswith("src/elspeth/core/landscape/"))
    assert _deadline_finalization_caller_violations(units) == ()
    changed = []
    removed = 0
    for unit in units:
        tree = ast.parse(unit.source)

        class RemoveFinalizer(ast.NodeTransformer):
            def visit_Expr(self, node):
                nonlocal removed
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == method:
                    removed += 1
                    return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)

        tree = RemoveFinalizer().visit(tree)
        changed.append(_parse_source(unit.path, ast.unparse(tree)))
    assert removed > 0
    violations = _deadline_finalization_caller_violations(tuple(changed))
    assert any("caller set incomplete: " + method in finding for finding in violations)
    assert not _proven_coordination_deadline_writers(tuple(changed))
