"""Journal restore boundary for barrier executors (F1 resume path).

The crash-resume hydration logic for both stateful barriers lives here, out
of the live executors: each ``*JournalRestorer`` validates journal BLOCKED
rows against audit-derived inputs (state ids, attempt offsets, batch
membership, checkpoint scalars) and returns a frozen, already-validated
state object. The executor's ``restore_from_journal`` stays a thin facade —
it builds the restorer, applies the returned state, and keeps only live-path
responsibilities (state replacement, late-arrival point lookup).

Restore-specific invariants (validation order, corruption messages, staleness
handling) therefore evolve in this module without touching runtime barrier
behavior, and vice versa.

Both restorers share one design shape:

* validate-before-anything: every journal/audit disagreement raises
  ``AuditIntegrityError`` before any state object is built;
* token payloads rehydrate through ``token_from_journal_item`` (the journal
  row is authoritative for payload and lineage);
* pending/trigger age derives from the absolute ``barrier_blocked_at`` stamp
  of the OLDEST row against the caller-supplied wall clock, clamped at 0
  against backward wall-clock steps.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from elspeth.contracts import TokenInfo
from elspeth.contracts.barrier_scalars import AggregationNodeScalars, CoalescePendingScalars
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.types import NodeID
from elspeth.core.config import CoalesceSettings, CollectorSettings
from elspeth.core.landscape.scheduler_repository import token_from_journal_item

if TYPE_CHECKING:
    from elspeth.core.landscape.scheduler import BarrierRestoreReadModel
    from elspeth.engine.clock import Clock

slog = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Coalesce restore state (frozen — the executor applies, never re-validates)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoredCoalesceBranch:
    """One rehydrated arrived branch within a restored pending coalesce."""

    branch_name: str
    token: TokenInfo
    arrival_time: float  # Monotonic timestamp (blocked-at offsets preserved)
    state_id: str  # Landscape node_state ID for the PENDING hold


@dataclass(frozen=True, slots=True)
class RestoredPendingCoalesce:
    """One validated pending coalesce key ready to apply to the executor.

    ``branches`` preserves journal grouping order (insertion order of the
    BLOCKED rows) so the executor's rebuilt branch dict iterates identically
    to the pre-extraction restore.
    """

    key: tuple[str, str]  # (coalesce_name, row_id)
    branches: tuple[RestoredCoalesceBranch, ...]
    first_arrival: float  # Monotonic anchor of the OLDEST branch
    lost_branches: Mapping[str, str]

    def __post_init__(self) -> None:
        freeze_fields(self, "lost_branches")


@dataclass(frozen=True, slots=True)
class RestoredCoalesceState:
    """Whole-executor restored coalesce state (single-shot apply)."""

    pending: tuple[RestoredPendingCoalesce, ...]
    completed_keys: tuple[tuple[str, str], ...]
    token_count: int


class CoalesceJournalRestorer:
    """Validates and hydrates coalesce restore inputs into a frozen state object.

    Owns the restore-side half of ``CoalesceExecutor.restore_from_journal``:
    journal validation, token rehydration, scalar-only (zero-arrival
    loss-record) handling, and completed-key reconstruction from the
    Landscape. The executor keeps state replacement and the late-arrival
    Landscape point lookup.
    """

    def __init__(
        self,
        *,
        settings: Mapping[str, CoalesceSettings],
        node_ids: Mapping[str, NodeID],
        barrier_restore_reads: "BarrierRestoreReadModel",
        run_id: str,
        clock: "Clock",
    ) -> None:
        """Initialize restorer.

        Args:
            settings: Registered coalesce configurations, keyed by name.
            node_ids: Registered coalesce node ids, keyed by coalesce name —
                used to reconstruct completed keys from the Landscape.
            barrier_restore_reads: Restore read model for Landscape audit reads.
            run_id: Run identifier for error context and audit queries.
            clock: Clock supplying the monotonic scale restored arrival
                anchors are expressed on.
        """
        self._settings = settings
        self._node_ids = node_ids
        self._barrier_restore_reads = barrier_restore_reads
        self._run_id = run_id
        self._clock = clock

    def restore(
        self,
        *,
        items: Sequence[TokenWorkItem],
        scalars: Mapping[tuple[str, str], CoalescePendingScalars],
        state_ids: Mapping[str, str],
        attempt_offsets: Mapping[str, int],
        resume_checkpoint_id: str,
        now: datetime,
    ) -> RestoredCoalesceState:
        """Validate journal rows and build the restored coalesce state.

        Argument semantics are documented on the facade
        (``CoalesceExecutor.restore_from_journal``), which forwards verbatim.

        Raises:
            AuditIntegrityError: On any journal/audit disagreement — NULL
                barrier_blocked_at, missing branch_name or coalesce_name,
                unknown coalesce, duplicate journal rows, duplicate branch
                claims, missing attempt offset, missing state_id.
        """
        # Validate and group ALL items before building any state — if
        # validation fails, the executor's in-memory state must remain intact
        # for error recovery (same discipline as the old blob restore).
        grouped: dict[tuple[str, str], dict[str, TokenWorkItem]] = {}
        blocked_at_by_token: dict[str, datetime] = {}
        for item in items:
            if not item.coalesce_name:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} (run {self._run_id!r}, "
                    f"resume checkpoint {resume_checkpoint_id!r}) has no coalesce_name — "
                    "coalesce barrier rows always carry the coalesce cursor; journal corruption."
                )
            if item.coalesce_name not in self._settings:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} (run {self._run_id!r}, "
                    f"resume checkpoint {resume_checkpoint_id!r}) references unknown coalesce "
                    f"'{item.coalesce_name}'. Configured coalesces: {sorted(self._settings)}"
                )
            if item.barrier_blocked_at is None:
                # Every post-epoch-20 BLOCKED row is stamped by mark_blocked.
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at coalesce "
                    f"{item.coalesce_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has NULL barrier_blocked_at — journal "
                    "corruption (every BLOCKED row is stamped at mark_blocked time)."
                )
            innermost = item.lineage_path[-1] if item.lineage_path else None
            if innermost is None or innermost.kind is not FrameKind.FORK:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at coalesce "
                    f"{item.coalesce_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has no innermost FORK frame — only forked branch "
                    "tokens block at a coalesce barrier; journal corruption."
                )
            branch_name = innermost.member_key
            if branch_name not in self._settings[item.coalesce_name].branches:
                # The live accept() path rejects unknown branches; restore must
                # apply the same allowlist (elspeth-a840cb774a) — a rogue branch
                # inflates quorum/best_effort arrival counts while contributing
                # no merge data.
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at coalesce "
                    f"{item.coalesce_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) claims branch '{branch_name}' which is "
                    f"not in the configured branches {sorted(self._settings[item.coalesce_name].branches)} — "
                    "journal corruption."
                )
            if item.token_id in blocked_at_by_token:
                raise AuditIntegrityError(
                    f"Duplicate BLOCKED journal rows for token {item.token_id!r} at "
                    f"coalesce {item.coalesce_name!r} (run {self._run_id!r}, resume "
                    f"checkpoint {resume_checkpoint_id!r}) — journal corruption."
                )
            if item.token_id not in attempt_offsets:
                raise AuditIntegrityError(
                    f"No entry in attempt_offsets for journal token {item.token_id!r} at "
                    f"coalesce {item.coalesce_name!r} (run {self._run_id!r}, resume "
                    f"checkpoint {resume_checkpoint_id!r}) — audit-derived offsets must "
                    "cover every BLOCKED journal row."
                )
            if item.token_id not in state_ids:
                # The PENDING node_state hold is written at accept() time, before
                # the journal row blocks; a BLOCKED row with no hold means the
                # journal and the audit trail disagree — corruption, not a default.
                raise AuditIntegrityError(
                    f"No entry in state_ids for journal token {item.token_id!r} at "
                    f"coalesce {item.coalesce_name!r} (run {self._run_id!r}, resume "
                    f"checkpoint {resume_checkpoint_id!r}) — every BLOCKED coalesce row "
                    "holds a PENDING node_state in the audit trail; a missing hold is "
                    "an audit inconsistency."
                )

            key = (item.coalesce_name, item.row_id)
            if key not in grouped:
                grouped[key] = {}
            branch_items = grouped[key]
            if branch_name in branch_items:
                raise AuditIntegrityError(
                    f"BLOCKED journal rows for tokens "
                    f"{branch_items[branch_name].token_id!r} and {item.token_id!r} "
                    f"both claim branch '{branch_name}' at coalesce "
                    f"{item.coalesce_name!r} for row {item.row_id!r} (run {self._run_id!r}, "
                    f"resume checkpoint {resume_checkpoint_id!r}) — accept() crashes on a "
                    "duplicate arrival, so this is journal corruption."
                )
            branch_items[branch_name] = item
            blocked_at_by_token[item.token_id] = item.barrier_blocked_at

        monotonic_now = self._clock.monotonic()
        restored_pending: dict[tuple[str, str], RestoredPendingCoalesce] = {}
        for key, branch_items in grouped.items():
            min_blocked_at = min(blocked_at_by_token[it.token_id] for it in branch_items.values())
            # Pending age derives from the absolute blocked-at stamp of the
            # OLDEST branch (first arrival of this pending key), clamped at 0:
            # a wall-clock backward step must not put first_arrival in the
            # monotonic future.
            first_arrival = monotonic_now - max(0.0, (now - min_blocked_at).total_seconds())
            branches: list[RestoredCoalesceBranch] = []
            for branch_name, branch_item in branch_items.items():
                token = token_from_journal_item(
                    branch_item,
                    attempt_offset=attempt_offsets[branch_item.token_id],
                    resume_checkpoint_id=resume_checkpoint_id,
                )
                branches.append(
                    RestoredCoalesceBranch(
                        branch_name=branch_name,
                        token=token,
                        arrival_time=first_arrival + (blocked_at_by_token[branch_item.token_id] - min_blocked_at).total_seconds(),
                        state_id=state_ids[branch_item.token_id],
                    )
                )

            key_scalars = scalars[key] if key in scalars else None
            lost_branches = dict(key_scalars.lost_branches) if key_scalars is not None else {}
            allowed_branches = self._settings[key[0]].branches
            unknown_lost = set(lost_branches) - set(allowed_branches)
            if unknown_lost:
                raise AuditIntegrityError(
                    f"Checkpoint lost_branches for coalesce {key[0]!r} row {key[1]!r} "
                    f"(run {self._run_id!r}, resume checkpoint {resume_checkpoint_id!r}) "
                    f"name branches {sorted(unknown_lost)} outside the configured branches "
                    f"{sorted(allowed_branches)} — checkpoint corruption."
                )
            arrived_and_lost = {b.branch_name for b in branches} & set(lost_branches)
            if arrived_and_lost:
                # Mirrors the live notify_branch_lost invariant: a branch
                # cannot both arrive and be lost for the same pending key.
                raise AuditIntegrityError(
                    f"Branches {sorted(arrived_and_lost)} at coalesce {key[0]!r} row {key[1]!r} "
                    f"(run {self._run_id!r}, resume checkpoint {resume_checkpoint_id!r}) "
                    "are recorded as both arrived and lost — journal/checkpoint corruption."
                )
            restored_pending[key] = RestoredPendingCoalesce(
                key=key,
                branches=tuple(branches),
                first_arrival=first_arrival,
                lost_branches=lost_branches,
            )

        # Reconstruct completed keys from Landscape (source of truth). The
        # executor seeds them into its bounded FIFO cache at apply time.
        # Queried BEFORE the executor mutates anything: a Landscape error
        # mid-restore must not leave the executor cleared-but-unrestored.
        completed_keys = self._reconstruct_completed_keys_from_landscape()
        completed_key_set = set(completed_keys)

        # Scalar-only entries have no arrived branch payloads in the journal.
        # If the Landscape says the key completed, the scalar is an older
        # checkpoint image and must be dropped. Otherwise, a non-empty
        # lost_branches scalar is the durable image of a zero-arrival pending
        # key: restore it so a later surviving branch accounts against the
        # recorded loss instead of forming a fresh, loss-free pending key.
        for scalar_key in scalars.keys() - grouped.keys():
            coalesce_name, row_id = scalar_key
            key_scalars = scalars[scalar_key]
            lost_branches = dict(key_scalars.lost_branches)
            if coalesce_name in self._settings and lost_branches and scalar_key not in completed_key_set:
                unknown_lost = set(lost_branches) - set(self._settings[coalesce_name].branches)
                if unknown_lost:
                    # An unknown BRANCH inside a configured, non-completed
                    # coalesce's scalars is corruption, not staleness — only
                    # unknown-coalesce / completed / empty keys drop-and-log.
                    raise AuditIntegrityError(
                        f"Checkpoint lost_branches for coalesce {coalesce_name!r} row {row_id!r} "
                        f"(run {self._run_id!r}, resume checkpoint {resume_checkpoint_id!r}) "
                        f"name branches {sorted(unknown_lost)} outside the configured branches "
                        f"{sorted(self._settings[coalesce_name].branches)} — checkpoint corruption."
                    )
                restored_pending[scalar_key] = RestoredPendingCoalesce(
                    key=scalar_key,
                    branches=(),
                    first_arrival=monotonic_now,
                    lost_branches=lost_branches,
                )
                slog.info(
                    "coalesce_journal_restored_loss_only_scalars",
                    coalesce_name=coalesce_name,
                    row_id=row_id,
                    run_id=self._run_id,
                    resume_checkpoint_id=resume_checkpoint_id,
                    lost_branches=lost_branches,
                )
                continue
            slog.info(
                "coalesce_journal_restore_dropped_stale_scalars",
                coalesce_name=coalesce_name,
                row_id=row_id,
                run_id=self._run_id,
                resume_checkpoint_id=resume_checkpoint_id,
                lost_branches=lost_branches,
            )

        return RestoredCoalesceState(
            pending=tuple(restored_pending.values()),
            completed_keys=tuple(completed_keys),
            token_count=len(blocked_at_by_token),
        )

    def _reconstruct_completed_keys_from_landscape(self) -> list[tuple[str, str]]:
        """Read completed coalesce keys from the Landscape audit trail.

        Queries node_states for completed entries at coalesce node IDs, joined
        with tokens to get row_ids. Maps node_id → coalesce_name via the
        reverse of the registered node_ids.

        This is the restore seeding path: the Landscape records all completed
        coalesces, but the executor keeps only a bounded FIFO performance
        cache. Late arrivals for evicted keys are rediscovered through an exact
        Landscape point lookup (which stays on the executor).
        """
        if not self._node_ids:
            return []

        # Build reverse map: node_id → coalesce_name
        node_id_to_name: dict[str, str] = {str(nid): name for name, nid in self._node_ids.items()}

        completed_pairs = self._barrier_restore_reads.get_completed_row_ids_for_nodes(
            run_id=self._run_id,
            node_ids=frozenset(node_id_to_name.keys()),
        )

        return [(node_id_to_name[node_id_str], row_id) for node_id_str, row_id in completed_pairs if node_id_str in node_id_to_name]


# ---------------------------------------------------------------------------
# Collector restore state (frozen — the executor applies, never re-validates)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoredCollectorMember:
    """One rehydrated arrived member within a restored pending collector group."""

    member_key: str
    token: TokenInfo
    state_id: str  # Landscape node_state ID for the PENDING arrival hold (M-4-checked OPEN)


@dataclass(frozen=True, slots=True)
class RestoredPendingCollectorGroup:
    """One validated pending collector group ready to apply to the executor.

    Carries only MEMBERS — roster and ordinal re-derivation stay on the
    executor side (task-6-7-review-prep.md I-6 / AMENDMENT note 2):
    ``_open_group`` is the durable-authority read (group_records /
    token_lineage_frames / token_parents), and re-deriving it here would
    create a second, unauthoritative copy the executor's own fresh-arrival
    path does not use.
    """

    key: tuple[str, str]  # (collector_name, group_id)
    members: tuple[RestoredCollectorMember, ...]


@dataclass(frozen=True, slots=True)
class RestoredCollectorState:
    """Whole-executor restored collector state (single-shot apply, Task 7)."""

    pending_groups: tuple[RestoredPendingCollectorGroup, ...]
    # (collector_name, group_id) -> settled member token_ids at closure
    # (META-20b): the durable reconstruction of CollectorExecutor's
    # in-memory _completed_keys settled-token memory (I-3, fix round 2),
    # covering BOTH the takeover gap and the max_completed_keys FIFO-
    # eviction gap (fix-round-3-review-prep.md's FIX 2 / I-2) from the SAME
    # read — both produce the identical empty-set symptom, so one durable
    # reconstruction is the principled fix for both rather than a second,
    # partial in-memory structure.
    completed_groups: tuple[tuple[tuple[str, str], frozenset[str]], ...]
    token_count: int


class CollectorJournalRestorer:
    """Validates and hydrates collector restore inputs into a frozen state object.

    Owns the restore-side half of ``CollectorExecutor.restore_from_journal``:
    journal validation, completed-group discovery, M-4's open-hold
    cross-check, and token rehydration. The executor keeps roster/ordinal
    re-derivation (``_open_group``, I-6) and state replacement — this
    restorer never calls it (that method is executor-private), so it returns
    MEMBERS only, never a prefabricated roster.

    **Caller obligation (I-7): call this only AFTER WS3's group-loss intake
    replay, or run this restore BEFORE that replay** — whichever order the
    caller adopts, it must hold consistently, because ``notify_member_lost``
    calling ``_open_group`` for a group this restore has not yet rebuilt
    would make ``CollectorExecutor.restore_from_journal``'s own
    ``if self._pending: raise`` empty-executor guard fire on a legitimately
    non-empty executor. This restorer does not itself restore losses (they
    replay through WS3's intake, spec §6.2 full-table-on-takeover) — mirrors
    ``CoalesceJournalRestorer``'s equivalent instruction.
    """

    def __init__(
        self,
        *,
        settings: Mapping[str, CollectorSettings],
        node_ids: Mapping[str, NodeID],
        barrier_restore_reads: "BarrierRestoreReadModel",
        run_id: str,
    ) -> None:
        """Initialize restorer.

        Args:
            settings: Registered collector configurations, keyed by name.
            node_ids: Registered collector node ids, keyed by collector
                name — used both to reconstruct completed groups from the
                Landscape and to scope the M-4 open-hold cross-check.
            barrier_restore_reads: Restore read model for Landscape audit reads.
            run_id: Run identifier for error context and audit queries.
        """
        self._settings = settings
        self._node_ids = node_ids
        self._barrier_restore_reads = barrier_restore_reads
        self._run_id = run_id

    def restore(
        self,
        *,
        items: Sequence[TokenWorkItem],
        state_ids: Mapping[str, str],
        attempt_offsets: Mapping[str, int],
        resume_checkpoint_id: str,
    ) -> RestoredCollectorState:
        """Validate journal rows and build the restored collector state.

        Argument semantics are documented on the facade
        (``CollectorExecutor.restore_from_journal``), which forwards verbatim.

        Raises:
            AuditIntegrityError: On any journal/audit disagreement — NULL
                barrier_blocked_at, missing collector_name, unknown
                collector, no innermost EXPAND frame, duplicate journal
                rows, duplicate member claims within a group, missing
                attempt offset, missing or non-OPEN state_id — for any
                journal row NOT already explained by a durable group
                closure (see the completed-group partition below).
        """
        # Step 1: minimal structural validation for EVERY item — needed to
        # resolve which group each item belongs to before step 2 can even
        # ask "is this group already completed".
        group_of: dict[str, tuple[str, str, str]] = {}  # token_id -> (collector_name, group_id, member_key)
        for item in items:
            if not item.collector_name:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} (run {self._run_id!r}, "
                    f"resume checkpoint {resume_checkpoint_id!r}) has no collector_name — "
                    "collector barrier rows always carry the collector cursor; journal corruption."
                )
            if item.collector_name not in self._settings:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} (run {self._run_id!r}, "
                    f"resume checkpoint {resume_checkpoint_id!r}) references unknown collector "
                    f"'{item.collector_name}'. Configured collectors: {sorted(self._settings)}"
                )
            if item.barrier_blocked_at is None:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at collector "
                    f"{item.collector_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has NULL barrier_blocked_at — journal "
                    "corruption (every BLOCKED row is stamped at mark_blocked time)."
                )
            innermost = item.lineage_path[-1] if item.lineage_path else None
            if innermost is None or innermost.kind is not FrameKind.EXPAND:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at collector "
                    f"{item.collector_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has no innermost EXPAND frame — only expansion "
                    "members block at a collector barrier; journal corruption."
                )
            if item.token_id in group_of:
                raise AuditIntegrityError(
                    f"Duplicate BLOCKED journal rows for token {item.token_id!r} at collector "
                    f"{item.collector_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) — journal corruption."
                )
            group_of[item.token_id] = (item.collector_name, innermost.group_id, innermost.member_key)

        # META-22: cross-check the durable "which node(s) has this group
        # completed at" derivation (resolve_group_collector_node) against
        # each distinct group's config-mapped node (self._node_ids[
        # collector_name], from the item's OWN declared collector_name)
        # BEFORE doing anything else with group membership. A group_id
        # claimed by the WRONG collector is a genuine corruption signal —
        # letting it through silently could misclassify a post-closure
        # residual (step 2 below matches on the ITEM's declared
        # collector_name) or route a live arrival into the wrong
        # collector's roster rebuild entirely.
        #
        # MEMBERSHIP, not equality: resolve_group_collector_node returns a
        # SET, not a single value — a NESTED outer group's frame is carried
        # by every descendant token at any depth (I-6's documented
        # semantic), so an outer group's durable set legitimately contains
        # more than one node once inner scopes have completed too. The
        # config node only needs to be ONE OF the durable nodes; requiring
        # equality against the whole set would raise on healthy nested
        # pipelines. Vacuous (no assertion possible) for a group that has
        # not completed anywhere yet — the ordinary, expected case for
        # every live restore.
        durable_nodes_by_group: dict[str, frozenset[str]] = {}
        for collector_name, group_id, _member_key in group_of.values():
            if group_id not in durable_nodes_by_group:
                durable_nodes_by_group[group_id] = self._barrier_restore_reads.resolve_group_collector_node(
                    run_id=self._run_id, group_id=group_id
                )
            durable_node_ids = durable_nodes_by_group[group_id]
            if not durable_node_ids:
                continue
            config_node_id = str(self._node_ids[collector_name])
            if config_node_id not in durable_node_ids:
                raise AuditIntegrityError(
                    f"Group {group_id!r} durably completed at node(s) {sorted(durable_node_ids)!r}, "
                    f"but a journal row declares collector {collector_name!r} (config node "
                    f"{config_node_id!r}, not among them) for it (run {self._run_id!r}, resume "
                    f"checkpoint {resume_checkpoint_id!r}) — durable and config derivations of "
                    '"the collector node for this group" disagree; refusing rather than silently '
                    "trusting either side."
                )

        # Step 2: reconstruct completed groups from the Landscape FIRST
        # (source of truth) — BEFORE any coverage validation. A group this
        # process (or a prior one) already closed has COMPLETED member
        # holds, not OPEN ones, so its journal residual legitimately has NO
        # entry in the caller-supplied state_ids (the caller derives that
        # mapping from OPEN holds only). Validating coverage before this
        # partition would reject a perfectly healthy post-flush resume with
        # AuditIntegrityError — and if it did not raise, would instead
        # rebuild and RE-FLUSH an already-closed group, exactly the
        # double-flush META-14.1's "crash post-flush -> takeover -> NO
        # second plugin call" guarantee forbids.
        completed_groups = self._reconstruct_completed_groups_from_landscape()
        completed_key_set = {key for key, _settled in completed_groups}

        live_items: list[TokenWorkItem] = []
        for item in items:
            collector_name, group_id, _member_key = group_of[item.token_id]
            if (collector_name, group_id) in completed_key_set:
                slog.info(
                    "collector_journal_restore_dropped_post_closure_residual",
                    collector_name=collector_name,
                    group_id=group_id,
                    token_id=item.token_id,
                    run_id=self._run_id,
                    resume_checkpoint_id=resume_checkpoint_id,
                )
                continue
            live_items.append(item)

        # Step 3: coverage + duplicate-member-claim validation, LIVE items only.
        grouped: dict[tuple[str, str], dict[str, TokenWorkItem]] = {}
        for item in live_items:
            collector_name, group_id, member_key = group_of[item.token_id]
            if item.token_id not in attempt_offsets:
                raise AuditIntegrityError(
                    f"No entry in attempt_offsets for journal token {item.token_id!r} at "
                    f"collector {collector_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) — audit-derived offsets must cover every live "
                    "BLOCKED journal row."
                )
            if item.token_id not in state_ids:
                raise AuditIntegrityError(
                    f"No entry in state_ids for journal token {item.token_id!r} at collector "
                    f"{collector_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) — every live BLOCKED collector row holds a "
                    "PENDING node_state in the audit trail; a missing hold is an audit "
                    "inconsistency (distinct from a post-closure residual, already excluded above)."
                )
            key = (collector_name, group_id)
            if key not in grouped:
                grouped[key] = {}
            branch_items = grouped[key]
            if member_key in branch_items:
                raise AuditIntegrityError(
                    f"BLOCKED journal rows for tokens {branch_items[member_key].token_id!r} and "
                    f"{item.token_id!r} both claim member {member_key!r} of group {group_id!r} "
                    f"at collector {collector_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) — accept() crashes on a duplicate arrival, so "
                    "this is journal corruption."
                )
            branch_items[member_key] = item

        # Step 4 (M-4): every live item's supplied state_id must name an
        # OPEN hold, not merely be PRESENT in the caller's mapping — a STALE
        # or already-completed state_id restored into a _MemberEntry would
        # be double-completed at the group's eventual flush. Batched once
        # across every live token/node pair rather than per item.
        live_node_ids = {str(self._node_ids[group_of[item.token_id][0]]) for item in live_items}
        open_ids: dict[str, str] = {}
        if live_items:
            open_ids = self._barrier_restore_reads.get_open_node_state_ids(
                self._run_id,
                node_ids=list(live_node_ids),
                token_ids=[item.token_id for item in live_items],
            )
        for item in live_items:
            collector_name, group_id, member_key = group_of[item.token_id]
            supplied = state_ids[item.token_id]
            if open_ids.get(item.token_id) != supplied:
                raise AuditIntegrityError(
                    f"state_ids entry {supplied!r} for token {item.token_id!r} (member "
                    f"{member_key!r} of group {group_id!r} at collector {collector_name!r}, "
                    f"run {self._run_id!r}, resume checkpoint {resume_checkpoint_id!r}) does "
                    "not name an OPEN node_state at this collector node — a stale or already-"
                    "completed hold would be double-completed at flush."
                )

        pending_groups: list[RestoredPendingCollectorGroup] = []
        for key, branch_items in grouped.items():
            members: list[RestoredCollectorMember] = []
            for member_key, item in branch_items.items():
                token = token_from_journal_item(
                    item,
                    attempt_offset=attempt_offsets[item.token_id],
                    resume_checkpoint_id=resume_checkpoint_id,
                )
                members.append(RestoredCollectorMember(member_key=member_key, token=token, state_id=state_ids[item.token_id]))
            pending_groups.append(RestoredPendingCollectorGroup(key=key, members=tuple(members)))

        return RestoredCollectorState(
            pending_groups=tuple(pending_groups),
            completed_groups=tuple(completed_groups),
            token_count=len(group_of),
        )

    def _reconstruct_completed_groups_from_landscape(self) -> list[tuple[tuple[str, str], frozenset[str]]]:
        """Read completed collector groups AND their settled member token_ids.

        Landscape-wide (not scoped to ``items``): a closed group carries no
        BLOCKED journal residual at all in the ordinary case, so the ONLY way
        to discover it is a scan over every registered collector node's
        completed groups — mirroring ``CoalesceJournalRestorer``'s own
        ``_reconstruct_completed_keys_from_landscape``, extended (META-20b)
        to also fetch each key's settled token_ids, since the collector
        (unlike coalesce) needs that set for the post-closure CAS-fenced
        redelivery skip (I-3, fix round 2).

        NOT routed through ``resolve_group_collector_node`` (META-22's
        family anchor) despite the family framing: that method returns a
        SET (nesting means an outer group_id can legitimately show
        completions at more than one node), and deduplicating this method's
        per-(node, group) pairs down to "one entry per group_id" to fit a
        single-value shape would risk DROPPING a genuine
        ``(collector_name, group_id)`` completion in favour of a spurious
        one from an unrelated inner collector whose descendant merely
        carries the outer group's ancestral frame. Every DISTINCT
        ``(node_id, group_id)`` pair this scoped query returns is kept and
        processed independently instead — each is already a concrete,
        unambiguous claim ("group_id shows completed AT node_id"), and
        ``get_completed_group_ids_for_nodes``'s own ``node_ids`` scoping
        (every REGISTERED collector node) already excludes non-collector
        matches. ``resolve_group_collector_node`` is for the ITEM-LEVEL
        cross-check above instead, where membership (not equality or
        deduplication) is exactly the right shape.
        """
        if not self._node_ids:
            return []
        node_id_to_name: dict[str, str] = {str(nid): name for name, nid in self._node_ids.items()}
        completed_pairs = self._barrier_restore_reads.get_completed_group_ids_for_nodes(
            run_id=self._run_id,
            node_ids=frozenset(node_id_to_name.keys()),
        )
        result: list[tuple[tuple[str, str], frozenset[str]]] = []
        for node_id_str, group_id in completed_pairs:
            if node_id_str not in node_id_to_name:
                continue
            collector_name = node_id_to_name[node_id_str]
            settled = self._barrier_restore_reads.get_settled_member_token_ids(run_id=self._run_id, node_id=node_id_str, group_id=group_id)
            result.append(((collector_name, group_id), settled))
        return result


# ---------------------------------------------------------------------------
# Aggregation restore state (frozen — the executor applies, never re-validates)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoredTriggerLatch:
    """Validated trigger-evaluator restore arguments for an in-progress batch.

    Present only when the node has buffered journal rows; a counter-only
    node restores with no latch (the executor resets its trigger instead —
    see ``AggregationJournalRestorer.restore`` for why stale latches are
    dropped rather than replanted).
    """

    batch_count: int
    elapsed_age_seconds: float
    count_fire_offset: float | None
    condition_fire_offset: float | None


@dataclass(frozen=True, slots=True)
class RestoredAggregationState:
    """One node's validated aggregation restore state, ready to apply.

    ``tokens`` is in batch_members.ordinal order (the authoritative accept
    order); buffers derive from it at apply time.
    """

    node_id: NodeID
    tokens: tuple[TokenInfo, ...]
    batch_id: str | None
    accepted_count_total: int
    completed_flush_count: int
    trigger_latch: RestoredTriggerLatch | None

    @property
    def elapsed_age_seconds(self) -> float:
        """Restored batch age for observability (0.0 for a counter-only node)."""
        return self.trigger_latch.elapsed_age_seconds if self.trigger_latch is not None else 0.0


class AggregationJournalRestorer:
    """Validates and hydrates one aggregation node's restore inputs.

    Owns the restore-side half of ``AggregationExecutor.restore_from_journal``:
    journal validation, journal-vs-batch_members reconciliation, batch/counter
    sanity checks, token rehydration in member order, and the trigger-latch
    staleness decision. The executor keeps applying the returned state to its
    node buffers and trigger evaluator.
    """

    def __init__(self, *, run_id: str) -> None:
        """Initialize restorer.

        Args:
            run_id: Run identifier for error context.
        """
        self._run_id = run_id

    def restore(
        self,
        *,
        node_id: NodeID,
        items: Sequence[TokenWorkItem],
        member_order: Sequence[str],
        batch_id: str | None,
        accepted_count_total: int,
        completed_flush_count: int,
        scalars: AggregationNodeScalars,
        attempt_offsets: Mapping[str, int],
        resume_checkpoint_id: str,
        now: datetime,
    ) -> RestoredAggregationState:
        """Validate journal rows and build one node's restored state.

        Argument semantics are documented on the facade
        (``AggregationExecutor.restore_from_journal``), which forwards
        verbatim after resolving the node.

        Raises:
            AuditIntegrityError: On any journal/audit disagreement — NULL
                barrier_blocked_at, duplicate journal rows, membership
                mismatch, duplicate member_order entries, missing attempt
                offset, batch_id/items inconsistency, impossible counters.
        """
        tokens_by_id: dict[str, TokenInfo] = {}
        oldest_blocked_at: datetime | None = None
        for item in items:
            if item.barrier_blocked_at is None:
                # Every post-epoch-20 BLOCKED row is stamped by mark_blocked.
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at aggregation node "
                    f"{node_id!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has NULL barrier_blocked_at — journal "
                    "corruption (every BLOCKED row is stamped at mark_blocked time)."
                )
            if item.token_id in tokens_by_id:
                raise AuditIntegrityError(
                    f"Duplicate BLOCKED journal rows for token {item.token_id!r} at "
                    f"aggregation node {node_id!r} (run {self._run_id!r}, resume "
                    f"checkpoint {resume_checkpoint_id!r}) — journal corruption."
                )
            try:
                attempt_offset = attempt_offsets[item.token_id]
            except KeyError:
                raise AuditIntegrityError(
                    f"No entry in attempt_offsets for journal token {item.token_id!r} at "
                    f"aggregation node {node_id!r} (run {self._run_id!r}, resume "
                    f"checkpoint {resume_checkpoint_id!r}) — audit-derived offsets must "
                    "cover every BLOCKED journal row."
                ) from None

            tokens_by_id[item.token_id] = token_from_journal_item(
                item,
                attempt_offset=attempt_offset,
                resume_checkpoint_id=resume_checkpoint_id,
            )
            if oldest_blocked_at is None or item.barrier_blocked_at < oldest_blocked_at:
                oldest_blocked_at = item.barrier_blocked_at

        self._reconcile_journal_batch_members(
            node_id=node_id,
            journal_token_ids=tokens_by_id.keys(),
            member_order=member_order,
        )

        # batch_id/items must agree: buffered journal rows imply an in-progress
        # batch; a batch_id with zero BLOCKED rows means batch membership
        # advanced past the journal (or vice versa) — corruption either way.
        if items and batch_id is None:
            raise AuditIntegrityError(
                f"Aggregation node {node_id!r} (run {self._run_id!r}, resume checkpoint "
                f"{resume_checkpoint_id!r}) has {len(items)} BLOCKED journal rows but no "
                "batch_id — buffered tokens always belong to an in-progress batch."
            )
        if not items and batch_id is not None:
            raise AuditIntegrityError(
                f"Aggregation node {node_id!r} (run {self._run_id!r}, resume checkpoint "
                f"{resume_checkpoint_id!r}) has batch_id {batch_id!r} but no BLOCKED "
                "journal rows — an in-progress batch must have blocked members."
            )

        # Counter sanity: the cumulative accept counter covers every currently
        # buffered row, so accepted_count_total < len(items) (or any negative
        # counter) is impossible audit state. Restoring it would silently emit
        # row_start <= 0 in the next flush's pagination metadata.
        if completed_flush_count < 0 or accepted_count_total < len(items):
            raise AuditIntegrityError(
                f"Aggregation node {node_id!r} (run {self._run_id!r}, resume checkpoint "
                f"{resume_checkpoint_id!r}): audit-derived counters are impossible "
                f"(accepted_count_total={accepted_count_total}, "
                f"completed_flush_count={completed_flush_count}, buffered={len(items)}). "
                "accepted_count_total must cover every buffered row and counters must "
                "be non-negative."
            )

        ordered_tokens = tuple(tokens_by_id[token_id] for token_id in member_order)

        # Trigger age derives from the absolute blocked-at stamp of the OLDEST
        # buffered row (first accept of the in-progress batch), clamped at 0
        # against clock skew.
        trigger_latch: RestoredTriggerLatch | None
        if oldest_blocked_at is not None:
            trigger_latch = RestoredTriggerLatch(
                batch_count=len(ordered_tokens),
                elapsed_age_seconds=max(0.0, (now - oldest_blocked_at).total_seconds()),
                count_fire_offset=scalars.count_fire_offset,
                condition_fire_offset=scalars.condition_fire_offset,
            )
        else:
            # Counter-only node: no in-progress batch, and trigger latches are
            # batch-scoped — so any non-None scalars are STALE (the checkpoint
            # predates the journal: crash after a flush terminalized the
            # BLOCKED rows but before the next checkpoint — a legitimate
            # window under D3's staleness model, so rejecting would refuse
            # valid resumes). Drop them (logged) and have the executor leave
            # the trigger fully unlatched via reset(): restoring a latch here
            # would plant a phantom first-accept anchor at restore time that
            # survives into the NEXT genuine batch (record_accept min-rewinds
            # first_accept_time but never clears it, so a phantom anchor
            # lingers whenever the genuine arrivals come later) → wrong
            # timeout age and, with latched offsets, a pre-fired
            # count/condition latch.
            trigger_latch = None
            if scalars.count_fire_offset is not None or scalars.condition_fire_offset is not None:
                slog.info(
                    "aggregation_journal_restore_dropped_stale_scalars",
                    node_id=str(node_id),
                    run_id=self._run_id,
                    resume_checkpoint_id=resume_checkpoint_id,
                    count_fire_offset=scalars.count_fire_offset,
                    condition_fire_offset=scalars.condition_fire_offset,
                )

        return RestoredAggregationState(
            node_id=node_id,
            tokens=ordered_tokens,
            batch_id=batch_id,
            accepted_count_total=accepted_count_total,
            completed_flush_count=completed_flush_count,
            trigger_latch=trigger_latch,
        )

    def _reconcile_journal_batch_members(
        self,
        *,
        node_id: NodeID,
        journal_token_ids: Iterable[str],
        member_order: Sequence[str],
    ) -> None:
        """Ensure journal BLOCKED rows and persisted batch_members agree as SETS.

        This is the F1 descendant of the old checkpoint-vs-batch_members
        reconcile. It degenerates to set equality because ``member_order`` IS
        the batch_members.ordinal ordering (derived by the caller) — comparing
        ordered tuples would compare batch_members against itself, proving
        nothing. The real cross-check left is membership: a token with a
        BLOCKED journal row but no batch_members row (or vice versa) means the
        journal and the audit trail disagree about the in-progress batch.
        """
        journal_set = set(journal_token_ids)
        member_set = set(member_order)
        if len(member_set) != len(member_order):
            duplicated = sorted(token_id for token_id, count in Counter(member_order).items() if count > 1)
            raise AuditIntegrityError(
                f"Duplicate token ids in batch_members order for aggregation node "
                f"{node_id!r} (run {self._run_id!r}): {duplicated!r} — audit trail corruption."
            )
        if journal_set != member_set:
            missing_from_journal = sorted(member_set - journal_set)
            missing_from_members = sorted(journal_set - member_set)
            raise AuditIntegrityError(
                f"Aggregation node {node_id!r} (run {self._run_id!r}): journal BLOCKED "
                f"rows and persisted batch_members disagree about batch membership. "
                f"In batch_members but not journal: {missing_from_journal!r}; "
                f"in journal but not batch_members: {missing_from_members!r}. "
                "Cannot safely resume this batch."
            )
