"""Row and token lifecycle persistence (split from ``DataFlowRepository``).

Owns the ``rows``, ``tokens`` and ``token_parents`` audit aggregates: source
row creation, token creation, and the atomic fork/coalesce/expand lineage
writes. Fork and expand record the parent's TRANSIENT outcome inside the same
transaction as the child inserts — that atomicity is why those
``token_outcomes`` writes live here rather than in the outcome component.

Atomic transactions in fork/coalesce/expand use the connection provider's
explicit write-transaction boundary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection, RowMapping

from elspeth.contracts import AggregationParentDisposition, CoalesceParentCompletion, Row, Token
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.contracts.enums import FrameKind, NodeStateStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame, pop_closer_frame, pop_fork_frame
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.checkpoint.serialization import checkpoint_dumps
from elspeth.core.ids import generate_id
from elspeth.core.landscape._database_ops import DatabaseOps
from elspeth.core.landscape._helpers import now
from elspeth.core.landscape.data_flow.ownership import RowTokenOwnership
from elspeth.core.landscape.data_flow.serialization import (
    canonical_or_recorded_hash,
    canonical_or_recorded_repr_payload,
)
from elspeth.core.landscape.ports import LandscapeConnectionProvider
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import (
    batch_members_table,
    batches_table,
    coalesce_effect_members_table,
    coalesce_effects_table,
    group_losses_table,
    group_records_table,
    node_states_table,
    rows_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
    tokens_table,
)

if TYPE_CHECKING:
    from elspeth.contracts.payload_store import PayloadStore
    from elspeth.core.landscape.data_flow.outcomes import TokenOutcomeRepository
    from elspeth.core.landscape.execution.node_states import NodeStateRepository

__all__ = ["RowTokenRepository"]


def _expected_child_frames(
    parent_path: tuple[LineageFrame, ...],
    *,
    kind: FrameKind,
    group_id: str,
    member_key: str,
) -> tuple[LineageFrame, ...]:
    """The child's expected persisted path: the parent's path plus one closer frame."""
    return (*parent_path, LineageFrame(kind=kind, group_id=group_id, member_key=member_key))


class RowTokenRepository:
    """Source row and token lifecycle writes: create, fork, coalesce, expand.

    Atomic transactions in fork/coalesce/expand use the connection provider's
    explicit write-transaction boundary.
    """

    def __init__(
        self,
        db: LandscapeConnectionProvider,
        ops: DatabaseOps,
        *,
        ownership: RowTokenOwnership,
        payload_store: PayloadStore | None = None,
        outcomes: TokenOutcomeRepository | None = None,
        node_states: NodeStateRepository | None = None,
    ) -> None:
        self._db = db
        self._ops = ops
        self._ownership = ownership
        self._payload_store = payload_store
        self._outcomes = outcomes
        self._node_states = node_states

    @staticmethod
    def _load_lineage_frames(conn: Connection, *, token_id: str, run_id: str) -> tuple[LineageFrame, ...]:
        """The token's durable lineage frames, outermost first, as typed frames."""
        rows = conn.execute(
            select(
                token_lineage_frames_table.c.kind,
                token_lineage_frames_table.c.group_id,
                token_lineage_frames_table.c.member_key,
            )
            .where(token_lineage_frames_table.c.token_id == token_id)
            .where(token_lineage_frames_table.c.run_id == run_id)
            .order_by(token_lineage_frames_table.c.depth)
        ).fetchall()
        return tuple(LineageFrame(kind=FrameKind(row.kind), group_id=str(row.group_id), member_key=str(row.member_key)) for row in rows)

    _LOAD_LINEAGE_PATHS_CHUNK_SIZE = 500

    def load_lineage_paths(
        self, run_id: str, token_ids: Sequence[str], *, conn: Connection | None = None
    ) -> dict[str, tuple[LineageFrame, ...]]:
        """Batch-load lineage paths from token_lineage_frames (outermost first).

        Every requested token_id is a key; tokens with no frames rows map to ().
        Depth gaps or duplicate depths are audit corruption (the frames are
        written atomically with the token INSERT — WS1a Task 6).

        ``conn``: reuse an ALREADY-OPEN transaction (e.g. a replay predicate
        running inside ``fork_token``'s write transaction) instead of opening
        a fresh one — SQLite refuses a nested BEGIN on the same connection.
        ``None`` (the default, external callers) opens its own read scope.
        """
        token_id_list = list(token_ids)
        paths: dict[str, list[tuple[int, LineageFrame]]] = {token_id: [] for token_id in token_id_list}

        def _load(active_conn: Connection) -> None:
            for offset in range(0, len(token_id_list), self._LOAD_LINEAGE_PATHS_CHUNK_SIZE):
                chunk = token_id_list[offset : offset + self._LOAD_LINEAGE_PATHS_CHUNK_SIZE]
                rows = active_conn.execute(
                    select(
                        token_lineage_frames_table.c.token_id,
                        token_lineage_frames_table.c.depth,
                        token_lineage_frames_table.c.kind,
                        token_lineage_frames_table.c.group_id,
                        token_lineage_frames_table.c.member_key,
                    )
                    .where(token_lineage_frames_table.c.run_id == run_id)
                    .where(token_lineage_frames_table.c.token_id.in_(chunk))
                    .order_by(token_lineage_frames_table.c.token_id, token_lineage_frames_table.c.depth)
                ).fetchall()
                for row in rows:
                    paths[str(row.token_id)].append(
                        (
                            int(row.depth),
                            LineageFrame(kind=FrameKind(row.kind), group_id=str(row.group_id), member_key=str(row.member_key)),
                        )
                    )

        if token_id_list:
            if conn is not None:
                _load(conn)
            else:
                with self._db.connection() as scoped_conn:
                    _load(scoped_conn)
        result: dict[str, tuple[LineageFrame, ...]] = {}
        for token_id, entries in paths.items():
            depths = [depth for depth, _frame in entries]
            if depths != list(range(len(depths))):
                raise AuditIntegrityError(
                    f"token_lineage_frames for token {token_id!r} (run {run_id!r}) has non-dense depths {depths} — audit corruption"
                )
            result[token_id] = tuple(frame for _depth, frame in entries)
        return result

    @staticmethod
    def _insert_lineage_frames(conn: Connection, *, token_id: str, run_id: str, frames: Sequence[LineageFrame]) -> None:
        for depth, frame in enumerate(frames):
            result = conn.execute(
                token_lineage_frames_table.insert().values(
                    token_id=token_id,
                    run_id=run_id,
                    depth=depth,
                    kind=frame.kind.value,
                    group_id=frame.group_id,
                    member_key=frame.member_key,
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"lineage frame INSERT affected zero rows (token_id={token_id}, depth={depth})")

    def _assert_parent_lineage(self, conn: Connection, *, parent_ref: TokenRef, supplied: tuple[LineageFrame, ...]) -> None:
        """Cross-check a parent's supplied current path against its durable mint frames.

        Exact equality, with ONE sanctioned divergence (ruling 27): a row_union
        release pops the parent's FORK frame via contracts.identity.pop_fork_frame
        — from wherever it sits in the mint path, not necessarily innermost (a
        row-multiplying transform inside the branch, e.g. an expand, stacks an
        EXPAND frame on top of the branch's FORK frame before the token reaches
        the union) — so a released token's current path is its mint frames minus
        that ONE FORK frame, symmetric with the pop itself. The release is
        journal-first, so the durable witness is the token's own row_union
        work-item row having left BLOCKED.
        """
        mint = self._load_lineage_frames(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id)
        if supplied == mint:
            return
        if mint and self._row_union_release_witness(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id):
            for frame in mint:
                if frame.kind is FrameKind.FORK and supplied == pop_fork_frame(mint, group_id=frame.group_id):
                    return
        raise AuditIntegrityError(
            f"parent lineage divergence for token {parent_ref.token_id!r} (run {parent_ref.run_id!r}): "
            f"supplied={supplied!r} mint={mint!r} and no completed row_union release explains the difference"
        )

    @staticmethod
    def _row_union_release_witness(conn: Connection, *, token_id: str, run_id: str) -> bool:
        """Durable evidence a row_union closer released this token (ruling 27)."""
        count = conn.execute(
            select(func.count())
            .select_from(token_work_items_table)
            .where(token_work_items_table.c.run_id == run_id)
            .where(token_work_items_table.c.token_id == token_id)
            .where(token_work_items_table.c.row_union_name.is_not(None))
            .where(token_work_items_table.c.status != TokenWorkStatus.BLOCKED.value)
        ).scalar()
        return bool(count)

    def _prepare_source_row_record(
        self,
        run_id: str,
        source_node_id: str,
        row_index: int,
        data: Mapping[str, object],
        *,
        source_row_index: int | None,
        ingest_sequence: int | None,
        row_id: str | None,
        quarantined: bool,
    ) -> Row:
        row_id = row_id or generate_id()
        missing_identity_fields = []
        if source_row_index is None:
            missing_identity_fields.append("source_row_index")
        if ingest_sequence is None:
            missing_identity_fields.append("ingest_sequence")
        if missing_identity_fields:
            raise AuditIntegrityError(
                f"create_row requires explicit source-scoped identity for run_id={run_id!r} row_id={row_id!r} "
                f"source_node_id={source_node_id!r}; missing {', '.join(missing_identity_fields)}. "
                "Do not fabricate source_row_index or ingest_sequence from row_index."
            )
        assert source_row_index is not None
        assert ingest_sequence is not None

        # Quarantined rows are Tier-3 external data that may contain non-canonical
        # values (NaN, Infinity). The coerce-and-record helper returns the canonical
        # hash or an explicit repr-based fallback per canonical.py docs. Non-quarantined
        # rows are trusted to be canonical — let stable_hash crash on any anomaly.
        if quarantined:
            data_hash = canonical_or_recorded_hash(data)
        else:
            data_hash = stable_hash(data)

        timestamp = now()

        # Landscape owns payload persistence - serialize and store if configured
        final_payload_ref: str | None = None
        if self._payload_store is not None:
            # Canonical JSON handles pandas/numpy/Decimal/datetime types.
            # For quarantined data, fall back to json.dumps(repr()) if
            # canonical serialization fails on non-canonical values.
            if quarantined:
                payload_bytes = canonical_or_recorded_repr_payload(data).encode("utf-8")
            else:
                payload_bytes = canonical_json(data).encode("utf-8")
            final_payload_ref = self._payload_store.store(payload_bytes)

        return Row(
            row_id=row_id,
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=row_index,
            source_row_index=source_row_index,
            ingest_sequence=ingest_sequence,
            source_data_hash=data_hash,
            source_data_ref=final_payload_ref,
            created_at=timestamp,
        )

    @staticmethod
    def _row_insert_values(row: Row) -> dict[str, object]:
        assert row.source_row_index is not None
        assert row.ingest_sequence is not None
        return {
            "row_id": row.row_id,
            "run_id": row.run_id,
            "source_node_id": row.source_node_id,
            "row_index": row.row_index,
            "source_row_index": row.source_row_index,
            "ingest_sequence": row.ingest_sequence,
            "source_data_hash": row.source_data_hash,
            "source_data_ref": row.source_data_ref,
            "created_at": row.created_at,
        }

    def create_row(
        self,
        run_id: str,
        source_node_id: str,
        row_index: int,
        data: Mapping[str, object],
        *,
        source_row_index: int | None = None,
        ingest_sequence: int | None = None,
        row_id: str | None = None,
        quarantined: bool = False,
    ) -> Row:
        """Create a source row record.

        Args:
            run_id: Run this row belongs to
            source_node_id: Source node that loaded this row
            row_index: Legacy/display row position
            data: Row data for hashing and optional storage
            source_row_index: Position within the source (0-indexed)
            ingest_sequence: Monotonic run-wide ingest order
            row_id: Optional row ID (generated if not provided)
            quarantined: If True, data is Tier-3 external data that may contain
                non-canonical values (NaN, Infinity). Uses repr_hash fallback.

        Returns:
            Row model

        Note:
            Payload persistence is handled by PayloadStore, not callers.
            If self._payload_store is configured, the method will:
            1. Serialize data using canonical_json (handles pandas/numpy/datetime/Decimal)
            2. Store in payload store
            3. Record reference in audit trail

            This ensures Landscape owns its audit format end-to-end.
        """
        row = self._prepare_source_row_record(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=row_index,
            data=data,
            source_row_index=source_row_index,
            ingest_sequence=ingest_sequence,
            row_id=row_id,
            quarantined=quarantined,
        )

        self._ops.execute_insert(rows_table.insert().values(**self._row_insert_values(row)))

        return row

    def create_row_with_token(
        self,
        run_id: str,
        source_node_id: str,
        row_index: int,
        data: Mapping[str, object],
        *,
        source_row_index: int | None = None,
        ingest_sequence: int | None = None,
        row_id: str | None = None,
        token_id: str | None = None,
        quarantined: bool = False,
        coordination_token: CoordinationToken | None = None,
    ) -> tuple[Row, Token]:
        """Create a source row and its initial token in one audit transaction.

        ``coordination_token`` (ADR-030 §C.4 row 9): an ingest-adjacent
        durable ``rows`` write at sequence N — when supplied, the
        verify-and-extend epoch fence is the first statement of the
        transaction (the boundary-failure and quarantine ingest arms ride
        this; the happy path composes through the scheduler's fenced
        ``ingest_row_with_initial_claim`` instead). ``None`` preserves the
        unfenced legacy arm for direct repository-level callers.
        """
        with self.create_row_with_token_transaction(coordination_token) as conn:
            return self.insert_row_with_token_on(
                conn,
                run_id=run_id,
                source_node_id=source_node_id,
                row_index=row_index,
                data=data,
                source_row_index=source_row_index,
                ingest_sequence=ingest_sequence,
                row_id=row_id,
                token_id=token_id,
                quarantined=quarantined,
            )

    def create_row_with_token_transaction(
        self,
        coordination_token: CoordinationToken | None,
    ) -> AbstractContextManager[Connection]:
        """Return the row/token write boundary for caller-owned composition."""
        if coordination_token is None:
            return self._db.write_connection()
        return fenced_leader_transaction(
            self._db.engine,
            token=coordination_token,
            now=now(),
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="create_row_with_token",
        )

    def insert_row_with_token_on(
        self,
        conn: Connection,
        *,
        run_id: str,
        source_node_id: str,
        row_index: int,
        data: Mapping[str, object],
        source_row_index: int | None = None,
        ingest_sequence: int | None = None,
        row_id: str | None = None,
        token_id: str | None = None,
        quarantined: bool = False,
    ) -> tuple[Row, Token]:
        """Connection-accepting rows+tokens insert: composes into the caller's transaction.

        Extracted from :meth:`create_row_with_token` so the fenced leader
        ingest (``TokenSchedulerRepository.ingest_row_with_initial_claim``,
        ADR-030 §C.4 row 9) can compose the rows insert, the tokens insert
        and the initial enqueue on ONE connection.
        """
        row = self._prepare_source_row_record(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=row_index,
            data=data,
            source_row_index=source_row_index,
            ingest_sequence=ingest_sequence,
            row_id=row_id,
            quarantined=quarantined,
        )
        token = Token(
            token_id=token_id or generate_id(),
            row_id=row.row_id,
            run_id=run_id,
            created_at=row.created_at,
        )

        result = conn.execute(rows_table.insert().values(**self._row_insert_values(row)))
        if result.rowcount == 0:
            raise AuditIntegrityError(f"create_row_with_token: row INSERT affected zero rows (row_id={row.row_id})")
        result = conn.execute(
            tokens_table.insert().values(
                token_id=token.token_id,
                row_id=token.row_id,
                run_id=token.run_id,
                join_group_id=token.join_group_id,
                created_at=token.created_at,
            )
        )
        if result.rowcount == 0:
            raise AuditIntegrityError(f"create_row_with_token: token INSERT affected zero rows (token_id={token.token_id})")
        # Root tokens have an empty lineage_path — no frames rows needed
        # (_insert_lineage_frames no-ops on an empty frame sequence).

        return row, token

    def create_token(
        self,
        row_id: str,
        *,
        token_id: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
        join_group_id: str | None = None,
    ) -> Token:
        """Create a token (row instance in DAG path).

        lineage_path frames are written to token_lineage_frames in the SAME
        transaction as the token INSERT (the sole lineage write path).
        join_group_id is the merge-event column and is set only by coalesce
        writers (kept column, anchors the coalesce_effects composite FK).

        Args:
            row_id: Source row this token represents
            token_id: Optional token ID (generated if not provided)
            lineage_path: Typed lineage-frame stack (crafted-token seam for
                tests and recovery tooling — production forks/expands write
                frames via their own transactions: fork_token / expand_token /
                coalesce_tokens).
            join_group_id: Optional join group (links merged tokens)

        Returns:
            Token model

        Raises:
            AuditIntegrityError: If row_id does not exist (Tier 1 corruption)
        """
        token_id = token_id or generate_id()
        timestamp = now()
        run_id = self._ownership.resolve_run_id_for_row(row_id)

        if join_group_id is not None and not join_group_id.strip():
            raise AuditIntegrityError(f"create_token: join_group_id must be None or non-empty, got {join_group_id!r}")

        token = Token(
            token_id=token_id,
            row_id=row_id,
            join_group_id=join_group_id,
            lineage_path=lineage_path,
            created_at=timestamp,
            run_id=run_id,
        )
        with self._db.write_connection() as conn:
            result = conn.execute(
                tokens_table.insert().values(
                    token_id=token.token_id,
                    row_id=token.row_id,
                    run_id=run_id,
                    join_group_id=token.join_group_id,
                    created_at=token.created_at,
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"create_token: token INSERT affected zero rows (token_id={token.token_id})")
            self._insert_lineage_frames(conn, token_id=token.token_id, run_id=run_id, frames=lineage_path)
        return token

    def fork_token(
        self,
        parent_ref: TokenRef,
        row_id: str,
        branches: list[str],
        *,
        step_in_pipeline: int | None = None,
        parent_lineage_path: tuple[LineageFrame, ...] | None = None,
    ) -> tuple[list[Token], str]:
        """Fork a token to multiple branches.

        ATOMIC: Creates children AND records parent FORKED outcome in single transaction.

        Validates that parent token belongs to the specified row_id and run_id
        before any writes. Cross-run/cross-row contamination crashes immediately
        per Tier 1 trust model.

        Args:
            parent_ref: TokenRef bundling parent token_id and run_id
            row_id: Row ID (same for all children)
            branches: List of branch names (must have at least one)
            step_in_pipeline: Step in the DAG where the fork occurs
            parent_lineage_path: The caller's (TokenManager's) in-memory
                current path for the parent, post-any-pop (decision D8). When
                supplied, cross-checked against the durable mint frames via
                ``_assert_parent_lineage`` and used — not the mint frames — to
                stack the child frame (ruling 27: a row_union-released parent's
                current path is its mint frames minus the popped FORK frame).
                ``None`` (direct/low-level callers) falls back to the durable
                mint frames with no cross-check.

        Returns:
            Tuple of (child Token models, fork_group_id)

        Raises:
            ValueError: If branches is empty (defense-in-depth for audit integrity)
            AuditIntegrityError: If parent token does not belong to specified run/row
        """
        # Defense-in-depth: validate even though RoutingAction.fork_to_paths()
        # already validates. Per CLAUDE.md "no silent drops" - empty forks
        # would cause tokens to disappear without audit trail.
        if not branches:
            raise ValueError("fork_token requires at least one branch")

        # Validate parent token ownership before any writes (Tier 1 invariant)
        self._ownership.validate_token_run_ownership(parent_ref)
        self._ownership.validate_token_row_ownership(parent_ref.token_id, row_id)

        if self._outcomes is None:
            raise RuntimeError("fork_token requires the token-outcome repository capability")

        with self._db.write_connection() as conn:
            self._outcomes.lock_token_outcome_dependencies((parent_ref,), conn=conn)
            existing_terminal = (
                conn.execute(
                    select(
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                    )
                    .where(token_outcomes_table.c.token_id == parent_ref.token_id)
                    .where(token_outcomes_table.c.run_id == parent_ref.run_id)
                    .where(token_outcomes_table.c.completed == 1)
                )
                .mappings()
                .one_or_none()
            )
            if existing_terminal is not None:
                return self._reconcile_fork_replay(
                    conn,
                    parent_ref=parent_ref,
                    row_id=row_id,
                    branches=branches,
                    step_in_pipeline=step_in_pipeline,
                    outcome=existing_terminal,
                )

            fork_group_id = generate_id()
            if parent_lineage_path is None:
                parent_frames = self._load_lineage_frames(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id)
            else:
                self._assert_parent_lineage(conn, parent_ref=parent_ref, supplied=parent_lineage_path)
                parent_frames = parent_lineage_path
            children = []
            # 1. Create child tokens
            for ordinal, branch_name in enumerate(branches):
                child_id = generate_id()
                timestamp = now()

                # Create child token (run_id derived from parent -- already validated)
                result = conn.execute(
                    tokens_table.insert().values(
                        token_id=child_id,
                        row_id=row_id,
                        run_id=parent_ref.run_id,
                        step_in_pipeline=step_in_pipeline,
                        created_at=timestamp,
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"fork_token: child token INSERT affected zero rows (token_id={child_id}, branch={branch_name!r})"
                    )

                # Record parent relationship
                result = conn.execute(
                    token_parents_table.insert().values(
                        token_id=child_id,
                        parent_token_id=parent_ref.token_id,
                        run_id=parent_ref.run_id,
                        ordinal=ordinal,
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"fork_token: token_parent INSERT affected zero rows (child={child_id}, parent={parent_ref.token_id})"
                    )

                child_path = (*parent_frames, LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch_name))
                self._insert_lineage_frames(conn, token_id=child_id, run_id=parent_ref.run_id, frames=child_path)

                children.append(
                    Token(
                        token_id=child_id,
                        row_id=row_id,
                        lineage_path=child_path,
                        step_in_pipeline=step_in_pipeline,
                        created_at=timestamp,
                        run_id=parent_ref.run_id,
                    )
                )

            # Mint the durable group record for the fork opener (spec §4.3
            # canon: group_records mints for BOTH FORK and EXPAND openers).
            result = conn.execute(
                group_records_table.insert().values(
                    run_id=parent_ref.run_id,
                    group_id=fork_group_id,
                    kind=FrameKind.FORK.value,
                    opener_token_id=parent_ref.token_id,
                    member_count=len(branches),
                    created_at=now(),
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"fork_token: group_records INSERT affected zero rows (group_id={fork_group_id})")

            # 2. Record parent FORKED outcome in SAME transaction (atomic)
            outcome_id = f"out_{generate_id()[:12]}"
            result = conn.execute(
                token_outcomes_table.insert().values(
                    outcome_id=outcome_id,
                    run_id=parent_ref.run_id,
                    token_id=parent_ref.token_id,
                    outcome=TerminalOutcome.TRANSIENT.value,
                    path=TerminalPath.FORK_PARENT.value,
                    completed=1,
                    recorded_at=now(),
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(
                    f"fork_token: FORKED outcome INSERT affected zero rows (parent={parent_ref.token_id}, outcome_id={outcome_id})"
                )

        return children, fork_group_id

    def _reconcile_fork_replay(
        self,
        conn: Connection,
        *,
        parent_ref: TokenRef,
        row_id: str,
        branches: Sequence[str],
        step_in_pipeline: int | None,
        outcome: RowMapping,
    ) -> tuple[list[Token], str]:
        """Return a previously committed exact fork or refuse divergent replay.

        The recorded roster is DERIVED from the children's persisted FORK
        frames (written atomically with the FORKED outcome — decision D2
        retired expected_branches_json): child i's innermost frame must be
        (FORK, fork_group_id, branches[i]) appended to the parent's own path.
        """
        children = self._load_children_for_parent(conn, parent_ref=parent_ref)
        parent_path = self.load_lineage_paths(parent_ref.run_id, [parent_ref.token_id], conn=conn)[parent_ref.token_id]
        child_paths = self.load_lineage_paths(parent_ref.run_id, [child.token_id for child, _ordinal in children], conn=conn)
        fork_group_ids = {
            child_paths[child.token_id][-1].group_id
            for child, _ordinal in children
            if child_paths[child.token_id] and child_paths[child.token_id][-1].kind is FrameKind.FORK
        }
        fork_group_id = next(iter(fork_group_ids)) if len(fork_group_ids) == 1 else None
        exact = (
            outcome["outcome"] == TerminalOutcome.TRANSIENT.value
            and outcome["path"] == TerminalPath.FORK_PARENT.value
            and fork_group_id is not None
            and len(children) == len(branches)
            and all(
                child.row_id == row_id
                and child.run_id == parent_ref.run_id
                and child.join_group_id is None
                and child.step_in_pipeline == step_in_pipeline
                and ordinal == expected_ordinal
                and child_paths[child.token_id]
                == _expected_child_frames(parent_path, kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch)
                for expected_ordinal, ((child, ordinal), branch) in enumerate(zip(children, branches, strict=True))
            )
        )
        if not exact:
            raise AuditIntegrityError(
                f"fork_token: divergent fork replay for parent token {parent_ref.token_id!r}; "
                "the requested branches or persisted lineage frames do not match the committed fork"
            )
        if fork_group_id is None:
            # Unreachable: `exact` already required `fork_group_id is not None` above.
            # Narrowing guard only — mypy cannot follow that through `exact`.
            raise AuditIntegrityError(f"fork_token: exact replay for parent token {parent_ref.token_id!r} lost its fork_group_id")
        return [child for child, _ordinal in children], fork_group_id

    def _load_children_for_parent(self, conn: Connection, *, parent_ref: TokenRef) -> list[tuple[Token, int]]:
        rows = (
            conn.execute(
                select(
                    tokens_table.c.token_id,
                    tokens_table.c.row_id,
                    tokens_table.c.run_id,
                    tokens_table.c.join_group_id,
                    tokens_table.c.step_in_pipeline,
                    tokens_table.c.token_data_ref,
                    tokens_table.c.created_at,
                    token_parents_table.c.ordinal,
                )
                .select_from(
                    token_parents_table.join(
                        tokens_table,
                        and_(
                            tokens_table.c.token_id == token_parents_table.c.token_id,
                            tokens_table.c.run_id == token_parents_table.c.run_id,
                        ),
                    )
                )
                .where(token_parents_table.c.parent_token_id == parent_ref.token_id)
                .where(token_parents_table.c.run_id == parent_ref.run_id)
                .order_by(token_parents_table.c.ordinal)
            )
            .mappings()
            .all()
        )
        lineage_paths = self.load_lineage_paths(parent_ref.run_id, [str(row["token_id"]) for row in rows], conn=conn)
        return [
            (
                Token(
                    token_id=str(row["token_id"]),
                    row_id=str(row["row_id"]),
                    run_id=str(row["run_id"]),
                    join_group_id=row["join_group_id"],
                    lineage_path=lineage_paths[str(row["token_id"])],
                    step_in_pipeline=row["step_in_pipeline"],
                    token_data_ref=row["token_data_ref"],
                    created_at=row["created_at"],
                ),
                int(row["ordinal"]),
            )
            for row in rows
        ]

    def coalesce_tokens(
        self,
        parent_refs: list[TokenRef],
        row_id: str,
        merged_payload: Mapping[str, object],
        *,
        coalesce_node_id: str | None = None,
        parent_state_ids: Sequence[str] | None = None,
        merged_contract: SchemaContract,
        step_in_pipeline: int | None = None,
        parent_lineage_paths: Mapping[str, tuple[LineageFrame, ...]] | None = None,
    ) -> Token:
        """Coalesce multiple tokens into one (join operation).

        ``parent_lineage_paths`` (decision D8, keyed by token_id): the
        caller's (TokenManager's) in-memory current path per parent,
        post-any-pop. When supplied, each parent is cross-checked against its
        durable mint frames via ``_assert_parent_lineage`` and the supplied
        path is popped — not the mint frames (ruling 27). ``None`` falls back
        to the durable mint frames with no cross-check.

        Creates a new token representing the merged result.
        Records all parent relationships.
        Persists a {data, contract} envelope to the payload store so the merged token
        is reconstructable on resume without re-executing the merge strategy and without
        any nodes-table lookup (ADDENDUM 3 — nodes.output_contract_json is NULL in prod
        for non-source nodes; the envelope is self-contained).

        Validates that all parent tokens belong to the specified row_id and
        that they all share the same run_id. Cross-run/cross-row contamination
        crashes immediately per Tier 1 trust model.

        Args:
            parent_refs: TokenRefs for tokens being merged (bundled token_id + run_id)
            row_id: Row ID for the merged token
            merged_payload: The merged row data dict to persist (Tier-1 audit write).
            merged_contract: The SchemaContract under which the merged token was produced.
                Serialised into the envelope via to_checkpoint_format() so recovery can
                restore a faithful PipelineRow without any nodes-table lookup.
                Uses checkpoint_dumps (type-faithful: preserves datetime via type-tagged
                envelopes) — NOT canonical_json, which would stringify datetime.
            step_in_pipeline: Step in the DAG where the coalesce occurs

        Returns:
            Merged Token model

        Raises:
            AuditIntegrityError: If parent tokens do not belong to specified row,
                if parent tokens span multiple runs, or if no payload store is configured
        """
        if self._payload_store is None:
            raise AuditIntegrityError(
                "coalesce_tokens requires a configured payload store — the merged token's "
                "payload must be persisted for resume correctness (epoch 11 invariant). "
                "Pass payload_store= to DataFlowRepository or RecorderFactory."
            )
        if not parent_refs:
            raise AuditIntegrityError(
                "coalesce_tokens requires at least one parent token — a coalesce with zero parents creates an unexplainable audit state"
            )
        ordered_parent_ids = tuple(ref.token_id for ref in parent_refs)
        duplicate_parent_ids = sorted(token_id for token_id, count in Counter(ordered_parent_ids).items() if count > 1)
        if duplicate_parent_ids:
            raise AuditIntegrityError(
                "coalesce_tokens received duplicate parent tokens; normalized effect membership requires distinct parents: "
                f"{duplicate_parent_ids!r}"
            )
        normalized_state_ids = None if parent_state_ids is None else tuple(parent_state_ids)
        if normalized_state_ids is not None:
            if len(normalized_state_ids) != len(parent_refs):
                raise AuditIntegrityError("coalesce_tokens parent_state_ids must align one-for-one with parent_refs")
            if any(not state_id for state_id in normalized_state_ids) or len(set(normalized_state_ids)) != len(normalized_state_ids):
                raise AuditIntegrityError("coalesce_tokens parent_state_ids must be distinct non-empty identities")
        resolved_node_id = coalesce_node_id or f"legacy-step:{step_in_pipeline if step_in_pipeline is not None else 'unknown'}"
        if not resolved_node_id:
            raise AuditIntegrityError("coalesce_tokens requires a non-empty coalesce_node_id")

        # Validate all parent tokens belong to the same row and run (Tier 1 invariant)
        run_id: str | None = None
        for ref in parent_refs:
            self._ownership.validate_token_row_ownership(ref.token_id, row_id)
            self._ownership.validate_token_run_ownership(ref)
            if run_id is None:
                run_id = ref.run_id
            elif ref.run_id != run_id:
                raise AuditIntegrityError(
                    f"Cross-run contamination prevented in coalesce: parent token {ref.token_id!r} "
                    f"belongs to run {ref.run_id!r}, but other parents belong to run {run_id!r}. "
                    f"All parent tokens in a coalesce must belong to the same run."
                )

        # Derive run_id from row if no parents (edge case: shouldn't happen in practice)
        if run_id is None:
            run_id = self._ownership.resolve_run_id_for_row(row_id)

        # Persist a self-contained {data, contract} envelope before the DB write so
        # the token_data_ref is available atomically at INSERT time.
        # The envelope carries both the row data and its SchemaContract so recovery can
        # reconstruct a faithful PipelineRow without any nodes-table lookup (ADDENDUM 3:
        # nodes.output_contract_json is NULL for non-source nodes in production).
        # checkpoint_dumps is type-faithful (datetime survives as datetime, not a
        # string) — canonical_json would stringify datetime and destroy Tier-1 fidelity.
        # Crash on store failure: a merged token with no persisted payload is
        # unreconstructable on resume (Tier-1 audit invariant).
        envelope = {"data": dict(merged_payload), "contract": merged_contract.to_checkpoint_format()}
        envelope_bytes = checkpoint_dumps(envelope).encode("utf-8")
        expected_token_data_ref = sha256(envelope_bytes).hexdigest()
        token_data_ref = self._payload_store.store(envelope_bytes)
        if token_data_ref != expected_token_data_ref:
            raise AuditIntegrityError(
                "coalesce_tokens payload store violated its content-addressed contract: "
                f"expected {expected_token_data_ref}, got {token_data_ref}"
            )

        canonical_parent_ids = tuple(sorted(ordered_parent_ids))
        parent_set_hash = stable_hash(canonical_parent_ids)
        effect_hash = stable_hash(
            {
                "ordered_parent_ids": ordered_parent_ids,
                "step_in_pipeline": step_in_pipeline,
                "token_data_ref": token_data_ref,
            }
        )
        scope = (
            coalesce_effects_table.c.run_id == run_id,
            coalesce_effects_table.c.coalesce_node_id == resolved_node_id,
            coalesce_effects_table.c.row_id == row_id,
            coalesce_effects_table.c.parent_set_hash == parent_set_hash,
        )

        with self._db.write_connection() as conn:
            # Global PostgreSQL order: parent tokens, parent node states, then
            # the effect scope row. Every writer for the same parent set must
            # overlap on these token locks, so the loser re-reads the winner's
            # committed effect below. A natural-key IntegrityError here is not
            # an idempotency signal; it indicates a violated lock invariant or
            # an unrelated structural constraint and must surface unchanged.
            self._lock_coalesce_dependencies(
                conn,
                parent_refs=parent_refs,
                parent_state_ids=normalized_state_ids,
                coalesce_node_id=resolved_node_id,
            )
            existing = conn.execute(select(coalesce_effects_table).where(*scope).with_for_update()).mappings().one_or_none()
            if existing is not None:
                return self._validate_and_load_coalesce_effect(
                    conn,
                    existing,
                    ordered_parent_ids=ordered_parent_ids,
                    parent_state_ids=normalized_state_ids,
                    step_in_pipeline=step_in_pipeline,
                    effect_hash=effect_hash,
                    expected_token_data_ref=expected_token_data_ref,
                )

            if parent_lineage_paths is None:
                parent_paths = [self._load_lineage_frames(conn, token_id=ref.token_id, run_id=run_id) for ref in parent_refs]
            else:
                for ref in parent_refs:
                    self._assert_parent_lineage(conn, parent_ref=ref, supplied=parent_lineage_paths[ref.token_id])
                parent_paths = [parent_lineage_paths[ref.token_id] for ref in parent_refs]
            anchor = parent_paths[0]
            if not anchor or anchor[-1].kind is not FrameKind.FORK:
                raise AuditIntegrityError(
                    f"coalesce_tokens: parent token {parent_refs[0].token_id!r} has no innermost FORK lineage frame "
                    f"to pop (frames={anchor!r}); a closer pops exactly its own frame (spec rulings 24/28)"
                )
            shared_group_id = anchor[-1].group_id
            remaining_paths: set[tuple[LineageFrame, ...]] = set()
            for ref, path in zip(parent_refs, parent_paths, strict=True):
                try:
                    remaining_paths.add(pop_closer_frame(path, kind=FrameKind.FORK, group_id=shared_group_id))
                except OrchestrationInvariantError as exc:
                    # pop_closer_frame refuses empty paths, non-FORK innermost
                    # frames, and cross-group parents in one place; this layer
                    # re-raises as the Tier-1 audit error it owes.
                    raise AuditIntegrityError(
                        f"coalesce_tokens: durable strict pop refused for parent token {ref.token_id!r}: {exc}"
                    ) from exc
            if len(remaining_paths) != 1:
                raise AuditIntegrityError(
                    "coalesce_tokens: parents do not share their remaining lineage path after the pop; "
                    f"distinct remaining paths={len(remaining_paths)}"
                )
            merged_frames = remaining_paths.pop()

            join_group_id = generate_id()
            token_id = generate_id()
            effect_id = generate_id()
            timestamp = now()
            result = conn.execute(
                tokens_table.insert().values(
                    token_id=token_id,
                    row_id=row_id,
                    run_id=run_id,
                    join_group_id=join_group_id,
                    step_in_pipeline=step_in_pipeline,
                    created_at=timestamp,
                    token_data_ref=token_data_ref,
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"coalesce_tokens: merged token INSERT affected zero rows (token_id={token_id})")
            self._insert_lineage_frames(conn, token_id=token_id, run_id=run_id, frames=merged_frames)

            # Record all parent relationships
            for ordinal, ref in enumerate(parent_refs):
                result = conn.execute(
                    token_parents_table.insert().values(
                        token_id=token_id,
                        parent_token_id=ref.token_id,
                        run_id=run_id,
                        ordinal=ordinal,
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"coalesce_tokens: token_parent INSERT affected zero rows (child={token_id}, parent={ref.token_id})"
                    )

            result = conn.execute(
                coalesce_effects_table.insert().values(
                    effect_id=effect_id,
                    run_id=run_id,
                    coalesce_node_id=resolved_node_id,
                    row_id=row_id,
                    parent_set_hash=parent_set_hash,
                    effect_hash=effect_hash,
                    expected_token_data_ref=expected_token_data_ref,
                    step_in_pipeline=step_in_pipeline,
                    status="materialized",
                    result_token_id=token_id,
                    result_join_group_id=join_group_id,
                    created_at=timestamp,
                    completed_at=None,
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"coalesce_tokens: effect INSERT affected zero rows (effect_id={effect_id})")
            for ordinal, ref in enumerate(parent_refs):
                result = conn.execute(
                    coalesce_effect_members_table.insert().values(
                        effect_id=effect_id,
                        run_id=run_id,
                        ordinal=ordinal,
                        parent_token_id=ref.token_id,
                        parent_state_id=None if normalized_state_ids is None else normalized_state_ids[ordinal],
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"coalesce_tokens: normalized effect member INSERT affected zero rows (effect_id={effect_id}, ordinal={ordinal})"
                    )

            return Token(
                token_id=token_id,
                row_id=row_id,
                join_group_id=join_group_id,
                lineage_path=merged_frames,
                step_in_pipeline=step_in_pipeline,
                created_at=timestamp,
                run_id=run_id,
                token_data_ref=token_data_ref,
            )

    def finalize_coalesce_effect(
        self,
        *,
        merged: Token,
        parent_completions: Sequence[CoalesceParentCompletion],
    ) -> None:
        """Atomically terminalize every parent and CAS the materialized effect."""
        if self._outcomes is None or self._node_states is None:
            raise RuntimeError("coalesce finalization is unavailable on this repository capability")
        if merged.join_group_id is None:
            raise AuditIntegrityError("coalesce finalization requires a merged token with join_group_id")
        completions = tuple(parent_completions)
        if not completions:
            raise AuditIntegrityError("coalesce finalization requires at least one parent completion")
        refs = tuple(item.parent_ref for item in completions)
        state_ids = tuple(item.state_id for item in completions)
        if len({ref.token_id for ref in refs}) != len(refs) or len(set(state_ids)) != len(state_ids):
            raise AuditIntegrityError("coalesce finalization requires distinct parent token and state identities")

        try:
            with self._db.write_connection() as conn:
                self._lock_coalesce_dependencies(
                    conn,
                    parent_refs=refs,
                    parent_state_ids=state_ids,
                    coalesce_node_id=None,
                )
                effect = (
                    conn.execute(
                        select(coalesce_effects_table)
                        .where(coalesce_effects_table.c.result_token_id == merged.token_id)
                        .where(coalesce_effects_table.c.run_id == merged.run_id)
                        .where(coalesce_effects_table.c.result_join_group_id == merged.join_group_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if effect is None:
                    raise AuditIntegrityError("coalesce finalization could not find the materialized effect receipt")
                members = self._load_coalesce_members(conn, str(effect["effect_id"]))
                observed = tuple((str(member["parent_token_id"]), member["parent_state_id"]) for member in members)
                expected = tuple((item.parent_ref.token_id, item.state_id) for item in completions)
                if tuple(token_id for token_id, _state_id in observed) != tuple(token_id for token_id, _state_id in expected):
                    raise AuditIntegrityError("coalesce finalization parent/state membership has a divergent ordered parent sequence")
                for member, (_token_id, expected_state_id) in zip(members, expected, strict=True):
                    observed_state_id = member["parent_state_id"]
                    if observed_state_id is None:
                        result = conn.execute(
                            coalesce_effect_members_table.update()
                            .where(coalesce_effect_members_table.c.effect_id == effect["effect_id"])
                            .where(coalesce_effect_members_table.c.ordinal == member["ordinal"])
                            .where(coalesce_effect_members_table.c.parent_state_id.is_(None))
                            .values(parent_state_id=expected_state_id)
                        )
                        if result.rowcount != 1:
                            raise AuditIntegrityError("coalesce finalization parent/state membership CAS lost")
                    elif str(observed_state_id) != expected_state_id:
                        raise AuditIntegrityError("coalesce finalization parent/state membership is divergent")

                if effect["status"] == "completed":
                    self._verify_completed_coalesce_effect(
                        conn,
                        merged=merged,
                        parent_completions=completions,
                    )
                    return
                if effect["status"] != "materialized":
                    raise AuditIntegrityError(f"coalesce effect has unknown status {effect['status']!r}")

                for item in completions:
                    self._node_states.complete_node_state(
                        state_id=item.state_id,
                        status=NodeStateStatus.COMPLETED,
                        output_data={"merged_into": merged.token_id},
                        duration_ms=item.duration_ms,
                        context_after=item.context_after,
                        conn=conn,
                    )
                    self._outcomes.record_token_outcome(
                        ref=item.parent_ref,
                        outcome=TerminalOutcome.SUCCESS,
                        path=TerminalPath.COALESCED,
                        conn=conn,
                        dependencies_prelocked=True,
                    )

                completed_at = now()
                result = conn.execute(
                    coalesce_effects_table.update()
                    .where(coalesce_effects_table.c.effect_id == effect["effect_id"])
                    .where(coalesce_effects_table.c.status == "materialized")
                    .where(coalesce_effects_table.c.completed_at.is_(None))
                    .values(status="completed", completed_at=completed_at)
                )
                if result.rowcount != 1:
                    raise AuditIntegrityError("coalesce effect completion CAS lost")
        except AuditIntegrityError:
            raise
        except Exception as exc:
            raise AuditIntegrityError(f"coalesce effect finalization failure: {type(exc).__name__}: {exc}") from exc

    def _lock_coalesce_dependencies(
        self,
        conn: Connection,
        *,
        parent_refs: Sequence[TokenRef],
        parent_state_ids: Sequence[str] | None,
        coalesce_node_id: str | None,
    ) -> None:
        if self._outcomes is None:
            raise RuntimeError("coalesce writes require the token-outcome repository capability")
        self._outcomes.lock_token_outcome_dependencies(parent_refs, conn=conn)
        if parent_state_ids is None:
            return
        state_rows = (
            conn.execute(
                select(
                    node_states_table.c.state_id,
                    node_states_table.c.token_id,
                    node_states_table.c.run_id,
                    node_states_table.c.node_id,
                )
                .where(node_states_table.c.state_id.in_(sorted(parent_state_ids)))
                .order_by(node_states_table.c.state_id)
                .with_for_update(of=node_states_table)
            )
            .mappings()
            .all()
        )
        if len(state_rows) != len(parent_state_ids):
            raise AuditIntegrityError("coalesce parent/state membership references a missing node state")
        expected_by_state = dict(zip(parent_state_ids, parent_refs, strict=True))
        for row in state_rows:
            expected_ref = expected_by_state[str(row["state_id"])]
            if row["token_id"] != expected_ref.token_id or row["run_id"] != expected_ref.run_id:
                raise AuditIntegrityError("coalesce parent/state membership crosses token or run identity")
            if coalesce_node_id is not None and row["node_id"] != coalesce_node_id:
                raise AuditIntegrityError("coalesce parent/state membership belongs to a different coalesce node")

    @staticmethod
    def _load_coalesce_members(conn: Connection, effect_id: str) -> list[RowMapping]:
        return list(
            conn.execute(
                select(coalesce_effect_members_table)
                .where(coalesce_effect_members_table.c.effect_id == effect_id)
                .order_by(coalesce_effect_members_table.c.ordinal)
            )
            .mappings()
            .all()
        )

    def _validate_and_load_coalesce_effect(
        self,
        conn: Connection,
        effect: RowMapping,
        *,
        ordered_parent_ids: Sequence[str],
        parent_state_ids: Sequence[str] | None,
        step_in_pipeline: int | None,
        effect_hash: str,
        expected_token_data_ref: str,
    ) -> Token:
        members = self._load_coalesce_members(conn, str(effect["effect_id"]))
        observed_parent_ids = tuple(str(member["parent_token_id"]) for member in members)
        if observed_parent_ids != tuple(ordered_parent_ids):
            raise AuditIntegrityError(
                "coalesce_tokens divergent collision: ordered parent sequence differs; "
                f"recorded={observed_parent_ids!r}, requested={tuple(ordered_parent_ids)!r}"
            )
        if parent_state_ids is not None:
            observed_state_ids = tuple(member["parent_state_id"] for member in members)
            if any(value is not None for value in observed_state_ids) and observed_state_ids != tuple(parent_state_ids):
                raise AuditIntegrityError("coalesce_tokens divergent collision: parent/state membership differs")
        if effect["step_in_pipeline"] != step_in_pipeline:
            raise AuditIntegrityError("coalesce_tokens divergent collision: step_in_pipeline differs")
        if effect["effect_hash"] != effect_hash or effect["expected_token_data_ref"] != expected_token_data_ref:
            raise AuditIntegrityError("coalesce_tokens divergent collision: merged payload/contract effect differs")

        token_row = (
            conn.execute(
                select(tokens_table)
                .where(tokens_table.c.token_id == effect["result_token_id"])
                .where(tokens_table.c.run_id == effect["run_id"])
                .where(tokens_table.c.join_group_id == effect["result_join_group_id"])
            )
            .mappings()
            .one_or_none()
        )
        if token_row is None:
            raise AuditIntegrityError("coalesce effect result tuple no longer resolves to its merged token")
        parent_links = tuple(
            conn.execute(
                select(token_parents_table.c.parent_token_id)
                .where(token_parents_table.c.token_id == token_row["token_id"])
                .where(token_parents_table.c.run_id == token_row["run_id"])
                .order_by(token_parents_table.c.ordinal)
            )
            .scalars()
            .all()
        )
        if parent_links != tuple(ordered_parent_ids):
            raise AuditIntegrityError("coalesce effect result token has divergent ordered parent links")
        merged_path = self.load_lineage_paths(str(token_row["run_id"]), [str(token_row["token_id"])], conn=conn)[str(token_row["token_id"])]
        return Token(
            token_id=str(token_row["token_id"]),
            row_id=str(token_row["row_id"]),
            run_id=str(token_row["run_id"]),
            join_group_id=token_row["join_group_id"],
            lineage_path=merged_path,
            step_in_pipeline=token_row["step_in_pipeline"],
            token_data_ref=token_row["token_data_ref"],
            created_at=token_row["created_at"],
        )

    @staticmethod
    def _verify_completed_coalesce_effect(
        conn: Connection,
        *,
        merged: Token,
        parent_completions: Sequence[CoalesceParentCompletion],
    ) -> None:
        state_ids = [item.state_id for item in parent_completions]
        states = conn.execute(
            select(node_states_table.c.state_id, node_states_table.c.status).where(node_states_table.c.state_id.in_(state_ids))
        ).all()
        if len(states) != len(state_ids) or any(row.status != NodeStateStatus.COMPLETED.value for row in states):
            raise AuditIntegrityError("completed coalesce effect has incomplete parent node-state evidence")
        # join_group_id retired from token_outcomes (D2): the merge-event
        # identity now lives solely on the result token's tokens.join_group_id
        # column (kept — the composite FK is the durable anchor for THIS
        # merge). This check verifies each parent has a terminal (SUCCESS,
        # COALESCED) outcome recorded; it can no longer cross-check that
        # outcome against a specific merge's join_group_id.
        token_ids = [item.parent_ref.token_id for item in parent_completions]
        outcomes = conn.execute(
            select(
                token_outcomes_table.c.token_id,
                token_outcomes_table.c.outcome,
                token_outcomes_table.c.path,
            )
            .where(token_outcomes_table.c.token_id.in_(token_ids))
            .where(token_outcomes_table.c.completed == 1)
        ).all()
        expected = {(token_id, TerminalOutcome.SUCCESS.value, TerminalPath.COALESCED.value) for token_id in token_ids}
        observed = {(row.token_id, row.outcome, row.path) for row in outcomes}
        if observed != expected:
            raise AuditIntegrityError("completed coalesce effect has divergent parent outcome evidence")

    def expand_token(
        self,
        parent_ref: TokenRef,
        row_id: str,
        child_payloads: Sequence[Mapping[str, object]],
        *,
        output_contract: SchemaContract,
        step_in_pipeline: int | None = None,
        parent_path: TerminalPath = TerminalPath.EXPAND_PARENT,
        parent_batch_id: str | None = None,
        aggregation_parent_dispositions: Sequence[AggregationParentDisposition] = (),
        parent_lineage_path: tuple[LineageFrame, ...] | None = None,
    ) -> tuple[list[Token], str]:
        """Expand a token into multiple child tokens (deaggregation).

        ``parent_lineage_path`` (decision D8): the caller's (TokenManager's)
        in-memory current path for the parent, post-any-pop. When supplied,
        cross-checked against the durable mint frames via
        ``_assert_parent_lineage`` and used — not the mint frames — to stack
        each child's EXPAND frame (ruling 27: a row_union-released parent's
        current path is its mint frames minus the popped FORK frame). ``None``
        falls back to the durable mint frames with no cross-check.

        ATOMIC: Creates children and records the parent's explicit terminal
        disposition in one transaction.

        Validates that parent token belongs to the specified row_id and run_id
        before any writes. Cross-run/cross-row contamination crashes immediately
        per Tier 1 trust model.

        Creates N child tokens from a single parent for 1->N expansion.
        All children share the same row_id (same source row) and are
        linked to the parent via token_parents table.

        Unlike fork_token (parallel DAG paths with branch names), expand_token
        creates sequential children for deaggregation transforms.

        Each child's {data, contract} envelope is persisted to the payload store before
        the DB write so token_data_ref is written atomically at INSERT time. This
        makes each expanded child self-contained and reconstructable on resume without
        re-executing the deaggregation transform and without any nodes-table lookup
        (ADDENDUM 3 — nodes.output_contract_json is NULL for non-source nodes in prod).

        Args:
            parent_ref: TokenRef bundling parent token_id and run_id
            row_id: Row ID (same for all children)
            child_payloads: Per-child row data dicts (one per expanded child).
                Must have at least 1 element. Uses checkpoint_dumps (type-faithful:
                preserves datetime via type-tagged envelopes) — NOT canonical_json.
            output_contract: The SchemaContract shared by all expanded children (from
                TransformResult.contract, locked before expansion). Serialised into each
                child's envelope so recovery can restore a faithful PipelineRow.
            step_in_pipeline: Step where expansion occurs (optional)
            parent_path: Parent terminal path. Normal deaggregation uses
                EXPAND_PARENT; batch aggregation uses BATCH_CONSUMED.
            parent_batch_id: Required when parent_path is BATCH_CONSUMED and
                forbidden for EXPAND_PARENT.

        Returns:
            Tuple of (child Token list, expand_group_id)

        Raises:
            ValueError: If child_payloads is empty
            AuditIntegrityError: If parent token does not belong to specified run/row,
                or if no payload store is configured
        """
        count = len(child_payloads)
        if count < 1:
            raise ValueError("expand_token requires at least 1 child payload")

        if parent_path == TerminalPath.EXPAND_PARENT:
            if parent_batch_id is not None:
                raise ValueError("expand_token EXPAND_PARENT forbids parent_batch_id")
        elif parent_path == TerminalPath.BATCH_CONSUMED:
            if parent_batch_id is None:
                raise ValueError("expand_token BATCH_CONSUMED requires parent_batch_id")
        else:
            raise ValueError(f"expand_token parent_path must be EXPAND_PARENT or BATCH_CONSUMED, got {parent_path.value!r}")

        dispositions = tuple(aggregation_parent_dispositions)
        if parent_path == TerminalPath.EXPAND_PARENT and dispositions:
            raise ValueError("expand_token EXPAND_PARENT forbids aggregation_parent_dispositions")
        if parent_path == TerminalPath.BATCH_CONSUMED:
            if not dispositions:
                assert parent_batch_id is not None  # validated above
                member_rows = self._ops.execute_fetchall(
                    select(batch_members_table.c.token_id)
                    .where(batch_members_table.c.batch_id == parent_batch_id)
                    .where(batch_members_table.c.run_id == parent_ref.run_id)
                    .order_by(batch_members_table.c.ordinal)
                )
                dispositions = tuple(
                    AggregationParentDisposition(
                        parent_ref=TokenRef(token_id=str(row.token_id), run_id=parent_ref.run_id),
                        outcome=TerminalOutcome.TRANSIENT,
                        path=TerminalPath.BATCH_CONSUMED,
                    )
                    for row in member_rows
                )
                if not dispositions:
                    raise AuditIntegrityError(
                        f"expand_token: parent token {parent_ref.token_id!r} is not a member of an existing non-empty batch"
                    )
            disposition_refs = tuple(item.parent_ref for item in dispositions)
            if len({ref.token_id for ref in disposition_refs}) != len(disposition_refs):
                raise AuditIntegrityError("batch expansion parent dispositions contain duplicate tokens")
            if parent_ref.token_id not in {ref.token_id for ref in disposition_refs}:
                raise AuditIntegrityError("batch expansion parent dispositions omit the expansion parent")
            for item in dispositions:
                if item.parent_ref.run_id != parent_ref.run_id:
                    raise AuditIntegrityError("batch expansion parent dispositions cross run identity")
                consumed = (
                    item.outcome is TerminalOutcome.TRANSIENT and item.path is TerminalPath.BATCH_CONSUMED and item.error_hash is None
                )
                quarantined = (
                    item.outcome is TerminalOutcome.FAILURE
                    and item.path is TerminalPath.QUARANTINED_AT_SOURCE
                    and type(item.error_hash) is str
                    and bool(item.error_hash)
                )
                if not (consumed or quarantined):
                    raise AuditIntegrityError("batch expansion parent disposition is not BATCH_CONSUMED or quarantined")

        if self._payload_store is None:
            raise AuditIntegrityError(
                "expand_token requires a configured payload store — each expanded child's "
                "payload must be persisted for resume correctness (epoch 11 invariant). "
                "Pass payload_store= to DataFlowRepository or RecorderFactory."
            )

        # Validate parent token ownership before any writes (Tier 1 invariant)
        self._ownership.validate_token_run_ownership(parent_ref)
        self._ownership.validate_token_row_ownership(parent_ref.token_id, row_id)

        expand_group_id = generate_id()
        children = []

        # Prepare each child's self-contained {data, contract} envelope and its
        # protocol-defined content address before taking the expansion claim.
        # Exact or divergent replay performs no payload-store writes. A new claim
        # stores the envelopes after the terminal-outcome lock/check and verifies
        # that the store returned the required SHA-256 identities before any DB
        # child insert.
        # checkpoint_dumps is type-faithful (datetime preserved as datetime, not
        # stringified) — canonical_json would destroy Tier-1 fidelity.
        # Crash on store failure: a child token with no persisted payload is
        # unreconstructable on resume (epoch 11 invariant).
        contract_fmt = output_contract.to_checkpoint_format()
        child_payload_bytes = [
            checkpoint_dumps({"data": dict(payload), "contract": contract_fmt}).encode("utf-8") for payload in child_payloads
        ]
        child_data_refs = [sha256(payload_bytes).hexdigest() for payload_bytes in child_payload_bytes]

        if self._outcomes is None:
            raise RuntimeError("expand_token requires the token-outcome repository capability")

        with self._db.write_connection() as conn:
            # Serialize expansion contenders on PostgreSQL before child inserts;
            # SQLite's write_connection() already holds BEGIN IMMEDIATE.  The
            # completed-outcome check is the durable claim shared by normal and
            # batch expansion paths.
            locked_refs = tuple(item.parent_ref for item in dispositions) if dispositions else (parent_ref,)
            self._outcomes.lock_token_outcome_dependencies(locked_refs, conn=conn)
            existing_terminal = (
                conn.execute(
                    select(
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                        token_outcomes_table.c.batch_id,
                    )
                    .where(token_outcomes_table.c.token_id == parent_ref.token_id)
                    .where(token_outcomes_table.c.run_id == parent_ref.run_id)
                    .where(token_outcomes_table.c.completed == 1)
                )
                .mappings()
                .one_or_none()
            )
            if existing_terminal is not None:
                if parent_path == TerminalPath.EXPAND_PARENT and existing_terminal["path"] == TerminalPath.EXPAND_PARENT.value:
                    return self._reconcile_expansion_replay(
                        conn,
                        parent_ref=parent_ref,
                        row_id=row_id,
                        child_data_refs=child_data_refs,
                        step_in_pipeline=step_in_pipeline,
                        outcome=existing_terminal,
                        expand_group_id=None,
                        expected_path=TerminalPath.EXPAND_PARENT,
                    )
                if parent_path == TerminalPath.BATCH_CONSUMED and existing_terminal["path"] == TerminalPath.BATCH_CONSUMED.value:
                    assert parent_batch_id is not None  # validated above
                    batch_claim = conn.execute(
                        select(batches_table.c.run_id, batches_table.c.expansion_group_id).where(
                            batches_table.c.batch_id == parent_batch_id
                        )
                    ).one_or_none()
                    if existing_terminal["batch_id"] != parent_batch_id or batch_claim is None or batch_claim.run_id != parent_ref.run_id:
                        raise AuditIntegrityError(
                            f"expand_token: divergent expansion replay for parent token {parent_ref.token_id!r}; "
                            "the committed batch claim does not match the requested batch"
                        )
                    self._verify_aggregation_parent_dispositions(
                        conn,
                        batch_id=parent_batch_id,
                        dispositions=dispositions,
                    )
                    return self._reconcile_expansion_replay(
                        conn,
                        parent_ref=parent_ref,
                        row_id=row_id,
                        child_data_refs=child_data_refs,
                        step_in_pipeline=step_in_pipeline,
                        outcome=existing_terminal,
                        expand_group_id=batch_claim.expansion_group_id,
                        expected_path=TerminalPath.BATCH_CONSUMED,
                    )
                raise AuditIntegrityError(
                    f"expand_token: parent token {parent_ref.token_id!r} already has a terminal outcome ({existing_terminal.path!r})"
                )

            if parent_lineage_path is None:
                parent_frames = self._load_lineage_frames(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id)
            else:
                self._assert_parent_lineage(conn, parent_ref=parent_ref, supplied=parent_lineage_path)
                parent_frames = parent_lineage_path

            stored_refs = [self._payload_store.store(payload_bytes) for payload_bytes in child_payload_bytes]
            if stored_refs != child_data_refs:
                raise AuditIntegrityError(
                    "expand_token: payload store returned an identity other than the required SHA-256 content address"
                )

            if parent_path == TerminalPath.BATCH_CONSUMED:
                assert parent_batch_id is not None  # validated above
                membership = conn.execute(
                    select(batch_members_table.c.token_id, batch_members_table.c.ordinal)
                    .where(batch_members_table.c.batch_id == parent_batch_id)
                    .where(batch_members_table.c.run_id == parent_ref.run_id)
                    .order_by(batch_members_table.c.ordinal)
                ).all()
                member_ids = tuple(str(row.token_id) for row in membership)
                if member_ids != tuple(item.parent_ref.token_id for item in dispositions):
                    raise AuditIntegrityError(
                        f"expand_token: parent dispositions do not match the exact ordered membership of batch "
                        f"{parent_batch_id!r} in run {parent_ref.run_id!r}"
                    )

                claim = conn.execute(
                    batches_table.update()
                    .where(batches_table.c.batch_id == parent_batch_id)
                    .where(batches_table.c.run_id == parent_ref.run_id)
                    .where(batches_table.c.expansion_group_id.is_(None))
                    .values(expansion_group_id=expand_group_id)
                )
                if claim.rowcount != 1:
                    existing_claim = conn.execute(
                        select(batches_table.c.run_id, batches_table.c.expansion_group_id).where(
                            batches_table.c.batch_id == parent_batch_id
                        )
                    ).one_or_none()
                    if existing_claim is None:
                        raise AuditIntegrityError(f"expand_token: batch {parent_batch_id!r} does not exist")
                    if existing_claim.run_id != parent_ref.run_id:
                        raise AuditIntegrityError(
                            f"expand_token: batch {parent_batch_id!r} belongs to run {existing_claim.run_id!r}, not {parent_ref.run_id!r}"
                        )
                    raise AuditIntegrityError(
                        f"expand_token: batch {parent_batch_id!r} already claimed an expansion ({existing_claim.expansion_group_id!r})"
                    )

            for ordinal, payload_ref in enumerate(child_data_refs):
                child_id = generate_id()
                timestamp = now()

                # Create child token (run_id from parent -- already validated)
                result = conn.execute(
                    tokens_table.insert().values(
                        token_id=child_id,
                        row_id=row_id,
                        run_id=parent_ref.run_id,
                        step_in_pipeline=step_in_pipeline,
                        created_at=timestamp,
                        token_data_ref=payload_ref,
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"expand_token: child token INSERT affected zero rows (token_id={child_id}, ordinal={ordinal})"
                    )

                # Record parent relationship
                result = conn.execute(
                    token_parents_table.insert().values(
                        token_id=child_id,
                        parent_token_id=parent_ref.token_id,
                        run_id=parent_ref.run_id,
                        ordinal=ordinal,
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"expand_token: token_parent INSERT affected zero rows (child={child_id}, parent={parent_ref.token_id})"
                    )

                child_path = (*parent_frames, LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=child_id))
                self._insert_lineage_frames(conn, token_id=child_id, run_id=parent_ref.run_id, frames=child_path)

                children.append(
                    Token(
                        token_id=child_id,
                        row_id=row_id,
                        lineage_path=child_path,
                        step_in_pipeline=step_in_pipeline,
                        created_at=timestamp,
                        run_id=parent_ref.run_id,
                        token_data_ref=payload_ref,
                    )
                )

            # Mint the durable group record for the expand opener (spec §4.3
            # canon: BATCH_CONSUMED flushes included — one row per aggregation
            # flush is accepted audit enrichment).
            result = conn.execute(
                group_records_table.insert().values(
                    run_id=parent_ref.run_id,
                    group_id=expand_group_id,
                    kind=FrameKind.EXPAND.value,
                    opener_token_id=parent_ref.token_id,
                    member_count=len(child_data_refs),
                    created_at=now(),
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"expand_token: group_records INSERT affected zero rows (group_id={expand_group_id})")

            # Record the explicit parent disposition in the SAME transaction.
            # No successful expansion path may leave a reprocessable parent.
            if parent_path == TerminalPath.EXPAND_PARENT:
                outcome_id = f"out_{generate_id()[:12]}"
                result = conn.execute(
                    token_outcomes_table.insert().values(
                        outcome_id=outcome_id,
                        run_id=parent_ref.run_id,
                        token_id=parent_ref.token_id,
                        outcome=TerminalOutcome.TRANSIENT.value,
                        path=TerminalPath.EXPAND_PARENT.value,
                        completed=1,
                        recorded_at=now(),
                    )
                )
                if result.rowcount == 0:
                    raise AuditIntegrityError(
                        f"expand_token: EXPANDED outcome INSERT affected zero rows (parent={parent_ref.token_id}, outcome_id={outcome_id})"
                    )
            else:
                for item in dispositions:
                    self._outcomes.record_token_outcome(
                        ref=item.parent_ref,
                        outcome=item.outcome,
                        path=item.path,
                        batch_id=parent_batch_id if item.path is TerminalPath.BATCH_CONSUMED else None,
                        error_hash=item.error_hash,
                        conn=conn,
                        dependencies_prelocked=True,
                    )

        return children, expand_group_id

    def get_group_records_for_run(self, run_id: str) -> list[RowMapping]:
        """All group_records rows for a run, ordered by group_id (export surface)."""
        with self._db.connection() as conn:
            return list(
                conn.execute(
                    select(group_records_table).where(group_records_table.c.run_id == run_id).order_by(group_records_table.c.group_id)
                )
                .mappings()
                .all()
            )

    def get_group_losses_for_run(self, run_id: str) -> list[RowMapping]:
        """All group_losses rows for a run, ordered by loss_id (export surface, WS3 writes the ledger)."""
        with self._db.connection() as conn:
            return list(
                conn.execute(select(group_losses_table).where(group_losses_table.c.run_id == run_id).order_by(group_losses_table.c.loss_id))
                .mappings()
                .all()
            )

    def record_empty_expansion(self, parent_ref: TokenRef) -> str:
        """Mint the durable group record for a zero-row expansion (spec §4.3).

        The zero-row multi-row-transform path never calls expand_token, so a
        require_all empty group previously had no durable referent. Idempotent
        per opener (uq_group_records_opener: one token opens at most one
        group): a re-driven claim returns the committed group_id. An opener
        that already opened a NON-empty group is a divergent replay — Tier-1.
        """
        self._ownership.validate_token_run_ownership(parent_ref)
        with self._db.write_connection() as conn:
            existing = conn.execute(
                select(group_records_table.c.group_id, group_records_table.c.member_count)
                .where(group_records_table.c.run_id == parent_ref.run_id)
                .where(group_records_table.c.opener_token_id == parent_ref.token_id)
            ).one_or_none()
            if existing is not None:
                if int(existing.member_count) != 0:
                    raise AuditIntegrityError(
                        f"record_empty_expansion: opener {parent_ref.token_id!r} already opened group "
                        f"{existing.group_id!r} with member_count={existing.member_count}; divergent empty-expansion replay"
                    )
                return str(existing.group_id)
            group_id = generate_id()
            result = conn.execute(
                group_records_table.insert().values(
                    run_id=parent_ref.run_id,
                    group_id=group_id,
                    kind=FrameKind.EXPAND.value,
                    opener_token_id=parent_ref.token_id,
                    member_count=0,
                    created_at=now(),
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"record_empty_expansion: INSERT affected zero rows (group_id={group_id})")
            return group_id

    def _verify_aggregation_parent_dispositions(
        self,
        conn: Connection,
        *,
        batch_id: str,
        dispositions: Sequence[AggregationParentDisposition],
    ) -> None:
        """Refuse a replay unless every batch parent terminal is exact."""
        token_ids = tuple(item.parent_ref.token_id for item in dispositions)
        rows: list[Any] = []
        for i in range(0, len(token_ids), 500):
            chunk = token_ids[i : i + 500]
            rows.extend(
                conn.execute(
                    select(
                        token_outcomes_table.c.token_id,
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                        token_outcomes_table.c.batch_id,
                        token_outcomes_table.c.error_hash,
                    )
                    .where(token_outcomes_table.c.run_id == dispositions[0].parent_ref.run_id)
                    .where(token_outcomes_table.c.token_id.in_(chunk))
                    .where(token_outcomes_table.c.completed == 1)
                ).all()
            )
        observed = {str(row.token_id): row for row in rows}
        if len(rows) != len(dispositions) or len(observed) != len(dispositions):
            raise AuditIntegrityError("batch expansion replay lacks exact terminal parent dispositions")
        for item in dispositions:
            row = observed[item.parent_ref.token_id]
            expected_batch_id = batch_id if item.path is TerminalPath.BATCH_CONSUMED else None
            if (
                row.outcome != item.outcome.value
                or row.path != item.path.value
                or row.batch_id != expected_batch_id
                or row.error_hash != item.error_hash
            ):
                raise AuditIntegrityError("batch expansion replay has divergent terminal parent dispositions")

    def _reconcile_expansion_replay(
        self,
        conn: Connection,
        *,
        parent_ref: TokenRef,
        row_id: str,
        child_data_refs: Sequence[str],
        step_in_pipeline: int | None,
        outcome: RowMapping,
        expand_group_id: str | None,
        expected_path: TerminalPath,
    ) -> tuple[list[Token], str]:
        """Return a previously committed exact expansion or refuse divergence.

        ``expand_group_id``: known-good for BATCH_CONSUMED (the batch's own
        durable claim, ``batches.expansion_group_id`` — unrelated column,
        kept); ``None`` for EXPAND_PARENT, where the group id is instead
        DERIVED from the children's persisted EXPAND frames (decision D2
        retired expected_branches_json — its count evidence is replaced by
        the group_records.member_count idempotency check below).
        """
        children = self._load_children_for_parent(conn, parent_ref=parent_ref)
        parent_path = self.load_lineage_paths(parent_ref.run_id, [parent_ref.token_id], conn=conn)[parent_ref.token_id]
        child_paths = self.load_lineage_paths(parent_ref.run_id, [child.token_id for child, _ordinal in children], conn=conn)
        if expand_group_id is None:
            expand_group_ids = {
                child_paths[child.token_id][-1].group_id
                for child, _ordinal in children
                if child_paths[child.token_id] and child_paths[child.token_id][-1].kind is FrameKind.EXPAND
            }
            expand_group_id = next(iter(expand_group_ids)) if len(expand_group_ids) == 1 else None
        exact = (
            outcome["outcome"] == TerminalOutcome.TRANSIENT.value
            and outcome["path"] == expected_path.value
            and expand_group_id is not None
            and len(children) == len(child_data_refs)
            and all(
                child.row_id == row_id
                and child.run_id == parent_ref.run_id
                and child.join_group_id is None
                and child.step_in_pipeline == step_in_pipeline
                and child.token_data_ref == expected_payload_ref
                and ordinal == expected_ordinal
                and child_paths[child.token_id]
                == _expected_child_frames(parent_path, kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=child.token_id)
                for expected_ordinal, ((child, ordinal), expected_payload_ref) in enumerate(zip(children, child_data_refs, strict=True))
            )
        )
        if not exact:
            raise AuditIntegrityError(
                f"expand_token: divergent expansion replay for parent token {parent_ref.token_id!r}; "
                "the requested payloads or persisted lineage frames do not match the committed expansion"
            )
        group_row = conn.execute(
            select(group_records_table.c.opener_token_id, group_records_table.c.member_count)
            .where(group_records_table.c.run_id == parent_ref.run_id)
            .where(group_records_table.c.group_id == expand_group_id)
        ).one_or_none()
        if group_row is None or group_row.opener_token_id != parent_ref.token_id or int(group_row.member_count) != len(children):
            raise AuditIntegrityError(
                f"expand_token: divergent expansion replay for parent token {parent_ref.token_id!r}; "
                f"group_records for group {expand_group_id!r} does not match the committed expansion "
                "(a re-drive can never mint a second group)"
            )
        if expand_group_id is None:
            # Unreachable: `exact` already required `expand_group_id is not None` above.
            # Narrowing guard only — mypy cannot follow that through `exact`.
            raise AuditIntegrityError(f"expand_token: exact replay for parent token {parent_ref.token_id!r} lost its expand_group_id")
        return [child for child, _ordinal in children], expand_group_id
