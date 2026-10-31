"""Reached service/admission failures, distinct from ordinary tool/Advisor faults.

The SDK and selected service refusal are controlled. Bootstrap, session fences,
the registered interpretation handler, audit, provider settlement and V2 phases
are real. These tests do not grant a portable representation to live exceptions.
"""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import litellm
import pytest
from litellm import ModelResponse
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.composer import session_tool as session_dispatch
from elspeth.web.composer import tools
from elspeth.web.composer._compose_loop_carriers import _ServiceHandlerFailure
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_message_family_builders import rebuild_tool_result_message
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_value, encode_tool_result_value
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.service import InterpretationPlaceholderConsumedError
from tests.unit.web.composer import test_compose_loop_interpretation_review_dispatch as legacy_fixtures
from tests.unit.web.composer._helpers import _persisted_tool_responses
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import _state_with_llm_node
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_fixtures
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import _assert_required_guard_admitted, initialized_primary_input
from tests.unit.web.sessions.test_continuation_tool_failure import _advisor_case

operation_store = _operation_store
engine = legacy_fixtures.engine
sessions_service = legacy_fixtures.sessions_service


def _review_call():
    return (
        "pending_review",
        "request_interpretation_review",
        {
            "affected_node_id": "rate_node",
            "kind": "vague_term",
            "user_term": "cool",
            "llm_draft": "Visually appealing and well-organized.",
        },
    )


@asynccontextmanager
async def _issued_review_case(store, tmp_path, monkeypatch, *, calls=None):
    """Seed an honest incomplete draft, then admit a genuine SDK-issued batch."""
    sessions = store[3]
    actual_save = sessions.save_composition_state
    actual_ready = initialization_fixtures.ready_case
    saved = []
    review_state = _state_with_llm_node()

    async def save_seed(session_id, state, **kwargs):
        assert kwargs["provenance"] == "session_seed"
        # Preserve bootstrap metadata/completion facts and its real issued
        # context. This remains an incomplete draft, never a fabricated valid
        # graph or completed continuation/P4 result.
        state = replace(state, nodes=review_state.to_dict()["nodes"], is_valid=False)
        result = await actual_save(session_id, state, **kwargs)
        saved.append(result)
        monkeypatch.setattr(sessions, "save_composition_state", actual_save)
        return result

    async def ready(*args, **kwargs):
        return await actual_ready(*args, **kwargs, bound_base=True)

    monkeypatch.setattr(sessions, "save_composition_state", save_seed)
    monkeypatch.setattr(initialization_fixtures, "ready_case", ready)
    runtime, work, guard_requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    assert len(saved) == 1
    assert runtime.live.current_state_id == str(saved[0].id)
    assert runtime.live.state.nodes[0].options["prompt_template"] == review_state.nodes[0].options["prompt_template"]
    running = work.running
    assert running is not None
    service, provider = runtime.materialization.service, runtime.bindings.provider_owner
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    owner = ContinuationLivePhases(service, store[2], work, provider, lease)
    owner.bind_initial(runtime)
    issued = (_review_call(),) if calls is None else calls
    sdk = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            store[0], running.session_operation_context, guard_requests, role="composer", user_prompt=runtime.bindings.message
        )
        sdk.append(kwargs)
        assert len(sdk) == 1, "an issued service failure must not start another primary call"
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "Review the existing draft through its ordinary tools.",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": canonical_json(encode_semantic_arguments(name, service._planner_dialect, arguments)),
                                },
                            }
                            for call_id, name, arguments in issued
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        _messages, _tools, delivery, _input = service._prepare_primary_delivery(
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            turn_evidence=runtime.live.turn_evidence,
            state=runtime.live.state,
            failed_turn=runtime.live.failed_turn,
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            primary = await run_continuation_primary(
                service=service,
                authority=store[2],
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=provider,
                timeout=5,
            )
        if delivery is not None:
            runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
        owner.accept_primary(primary)
        selected = await load_selected_continuation(authority=store[2], running=running, selected=primary.snapshot, required_work=work)
        await owner.advance(selected)
        assert tools._SESSION_AWARE_TOOL_HANDLERS["request_interpretation_review"] is tools._handle_request_interpretation_review
        yield owner, running, sdk
    finally:
        await lease.close()


def _service_error(kind):
    if kind == "sql":
        return OperationalError("SELECT interpretation_events", {}, RuntimeError("controlled native SQL refusal"))
    if kind == "admission":
        return AsyncWorkerAdmissionTimeoutError("controlled service admission refusal")
    assert kind == "generation"
    return RequiredGenerationUnavailable("controlled service generation refusal")


@pytest.mark.parametrize("tool", ("request_interpretation_review", "request_advisor_hint"))
def test_service_result_redaction_accepts_only_the_exact_value_free_envelope(tool):
    telemetry = NoopRedactionTelemetry()
    payload = {"success": False, "status": "SERVICE_FAILURE", "reason": "service_failure"}
    assert redact_tool_call_response(tool_name=tool, response=payload, telemetry=telemetry) == payload
    bad = {**payload, "error": "private original exception text must not escape"}
    if tool == "request_interpretation_review":
        with pytest.raises(ValidationError):
            redact_tool_call_response(tool_name=tool, response=bad, telemetry=telemetry)
    else:
        projected = redact_tool_call_response(tool_name=tool, response=bad, telemetry=telemetry)
        assert projected != bad and bad["error"] not in canonical_json(projected)


def _refuse_service(sessions, monkeypatch, *, seam, original, cause, context):
    entered = []
    original.__cause__ = cause

    async def read(session_id, **kwargs):
        entered.append((session_id, kwargs))
        raise original

    async def write(**kwargs):
        assert kwargs["session_operation_context"] is context
        assert kwargs["affected_node_id"] == "rate_node"
        assert kwargs["llm_draft"] == _review_call()[2]["llm_draft"]
        entered.append(kwargs)
        raise original

    if seam == "read":
        monkeypatch.setattr(sessions, "list_interpretation_events", read)
    else:
        assert seam == "write"
        monkeypatch.setattr(sessions, "create_pending_interpretation_event", write)
    return entered


@pytest.mark.asyncio
async def test_registered_pending_review_reaches_real_service_and_p4(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        await _advance(owner)
        rows = await operation_store[3].list_interpretation_events(running.claim.session_id, status="pending")
        assert len(rows) == 1 and rows[0].affected_node_id == "rate_node"
        assert rows[0].tool_call_id == "pending_review"
        invocation = owner.runtime.live.recorder.invocations[-1]
        assert invocation.status is ComposerToolStatus.SUCCESS
        assert invocation.error_class is invocation.private_failure is None
        response = owner.runtime.graph.batch.batch.tool_outcomes[-1].response
        assert isinstance(response, tools.ToolResult) and response.success is True
        call = owner.runtime.graph.batch.batch.tool_outcomes[-1].call
        assert rebuild_tool_result_message(call=call, result=response) == owner.runtime.live.llm_messages[-1]
        encoded = encode_tool_result_value("request_interpretation_review", response, env_ref_names=owner.runtime.env_ref_names)
        restored = decode_tool_result_value(encoded, pre_state=response.updated_state, env_ref_names=owner.runtime.env_ref_names)
        assert canonical_json(restored.to_dict()) == canonical_json(response.to_dict())
        await _advance(owner)
        assert owner.p4.committed_outcome is not None
        assert len(sdk) == len(owner.runtime.live.recorder.llm_calls) == 1


@pytest.mark.asyncio
async def test_completed_result_preserves_declared_discovery_and_unknown_refusal(operation_store, tmp_path, monkeypatch):
    async with _phase_case(
        operation_store, tmp_path, monkeypatch, tool_call=("selected_schema", "get_plugin_schema", {"plugin_type": "source", "name": "csv"})
    ) as (owner, _running, _primary, sdk):
        await _advance(owner)
        result = owner.runtime.graph.batch.batch.tool_outcomes[-1].response
        assert type(result) is tools.ToolResult and result.success is True
        call = owner.runtime.graph.batch.batch.tool_outcomes[-1].call
        assert rebuild_tool_result_message(call=call, result=result) == owner.runtime.live.llm_messages[-1]
        names = owner.runtime.env_ref_names
        encoded = encode_tool_result_value("get_plugin_schema", result, env_ref_names=names)
        restored = decode_tool_result_value(encoded, pre_state=result.updated_state, env_ref_names=names)
        assert canonical_json(admit_discovery_result("get_plugin_schema", restored).to_dict()) == canonical_json(
            admit_discovery_result("get_plugin_schema", result).to_dict()
        )
        with pytest.raises(FrameworkBugError, match="Successful discovery response has no data"):
            encode_tool_result_value("get_plugin_schema", replace(result, data=None), env_ref_names=names)
        with pytest.raises(FrameworkBugError, match="Successful discovery response has no data"):
            rebuild_tool_result_message(call=call, result=replace(result, data=None))
        with pytest.raises(AuditIntegrityError, match="tool continuation result has no registered tool"):
            encode_tool_result_value("unregistered_service_failure_control", result, env_ref_names=names)
        unknown = replace(call, function=replace(call.function, name="unregistered_service_failure_control"))
        with pytest.raises(AuditIntegrityError, match="tool continuation result has no registered tool"):
            rebuild_tool_result_message(call=unknown, result=result)
        assert encode_tool_result_value("get_plugin_schema", result, env_ref_names=names) == encoded
        assert len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seam,kind", (("read", "sql"), ("write", "sql"), ("read", "admission"), ("write", "generation")))
async def test_registered_service_original_is_not_a_plugin_crash(operation_store, tmp_path, monkeypatch, seam, kind):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = _service_error(kind), RuntimeError("original service cause")
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam=seam, original=original, cause=cause, context=running.session_operation_context
        )
        with pytest.raises(type(original)) as observed:
            await _advance(owner)
        assert observed.value is original
        assert owner.p4 is not None and owner.p4.committed_outcome is not None
        assert len(entered) == 1, "the real registered handler must reach its selected service seam"
        custody = owner.failure_custody
        assert custody.original is original and original.__cause__ is cause
        invocation = owner.runtime.live.recorder.invocations[-1]
        assert invocation.status is ComposerToolStatus.SUCCESS, "a nominal service refusal is a controlled dispatch, not a plugin crash"
        assert invocation.private_failure is invocation.error_class is None
        assert invocation.result_canonical is not None
        payload = json.loads(invocation.result_canonical)
        assert payload["success"] is False and payload["status"] == "SERVICE_FAILURE"
        assert "original service cause" not in invocation.result_canonical
        assert custody.wrapper is None
        assert owner.runtime.graph.batch.batch.stop_issued_batch
        assert len(sdk) == len(owner.runtime.live.recorder.llm_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("normalization", "audit"))
async def test_actual_returned_handler_service_error_is_not_a_controlled_refusal(operation_store, tmp_path, monkeypatch, seam):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = _service_error("sql"), RuntimeError("post-handler service cause")
        original.__cause__ = cause
        entered = []
        if seam == "normalization":
            actual_normalize = session_dispatch.normalize_tool_result_validation

            def normalize(result, catalog):
                assert result.success and result.data["_kind"] == "interpretation_review_pending"
                entered.append(result)
                raise original

            monkeypatch.setattr(session_dispatch, "normalize_tool_result_validation", normalize)
        else:
            actual_append = BufferingRecorder._append_invocation_locked

            def append(self, invocation, *, observation=None):
                if invocation.tool_name == "request_interpretation_review" and invocation.status is ComposerToolStatus.SUCCESS:
                    assert observation is owner.failure_custody.observation
                    entered.append(invocation)
                    raise original
                return actual_append(self, invocation, observation=observation)

            monkeypatch.setattr(BufferingRecorder, "_append_invocation_locked", append)
        with pytest.raises(OperationalError) as observed:
            await _advance(owner)
        assert observed.value is original and original.__cause__ is cause
        assert len(entered) == 1
        custody = owner.failure_custody
        assert custody.observation.returned and not custody.observation.execution_failed
        assert not custody.completed and custody.wrapper is None
        assert owner.runtime.graph.batch.batch.service_failure is None
        assert owner.terminal_completion is None and owner.p4 is None
        rows = await operation_store[3].list_interpretation_events(running.claim.session_id, status="pending")
        assert len(rows) == 1 and rows[0].tool_call_id == "pending_review", "the genuine handler returned after its real write"
        assert len(sdk) == 1
        if seam == "normalization":
            monkeypatch.setattr(session_dispatch, "normalize_tool_result_validation", actual_normalize)


@pytest.mark.asyncio
async def test_actual_service_handler_and_audit_secondary_keep_origins_without_internal_marker(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = _service_error("sql"), RuntimeError("service prior cause")
        original_context = RuntimeError("service prior context")
        original.__context__ = original_context
        secondary = OperationalError("actual audit refusal", {}, RuntimeError("audit driver origin"))
        secondary_cause, secondary_context = RuntimeError("audit prior cause"), RuntimeError("audit prior context")
        secondary.__cause__, secondary.__context__ = secondary_cause, secondary_context
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        actual_append = BufferingRecorder._append_invocation_locked
        audit_attempts = []

        def append(self, invocation, *, observation=None):
            if invocation.tool_name == "request_interpretation_review" and invocation.status is ComposerToolStatus.SUCCESS:
                assert observation is owner.failure_custody.observation
                audit_attempts.append(invocation)
                raise secondary
            return actual_append(self, invocation, observation=observation)

        monkeypatch.setattr(BufferingRecorder, "_append_invocation_locked", append)
        with pytest.raises(BaseExceptionGroup) as observed:
            await _advance(owner)
        assert len(entered) == len(audit_attempts) == len(sdk) == 1
        pending, seen, origins = [observed.value], set(), []
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            origins.append(error)
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
            if error.__cause__ is not None:
                pending.append(error.__cause__)
            if error.__context__ is not None:
                pending.append(error.__context__)
        assert any(error is original for error in origins) and any(error is secondary for error in origins)
        assert not any(isinstance(error, _ServiceHandlerFailure) for error in origins)
        assert original.__cause__ is cause and original.__context__ is original_context
        assert secondary.__cause__ is secondary_cause and secondary.__context__ is secondary_context
        assert "_ServiceHandlerFailure" not in capture_failure_facts(observed.value).canonical
        assert owner.failure_custody.original is original and not owner.failure_custody.completed
        assert owner.terminal_completion is None and owner.p4 is None


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("handler", "normalization", "audit"))
async def test_legacy_registered_service_marker_separates_actual_handler_from_post_return(tmp_path, sessions_service, monkeypatch, seam):
    from elspeth.web.composer.service import ComposerAvailability

    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=str(kwargs["model"]), provider="anthropic"),
    )
    composer = legacy_fixtures._build_composer(tmp_path, sessions_service)
    actual_save = sessions_service.save_composition_state

    async def save_incomplete(session_id, payload, **kwargs):
        return await actual_save(session_id, replace(payload, is_valid=False), **kwargs)

    monkeypatch.setattr(sessions_service, "save_composition_state", save_incomplete)
    session_id, state_id = await legacy_fixtures._seed_session_and_state(sessions_service)
    monkeypatch.setattr(sessions_service, "save_composition_state", actual_save)
    original, cause = _service_error("sql"), RuntimeError("legacy service prior cause")
    original.__cause__ = cause
    entered, early = [], []
    if seam == "handler":

        async def create(**kwargs):
            entered.append(kwargs)
            raise original

        monkeypatch.setattr(sessions_service, "create_pending_interpretation_event", create)
    elif seam == "normalization":

        def normalize(result, catalog):
            assert result.success and result.data["_kind"] == "interpretation_review_pending"
            entered.append(result)
            raise original

        monkeypatch.setattr(session_dispatch, "normalize_tool_result_validation", normalize)
    else:
        actual_record = BufferingRecorder.record

        def record(self, invocation):
            if invocation.tool_name == "request_interpretation_review" and invocation.status is ComposerToolStatus.SUCCESS:
                entered.append(invocation)
                raise original
            return actual_record(self, invocation)

        monkeypatch.setattr(BufferingRecorder, "record", record)

    async def forbid_early(**kwargs):
        early.append(kwargs)
        raise AssertionError("service failure cannot start EARLY Advisor work")

    monkeypatch.setattr(composer._advisor_checkpoint, "_maybe_run_early_checkpoint", forbid_early)
    call_id, tool_name, arguments = _review_call()
    llm = legacy_fixtures._ScriptedLLM(
        [legacy_fixtures._fake_response_with_tool_call(tool_call_id=call_id, tool_name=tool_name, arguments=arguments)]
    )
    with pytest.raises(OperationalError) as observed:
        await composer._run_one_turn_for_test(
            llm=llm, session_id=str(session_id), current_state_id=str(state_id), initial_state=_state_with_llm_node()
        )
    assert observed.value is original and original.__cause__ is cause
    assert len(entered) == len(llm.messages) == 1 and not early
    events = await sessions_service.list_interpretation_events(session_id, status="pending")
    rows = _persisted_tool_responses(composer, str(session_id))
    if seam == "handler":
        assert not events
        assert [row["tool_call_id"] for row in rows] == ["pending_review"]
        assert json.loads(rows[0]["result_canonical"]) == {"success": False, "status": "SERVICE_FAILURE", "reason": "service_failure"}
    else:
        assert len(events) == 1 and events[0].tool_call_id == "pending_review"
        assert not rows, "a returned handler's later failure cannot fabricate controlled completion/P4"


@pytest.mark.asyncio
async def test_registered_implementation_error_remains_a_coded_crash(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = ValueError("implementation defect stays coded"), RuntimeError("implementation cause")
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        await _advance(owner)
        assert len(entered) == 1
        custody = owner.failure_custody
        assert custody.original is original and original.__cause__ is cause
        assert custody.invocation.status is ComposerToolStatus.PLUGIN_CRASH
        assert type(custody.wrapper) is ComposerPluginCrashError and custody.wrapper.original_exc is original
        with pytest.raises(ComposerPluginCrashError) as caught:
            await _advance(owner)
        assert caught.value.original_exc is original and owner.p4.committed_outcome is not None
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_registered_interpretation_conflict_remains_argument_feedback(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = InterpretationPlaceholderConsumedError("site changed under the service lock"), RuntimeError("conflict cause")
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        await _advance(owner)
        assert len(entered) == 1
        invocation = owner.runtime.live.recorder.invocations[-1]
        assert invocation.status is ComposerToolStatus.ARG_ERROR and invocation.error_class == "ToolArgumentError"
        assert owner.failure_custody.original is None and not owner.failure_custody.completed
        await _advance(owner)
        assert owner.p4.committed_outcome is not None and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("compose_limited", (True, False), ids=("compose-limited", "advisor-limited"))
async def test_hint_admission_reaches_operation_owner(operation_store, tmp_path, monkeypatch, compose_limited):
    async with _advisor_case(operation_store, tmp_path, monkeypatch, following_call=True) as (owner, running, sdk):
        original = AsyncWorkerAdmissionTimeoutError("private hint admission refusal")
        cause = RuntimeError("hint admission original cause")
        original.__cause__ = cause
        entered, inventory_reads, timeouts = [], [], []
        in_hint = False
        sessions = operation_store[3]
        actual_get = sessions.get_session
        actual_hint = AdvisorCheckpointOwner._call_advisor_for_tool
        entry_deadline = owner.runtime.bindings.deadline

        class Inventory:
            def list_refs(self, user_id):
                inventory_reads.append(user_id)
                return []

        async def get_session(session_id):
            if in_hint:
                entered.append(session_id)
                raise original
            return await actual_get(session_id)

        async def hint(self, arguments, **kwargs):
            nonlocal in_hint
            timeouts.append(kwargs["timeout"])
            in_hint = True
            try:
                return await actual_hint(self, arguments, **kwargs)
            finally:
                in_hint = False

        monkeypatch.setattr(owner.service._advisor_checkpoint, "_secret_service", Inventory())
        monkeypatch.setattr(sessions, "get_session", get_session)
        monkeypatch.setattr(AdvisorCheckpointOwner, "_call_advisor_for_tool", hint)
        timeout = 300.0 if compose_limited else 1.0
        monkeypatch.setattr(
            owner.service, "_settings", owner.service._settings.model_copy(update={"composer_advisor_timeout_seconds": timeout})
        )
        with pytest.raises(AsyncWorkerAdmissionTimeoutError) as observed:
            await _advance(owner)
        assert observed.value is original
        assert owner.p4 is not None and owner.p4.committed_outcome is not None
        assert entered == [running.claim.session_id] and inventory_reads
        assert len(timeouts) == 1 and timeouts[0] <= timeout
        assert len(sdk) == 1, "admission refusal must precede any Advisor SDK dispatch"
        assert owner.runtime.bindings.deadline == entry_deadline
        custody = owner.failure_custody
        assert custody.original is original, "hint admission must retain its original first-party service owner"
        assert original.__cause__ is cause and custody.wrapper is None
        invocation = owner.runtime.live.recorder.invocations[-1]
        assert invocation.status is ComposerToolStatus.SUCCESS and invocation.result_canonical is not None
        assert json.loads(invocation.result_canonical)["status"] == "SERVICE_FAILURE"
        assert [outcome.call.id for outcome in owner.runtime.graph.batch.batch.tool_outcomes] == ["actual_advisor"]


@pytest.mark.asyncio
async def test_plain_hint_timeout_remains_recoverable_with_original_deadline(operation_store, tmp_path, monkeypatch):
    async with _advisor_case(operation_store, tmp_path, monkeypatch, following_call=True) as (owner, _running, sdk):
        original, cause = TimeoutError("ordinary audited Advisor timeout"), RuntimeError("ordinary timeout cause")
        original.__cause__ = cause
        deadline = owner.runtime.bindings.deadline

        async def timeout(**kwargs):
            sdk.append(kwargs)
            raise original

        monkeypatch.setattr(litellm, "acompletion", timeout)
        monkeypatch.setattr(
            owner.service, "_settings", owner.service._settings.model_copy(update={"composer_advisor_timeout_seconds": 1.0})
        )
        await _advance(owner)
        custody = owner.failure_custody
        assert custody.original is None and custody.handled == [original] and not custody.completed
        assert original.__cause__ is cause and len(sdk) == 2
        response = owner.runtime.graph.batch.batch.tool_outcomes[-1].response
        assert response["status"] == "ADVISOR_ERROR"
        assert owner.runtime.bindings.deadline == deadline
        await _advance(owner)
        assert [outcome.call.id for outcome in owner.runtime.graph.batch.batch.tool_outcomes] == ["actual_advisor", "after_advisor"]
        assert owner.runtime.bindings.deadline == deadline and len(sdk) == 2
        await _advance(owner)
        assert owner.p4.committed_outcome is not None
