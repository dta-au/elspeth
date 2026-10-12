"""Private authoring evidence must settle with its existing display cohort."""

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import update

from elspeth.contracts.composer_audit import PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer.audit import (
    BufferingRecorder,
    ComposerToolCancellation,
    audit_envelope,
    begin_dispatch,
    begin_dispatch_or_arg_error,
    finish_arg_error,
    finish_cancelled,
    finish_success,
)
from elspeth.web.composer.audit_storage import private_tool_invocation_draft, redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, build_private_tool_evidence
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.turn_audit import persist_turn_audit
from elspeth.web.sessions._persist_payload import AuditMessageDraft, RedactedToolRow
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes import _helpers
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer.test_compose_loop_persistence import _run_one_turn, _tool_batch_response
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


@pytest.fixture
def private_writer(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Private evidence", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    evidence = build_private_tool_evidence(
        session_id=str(session.id),
        operation_id=context.fence.operation_id,
        operation_epoch=context.fence.operation_epoch,
        tool_call_id="private-call",
        tool_name="create_blob",
        status="success",
        arguments_canonical=canonical_json({"content": "ordinary authored text"}),
        result_canonical=canonical_json({"success": True}),
    )
    yield service, session, context, evidence
    authority.release(context)
    engine.dispose()


def _normal_kwargs(session, context, evidence):
    return {
        "session_id": str(session.id),
        "assistant_content": "",
        "raw_content": None,
        "redacted_assistant_tool_calls": ({"id": "private-call", "function": {"name": "create_blob"}},),
        "redacted_tool_rows": (RedactedToolRow("private-call", "{}", None),),
        "private_tool_evidence": (evidence,),
        "parent_composition_state_id": None,
        "expected_current_state_id": None,
        "writer_principal": "compose_loop",
        "plugin_crash_pending": False,
        "session_operation_context": context,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["missing", "duplicate", "wrong_operation", "extra", "public_writer"])
async def test_normal_private_evidence_requires_exact_bound_set_before_any_write(private_writer, violation):
    service, session, context, evidence = private_writer
    kwargs = _normal_kwargs(session, context, evidence)
    kwargs["private_tool_evidence"] = {
        "missing": (),
        "duplicate": (evidence, evidence),
        "wrong_operation": (replace(evidence, operation_id="another-operation"),),
        "extra": (evidence, replace(evidence, tool_call_id="another-call", evidence_id="another-call")),
        "public_writer": (evidence,),
    }[violation]
    if violation == "public_writer":
        kwargs["writer_principal"] = "user"
    with pytest.raises(AuditIntegrityError):
        await service.persist_compose_turn_async(**kwargs)
    assert await service.get_messages(session.id, limit=None) == []


@pytest.mark.asyncio
async def test_private_insert_failure_rolls_back_assistant_and_tool(private_writer, monkeypatch):
    service, session, context, evidence = private_writer
    original = service._insert_chat_message
    attempted = []

    def insert(conn, **kwargs):
        attempted.append(kwargs["role"])
        if kwargs["role"] == "audit":
            raise AuditIntegrityError("injected private sibling failure")
        return original(conn, **kwargs)

    monkeypatch.setattr(service, "_insert_chat_message", insert)
    with pytest.raises(AuditIntegrityError, match="injected private sibling"):
        await service.persist_compose_turn_async(**_normal_kwargs(session, context, evidence))
    assert attempted == ["assistant", "tool", "audit"]
    assert await service.get_messages(session.id, limit=None) == []


@pytest.mark.asyncio
async def test_private_cohort_exact_replay_deduplicates_and_conflict_fails(private_writer):
    service, session, context, evidence = private_writer

    async def write(value):
        await service.add_messages_atomic(
            session.id,
            (AuditMessageDraft(role="audit", content="Private credential-filtered tool evidence", tool_calls=(value.to_envelope(),)),),
            writer_principal="compose_loop",
            session_operation_context=context,
            audit_only=True,
        )

    await write(evidence)
    await write(evidence)
    assert len(await service.get_messages(session.id, limit=None)) == 1
    with pytest.raises(AuditIntegrityError, match="replay changed"):
        await write(replace(evidence, status="cancelled"))
    assert len(await service.get_messages(session.id, limit=None)) == 1


@pytest.mark.asyncio
async def test_private_cohort_rejects_wrong_epoch_and_public_role(private_writer):
    service, session, context, evidence = private_writer
    for value, role in ((replace(evidence, operation_epoch=evidence.operation_epoch + 1), "audit"), (evidence, "assistant")):
        with pytest.raises(AuditIntegrityError):
            await service.add_messages_atomic(
                session.id,
                (AuditMessageDraft(role=role, content="private", tool_calls=(value.to_envelope(),)),),
                writer_principal="compose_loop",
                session_operation_context=context,
                audit_only=True,
            )
    assert await service.get_messages(session.id, limit=None) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("operation_id", "tampered"), ("operation_epoch", 99), ("evidence_id", "tampered"), ("_kind", "tampered")]
)
async def test_private_replay_finds_and_refuses_tampered_outer_binding(private_writer, field, value):
    service, session, context, evidence = private_writer
    draft = AuditMessageDraft(role="audit", content="private", tool_calls=(evidence.to_envelope(),))
    await service.add_messages_atomic(
        session.id, (draft,), writer_principal="compose_loop", session_operation_context=context, audit_only=True
    )
    tampered = evidence.to_envelope()
    tampered[field] = value
    with service._engine.begin() as conn:
        conn.execute(update(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id)).values(tool_calls=[tampered]))
    with pytest.raises(AuditIntegrityError):
        await service.add_messages_atomic(
            session.id, (draft,), writer_principal="compose_loop", session_operation_context=context, audit_only=True
        )
    assert len(await service.get_messages(session.id, limit=None)) == 1


@pytest.mark.asyncio
async def test_cancelled_alternate_tool_has_authored_arguments_and_absent_result(private_writer):
    service, session, context, _ = private_writer
    invocation = finish_cancelled(
        begin_dispatch("cancelled-call", "create_blob", {"content": "unfinished authored content"}, version_before=0, actor="test"),
        exc=asyncio.CancelledError("arbitrary external cancellation reason"),
    )
    draft = private_tool_invocation_draft(invocation, session_id=str(session.id), session_operation_context=context)
    await service.add_messages_atomic(
        session.id,
        (draft,),
        writer_principal="compose_loop",
        session_operation_context=context,
        audit_only=True,
    )
    (row,) = await service.get_messages(session.id, limit=None)
    restored = ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(session.id))
    assert restored.status == "cancelled"
    assert "unfinished authored content" in restored.arguments.content
    assert restored.result.disposition == "not_returned"
    assert restored.wire_arguments.disposition == "unavailable"
    assert row.tool_call_id is None
    assert row.parent_assistant_id is None


@pytest.mark.asyncio
async def test_normal_private_evidence_retains_monotone_contiguous_cohort(private_writer):
    service, session, context, evidence = private_writer
    await service.persist_compose_turn_async(**_normal_kwargs(session, context, evidence))
    rows = await service.get_messages(session.id, limit=None)
    assert [row.role for row in rows] == ["assistant", "tool", "audit"]
    assert [row.sequence_no for row in rows] == [1, 2, 3]
    assert rows[1].parent_assistant_id == rows[0].id
    assert rows[2].parent_assistant_id is None
    assert rows[2].tool_call_id is None


@pytest.mark.asyncio
async def test_numeric_domain_argument_error_keeps_checkpoint_and_explicit_unavailable_evidence(private_writer):
    service, session, context, _ = private_writer
    call = _AdmittedToolCall("numeric-call", _AdmittedToolFunction("get_pipeline_state", json.dumps({"limit": 2**60})))
    dispatch, _ = begin_dispatch_or_arg_error("numeric-call", "get_pipeline_state", {"limit": 2**60}, version_before=0, actor="test")
    invocation = finish_arg_error(
        dispatch,
        error_class="ToolArgumentError",
        error_category=ToolArgumentErrorCategory.SCHEMA_BOUND,
        error_message="invalid number",
        error_payload={"error": "invalid number"},
    )
    await persist_turn_audit(
        sessions_service=service,
        redaction_telemetry=NoopRedactionTelemetry(),
        tool_outcomes=(_ToolOutcome(call, None, "ToolArgumentError", ToolArgumentErrorCategory.SCHEMA_BOUND, "invalid number", 0, 0),),
        decoded_args_by_call_id={"numeric-call": {"limit": 2**60}},
        private_authorings=(
            PrivateToolAuthoring("numeric-call", "get_pipeline_state", dispatch.arguments_canonical, None, call.function.arguments),
        ),
        private_invocations=(invocation,),
        assistant_message=_AdmittedAssistantMessage(""),
        raw_assistant_content=None,
        assistant_tool_calls=(call,),
        crash_pending=False,
        session_id=str(session.id),
        session_operation_context=context,
        current_state_id=None,
        persisted_tool_call_turn=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        assistant_row_uses_current_dispatch=True,
    )
    (row,) = _private_rows(await service.get_messages(session.id, limit=None))
    evidence = ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(session.id))
    assert evidence.status == "arg_error"
    assert evidence.arguments.disposition == "unavailable"
    assert evidence.wire_arguments.disposition == "unavailable"


@pytest.mark.asyncio
async def test_received_and_accepted_one_shot_candidates_settle_in_actual_planner_cohort(
    tmp_path, tool_context, composer_service_with_real_sessions, result_session_id
):
    from elspeth.web.composer.audit import BufferingRecorder
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    recorder = BufferingRecorder()
    completion = _ScriptedCompletion(
        _response(("list_sources", {}), ("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)
    composer = composer_service_with_real_sessions
    service = composer._sessions_service
    async with acquire_compose_context(service, UUID(result_session_id)) as context:
        await composer._planning_application._persist_pipeline_planner_audit(
            session_id=UUID(result_session_id),
            current_state_id=None,
            llm_calls=recorder.llm_calls,
            planner_attempts=recorder.planner_attempts,
            invocations=recorder.invocations,
            withheld_replies=recorder.withheld_replies,
            private_candidates=recorder.private_candidates,
            session_operation_context=context,
        )
    rows = _private_rows(await service.get_messages(UUID(result_session_id), limit=None))
    evidence = [ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=result_session_id) for row in rows]
    assert [item.status for item in evidence] == ["received_not_dispatched", "accepted"]
    assert all(item.authority_arguments is not None for item in evidence)
    assert all(item.wire_arguments.disposition == "retained" for item in evidence)


def _private_rows(messages):
    return [
        message
        for message in messages
        if message.role == "audit" and message.tool_calls and message.tool_calls[0]["_kind"] == "composer_diagnostic_evidence"
    ]


def test_recorder_binds_owned_receipt_and_public_projection_never_carries_it():
    receipt = PrivateToolAuthoring(
        "receipt-call", "create_blob", canonical_json({"content": "PRIVATE RECEIPT CANARY"}), None, "original wire"
    )
    invocation = finish_success(
        begin_dispatch(
            "receipt-call",
            "create_blob",
            {"filename": "ordinary.txt", "mime_type": "text/plain", "content": "public execution value"},
            version_before=0,
            actor="test",
        ),
        result_payload={
            "success": True,
            "validation": {
                "is_valid": True,
                "errors": [],
                "warnings": [],
                "suggestions": [],
                "semantic_contracts": [],
                "graph_repair_suggestions": [],
            },
            "affected_nodes": [],
            "version": 0,
        },
        version_after=0,
    )
    recorder = BufferingRecorder()
    with pytest.raises(TypeError, match="owned receipt"):
        recorder.record_private_tool_authoring(object())
    recorder.record_private_tool_authoring(receipt)
    with pytest.raises(ValueError, match="another dispatch"):
        recorder.record(replace(invocation, tool_name="another-tool"))
    recorder.record(invocation)
    (bound,) = recorder.invocations
    assert bound.private_authoring is receipt
    assert bound.to_dict() == invocation.to_dict()
    assert audit_envelope(bound) == audit_envelope(invocation)
    assert "PRIVATE RECEIPT CANARY" not in repr(bound)
    assert "PRIVATE RECEIPT CANARY" not in repr(receipt)
    content, envelope = redacted_tool_invocation_content_and_envelope(bound)
    assert "PRIVATE RECEIPT CANARY" not in json.dumps(envelope)
    assert "PRIVATE RECEIPT CANARY" not in content
    assert "private_authoring" not in envelope["invocation"]
    with pytest.raises(AuditIntegrityError, match="closed"):
        recorder.record_private_tool_authoring(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["preflight_crash", "child_cancel"])
async def test_aborted_batch_after_prior_checkpoint_retains_original_inline_receipt_in_alternate_sql(
    composer_service_with_real_sessions, fake_llm_one_set_pipeline_tool_call, result_session_id, monkeypatch, outcome
):
    composer = composer_service_with_real_sessions
    service = composer._sessions_service
    original = await fake_llm_one_set_pipeline_tool_call(None, None)
    arguments = json.loads(original.tool_batch.calls[0].function.arguments)
    pipeline = arguments["pipeline"]
    inline_content = "value\noriginal failed batch receipt\n"
    pipeline["source"]["options"].pop("path")
    pipeline["source"]["inline_blob"] = {"filename": "original.csv", "mime_type": "text/csv", "content": inline_content}
    responses = [
        _tool_batch_response(("prior-durable", "set_metadata", {"patch": {"name": "Prior durable mutation"}})),
        _tool_batch_response(("failed-inline", "set_pipeline", arguments), ("failed-preview", "preview_pipeline", {})),
    ]
    original_failure = RuntimeError("private primary preflight failure") if outcome == "preflight_crash" else asyncio.CancelledError()

    async def completion(**kwargs):
        return responses.pop(0)

    async def fail_preflight(state, **kwargs):
        if outcome == "child_cancel":
            raise original_failure
        raise ComposerRuntimePreflightError.capture(original_failure, state=state, initial_version=state.version)

    monkeypatch.setattr("elspeth.web.composer.provider_gateway._litellm_acompletion", completion)
    monkeypatch.setattr(composer._preflight, "cached_runtime_preflight", fail_preflight)
    async with acquire_compose_context(service, UUID(result_session_id)) as context:
        with pytest.raises(ComposerRuntimePreflightError if outcome == "preflight_crash" else ComposerToolCancellation) as caught:
            await _run_one_turn(composer, llm=None, session_id=result_session_id, session_operation_context=context)
        before = _private_rows(await service.get_messages(UUID(result_session_id), limit=None))
        assert [row.tool_calls[0]["tool_call_id"] for row in before] == ["prior-durable"]
        prior_state_id = before[0].composition_state_id
        assert prior_state_id is not None
        failure = caught.value
        assert [item.tool_call_id for item in failure.tool_invocations] == ["failed-inline", "failed-preview"]
        if outcome == "child_cancel":
            assert failure.original_cancellation is original_failure
            assert failure.__cause__ is original_failure
            assert [item.tool_call_id for item in failure.tool_invocations] == ["failed-inline", "failed-preview"]
        else:
            assert failure.original_exc is original_failure
        await _helpers._persist_turn_audit_cohort(
            service,
            UUID(result_session_id),
            failure.tool_invocations,
            failure.llm_calls,
            tool_composition_state_id=None,
            llm_composition_state_id=None,
            plugin_crash_pending=True,
            session_operation_context=context,
        )
    rows = await service.get_messages(UUID(result_session_id), limit=None)
    evidence = [
        ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=result_session_id) for row in _private_rows(rows)
    ]
    assert sum(item.tool_call_id == "prior-durable" for item in evidence) == 1
    assert (
        next(row.composition_state_id for row in _private_rows(rows) if row.tool_calls[0]["tool_call_id"] == "prior-durable")
        == prior_state_id
    )
    inline = next(item for item in evidence if item.tool_call_id == "failed-inline")
    assert inline.arguments.content == canonical_json(pipeline)
    assert inline.authority_arguments.content == composer_authority_canonical_json(pipeline)
    assert inline.wire_arguments.content == json.dumps(arguments)
    public_rows = [row for row in rows if row not in _private_rows(rows)]
    assert inline_content not in json.dumps(deep_thaw([row.tool_calls for row in public_rows]))
    public_inline = next(
        row.tool_calls[0]["invocation"]
        for row in public_rows
        if row.role == "audit"
        and row.tool_calls
        and row.tool_calls[0].get("_kind") == "audit"
        and row.tool_calls[0]["invocation"]["tool_call_id"] == "failed-inline"
    )
    assert json.loads(public_inline["arguments_canonical"])["source"]["inline_blob"] == "[redacted inline content held for custody]"
    assert public_inline["arguments_hash"] == hashlib.sha256(public_inline["arguments_canonical"].encode()).hexdigest()
    assert public_inline["authority_arguments_hash"] == hashlib.sha256(public_inline["authority_arguments_canonical"].encode()).hexdigest()


@pytest.mark.asyncio
async def test_actual_child_cancellation_joins_private_display_cohort_before_route_reraise(
    tmp_path, composer_service_with_real_sessions, fake_llm_one_set_pipeline_tool_call, monkeypatch
):
    from elspeth.web.sessions import composer_turn
    from elspeth.web.sessions.composer_app_services import composer_app_services
    from tests.unit.web.sessions.test_routes import _make_app
    from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job

    app, service = _make_app(tmp_path)
    composer = composer_service_with_real_sessions
    monkeypatch.setattr(composer, "_sessions_service", service)
    session = await service.create_session("alice", "Child cancel receipt", "local")
    original = await fake_llm_one_set_pipeline_tool_call(None, None)
    arguments = json.loads(original.tool_batch.calls[0].function.arguments)
    pipeline = arguments["pipeline"]
    pipeline["source"]["options"].pop("path")
    pipeline["source"]["inline_blob"] = {
        "filename": "authored.csv",
        "mime_type": "text/csv",
        "content": "value\nroute cancellation receipt\n",
    }
    original_cancel = asyncio.CancelledError()

    async def completion(**kwargs):
        return _tool_batch_response(("route-inline", "set_pipeline", arguments), ("route-preview", "preview_pipeline", {}))

    async def fail_preflight(state, **kwargs):
        raise original_cancel

    class ActualChildCancellingComposer:
        async def compose(self, *args, **kwargs):
            return await _run_one_turn(
                composer,
                llm=None,
                session_id=kwargs["session_id"],
                session_operation_context=kwargs["session_operation_context"],
            )

    app.state.composer_service = ActualChildCancellingComposer()
    monkeypatch.setattr("elspeth.web.composer.provider_gateway._litellm_acompletion", completion)
    monkeypatch.setattr(composer._preflight, "cached_runtime_preflight", fail_preflight)
    async with _running_job(app, service, session.id, kind="compose_message", content="Build it.") as job:
        with pytest.raises(ComposerToolCancellation) as caught:
            await _run(app, job)
        assert caught.value.original_cancellation is original_cancel
        assert job.observation.audit_cohort_durable is True
        assert [item.tool_call_id for item in job.observation.pending_exception_tool_invocations] == ["route-inline", "route-preview"]
        await composer_turn.persist_cancelled_turn_audit(composer_app_services(app), job.running, job.observation)
    rows = await service.get_messages(session.id, limit=None)
    evidence = [
        ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(session.id)) for row in _private_rows(rows)
    ]
    assert [item.tool_call_id for item in evidence] == ["route-inline", "route-preview"]
    assert evidence[0].arguments.content == canonical_json(pipeline)
    assert evidence[0].wire_arguments.content == json.dumps(arguments)
    assert evidence[1].status == "cancelled"
    assert evidence[1].result.disposition == "not_returned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "cache_progress",
        "start_progress",
        "pipeline_proposal",
        "component_proposal",
        "advisor",
        "review_preflight",
        "review_handler",
        "tolerant_preview",
    ],
)
async def test_open_async_dispatch_cancellation_records_current_received_tool(
    composer_service_with_real_sessions, fake_llm_one_set_pipeline_tool_call, result_session_id, monkeypatch, stage
):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.progress import tool_started_progress_event
    from elspeth.web.sessions.models import sessions_table
    from tests.unit.web.composer.test_advisor_checkpoint import _pending_handoff_preflight

    composer = composer_service_with_real_sessions
    service = composer._sessions_service
    cancellation = asyncio.CancelledError()
    hits = 0

    async def cancel(*args, **kwargs):
        nonlocal hits
        hits += 1
        raise cancellation

    async def no_findings(*args, **kwargs):
        return None

    if stage in {"review_preflight", "review_handler"}:
        tool_name = "request_interpretation_review"
        arguments = {
            "affected_node_id": "classify_category",
            "kind": "vague_term",
            "user_term": "category",
            "llm_draft": "billing, outage, or other",
        }
        monkeypatch.setattr(tool_batch, "_pending_review_runtime_findings", cancel if stage == "review_preflight" else no_findings)
        if stage == "review_handler":
            monkeypatch.setattr(composer._session_tools, "_dispatch_session_aware_tool", cancel)
    elif stage == "advisor":
        tool_name = "request_advisor_hint"
        arguments = {
            "trigger": "proactive_security_safety",
            "problem_summary": "Review ordinary candidate",
            "recent_errors": ["ordinary validation issue"],
            "attempted_actions": ["checked schema"],
        }
        monkeypatch.setattr(composer._advisor_checkpoint, "_call_advisor_for_tool", cancel)
    elif stage == "tolerant_preview":
        tool_name, arguments = "preview_pipeline", {}

        async def preflight(*args, **kwargs):
            return await cancel() if kwargs.get("interpretation_tolerant") else _pending_handoff_preflight()

        monkeypatch.setattr(composer._preflight, "cached_runtime_preflight", preflight)
    elif stage in {"pipeline_proposal", "component_proposal"}:
        with service._engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))
        if stage == "pipeline_proposal":
            tool_name = "set_pipeline"
            original = await fake_llm_one_set_pipeline_tool_call(None, None)
            arguments = json.loads(original.tool_batch.calls[0].function.arguments)
            monkeypatch.setattr(service, "create_pipeline_composition_proposal", cancel)
        else:
            tool_name, arguments = "set_metadata", {"patch": {"name": "ordinary candidate"}}
            monkeypatch.setattr(service, "create_composition_proposal", cancel)
    else:
        tool_name, arguments = ("list_sources", {}) if stage == "cache_progress" else ("get_pipeline_state", {})

        async def progress(sink, event):
            if (stage == "cache_progress" and event.headline == "I'm reusing recently checked tool information.") or (
                stage == "start_progress" and event == tool_started_progress_event(tool_name)
            ):
                await cancel()

        monkeypatch.setattr(tool_batch, "emit_progress", progress)
    calls = [("current-cancel", tool_name, arguments)]
    if stage == "cache_progress":
        calls.insert(0, ("cache-seed", tool_name, arguments))

    async def completion(**kwargs):
        return _tool_batch_response(*calls)

    monkeypatch.setattr("elspeth.web.composer.provider_gateway._litellm_acompletion", completion)
    async with acquire_compose_context(service, UUID(result_session_id)) as context:
        with pytest.raises(ComposerToolCancellation) as caught:
            await _run_one_turn(composer, llm=None, session_id=result_session_id, session_operation_context=context)
        assert hits == 1
        assert caught.value.original_cancellation is cancellation
        current = [invocation for invocation in caught.value.tool_invocations if invocation.tool_call_id == "current-cancel"]
        assert len(current) == 1
        assert current[0].status.value == "cancelled"
        assert current[0].private_authoring is not None
        await _helpers._persist_turn_audit_cohort(
            service,
            UUID(result_session_id),
            caught.value.tool_invocations,
            caught.value.llm_calls,
            tool_composition_state_id=None,
            llm_composition_state_id=None,
            plugin_crash_pending=True,
            session_operation_context=context,
        )
    rows = _private_rows(await service.get_messages(UUID(result_session_id), limit=None))
    current_private = [row for row in rows if row.tool_calls[0]["tool_call_id"] == "current-cancel"]
    assert len(current_private) == 1
    evidence = ComposerDiagnosticEvidence.from_envelope(current_private[0].tool_calls[0], expected_session_id=result_session_id)
    assert evidence.status == "cancelled"
    assert evidence.arguments.content == current[0].private_authoring.arguments_canonical


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fatal", "caller_cancel", "caller_cancel_audit_failure"])
async def test_rejected_receipt_then_fatal_assertion_drains_private_audit_before_original_route_failure(
    tmp_path, composer_service_with_real_sessions, fake_llm_one_set_pipeline_tool_call, monkeypatch, mode
):
    from elspeth.web.composer import tool_batch
    from tests.unit.web.sessions.test_routes import _make_app
    from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job

    app, service = _make_app(tmp_path)
    composer = composer_service_with_real_sessions
    monkeypatch.setattr(composer, "_sessions_service", service)
    session = await service.create_session("alice", "Fatal receipt", "local")
    original = await fake_llm_one_set_pipeline_tool_call(None, None)
    arguments = json.loads(original.tool_batch.calls[0].function.arguments)
    pipeline = arguments["pipeline"]
    pipeline["source"]["options"].pop("path")
    pipeline["source"]["inline_blob"] = {"filename": "authored.csv", "mime_type": "text/csv", "content": "value\nfatal batch receipt\n"}
    original_failure = AssertionError("private primary assertion detail")
    original_cause = ValueError("original cause")
    original_failure.__cause__ = original_cause
    real_execute = tool_batch.execute_tool
    hits = 0
    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    release_worker = threading.Event()

    def execute(tool_name, *args, **kwargs):
        nonlocal hits
        if tool_name == "get_pipeline_state":
            hits += 1
            if mode != "fatal":
                loop.call_soon_threadsafe(worker_started.set)
                if not release_worker.wait(5):
                    raise TimeoutError("controlled worker was not released")
            raise original_failure
        return real_execute(tool_name, *args, **kwargs)

    async def completion(**kwargs):
        return _tool_batch_response(("fatal-inline", "set_pipeline", arguments), ("fatal-current", "get_pipeline_state", {}))

    class ActualFatalComposer:
        async def compose(self, *args, **kwargs):
            return await _run_one_turn(
                composer, llm=None, session_id=kwargs["session_id"], session_operation_context=kwargs["session_operation_context"]
            )

    app.state.composer_service = ActualFatalComposer()
    monkeypatch.setattr(tool_batch, "execute_tool", execute)
    monkeypatch.setattr("elspeth.web.composer.provider_gateway._litellm_acompletion", completion)
    if mode == "caller_cancel_audit_failure":
        real_insert = service._insert_chat_message

        def insert(*args, **kwargs):
            if kwargs.get("role") == "audit":
                raise AuditIntegrityError("controlled required audit failure")
            return real_insert(*args, **kwargs)

        monkeypatch.setattr(service, "_insert_chat_message", insert)
    async with _running_job(app, service, session.id, kind="compose_message", content="Build it.") as job:
        if mode == "fatal":
            with pytest.raises(AssertionError) as caught:
                await _run(app, job)
            assert caught.value is original_failure
            assert caught.value.__cause__ is original_cause
        else:
            task = asyncio.create_task(_run(app, job))
            await asyncio.wait_for(worker_started.wait(), timeout=5)
            task.cancel("synthetic caller cancellation")
            await asyncio.sleep(0)
            release_worker.set()
            if mode == "caller_cancel_audit_failure":
                with pytest.raises(AuditIntegrityError, match="controlled required audit failure"):
                    await task
            else:
                with pytest.raises(ComposerToolCancellation) as cancelled:
                    await task
                assert cancelled.value.original_cancellation.args == ("synthetic caller cancellation",)
                assert cancelled.value.__cause__ is original_failure
        assert hits == 1
        assert job.observation.audit_cohort_durable is (mode != "caller_cancel_audit_failure")
    rows = await service.get_messages(session.id, limit=None)
    assert not [row for row in rows if row.role in {"assistant", "tool"}]
    assert await service.get_current_state(session.id) is None
    evidence = [
        ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(session.id)) for row in _private_rows(rows)
    ]
    if mode == "caller_cancel_audit_failure":
        assert evidence == []
        return
    assert [item.tool_call_id for item in evidence] == ["fatal-inline", "fatal-current"]
    assert evidence[0].arguments.content == canonical_json(pipeline)
    assert evidence[0].wire_arguments.content == json.dumps(arguments)
    assert evidence[1].status == "plugin_crash"
    assert evidence[1].result.disposition == "not_returned"
    assert "private primary assertion detail" not in json.dumps(deep_thaw([row.tool_calls for row in rows]))


@pytest.mark.asyncio
async def test_normal_tool_turn_retains_private_authored_content(
    composer_service_with_real_sessions, fake_llm_with_sensitive_tool_call, result_session_id
):
    await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_with_sensitive_tool_call,
        session_id=result_session_id,
    )
    messages = await composer_service_with_real_sessions._sessions_service.get_messages(UUID(result_session_id), limit=None)
    private = _private_rows(messages)
    assert len(private) == 1
    assert "top-secret" in json.dumps(deep_thaw(private[0].tool_calls))
    display = [message for message in messages if message.role == "assistant"]
    assert "top-secret" not in json.dumps(deep_thaw([message.tool_calls for message in display]))


@pytest.mark.asyncio
async def test_alternate_tool_cohort_retains_private_nested_options(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Private evidence", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    invocation = finish_success(
        begin_dispatch(
            "call_private_options",
            "set_source",
            {
                "plugin": "csv",
                "on_success": "out",
                "on_validation_failure": "discard",
                "options": {"path": "ordinary.csv", "prompt_template": "Classify {{ row.category }}."},
            },
            version_before=0,
            actor="composer-web:user:alice",
        ),
        result_payload={
            "success": False,
            "validation": {
                "is_valid": False,
                "errors": [{"component": "source", "severity": "error", "message": "Rejected ordinary authored candidate"}],
                "warnings": [],
                "suggestions": [],
                "semantic_contracts": [],
                "graph_repair_suggestions": [],
            },
            "affected_nodes": [],
            "version": 0,
        },
        version_after=0,
    )
    await _helpers._persist_turn_audit_cohort(
        service,
        session.id,
        (invocation,),
        (),
        tool_composition_state_id=None,
        llm_composition_state_id=None,
        plugin_crash_pending=False,
        session_operation_context=context,
    )
    messages = await service.get_messages(session.id, limit=None)
    private = _private_rows(messages)
    assert len(private) == 1
    assert "Classify {{ row.category }}." in json.dumps(deep_thaw(private[0].tool_calls))
    assert "Rejected ordinary authored candidate" in json.dumps(deep_thaw(private[0].tool_calls))
    assert len(messages) == 2
    authority.release(context)
    engine.dispose()
