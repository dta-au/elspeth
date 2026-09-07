"""Disposition verbs: transitions out of LEASED and pending-sink terminalization.

The lease-owner-CAS ``mark_*`` verbs (BLOCKED / TERMINAL / FAILED /
PENDING_SINK), the strict post-sink terminalizers, and the crash-repair
terminalization sweep, all built on the shared ``_transition`` CAS.
Extracted from ``TokenSchedulerRepository`` (filigree elspeth-ef9c36d767).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import and_, case, select, update
from sqlalchemy.engine import Connection, RowMapping

from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import BarrierEmission, GroupLossSpec, SchedulerEventType, TokenWorkItem, TokenWorkStatus
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction, fenced_member_transaction
from elspeth.core.landscape.scheduler.events import SchedulerEventRecord, SchedulerEventStore
from elspeth.core.landscape.scheduler.fencing import fenced_write
from elspeth.core.landscape.scheduler.group_losses import record_group_losses
from elspeth.core.landscape.scheduler.payload_codec import scrubbed_row_payload_json
from elspeth.core.landscape.scheduler.work_items import (
    insert_work_items_idempotent,
    item_from_mapping,
    ready_work_item_values,
    validate_work_item_references,
)
from elspeth.core.landscape.schema import (
    pending_sink_bundle_clause,
    token_outcomes_table,
    token_work_items_table,
)


class _PendingSinkTerminalMiss(Exception):
    """Internal rollback signal for a singleton pending-sink CAS miss."""


@dataclass(frozen=True, slots=True)
class BlockedImage:
    """Column image of a BLOCKED disposition: the hold location.

    ``barrier_blocked_at`` is stamped from Landscape database time inside the
    transaction (ADR-047); the lease is cleared. Nothing here is a deadline a
    caller can supply.
    """

    queue_key: str | None
    barrier_key: str | None


@dataclass(frozen=True, slots=True)
class TerminalImage:
    """Column image of a TERMINAL or FAILED disposition: the scrubbed payload; the lease is cleared."""

    row_payload_json: str


@dataclass(frozen=True, slots=True)
class PendingSinkImage:
    """Column image of a PENDING_SINK park: the durable sink bundle, owner-attributed, not leased."""

    row_payload_json: str
    sink_name: str
    outcome: str
    path: str
    error_hash: str | None
    error_message: str | None
    lease_owner: str


DispositionImage = BlockedImage | TerminalImage | PendingSinkImage


class SchedulerDispositionRepository:
    """Lease-fenced dispositions and pending-sink terminalization."""

    _TRANSITION_EVENT_TYPES: ClassVar[dict[TokenWorkStatus, SchedulerEventType]] = {
        TokenWorkStatus.BLOCKED: SchedulerEventType.MARK_BLOCKED,
        TokenWorkStatus.TERMINAL: SchedulerEventType.MARK_TERMINAL,
        TokenWorkStatus.FAILED: SchedulerEventType.MARK_FAILED,
        TokenWorkStatus.PENDING_SINK: SchedulerEventType.MARK_PENDING_SINK,
    }

    def __init__(self, engine: Tier1Engine, *, events: SchedulerEventStore) -> None:
        self._engine = engine
        self._events = events

    def mark_blocked(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        queue_key: str | None,
        barrier_key: str | None,
        expected_lease_owner: str,
    ) -> TokenWorkItem:
        """Move an item to BLOCKED at a queue or barrier.

        Required ``member_token`` proves membership — see
        :meth:`mark_terminal`. The hold's ``barrier_blocked_at`` is Landscape
        database time read inside the transaction (ADR-047).
        """
        if queue_key is None and barrier_key is None:
            raise AuditIntegrityError(
                f"Scheduler cannot block work_item_id={work_item_id!r} without a queue_key or barrier_key; "
                "the work item would be unreleasable on resume."
            )
        return self._transition(
            work_item_id=work_item_id,
            status=TokenWorkStatus.BLOCKED,
            expected_lease_owner=expected_lease_owner,
            member_token=member_token,
            image=BlockedImage(queue_key=queue_key, barrier_key=barrier_key),
        )

    def mark_terminal(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...] = (),
    ) -> TokenWorkItem:
        """Mark a leased work item terminal.

        ``group_losses`` (spec §6.2: losses staged by the settle-member seam
        for any bound closer kind): a non-failure lossy disposition of a
        bound frame member (filter-drop / gate-discard) records its durable
        loss in the SAME transaction (record-then-notify uniformity rule).

        Required membership authority admits leaders and followers alike.
        The item CAS also binds the run and exact lease owner to that token.

        Every disposition stamps ``updated_at`` and its event's
        ``recorded_at`` from Landscape database time read inside its own
        transaction (ADR-047); no caller instant enters the scheduler.
        """
        return self._transition(
            work_item_id=work_item_id,
            status=TokenWorkStatus.TERMINAL,
            expected_lease_owner=expected_lease_owner,
            member_token=member_token,
            group_losses=group_losses,
            image=TerminalImage(row_payload_json=scrubbed_row_payload_json(work_item_id)),
        )

    def mark_terminal_with_ready_children(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        emitted_ready: Sequence[BarrierEmission],
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...] = (),
    ) -> tuple[TokenWorkItem, tuple[TokenWorkItem, ...]]:
        """Atomically enqueue child continuations and terminalize their parent.

        The parent transform result has already produced durable child tokens.
        This scheduler transaction makes their READY visibility inseparable
        from the exact parent lease disposition: an event, reference, fence,
        or insert failure rolls the complete scheduler image back.
        """
        return self._transition_with_ready_children(
            work_item_id=work_item_id,
            emitted_ready=emitted_ready,
            status=TokenWorkStatus.TERMINAL,
            expected_lease_owner=expected_lease_owner,
            group_losses=group_losses,
            member_token=member_token,
            image=TerminalImage(row_payload_json=scrubbed_row_payload_json(work_item_id)),
        )

    def mark_failed(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...] = (),
    ) -> TokenWorkItem:
        """Mark a leased work item failed after retries are exhausted.

        ``group_losses`` (spec §6.2: losses staged by the settle-member seam
        for any bound closer kind): when the failed item is a bound frame
        member, the durable loss record commits in the SAME transaction as
        this disposition (record-then-notify uniformity rule).

        Required ``member_token`` proves membership — see
        :meth:`mark_terminal`.
        """
        return self._transition(
            work_item_id=work_item_id,
            status=TokenWorkStatus.FAILED,
            expected_lease_owner=expected_lease_owner,
            member_token=member_token,
            group_losses=group_losses,
            image=TerminalImage(row_payload_json=scrubbed_row_payload_json(work_item_id)),
        )

    def mark_failed_with_ready_children(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        emitted_ready: Sequence[BarrierEmission],
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...] = (),
    ) -> tuple[TokenWorkItem, tuple[TokenWorkItem, ...]]:
        """Atomically enqueue child continuations and fail their parent."""
        return self._transition_with_ready_children(
            work_item_id=work_item_id,
            emitted_ready=emitted_ready,
            status=TokenWorkStatus.FAILED,
            expected_lease_owner=expected_lease_owner,
            group_losses=group_losses,
            member_token=member_token,
            image=TerminalImage(row_payload_json=scrubbed_row_payload_json(work_item_id)),
        )

    def mark_pending_sink(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        row_payload_json: str,
        sink_name: str,
        outcome: str,
        path: str,
        error_hash: str | None,
        error_message: str | None,
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...] = (),
    ) -> TokenWorkItem:
        """Move a claimed item to a durable sink handoff state.

        Required ``member_token`` proves membership — see
        :meth:`mark_terminal`.

        Attributed park (ADR-030 strict pending-sink terminalization): the
        parked row KEEPS ``lease_owner=expected_lease_owner`` with
        ``lease_expires_at=None`` — "parked, owner-attributed, not leased"
        (schema-legal: the lease CHECK constrains only LEASED rows). The
        post-sink terminalization (:meth:`mark_pending_sink_terminal`) then
        CASes strictly on that owner; the historical NULL park forced a
        NULL-acceptance arm there that a takeover could slip through.

        ``group_losses`` (spec §6.2: losses staged by the settle-member seam
        for any bound closer kind): a divert arm that lossy-disposes a bound
        frame member records its durable loss in the SAME transaction.
        """
        return self._transition(
            work_item_id=work_item_id,
            status=TokenWorkStatus.PENDING_SINK,
            expected_lease_owner=expected_lease_owner,
            group_losses=group_losses,
            member_token=member_token,
            require_complete_pending_sink_bundle=True,
            image=PendingSinkImage(
                row_payload_json=row_payload_json,
                sink_name=sink_name,
                outcome=outcome,
                path=path,
                error_hash=error_hash,
                error_message=error_message,
                lease_owner=expected_lease_owner,
            ),
        )

    def mark_pending_sink_with_ready_children(
        self,
        *,
        member_token: WorkerMembershipToken,
        work_item_id: str,
        emitted_ready: Sequence[BarrierEmission],
        row_payload_json: str,
        sink_name: str,
        outcome: str,
        path: str,
        error_hash: str | None,
        error_message: str | None,
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...] = (),
    ) -> tuple[TokenWorkItem, tuple[TokenWorkItem, ...]]:
        """Atomically enqueue children and durably park their parent for a sink."""
        return self._transition_with_ready_children(
            work_item_id=work_item_id,
            emitted_ready=emitted_ready,
            status=TokenWorkStatus.PENDING_SINK,
            expected_lease_owner=expected_lease_owner,
            group_losses=group_losses,
            member_token=member_token,
            require_complete_pending_sink_bundle=True,
            image=PendingSinkImage(
                row_payload_json=row_payload_json,
                sink_name=sink_name,
                outcome=outcome,
                path=path,
                error_hash=error_hash,
                error_message=error_message,
                lease_owner=expected_lease_owner,
            ),
        )

    def mark_pending_sink_terminal(
        self,
        *,
        token_id: str,
        expected_lease_owner: str,
        coordination_token: CoordinationToken,
    ) -> int:
        """Terminalize pending sink scheduler work after token outcome durability.

        ``expected_lease_owner`` is REQUIRED and the owner CAS is STRICT
        (ADR-030 §C.4 row 7): the historical NULL-owner acceptance arm is
        deleted. Every path that parks a row into PENDING_SINK now attributes
        the owner (``mark_pending_sink`` / ``complete_barrier``'s emission
        arms stamp it; ``recover_expired_leases``' reap arm deliberately
        parks NULL because the prior owner is deposed — reaped handoffs are
        always re-claimed via ``claim_pending_sink``, which overwrites the
        owner, before terminalization). A row whose owner does not match —
        including NULL — is simply not terminalized (returns 0 for the
        caller's loud invariant check). The miss rolls back the surrounding
        leader-heartbeat extension so this F-04 refusal is zero-mutation.

        ``coordination_token`` (ADR-030 §C.4 row 7, slice-4 ratchet:
        REQUIRED): the verify-and-extend leader epoch fence is the FIRST
        statement of this verb's transaction — a deposed leader cannot
        terminalize the new leader's ledger even with a matching owner.
        The epoch fence stacks on top of the owner CAS: both must pass.
        """
        predicates = [
            token_work_items_table.c.run_id == coordination_token.run_id,
            token_work_items_table.c.token_id == token_id,
            token_work_items_table.c.status.in_((TokenWorkStatus.PENDING_SINK.value, TokenWorkStatus.LEASED.value)),
            token_work_items_table.c.pending_sink_name.is_not(None),
            token_work_items_table.c.lease_owner == expected_lease_owner,
        ]
        try:
            with fenced_leader_transaction(
                self._engine,
                token=coordination_token,
                window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
                verb="mark_pending_sink_terminal",
            ) as conn:
                database_now = read_landscape_transaction_time(conn)
                rows = (
                    conn.execute(select(token_work_items_table).where(and_(*predicates)).order_by(token_work_items_table.c.work_item_id))
                    .mappings()
                    .all()
                )
                if not rows:
                    raise _PendingSinkTerminalMiss
                terminalized = 0
                if rows:
                    changed_ids = frozenset(
                        conn.execute(
                            update(token_work_items_table)
                            .where(token_work_items_table.c.work_item_id.in_(tuple(row["work_item_id"] for row in rows)))
                            .where(and_(*predicates))
                            .values(
                                status=TokenWorkStatus.TERMINAL.value,
                                row_payload_json=scrubbed_row_payload_json(token_id),
                                lease_owner=None,
                                lease_expires_at=None,
                                updated_at=database_now,
                            )
                            .returning(token_work_items_table.c.work_item_id)
                        )
                        .scalars()
                        .all()
                    )
                    if len(changed_ids) != len(rows):
                        raise _PendingSinkTerminalMiss
                    self._events.record_many(
                        conn,
                        records=[
                            SchedulerEventRecord(
                                event_type=SchedulerEventType.MARK_PENDING_SINK_TERMINAL,
                                run_id=coordination_token.run_id,
                                token_id=row["token_id"],
                                work_item_id=row["work_item_id"],
                                node_id=row["node_id"],
                                from_status=TokenWorkStatus(row["status"]),
                                to_status=TokenWorkStatus.TERMINAL,
                                from_lease_owner=row["lease_owner"],
                                to_lease_owner=None,
                                from_attempt=row["attempt"],
                                to_attempt=row["attempt"],
                                recorded_at=database_now,
                                from_lease_expires_at=row["lease_expires_at"],
                                to_lease_expires_at=None,
                                caller_owner=expected_lease_owner,
                            )
                            for row in rows
                            if row["work_item_id"] in changed_ids
                        ],
                    )
                    terminalized = len(changed_ids)
        except _PendingSinkTerminalMiss:
            return 0
        return terminalized

    def mark_pending_sink_terminal_many(
        self,
        *,
        token_ids: tuple[str, ...],
        expected_lease_owner: str,
        coordination_token: CoordinationToken,
    ) -> int:
        """Terminalize sink-bound scheduler work for a durable sink batch.

        This preserves the audit contract of one scheduler event per terminalized
        work item while avoiding one transaction and one indexed SELECT per token
        after large sink writes.

        Every requested token must resolve to exactly one complete durable sink
        bundle with the expected owner before the first mutation. Completeness
        is repeated in each update CAS so a concurrently malformed member aborts
        and rolls back the whole batch rather than returning a partial count.

        ``expected_lease_owner`` is REQUIRED and the owner CAS is STRICT
        (ADR-030 §C.4 row 7; see :meth:`mark_pending_sink_terminal` for the
        attributed-park co-change): a row whose owner does not match —
        including NULL — refuses the whole batch with
        :class:`~elspeth.contracts.errors.AuditIntegrityError`.

        ``coordination_token`` (ADR-030 §C.4 row 7, slice-4 ratchet:
        REQUIRED): the verify-and-extend leader epoch fence is the FIRST
        statement of the batch transaction; a deposed leader's batch is
        refused with :class:`RunLeadershipLostError` (and a
        ``fence_refusal`` event) before any row is touched. Mirrors the
        singleton sibling :meth:`mark_pending_sink_terminal`.
        """
        run_id = coordination_token.run_id
        requested_token_ids = token_ids
        if not requested_token_ids:
            return 0
        seen_token_ids: set[str] = set()
        for token_id in requested_token_ids:
            if token_id in seen_token_ids:
                raise AuditIntegrityError(
                    f"Scheduler pending-sink batch terminalization for run_id={run_id!r} received duplicate token_id={token_id!r}."
                )
            seen_token_ids.add(token_id)

        predicates = [
            token_work_items_table.c.run_id == coordination_token.run_id,
            token_work_items_table.c.token_id.in_(requested_token_ids),
            token_work_items_table.c.status.in_((TokenWorkStatus.PENDING_SINK.value, TokenWorkStatus.LEASED.value)),
            token_work_items_table.c.pending_sink_name.is_not(None),
        ]
        complete_bundle = pending_sink_bundle_clause()
        with fenced_leader_transaction(
            self._engine,
            token=coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="mark_pending_sink_terminal_many",
        ) as conn:
            database_now = read_landscape_transaction_time(conn)
            rows = (
                conn.execute(
                    select(token_work_items_table, complete_bundle.label("_pending_sink_bundle_complete"))
                    .where(and_(*predicates))
                    .order_by(
                        token_work_items_table.c.ingest_sequence,
                        token_work_items_table.c.step_index,
                        token_work_items_table.c.work_item_id,
                    )
                )
                .mappings()
                .all()
            )
            rows_by_token_id: dict[str, list[RowMapping]] = {}
            for row in rows:
                row_token_id = row["token_id"]
                if row_token_id not in rows_by_token_id:
                    rows_by_token_id[row_token_id] = []
                rows_by_token_id[row_token_id].append(row)
            for token_id in requested_token_ids:
                matching_rows = rows_by_token_id[token_id] if token_id in rows_by_token_id else []
                if not matching_rows:
                    raise AuditIntegrityError(
                        f"Scheduler pending-sink batch terminalization for run_id={run_id!r} is missing token_id={token_id!r}; "
                        "refusing partial terminalization."
                    )
                if len(matching_rows) != 1:
                    raise AuditIntegrityError(
                        f"Scheduler pending-sink batch terminalization for run_id={run_id!r} token_id={token_id!r} found "
                        f"{len(matching_rows)} matching rows; expected exactly one."
                    )
                matching_row = matching_rows[0]
                if not matching_row["_pending_sink_bundle_complete"]:
                    raise AuditIntegrityError(
                        f"Scheduler pending-sink batch terminalization for run_id={run_id!r} token_id={token_id!r} "
                        "refused a member without a complete durable sink bundle; refusing partial terminalization."
                    )
                row_lease_owner = matching_row["lease_owner"]
                if row_lease_owner != expected_lease_owner:
                    raise AuditIntegrityError(
                        f"Scheduler pending-sink batch terminalization for run_id={run_id!r} token_id={token_id!r} found "
                        f"lease_owner={row_lease_owner!r}; expected lease_owner={expected_lease_owner!r} "
                        "(strict owner CAS — NULL-owner acceptance removed, ADR-030)."
                    )

            terminalized = 0
            if rows:
                changed_ids = frozenset(
                    conn.execute(
                        update(token_work_items_table)
                        .where(token_work_items_table.c.work_item_id.in_(tuple(row["work_item_id"] for row in rows)))
                        .where(token_work_items_table.c.run_id == coordination_token.run_id)
                        .where(
                            token_work_items_table.c.status
                            == case({row["work_item_id"]: row["status"] for row in rows}, value=token_work_items_table.c.work_item_id)
                        )
                        .where(
                            token_work_items_table.c.token_id
                            == case({row["work_item_id"]: row["token_id"] for row in rows}, value=token_work_items_table.c.work_item_id)
                        )
                        .where(token_work_items_table.c.pending_sink_name.is_not(None))
                        .where(token_work_items_table.c.lease_owner == expected_lease_owner)
                        .where(complete_bundle)
                        .values(
                            status=TokenWorkStatus.TERMINAL.value,
                            row_payload_json=case(
                                {row["token_id"]: scrubbed_row_payload_json(row["token_id"]) for row in rows},
                                value=token_work_items_table.c.token_id,
                            ),
                            lease_owner=None,
                            lease_expires_at=None,
                            updated_at=database_now,
                        )
                        .returning(token_work_items_table.c.work_item_id)
                    )
                    .scalars()
                    .all()
                )
                if len(changed_ids) != len(rows):
                    raise AuditIntegrityError(
                        "Scheduler pending-sink batch CAS missed a complete owner-matched member; refusing partial terminalization"
                    )
                self._events.record_many(
                    conn,
                    records=[
                        SchedulerEventRecord(
                            event_type=SchedulerEventType.MARK_PENDING_SINK_TERMINAL,
                            run_id=coordination_token.run_id,
                            token_id=row["token_id"],
                            work_item_id=row["work_item_id"],
                            node_id=row["node_id"],
                            from_status=TokenWorkStatus(row["status"]),
                            to_status=TokenWorkStatus.TERMINAL,
                            from_lease_owner=row["lease_owner"],
                            to_lease_owner=None,
                            from_attempt=row["attempt"],
                            to_attempt=row["attempt"],
                            recorded_at=database_now,
                            from_lease_expires_at=row["lease_expires_at"],
                            to_lease_expires_at=None,
                            caller_owner=expected_lease_owner,
                        )
                        for row in rows
                        if row["work_item_id"] in changed_ids
                    ],
                )
                terminalized = len(changed_ids)
        return terminalized

    def terminalize_pending_sinks_with_terminal_outcomes(
        self,
        *,
        caller_owner: str,
        coordination_token: CoordinationToken,
    ) -> int:
        """Repair PENDING_SINK work whose terminal token outcome is already durable.

        A crash can land after sink outcome durability but before the scheduler
        handoff row is marked terminal. Resume must not claim and re-emit those
        rows externally; the terminal token outcome is the authoritative witness.

        ``coordination_token`` (ADR-030 §G, slice-4 ratchet: REQUIRED): this
        verb deliberately terminalizes REGARDLESS of owner — it is the
        crash-repair verb and the terminal token outcome is the witness — so
        its protection is the leader epoch fence (first statement of the
        transaction), not the owner CAS. Owner-blindness is unchanged;
        ``caller_owner`` remains the event attribution only.
        """
        terminal_outcome_exists = (
            select(token_outcomes_table.c.outcome_id)
            .where(token_outcomes_table.c.run_id == coordination_token.run_id)
            .where(token_outcomes_table.c.token_id == token_work_items_table.c.token_id)
            .where(token_outcomes_table.c.completed == 1)
            .exists()
        )
        with fenced_write(
            self._engine, coordination_token=coordination_token, verb="terminalize_pending_sinks_with_terminal_outcomes"
        ) as conn:
            database_now = read_landscape_transaction_time(conn)
            rows = (
                conn.execute(
                    select(token_work_items_table)
                    .where(token_work_items_table.c.run_id == coordination_token.run_id)
                    .where(token_work_items_table.c.status == TokenWorkStatus.PENDING_SINK.value)
                    .where(token_work_items_table.c.pending_sink_name.is_not(None))
                    .where(terminal_outcome_exists)
                    .order_by(
                        token_work_items_table.c.ingest_sequence,
                        token_work_items_table.c.step_index,
                        token_work_items_table.c.work_item_id,
                    )
                )
                .mappings()
                .all()
            )
            terminalized = 0
            if rows:
                changed_ids = frozenset(
                    conn.execute(
                        update(token_work_items_table)
                        .where(token_work_items_table.c.work_item_id.in_(tuple(row["work_item_id"] for row in rows)))
                        .where(token_work_items_table.c.run_id == coordination_token.run_id)
                        .where(token_work_items_table.c.status == TokenWorkStatus.PENDING_SINK.value)
                        .where(token_work_items_table.c.pending_sink_name.is_not(None))
                        .values(
                            status=TokenWorkStatus.TERMINAL.value,
                            row_payload_json=case(
                                {row["token_id"]: scrubbed_row_payload_json(row["token_id"]) for row in rows},
                                value=token_work_items_table.c.token_id,
                            ),
                            lease_owner=None,
                            lease_expires_at=None,
                            updated_at=database_now,
                        )
                        .returning(token_work_items_table.c.work_item_id)
                    )
                    .scalars()
                    .all()
                )
                self._events.record_many(
                    conn,
                    records=[
                        SchedulerEventRecord(
                            event_type=SchedulerEventType.MARK_PENDING_SINK_TERMINAL,
                            run_id=coordination_token.run_id,
                            token_id=row["token_id"],
                            work_item_id=row["work_item_id"],
                            node_id=row["node_id"],
                            from_status=TokenWorkStatus.PENDING_SINK,
                            to_status=TokenWorkStatus.TERMINAL,
                            from_lease_owner=row["lease_owner"],
                            to_lease_owner=None,
                            from_attempt=row["attempt"],
                            to_attempt=row["attempt"],
                            recorded_at=database_now,
                            from_lease_expires_at=row["lease_expires_at"],
                            to_lease_expires_at=None,
                            caller_owner=caller_owner,
                        )
                        for row in rows
                        if row["work_item_id"] in changed_ids
                    ],
                )
                terminalized = len(changed_ids)
        return terminalized

    def _transition(
        self,
        *,
        work_item_id: str,
        status: TokenWorkStatus,
        image: DispositionImage,
        expected_statuses: tuple[TokenWorkStatus, ...] = (TokenWorkStatus.LEASED,),
        expected_lease_owner: str | None = None,
        group_losses: tuple[GroupLossSpec, ...] = (),
        member_token: WorkerMembershipToken,
        require_complete_pending_sink_bundle: bool = False,
    ) -> TokenWorkItem:
        if expected_lease_owner != member_token.worker_id:
            raise ValueError("disposition lease owner must match membership token")
        with fenced_member_transaction(self._engine, member_token=member_token, verb="_transition") as conn:
            after = self._transition_on(
                conn,
                work_item_id=work_item_id,
                status=status,
                image=image,
                expected_statuses=expected_statuses,
                expected_lease_owner=expected_lease_owner,
                group_losses=group_losses,
                member_token=member_token,
                require_complete_pending_sink_bundle=require_complete_pending_sink_bundle,
            )
        return item_from_mapping(after)

    def _transition_with_ready_children(
        self,
        *,
        work_item_id: str,
        emitted_ready: Sequence[BarrierEmission],
        status: TokenWorkStatus,
        image: DispositionImage,
        expected_lease_owner: str,
        group_losses: tuple[GroupLossSpec, ...],
        member_token: WorkerMembershipToken,
        require_complete_pending_sink_bundle: bool = False,
    ) -> tuple[TokenWorkItem, tuple[TokenWorkItem, ...]]:
        if not emitted_ready:
            raise ValueError("atomic child disposition requires at least one READY emission")
        if expected_lease_owner != member_token.worker_id:
            raise ValueError("disposition lease owner must match membership token")
        with fenced_member_transaction(self._engine, member_token=member_token, verb="_transition_with_ready_children") as conn:
            children = [
                self._prepare_ready_emission_on(
                    conn,
                    parent_work_item_id=work_item_id,
                    run_id=member_token.run_id,
                    emission=emission,
                )
                for emission in emitted_ready
            ]
            inserted_ids = insert_work_items_idempotent(
                conn,
                values=[values for values, _event in children],
                operation=f"atomic child enqueue for parent work_item_id={work_item_id!r}",
            )
            events_by_id = {event.work_item_id: event for _values, event in children}
            self._events.record_many(conn, records=[event for identity, event in events_by_id.items() if identity in inserted_ids])
            persisted_children = (
                conn.execute(
                    select(token_work_items_table).where(
                        token_work_items_table.c.run_id == member_token.run_id,
                        token_work_items_table.c.work_item_id.in_(tuple(events_by_id)),
                    )
                )
                .mappings()
                .all()
            )
            rows_by_id = {row["work_item_id"]: row for row in persisted_children}
            child_rows = tuple(rows_by_id[event.work_item_id] for _values, event in children)
            parent_row = self._transition_on(
                conn,
                work_item_id=work_item_id,
                status=status,
                image=image,
                expected_lease_owner=expected_lease_owner,
                group_losses=group_losses,
                member_token=member_token,
                require_complete_pending_sink_bundle=require_complete_pending_sink_bundle,
            )
        return item_from_mapping(parent_row), tuple(item_from_mapping(row) for row in child_rows)

    def _prepare_ready_emission_on(
        self,
        conn: Connection,
        *,
        parent_work_item_id: str,
        run_id: str,
        emission: BarrierEmission,
    ) -> tuple[dict[str, object], SchedulerEventRecord]:
        """Insert or reconcile one child READY cursor on a caller transaction."""
        if emission.row_id is None or emission.step_index is None or emission.ingest_sequence is None:
            raise AuditIntegrityError(
                f"Atomic child enqueue for parent work_item_id={parent_work_item_id!r} token_id={emission.token_id!r} "
                "requires row_id, step_index and ingest_sequence."
            )
        validate_work_item_references(
            conn,
            run_id=run_id,
            token_id=emission.token_id,
            row_id=emission.row_id,
            ingest_sequence=emission.ingest_sequence,
            node_id=emission.node_id,
            coalesce_node_id=emission.coalesce_node_id,
        )
        # The child becomes available at Landscape database time (ADR-047):
        # claim_ready admits it against that clock, and its ENQUEUE event is
        # recorded at the same instant.
        available_at = read_landscape_transaction_time(conn)
        values = ready_work_item_values(
            run_id=run_id,
            token_id=emission.token_id,
            row_id=emission.row_id,
            node_id=emission.node_id,
            step_index=emission.step_index,
            ingest_sequence=emission.ingest_sequence,
            row_payload_json=emission.row_payload_json,
            available_at=available_at,
            attempt=emission.attempt,
            queue_key=emission.queue_key,
            barrier_key=emission.barrier_key,
            on_success_sink=emission.on_success_sink,
            join_group_id=emission.join_group_id,
            lineage_path=emission.lineage_path,
            coalesce_node_id=emission.coalesce_node_id,
            coalesce_name=emission.coalesce_name,
            row_union_name=emission.row_union_name,
            collector_name=emission.collector_name,
        )
        event = SchedulerEventRecord(
            event_type=SchedulerEventType.ENQUEUE,
            run_id=run_id,
            token_id=emission.token_id,
            work_item_id=str(values["work_item_id"]),
            node_id=emission.node_id,
            from_status=None,
            to_status=TokenWorkStatus.READY,
            from_lease_owner=None,
            to_lease_owner=None,
            from_attempt=None,
            to_attempt=emission.attempt,
            recorded_at=available_at,
        )
        return values, event

    def _transition_on(
        self,
        conn: Connection,
        *,
        work_item_id: str,
        status: TokenWorkStatus,
        image: DispositionImage,
        expected_statuses: tuple[TokenWorkStatus, ...] = (TokenWorkStatus.LEASED,),
        expected_lease_owner: str | None = None,
        group_losses: tuple[GroupLossSpec, ...] = (),
        member_token: WorkerMembershipToken,
        require_complete_pending_sink_bundle: bool = False,
    ) -> RowMapping:
        # ADR-047: one database-time read per disposition transaction stamps
        # updated_at, the hold instant and the event; the lease is always
        # cleared (a PENDING_SINK park keeps its owner with no deadline), so
        # no caller value can reach a deadline column.
        database_now = read_landscape_transaction_time(conn)
        expected_status_values = tuple(candidate.value for candidate in expected_statuses)
        expected_status_text = " or ".join(candidate.name for candidate in expected_statuses)
        predicates = [
            token_work_items_table.c.work_item_id == work_item_id,
            token_work_items_table.c.run_id == member_token.run_id,
            token_work_items_table.c.status.in_(expected_status_values),
            # TS-07 through TS-10 are transform-work dispositions.  A sink
            # handoff reclaimed by claim_pending_sink is also LEASED, but its
            # non-NULL pending_sink_name makes it the sink-redrive subtype;
            # only the dedicated pending-sink terminalizers may consume it.
            token_work_items_table.c.pending_sink_name.is_(None),
        ]
        if expected_lease_owner is not None:
            predicates.append(token_work_items_table.c.lease_owner == expected_lease_owner)
        before = (
            conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id))
            .mappings()
            .one_or_none()
        )
        next_lease_owner: str | None
        if isinstance(image, BlockedImage):
            # F1: barrier holds are restored from the journal using this
            # absolute timestamp. Queue-holds (ADR-028) get stamped too —
            # harmless; nothing reads the column on that arm, and a single
            # UPDATE shape keeps this verb the column's only writer.
            result = conn.execute(
                update(token_work_items_table)
                .where(and_(*predicates))
                .values(
                    status=status.value,
                    updated_at=database_now,
                    queue_key=image.queue_key,
                    barrier_key=image.barrier_key,
                    barrier_blocked_at=database_now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            next_lease_owner = None
        elif isinstance(image, TerminalImage):
            result = conn.execute(
                update(token_work_items_table)
                .where(and_(*predicates))
                .values(
                    status=status.value,
                    updated_at=database_now,
                    row_payload_json=image.row_payload_json,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            next_lease_owner = None
        elif isinstance(image, PendingSinkImage):
            result = conn.execute(
                update(token_work_items_table)
                .where(and_(*predicates))
                .values(
                    status=status.value,
                    updated_at=database_now,
                    row_payload_json=image.row_payload_json,
                    pending_sink_name=image.sink_name,
                    pending_outcome=image.outcome,
                    pending_path=image.path,
                    pending_error_hash=image.error_hash,
                    pending_error_message=image.error_message,
                    lease_owner=image.lease_owner,
                    lease_expires_at=None,
                )
            )
            next_lease_owner = image.lease_owner
        else:
            raise TypeError(f"disposition image must be BlockedImage, TerminalImage or PendingSinkImage, got {type(image).__name__}")
        if result.rowcount != 1:
            actual = (
                conn.execute(
                    select(
                        token_work_items_table.c.status,
                        token_work_items_table.c.lease_owner,
                        token_work_items_table.c.pending_sink_name,
                    ).where(token_work_items_table.c.work_item_id == work_item_id)
                )
                .mappings()
                .one_or_none()
            )
            if actual is None:
                actual_message = "missing"
            else:
                actual_subtype = "transform" if actual["pending_sink_name"] is None else "sink-redrive"
                actual_message = (
                    f"actual status {actual['status']}, actual subtype {actual_subtype}, actual lease_owner {actual['lease_owner']!r}"
                )
            expected_owner_message = "" if expected_lease_owner is None else f" and expected lease_owner {expected_lease_owner!r}"
            fence_message = f" under membership fence for worker {member_token.worker_id!r}"
            raise AuditIntegrityError(
                f"Scheduler transition to {status.name!r} for work_item_id={work_item_id!r} "
                f"affected {result.rowcount} rows; expected exactly 1 transform-lease row with expected status {expected_status_text}"
                f"{expected_owner_message}{fence_message}. Caller assumed ownership but the row is missing or in an "
                f"unexpected state ({actual_message})."
            )
        after = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        if require_complete_pending_sink_bundle:
            bundle_complete = conn.execute(
                select(pending_sink_bundle_clause()).where(token_work_items_table.c.work_item_id == work_item_id)
            ).scalar_one()
            if not bundle_complete:
                raise AuditIntegrityError(
                    f"Scheduler transition to PENDING_SINK for work_item_id={work_item_id!r} refused an incomplete "
                    "durable sink bundle. Required: non-empty row payload and sink name; legal outcome/path pair; "
                    "pair-specific error evidence; and a join group for COALESCED."
                )
        if before is None:
            raise AuditIntegrityError(
                f"Scheduler transition to {status.name!r} for work_item_id={work_item_id!r} "
                "updated a row that could not be read before the write; audit transition history cannot be proven."
            )
        next_attempt = before["attempt"]
        if type(next_attempt) is not int:
            raise AuditIntegrityError(
                f"Scheduler transition to {status.name!r} for work_item_id={work_item_id!r} "
                f"produced invalid attempt type {type(next_attempt).__name__}; audit transition history cannot be proven."
            )
        self._events.record(
            conn,
            event_type=self._TRANSITION_EVENT_TYPES[status],
            run_id=before["run_id"],
            token_id=before["token_id"],
            work_item_id=work_item_id,
            node_id=before["node_id"],
            from_status=TokenWorkStatus(before["status"]),
            to_status=status,
            from_lease_owner=before["lease_owner"],
            to_lease_owner=next_lease_owner,
            from_attempt=before["attempt"],
            to_attempt=next_attempt,
            recorded_at=database_now,
            from_lease_expires_at=before["lease_expires_at"],
            to_lease_expires_at=None,
            caller_owner=expected_lease_owner,
        )
        record_group_losses(conn, run_id=member_token.run_id, specs=group_losses, recorded_by=member_token.worker_id, now=database_now)

        return after
