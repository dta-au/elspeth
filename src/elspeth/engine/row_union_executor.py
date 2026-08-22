"""RowUnionExecutor: releases fork-branch tokens as indivisible groups.

row_union is the correlated, same-row_id, N->N UNION ALL barrier
(elspeth-a5b86149d4 v1 contract). It holds fork-branch tokens per
(row_union_name, row_id) until EVERY declared branch has arrived, then
releases the ORIGINAL tokens as one group in declared branch order. It never
merges fields, deduplicates rows, fabricates rows, or synthesizes a wide
intermediate row — payloads pass through untouched and token_id/row_id are
preserved, but the release pops each released token's FORK frame (ruling 27:
RowUnionExecutor._pop_released_group), so branch_name/fork_group_id go None
downstream of the union. A consumer needing the pre-union branch identity
reads it from audit rows (token_lineage_frames / group_records), not the
live token.

v1 arrival policy is require_all only, fail-closed with no partial release:
- a timeout fails the whole pending group;
- a lost branch (error-routed sibling) fails the whole pending group;
- an end-of-source flush fails every incomplete group;
- a late arrival after release/failure fails that token.

The barrier mechanics (pending map, per-branch Landscape node states,
bounded completed-keys cache with Landscape fallback, duplicate-arrival
crash) deliberately mirror CoalesceExecutor so the two barrier kinds stay
recognizable siblings; the merge machinery is deliberately absent.
"""

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import structlog

from elspeth.contracts import TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import NodeStateStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError, RowUnionFailureReason
from elspeth.contracts.identity import innermost_fork_frame, pop_fork_frame
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.config import RowUnionSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.clock import DEFAULT_CLOCK, Clock

if TYPE_CHECKING:
    from elspeth.core.landscape.scheduler import BarrierRestoreReadModel

slog = structlog.get_logger(__name__)

#: Why a pending group stopped accepting arrivals. Stored per closed key so a
#: straggler's audit record names the actual cause.
_CLOSED_BY_RELEASE = "released"
_CLOSED_BY_BRANCH_LOSS = "row_union_branch_lost"
#: Conservative closure reason for a group whose FAILED node states were
#: found through the Landscape point read (cache miss after eviction or
#: resume): the state proves the group failed closed, but the original
#: reason (timeout / EOF flush) is not cheaply recoverable from that read.
_CLOSED_BY_PRIOR_FAILURE = "row_union_group_failed"


@dataclass(frozen=True, slots=True)
class RowUnionOutcome:
    """Result of a row_union accept/timeout/flush/loss evaluation.

    Attributes:
        held: True if the token is being held waiting for sibling branches.
        released_tokens: The ORIGINAL branch tokens, in declared branch
            order, when the group released. Empty otherwise.
        consumed_tokens: Tokens consumed by this outcome. On release this
            equals released_tokens (their BLOCKED journal rows are consumed
            by the same atomic barrier completion); on failure it is the
            held tokens that were failed closed.
        failure_reason: Machine-readable reason when the group failed.
        row_union_name: Name of the barrier that produced this outcome.
        outcomes_recorded: True when terminal outcomes were already recorded
            by the executor — the caller MUST NOT record them again.
        late_arrival: True when this failure is the late-arrival arm (token
            arrived after its group already released/failed).
    """

    held: bool
    released_tokens: tuple[TokenInfo, ...] = ()
    consumed_tokens: tuple[TokenInfo, ...] = ()
    failure_reason: str | None = None
    row_union_name: str | None = None
    outcomes_recorded: bool = False
    late_arrival: bool = False

    def __post_init__(self) -> None:
        if self.held and (self.released_tokens or self.failure_reason is not None):
            raise OrchestrationInvariantError("RowUnionOutcome: held=True excludes released_tokens/failure_reason")
        if self.released_tokens and self.failure_reason is not None:
            raise OrchestrationInvariantError("RowUnionOutcome: released_tokens and failure_reason are mutually exclusive")


@dataclass(frozen=True, slots=True)
class _BranchEntry:
    """Per-branch state within a pending row_union group."""

    token: TokenInfo
    arrival_time: float  # Monotonic timestamp of arrival
    state_id: str  # Landscape node_state ID for the pending hold


@dataclass
class _PendingRowUnion:
    """Tracks held tokens for a single row_id at a row_union barrier."""

    branches: dict[str, _BranchEntry]  # branch_name -> entry
    first_arrival: float  # Timeout anchor (oldest member's arrival)


@dataclass(frozen=True, slots=True)
class RowUnionRestoreEntry:
    """One adopted durable row_union hold reconstructed for resume."""

    token: TokenInfo
    row_union_name: str
    state_id: str | None
    arrival_time: float


class RowUnionExecutor:
    """Executes row_union barriers with audit recording.

    Example:
        executor = RowUnionExecutor(execution, span_factory, run_id, step_resolver, data_flow=data_flow, ...)
        executor.register_row_union(settings, node_id)
        outcome = executor.accept(token, "variant_union")
        if outcome.released_tokens:
            # The whole group continues downstream as one indivisible unit.
            ...
    """

    def __init__(
        self,
        execution: ExecutionRepository,
        span_factory: object,
        run_id: str,
        step_resolver: StepResolver,
        data_flow: DataFlowRepository,
        clock: "Clock | None" = None,
        max_completed_keys: int = 10000,
        barrier_restore_reads: "BarrierRestoreReadModel | None" = None,
    ) -> None:
        if max_completed_keys <= 0:
            raise OrchestrationInvariantError(f"max_completed_keys must be > 0, got {max_completed_keys}")
        if barrier_restore_reads is None:
            raise OrchestrationInvariantError("barrier_restore_reads is required for row_union late-arrival reads")

        self._execution = execution
        self._barrier_restore_reads = barrier_restore_reads
        self._data_flow = data_flow
        self._spans = span_factory
        self._run_id = run_id
        self._step_resolver = step_resolver
        self._clock = clock if clock is not None else DEFAULT_CLOCK

        self._settings: dict[str, RowUnionSettings] = {}
        self._node_ids: dict[str, NodeID] = {}
        # Pending groups: (row_union_name, row_id) -> _PendingRowUnion
        self._pending: dict[tuple[str, str], _PendingRowUnion] = {}
        # Completed groups (released OR failed): bounded FIFO cache backed by
        # the Landscape fallback in accept() — cache, not correctness.
        # key -> the reason the group closed, so a straggler is told WHY its
        # group is gone (released vs killed by a lost branch) instead of a
        # generic late-arrival message.
        self._completed_keys: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._max_completed_keys = max_completed_keys
        # Recent branch losses for §E.5 replay dedup, bounded like the
        # completion cache. Durable storage remains the source of truth.
        # (row_union_name, row_id, branch_name)
        self._recorded_losses: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        # Group-level recent-loss cache. Durable point reads preserve
        # correctness after an entry leaves this bounded resident index.
        self._recorded_loss_groups: OrderedDict[tuple[str, str], None] = OrderedDict()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_row_union(self, settings: RowUnionSettings, node_id: NodeID) -> None:
        """Register a row_union barrier's config and graph node id."""
        self._settings[settings.name] = settings
        self._node_ids[settings.name] = node_id

    def get_registered_names(self) -> list[str]:
        """Names of all registered row_union barriers."""
        return list(self._settings.keys())

    def has_timeout_configured(self) -> bool:
        """Return whether any registered row_union needs wall-clock polling."""
        return any(settings.timeout_seconds is not None for settings in self._settings.values())

    def node_id_for(self, row_union_name: str) -> NodeID:
        """Graph node id for a registered row_union barrier."""
        if row_union_name not in self._node_ids:
            raise OrchestrationInvariantError(f"row_union '{row_union_name}' not registered")
        return self._node_ids[row_union_name]

    def restore_from_journal(
        self,
        *,
        entries: Sequence[RowUnionRestoreEntry],
    ) -> tuple[RowUnionOutcome, ...]:
        """Restore adopted pending groups from durable scheduler and audit rows.

        Validation completes before executor memory is replaced. Entries whose
        key already closed in the Landscape are begin-window late-arrival
        residuals (elspeth-6d37341e45: _fail_late_arrival crashed between
        begin_node_state and complete_node_state, stranding an OPEN hold) and
        fail immediately with the key's true closure reason; groups with a
        durable branch loss fail through point reads without preloading
        historical losses; fully adopted groups are released. The caller must
        commit returned outcomes through the scheduler barrier-completion seam.
        """
        if self._pending:
            raise OrchestrationInvariantError("row_union restore requires an empty executor pending map")

        restored: dict[tuple[str, str], _PendingRowUnion] = {}
        for entry in entries:
            if entry.row_union_name not in self._settings:
                raise OrchestrationInvariantError(f"Cannot restore unknown row_union '{entry.row_union_name}'")
            branch_name = entry.token.branch_name
            if branch_name is None:
                raise OrchestrationInvariantError(f"Cannot restore row_union token {entry.token.token_id}: branch_name is None")
            settings = self._settings[entry.row_union_name]
            if branch_name not in settings.branches:
                raise OrchestrationInvariantError(
                    f"Cannot restore branch '{branch_name}' into row_union '{entry.row_union_name}'; "
                    f"expected one of {list(settings.branches)}"
                )
            key = (entry.row_union_name, entry.token.row_id)
            pending = restored.setdefault(key, _PendingRowUnion(branches={}, first_arrival=entry.arrival_time))
            if branch_name in pending.branches:
                raise OrchestrationInvariantError(
                    f"Duplicate restored branch '{branch_name}' for row_union '{entry.row_union_name}', row {entry.token.row_id}"
                )
            if entry.state_id is None:
                raise OrchestrationInvariantError(f"Cannot restore row_union token {entry.token.token_id} without an OPEN node state")
            pending.first_arrival = min(pending.first_arrival, entry.arrival_time)
            pending.branches[branch_name] = _BranchEntry(
                token=entry.token,
                arrival_time=entry.arrival_time,
                state_id=entry.state_id,
            )

        # A restored entry at a Landscape-closed key is a begin-window
        # late-arrival residual: every closure writer (release, _fail_pending,
        # _fail_late_arrival) completes held states before the key closes, so
        # an OPEN hold surviving at a closed key can only be the crash prefix
        # of _fail_late_arrival — which records its terminal outcome AFTER
        # completing the state, so the residual owes both writes. Classifying
        # closed keys ahead of the release arm also keeps a residual-only
        # group that happens to cover every branch from replaying the release.
        closed_keys: dict[tuple[str, str], str] = {}
        durable_loss_keys: list[tuple[str, str]] = []
        for key in restored:
            if key in self._completed_keys or self._check_landscape_for_completion(key[0], key[1]):
                closed_keys[key] = self._completed_keys.get(key, _CLOSED_BY_RELEASE)
            elif key in self._recorded_loss_groups or self._barrier_restore_reads.has_branch_loss_for_group(
                run_id=self._run_id,
                barrier_name=key[0],
                row_id=key[1],
            ):
                durable_loss_keys.append(key)

        self._pending = restored
        outcomes: list[RowUnionOutcome] = []
        for key, closed_reason in closed_keys.items():
            failure_reason = "late_arrival_after_release" if closed_reason == _CLOSED_BY_RELEASE else closed_reason
            outcomes.append(self._fail_pending(self._settings[key[0]], key, failure_reason))
            # _fail_pending recaches the key under the residual's failure
            # reason; the GROUP's closure predates the residual — keep it.
            self._mark_completed(key, closed_reason)
        for key in durable_loss_keys:
            outcomes.append(self._fail_pending(self._settings[key[0]], key, "row_union_branch_lost"))
        for key in tuple(self._pending):
            settings = self._settings[key[0]]
            pending = self._pending[key]
            if set(pending.branches) == set(settings.branches):
                outcomes.append(self._execute_release(settings=settings, key=key, pending=pending))
        return tuple(outcomes)

    def restore_branch_losses(self, losses: Sequence[tuple[str, str, str]]) -> None:
        """Restore the durable lost-branch index before pending groups reopen."""
        for row_union_name, row_id, branch_name in losses:
            if row_union_name not in self._settings:
                raise OrchestrationInvariantError(f"Cannot restore loss for unknown row_union '{row_union_name}'")
            if branch_name not in self._settings[row_union_name].branches:
                raise OrchestrationInvariantError(f"Cannot restore loss for branch '{branch_name}' in row_union '{row_union_name}'")
            self._remember_branch_loss(row_union_name, row_id, branch_name)

    def reconcile_released_group(
        self,
        *,
        entries: Sequence[RowUnionRestoreEntry],
    ) -> RowUnionOutcome:
        """Finish a release whose node-state writes preceded scheduler commit.

        A completed Landscape state proves that the group reached the release
        arm. Entries still carrying an OPEN state are the prefix/suffix left by
        a crash during state completion; already-completed entries carry
        ``state_id=None`` and are not written twice.
        """
        if not entries:
            raise OrchestrationInvariantError("Cannot reconcile an empty row_union release")
        row_union_name = entries[0].row_union_name
        row_id = entries[0].token.row_id
        key = (row_union_name, row_id)
        if row_union_name not in self._settings:
            raise OrchestrationInvariantError(f"Cannot reconcile unknown row_union '{row_union_name}'")
        if key in self._pending or key in self._completed_keys or key in self._recorded_loss_groups:
            raise OrchestrationInvariantError(f"Cannot reconcile non-pristine row_union group {row_union_name}/{row_id}")

        by_branch: dict[str, RowUnionRestoreEntry] = {}
        for entry in entries:
            if entry.row_union_name != row_union_name or entry.token.row_id != row_id:
                raise OrchestrationInvariantError("Row_union release reconciliation mixed group identities")
            branch_name = entry.token.branch_name
            if branch_name is None or branch_name in by_branch:
                raise OrchestrationInvariantError("Row_union release reconciliation has a missing or duplicate branch")
            by_branch[branch_name] = entry

        settings = self._settings[row_union_name]
        if set(by_branch) != set(settings.branches):
            raise OrchestrationInvariantError(
                f"Row_union release reconciliation for {row_union_name}/{row_id} has branches "
                f"{sorted(by_branch)}; expected {list(settings.branches)}"
            )

        now = self._clock.monotonic()
        for entry in by_branch.values():
            if entry.state_id is not None:
                self._execution.complete_node_state(
                    state_id=entry.state_id,
                    status=NodeStateStatus.COMPLETED,
                    output_data=entry.token.row_data.to_dict(),
                    duration_ms=(now - entry.arrival_time) * 1000,
                )

        released = self._pop_released_group(tuple(by_branch[branch_name].token for branch_name in settings.branches))
        self._mark_completed(key, _CLOSED_BY_RELEASE)
        return RowUnionOutcome(
            held=False,
            released_tokens=released,
            consumed_tokens=released,
            row_union_name=row_union_name,
        )

    # ------------------------------------------------------------------
    # Accept
    # ------------------------------------------------------------------

    def accept(
        self,
        token: TokenInfo,
        row_union_name: str,
        *,
        arrival_time: float | None = None,
    ) -> RowUnionOutcome:
        """Accept a fork-branch token at a row_union barrier.

        Releases the group (all declared branches, declared order) when this
        arrival completes it; otherwise holds. ``arrival_time`` carries the
        journal-first intake's backdated monotonic anchor (ADR-030 §E.2) so
        timeout anchors survive leader takeover; ``None`` uses the live clock.
        """
        if row_union_name not in self._settings:
            raise OrchestrationInvariantError(f"row_union '{row_union_name}' not registered")
        if token.branch_name is None:
            raise OrchestrationInvariantError(f"Token {token.token_id} has no branch_name - only forked tokens can join a row_union")

        settings = self._settings[row_union_name]
        node_id = self._node_ids[row_union_name]
        step = self._step_resolver(node_id)

        if token.branch_name not in settings.branches:
            raise OrchestrationInvariantError(
                f"Token branch '{token.branch_name}' not in expected branches for row_union '{row_union_name}': {list(settings.branches)}"
            )

        key = (row_union_name, token.row_id)
        now = arrival_time if arrival_time is not None else self._clock.monotonic()

        if key in self._completed_keys or self._check_landscape_for_completion(row_union_name, token.row_id):
            return self._fail_late_arrival(
                token,
                settings,
                node_id,
                step,
                closed_reason=self._completed_keys.get(key, _CLOSED_BY_RELEASE),
            )
        if key in self._recorded_loss_groups or self._barrier_restore_reads.has_branch_loss_for_group(
            run_id=self._run_id, barrier_name=row_union_name, row_id=token.row_id
        ):
            self._mark_completed(key, _CLOSED_BY_BRANCH_LOSS)
            return self._fail_late_arrival(
                token,
                settings,
                node_id,
                step,
                closed_reason=_CLOSED_BY_BRANCH_LOSS,
            )

        if key not in self._pending:
            self._pending[key] = _PendingRowUnion(branches={}, first_arrival=now)

        pending = self._pending[key]
        if now < pending.first_arrival:
            # §H doctrine: the timeout anchor pins to the OLDEST member's
            # durable arrival, even when adoption order is backdated.
            pending.first_arrival = now

        if token.branch_name in pending.branches:
            existing = pending.branches[token.branch_name]
            raise OrchestrationInvariantError(
                f"Duplicate arrival for branch '{token.branch_name}' at row_union '{row_union_name}'. "
                f"Existing token: {existing.token.token_id}, new token: {token.token_id}. "
                f"This indicates a bug in fork, retry, or checkpoint/resume logic."
            )

        state = self._execution.begin_node_state(
            token_id=token.token_id,
            node_id=node_id,
            run_id=self._run_id,
            step_index=step,
            input_data=token.row_data.to_dict(),
            attempt=token.resume_attempt_offset,
            resume_checkpoint_id=token.resume_checkpoint_id,
        )
        pending.branches[token.branch_name] = _BranchEntry(token=token, arrival_time=now, state_id=state.state_id)

        if set(pending.branches.keys()) == set(settings.branches.keys()):
            return self._execute_release(settings=settings, key=key, pending=pending)

        return RowUnionOutcome(held=True, row_union_name=row_union_name)

    # ------------------------------------------------------------------
    # Release / failure
    # ------------------------------------------------------------------

    @staticmethod
    def _pop_released_group(released: Sequence[TokenInfo]) -> tuple[TokenInfo, ...]:
        """Pop the shared FORK frame off every released token (ruling 27).

        A row_union release ends the branches' fork identity: the released
        tokens continue downstream sharing the union's post-fork lineage, not
        the popped branch frame. Every released token must carry the SAME
        FORK frame — but not necessarily as the innermost/last frame. A
        row-multiplying transform inside a branch (e.g. an expand) stacks an
        EXPAND frame on top of the branch's FORK frame before the token
        reaches the union (elspeth-a5b86149d4;
        tests/integration/pipeline/test_row_union_branch_cardinality.py), so
        this locates each token's FORK frame via innermost_fork_frame and
        pops exactly that frame via pop_fork_frame, leaving any surviving
        EXPAND frame in place — discarding it would fabricate lineage.
        """
        release_group_ids: set[str] = set()
        for token in released:
            frame = innermost_fork_frame(token.lineage_path)
            if frame is None:
                raise OrchestrationInvariantError(f"row_union release: token {token.token_id!r} has no FORK frame to pop (ruling 27 pop)")
            release_group_ids.add(frame.group_id)
        if len(release_group_ids) != 1:
            raise OrchestrationInvariantError(
                f"row_union release: released tokens do not share one FORK group "
                f"(got {sorted(release_group_ids)!r}) — ruling 27 pop requires it"
            )
        (fork_group_id,) = release_group_ids
        return tuple(replace(token, lineage_path=pop_fork_frame(token.lineage_path, group_id=fork_group_id)) for token in released)

    def _execute_release(
        self,
        *,
        settings: RowUnionSettings,
        key: tuple[str, str],
        pending: _PendingRowUnion,
    ) -> RowUnionOutcome:
        """Release the full group in declared branch order.

        Completes each held branch's node state as COMPLETED with its
        pass-through payload. NO terminal token outcomes are recorded — the
        released tokens are not terminal at the barrier; they continue
        downstream and terminate there.
        """
        now = self._clock.monotonic()
        released: list[TokenInfo] = []
        for branch_name in settings.branches:
            entry = pending.branches[branch_name]
            self._execution.complete_node_state(
                state_id=entry.state_id,
                status=NodeStateStatus.COMPLETED,
                output_data=entry.token.row_data.to_dict(),
                duration_ms=(now - entry.arrival_time) * 1000,
            )
            released.append(entry.token)

        del self._pending[key]
        self._mark_completed(key, _CLOSED_BY_RELEASE)

        popped = self._pop_released_group(released)
        return RowUnionOutcome(
            held=False,
            released_tokens=popped,
            consumed_tokens=popped,
            row_union_name=settings.name,
        )

    def _fail_pending(
        self,
        settings: RowUnionSettings,
        key: tuple[str, str],
        failure_reason: str,
        *,
        is_timeout: bool = False,
    ) -> RowUnionOutcome:
        """Fail every held token in a pending group and clean up (fail-closed)."""
        pending = self._pending[key]
        consumed_tokens = tuple(entry.token for entry in pending.branches.values())
        error_hash = compute_error_hash(failure_reason)
        now = self._clock.monotonic()

        error = RowUnionFailureReason(
            failure_reason=failure_reason,
            expected_branches=tuple(settings.branches),
            branches_arrived=tuple(pending.branches.keys()),
            timeout_ms=int(settings.timeout_seconds * 1000) if is_timeout and settings.timeout_seconds is not None else None,
        )
        for entry in pending.branches.values():
            self._execution.complete_node_state(
                state_id=entry.state_id,
                status=NodeStateStatus.FAILED,
                error=error,
                duration_ms=(now - entry.arrival_time) * 1000,
            )
            self._data_flow.record_token_outcome(
                ref=TokenRef(token_id=entry.token.token_id, run_id=self._run_id),
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.UNROUTED,
                error_hash=error_hash,
            )

        del self._pending[key]
        self._mark_completed(key, failure_reason)

        return RowUnionOutcome(
            held=False,
            failure_reason=failure_reason,
            consumed_tokens=consumed_tokens,
            row_union_name=settings.name,
            outcomes_recorded=True,
        )

    def _fail_late_arrival(
        self,
        token: TokenInfo,
        settings: RowUnionSettings,
        node_id: NodeID,
        step: int,
        *,
        closed_reason: str = _CLOSED_BY_RELEASE,
    ) -> RowUnionOutcome:
        """Fail-closed arm for a token arriving after its group closed.

        The recorded reason distinguishes a genuine straggler (its group
        already released) from a token whose group was killed before it ever
        arrived — timeout, EOF flush, or a lost sibling branch. Both fail
        closed; only the audit trail tells the operator which happened, so a
        non-release closure must carry the group's true closure reason.
        """
        failure_reason = "late_arrival_after_release" if closed_reason == _CLOSED_BY_RELEASE else closed_reason
        error_hash = compute_error_hash(failure_reason)
        state = self._execution.begin_node_state(
            token_id=token.token_id,
            node_id=node_id,
            run_id=self._run_id,
            step_index=step,
            input_data=token.row_data.to_dict(),
            attempt=token.resume_attempt_offset,
            resume_checkpoint_id=token.resume_checkpoint_id,
        )
        error = RowUnionFailureReason(
            failure_reason=failure_reason,
            expected_branches=tuple(settings.branches),
            branches_arrived=(),
        )
        self._execution.complete_node_state(
            state_id=state.state_id,
            status=NodeStateStatus.FAILED,
            error=error,
            duration_ms=0,
        )
        self._data_flow.record_token_outcome(
            ref=TokenRef(token_id=token.token_id, run_id=self._run_id),
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
            error_hash=error_hash,
        )
        return RowUnionOutcome(
            held=False,
            failure_reason=failure_reason,
            consumed_tokens=(token,),
            row_union_name=settings.name,
            outcomes_recorded=True,
            late_arrival=True,
        )

    # ------------------------------------------------------------------
    # Timeouts / flush / branch loss
    # ------------------------------------------------------------------

    def check_timeouts(self, row_union_name: str) -> list[RowUnionOutcome]:
        """Fail every pending group of this barrier that exceeded its timeout."""
        if row_union_name not in self._settings:
            raise OrchestrationInvariantError(f"row_union '{row_union_name}' not registered")
        settings = self._settings[row_union_name]
        if settings.timeout_seconds is None:
            return []
        now = self._clock.monotonic()
        timed_out = [
            key
            for key, pending in self._pending.items()
            if key[0] == row_union_name and (now - pending.first_arrival) > settings.timeout_seconds
        ]
        return [self._fail_pending(settings, key, "row_union_timeout", is_timeout=True) for key in timed_out]

    def flush_pending(self) -> list[RowUnionOutcome]:
        """End-of-source flush: fail every incomplete group closed (v1)."""
        outcomes: list[RowUnionOutcome] = []
        for key in list(self._pending.keys()):
            settings = self._settings[key[0]]
            outcomes.append(self._fail_pending(settings, key, "row_union_incomplete_at_flush"))
        return outcomes

    def has_recorded_branch_loss(self, row_union_name: str, row_id: str, branch_name: str) -> bool:
        """Recent-cache idempotency check for §E.5 branch-loss replay dedup."""
        return (row_union_name, row_id, branch_name) in self._recorded_losses

    def is_group_released(self, row_union_name: str, row_id: str) -> bool:
        """Whether this group's in-memory closure reason is a release.

        Leader fast path for the processor's post-release divert
        discrimination; followers and post-resume processes fall back to the
        durable status-COMPLETED node state read.
        """
        return self._completed_keys.get((row_union_name, row_id)) == _CLOSED_BY_RELEASE

    def notify_branch_lost(
        self,
        row_union_name: str,
        row_id: str,
        lost_branch: str,
        reason: str,
    ) -> RowUnionOutcome | None:
        """Notify that a forked branch will never arrive (error-routed).

        v1 has no partial release: any loss makes the group impossible, so
        arrived siblings fail immediately. When nothing has arrived yet the
        key is marked completed so later siblings fail closed via the
        late-arrival arm instead of waiting forever.

        A group that already closed is exempt entirely. In particular,
        released tokens keep their branch_name, so a terminal divert
        downstream of the union re-enters this path — that is not a
        pre-barrier loss, and recording it would poison the loss indexes for
        this key. Landscape point reads preserve this rule after cache
        eviction.
        """
        if row_union_name not in self._settings:
            raise OrchestrationInvariantError(f"row_union '{row_union_name}' not registered")
        key = (row_union_name, row_id)
        if key in self._completed_keys or self._check_landscape_for_completion(row_union_name, row_id):
            return None
        self._remember_branch_loss(row_union_name, row_id, lost_branch)
        if key not in self._pending:
            # Nothing held yet: mark the group dead so future arrivals
            # fail closed instead of holding forever.
            self._mark_completed(key, _CLOSED_BY_BRANCH_LOSS)
            slog.info(
                "row_union branch lost before any sibling arrived; group marked dead",
                row_union=row_union_name,
                row_id=row_id,
                lost_branch=lost_branch,
                reason=reason,
            )
            return None
        settings = self._settings[row_union_name]
        return self._fail_pending(settings, key, "row_union_branch_lost")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_landscape_for_completion(self, row_union_name: str, row_id: str) -> bool:
        """Landscape fallback for late-arrival detection (cache-miss path).

        A completed state alone does not prove a release: _fail_pending's
        FAILED closures carry completed_at too. The released-only point read
        distinguishes them so the recached closure reason stays truthful.
        """
        if row_union_name not in self._node_ids:
            return False
        node_id = self._node_ids[row_union_name]
        if not self._barrier_restore_reads.has_completed_row_for_node(run_id=self._run_id, node_id=str(node_id), row_id=row_id):
            return False
        key = (row_union_name, row_id)
        if self._barrier_restore_reads.has_released_row_for_node(run_id=self._run_id, node_id=str(node_id), row_id=row_id):
            closed_reason = _CLOSED_BY_RELEASE
        elif key in self._recorded_loss_groups or self._barrier_restore_reads.has_branch_loss_for_group(
            run_id=self._run_id,
            barrier_name=row_union_name,
            row_id=row_id,
        ):
            closed_reason = _CLOSED_BY_BRANCH_LOSS
        else:
            closed_reason = _CLOSED_BY_PRIOR_FAILURE
        self._mark_completed(key, closed_reason)
        return True

    def _mark_completed(self, key: tuple[str, str], reason: str) -> None:
        """Mark a group closed, with its cause, under bounded memory."""
        self._completed_keys[key] = reason
        while len(self._completed_keys) > self._max_completed_keys:
            self._completed_keys.popitem(last=False)

    def _remember_branch_loss(self, row_union_name: str, row_id: str, branch_name: str) -> None:
        """Cache recent loss identities for replay dedup and fast group checks."""
        loss_key = (row_union_name, row_id, branch_name)
        group_key = (row_union_name, row_id)
        self._recorded_losses[loss_key] = None
        self._recorded_losses.move_to_end(loss_key)
        self._recorded_loss_groups[group_key] = None
        self._recorded_loss_groups.move_to_end(group_key)
        while len(self._recorded_losses) > self._max_completed_keys:
            self._recorded_losses.popitem(last=False)
        while len(self._recorded_loss_groups) > self._max_completed_keys:
            self._recorded_loss_groups.popitem(last=False)
