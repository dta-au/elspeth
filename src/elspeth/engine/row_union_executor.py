"""RowUnionExecutor: releases fork-branch tokens as indivisible groups.

row_union is the correlated, same-row_id, N->N UNION ALL barrier
(elspeth-a5b86149d4 v1 contract). It holds fork-branch tokens per
(row_union_name, row_id) until EVERY declared branch has arrived, then
releases the ORIGINAL tokens as one group in declared branch order. It never
merges fields, deduplicates rows, fabricates rows, or synthesizes a wide
intermediate row — payloads pass through untouched and token identity
(token_id, row_id, branch_name, fork_group_id) is preserved.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from elspeth.contracts import TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import NodeStateStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError, RowUnionFailureReason
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.config import RowUnionSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.clock import DEFAULT_CLOCK, Clock

if TYPE_CHECKING:
    from elspeth.core.landscape.scheduler import BarrierRestoreReadModel

slog = structlog.get_logger(__name__)


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
        self._completed_keys: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._max_completed_keys = max_completed_keys
        # Recorded branch losses for §E.5 replay dedup:
        # (row_union_name, row_id, branch_name)
        self._recorded_losses: set[tuple[str, str, str]] = set()

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
            return self._fail_late_arrival(token, settings, node_id, step)

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
        self._mark_completed(key)

        return RowUnionOutcome(
            held=False,
            released_tokens=tuple(released),
            consumed_tokens=tuple(released),
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
        self._mark_completed(key)

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
    ) -> RowUnionOutcome:
        """Fail-closed arm for a token arriving after its group completed."""
        failure_reason = "late_arrival_after_release"
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
        """Idempotency check for §E.5 branch-loss replay dedup."""
        return (row_union_name, row_id, branch_name) in self._recorded_losses

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
        """
        if row_union_name not in self._settings:
            raise OrchestrationInvariantError(f"row_union '{row_union_name}' not registered")
        self._recorded_losses.add((row_union_name, row_id, lost_branch))

        key = (row_union_name, row_id)
        if key in self._completed_keys:
            return None
        if key not in self._pending:
            # Nothing held yet: mark the group dead so future arrivals
            # fail closed instead of holding forever.
            self._mark_completed(key)
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
        """Landscape fallback for late-arrival detection (cache-miss path)."""
        if row_union_name not in self._node_ids:
            return False
        node_id = self._node_ids[row_union_name]
        if self._barrier_restore_reads.has_completed_row_for_node(run_id=self._run_id, node_id=str(node_id), row_id=row_id):
            self._mark_completed((row_union_name, row_id))
            return True
        return False

    def _mark_completed(self, key: tuple[str, str]) -> None:
        """Mark a group completed with bounded memory (FIFO eviction)."""
        self._completed_keys[key] = None
        while len(self._completed_keys) > self._max_completed_keys:
            self._completed_keys.popitem(last=False)
