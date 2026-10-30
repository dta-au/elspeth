"""Retained live P4 custody around the existing redaction/publication owner.

This owner holds DATA and the actual physical outcomes. The session SQL owner
rechecks the current fence, exact prepared batch and original issue ordering.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer._compose_loop_carriers import _PersistOutcome
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, _read_current_env_names
from elspeth.web.composer.continuation_p4_batch import encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value, encode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    _capture_owners,
    _registered_read,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource, RequiredWorkSubphase, RequiredWorkTicket
from elspeth.web.sessions._persist_payload import AuditOutcome
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import ContinuationOwnerCommit
from elspeth.web.sessions.continuation_p4 import P4WriterObservation
from elspeth.web.sessions.continuation_p4_application_store import CommittedContinuationP4, StoredP4Application
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource


@dataclass(slots=True)
class ContinuationLiveP4:
    authority: ComposerAsyncOperationAuthority
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    work: RequiredWorkBinding
    data: RestoredBatchData
    runtime: RestoredContinuationRuntime
    withheld_replies: tuple[WithheldReply, ...] = ()
    commit: ContinuationOwnerCommit | None = field(default=None, init=False)
    prepared: PreparedP4Value | None = field(default=None, init=False)
    preparation: RequiredSQLFinishOnce[ContinuationOwnerCommit] | None = field(default=None, init=False)
    physical: RequiredSQLFinishOnce[CommittedContinuationP4] | None = field(default=None, init=False)
    committed_outcome: CommittedContinuationP4 | None = field(default=None, init=False)
    publication: _PersistOutcome | None = field(default=None, init=False)
    captured: CapturedContinuationRuntime | None = field(default=None, init=False)
    application_sql: RequiredSQLFinishOnce[StoredP4Application] | None = field(default=None, init=False)
    application: StoredP4Application | None = field(default=None, init=False)
    writer_observation: P4WriterObservation | None = field(default=None, init=False)
    _checkpoint_ticket: RequiredWorkTicket | None = field(default=None, init=False, repr=False)
    deferred_failures: list[BaseException] = field(default_factory=list, init=False)
    deferred_cancellations: list[asyncio.CancelledError] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not ComposerAsyncOperationAuthority
            or type(self.selected) is not SelectedContinuationState
            or type(self.inventory) is not CurrentContinuationEnvInventory
            or type(self.work) is not RequiredWorkBinding
            or type(self.data) is not RestoredBatchData
            or type(self.runtime) is not RestoredContinuationRuntime
            or self.runtime.graph.batch is not self.data
            or self.runtime.bindings.required_work is not self.work
            or self.work.running is None
            or self.inventory.running is not self.work.running
        ):
            raise AuditIntegrityError("live P4 lost its actual current phase owners")
        self.work.validate_context(self.inventory.running.session_operation_context)

    def _retain_cancellations[T](self, outcome: RequiredSQLFinishOnce[T]) -> None:
        for cancellation in outcome.deferred_cancellations:
            if all(cancellation is not previous for previous in self.deferred_cancellations):
                self.deferred_cancellations.append(cancellation)

    async def prepare(self, prepared: PreparedP4Value) -> ContinuationOwnerCommit:
        if self.preparation is not None or self.prepared is not None:
            raise AuditIntegrityError("live P4 preparation cannot replace a retained physical outcome")
        names = await _read_current_env_names(
            self.runtime.materialization.service._advisor_checkpoint, self.selected.stack.logical.session_id
        )
        self.inventory = CurrentContinuationEnvInventory(self.inventory.running, self.inventory.selection, names)
        pending = replace(
            self.data.batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared, env_ref_names=self.inventory.names)
        )
        chunk = encode_p4_pending_batch_chunk(
            batch=pending, accumulator=self.data.accumulator, calls=self.data.calls, env_ref_names=self.inventory.names
        )
        members = prepared_p4_members(prepared, env_ref_names=self.inventory.names)
        recorded = self.runtime.live.recorder.invocations
        linked = self.runtime.live.recorder.visible_tool_events
        for expected, row in zip(members.invocations, members.redacted_rows, strict=True):
            actual = tuple(invocation for invocation in recorded if invocation.tool_call_id == expected.tool_call_id)
            if len(actual) != 1 or actual[0] != expected:
                raise AuditIntegrityError("P4 redaction lost its actual recorded invocation")
            if row.visible_event is not None:
                previous = tuple(event for invocation, event in linked if invocation is actual[0])
                if previous and previous != (row.visible_event,):
                    raise AuditIntegrityError("P4 redaction replaced its actual visible event")
                if not previous:
                    self.runtime.live.recorder.record_visible_tool_event(actual[0], row.visible_event)
        pending_data = RestoredBatchData(pending, self.data.accumulator, self.data.calls)
        runtime = replace(self.runtime, graph=replace(self.runtime.graph, batch=pending_data))

        def read_source() -> ContinuationRuntimeSource:
            try:
                return self.authority.read_continuation_runtime(self.inventory.running, selected=self.selected, inventory=self.inventory)
            except ComposerOperationCancelledDuringTurn:
                # A real durable Stop selects the separate read facet. It must
                # prove immutable pre-Stop issue ordering again; no caller flag
                # or old frontier grants the pending P4 write.
                return self.authority.read_continuation_owed_batch_runtime(
                    self.inventory.running, selected=self.selected, inventory=self.inventory
                )

        def capture() -> CapturedContinuationRuntime:
            source = read_source()
            captured = _capture_owners(runtime, source=source, inventory=self.inventory)
            actual = read_source()
            if actual.selected != source.selected or actual.bootstrap != source.bootstrap or actual.evidence != source.evidence:
                raise AuditIntegrityError("P4 capture changed its complete current source during retention")
            self.captured = captured
            return captured

        captured = await _registered_read(self.work, CapturedContinuationRuntime, capture)
        top = captured.stack.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("P4 capture lost its actual completed batch frame")
        sql, projection = self.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
        )
        try:
            outcome = await run_required_sql_finish_once(
                sql,
                self.authority.prepare_continuation_p4,
                self.inventory.running,
                expected_checkpoint_id=self.selected.checkpoint_id,
                expected_cursor=self.selected.stack.cursor,
                pending_batch=chunk,
                env_ref_names=self.inventory.names,
                captured=captured,
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        self.preparation = outcome
        self._retain_cancellations(outcome)
        try:
            self.work.coordinator.validate_continuation_sql_outcome(
                ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        if type(outcome) is RequiredSQLRaised:
            projection.complete_without_submission()
            self.raise_after_publication(outcome.error)
        if type(outcome) is not RequiredSQLReturned:
            projection.complete_without_submission()
            raise AuditIntegrityError("live P4 preparation returned another physical outcome")
        projection.begin_projection()
        try:
            if type(outcome.value) is not ContinuationOwnerCommit or outcome.value.running is not self.inventory.running:
                raise AuditIntegrityError("live P4 preparation changed its actual current owner")
            self.commit, self.prepared = outcome.value, prepared
            self.writer_observation = P4WriterObservation(outcome.value, prepared)
            self.data.batch.phase, self.data.batch.p4_input_json = pending.phase, pending.p4_input_json
        except BaseException as error:
            projection.complete_owned(error)
            raise
        projection.complete_owned()
        return outcome.value

    def bind_checkpoint_ticket(self, ticket: RequiredWorkTicket) -> None:
        """Capture this P4's actual reservation before its physical invocation.

        This pointer grants no write. The service and transaction still prove
        source/current authority, and acceptance separately proves its Future.
        """
        coordinator = self.work.coordinator
        with coordinator._lock:
            if self._checkpoint_ticket is not None or self.physical is not None:
                raise AuditIntegrityError("live P4 checkpoint ticket is already bound or observed")
            coordinator._registered(ticket, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
            running = self.inventory.running
            self.work.validate_context(running.session_operation_context)
            if (
                self.work.running is not running
                or self.commit is None
                or self.commit.running is not running
                or self.prepared is None
                or self.writer_observation is None
                or self.writer_observation.commit is not self.commit
                or self.writer_observation.prepared is not self.prepared
                or ticket.key.authority.context != running.session_operation_context
                or ticket.key.authority.durable_operation_id != running.claim.operation_id
                or ticket.key.authority.claim_attempt != running.claim.attempt
                or ticket.key.transition_ordinal != self.work.transition_ordinal
                or ticket.key.semantic_ordinal != self.work.semantic_ordinal
                or ticket.key.subphase is not RequiredWorkSubphase.SQL_INITIAL
                or ticket.key.sql_attempt_ordinal != 0
            ):
                raise AuditIntegrityError("live P4 checkpoint ticket crossed its actual prepared scope")
            with ticket._lock:
                if (
                    ticket._complete
                    or ticket._future is not None
                    or ticket._submission_unknown
                    or ticket._projection_started
                    or ticket._finish_once_handoff is not None
                ):
                    raise AuditIntegrityError("live P4 checkpoint binding must precede physical submission")
                self._checkpoint_ticket = ticket

    def accept_physical(self, ticket: RequiredWorkTicket, outcome: RequiredSQLFinishOnce[CommittedContinuationP4]) -> AuditOutcome:
        with self.work.coordinator._lock:
            if self.physical is not None:
                raise AuditIntegrityError("live P4 cannot replace its retained physical SQL result")
            self.physical = outcome
            self._retain_cancellations(outcome)
            if self.writer_observation is not None:
                self.deferred_failures.extend(self.writer_observation.prior_errors)
            if self.writer_observation is not None and self.writer_observation.original_error is not None:
                self.deferred_failures.append(self.writer_observation.original_error)
            if self._checkpoint_ticket is None or ticket is not self._checkpoint_ticket:
                raise AuditIntegrityError("live P4 replaced its actual bound checkpoint ticket")
            self.work.coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=outcome)
        if type(outcome) is RequiredSQLRaised:
            self.raise_after_publication(outcome.error)
        if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not CommittedContinuationP4:
            raise AuditIntegrityError("live P4 returned a foreign physical publication")
        observed = self.writer_observation
        if (
            observed is None
            or not observed.committed
            or outcome.value.audit_outcome is not observed.candidate
            or outcome.value.application is not observed.application_candidate
        ):
            raise AuditIntegrityError("live P4 replaced its actual committing owner's outputs")
        self.committed_outcome = outcome.value
        return outcome.value.audit_outcome

    def raise_after_publication(self, original: BaseException | None = None) -> None:
        errors: list[BaseException] = [] if original is None else [original]
        errors.extend(failure for failure in self.deferred_failures if failure is not original)
        errors.extend(cancellation for cancellation in self.deferred_cancellations if cancellation is not original)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Live P4 retained outcomes", errors)
