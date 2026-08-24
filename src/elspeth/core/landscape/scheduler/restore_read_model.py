"""Barrier restore read models over token outcomes.

These queries encode ADR-030 crash-window semantics for journal restore. They
live with scheduler/barrier recovery rather than the generic token-outcome
writer so the persistence layer does not own restore policy.

**Group-collector-node-resolution family (META-22)**: :meth:`BarrierRestoreReadModel.resolve_group_collector_node`
is the durable derivation of "which node(s) has this group completed at" —
a SET, not a single value, since a nested outer group's frame is carried by
every descendant token at any depth (I-6), so it can legitimately show
completions at more than one node once inner scopes have also completed.
Any caller holding BOTH a durable node_id (from this method or an
equivalent completed-group scan) AND a config-side node_id
(``self._node_ids[collector_name]``) for the SAME group must cross-check
membership — the config node must be ONE OF the durable set, never
compared as if the set were a single value — and fail closed on
disagreement (see :class:`~elspeth.engine.journal_restore.CollectorJournalRestorer`
for the restore-time item-level cross-check this backs). WS5's future
EXPAND re-derivation query should resolve through this same method rather
than re-deriving the join independently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import and_, func, select

from elspeth.contracts import (
    AggregationMemberAction,
    AggregationResultMember,
    CommittedAggregationChild,
    CommittedAggregationOutputReceipt,
    CommittedAggregationResidual,
    CommittedCoalesceResidual,
    NodeStateStatus,
    TokenOutcome,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import BatchStatus, FrameKind, OutputMode, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape._database_ops import DatabaseOps
from elspeth.core.landscape.batch_lineage import batch_retry_lineage_ids
from elspeth.core.landscape.model_loaders import TokenOutcomeLoader
from elspeth.core.landscape.schema import (
    aggregation_result_members_table,
    aggregation_result_outputs_table,
    aggregation_results_table,
    batch_members_table,
    batches_table,
    coalesce_effect_members_table,
    coalesce_effects_table,
    group_losses_table,
    group_records_table,
    node_states_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
    tokens_table,
)

_TOKEN_ID_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class GroupRecordRow:
    """One durable group roster-authority row (spec §4.3)."""

    run_id: str
    group_id: str
    kind: str
    opener_token_id: str
    member_count: int


class BarrierRestoreReadModel:
    """Read-only audit queries used by barrier journal restore."""

    def __init__(
        self,
        ops: DatabaseOps,
        *,
        token_outcome_loader: TokenOutcomeLoader,
    ) -> None:
        self._ops = ops
        self._token_outcome_loader = token_outcome_loader

    def _load_lineage_paths(self, run_id: str, token_ids: Sequence[str]) -> dict[str, tuple[LineageFrame, ...]]:
        """Batch-load lineage paths from token_lineage_frames (outermost first)."""
        ordered_token_ids = tuple(dict.fromkeys(token_ids))
        paths: dict[str, list[tuple[int, LineageFrame]]] = {token_id: [] for token_id in ordered_token_ids}
        for offset in range(0, len(ordered_token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = ordered_token_ids[offset : offset + _TOKEN_ID_CHUNK_SIZE]
            rows = self._ops.execute_fetchall(
                select(
                    token_lineage_frames_table.c.token_id,
                    token_lineage_frames_table.c.depth,
                    token_lineage_frames_table.c.kind,
                    token_lineage_frames_table.c.group_id,
                    token_lineage_frames_table.c.member_key,
                )
                .where(token_lineage_frames_table.c.run_id == run_id)
                .where(token_lineage_frames_table.c.token_id.in_(chunk))
            )
            for row in rows:
                paths[str(row.token_id)].append(
                    (int(row.depth), LineageFrame(kind=FrameKind(row.kind), group_id=str(row.group_id), member_key=str(row.member_key)))
                )
        result: dict[str, tuple[LineageFrame, ...]] = {}
        for token_id, entries in paths.items():
            entries.sort(key=lambda entry: entry[0])
            depths = [depth for depth, _frame in entries]
            if depths != list(range(len(depths))):
                raise AuditIntegrityError(f"token_lineage_frames for token {token_id!r} (run {run_id!r}) has non-dense depths {depths}")
            result[token_id] = tuple(frame for _depth, frame in entries)
        return result

    def verify_lineage_journal_consistency(self, run_id: str, items: Sequence[TokenWorkItem]) -> None:
        """Codec-vs-table bidirectional check (spec §4.3): each journal row's
        decoded lineage_path must equal the token's token_lineage_frames rows
        exactly (both directions — a frames row absent from the codec path
        and a codec frame absent from the table are BOTH AuditIntegrityError).
        """
        table_paths = self._load_lineage_paths(run_id, [item.token_id for item in items])
        for item in items:
            if item.lineage_path != table_paths[item.token_id]:
                raise AuditIntegrityError(
                    f"lineage journal/table divergence for token {item.token_id!r} (run {run_id!r}): "
                    f"journal={item.lineage_path!r} table={table_paths[item.token_id]!r}"
                )

    def list_live_buffered_outcomes(self, ref: TokenRef) -> list[TokenOutcome]:
        """All live BUFFERED outcomes for one token.

        "Live" means the token has no completed outcome; a flushed token's
        BUFFERED row is dead history and exempt. Multiple live rows signal a
        duplicate barrier acceptance that restore must refuse loudly.
        """
        terminal = token_outcomes_table.alias("terminal_outcomes")
        terminal_witness = (
            select(terminal.c.outcome_id)
            .where(terminal.c.token_id == ref.token_id)
            .where(terminal.c.run_id == ref.run_id)
            .where(terminal.c.completed == 1)
            .exists()
        )
        query = (
            select(token_outcomes_table)
            .where(token_outcomes_table.c.token_id == ref.token_id)
            .where(token_outcomes_table.c.run_id == ref.run_id)
            .where(token_outcomes_table.c.completed == 0)
            .where(token_outcomes_table.c.path == TerminalPath.BUFFERED.value)
            .where(~terminal_witness)
            .order_by(token_outcomes_table.c.recorded_at, token_outcomes_table.c.outcome_id)
        )
        return [self._token_outcome_loader.load(row) for row in self._ops.execute_fetchall(query)]

    def get_max_node_state_attempts(
        self,
        run_id: str,
        token_ids: Sequence[str],
        *,
        step_index: int | None = None,
    ) -> dict[str, int]:
        """Max ``node_states.attempt`` per token for resume attempt offsets."""
        result: dict[str, int] = {}
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id, func.max(node_states_table.c.attempt).label("max_attempt"))
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.token_id.in_(chunk))
                .group_by(node_states_table.c.token_id)
            )
            if step_index is not None:
                query = query.where(node_states_table.c.step_index == step_index)
            for row in self._ops.execute_fetchall(query):
                result[row.token_id] = int(row.max_attempt)
        return result

    def get_max_node_state_attempts_for_node(
        self,
        run_id: str,
        token_ids: Sequence[str],
        *,
        node_id: str,
    ) -> dict[str, int]:
        """Max ``node_states.attempt`` per token, scoped to ONE node (Task 7, META-14.1).

        Sibling of :meth:`get_max_node_state_attempts`, not a widening of it:
        that method's callers (``barrier_coordination.py``, ``scheduler_drain.py``)
        derive a token's resume attempt at the SAME node it is about to
        re-arrive at, where an unscoped max is the intended shape or is
        additionally scoped by ``step_index``. The collector's opener-attempt
        derivation is different in kind — the opener token visited at least
        one OTHER node (wherever it triggered the expansion) before the
        collector node ever wrote it a node_state at all, so an unscoped (or
        merely ``step_index``-scoped) read risks picking up that PRODUCER
        node's attempt instead of the collector's own flush-guard history,
        exactly the failure mode ``scheduler_drain.py``'s sink attempt-offset
        comment warns about ("producer-node attempts must not inflate the
        offset"). A dedicated node-scoped method removes the ambiguity
        entirely rather than relying on step_index correlation, which a
        constant step_resolver (as in this module's own test fixtures) cannot
        even exercise.
        """
        result: dict[str, int] = {}
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id, func.max(node_states_table.c.attempt).label("max_attempt"))
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.node_id == node_id)
                .where(node_states_table.c.token_id.in_(chunk))
                .group_by(node_states_table.c.token_id)
            )
            for row in self._ops.execute_fetchall(query):
                result[row.token_id] = int(row.max_attempt)
        return result

    def get_open_node_state_ids(
        self,
        run_id: str,
        *,
        node_ids: Sequence[str],
        token_ids: Sequence[str],
    ) -> dict[str, str]:
        """Outstanding OPEN node_state ids per token, at the given node(s).

        Generalised (Task 7, first collector user — was "coalesce-hold" only):
        parameterised by ``node_ids`` from the start, so it already worked for
        any barrier node with an accept-time hold journaled as a PENDING
        node_state (canon item 11) — coalesce, and now the collector's
        per-member arrival holds and M-4's restored-state_id cross-check.
        """
        if not node_ids:
            return {}
        result: dict[str, str] = {}
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id, node_states_table.c.state_id)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.node_id.in_(list(node_ids)))
                .where(node_states_table.c.token_id.in_(chunk))
                .where(node_states_table.c.status == NodeStateStatus.OPEN.value)
                .order_by(node_states_table.c.token_id, node_states_table.c.attempt)
            )
            for row in self._ops.execute_fetchall(query):
                result[row.token_id] = row.state_id
        return result

    def get_completed_row_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Completed ``(node_id, row_id)`` pairs for coalesce restore."""
        if not node_ids:
            return set()

        query = (
            select(node_states_table.c.node_id, tokens_table.c.row_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.row_id) for row in rows}

    def has_completed_row_for_node(self, *, run_id: str, node_id: str, row_id: str) -> bool:
        """Return whether one coalesce row completed at one node in one run."""
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                tokens_table.c.row_id == row_id,
                node_states_table.c.completed_at.isnot(None),
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def get_group_record(self, *, run_id: str, group_id: str) -> GroupRecordRow | None:
        """Durable roster authority for one group (spec §5 'minted')."""
        query = select(
            group_records_table.c.run_id,
            group_records_table.c.group_id,
            group_records_table.c.kind,
            group_records_table.c.opener_token_id,
            group_records_table.c.member_count,
        ).where(
            group_records_table.c.run_id == run_id,
            group_records_table.c.group_id == group_id,
        )
        row = self._ops.execute_fetchone(query)
        if row is None:
            return None
        return GroupRecordRow(
            run_id=row.run_id,
            group_id=row.group_id,
            kind=row.kind,
            opener_token_id=row.opener_token_id,
            member_count=row.member_count,
        )

    def get_group_member_keys(self, *, run_id: str, group_id: str) -> frozenset[str]:
        """DISTINCT member identities minted into one group (identity set, never a count).

        CAVEAT (M-3): an empty ``frozenset()`` is ambiguous between "no such
        group_id exists" and "a legal M=0 group exists with zero members"
        (record_empty_expansion / collect_tokens M=0 both mint the latter).
        Callers that must distinguish the two cases need ``get_group_record``
        first — its ``None`` vs a real row with ``member_count == 0`` makes
        the distinction this method cannot.
        """
        query = (
            select(token_lineage_frames_table.c.member_key)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.group_id == group_id,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return frozenset(row.member_key for row in rows)

    def get_group_member_ordinals(self, *, run_id: str, opener_token_id: str) -> dict[str, int]:
        """member token_id -> opener expansion ordinal (spec §5 flush order, decision 11).

        Resolved from the OPENER's ``token_parents`` rows -- never from an
        arriving token's own parent chain (a member whose subtree
        forked-and-coalesced arrives as a merged token with a fresh token_id).

        CAVEAT (M-4): keyed on ``parent_token_id`` alone, with no group
        discrimination. Task 3's collect_tokens makes a MEMBER token the
        parent of its own release children (``token_parents.parent_token_id
        = representative.token_id``) -- if the same token_id ever both opens
        a roster (this method's ``opener_token_id`` sense) AND receives
        release children as a collect representative, their ordinals would
        collide in the result. ``uq_group_records_opener`` blocks a token
        from opening two groups today, which prevents this in practice, but
        that guarantee is not enforced HERE -- see also the schema comment on
        ``uq_group_records_opener`` for the collect-release opener's
        weaker (eventually-sustained) invariant.
        """
        query = select(token_parents_table.c.token_id, token_parents_table.c.ordinal).where(
            token_parents_table.c.run_id == run_id,
            token_parents_table.c.parent_token_id == opener_token_id,
        )
        rows = self._ops.execute_fetchall(query)
        return {row.token_id: row.ordinal for row in rows}

    def has_completed_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool:
        """Group-keyed sibling of has_completed_row_for_node.

        Two sibling groups can share a row_id at one closer node (spec §5,
        arch M1), so completion must be tested per GROUP: any completed
        node_state at the node whose token carries a lineage frame in the
        group.

        DEPTH-AGNOSTIC (I-6): the join matches ``group_id`` at ANY frame
        depth on the token's lineage path, not only its innermost frame. A
        token whose path carries an ENCLOSING group_id (e.g. an inner
        collect release nested inside an outer expand) reports that outer
        group as "completed" too, once the inner one completes at this node
        -- this is intentional (an enclosing group's membership includes
        every token descended from it, at any depth), not a bug, but it means
        this method answers "did SOME token carrying this group_id complete
        here", not "did THIS group's own closer complete here".
        """
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                token_lineage_frames_table.c.group_id == group_id,
                node_states_table.c.completed_at.isnot(None),
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def resolve_group_collector_node(self, *, run_id: str, group_id: str) -> frozenset[str]:
        """The set of DURABLE node_ids where ``group_id`` shows a completed
        node_state (Task 7, META-22).

        Same depth-agnostic join as :meth:`has_completed_group_for_node`
        (I-6): any completed node_state whose token carries ``group_id``'s
        frame, at any depth, DISTINCT on node_id. Deliberately a SET, not a
        single value — nesting makes "exactly one node" false on healthy
        data: an OUTER group's frame is carried by every descendant token at
        ANY depth (see :func:`test_group_completion_joins_match_any_frame_depth_not_only_innermost`,
        the sibling test this method's own test file already documents this
        against), so a doubly-nested descendant completing at an INNER
        collector's node legitimately adds that inner node to this set
        alongside the outer group's own closer node. That is not ambiguity
        to resolve to one value — it is the same intentional any-depth
        semantic :meth:`has_completed_group_for_node` already has, applied
        to node identity instead of a boolean.

        Empty before the group has completed anywhere — there is no durable
        evidence yet, and config remains the only source before completion
        (a still-pending group's eventual collector cannot be cross-checked
        this way).

        Callers that need "is config's node ONE OF the durable nodes for
        this group" should test membership (``config_node_id in result``),
        never equality against a single resolved value — see
        :class:`~elspeth.engine.journal_restore.CollectorJournalRestorer`'s
        cross-check.
        """
        query = (
            select(node_states_table.c.node_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                token_lineage_frames_table.c.group_id == group_id,
                node_states_table.c.completed_at.isnot(None),
            )
            .distinct()
        )
        return frozenset(str(row.node_id) for row in self._ops.execute_fetchall(query))

    def get_settled_member_token_ids(self, *, run_id: str, node_id: str, group_id: str) -> frozenset[str]:
        """Token ids that settled ``group_id``'s roster at ``node_id`` (Task 7, META-20b).

        The durable twin of :meth:`has_completed_group_for_node` — same join,
        same DEPTH-AGNOSTIC semantics (I-6: matches ``group_id`` at any frame
        depth on the token's lineage path), but projecting
        ``node_states.token_id`` instead of an existence/``limit(1)`` check.
        Reconstructs `CollectorExecutor`'s in-memory settled-token-id memory
        (I-3, fix round 2) from durable state after a takeover or a
        ``max_completed_keys`` eviction (fix round 3, META-20b) — both
        producers of an empty in-memory set are the same class of gap, and
        this read is the honest fix for both.

        Returns TOKEN_IDS, not member_keys: ``accept()`` compares
        ``token.token_id`` against this set, and the two diverge for a merged
        member whose durable frame's member_key differs from the token_id
        that carries it (T4-5 prep's B-8) — do not "helpfully" convert to
        member_keys.
        """
        query = (
            select(node_states_table.c.token_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                token_lineage_frames_table.c.group_id == group_id,
                node_states_table.c.completed_at.isnot(None),
            )
            .distinct()
        )
        return frozenset(row.token_id for row in self._ops.execute_fetchall(query))

    def has_released_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool:
        """Status-COMPLETED variant (row_union release discrimination).

        Same DEPTH-AGNOSTIC join as ``has_completed_group_for_node`` (I-6):
        matches ``group_id`` at any frame depth, not only innermost. The
        additional ``status == COMPLETED`` predicate is what discriminates
        this from the plain-completion variant -- a FAILED or other
        terminal-but-non-COMPLETED node_state with ``completed_at`` set
        satisfies ``has_completed_group_for_node`` but not this method.
        """
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                token_lineage_frames_table.c.group_id == group_id,
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def get_completed_group_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Completed ``(node_id, group_id)`` pairs -- the group-keyed restore sweep.

        DEPTH-AGNOSTIC (I-6): same any-depth join as
        ``has_completed_group_for_node``. A token whose lineage path carries
        MULTIPLE group_ids (nested groups) emits one ``(node_id, group_id)``
        pair PER depth once it completes at that node -- an enclosing
        group's id appears here alongside its nested group's id, not just
        the innermost one.
        """
        if not node_ids:
            return set()
        query = (
            select(node_states_table.c.node_id, token_lineage_frames_table.c.group_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.group_id) for row in rows}

    def get_released_row_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Status-COMPLETED ``(node_id, row_id)`` pairs, row-keyed.

        Released-only sibling of :meth:`get_completed_row_ids_for_nodes`: a
        FAILED closure has ``completed_at`` set too, so a released-group
        classification must filter on status. WS4 Task 12: row_union's
        crash-window holdless-reconcile — the last row-keyed production
        caller — moved onto the group-keyed sibling
        :meth:`get_released_group_ids_for_nodes` (arch-M1). This row-keyed
        method currently has no production caller; it stays a public,
        directly-tested read-model primitive pending a ruling on removal.
        """
        if not node_ids:
            return set()

        query = (
            select(node_states_table.c.node_id, tokens_table.c.row_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.row_id) for row in rows}

    def get_released_group_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Status-COMPLETED ``(node_id, group_id)`` pairs -- the group-keyed
        released sweep (F-1, elspeth-14660ce1c0).

        Group-keyed sibling of :meth:`get_released_row_ids_for_nodes`, same
        relationship as :meth:`get_completed_group_ids_for_nodes` is to
        :meth:`get_completed_row_ids_for_nodes`: two sibling fork groups can
        share a row_id at one row_union node (spec §5, arch-M1), so the
        crash-window holdless-reconcile's release probe must discriminate by
        GROUP, not row. DEPTH-AGNOSTIC (I-6) like its completed-pairs
        sibling: a token whose lineage path carries multiple group_ids
        emits one ``(node_id, group_id)`` pair per depth once it releases at
        that node.
        """
        if not node_ids:
            return set()
        query = (
            select(node_states_table.c.node_id, token_lineage_frames_table.c.group_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.group_id) for row in rows}

    def has_released_row_for_node(self, *, run_id: str, node_id: str, row_id: str) -> bool:
        """Return whether one row completed as COMPLETED at one node in one run."""
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                tokens_table.c.row_id == row_id,
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def row_id_for_token(self, *, run_id: str, token_id: str) -> str | None:
        """Return the durable row_id for one token, or None if it never minted.

        Transitional resolution (spec §5/§6.2): the unified ``group_losses``
        ledger carries no ``row_id`` — callers that still key on
        ``(closer_name, row_id)`` resolve it from the token's own durable
        row here.
        """
        query = select(tokens_table.c.row_id).where(
            tokens_table.c.token_id == token_id,
            tokens_table.c.run_id == run_id,
        )
        row = self._ops.execute_fetchone(query)
        return None if row is None else str(row.row_id)

    def has_group_loss(self, *, run_id: str, closer_name: str, group_id: str) -> bool:
        """Return whether the unified ledger records any loss for one bound group.

        WS4 Task 12 re-key: replaces ``has_branch_loss_for_group`` (retired —
        that method joined through ``tokens`` to recover a row_id the
        unified ``group_losses`` ledger never needed in the first place;
        ``group_losses`` carries ``group_id`` directly, spec §6.2
        unification). No dual read — the join predecessor is gone, not kept
        alongside this as a second path.
        """
        query = (
            select(group_losses_table.c.loss_id)
            .where(
                group_losses_table.c.run_id == run_id,
                group_losses_table.c.closer_name == closer_name,
                group_losses_table.c.group_id == group_id,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def has_group_member_loss(self, *, run_id: str, closer_name: str, group_id: str, member_key: str) -> bool:
        """Return whether the durable group_losses ledger records THIS member's loss.

        Unlike ``has_branch_loss_for_group`` (row+closer_name-keyed, a
        retired-table compatibility shape), ``group_losses`` carries
        ``group_id`` and ``member_key`` directly (spec §6.2 unification) —
        no join through ``tokens`` needed. WS4 fix-round 2 (I-4): the
        collector's in-memory ``has_recorded_member_loss`` consults this as
        its durable fallback, since a resumed worker's in-memory
        ``_pending`` state carries no history of losses recorded before
        open, after close, or across a takeover.
        """
        query = (
            select(group_losses_table.c.loss_id)
            .where(
                group_losses_table.c.run_id == run_id,
                group_losses_table.c.closer_name == closer_name,
                group_losses_table.c.group_id == group_id,
                group_losses_table.c.member_key == member_key,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def find_failed_unrouted_terminal_token_ids(self, run_id: str, token_ids: Sequence[str]) -> frozenset[str]:
        """Token ids holding terminal ``(FAILURE, UNROUTED)`` outcomes.

        This is the ADR-030 aggregation restore reconcile signature for a crash
        after failed-flush terminal writes but before BLOCKED scheduler rows are
        released.
        """
        if not token_ids:
            return frozenset()
        query = (
            select(token_outcomes_table.c.token_id)
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.token_id.in_(tuple(token_ids)))
            .where(token_outcomes_table.c.completed == 1)
            .where(token_outcomes_table.c.outcome == TerminalOutcome.FAILURE.value)
            .where(token_outcomes_table.c.path == TerminalPath.UNROUTED.value)
        )
        return frozenset(row.token_id for row in self._ops.execute_fetchall(query))

    def get_committed_coalesce_residual(
        self,
        run_id: str,
        *,
        coalesce_node_id: str,
        coalesce_name: str,
        row_id: str,
        blocked_token_ids: Sequence[str],
    ) -> CommittedCoalesceResidual | None:
        """Return an exact completed coalesce effect still lacking continuation."""
        if not blocked_token_ids:
            return None
        effect_rows = self._ops.execute_fetchall(
            select(coalesce_effects_table)
            .where(coalesce_effects_table.c.run_id == run_id)
            .where(coalesce_effects_table.c.coalesce_node_id == coalesce_node_id)
            .where(coalesce_effects_table.c.row_id == row_id)
            .where(coalesce_effects_table.c.status == "completed")
            .order_by(coalesce_effects_table.c.effect_id)
        )
        if not effect_rows:
            return None
        if len(effect_rows) != 1:
            raise AuditIntegrityError(f"Coalesce residual {coalesce_name!r}/{row_id!r} has {len(effect_rows)} completed effect receipts")
        effect = effect_rows[0]
        members = self._ops.execute_fetchall(
            select(
                coalesce_effect_members_table.c.parent_token_id,
                coalesce_effect_members_table.c.parent_state_id,
                coalesce_effect_members_table.c.ordinal,
            )
            .where(coalesce_effect_members_table.c.run_id == run_id)
            .where(coalesce_effect_members_table.c.effect_id == effect.effect_id)
            .order_by(coalesce_effect_members_table.c.ordinal)
        )
        member_ids = tuple(str(row.parent_token_id) for row in members)
        if (
            not member_ids
            or tuple(int(row.ordinal) for row in members) != tuple(range(len(members)))
            or any(type(row.parent_state_id) is not str or not row.parent_state_id for row in members)
        ):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has incomplete ordered membership")
        blocked_set = frozenset(blocked_token_ids)
        member_set = frozenset(member_ids)
        blocked_members = member_set & blocked_set
        if not blocked_members:
            return None
        if blocked_members != member_set:
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has only a partial BLOCKED member set")

        terminal_rows = []
        for i in range(0, len(member_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = member_ids[i : i + _TOKEN_ID_CHUNK_SIZE]
            terminal_rows.extend(
                self._ops.execute_fetchall(
                    select(
                        token_outcomes_table.c.token_id,
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                    )
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id.in_(chunk))
                    .where(token_outcomes_table.c.completed == 1)
                )
            )
        if len(terminal_rows) != len(member_ids):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} lacks exact terminal parent outcomes")
        terminals = {str(row.token_id): row for row in terminal_rows}
        if len(terminals) != len(member_ids):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has duplicate terminal parent outcomes")
        for member_id in member_ids:
            outcome = terminals[member_id]
            # member-outcome join binding retired with the outcome column;
            # the result-token binding (tokens.join_group_id + composite FK)
            # is the durable anchor, checked below.
            if not (outcome.outcome == TerminalOutcome.SUCCESS.value and outcome.path == TerminalPath.COALESCED.value):
                raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has divergent parent outcomes")

        token = self._ops.execute_fetchone(
            select(
                tokens_table.c.token_id,
                tokens_table.c.row_id,
                tokens_table.c.join_group_id,
                tokens_table.c.token_data_ref,
                tokens_table.c.step_in_pipeline,
            )
            .where(tokens_table.c.run_id == run_id)
            .where(tokens_table.c.token_id == effect.result_token_id)
        )
        if (
            token is None
            or token.row_id != row_id
            or token.join_group_id != effect.result_join_group_id
            or token.token_data_ref != effect.expected_token_data_ref
            or type(token.token_data_ref) is not str
            or type(token.step_in_pipeline) is not int
        ):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has divergent result-token identity")
        expected_parent_set_hash = stable_hash(tuple(sorted(member_ids)))
        expected_effect_hash = stable_hash(
            {
                "ordered_parent_ids": member_ids,
                "step_in_pipeline": token.step_in_pipeline,
                "token_data_ref": token.token_data_ref,
            }
        )
        if effect.parent_set_hash != expected_parent_set_hash or effect.effect_hash != expected_effect_hash:
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has divergent identity hashes")
        has_result_outcome = self._ops.execute_fetchone(
            select(token_outcomes_table.c.outcome_id)
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.token_id == token.token_id)
            .limit(1)
        )
        has_result_work = self._ops.execute_fetchone(
            select(token_work_items_table.c.work_item_id)
            .where(token_work_items_table.c.run_id == run_id)
            .where(token_work_items_table.c.token_id == token.token_id)
            .limit(1)
        )
        if has_result_outcome is not None or has_result_work is not None:
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} result already has continuation evidence")
        return CommittedCoalesceResidual(
            effect_id=str(effect.effect_id),
            coalesce_node_id=coalesce_node_id,
            coalesce_name=coalesce_name,
            row_id=row_id,
            result_token_id=str(token.token_id),
            result_join_group_id=str(token.join_group_id),
            token_data_ref=token.token_data_ref,
            step_in_pipeline=token.step_in_pipeline,
            member_token_ids=member_ids,
        )

    def list_committed_aggregation_residuals(
        self,
        run_id: str,
        *,
        aggregation_node_id: str,
        blocked_token_ids: Sequence[str],
    ) -> tuple[CommittedAggregationResidual, ...]:
        """Return exact committed aggregation receipts still stranded BLOCKED.

        A receipt is admitted only when the completed batch/node result, exact
        ordered membership, terminal BATCH_CONSUMED inputs, claimed expansion
        group, ordered self-contained child tokens, and absence of any child
        scheduler/outcome continuation agree. Partial or mixed images fail
        closed instead of being mistaken for replayable buffered work.
        """
        if not blocked_token_ids:
            return ()

        consumed_rows = []
        for i in range(0, len(blocked_token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = tuple(blocked_token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            consumed_rows.extend(
                self._ops.execute_fetchall(
                    select(token_outcomes_table.c.token_id, token_outcomes_table.c.batch_id)
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id.in_(chunk))
                    .where(token_outcomes_table.c.completed == 1)
                    .where(token_outcomes_table.c.outcome == TerminalOutcome.TRANSIENT.value)
                    .where(token_outcomes_table.c.path == TerminalPath.BATCH_CONSUMED.value)
                    .order_by(token_outcomes_table.c.batch_id, token_outcomes_table.c.token_id)
                )
            )
        batch_ids = tuple(sorted({str(row.batch_id) for row in consumed_rows if row.batch_id is not None}))
        residuals: list[CommittedAggregationResidual] = []
        blocked_set = frozenset(blocked_token_ids)

        for batch_id in batch_ids:
            batch_row = self._ops.execute_fetchone(
                select(
                    batches_table.c.batch_id,
                    batches_table.c.aggregation_node_id,
                    batches_table.c.expansion_group_id,
                    batches_table.c.status,
                    node_states_table.c.status.label("node_state_status"),
                    node_states_table.c.output_hash,
                )
                .select_from(
                    batches_table.join(
                        node_states_table,
                        and_(
                            batches_table.c.aggregation_state_id == node_states_table.c.state_id,
                            batches_table.c.run_id == node_states_table.c.run_id,
                        ),
                    )
                )
                .where(batches_table.c.batch_id == batch_id)
                .where(batches_table.c.run_id == run_id)
                .where(batches_table.c.aggregation_node_id == aggregation_node_id)
            )
            if batch_row is None:
                batch_identity = self._ops.execute_fetchone(
                    select(batches_table.c.aggregation_node_id, batches_table.c.status)
                    .where(batches_table.c.batch_id == batch_id)
                    .where(batches_table.c.run_id == run_id)
                )
                if (
                    batch_identity is not None
                    and batch_identity.aggregation_node_id == aggregation_node_id
                    and batch_identity.status != BatchStatus.COMPLETED.value
                ):
                    continue
                raise AuditIntegrityError(
                    f"Committed aggregation residual batch {batch_id!r} is missing, foreign, or belongs to another node"
                )
            if (
                batch_row.status != BatchStatus.COMPLETED.value
                or batch_row.node_state_status != NodeStateStatus.COMPLETED.value
                or type(batch_row.expansion_group_id) is not str
                or not batch_row.expansion_group_id
                or type(batch_row.output_hash) is not str
                or not batch_row.output_hash
            ):
                raise AuditIntegrityError(
                    f"Committed aggregation residual batch {batch_id!r} lacks a completed node/result expansion receipt"
                )

            member_rows = self._ops.execute_fetchall(
                select(batch_members_table.c.token_id, batch_members_table.c.ordinal)
                .where(batch_members_table.c.batch_id == batch_id)
                .where(batch_members_table.c.run_id == run_id)
                .order_by(batch_members_table.c.ordinal)
            )
            member_ids = tuple(str(row.token_id) for row in member_rows)
            if not member_ids or tuple(int(row.ordinal) for row in member_rows) != tuple(range(len(member_rows))):
                raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has non-contiguous membership")
            if not frozenset(member_ids).issubset(blocked_set):
                raise AuditIntegrityError(
                    f"Committed aggregation residual batch {batch_id!r} is not represented by its exact BLOCKED member set"
                )

            terminal_rows = []
            for i in range(0, len(member_ids), _TOKEN_ID_CHUNK_SIZE):
                chunk = member_ids[i : i + _TOKEN_ID_CHUNK_SIZE]
                terminal_rows.extend(
                    self._ops.execute_fetchall(
                        select(
                            token_outcomes_table.c.token_id,
                            token_outcomes_table.c.outcome,
                            token_outcomes_table.c.path,
                            token_outcomes_table.c.batch_id,
                        )
                        .where(token_outcomes_table.c.run_id == run_id)
                        .where(token_outcomes_table.c.token_id.in_(chunk))
                        .where(token_outcomes_table.c.completed == 1)
                        .order_by(token_outcomes_table.c.token_id, token_outcomes_table.c.recorded_at)
                    )
                )
            terminals_by_token: dict[str, list[Any]] = {}
            for row in terminal_rows:
                terminals_by_token.setdefault(str(row.token_id), []).append(row)
            for member_id in member_ids:
                terminal = terminals_by_token[member_id] if member_id in terminals_by_token else []
                if len(terminal) != 1:
                    raise AuditIntegrityError(
                        f"Committed aggregation residual batch {batch_id!r} member {member_id!r} does not have one terminal outcome"
                    )
                outcome = terminal[0]
                batch_consumed = (
                    outcome.outcome == TerminalOutcome.TRANSIENT.value
                    and outcome.path == TerminalPath.BATCH_CONSUMED.value
                    and outcome.batch_id == batch_id
                )
                quarantined = (
                    outcome.outcome == TerminalOutcome.FAILURE.value
                    and outcome.path == TerminalPath.QUARANTINED_AT_SOURCE.value
                    and outcome.batch_id is None
                )
                if not (batch_consumed or quarantined):
                    raise AuditIntegrityError(
                        f"Committed aggregation residual batch {batch_id!r} member {member_id!r} has a divergent terminal outcome"
                    )

            # expand_group_id retired from tokens (D2 flip): a child of THIS
            # expansion is identified by its own token_lineage_frames row
            # (kind='expand', group_id=batch_row.expansion_group_id) instead
            # of the deleted tokens.expand_group_id column.
            child_rows = self._ops.execute_fetchall(
                select(
                    tokens_table.c.token_id,
                    tokens_table.c.row_id,
                    tokens_table.c.token_data_ref,
                    tokens_table.c.step_in_pipeline,
                    token_parents_table.c.parent_token_id,
                    token_parents_table.c.ordinal,
                )
                .select_from(
                    tokens_table.join(
                        token_parents_table,
                        and_(
                            tokens_table.c.token_id == token_parents_table.c.token_id,
                            tokens_table.c.run_id == token_parents_table.c.run_id,
                        ),
                    ).join(
                        token_lineage_frames_table,
                        and_(
                            tokens_table.c.token_id == token_lineage_frames_table.c.token_id,
                            tokens_table.c.run_id == token_lineage_frames_table.c.run_id,
                        ),
                    )
                )
                .where(tokens_table.c.run_id == run_id)
                .where(token_lineage_frames_table.c.kind == FrameKind.EXPAND.value)
                .where(token_lineage_frames_table.c.group_id == batch_row.expansion_group_id)
                .order_by(token_parents_table.c.ordinal)
            )
            if not child_rows or tuple(int(row.ordinal) for row in child_rows) != tuple(range(len(child_rows))):
                raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has no exact ordered child set")
            parent_ids = {str(row.parent_token_id) for row in child_rows}
            if len(parent_ids) != 1 or not parent_ids.issubset(frozenset(member_ids)):
                raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has divergent child parentage")

            children: list[CommittedAggregationChild] = []
            for row in child_rows:
                if type(row.token_data_ref) is not str or not row.token_data_ref or type(row.step_in_pipeline) is not int:
                    raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has an incomplete child receipt")
                child_has_outcome = self._ops.execute_fetchone(
                    select(token_outcomes_table.c.outcome_id)
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id == row.token_id)
                    .limit(1)
                )
                child_has_work = self._ops.execute_fetchone(
                    select(token_work_items_table.c.work_item_id)
                    .where(token_work_items_table.c.run_id == run_id)
                    .where(token_work_items_table.c.token_id == row.token_id)
                    .limit(1)
                )
                if child_has_outcome is not None or child_has_work is not None:
                    raise AuditIntegrityError(
                        f"Committed aggregation residual batch {batch_id!r} child {row.token_id!r} already has continuation evidence"
                    )
                children.append(
                    CommittedAggregationChild(
                        token_id=str(row.token_id),
                        row_id=str(row.row_id),
                        expand_group_id=str(batch_row.expansion_group_id),
                        token_data_ref=row.token_data_ref,
                        step_in_pipeline=row.step_in_pipeline,
                        parent_token_id=str(row.parent_token_id),
                        ordinal=row.ordinal,
                    )
                )

            residuals.append(
                CommittedAggregationResidual(
                    batch_id=batch_id,
                    aggregation_node_id=aggregation_node_id,
                    output_hash=batch_row.output_hash,
                    member_token_ids=member_ids,
                    children=tuple(children),
                )
            )

        return tuple(residuals)

    def list_committed_aggregation_output_receipts(
        self,
        run_id: str,
        *,
        aggregation_node_id: str,
        blocked_token_ids: Sequence[str],
    ) -> tuple[CommittedAggregationOutputReceipt, ...]:
        """Return completed aggregation outputs that have no routing claim."""
        if not blocked_token_ids:
            return ()
        candidate_batch_ids: set[str] = set()
        for i in range(0, len(blocked_token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = tuple(blocked_token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            rows = self._ops.execute_fetchall(
                select(aggregation_results_table.c.batch_id)
                .select_from(
                    aggregation_results_table.join(
                        batch_members_table,
                        and_(
                            aggregation_results_table.c.batch_id == batch_members_table.c.batch_id,
                            aggregation_results_table.c.run_id == batch_members_table.c.run_id,
                        ),
                    )
                )
                .where(aggregation_results_table.c.run_id == run_id)
                .where(batch_members_table.c.token_id.in_(chunk))
                .distinct()
            )
            candidate_batch_ids.update(str(row.batch_id) for row in rows)

        blocked_set = frozenset(blocked_token_ids)
        receipts: list[CommittedAggregationOutputReceipt] = []
        claimed_member_ids: set[str] = set()
        for batch_id in sorted(candidate_batch_ids):
            header = self._ops.execute_fetchone(
                select(
                    aggregation_results_table,
                    batches_table.c.aggregation_node_id,
                    batches_table.c.aggregation_state_id.label("batch_state_id"),
                    batches_table.c.expansion_group_id,
                    batches_table.c.status.label("batch_status"),
                    batches_table.c.retry_of_batch_id,
                    node_states_table.c.status.label("node_status"),
                    node_states_table.c.output_hash.label("node_output_hash"),
                )
                .select_from(
                    aggregation_results_table.join(
                        batches_table,
                        and_(
                            aggregation_results_table.c.batch_id == batches_table.c.batch_id,
                            aggregation_results_table.c.run_id == batches_table.c.run_id,
                        ),
                    ).join(
                        node_states_table,
                        and_(
                            aggregation_results_table.c.aggregation_state_id == node_states_table.c.state_id,
                            aggregation_results_table.c.run_id == node_states_table.c.run_id,
                        ),
                    )
                )
                .where(aggregation_results_table.c.batch_id == batch_id)
                .where(aggregation_results_table.c.run_id == run_id)
            )
            if header is None:
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} is missing its batch or node state")
            if header.expansion_group_id is not None:
                raise AuditIntegrityError(
                    f"Aggregation result receipt {batch_id!r} already has expansion claim {header.expansion_group_id!r} "
                    "but still has BLOCKED members"
                )
            try:
                output_mode = OutputMode(str(header.output_mode))
            except ValueError as exc:
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has unknown output mode") from exc
            shape_parent_valid = (
                (
                    output_mode is OutputMode.TRANSFORM
                    and header.output_shape in {"single", "multi"}
                    and header.expansion_parent_token_id is not None
                )
                or (output_mode is OutputMode.TRANSFORM and header.output_shape == "empty" and header.expansion_parent_token_id is None)
                or (
                    output_mode is OutputMode.PASSTHROUGH
                    and header.output_shape in {"empty", "multi"}
                    and header.expansion_parent_token_id is None
                )
            )
            if (
                header.aggregation_node_id != aggregation_node_id
                or header.batch_state_id != header.aggregation_state_id
                or header.batch_status != BatchStatus.COMPLETED.value
                or header.node_status != NodeStateStatus.COMPLETED.value
                or not shape_parent_valid
                or header.output_hash != header.node_output_hash
                or type(header.output_hash) is not str
            ):
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has divergent completion identity")

            member_rows = self._ops.execute_fetchall(
                select(batch_members_table.c.token_id, batch_members_table.c.ordinal)
                .where(batch_members_table.c.batch_id == batch_id)
                .where(batch_members_table.c.run_id == run_id)
                .order_by(batch_members_table.c.ordinal)
            )
            member_ids = tuple(str(row.token_id) for row in member_rows)
            if not member_ids or tuple(int(row.ordinal) for row in member_rows) != tuple(range(len(member_rows))):
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has invalid batch membership")
            if not frozenset(member_ids).issubset(blocked_set) or claimed_member_ids.intersection(member_ids):
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} lacks an exact non-overlapping BLOCKED member set")

            disposition_rows = self._ops.execute_fetchall(
                select(aggregation_result_members_table)
                .where(aggregation_result_members_table.c.batch_id == batch_id)
                .where(aggregation_result_members_table.c.run_id == run_id)
                .order_by(aggregation_result_members_table.c.ordinal)
            )
            if (
                tuple(int(row.ordinal) for row in disposition_rows) != tuple(range(len(member_ids)))
                or tuple(str(row.token_id) for row in disposition_rows) != member_ids
            ):
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has divergent ordered member actions")
            members: list[AggregationResultMember] = []
            for ordinal, row in enumerate(disposition_rows):
                try:
                    action = AggregationMemberAction(str(row.action))
                except ValueError as exc:
                    raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has unknown member action") from exc
                if action is AggregationMemberAction.QUARANTINE:
                    if row.error_hash != sha256(f"quarantined_in_batch:{batch_id}:{ordinal}".encode()).hexdigest()[:16]:
                        raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has divergent quarantine action")
                elif row.error_hash is not None:
                    raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has error_hash on a non-quarantine action")
                members.append(
                    AggregationResultMember(
                        member_ref=TokenRef(token_id=str(row.token_id), run_id=run_id),
                        action=action,
                        error_hash=row.error_hash,
                    )
                )
            actions = tuple(member.action for member in members)
            if output_mode is OutputMode.TRANSFORM and header.output_shape != "empty":
                if any(action not in {AggregationMemberAction.CONSUME_BATCH, AggregationMemberAction.QUARANTINE} for action in actions):
                    raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has illegal transform member actions")
                first_consumed = next(
                    (member.member_ref.token_id for member in members if member.action is AggregationMemberAction.CONSUME_BATCH),
                    None,
                )
                if first_consumed is None or header.expansion_parent_token_id != first_consumed:
                    raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has a divergent expansion parent")
            elif output_mode is OutputMode.TRANSFORM:
                if any(action not in {AggregationMemberAction.DROP_FILTERED, AggregationMemberAction.QUARANTINE} for action in actions):
                    raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has illegal empty transform member actions")
            elif header.output_shape == "multi":
                if any(action is not AggregationMemberAction.CONTINUE_PASSTHROUGH for action in actions):
                    raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has illegal passthrough member actions")
            elif any(action is not AggregationMemberAction.DROP_FILTERED for action in actions):
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has illegal empty passthrough member actions")
            terminal_member_ids: list[str] = []
            for i in range(0, len(member_ids), _TOKEN_ID_CHUNK_SIZE):
                chunk = member_ids[i : i + _TOKEN_ID_CHUNK_SIZE]
                terminal_rows = self._ops.execute_fetchall(
                    select(token_outcomes_table.c.token_id)
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id.in_(chunk))
                    .where(token_outcomes_table.c.completed == 1)
                )
                terminal_member_ids.extend(str(row.token_id) for row in terminal_rows)
            if terminal_member_ids:
                raise AuditIntegrityError(
                    f"Aggregation result receipt {batch_id!r} has members with terminal outcomes: {sorted(terminal_member_ids)!r}"
                )
            # A receipt committed by a resumed flush completes a RETRY batch;
            # its members' live BUFFERED acceptances keep the original
            # batch_id (immutable acceptance history), so the liveness proof
            # binds against the durable retry lineage.
            lineage_batch_ids = frozenset(
                batch_retry_lineage_ids(
                    self._ops.execute_fetchone,
                    batch_id=batch_id,
                    run_id=run_id,
                    aggregation_node_id=aggregation_node_id,
                    retry_of_batch_id=(str(header.retry_of_batch_id) if header.retry_of_batch_id is not None else None),
                )
            )
            for member_id in member_ids:
                live = self.list_live_buffered_outcomes(TokenRef(token_id=member_id, run_id=run_id))
                if len(live) != 1 or live[0].batch_id not in lineage_batch_ids:
                    raise AuditIntegrityError(
                        f"Aggregation result receipt {batch_id!r} lacks one live BUFFERED outcome per member within the batch retry lineage"
                    )

            output_rows = self._ops.execute_fetchall(
                select(aggregation_result_outputs_table)
                .where(aggregation_result_outputs_table.c.batch_id == batch_id)
                .where(aggregation_result_outputs_table.c.run_id == run_id)
                .order_by(aggregation_result_outputs_table.c.ordinal)
            )
            output_refs = tuple(str(row.token_data_ref) for row in output_rows)
            if (
                tuple(int(row.ordinal) for row in output_rows) != tuple(range(len(output_rows)))
                or (header.output_shape == "empty" and len(output_refs) != 0)
                or (header.output_shape == "single" and len(output_refs) != 1)
                or (header.output_shape == "multi" and len(output_refs) == 0)
                or (output_mode is OutputMode.PASSTHROUGH and header.output_shape == "multi" and len(output_refs) != len(member_ids))
                or any(len(ref) != 64 or any(character not in "0123456789abcdef" for character in ref) for ref in output_refs)
            ):
                raise AuditIntegrityError(f"Aggregation result receipt {batch_id!r} has invalid ordered output references")
            receipts.append(
                CommittedAggregationOutputReceipt(
                    batch_id=batch_id,
                    aggregation_node_id=aggregation_node_id,
                    aggregation_state_id=str(header.aggregation_state_id),
                    output_mode=output_mode.value,
                    output_shape=str(header.output_shape),
                    output_hash=str(header.output_hash),
                    output_refs=output_refs,
                    member_token_ids=member_ids,
                    members=tuple(members),
                    expansion_parent_token_id=(
                        str(header.expansion_parent_token_id) if header.expansion_parent_token_id is not None else None
                    ),
                )
            )
            claimed_member_ids.update(member_ids)
        return tuple(receipts)

    def find_released_node_state_token_ids(
        self,
        run_id: str,
        *,
        node_ids: Sequence[str],
        token_ids: Sequence[str],
    ) -> frozenset[str]:
        """Token ids holding a status-COMPLETED node_state at the given nodes.

        Token-scoped sibling of :meth:`get_released_row_ids_for_nodes`: release
        evidence for a (node, row) key proves the GROUP released, but a
        late-arrival residual shares that key while its own closure at the
        barrier node is FAILED. Restore's released-group reconstruction must
        admit only tokens the release itself completed.
        """
        if not node_ids:
            return frozenset()
        result: set[str] = set()
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.node_id.in_(list(node_ids)))
                .where(node_states_table.c.token_id.in_(chunk))
                .where(node_states_table.c.completed_at.isnot(None))
                .where(node_states_table.c.status == NodeStateStatus.COMPLETED.value)
            )
            result.update(row.token_id for row in self._ops.execute_fetchall(query))
        return frozenset(result)

    def find_duplicate_live_buffered_acceptances(self, run_id: str) -> list[tuple[str, int]]:
        """Run-wide sweep for tokens with more than one live BUFFERED outcome."""
        terminal = token_outcomes_table.alias("terminal_outcomes")
        terminal_witness = (
            select(terminal.c.outcome_id)
            .where(terminal.c.token_id == token_outcomes_table.c.token_id)
            .where(terminal.c.run_id == run_id)
            .where(terminal.c.completed == 1)
            .exists()
        )
        query = (
            select(token_outcomes_table.c.token_id, func.count())
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.completed == 0)
            .where(token_outcomes_table.c.path == TerminalPath.BUFFERED.value)
            .where(~terminal_witness)
            .group_by(token_outcomes_table.c.token_id)
            .having(func.count() > 1)
            .order_by(token_outcomes_table.c.token_id)
        )
        return [(str(row[0]), int(row[1])) for row in self._ops.execute_fetchall(query)]
