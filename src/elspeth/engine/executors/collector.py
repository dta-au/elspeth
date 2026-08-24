"""CollectorExecutor: closes bound EXPAND groups (spec §5, WS4).

The collector is the EXPAND-group closer — roster-flushed on end_of_group
only, no trigger config, transform-only output. It shares the aggregator's
batch-transform plugin contract but is NOT an aggregator (standing ruling:
aggregator is a window, never a closer; ``executors/aggregation.py`` and
``engine/triggers.py`` are untouched by this module's existence).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from elspeth.contracts import BatchTransformProtocol, PipelineRow, TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind, NodeStateStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import (
    AuditIntegrityError,
    ExecutionError,
    OrchestrationInvariantError,
    PluginContractViolation,
)
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.config import CollectorSettings, ScopeSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.aggregation_result import validated_quarantined_indices
from elspeth.engine.clock import DEFAULT_CLOCK
from elspeth.engine.executors.state_guard import NodeStateGuard
from elspeth.engine.spans import SpanFactory

if TYPE_CHECKING:
    from elspeth.core.landscape.scheduler import BarrierRestoreReadModel
    from elspeth.engine.clock import Clock
    from elspeth.engine.tokens import TokenManager

slog = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CollectorOutcome:
    """Result of a collector arrival/loss/close operation.

    Mutual exclusivity mirrors CoalesceOutcome: held excludes release/failure;
    a release and a failure never coexist.
    """

    held: bool
    released_tokens: tuple[TokenInfo, ...] = ()
    consumed_tokens: tuple[TokenInfo, ...] = ()
    collector_name: str | None = None
    group_id: str | None = None
    failure_reason: str | None = None  # "collector_missing_members" | "collector_transform_error" | "empty_expansion" | None
    closed_without_plugin: str | None = None  # "all_members_lost" | "empty_expansion" | None

    def __post_init__(self) -> None:
        if self.held and (self.released_tokens or self.failure_reason is not None):
            raise OrchestrationInvariantError("CollectorOutcome: held=True excludes release/failure")
        if self.released_tokens and self.failure_reason is not None:
            raise OrchestrationInvariantError("CollectorOutcome: release and failure are mutually exclusive")


@dataclass(frozen=True, slots=True)
class _MemberEntry:
    token: TokenInfo
    arrival_time: float
    state_id: str
    ordinal: int


@dataclass
class _PendingGroup:
    """One open EXPAND group's roster ledger at this collector.

    Identity sets, never arithmetic (spec §2 'roster accounting'):
    closure = roster == arrived_keys | lost_keys.
    """

    roster: frozenset[str]  # minted member_keys (cross-checked authorities)
    opener_token_id: str
    ordinals: dict[str, int]  # member_key -> opener expansion ordinal
    arrived: dict[str, _MemberEntry] = field(default_factory=dict)
    lost: dict[str, str] = field(default_factory=dict)  # member_key -> reason
    first_arrival: float = 0.0


class CollectorExecutor:
    """Closes bound EXPAND groups: per-group buffers, roster accounting,
    CAS-fenced idempotent arrivals, end_of_group-only flush ordered by the
    opener's ``token_parents.ordinal``, and empty-group / all-members-lost
    closes that never invoke the plugin (spec §5).
    """

    def __init__(
        self,
        execution: ExecutionRepository,
        span_factory: SpanFactory,
        token_manager: TokenManager,
        run_id: str,
        step_resolver: StepResolver,
        data_flow: DataFlowRepository,
        clock: Clock | None = None,
        max_completed_keys: int = 10000,
        barrier_restore_reads: BarrierRestoreReadModel | None = None,
    ) -> None:
        if max_completed_keys <= 0:
            raise OrchestrationInvariantError(f"max_completed_keys must be > 0, got {max_completed_keys}")
        if barrier_restore_reads is None:
            raise OrchestrationInvariantError("barrier_restore_reads is required for collector roster/restore reads")
        self._execution = execution
        self._barrier_restore_reads = barrier_restore_reads
        self._data_flow = data_flow
        self._spans = span_factory
        self._token_manager = token_manager
        self._run_id = run_id
        self._step_resolver = step_resolver
        self._clock = clock if clock is not None else DEFAULT_CLOCK
        self._settings: dict[str, CollectorSettings] = {}
        self._scopes: dict[str, ScopeSettings] = {}
        self._node_ids: dict[str, NodeID] = {}
        self._transforms: dict[str, BatchTransformProtocol] = {}
        # (collector_name, group_id) -> _PendingGroup
        self._pending: dict[tuple[str, str], _PendingGroup] = {}
        # (collector_name, group_id) -> the settled member token_ids at
        # closure time (I-3, fix round 2): lets accept() distinguish a
        # same-token post-closure redelivery (CAS-fenced idempotent skip,
        # spec §5) from a genuinely distinct post-closure arrival (engine
        # bug). The settled set reads as empty in TWO cases (fix round 3,
        # META-20b — documented rather than fixed in-memory): (1) closures
        # this process only DISCOVERED durably (_check_landscape_for_completion)
        # rather than performed itself, and (2) a closure THIS process DID
        # perform, whose key was later evicted by the max_completed_keys
        # FIFO below — the settled set lives INSIDE this same
        # OrderedDict, so eviction discards it along with the key, and a
        # legitimate same-token redelivery arriving after eviction falls
        # through to the AuditIntegrityError exactly like the durable-
        # discovery case. Both are the same class of gap and the honest
        # fix is a durable one: a takeover has case (1) regardless, and
        # Task 7's restore mechanism must already reconstruct this memory
        # from durable state — a second in-memory structure with its own
        # bound would close the eviction half while leaving the takeover
        # half open, closing half the gap while appearing to close all of
        # it. Deliberately not attempted here; carried into Task 7's scope.
        self._completed_keys: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()
        self._max_completed_keys = max_completed_keys

    def register_collector(
        self,
        settings: CollectorSettings,
        scope: ScopeSettings,
        node_id: NodeID,
        transform: BatchTransformProtocol,
    ) -> None:
        if scope.closer != settings.name:
            raise OrchestrationInvariantError(f"Scope {scope.name!r} closer {scope.closer!r} does not name collector {settings.name!r}")
        self._settings[settings.name] = settings
        self._scopes[settings.name] = scope
        self._node_ids[settings.name] = node_id
        self._transforms[settings.name] = transform

    def get_registered_names(self) -> list[str]:
        return list(self._settings.keys())

    def buffered_member_count(self) -> int:
        """Total in-memory buffered members across open groups (EOF diagnostics)."""
        return sum(len(g.arrived) for g in self._pending.values())

    def accept(self, token: TokenInfo, collector_name: str, ctx: PluginContext, *, arrival_time: float | None = None) -> CollectorOutcome:
        if collector_name not in self._settings:
            raise OrchestrationInvariantError(f"Collector '{collector_name}' not registered")
        if not token.lineage_path or token.lineage_path[-1].kind is not FrameKind.EXPAND:
            raise OrchestrationInvariantError(
                f"Token {token.token_id} arrived at collector '{collector_name}' without an "
                f"innermost EXPAND frame (path={token.lineage_path!r}). Under §7 rule 5 every "
                f"member presents exactly one token whose innermost frame is the closer's own."
            )
        frame = token.lineage_path[-1]
        group_id = frame.group_id
        member_key = frame.member_key
        key = (collector_name, group_id)
        now = arrival_time if arrival_time is not None else self._clock.monotonic()
        node_id = self._node_ids[collector_name]

        # META-20b (fix round 3): operand order is LOAD-BEARING. `or`
        # short-circuits, so when the key is already in _completed_keys
        # (the common case — this process performed the closure and its
        # settled set has not been evicted), _check_landscape_for_completion
        # never runs and can never re-mark this key with the default-empty
        # settled set below. Reversing the operands, splitting into two
        # statements that both evaluate, or rewriting as any([...]) would
        # clobber a POPULATED settled set with an empty one on every single
        # post-closure arrival, not just after eviction — silently
        # re-breaking the I-3 skip everywhere. Any future change to this
        # condition must preserve the short-circuit shape exactly.
        if key in self._completed_keys or self._check_landscape_for_completion(collector_name, group_id):
            # I-3 (fix round 2): the old message claimed "the pending-entry
            # check below" skips same-token redelivery, but closure DELETES
            # the pending entry — that check is unreachable from here, so
            # every post-closure arrival used to crash, contradicting spec
            # §5's "duplicate arrival of the SAME token for a settled member
            # is a CAS-fenced idempotent skip". settled_token_ids (recorded
            # by every in-process close path) now makes that distinction
            # directly: this exact token already settled here -> skip;
            # anything else -> still a genuine engine bug. Empty when this
            # process only DISCOVERED completion durably rather than
            # performed it — that cross-process redelivery history belongs
            # to Task 7's restore mechanism, not this executor.
            settled_token_ids = self._completed_keys[key] if key in self._completed_keys else frozenset()
            if token.token_id in settled_token_ids:
                slog.info(
                    "collector_duplicate_arrival_skipped_post_closure",
                    collector=collector_name,
                    group_id=group_id,
                    member_key=member_key,
                    token_id=token.token_id,
                    run_id=self._run_id,
                )
                return CollectorOutcome(held=True, collector_name=collector_name, group_id=group_id)
            raise AuditIntegrityError(
                f"Token {token.token_id} (member {member_key!r}) arrived at collector "
                f"'{collector_name}' after group {group_id!r} closed, and this token is not "
                f"among the members this process settled the group with. One token per "
                f"member is build-time guaranteed (§7 rule 5); an unverifiable post-closure "
                f"arrival fails closed as an engine bug."
            )

        pending = self._pending.get(key)
        if pending is None:
            pending = self._open_group(collector_name, group_id, first_arrival=now)
        elif now < pending.first_arrival:
            pending.first_arrival = now

        if member_key not in pending.roster:
            # I-5 (fix round 2): validated BEFORE self._pending[key] is ever
            # assigned — a fresh _PendingGroup that fails this check must
            # never be installed. The old order (install-then-validate)
            # left a phantom open group with zero members that could never
            # settle: buffered_member_count() undercounts it and WS5's
            # satisfiability gate never sees it close.
            raise AuditIntegrityError(
                f"Token {token.token_id} claims member {member_key!r} of group {group_id!r} "
                f"at collector '{collector_name}' but the durable roster is {sorted(pending.roster)!r}."
            )
        existing = pending.arrived.get(member_key)
        if existing is not None:
            if existing.token.token_id == token.token_id:
                # CAS-fenced idempotent skip: lease-expiry redelivery is by design
                # (spec §5, decision 10). The durable fence is barrier_adopted_epoch;
                # this is the in-memory mirror.
                slog.info(
                    "collector_duplicate_arrival_skipped",
                    collector=collector_name,
                    group_id=group_id,
                    member_key=member_key,
                    token_id=token.token_id,
                    run_id=self._run_id,
                )
                return CollectorOutcome(held=True, collector_name=collector_name, group_id=group_id)
            raise AuditIntegrityError(
                f"Two DISTINCT tokens for member {member_key!r} of group {group_id!r} at "
                f"collector '{collector_name}': {existing.token.token_id!r} then "
                f"{token.token_id!r}. Build-time impossible (§7 rule 5) — engine bug."
            )
        if member_key in pending.lost:
            raise OrchestrationInvariantError(
                f"Member {member_key!r} of group {group_id!r} both arrived and was reported "
                f"lost at collector '{collector_name}' — a token cannot both arrive and be error-routed."
            )

        # I-5 residual (fix round 3, META-20a): the ordinal lookup must be
        # resolved and validated BEFORE begin_node_state, not just before
        # the self._pending install above. The old order (begin_node_state
        # then ordinal-check) opened a durable node_state whose state_id
        # lived only in the local `state` variable — if the ordinal check
        # then raised, that node_state was never stored into a
        # _MemberEntry and became permanently orphaned: _fail_group and
        # _execute_flush only ever complete holds reachable through
        # pending.arrived, and _roster_settled can never true for a group
        # missing this member from both arrived and lost, so the group
        # (and its orphaned node_state) could never close by any path.
        # ordinals.get(member_key) returning None is a genuine audit
        # inconsistency (pending.roster comes from token_lineage_frames,
        # pending.ordinals from token_parents — two different tables, and
        # a member can hold a frame with no token_parents row under the
        # opener) — NOT defensive padding, so the raise stays; only its
        # position relative to the durable write moves.
        ordinal = pending.ordinals.get(member_key)
        if ordinal is None:
            raise AuditIntegrityError(
                f"Member {member_key!r} of group {group_id!r} has no token_parents ordinal "
                f"under opener {pending.opener_token_id!r} — expansion audit inconsistency."
            )

        step = self._step_resolver(node_id)
        state = self._execution.begin_node_state(
            token_id=token.token_id,
            node_id=node_id,
            run_id=self._run_id,
            step_index=step,
            input_data=token.row_data.to_dict(),
            attempt=token.resume_attempt_offset,
            resume_checkpoint_id=token.resume_checkpoint_id,
        )
        # I-5 (fix round 2) + residual (fix round 3): the install moves all
        # the way down here, after every validation AND the durable write
        # that can still fail this arrival — a fresh _PendingGroup is only
        # ever made reachable via self._pending once an arrival has fully
        # succeeded through it. Existing groups (pending already installed
        # by an earlier member's successful arrival) re-assign the same
        # object, a harmless no-op.
        if key not in self._pending:
            self._pending[key] = pending
        pending.arrived[member_key] = _MemberEntry(token=token, arrival_time=now, state_id=state.state_id, ordinal=ordinal)

        if self._roster_settled(pending):
            return self._close_group(collector_name, key, pending, ctx)
        return CollectorOutcome(held=True, collector_name=collector_name, group_id=group_id)

    @staticmethod
    def _roster_settled(pending: _PendingGroup) -> bool:
        return frozenset(pending.arrived) | frozenset(pending.lost) == pending.roster

    def _open_group(self, collector_name: str, group_id: str, *, first_arrival: float) -> _PendingGroup:
        record = self._barrier_restore_reads.get_group_record(run_id=self._run_id, group_id=group_id)
        if record is None:
            raise AuditIntegrityError(
                f"Collector '{collector_name}' opened group {group_id!r} with no group_records "
                f"row — the opener's expansion transaction mints it unconditionally (spec §4.3)."
            )
        if record.kind != FrameKind.EXPAND.value:
            # I-6 (fix round 2): Task 2's reads are deliberately kind-agnostic
            # (a group_id is globally unique per run regardless of kind), so
            # without this assertion a collector mis-bound to a FORK group
            # silently proceeds with a roster of branch names and ordinals
            # keyed by token_ids, surfacing later as a confusing
            # "expansion audit inconsistency" rather than the real binding
            # error at its source.
            raise AuditIntegrityError(
                f"Collector '{collector_name}' is bound to group {group_id!r} whose "
                f"group_records.kind={record.kind!r}, not {FrameKind.EXPAND.value!r} — a "
                f"collector only closes EXPAND groups (spec §4.3); the scope/binding registry "
                f"has misassigned this collector to a {record.kind!r} group."
            )
        roster = self._barrier_restore_reads.get_group_member_keys(run_id=self._run_id, group_id=group_id)
        if len(roster) != record.member_count:
            raise AuditIntegrityError(
                f"Group {group_id!r} roster mismatch at collector '{collector_name}': "
                f"group_records.member_count={record.member_count} but "
                f"{len(roster)} DISTINCT member_key rows in token_lineage_frames (spec §5)."
            )
        ordinals = self._barrier_restore_reads.get_group_member_ordinals(run_id=self._run_id, opener_token_id=record.opener_token_id)
        return _PendingGroup(
            roster=roster,
            opener_token_id=record.opener_token_id,
            ordinals=ordinals,
            first_arrival=first_arrival,
        )

    def has_recorded_member_loss(self, collector_name: str, group_id: str, member_key: str) -> bool:
        """§5 loss dedup guard: checks in-memory state first, then the
        durable group_losses ledger (I-4, fix round 2) — the in-memory
        _pending entry carries no history before a group opens, after it
        closes, or across a takeover, so a resumed worker consulting only
        _pending would see every previously-adopted loss as unrecorded and
        risk a duplicate notify_member_lost call."""
        key = (collector_name, group_id)
        pending = self._pending.get(key)
        if pending is not None and member_key in pending.lost:
            return True
        return self._barrier_restore_reads.has_group_member_loss(
            run_id=self._run_id, closer_name=collector_name, group_id=group_id, member_key=member_key
        )

    def notify_member_lost(
        self, collector_name: str, group_id: str, member_key: str, reason: str, ctx: PluginContext
    ) -> CollectorOutcome | None:
        if collector_name not in self._settings:
            raise OrchestrationInvariantError(f"Collector '{collector_name}' not registered")
        key = (collector_name, group_id)
        # META-20b (fix round 3): same load-bearing operand order as
        # accept()'s identical condition — see the comment there. Even
        # though this method only branches on the result (both arms return
        # None either way), _check_landscape_for_completion's side effect
        # (re-marking with the default-empty settled set) would still
        # clobber a populated one in self._completed_keys if the
        # short-circuit were lost, corrupting state accept() later depends
        # on for the SAME key.
        if key in self._completed_keys or self._check_landscape_for_completion(collector_name, group_id):
            return None
        pending = self._pending.get(key)
        if pending is None:
            pending = self._open_group(collector_name, group_id, first_arrival=self._clock.monotonic())
        if member_key not in pending.roster:
            # I-5 (fix round 2): same install-after-validate ordering as
            # accept() — see the comment there.
            raise OrchestrationInvariantError(
                f"Lost member {member_key!r} not in group {group_id!r} roster at collector '{collector_name}': {sorted(pending.roster)!r}"
            )
        if key not in self._pending:
            self._pending[key] = pending
        if member_key in pending.arrived:
            raise OrchestrationInvariantError(
                f"Member {member_key!r} already arrived at collector '{collector_name}' but was reported lost — processor bug."
            )
        if member_key in pending.lost:
            raise OrchestrationInvariantError(
                f"Member {member_key!r} already marked lost at collector '{collector_name}' — "
                f"duplicate loss notification (dedup with has_recorded_member_loss first)."
            )
        pending.lost[member_key] = reason
        if self._roster_settled(pending):
            return self._close_group(collector_name, key, pending, ctx)
        return None

    def notify_empty_group(self, collector_name: str, group_id: str) -> CollectorOutcome:
        """Close a member_count=0 group (spec §6.4): no plugin, ever.

        require_all -> group failure 'empty_expansion'; best_effort -> silent
        close (parent keeps SUCCESS/FILTER_DROPPED — the caller's disposition).
        """
        if collector_name not in self._settings:
            raise OrchestrationInvariantError(f"Collector '{collector_name}' not registered")
        record = self._barrier_restore_reads.get_group_record(run_id=self._run_id, group_id=group_id)
        if record is None or record.member_count != 0:
            raise AuditIntegrityError(
                f"notify_empty_group for {group_id!r} at '{collector_name}': group_records says "
                f"{'absent' if record is None else record.member_count} — not an empty group."
            )
        key = (collector_name, group_id)
        # M-1 (fix round 2): pop defensively alongside _mark_completed, same
        # as the other three close paths (_close_group, _fail_group,
        # _execute_flush) — an M=0 group has an empty roster so nothing
        # should ever populate self._pending[key] for it, but a stray entry
        # from a future code path must not be left behind silently.
        if key in self._pending:
            del self._pending[key]
        self._mark_completed(key)
        scope = self._scopes[collector_name]
        if scope.policy == "require_all":
            return CollectorOutcome(
                held=False,
                collector_name=collector_name,
                group_id=group_id,
                failure_reason="empty_expansion",
                closed_without_plugin="empty_expansion",
            )
        return CollectorOutcome(
            held=False,
            collector_name=collector_name,
            group_id=group_id,
            closed_without_plugin="empty_expansion",
        )

    def _check_landscape_for_completion(self, collector_name: str, group_id: str) -> bool:
        # M-3 (fix round 2): the `collector_name not in self._node_ids` guard
        # this method used to open with was unreachable — both callers
        # (accept, notify_member_lost) already validate `collector_name in
        # self._settings` first, and register_collector populates
        # self._settings/self._node_ids together, so the two dicts always
        # share the same key set.
        node_id = self._node_ids[collector_name]
        if self._barrier_restore_reads.has_completed_group_for_node(run_id=self._run_id, node_id=str(node_id), group_id=group_id):
            self._mark_completed((collector_name, group_id))
            return True
        return False

    def _mark_completed(self, key: tuple[str, str], *, settled_token_ids: frozenset[str] = frozenset()) -> None:
        self._completed_keys[key] = settled_token_ids
        while len(self._completed_keys) > self._max_completed_keys:
            self._completed_keys.popitem(last=False)

    def _close_group(self, collector_name: str, key: tuple[str, str], pending: _PendingGroup, ctx: PluginContext) -> CollectorOutcome:
        """Roster closed (minted == settled as identity sets) — render the verdict.

        Verdicts wait for settlement (spec §6.3 item 3): this is only ever
        called from a settlement event that completed the roster.
        """
        scope = self._scopes[collector_name]
        group_id = key[1]
        if pending.lost and scope.policy == "require_all":
            return self._fail_group(collector_name, key, pending, failure_reason="collector_missing_members")
        if not pending.arrived:
            # best_effort, every member lost: engine closes WITHOUT the plugin
            # (spec §6.4 'all_members_lost'; not a failure under best_effort).
            del self._pending[key]
            self._mark_completed(key)
            return CollectorOutcome(
                held=False,
                collector_name=collector_name,
                group_id=group_id,
                closed_without_plugin="all_members_lost",
            )
        return self._execute_flush(collector_name, key, pending, ctx)

    def _fail_group(self, collector_name: str, key: tuple[str, str], pending: _PendingGroup, *, failure_reason: str) -> CollectorOutcome:
        """require_all group failure: engine-performed, plugin never invoked (spec §6.4)."""
        group_id = key[1]
        consumed = tuple(entry.token for entry in sorted(pending.arrived.values(), key=lambda e: e.ordinal))
        now = self._clock.monotonic()
        # I-2 (fix round 2): this method is BOTH the require_all-loss arm
        # (_close_group, pending.lost always non-empty there) AND the
        # collector_transform_error arm (_execute_flush, pending.lost is
        # typically empty and the scope may be best_effort) — the message
        # must not hardcode "under require_all" for a call it also serves
        # on a different failure_reason and policy.
        if pending.lost:
            exception_text = f"Collector group {group_id!r} failed ({failure_reason}): lost members {sorted(pending.lost)!r}"
        else:
            exception_text = f"Collector group {group_id!r} failed ({failure_reason})"
        error = ExecutionError(
            exception=exception_text,
            exception_type="CollectorGroupFailure",
            phase="collector_flush",
        )
        for entry in pending.arrived.values():
            # Close out THIS token's own accept()-time hold (canon item 11) —
            # an audit-trail bookkeeping concern, not a terminal-disposition
            # write, so it stays here regardless of who writes the outcome.
            self._execution.complete_node_state(
                state_id=entry.state_id,
                status=NodeStateStatus.FAILED,
                error=error,
                duration_ms=(now - entry.arrival_time) * 1000,
            )
        del self._pending[key]
        self._mark_completed(key, settled_token_ids=frozenset(entry.token.token_id for entry in pending.arrived.values()))
        return CollectorOutcome(
            held=False,
            collector_name=collector_name,
            group_id=group_id,
            consumed_tokens=consumed,
            failure_reason=failure_reason,
        )

    # NOTE for the WS3 integration reviewer: `_fail_group` writes NO
    # ARRIVED members' terminal disposition itself (META-11.2 fix-round
    # correction — this write used to sit here, matching what
    # CoalesceExecutor's OWN require_all-shaped merge-failure write looked
    # like before Task 6/Ruling 37 removed it: "the executor never records a
    # consumed sibling's terminal outcome itself anymore — the caller always
    # does, through the settlement channel" (test_processor.py's
    # test_coalesce_failure_always_records_through_settlement_channel).
    # META-17 (fix-round 2, I-7): `CollectorOutcome.outcomes_recorded` is
    # DELETED entirely rather than carried as a boolean — the same end state
    # `d13da9801` reached for `CoalesceOutcome` once its own flag went
    # constant-False with zero consumers. A single boolean cannot describe
    # the flush success path either (quarantined members ARE terminalized
    # here under the Ruling-36 kept exception below; survivors are not), so
    # the WS3 settle-member seam (CloserKind.COLLECTOR, wired at
    # integration) is the sole source of truth: it records whatever
    # terminal disposition it finds missing among `consumed_tokens`, full
    # stop, on every path — no flag to consult, none to keep in sync. The
    # group verdict itself (escalation vs quarantine per
    # `scope.on_group_failure`, survivor termination as `scope_group_failed`)
    # is staged by that same seam consuming this `CollectorOutcome` — the
    # executor renders, WS3 settles. Do not add escalation logic here, do
    # not reintroduce a direct `record_token_outcome` call for arrived
    # members in this method, and do not resurrect `outcomes_recorded`.

    def _execute_flush(self, collector_name: str, key: tuple[str, str], pending: _PendingGroup, ctx: PluginContext) -> CollectorOutcome:
        """end_of_group flush: opener-ordinal order, transform-only, audit-guarded."""
        group_id = key[1]
        node_id = self._node_ids[collector_name]
        transform = self._transforms[collector_name]
        step = self._step_resolver(node_id)
        entries = sorted(pending.arrived.values(), key=lambda e: e.ordinal)
        members = tuple(entry.token for entry in entries)
        pipeline_rows: list[PipelineRow] = []
        for entry in entries:
            contract = entry.token.row_data.contract
            if contract is None:
                raise OrchestrationInvariantError(
                    f"Token {entry.token.token_id} has no contract — cannot flush collector '{collector_name}' group {group_id!r}."
                )
            pipeline_rows.append(PipelineRow(entry.token.row_data.to_dict(), contract))

        batch_input = {"batch_rows": [row.to_dict() for row in pipeline_rows]}
        now = self._clock.monotonic()

        # The guard's own audit identity is anchored on the OPENER token, not
        # a member. Every arrived member already holds an OPEN node_state at
        # this same (node_id, attempt=0) from its own accept() journal entry
        # (RATIFIED canon item 11: every arrival journals a durable hold) —
        # anchoring the flush's guard on a member's token_id would collide
        # with that hold under UniqueConstraint(token_id, node_id, attempt),
        # since the guard always opens a FRESH node_state. The opener token
        # never itself visits this collector node, so it carries no prior
        # hold here. This mirrors CoalesceExecutor._execute_merge's actual
        # mechanism (verified: it opens NO new node_state at all — it only
        # completes the pre-existing per-branch arrival holds via
        # parent_completions / the except-arm's explicit loop); the collector
        # keeps ONE extra guarded state for the batch-level transform
        # invocation itself, distinct from any member's own hold, so it needs
        # an identity that cannot already be open. Per-member completion
        # below (the entries loop) is the actual "member-hold completion in
        # the style of coalesce's _execute_merge" the plan calls for.
        with NodeStateGuard(
            self._execution,
            token_id=pending.opener_token_id,
            node_id=node_id,
            run_id=self._run_id,
            step_index=step,
            input_data=batch_input,
            attempt=0,
            resume_checkpoint_id=None,
            auto_fail_phase="collector_flush",
        ) as guard:
            result = transform.process(pipeline_rows, ctx)
            if result.status != "success":
                guard.complete(
                    NodeStateStatus.FAILED,
                    duration_ms=(self._clock.monotonic() - now) * 1000,
                    error=ExecutionError(
                        exception=str(result.reason) if result.reason else "Collector transform returned error",
                        exception_type="TransformError",
                    ),
                )
                return self._fail_group(collector_name, key, pending, failure_reason="collector_transform_error")

            if result.row is None and result.rows is None:
                raise PluginContractViolation(
                    f"Collector transform '{transform.name}' returned success status but "
                    f"neither row nor rows contains data. Batch-aware transforms must return "
                    f"output via TransformResult.success(row) or TransformResult.success_multi(rows)."
                )
            output_rows: tuple[PipelineRow, ...] = (result.row,) if result.row is not None else tuple(result.rows or ())
            quarantined = validated_quarantined_indices(result, buffered_token_count=len(members), aggregation_name=collector_name)
            surviving = tuple(entry for index, entry in enumerate(entries) if index not in quarantined)
            if not surviving:
                # I-1 (fix round 2): checked unconditionally, not just when
                # output_rows is present. An all-quarantined WITHOUT output
                # flush used to fall through to collect_tokens(members=(), …)
                # and die two layers down on its own generic
                # "requires at least one member token" guard — collect_tokens
                # genuinely cannot open/close a release with zero members,
                # M=0 included, since even the M=0 empty-mint path needs one
                # representative member to key the durable group_records row.
                raise OrchestrationInvariantError(
                    f"Collector {collector_name!r} group {group_id!r}: all group members were "
                    f"quarantined (output_rows={'present' if output_rows else 'empty'}) — "
                    f"collect_tokens has no surviving representative member left to open or "
                    f"close the release with."
                )

            released = self._token_manager.collect_tokens(
                members=tuple(entry.token for entry in surviving),
                output_rows=output_rows,
                node_id=node_id,
                run_id=self._run_id,
                group_id=group_id,
            )

            duration_ms = (self._clock.monotonic() - now) * 1000
            for index, entry in enumerate(entries):
                if index in quarantined:
                    # DIRECT terminal write for a quarantined ARRIVED member — an
                    # executor-owned disposition kept permanently by the same
                    # precedent as CoalesceExecutor's `_execute_merge` crash-cleanup
                    # write (Ruling 36, `coalesce_executor.py:~1264`): the collector
                    # has no aggregation_results/batch member-action table to route
                    # a quarantine disposition through, and a quarantined-but-present
                    # member is not a `GroupLossSpec` case (it never went missing —
                    # the plugin excluded it from output on the group's SUCCESS
                    # path), so there is nothing for the WS3 settlement seam to
                    # stage here. This write does NOT get "retired into the seam" —
                    # it is the executor's own permanent responsibility, same class
                    # as Ruling 36's kept exceptions.
                    self._execution.complete_node_state(
                        state_id=entry.state_id,
                        status=NodeStateStatus.FAILED,
                        error=ExecutionError(
                            exception=f"quarantined_in_group:{group_id}:{index}",
                            exception_type="CollectorMemberQuarantine",
                            phase="collector_flush",
                        ),
                        duration_ms=duration_ms,
                    )
                    self._data_flow.record_token_outcome(
                        ref=TokenRef(token_id=entry.token.token_id, run_id=self._run_id),
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.QUARANTINED_AT_SOURCE,
                        error_hash=compute_error_hash(f"quarantined_in_group:{group_id}:{index}"),
                    )
                else:
                    self._execution.complete_node_state(
                        state_id=entry.state_id,
                        status=NodeStateStatus.COMPLETED,
                        output_data={},
                        duration_ms=duration_ms,
                    )
            # COMPLETED requires non-None output_data (output_hash would
            # otherwise be NULL — audit integrity violation); the flush
            # summary, not the released rows themselves (those are audited
            # via each child token's own token_data_ref through collect_tokens).
            guard.complete(
                NodeStateStatus.COMPLETED,
                output_data={"released_count": len(released), "member_count": len(members)},
                duration_ms=duration_ms,
            )

        del self._pending[key]
        self._mark_completed(key, settled_token_ids=frozenset(entry.token.token_id for entry in entries))
        return CollectorOutcome(
            held=False,
            released_tokens=released,
            consumed_tokens=members,
            collector_name=collector_name,
            group_id=group_id,
        )

    # Two threading notes for future callers of this module (WS3+WS4
    # integration; the WS4 plan's Task 5 §Implementation notes):
    # 1. `ctx` arrives as a parameter end to end (accept/notify_member_lost ->
    #    _close_group -> _execute_flush), mirroring how
    #    AggregationExecutor.execute_flush receives it — this executor NEVER
    #    constructs its own context. notify_empty_group takes none (it can
    #    never reach the plugin).
    # 2. Surviving members' TERMINAL outcomes are NOT written here — under the
    #    unified system the member terminals ride the WS3 settlement seam
    #    consuming CollectorOutcome.consumed_tokens. Quarantined-member
    #    FAILURE outcomes ARE written here — see the comment beside that write
    #    above (Ruling 36 kept-exception precedent, not a seam retirement).
    #    META-17: there is no `outcomes_recorded` flag to consult for this —
    #    it was deleted (I-7) because a single boolean cannot describe a
    #    mixed path (quarantined members terminalized here, survivors not).
    #    The seam must record whatever it finds unterminalized among
    #    `consumed_tokens`, unconditionally, on every `CollectorOutcome`.
