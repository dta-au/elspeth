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
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer.continuation import ComposeFrame, InitializeFrame
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation, SegmentFit, admit_action
from elspeth.web.composer.continuation_composition_initialization import initialize_continuation_composition
from elspeth.web.composer.continuation_env_inventory import _read_current_env_names
from elspeth.web.composer.continuation_initialization import _registered, prepare_pretitle_bootstrap
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
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
from elspeth.web.sessions.continuation_ingress import ContinuationIngressCheckpoint
from elspeth.web.sessions.continuation_tool_failure import ContinuationToolFailureRead, RetainedToolFailureSource
from elspeth.web.sessions.schemas import MessageWithStateResponse, SendMessageRequest

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
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
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
            names = await _read_current_env_names(self.service._advisor_checkpoint, str(self.running.claim.session_id))
            failure_read = await self.read_failure(names)
        except BaseException as error:
            combined = self.join_failure(error)
            if combined is error:
                raise
            raise combined from combined.__cause__
        source, custody = failure_read.source, self.phases.failure_custody
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
                source=RequiredWorkSource.CONTINUATION_READ_SQL,
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
        from elspeth.web.sessions.composer_async_worker import _owned

        if original is not None and not any(original is item for item in self._consumed_native):
            self._consumed_native.append(original)
        self.sql_deadline.begin_settlement()
        record = await self.record()
        if record.cancel_requested_at is not None:
            return await self.settle(record, original)
        names = await _read_current_env_names(self.service._advisor_checkpoint, str(self.running.claim.session_id))
        if (await self.read_failure(names)).source is not None:
            return await self.settle(record, original)
        disposition = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=partial(
                self.authority.classify_continuation_owner, self.running, env_ref_names=names, deadline=self.worker.drain_deadline
            ),
            expected=str,
        )
        if disposition == "unresolved":
            return await self.settle(record, original)
        if disposition != "handover":
            raise AuditIntegrityError("V2 driver has no landed cross-sequence lineage owner")
        try:
            receipt = await self.lease.consume_continuation_handoff(
                partial(
                    self.services.session_service.session_operation_authority.suspend_composer_segment,
                    self.running,
                    deadline=self.worker.drain_deadline,
                ),
                deadline=self.worker.drain_deadline,
            )
        except ComposerOperationCancelledDuringTurn:
            return await self.settle(await self.record(), original)
        if type(receipt) is not ComposerSegmentRecoveryReceipt or receipt.released_context is not self.running.session_operation_context:
            raise AuditIntegrityError("V2 handover lost its actual committing release receipt")
        # Consume already released atomically. Read back immutable history; a
        # peer may be running now. Never try another release after a read error.
        await _owned(
            run_sync_in_worker(
                self.services.session_service.session_operation_authority.verify_recovery, receipt, deadline=self.worker.drain_deadline
            )
        )
        return ContinuationSegmentHandedOver(receipt)

    async def advance(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> MessageWithStateResponse | None:
        root, top = selected.stack, selected.stack.frames[-1]
        if type(top) is InitializeFrame:
            if top.ingress_phase == "reserved":
                if root.logical.kind != "compose_message":
                    await self.service.advance_continuation_phase(
                        owner=self.phases, authority=self.authority, selected=selected, work=self.work, provider_owner=self.provider
                    )
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
            if self.phases.runtime is None:
                runtime = await initialize_continuation_composition(
                    service=self.service, authority=self.authority, selected=stored, required_work=self.work, provider_owner=self.provider
                )
                self.phases.bind_initial(runtime)
            else:
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
            if delivery is not None:
                runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
            self.phases.accept_primary(primary_result)
            return None
        await self.service.advance_continuation_phase(
            owner=self.phases, authority=self.authority, selected=selected, work=self.work, provider_owner=self.provider
        )
        return None

    async def watch_stage(self, stage: asyncio.Task[MessageWithStateResponse | None], reader: ComposerOperationWatchReader) -> None:
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

    async def run(self, adoption_failure: BaseException | None) -> ContinuationSegmentOutcome:
        from elspeth.web.sessions.composer_async_worker import _finish_operation_watcher, _OperationWatchObservation, _StartedSettlement

        if adoption_failure is not None:
            return await self.dispose(adoption_failure)
        while True:
            self.sql_deadline.begin_settlement()
            self.lease.raise_if_lost()
            record = await self.record()
            if record.cancel_requested_at is not None:
                return await self.settle(record)
            names = await _read_current_env_names(self.service._advisor_checkpoint, str(self.running.claim.session_id))
            if (await self.read_failure(names)).source is not None:
                return await self.settle(record)
            if self.worker.drain_deadline is not None:
                return await self.dispose()
            stored, selected = await self.selected()
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
                if type(result) is not MessageWithStateResponse:
                    raise AuditIntegrityError("Continuation publication lost its nominal actual response")
                names = await _read_current_env_names(self.service._advisor_checkpoint, str(self.running.claim.session_id))
                try:
                    terminal = await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                        operation=partial(
                            self.authority.settle_continuation_owner,
                            self.running,
                            outcome=result,
                            env_ref_names=names,
                            deadline=self.worker.drain_deadline,
                        ),
                        expected=ComposerOperationRecord,
                    )
                except ComposerOperationCancelledDuringTurn:
                    return await self.settle(await self.record())
                await self.worker._verify_terminal_before_release(
                    self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal)
                )
                return ContinuationSegmentSettled(terminal)


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
