"""Queue intake for scheduler work items: enqueue verbs and fenced ingest.

Persisting READY continuations (plain, claimed-in-transaction, and the
composed fenced leader INGEST). Depends on the lease repository for the
in-transaction claim CAS. Extracted from ``TokenSchedulerRepository``
(filigree elspeth-ef9c36d767).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError, RunWorkerEvictedError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import SchedulerEventType, SourceIngestSpec, TokenWorkItem, TokenWorkStatus
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction, fenced_member_transaction
from elspeth.core.landscape.scheduler.events import SchedulerEventStore
from elspeth.core.landscape.scheduler.leases import SchedulerLeaseRepository
from elspeth.core.landscape.scheduler.work_items import (
    insert_work_item_idempotent,
    item_from_mapping,
    ready_work_item_values,
    validate_work_item_references,
)
from elspeth.core.landscape.scheduler.work_items import (
    work_item_id as make_work_item_id,
)
from elspeth.core.landscape.schema import active_worker_fence_clause, token_work_items_table

if TYPE_CHECKING:
    from elspeth.contracts.audit import Row, Token
    from elspeth.core.landscape.data_flow_repository import DataFlowRepository
    from elspeth.core.landscape.execution_repository import ExecutionRepository


class SchedulerQueueRepository:
    """Persists READY token continuations into the scheduler queue."""

    def __init__(self, engine: Tier1Engine, *, events: SchedulerEventStore, leases: SchedulerLeaseRepository) -> None:
        self._engine = engine
        self._events = events
        self._leases = leases

    def enqueue_ready(
        self,
        *,
        member_token: WorkerMembershipToken,
        token_id: str,
        row_id: str,
        node_id: str | None,
        step_index: int,
        ingest_sequence: int,
        row_payload_json: str,
        attempt: int = 1,
        queue_key: str | None = None,
        barrier_key: str | None = None,
        on_success_sink: str | None = None,
        join_group_id: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
        coalesce_node_id: str | None = None,
        coalesce_name: str | None = None,
        row_union_name: str | None = None,
        collector_name: str | None = None,
    ) -> TokenWorkItem:
        """Persist a READY token continuation.

        Token lineage and coalesce cursor fields are durable resume metadata:
        workers that claim this row later must be able to rebuild the same
        ``TokenInfo`` and ``WorkItem`` without any live in-memory queue state.

        Membership authority is required even on an idempotent replay.
        """
        run_id = member_token.run_id
        work_item_id = make_work_item_id(run_id, token_id, node_id, attempt)
        with fenced_member_transaction(self._engine, member_token=member_token, verb="enqueue_ready") as conn:
            # The row becomes available at Landscape database time (ADR-047):
            # claim_ready admits it against that same clock, so a caller clock
            # — whole seconds behind or microseconds ahead of the database —
            # can neither park the row nor make it claimable early.
            available_at = read_landscape_transaction_time(conn)
            values = ready_work_item_values(
                run_id=run_id,
                token_id=token_id,
                row_id=row_id,
                node_id=node_id,
                step_index=step_index,
                ingest_sequence=ingest_sequence,
                row_payload_json=row_payload_json,
                available_at=available_at,
                attempt=attempt,
                queue_key=queue_key,
                barrier_key=barrier_key,
                on_success_sink=on_success_sink,
                join_group_id=join_group_id,
                lineage_path=lineage_path,
                coalesce_node_id=coalesce_node_id,
                coalesce_name=coalesce_name,
                row_union_name=row_union_name,
                collector_name=collector_name,
            )
            validate_work_item_references(
                conn,
                run_id=run_id,
                token_id=token_id,
                row_id=row_id,
                ingest_sequence=ingest_sequence,
                node_id=node_id,
                coalesce_node_id=coalesce_node_id,
            )
            inserted = insert_work_item_idempotent(conn, values=values, operation="enqueue READY scheduler work")
            if inserted:
                self._events.record(
                    conn,
                    event_type=SchedulerEventType.ENQUEUE,
                    run_id=run_id,
                    token_id=token_id,
                    work_item_id=work_item_id,
                    node_id=node_id,
                    from_status=None,
                    to_status=TokenWorkStatus.READY,
                    from_lease_owner=None,
                    to_lease_owner=None,
                    from_attempt=None,
                    to_attempt=attempt,
                    recorded_at=available_at,
                )
            row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        return item_from_mapping(row)

    def enqueue_ready_claimed(
        self,
        *,
        member_token: WorkerMembershipToken,
        token_id: str,
        row_id: str,
        node_id: str | None,
        step_index: int,
        ingest_sequence: int,
        row_payload_json: str,
        lease_owner: str,
        lease_seconds: int,
        attempt: int = 1,
        queue_key: str | None = None,
        barrier_key: str | None = None,
        on_success_sink: str | None = None,
        join_group_id: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
        coalesce_node_id: str | None = None,
        coalesce_name: str | None = None,
        row_union_name: str | None = None,
        collector_name: str | None = None,
    ) -> TokenWorkItem:
        """Persist and claim READY work for an active registered worker.

        ``lease_owner`` is also the durable ``run_workers`` identity.  The
        membership fence runs before any payload mutation, then rides the
        claim UPDATE CAS so eviction between the entry check and claim rolls
        back the INSERT and both scheduler events.

        """
        return self._enqueue_ready_claimed(
            member_token=member_token,
            token_id=token_id,
            row_id=row_id,
            node_id=node_id,
            step_index=step_index,
            ingest_sequence=ingest_sequence,
            row_payload_json=row_payload_json,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            attempt=attempt,
            queue_key=queue_key,
            barrier_key=barrier_key,
            on_success_sink=on_success_sink,
            join_group_id=join_group_id,
            lineage_path=lineage_path,
            coalesce_node_id=coalesce_node_id,
            coalesce_name=coalesce_name,
            row_union_name=row_union_name,
            collector_name=collector_name,
        )

    def _enqueue_ready_claimed(
        self,
        *,
        member_token: WorkerMembershipToken,
        token_id: str,
        row_id: str,
        node_id: str | None,
        step_index: int,
        ingest_sequence: int,
        row_payload_json: str,
        lease_owner: str,
        lease_seconds: int,
        attempt: int,
        queue_key: str | None,
        barrier_key: str | None,
        on_success_sink: str | None,
        join_group_id: str | None,
        coalesce_node_id: str | None,
        coalesce_name: str | None,
        row_union_name: str | None = None,
        collector_name: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
    ) -> TokenWorkItem:
        run_id = member_token.run_id
        if lease_owner != member_token.worker_id:
            raise ValueError("enqueue lease owner must match membership token")
        with fenced_member_transaction(self._engine, member_token=member_token, verb="enqueue_ready_claimed") as conn:
            row = self.enqueue_ready_claimed_on(
                conn,
                run_id=run_id,
                token_id=token_id,
                row_id=row_id,
                node_id=node_id,
                step_index=step_index,
                ingest_sequence=ingest_sequence,
                row_payload_json=row_payload_json,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                attempt=attempt,
                queue_key=queue_key,
                barrier_key=barrier_key,
                on_success_sink=on_success_sink,
                join_group_id=join_group_id,
                lineage_path=lineage_path,
                coalesce_node_id=coalesce_node_id,
                coalesce_name=coalesce_name,
                row_union_name=row_union_name,
                collector_name=collector_name,
                worker_id=member_token.worker_id,
            )
        return item_from_mapping(row)

    def enqueue_ready_claimed_on(
        self,
        conn: Connection,
        *,
        run_id: str,
        token_id: str,
        row_id: str,
        node_id: str | None,
        step_index: int,
        ingest_sequence: int,
        row_payload_json: str,
        lease_owner: str,
        lease_seconds: int,
        attempt: int = 1,
        queue_key: str | None = None,
        barrier_key: str | None = None,
        on_success_sink: str | None = None,
        join_group_id: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
        coalesce_node_id: str | None = None,
        coalesce_name: str | None = None,
        row_union_name: str | None = None,
        collector_name: str | None = None,
        worker_id: str | None = None,
    ) -> RowMapping:
        """Connection-accepting enqueue-and-claim: composes into the caller's transaction.

        Extracted from :meth:`enqueue_ready_claimed` so the fenced ingest verb
        (:meth:`ingest_row_with_initial_claim`, ADR-030 §C.4 row 9) can
        compose the reference validation + idempotent insert + ENQUEUE/CLAIM
        events onto ONE connection with the rows/tokens inserts.  Standalone
        production callers pass ``worker_id``; the strict membership check is
        then the first database statement in this method and the claim UPDATE
        rechecks membership. ``None`` is reserved for ingest, whose
        leader-epoch CAS is the outer fence.
        """
        work_item_id = make_work_item_id(run_id, token_id, node_id, attempt)
        # Available at the caller transaction's database time (ADR-047); the
        # claim CAS below reads the same clock on the same connection.
        available_at = read_landscape_transaction_time(conn)
        values = ready_work_item_values(
            run_id=run_id,
            token_id=token_id,
            row_id=row_id,
            node_id=node_id,
            step_index=step_index,
            ingest_sequence=ingest_sequence,
            row_payload_json=row_payload_json,
            available_at=available_at,
            attempt=attempt,
            queue_key=queue_key,
            barrier_key=barrier_key,
            on_success_sink=on_success_sink,
            join_group_id=join_group_id,
            lineage_path=lineage_path,
            coalesce_node_id=coalesce_node_id,
            coalesce_name=coalesce_name,
            row_union_name=row_union_name,
            collector_name=collector_name,
        )
        if worker_id is not None:
            fence_holds = conn.execute(select(active_worker_fence_clause(worker_id=worker_id, run_id=run_id))).scalar()
            if not fence_holds:
                raise RunWorkerEvictedError(worker_id=worker_id, run_id=run_id)
        validate_work_item_references(
            conn,
            run_id=run_id,
            token_id=token_id,
            row_id=row_id,
            ingest_sequence=ingest_sequence,
            node_id=node_id,
            coalesce_node_id=coalesce_node_id,
        )
        inserted = insert_work_item_idempotent(conn, values=values, operation="enqueue and claim READY scheduler work")
        if inserted:
            self._events.record(
                conn,
                event_type=SchedulerEventType.ENQUEUE,
                run_id=run_id,
                token_id=token_id,
                work_item_id=work_item_id,
                node_id=node_id,
                from_status=None,
                to_status=TokenWorkStatus.READY,
                from_lease_owner=None,
                to_lease_owner=None,
                from_attempt=None,
                to_attempt=attempt,
                recorded_at=available_at,
            )
        row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        if row["status"] == TokenWorkStatus.READY.value:
            claimed = self._leases.claim_ready_row(
                conn,
                row=row,
                run_id=run_id,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                strict_membership_fenced=worker_id is not None,
            )
            if claimed is not None:
                row = claimed
        return row

    def ingest_row_with_initial_claim(
        self,
        *,
        coordination_token: CoordinationToken,
        source: SourceIngestSpec,
        data_flow: DataFlowRepository,
        execution: ExecutionRepository,
        node_id: str | None,
        step_index: int,
        row_payload_json: str,
        lease_owner: str,
        lease_seconds: int,
        queue_key: str | None = None,
        barrier_key: str | None = None,
        on_success_sink: str | None = None,
        join_group_id: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
        coalesce_node_id: str | None = None,
        coalesce_name: str | None = None,
        row_union_name: str | None = None,
        collector_name: str | None = None,
    ) -> tuple[Row, Token, TokenWorkItem]:
        """Fenced leader INGEST (ADR-030 §C.4 row 9): one IMMEDIATE transaction.

        Composes (1) the verify-and-extend epoch fence, (2) the ``rows`` +
        ``tokens`` inserts through the owned data-flow repository, (3) the
        source completion state through the owned execution repository,
        and (4) the initial enqueue-and-claim — on ONE connection. A stale
        epoch refuses the WHOLE ingest: the rows insert rolls back with
        everything else, so a deposed leader woken mid-ingest leaves no
        orphan ``rows`` row (crash-walk step 8). The UNIQUE
        ``(run_id, ingest_sequence)`` constraint becomes a true backstop.

        ``coordination_token`` is REQUIRED — this verb has no legacy callers.
        Raises :class:`~elspeth.contracts.errors.RunLeadershipLostError` on a
        fence miss (``fence_refusal`` evented on a fresh connection).

        Ingested work is available immediately, and "immediately" is Landscape
        database time read inside the fenced transaction (ADR-047), not the
        leader's process clock. ``available_at`` is compared against that same
        clock by ``claim_ready``, so a leader whose clock ran fast can no
        longer enqueue work that its own next claim refuses as not-yet-due.
        """
        # Execution's source-recovery component imports scheduler codecs;
        # defer nominal dependency imports until their modules are initialized.
        from elspeth.core.landscape.data_flow_repository import DataFlowRepository
        from elspeth.core.landscape.execution_repository import ExecutionRepository

        if type(source) is not SourceIngestSpec:
            raise TypeError("scheduler ingest requires a SourceIngestSpec")
        if type(data_flow) is not DataFlowRepository:
            raise TypeError("scheduler ingest requires an exact DataFlowRepository")
        if type(execution) is not ExecutionRepository:
            raise TypeError("scheduler ingest requires an exact ExecutionRepository")
        if not isinstance(coordination_token, CoordinationToken):
            raise TypeError("scheduler ingest requires a CoordinationToken")
        if lease_owner != coordination_token.worker_id:
            raise ValueError("ingest lease owner must match coordination token")
        run_id = coordination_token.run_id
        with fenced_leader_transaction(
            self._engine,
            token=coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="ingest_row_with_initial_claim",
        ) as conn:
            row_record, token_record = data_flow.insert_row_with_token_on(
                conn,
                coordination_token=coordination_token,
                source_node_id=source.source_node_id,
                row_index=source.row_index,
                data=source.data,
                source_row_index=source.source_row_index,
                ingest_sequence=source.ingest_sequence,
                row_id=source.row_id,
                token_id=source.token_id,
            )
            if row_record.row_id != source.row_id or token_record.token_id != source.token_id:
                raise AuditIntegrityError(
                    f"Fenced ingest for run_id={run_id!r} returned row/token identities that differ from its SourceIngestSpec"
                )
            execution.record_completed_node_state_on(
                conn,
                coordination_token=coordination_token,
                token_id=source.token_id,
                node_id=source.source_node_id,
                step_index=0,
                input_data=source.data,
                output_data=source.data,
                duration_ms=0,
            )
            scheduled = self.enqueue_ready_claimed_on(
                conn,
                run_id=run_id,
                token_id=source.token_id,
                row_id=source.row_id,
                node_id=node_id,
                step_index=step_index,
                ingest_sequence=source.ingest_sequence,
                row_payload_json=row_payload_json,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                queue_key=queue_key,
                barrier_key=barrier_key,
                on_success_sink=on_success_sink,
                join_group_id=join_group_id,
                lineage_path=lineage_path,
                coalesce_node_id=coalesce_node_id,
                coalesce_name=coalesce_name,
                row_union_name=row_union_name,
                collector_name=collector_name,
            )
        return row_record, token_record, item_from_mapping(scheduled)
