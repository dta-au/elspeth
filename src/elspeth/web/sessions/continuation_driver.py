"""The live V2 owner: real phase actions, Stop and verified disposition.

Selected DATA never replaces current SQL authority. Every action enters its
source owner again; every disposition uses the existing transaction body.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, EndFrame, InitializeFrame
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation, SegmentFit, admit_action
from elspeth.web.composer.continuation_composition_initialization import initialize_continuation_composition
from elspeth.web.composer.continuation_control_transitions import StoredContinuationControlIntent, read_pending_control_on_connection
from elspeth.web.composer.continuation_env_inventory import read_disposition_env_names
from elspeth.web.composer.continuation_initialization import _registered, adopt_recompose_original, prepare_pretitle_bootstrap
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_next_action import (
    _has_committed_primary,
    _is_first_compose_iteration,
    next_continuation_action_policy,
)
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_title_application import apply_current_title_success
from elspeth.web.composer.continuation_title_provider import (
    prepare_current_title_provider,
    run_prepared_title_provider,
    settle_current_title_result,
)
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer_watch_reads import ComposerOperationWatchReader
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkCoordinator, RequiredWorkRole, RequiredWorkSource
from elspeth.web.sessions.composer_app_services import ComposerAppServices
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error, request_cancelled_error, worker_lost_error
from elspeth.web.sessions.composer_operations import (
    COMPOSER_CANCEL_REQUESTED,
    COMPOSER_LEASE_LOST,
    COMPOSER_SHUTDOWN,
    ComposerOperationCancelledDuringTurn,
    ComposerOperationFenceLost,
    ComposerOperationRecord,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt, ComposerSegmentYieldReceipt, StoredContinuationSnapshot
from elspeth.web.sessions.continuation_end import enter_continuation_end, primary_issued_no_tools, review_continuation_end
from elspeth.web.sessions.continuation_ingress import ContinuationIngressCheckpoint
from elspeth.web.sessions.continuation_next_iteration import advance_continuation_iteration, is_applied_batch_cut
from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt
from elspeth.web.sessions.continuation_tool_failure import ContinuationToolFailureRead, RetainedToolFailureSource
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.schemas import SendMessageRequest

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
    from elspeth.web.sessions.continuation_tool_failure import CommittedContinuationFailure


@dataclass(frozen=True, slots=True)
class ContinuationSegmentYielded:
    receipt: ComposerSegmentYieldReceipt


@dataclass(frozen=True, slots=True)
class ContinuationSegmentHandedOver:
    receipt: ComposerSegmentRecoveryReceipt


@dataclass(frozen=True, slots=True)
class ContinuationSegmentSettled:
    terminal: ComposerOperationRecord


type ContinuationSegmentOutcome = ContinuationSegmentYielded | ContinuationSegmentHandedOver | ContinuationSegmentSettled


class _SegmentDriver:
    def __init__(
        self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease
    ):
        if type(services.composer_service) is not ComposerServiceImpl or type(lease.required_work) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("V2 driver requires its actual Composer and required-work owners")
        if lease.context is not running.session_operation_context:
            raise AuditIntegrityError("V2 driver changed its exact started context")
        self.worker, self.services, self.running, self.lease = worker, services, running, lease
        self.service = services.composer_service
        self.authority = services.composer_operation_authority
        self.work = RequiredWorkBinding(lease.required_work, 0, 0, RequiredWorkRole.TURN, running)
        self.provider = ProviderInvocationOwner(service=services.session_service, required_work=self.work)
        self.phases = ContinuationLivePhases(self.service, self.authority, self.work, self.provider, lease)
        sql_deadline = lease.required_work.sql_deadline_owner
        if sql_deadline is None:
            raise AuditIntegrityError("V2 driver lost its actual SQL timing owner")
        self.sql_deadline = sql_deadline
        self._consumed_native: list[BaseException] = []
        self._committed_failure: CommittedContinuationFailure | None = None

    def join_failure(self, error: BaseException, *additional: BaseException) -> BaseException:
        from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
        from elspeth.web.required_sql_outcomes import RequiredSQLRaised

        values = list(self._consumed_native)
        custody = self.phases.failure_custody
        if custody is not None:
            if custody.original is not None:
                values.append(custody.original)
            if custody.wrapper is not None:
                values.append(custody.wrapper)
            values.extend(custody.errors)
            if custody.observation.feedback is not None and (custody.original is not None or custody.errors):
                values.append(custody.observation.feedback)
        observation = None if self._committed_failure is None else self._committed_failure.observation
        while observation is not None:
            values.extend(observation.original_errors)
            values.extend(observation.deferred_cancellations)
            for actual in observation.physical:
                if type(actual) is RequiredSQLRaised:
                    values.append(actual.error)
                values.extend(actual.deferred_cancellations)
            observation = observation.prior
        values.extend((*additional, error))
        return combine_failure_objects(values[0], *values[1:])

    async def record(self) -> ComposerOperationRecord:
        def read_record() -> ComposerOperationRecord:
            record = self.authority.get_for_turn(session_id=self.running.claim.session_id, operation_id=self.running.claim.operation_id)
            if record is None:
                raise ComposerOperationFenceLost(
                    session_id=self.running.claim.session_id,
                    operation_id=self.running.claim.operation_id,
                    attempt=self.running.claim.attempt,
                )
            return record

        record = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
            operation=read_record,
            expected=ComposerOperationRecord,
        )
        fence = self.running.session_operation_context.fence
        if (
            record.attempt,
            record.current_segment_sequence,
            record.ownership_generation,
            record.session_operation_id,
            record.session_operation_epoch,
        ) != (
            self.running.claim.attempt,
            self.running.claim.segment_sequence,
            self.running.claim.ownership_generation,
            fence.operation_id,
            fence.operation_epoch,
        ):
            raise ComposerOperationFenceLost(
                session_id=record.session_id, operation_id=record.operation_id, attempt=self.running.claim.attempt
            )
        return record

    async def selected(self) -> tuple[StoredContinuationSnapshot, SelectedContinuationState]:
        stored = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=partial(self.authority.load_continuation_snapshot, self.running),
            expected=StoredContinuationSnapshot,
        )
        selected = await load_selected_continuation(
            authority=self.authority, running=self.running, selected=stored, required_work=self.work
        )
        return stored, selected

    async def settle(self, record: ComposerOperationRecord, original: BaseException | None = None) -> ContinuationSegmentSettled:
        from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
        from elspeth.web.sessions.composer_async_worker import _StartedSettlement, _terminal_completion_unknown

        if original is not None and not any(original is item for item in self._consumed_native):
            self._consumed_native.append(original)
        self.sql_deadline.begin_settlement()
        try:
            names = await read_disposition_env_names(
                advisor=self.service._advisor_checkpoint, authority=self.authority, required_work=self.work
            )
            failure_read = await self.read_failure(names)
        except BaseException as error:
            combined = self.join_failure(error)
            if combined is error:
                raise
            raise combined from combined.__cause__
        source, custody = failure_read.source, self.phases.failure_custody
        p4 = self.phases.p4
        p4_writer_observation = None if p4 is None else p4.writer_observation
        from elspeth.web.sessions.continuation_p4 import terminal_service_failure
        from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application, StoredTerminalP4Preparation
        from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

        if type(source) is StoredTerminalP4Preparation:
            unresolved = ComposerTerminalSQLCompletionUnknown("terminal P4 preparation has no actual committed result")
            combined = self.join_failure(unresolved)
            raise combined from combined.__cause__
        if type(source) is StoredTerminalP4Application:
            failure = (
                request_cancelled_error(request_id=record.request_id)
                if record.cancel_requested_at is not None
                else project_composer_operation_error(original, request_id=record.request_id)
                if original is not None
                else terminal_service_failure(source, request_id=record.request_id)
            )
            terminal = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=partial(
                    self.authority.settle_continuation_owner,
                    self.running,
                    outcome=failure,
                    env_ref_names=names,
                    deadline=self.worker.drain_deadline,
                ),
                expected=ComposerOperationRecord,
            )
            await self.worker._verify_terminal_before_release(
                self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal)
            )
            return ContinuationSegmentSettled(terminal)
        if source is not None and (not isinstance(source, RetainedToolFailureSource) or type(source) is not RetainedToolFailureSource):
            raise AuditIntegrityError("continuation settlement lost its closed failure source owner")
        owns_failure = source is not None or (custody is not None and (custody.completed or original is not None))
        if owns_failure:
            if source is not None and not source.applied:
                try:
                    await self.phases.finish_owed_prefix(source.selected)
                except BaseException as error:
                    if _terminal_completion_unknown(error):
                        combined = self.join_failure(error)
                        if combined is error:
                            raise
                        raise combined from combined.__cause__
                    original = error if original is None else combine_failure_objects(original, error)
            # Re-enter the current source in the terminal SQL body. No earlier
            # source DTO, native pointer or cancellation flag grants a write.
            try:
                projection = self.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
                committed = await self.services.session_service.fail_continuation_tool_operation(
                    self.running,
                    required_work=self.work.coordinator,
                    failure_projection_work=projection,
                    env_ref_names=names,
                    original=original,
                    custody=custody,
                    p4_writer_observation=p4_writer_observation,
                    p4_live_owner=p4,
                )
            except BaseException as error:
                combined = self.join_failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            self._committed_failure = committed
            settlement = _StartedSettlement(
                terminal_receipt=committed.terminal, deferred_cancellations=list(committed.observation.deferred_cancellations)
            )
            try:
                await self.worker._verify_terminal_before_release(self.running, self.lease, committed.terminal, settlement)
            except BaseException as error:
                combined = self.join_failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            return ContinuationSegmentSettled(committed.terminal)
        if record.cancel_requested_at is not None:
            _stored, selected = await self.selected()
            await self.phases.finish_owed_prefix(selected)
            failure = request_cancelled_error(request_id=record.request_id)
        elif original is None or isinstance(original, asyncio.CancelledError):
            failure = worker_lost_error(request_id=record.request_id)
        else:
            failure = project_composer_operation_error(original, request_id=record.request_id)
        terminal = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                self.authority.settle_continuation_owner,
                self.running,
                outcome=failure,
                env_ref_names=names,
                deadline=self.worker.drain_deadline,
            ),
            expected=ComposerOperationRecord,
        )
        await self.worker._verify_terminal_before_release(self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal))
        return ContinuationSegmentSettled(terminal)

    async def read_failure(self, names: frozenset[str]) -> ContinuationToolFailureRead:
        try:
            return await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                operation=lambda: ContinuationToolFailureRead(
                    self.authority.read_continuation_tool_failure(self.running, env_ref_names=names)
                ),
                expected=ContinuationToolFailureRead,
            )
        except BaseException as error:
            custody = self.phases.failure_custody
            if custody is not None and custody.completed:
                combined = self.join_failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            raise

    async def dispose(self, original: BaseException | None = None) -> ContinuationSegmentOutcome:
        if original is not None and not any(original is item for item in self._consumed_native):
            self._consumed_native.append(original)
        self.sql_deadline.begin_settlement()
        record = await self.record()
        if record.cancel_requested_at is not None:
            return await self.settle(record, original)
        names = await read_disposition_env_names(
            advisor=self.service._advisor_checkpoint, authority=self.authority, required_work=self.work
        )
        if (await self.read_failure(names)).source is not None:
            return await self.settle(record, original)
        disposition = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
            operation=partial(
                self.authority.classify_continuation_owner, self.running, env_ref_names=names, deadline=self.worker.drain_deadline
            ),
            expected=str,
        )
        if disposition == "unresolved":
            return await self.settle(record, original)
        if disposition != "handover":
            raise AuditIntegrityError("V2 driver has no landed cross-sequence lineage owner")

        async def verify_handover(
            committed: ComposerSegmentRecoveryReceipt | None, original_failure: BaseException | None
        ) -> ComposerSegmentRecoveryReceipt:
            if original_failure is not None:
                raise original_failure
            if (
                type(committed) is not ComposerSegmentRecoveryReceipt
                or committed.released_context is not self.running.session_operation_context
            ):
                raise AuditIntegrityError("V2 handover lost its actual committing release receipt")

            def verify_receipt() -> ComposerSegmentRecoveryReceipt:
                self.services.session_service.session_operation_authority.verify_recovery(committed, deadline=self.worker.drain_deadline)
                return committed

            verified = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_HANDOFF_READBACK_SQL,
                operation=verify_receipt,
                expected=ComposerSegmentRecoveryReceipt,
            )
            if verified is not committed:
                raise AuditIntegrityError("V2 handover verification replaced its actual committing receipt")
            return committed

        try:
            receipt = await self.lease.consume_continuation_handoff(
                partial(
                    self.services.session_service.session_operation_authority.suspend_composer_segment,
                    self.running,
                    deadline=self.worker.drain_deadline,
                ),
                deadline=self.worker.drain_deadline,
                verify_outcome=verify_handover,
            )
        except ComposerOperationCancelledDuringTurn:
            return await self.settle(await self.record(), original)
        return ContinuationSegmentHandedOver(receipt)

    async def advance(
        self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState
    ) -> ComposerOperationRecord | PrimaryNoSdkRefusalReceipt | None:
        root, top = selected.stack, selected.stack.frames[-1]
        if type(top) is InitializeFrame:
            if top.ingress_phase == "reserved":
                if root.logical.kind != "compose_message":
                    await adopt_recompose_original(service=self.service, selected=stored, required_work=self.work)
                    return None
                request = SendMessageRequest.model_validate_json(top.request_json)
                sql, projection = self.work.reserve_pair(RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION)
                try:
                    ingress_result = await self.services.session_service.add_message_with_transcript(
                        self.running.claim.session_id,
                        "user",
                        request.content,
                        operation_id=UUID(self.running.claim.operation_id),
                        requested_state_id=request.state_id,
                        composition_state_id=request.state_id,
                        writer_principal="route_user_message",
                        session_operation_context=self.running.session_operation_context,
                        running=self.running,
                        required_work=sql,
                        continuation=stored,
                    )
                    if type(ingress_result) is not ContinuationIngressCheckpoint:
                        raise AuditIntegrityError("V2 ingress returned another actual owner")
                except BaseException as original:
                    projection.complete_owned(original)
                    raise
                projection.complete_owned()
                return None
            if top.ingress_phase == "inserted":
                await prepare_pretitle_bootstrap(service=self.service, authority=self.authority, selected=stored, required_work=self.work)
                return None
            if top.ingress_phase == "title_pending":
                if top.provider_effect_key is None:
                    prepared = await prepare_current_title_provider(
                        service=self.service, authority=self.authority, selected=stored, required_work=self.work
                    )
                    await run_prepared_title_provider(prepared=prepared, invocation_owner=self.provider)
                else:
                    current = await settle_current_title_result(service=self.service, running=self.running, required_work=self.work)
                    await apply_current_title_success(service=self.service, selected=current.selected, required_work=self.work)
                return None
            runtime = await initialize_continuation_composition(
                service=self.service, authority=self.authority, selected=stored, required_work=self.work, provider_owner=self.provider
            )
            self.phases.bind_initial(runtime)
            return None
        if type(top) is ComposeFrame and top.phase == "action_ready":
            if self.phases.runtime is None and _is_first_compose_iteration(top) and not _has_committed_primary(root):
                runtime = await initialize_continuation_composition(
                    service=self.service, authority=self.authority, selected=stored, required_work=self.work, provider_owner=self.provider
                )
                self.phases.bind_initial(runtime)
            else:
                # Later published cuts restore their retained runtime sources;
                # first initialization keeps its original stricter contract.
                runtime, _inventory = await self.phases.current(selected)
            policy = runtime.source.policy
            # This packet owns delivery preflight only. The current SQL-backed
            # PRIMARY factory separately admits its original producer messages.
            _messages, _tools, delivery, _delivery_input = self.service._prepare_primary_delivery(
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                turn_evidence=runtime.live.turn_evidence,
                state=runtime.live.state,
                failed_turn=runtime.live.failed_turn,
            )
            primary_result = await run_continuation_primary(
                service=self.service,
                authority=self.authority,
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=self.work,
                provider_owner=self.provider,
                timeout=policy.actions.primary.total,
            )
            if isinstance(primary_result, PrimaryNoSdkRefusalReceipt):
                return primary_result
            if delivery is not None:
                runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
            self.phases.accept_primary(primary_result)
            return None
        if is_applied_batch_cut(selected):
            # The post-batch phase either returns the next authoring
            # transition to the provider or publishes the result it reached.
            return await advance_continuation_iteration(self.phases, self.services, selected)
        if type(top) is EndFrame:
            # END publishes through the shared terminal owner in its own
            # transaction; the returned record is that committed terminal.
            return await review_continuation_end(self.phases, self.services, selected)
        if type(top) is ComposeFrame and top.phase == "response_retained" and await primary_issued_no_tools(self.phases, selected):
            await enter_continuation_end(self.phases, selected)
            return None
        await self.service.advance_continuation_phase(
            owner=self.phases, authority=self.authority, selected=selected, work=self.work, provider_owner=self.provider
        )
        return None

    async def pending_control(self, selected: SelectedContinuationState) -> StoredContinuationControlIntent | None:
        top = selected.stack.frames[-1]
        if type(top) is not ComposeFrame or top.phase != "action_ready" or not _has_committed_primary(selected.stack):
            return None
        _runtime, inventory = await self.phases.current(selected)

        def read() -> tuple[StoredContinuationControlIntent | None]:
            with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                return (read_pending_control_on_connection(conn, self.running, selected=selected, inventory=inventory),)

        (intent,) = await _registered(work=self.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=tuple)
        if intent is not None and type(intent) is not StoredContinuationControlIntent:
            raise AuditIntegrityError("model yield lost its actual durable intent DATA")
        return intent

    async def yield_control(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> ContinuationSegmentOutcome:
        """Yield only after the actual phase and watcher cohorts have joined."""
        self.sql_deadline.begin_settlement()
        top = selected.stack.frames[-1]
        if type(top) is not ComposeFrame or top.phase != "action_ready" or not selected.stack.is_handoff_safe:
            raise AuditIntegrityError("model yield lost its actual quiescent control cut")
        repository = self.services.session_service.session_operation_authority

        def readback() -> tuple[ComposerSegmentYieldReceipt | None]:
            value = repository.read_yield_outcome(self.running)
            if value is not None:
                repository.verify_yield(value)
            return (value,)

        async def readback_owned() -> ComposerSegmentYieldReceipt | None:
            # The registered immutable readback retains the same original
            # hard/drain lifecycle clock after the committing owner releases.
            values = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_HANDOFF_READBACK_SQL,
                operation=readback,
                expected=tuple,
            )
            if len(values) != 1 or (values[0] is not None and type(values[0]) is not ComposerSegmentYieldReceipt):
                raise AuditIntegrityError("model yield lost its actual immutable readback outcome")
            return values[0]

        async def verify_yield(
            committed: ComposerSegmentYieldReceipt | None, original: BaseException | None
        ) -> ComposerSegmentYieldReceipt:
            if original is not None and not any(original is item for item in self._consumed_native):
                self._consumed_native.append(original)
            observed = await readback_owned()
            if observed is None:
                if original is not None:
                    raise original
                raise AuditIntegrityError("model yield has no actual immutable committed outcome")
            if (
                (observed.session_id, observed.operation_id) != (self.running.claim.session_id, self.running.claim.operation_id)
                or (observed.predecessor_sequence, observed.predecessor_generation)
                != (self.running.claim.segment_sequence, self.running.claim.ownership_generation)
                or (observed.successor_sequence, observed.successor_generation)
                != (self.running.claim.segment_sequence + 1, self.running.claim.ownership_generation + 1)
                or observed.released_epoch != self.running.session_operation_context.fence.operation_epoch
                or observed.cursor != stored.cursor + 1
            ):
                raise AuditIntegrityError("model yield lost its actual atomic successor/release receipt")
            if committed is not None and observed != committed:
                raise AuditIntegrityError("model yield readback differs from its actual committing receipt")
            return observed if committed is None else committed

        try:
            receipt = await self.lease.consume_continuation_handoff(
                partial(
                    repository.commit_checkpoint_and_successor,
                    self.running,
                    expected_head=None if top.current_state_id is None else UUID(top.current_state_id),
                    expected_checkpoint_id=stored.checkpoint_id,
                    expected_cursor=stored.cursor,
                    snapshot_json=stored.payload_json,
                    reason="model_request",
                ),
                deadline=self.worker.drain_deadline,
                verify_outcome=verify_yield,
            )
        except ComposerOperationCancelledDuringTurn:
            return await self.settle(await self.record())
        return ContinuationSegmentYielded(receipt)

    async def watch_stage(
        self, stage: asyncio.Task[ComposerOperationRecord | PrimaryNoSdkRefusalReceipt | None], reader: ComposerOperationWatchReader
    ) -> None:
        key = (self.running.claim.session_id, self.running.claim.operation_id)
        stop_delivered = False
        try:
            while not stage.done():
                self.lease.raise_if_lost()
                if self.worker._local_cancels[key].is_set() and not stop_delivered:
                    self.phases.observe_stop()
                    stage.cancel(COMPOSER_CANCEL_REQUESTED)
                    stop_delivered = True
                current, _now = await reader.read()
                if current is None or (current.attempt, current.current_segment_sequence, current.ownership_generation) != (
                    self.running.claim.attempt,
                    self.running.claim.segment_sequence,
                    self.running.claim.ownership_generation,
                ):
                    self.phases.cancel_business(COMPOSER_LEASE_LOST)
                    stage.cancel(COMPOSER_LEASE_LOST)
                    return
                if current.cancel_requested_at is not None and not stop_delivered:
                    self.phases.observe_stop()
                    stage.cancel(COMPOSER_CANCEL_REQUESTED)
                    stop_delivered = True
                deadline = self.worker.drain_deadline
                if deadline is not None and time.monotonic() >= deadline.expires_at - self.sql_deadline.drain_finish_reserve_seconds:
                    self.phases.cancel_business(COMPOSER_SHUTDOWN)
                    stage.cancel(COMPOSER_SHUTDOWN)
                    return
                await reader.wait_watch_cadence()
        except asyncio.CancelledError as original:
            if reader.owns_internal_cleanup_observation(original):
                return
            self.phases.cancel_business(COMPOSER_LEASE_LOST)
            stage.cancel(COMPOSER_LEASE_LOST)
            raise
        except BaseException:
            self.phases.cancel_business(COMPOSER_LEASE_LOST)
            stage.cancel(COMPOSER_LEASE_LOST)
            raise

    def _worker_is_draining(self) -> bool:
        """Read live drain state afresh across asynchronous phase boundaries."""
        return self.worker.drain_deadline is not None

    async def run(self, adoption_failure: BaseException | None) -> ContinuationSegmentOutcome:
        from elspeth.web.sessions.composer_async_worker import _finish_operation_watcher, _OperationWatchObservation, _StartedSettlement

        if adoption_failure is not None:
            return await self.dispose(adoption_failure)
        while True:
            self.sql_deadline.begin_settlement()
            self.lease.raise_if_lost()
            if self._worker_is_draining():
                return await self.dispose()
            record = await self.record()
            if record.cancel_requested_at is not None:
                return await self.settle(record)
            names = await read_disposition_env_names(
                advisor=self.service._advisor_checkpoint, authority=self.authority, required_work=self.work
            )
            if (await self.read_failure(names)).source is not None:
                return await self.settle(record)
            if self._worker_is_draining():
                return await self.dispose()
            stored, selected = await self.selected()
            if await self.pending_control(selected) is not None:
                return await self.yield_control(stored, selected)
            policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
            started_read = time.monotonic()
            timing = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_READ_SQL,
                operation=partial(self.authority.read_continuation_timing, self.running, deadline=self.worker.drain_deadline),
                expected=ContinuationTimingObservation,
            )
            elapsed = time.monotonic() - started_read
            action = next_continuation_action_policy(selected=selected, policy=policy)
            if (
                admit_action(
                    action,
                    policy.segment,
                    hard_remaining=max(0.0, timing.hard_remaining_seconds - elapsed),
                    cooperative_remaining=max(0.0, timing.cooperative_remaining_seconds - elapsed),
                    fresh=False,
                )
                is SegmentFit.YIELD
            ):
                return await self.dispose()
            self.sql_deadline.begin_action(action, started_at=time.monotonic())
            stage_owner = self.lease.create_continuation_stage(self.advance(stored, selected), name="continuation-phase")
            stage = stage_owner.task
            if stage is None:
                raise AuditIntegrityError("Continuation phase allocation lacks its actual Task")
            reader = ComposerOperationWatchReader.for_segment(self.worker, self.running, self.lease)
            watch = asyncio.create_task(self.watch_stage(stage, reader), name="continuation-stage-watch")
            observation = _OperationWatchObservation(reader=reader)
            original: BaseException | None = None
            try:
                result = await self.lease.join_continuation_stage(stage_owner)
            except (SessionOperationFenceLost, ComposerOperationFenceLost):
                raise
            except BaseException as error:
                original = error
                if not any(error is item for item in self._consumed_native):
                    self._consumed_native.append(error)
                result = None
            finally:
                failures = await _finish_operation_watcher(watch, observation)
                if failures:
                    raise BaseExceptionGroup(
                        "Continuation stage observation failed", ([original] if original is not None else []) + list(failures)
                    )
            if original is not None:
                from elspeth.web.sessions.composer_async_worker import _terminal_completion_unknown

                if _terminal_completion_unknown(original):
                    raise original
                if isinstance(original, asyncio.CancelledError) or self.worker.drain_deadline is not None:
                    return await self.dispose(original)
                return await self.settle(await self.record(), original)
            if result is not None:
                if type(result) is PrimaryNoSdkRefusalReceipt:
                    from elspeth.web.sessions.composer_async_worker import _StartedSettlement

                    if result.terminal.status != "failed":
                        raise AuditIntegrityError("PRIMARY no-SDK refusal lost its failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        result.terminal,
                        _StartedSettlement(deferred_cancellations=list(result.deferred_cancellations), terminal_receipt=result.terminal),
                    )
                    return ContinuationSegmentSettled(result.terminal)
                # The END owner already committed this terminal together with
                # its assistant row and audit cohort (the shared R2 composite).
                if type(result) is not ComposerOperationRecord or result.status != "completed":
                    raise AuditIntegrityError("Continuation publication lost its actual committed terminal")
                await self.worker._verify_terminal_before_release(
                    self.running, self.lease, result, _StartedSettlement(terminal_receipt=result)
                )
                return ContinuationSegmentSettled(result)


async def run_continuation_segment(
    worker: ComposerAsyncWorker,
    services: ComposerAppServices,
    running: ComposerOperationRunning,
    lease: SessionOperationLease,
    *,
    adoption_failure: BaseException | None = None,
) -> ContinuationSegmentOutcome:
    """Run one real owner and join exactly its retained lease finish task."""
    from elspeth.web.composer.continuation_tool_failure import combine_failure_objects

    original: BaseException | None = None
    driver: _SegmentDriver | None = None
    try:
        # Keep native custody alive through the one retained physical close,
        # including a verified failed operation whose stage is now consumed.
        driver = _SegmentDriver(worker, services, running, lease)
        return await driver.run(adoption_failure)
    except BaseException as failure:
        original = failure
        if driver is not None:
            original = driver.join_failure(failure)
        elif adoption_failure is not None:
            original = combine_failure_objects(adoption_failure, failure)
        if original is failure:
            raise
        raise original from original.__cause__
    finally:
        if lease.required_work is not None and lease.required_work.sql_deadline_owner is not None:
            lease.required_work.sql_deadline_owner.begin_settlement()
        try:
            await lease.close(deadline=worker.drain_deadline)
        except BaseException as cleanup:
            if driver is not None:
                combined = driver.join_failure(cleanup, *((original,) if original is not None else ()))
                if combined is cleanup:
                    raise
                raise combined from combined.__cause__
            if original is None:
                raise
            combined = combine_failure_objects(original, cleanup)
            if combined is cleanup:
                raise
            raise combined from combined.__cause__
