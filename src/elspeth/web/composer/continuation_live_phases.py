"""Live retained phase adapters for the actual Composer batch owners.

One segment keeps these objects. Current SQL reads admit each new selected
cut; retained DATA never substitutes for dispatch authority or SDK identity.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, encode_coupled_batch_data
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import (
    ContinuationEnvSelection,
    CurrentContinuationEnvInventory,
    _read_current_env_names,
    materialize_continuation_env_inventory,
)
from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
from elspeth.web.composer.continuation_phase_transitions import BatchTransition, retain_batch_transition
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    _capture_owners,
    _CurrentPhaseRead,
    _registered_read,
    _selected_chunks,
    build_runtime_tool_batch_context,
    capture_continuation_runtime_owners,
    restore_continuation_runtime,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_tool_failure import ContinuationToolFailureCustody, TerminalServiceCompletion
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tool_batch import (
    BatchAccumulator,
    ContinuationBatchState,
    ContinuationCallState,
    ToolBatchContext,
    _execute_continuation_call_live,
    _finish_continuation_batch_live,
    _initialize_continuation_batch_live,
    _prepare_continuation_call_live,
)
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationStage, SessionOperationLease
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationCancel, ComposerOperationCancelledDuringTurn, ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult, RetainedPrimaryPhaseData
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl


def _require_runtime_source_refresh(before: ContinuationRuntimeSource, fresh: ContinuationRuntimeSource) -> None:
    """Keep exact source DATA while allowing its later database-clock read.

    Both values come from the current SQL owner. Timing is an observation of
    that owner's original deadlines, so equality of observation timestamps is
    neither expected nor authority to renew the segment or action clocks.
    """
    if (
        type(before) is not ContinuationRuntimeSource
        or type(fresh) is not ContinuationRuntimeSource
        or replace(fresh, timing=before.timing) != before
    ):
        raise AuditIntegrityError("live phase refresh returned a foreign source")
    earlier, later = before.timing, fresh.timing
    if (
        type(earlier) is not ContinuationTimingObservation
        or type(later) is not ContinuationTimingObservation
        or (later.sequence, later.generation, later.watermark_seconds) != (earlier.sequence, earlier.generation, earlier.watermark_seconds)
        or later.observed_at < earlier.observed_at
    ):
        raise AuditIntegrityError("live phase refresh changed its original timing owner or observation order")
    for previous, remaining in (
        (earlier.hard_remaining_seconds, later.hard_remaining_seconds),
        (earlier.cooperative_remaining_seconds, later.cooperative_remaining_seconds),
    ):
        if remaining > previous or (
            remaining > 0 and later.observed_at + timedelta(seconds=remaining) > earlier.observed_at + timedelta(seconds=previous)
        ):
            raise AuditIntegrityError("live phase refresh extended an original absolute deadline")


@dataclass(slots=True)
class ContinuationLivePhases:
    service: ComposerServiceImpl
    authority: ComposerAsyncOperationAuthority
    work: RequiredWorkBinding
    provider_owner: ProviderInvocationOwner
    lease: SessionOperationLease
    runtime: RestoredContinuationRuntime | None = None
    primary: PrimaryProviderResult | RetainedPrimaryPhaseData | None = None
    p4: ContinuationLiveP4 | None = None
    latest_snapshot: StoredContinuationSnapshot | None = None
    cancellation_requested: asyncio.Event = field(default_factory=asyncio.Event)
    business_stage: ContinuationStage[None] | None = None
    failure_custody: ContinuationToolFailureCustody | None = None
    terminal_completion: TerminalServiceCompletion | None = None

    def observe_stop(self) -> None:
        self.cancellation_requested.set()

    def cancel_business(self, reason: ComposerOperationCancel) -> None:
        self.observe_stop()
        stage = self.business_stage
        if stage is not None and stage.task is not None and not stage.task.done():
            stage.task.cancel(reason)

    async def _tool_context(self, runtime: RestoredContinuationRuntime) -> ToolBatchContext:
        context = await build_runtime_tool_batch_context(runtime, cancellation_requested=self.cancellation_requested)
        owner = self.work.coordinator.sql_deadline_owner
        if owner is not None:
            if owner.running is not runtime.materialization.inventory.running:
                raise AuditIntegrityError("tool context changed its actual original timing owner")
            now = time.monotonic()
            remaining = owner.remaining_dispatch(monotonic_now=now)
            if remaining <= 0:
                raise TimeoutError("tool action exhausted its original dispatch allowance")
            context = replace(context, deadline=min(context.deadline, now + remaining))
        return context

    def bind_initial(self, runtime: RestoredContinuationRuntime) -> None:
        if (
            self.runtime is not None
            or type(runtime) is not RestoredContinuationRuntime
            or runtime.materialization.service is not self.service
            or runtime.materialization.authority is not self.authority
            or runtime.bindings.required_work is not self.work
            or runtime.bindings.provider_owner is not self.provider_owner
        ):
            raise AuditIntegrityError("live phase initialization changed its actual runtime owners")
        self.runtime = runtime

    def accept_primary(self, result: PrimaryProviderResult) -> None:
        if type(result) is not PrimaryProviderResult or type(result.outcome) is not _CallModelOutcome:
            raise AuditIntegrityError("live phases lost their actual provider result")
        self.primary = result

    async def _primary_for_phase(
        self, runtime: RestoredContinuationRuntime, inventory: CurrentContinuationEnvInventory, selected: SelectedContinuationState
    ) -> PrimaryProviderResult | RetainedPrimaryPhaseData:
        compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
        if len(compose) != 1 or compose[0].pending_response is None:
            raise AuditIntegrityError("live phase lost its actual selected paid response")
        if self.primary is None:
            retained = await _registered_read(
                self.work,
                RetainedPrimaryPhaseData,
                partial(
                    self.authority.read_continuation_phase_primary,
                    inventory.running,
                    selected=selected,
                    reference=compose[0].pending_response,
                    env_ref_names=inventory.names,
                ),
            )
            audit = retained.outcome.audit_call
            if audit is None:
                raise AuditIntegrityError("cold paid phase lost its actual physical audit DATA")
            matching = tuple(call for call in runtime.live.recorder.llm_calls if call.call_id == audit.call_id)
            if len(matching) != 1 or matching[0] != audit:
                raise AuditIntegrityError("cold paid phase changed its actual restored recorder identity")
            completion = retained.outcome.completion
            data = runtime.graph.batch
            if data is not None:
                if completion.tool_batch.calls != data.calls:
                    raise AuditIntegrityError("cold paid phase changed its actual restored issued calls")
                # Reuse the already restored SDK atoms; only owned carriers
                # are rebound. Warm phases never take this cold DATA path.
                completion = replace(completion, tool_batch=replace(completion.tool_batch, calls=data.calls))
            self.primary = RetainedPrimaryPhaseData(retained.response, _CallModelOutcome(completion, matching[0]))
        if self.primary.response.reference != compose[0].pending_response:
            raise AuditIntegrityError("live phase replaced its original paid response")
        return self.primary

    async def current(self, selected: SelectedContinuationState) -> tuple[RestoredContinuationRuntime, CurrentContinuationEnvInventory]:
        phase = await self._current_phase_read(selected)
        return phase.runtime, phase.inventory

    async def _current_phase_read(self, selected: SelectedContinuationState) -> _CurrentPhaseRead:
        running = self.work.running
        if type(running) is not ComposerOperationRunning or running.session_operation_context is not self.lease.context:
            raise AuditIntegrityError("live phase lost its actual running/physical owner")
        finish_read: ContinuationFinishRead | None = None
        try:
            inventory = await materialize_continuation_env_inventory(
                advisor=self.service._advisor_checkpoint, authority=self.authority, selected=selected, required_work=self.work
            )
        except ComposerOperationCancelledDuringTurn:
            names = await _read_current_env_names(self.service._advisor_checkpoint, selected.stack.logical.session_id)
            inventory = CurrentContinuationEnvInventory(
                running,
                ContinuationEnvSelection(selected.checkpoint_id, selected.stack.cursor, encode_continuation_snapshot(selected.stack)),
                names,
            )
            finish_read = ContinuationFinishRead(running, selected, inventory)

        def read_source() -> ContinuationRuntimeSource:
            nonlocal finish_read
            if finish_read is not None:
                return finish_read.read_runtime(self.authority)
            try:
                return self.authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
            except ComposerOperationCancelledDuringTurn:
                finish_read = ContinuationFinishRead(running, selected, inventory)
                return finish_read.read_runtime(self.authority)

        source = await _registered_read(self.work, ContinuationRuntimeSource, read_source)
        if self.runtime is None:
            self.runtime = await restore_continuation_runtime(
                service=self.service,
                authority=self.authority,
                selected=selected,
                inventory=inventory,
                required_work=self.work,
                provider_owner=self.provider_owner,
                finish_read=finish_read,
            )
        elif self.runtime.source.selected != selected:
            runtime = self.runtime

            def refresh() -> RestoredContinuationRuntime:
                fresh = read_source()
                _require_runtime_source_refresh(source, fresh)
                runtime.materialization.advance_current(selected, inventory)
                following = replace(runtime, source=fresh)
                # Retain the actual fresh source before the async SQL bridge can
                # defer cancellation. No live messages/graph objects are rebuilt.
                self.runtime = following
                return following

            await _registered_read(self.work, RestoredContinuationRuntime, refresh)
        else:
            # Same SQL cut can arrive as a fresh selected object, including a
            # warm owner which has just observed durable Stop. Carry that
            # exact read choice without replacing live owners or their clocks.
            _require_runtime_source_refresh(self.runtime.source, source)
            self.runtime = replace(self.runtime, source=source)
        if self.runtime is None:
            raise AuditIntegrityError("live phase source lost its actual runtime")
        if self.runtime.source.selected is not selected:
            raise AuditIntegrityError("live phase read lost its actual fresh selection")
        if finish_read is not None:
            finish_read.require_selection(selected, inventory)
        return _CurrentPhaseRead(self.runtime, selected, inventory, finish_read)

    async def _retain(
        self, captured: CapturedContinuationRuntime, inventory: CurrentContinuationEnvInventory, transition: BatchTransition
    ) -> None:
        def retain() -> StoredContinuationSnapshot:
            stored = retain_batch_transition(
                self.authority, inventory.running, captured=captured, inventory=inventory, transition=transition
            )
            # Preserve the actual committing owner's return on its physical
            # thread before bridge delivery can surface a caller cancellation.
            self.latest_snapshot = stored
            return stored

        stored = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=retain,
            expected=StoredContinuationSnapshot,
        )
        selected = await load_selected_continuation(
            authority=self.authority, running=inventory.running, selected=stored, required_work=self.work
        )
        await self.current(selected)

    async def _capture_issued_finish(
        self, runtime: RestoredContinuationRuntime, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> CapturedContinuationRuntime:
        def read_source() -> ContinuationRuntimeSource:
            try:
                return self.authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
            except ComposerOperationCancelledDuringTurn:
                return self.authority.read_continuation_owed_tool_runtime(inventory.running, selected=selected, inventory=inventory)

        def capture() -> CapturedContinuationRuntime:
            source = read_source()
            captured = _capture_owners(runtime, source=source, inventory=inventory)
            following = read_source()
            if following.selected != source.selected or following.bootstrap != source.bootstrap or following.evidence != source.evidence:
                raise AuditIntegrityError("issued tool capture changed its complete current source during retention")
            return captured

        return await _registered_read(self.work, CapturedContinuationRuntime, capture)

    async def _initialize_batch(self, selected: SelectedContinuationState) -> None:
        runtime, inventory = await self.current(selected)
        result, live = await self._primary_for_phase(runtime, inventory, selected), runtime.live
        frame = selected.stack.frames[-1]
        if type(frame) is not ComposeFrame or frame.pending_response != result.response.reference:
            raise AuditIntegrityError("batch initialization changed its exact original paid response")
        self.service._enforce_tool_call_cap(
            assistant_tool_calls=result.outcome.completion.tool_batch.calls,
            state=live.state,
            initial_version=live.initial_version,
            composition_turns_used=live.composition_turns_used,
            discovery_turns_used=live.discovery_turns_used,
            recorder=live.recorder,
            failed_turn=live.failed_turn,
            persisted_tool_call_turn=live.persisted_tool_call_turn,
        )
        accumulator = BatchAccumulator(live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used)
        batch = ContinuationBatchState(
            live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used, entry_state=live.state
        )
        data = RestoredBatchData(batch, accumulator, result.outcome.completion.tool_batch.calls)
        context = await self._tool_context(runtime)
        await _initialize_continuation_batch_live(
            call_model=result.outcome, ctx=context, acc=accumulator, llm_messages=live.llm_messages, batch=batch
        )
        self.runtime = runtime = replace(runtime, graph=runtime.graph.with_batch(data))
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        chunk = encode_private_chunk(
            codec="composer.coupled-batch-data.v1",
            canonical=encode_coupled_batch_data(batch=batch, accumulator=accumulator, calls=data.calls, env_ref_names=inventory.names),
            env_ref_names=inventory.names,
        )
        tool = ToolBatchFrame(
            phase="tool_ready", response=result.response.reference, accumulator=chunk.reference, next_tool_index=0, settled_effect_ids=()
        )
        root = captured.stack.model_copy(update={"frames": (*captured.stack.frames, tool)})
        following = CapturedContinuationRuntime(selected, root, _selected_chunks(root, captured.chunks, (chunk,)))
        await self._retain(following, inventory, "batch_initialized")

    async def _execute_one(self, selected: SelectedContinuationState) -> None:
        previous = self.business_stage
        if previous is not None and (previous.task is None or not previous.task.done() or not previous.observed):
            raise AuditIntegrityError("tool dispatch cannot replace its retained physical stage")
        self.business_stage = stage = self.lease.create_continuation_stage(
            self._execute_one_owned(selected), name="continuation-issued-tool"
        )
        await self.lease.join_continuation_stage(stage)

    async def _execute_one_owned(self, selected: SelectedContinuationState) -> None:
        runtime, inventory = await self.current(selected)
        data = runtime.graph.batch
        if data is None:
            raise AuditIntegrityError("live dispatch lost its actual batch/provider owners")
        result = await self._primary_for_phase(runtime, inventory, selected)
        batch, accumulator, live = data.batch, data.accumulator, runtime.live
        if batch.phase != "call_ready" or batch.current_call is not None or batch.next_tool_index >= len(data.calls):
            raise AuditIntegrityError("live dispatch lost its actual unissued next call")
        context = await self._tool_context(runtime)
        call = ContinuationCallState(batch.next_tool_index, data.calls[batch.next_tool_index], batch.state, batch.state.version)
        batch.current_call, batch.phase = call, "call_active"
        preparation = await _prepare_continuation_call_live(
            call_model=result.outcome, ctx=context, llm_messages=live.llm_messages, batch=batch, call=call
        )
        issued_effect_key: str | None = None
        if preparation == "prepared":
            if call.audit is None:
                raise AuditIntegrityError("tool preparation lost its actual dispatch audit")
            # Prepared DATA precedes the SQL issue owner. The canonical writer
            # binds this locator to its actual checkpoint/event/physical fence;
            # it must never be injected into the pure prepared call codec.
            issued_effect_key = f"tool:{result.response.reference.digest}:{call.index}"
            self.runtime = runtime = replace(
                runtime,
                graph=runtime.graph.with_selected_dispatch(
                    capture_dispatch_audit(call.audit, env_ref_names=inventory.names), context.tool_contract_dialect
                ),
            )
            captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
            top = captured.stack.frames[-1]
            if type(top) is not ToolBatchFrame:
                raise AuditIntegrityError("prepared tool capture changed its actual frame family")
            root = captured.stack.model_copy(
                update={
                    "frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "effect_reconciling"})),
                    "effect_frontier": (
                        *captured.stack.effect_frontier,
                        EffectFrontierEntry(effect_id=issued_effect_key, disposition="dispatched", receipt_id=None),
                    ),
                }
            )
            await self._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, "tool_issued")
            self.failure_custody = ContinuationToolFailureCustody(
                inventory.running, self.work, result.response, batch, data.calls, live.recorder, call
            )
            await _execute_continuation_call_live(
                call_model=result.outcome,
                ctx=context,
                acc=accumulator,
                llm_messages=live.llm_messages,
                batch=batch,
                call=call,
                failure_custody=self.failure_custody,
            )
        if preparation == "stop":
            batch.current_call, batch.phase = None, "dispatch_ready"
            return
        batch.next_tool_index += 1
        batch.current_call = None
        batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(data.calls) else "call_ready"
        live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used = (
            batch.state,
            batch.last_validation,
            batch.last_runtime_preflight,
            batch.advisor_calls_used,
        )
        if batch.service_failure is not None:
            custody = self.failure_custody
            if preparation != "prepared" or custody is None or issued_effect_key is None or self.runtime is None:
                raise AuditIntegrityError("service completion lost its actual issued native phase")
            if self.terminal_completion is not None:
                raise AuditIntegrityError("service completion cannot replace another native terminal owner")
            try:
                self.terminal_completion = TerminalServiceCompletion(custody, self.runtime, context, result.outcome, issued_effect_key)
                # This failed batch cannot be encoded or restored. Its last valid
                # issued SQL cut is only a source bookmark for terminal P4 proof.
                await self._persist_batch(self.runtime.source.selected)
            except BaseException as error:
                combined = custody.failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            return
        if self.runtime is None:
            raise AuditIntegrityError("completed tool lost its retained runtime")
        runtime = self.runtime
        inventory = runtime.materialization.inventory
        selected = runtime.source.selected
        self.runtime = runtime = replace(runtime, graph=runtime.graph.without_selected_dispatch())
        captured = (
            await self._capture_issued_finish(runtime, selected=selected, inventory=inventory)
            if preparation == "prepared"
            else await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        )
        top = captured.stack.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("completed tool capture changed its actual frame family")
        root = captured.stack.model_copy(update={"frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "tool_ready"}))})
        if preparation == "prepared":
            if issued_effect_key is None:
                raise AuditIntegrityError("completed tool lost its actual issued effect locator")
            receipt = f"tool-result:{top.accumulator.digest}"
            root = root.model_copy(
                update={
                    "effect_frontier": tuple(
                        EffectFrontierEntry(effect_id=entry.effect_id, disposition="committed", receipt_id=receipt)
                        if entry.effect_id == issued_effect_key
                        else entry
                        for entry in root.effect_frontier
                    ),
                    "frames": (
                        *root.frames[:-1],
                        root.frames[-1].model_copy(update={"settled_effect_ids": (*top.settled_effect_ids, issued_effect_key)}),
                    ),
                }
            )
        await self._retain(
            CapturedContinuationRuntime(selected, root, captured.chunks),
            inventory,
            "tool_completed" if preparation == "prepared" else "tool_preparation_completed",
        )

    async def _persist_batch(self, selected: SelectedContinuationState) -> None:
        terminal = self.terminal_completion
        if terminal is None:
            phase_read = await self._current_phase_read(selected)
            runtime, inventory = phase_read.runtime, phase_read.inventory
            result = await self._primary_for_phase(runtime, inventory, selected)
            context = await build_runtime_tool_batch_context(
                runtime, cancellation_requested=self.cancellation_requested, phase_read=phase_read
            )
            call_model = result.outcome
        else:
            terminal.require_native()
            if self.runtime is not terminal.runtime or selected is not terminal.runtime.source.selected or terminal.p4 is not None:
                raise terminal.custody.failure(AuditIntegrityError("terminal P4 cannot replace or replay its actual native completion"))
            runtime = terminal.runtime
            inventory = runtime.materialization.inventory
            context, call_model = terminal.context, terminal.call_model
        data = runtime.graph.batch
        if data is None or data.batch.phase != "dispatch_ready":
            raise AuditIntegrityError("live P4 lost its actual completed batch/provider owners")
        dispatch, advisor_calls = _finish_continuation_batch_live(
            call_model=call_model,
            ctx=context,
            acc=data.accumulator,
            batch=data.batch,
            failure_custody=self.failure_custody,
        )
        live, bindings = runtime.live, runtime.bindings
        self.p4 = owner = ContinuationLiveP4(
            self.authority,
            selected,
            inventory,
            self.work,
            data,
            runtime,
            withheld_replies=live.recorder.withheld_replies[runtime.drained.withheld_replies :],
        )
        if terminal is not None:
            terminal.bind_p4(owner)

        async def publish() -> None:
            deadline_owner = self.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                if deadline_owner.running is not inventory.running:
                    raise AuditIntegrityError("P4 finishing changed its actual original timing owner")
                # This retained child only finishes the actual existing batch;
                # its SQL owner still proves all current/pre-Stop authority.
                # Spend the original action's reserve without restarting D.
                deadline_owner.begin_settlement()
            original: BaseException | None = None
            try:
                await self.service._persist_turn_audit(
                    tool_outcomes=dispatch.tool_outcomes,
                    mutation_ceiling=bindings.mutation_ceiling,
                    authority_revocation=dispatch.authority_revocation,
                    base_state=data.batch.entry_state,
                    decoded_args_by_call_id=dispatch.decoded_args_by_call_id,
                    private_authorings=dispatch.private_authorings,
                    private_invocations=dispatch.private_invocations,
                    assistant_message=dispatch.assistant_message,
                    raw_assistant_content=dispatch.raw_assistant_content,
                    assistant_tool_calls=dispatch.assistant_tool_calls,
                    crash_pending=(
                        dispatch.plugin_crash is not None
                        or dispatch.advisor_failure is not None
                        or dispatch.service_failure is not None
                        or (
                            data.batch.retained_failure is not None
                            and data.batch.retained_failure.kind in ("plugin_crash", "advisor_failure")
                        )
                    ),
                    session_id=bindings.session_id,
                    session_operation_context=bindings.session_operation_context,
                    current_state_id=live.current_state_id,
                    persisted_tool_call_turn=live.persisted_tool_call_turn,
                    persisted_assistant_message_id=live.persisted_assistant_message_id,
                    persisted_assistant_content=live.persisted_assistant_content,
                    ingress=bindings.ingress,
                    chat_ingress_inputs=None if bindings.chat_ingress_inputs is None else list(bindings.chat_ingress_inputs),
                    advisor_repair_context_introduced=live.advisor_repair_context_introduced,
                    required_work=self.work,
                    turn_evidence=live.turn_evidence,
                    continuation=owner,
                    terminal_completion=terminal,
                )
            except BaseException as failure:
                original = failure if self.failure_custody is None else self.failure_custody.failure(failure)
            finally:
                # turn_audit retains actual committed publication before a
                # deferred cancellation or verification failure may escape.
                persist = owner.publication
                if persist is not None:
                    live.current_state_id = persist.current_state_id
                    live.persisted_assistant_message_id = persist.persisted_assistant_message_id
                    live.persisted_assistant_content = persist.persisted_assistant_content
                    live.persisted_tool_call_turn, live.failed_turn = persist.persisted_tool_call_turn, persist.failed_turn
                    live.advisor_calls_used = advisor_calls
            if owner.publication is not None:
                # The one checkpoint projection reconciled both actual SQL
                # outputs before completing. Never submit a second apply here.
                self.runtime = owner.runtime
                if original is not None:
                    raise original
            elif original is not None:
                raise original
            try:
                owner.raise_after_publication()
            except BaseException as failure:
                if self.failure_custody is not None:
                    combined = self.failure_custody.failure(failure)
                    if combined is failure:
                        raise
                    raise combined from combined.__cause__
                raise
            custody = self.failure_custody
            if custody is not None and custody.completed:
                if custody.wrapper is not None:
                    raise custody.wrapper from custody.original
                if custody.original is not None:
                    raise custody.original

        critical = self.lease.create_continuation_stage(publish(), name="continuation-required-p4")
        await self.lease.join_continuation_stage(critical)

    async def finish_owed_prefix(self, selected: SelectedContinuationState) -> None:
        """Finish an actual retained prefix; the SQL owner proves durable Stop."""
        if self.terminal_completion is not None:
            await self._persist_batch(selected)
            return
        top = selected.stack.frames[-1]
        if type(top) is not ToolBatchFrame or top.phase != "tool_ready" or top.accumulator.codec != "composer.coupled-batch-data.v1":
            return
        runtime, _inventory = await self.current(selected)
        data = runtime.graph.batch
        if data is None or not data.batch.tool_outcomes:
            return
        if data.batch.phase == "call_ready":
            data.batch.stop_issued_batch = True
            data.batch.phase = "dispatch_ready"
        if data.batch.phase == "dispatch_ready":
            await self._persist_batch(selected)

    async def advance(self, selected: SelectedContinuationState) -> None:
        if self.terminal_completion is not None:
            await self._persist_batch(selected)
            return
        top = selected.stack.frames[-1]
        if type(top) is ComposeFrame and top.phase == "response_retained":
            await self._initialize_batch(selected)
            return
        if type(top) is ToolBatchFrame and top.phase == "tool_ready":
            runtime, _inventory = await self.current(selected)
            if runtime.graph.batch is not None and runtime.graph.batch.batch.phase == "call_ready":
                await self._execute_one(selected)
                return
            if runtime.graph.batch is not None and runtime.graph.batch.batch.phase == "dispatch_ready":
                await self._persist_batch(selected)
                return
        raise AuditIntegrityError("selected phase has no matching live tool action")
