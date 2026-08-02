"""Compose-loop Step 1/2/3 unit tests (spec §5.2.1)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select, text, update

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.core.canonical import canonical_json
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json
from elspeth.web.composer.protocol import ComposerPluginCrashError, ComposerServiceError, ToolArgumentError
from elspeth.web.composer.redaction import redact_tool_call_arguments, redact_tool_call_response
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    interpretation_events_table,
    proposal_events_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import ComposerSessionPreferencesRecord, CompositionStateData
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean  # noqa: F401  (autouse end-gate CLEAN stub)


async def _run_one_turn(
    service: ComposerServiceImpl,
    *,
    llm: Any,
    session_id: str,
    current_state_id: str | None = None,
    session_operation_context: SessionOperationContext | None = None,
) -> Any:
    driver = cast(Any, service)
    owned_context = session_operation_context
    acquired_here = owned_context is None
    if owned_context is None:
        owned_context = _acquire_compose_authority(
            service,
            session_id=session_id,
            owner="compose-loop-test",
        )
    try:
        return await driver._run_one_turn_for_test(
            llm=llm,
            session_id=session_id,
            current_state_id=current_state_id,
            session_operation_context=owned_context,
        )
    finally:
        if acquired_here:
            service._require_sessions_service().session_operation_authority.release(owned_context)  # type: ignore[attr-defined]


def _acquire_compose_authority(
    service: ComposerServiceImpl,
    *,
    session_id: str,
    owner: str,
) -> SessionOperationContext:
    sessions_service = service._require_sessions_service()  # type: ignore[attr-defined]
    return sessions_service.session_operation_authority.acquire(
        session_id=UUID(session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=owner,
        lease_seconds=sessions_service.session_operation_lease_seconds,
    )


def _expire_and_take_over_compose_authority(
    service: ComposerServiceImpl,
    *,
    session_id: str,
    successor_owner: str,
) -> SessionOperationContext:
    sessions_service = service._require_sessions_service()  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == session_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    successor = sessions_service.session_operation_authority.acquire(
        session_id=UUID(session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=successor_owner,
        lease_seconds=sessions_service.session_operation_lease_seconds,
    )
    return successor


def _patch_auto_commit_preferences(monkeypatch: pytest.MonkeyPatch, sessions_service: Any) -> None:
    async def _get_composer_preferences(session_id: UUID) -> ComposerSessionPreferencesRecord:
        return ComposerSessionPreferencesRecord(
            session_id=session_id,
            trust_mode="auto_commit",
            density_default="high",
            interpretation_review_disabled=False,
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(sessions_service, "get_composer_preferences", _get_composer_preferences)


def _advisor_tool_call_response(call_id: str, *, extra_args: dict[str, Any] | None = None) -> Any:
    arguments = {
        "trigger": "proactive_security_safety",
        "problem_summary": "stuck on llm config with private schema",
        "recent_errors": [
            "validator rejected the private column",
            "validator rejected the private column",
        ],
        "attempted_actions": [
            "set_pipeline with sensitive options",
            "checked the relevant schema",
        ],
    }
    if extra_args is not None:
        arguments.update(extra_args)
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(
                                name="request_advisor_hint",
                                arguments=json.dumps(arguments),
                            ),
                        )
                    ],
                )
            )
        ],
    )


def _text_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])


def _metadata_tool_response(call_id: str, name: str) -> Any:
    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="set_metadata",
            arguments=json.dumps({"patch": {"name": name}}),
        ),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])


def _tool_batch_response(*calls: tuple[object, str, dict[str, Any]]) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(
                                name=tool_name,
                                arguments=json.dumps(arguments),
                            ),
                        )
                        for call_id, tool_name, arguments in calls
                    ],
                )
            )
        ],
    )


def _record_real_tool_handlers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    handler_calls: list[str] = []
    real_execute_tool = tool_batch_module.execute_tool

    def _record(tool_name: str, *args: Any, **kwargs: Any) -> Any:
        handler_calls.append(tool_name)
        return real_execute_tool(tool_name, *args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", _record)
    return handler_calls


async def _capture_tool_batch_rejection(
    service: ComposerServiceImpl,
    *,
    session_id: str,
    response: Any,
    current_state_id: str | None = None,
) -> BaseException | None:
    responses = [response, _text_response("Done.")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    try:
        await _run_one_turn(
            service,
            llm=_fake_llm,
            session_id=session_id,
            current_state_id=current_state_id,
        )
    except BaseException as exc:
        return exc
    return None


def _assert_no_blob_side_effects(
    service: ComposerServiceImpl,
    *,
    session_id: str,
    tmp_path: Path,
) -> None:
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert conn.execute(select(blobs_table.c.id).where(blobs_table.c.session_id == session_id)).fetchall() == []
    blob_dir = tmp_path / "blobs" / session_id
    assert not blob_dir.exists() or list(blob_dir.iterdir()) == []


def _interpretation_review_node() -> dict[str, Any]:
    """Return a persisted LLM node with one unresolved vague-term slot."""
    state = CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="interpretation_node",
                node_type="transform",
                plugin="llm",
                input="input",
                on_success="out",
                on_error="quarantine",
                options={"prompt_template": "Rate how {{interpretation:cool}} this is."},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Interpretation ID ownership"),
        version=1,
    )
    return state.to_dict()["nodes"][0]


def _unknown_tool_response(call_id: str, *, arguments: dict[str, Any]) -> Any:
    tool_call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="hallucinated_tool",
            arguments=json.dumps(arguments),
        ),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))])


def _advisor_model_response(content: str = "Try setting `provider: azure` with the deployment name.") -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
        model="anthropic/claude-sonnet-4-6",
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45, total_tokens=165),
    )


def test_current_loop_arg_error_tool_row_scrubs_arbitrary_error_message(
    composer_service_with_real_sessions: ComposerServiceImpl,
) -> None:
    canary = "RAW_CURRENT_LOOP_ARG_ERROR_/private/operator/path_sk-secret"
    outcome = SimpleNamespace(
        call=SimpleNamespace(function=SimpleNamespace(name="set_source")),
        error_class="ToolArgumentError",
        error_message=canary,
    )

    serialized = composer_service_with_real_sessions._serialize_response_via_walker(  # type: ignore[attr-defined]
        outcome,
        telemetry=composer_service_with_real_sessions._redaction_telemetry,  # type: ignore[attr-defined]
    )
    payload = json.loads(serialized)

    assert payload["_redaction_status"] == "arg_error"
    assert payload["error_class"] == "ToolArgumentError"
    assert payload["error_message"] == "<redacted-arg-error-message>"
    assert canary not in serialized


@pytest.mark.parametrize(
    ("failure_status", "error_class", "error_message", "expected"),
    [
        (
            ComposerToolStatus.PLUGIN_CRASH,
            "RAW_CLASS_/private/operator/path_sk-secret",
            "RAW_MESSAGE_/private/operator/path_sk-secret",
            {
                "_redaction_status": "plugin_crash",
                "error_class": "<redacted-plugin-crash-class>",
                "error_message": "<redacted-failure-message>",
            },
        ),
        (
            ComposerToolStatus.CANCELLED,
            "CancelledError",
            "RAW_CANCEL_/private/operator/path_sk-secret",
            {
                "_redaction_status": "cancelled",
                "error_class": "CancelledError",
                "error_message": "cancelled",
            },
        ),
    ],
)
def test_current_loop_non_arg_failure_projection_matches_legacy(
    composer_service_with_real_sessions: ComposerServiceImpl,
    failure_status: ComposerToolStatus,
    error_class: str,
    error_message: str,
    expected: dict[str, object],
) -> None:
    outcome = SimpleNamespace(
        call=SimpleNamespace(function=SimpleNamespace(name="set_source")),
        error_class=error_class,
        error_message=error_message,
    )

    serialized = composer_service_with_real_sessions._serialize_response_via_walker(  # type: ignore[attr-defined]
        outcome,
        telemetry=composer_service_with_real_sessions._redaction_telemetry,  # type: ignore[attr-defined]
        failure_status=failure_status,
    )

    assert json.loads(serialized) == expected
    if error_class != expected["error_class"]:
        assert error_class not in serialized
    assert error_message not in serialized


@pytest.mark.asyncio
async def test_current_planner_persistence_rejects_malformed_bound_content_hash(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    arguments = {"source": None, "nodes": [], "edges": [], "outputs": []}
    arguments_canonical = canonical_json(arguments)
    authority_arguments_canonical = composer_authority_canonical_json(arguments)
    result_canonical = canonical_json(
        {
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
            "version": 1,
            "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
            "pipeline_content_hash": "RAW_CURRENT_PLANNER_HASH_/private/operator/path_sk-secret",
        }
    )
    invocation = ComposerToolInvocation(
        tool_call_id="call_current_planner_hash_canary",
        tool_name="set_pipeline",
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode()).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode()).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=0,
        version_after=1,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
        authority_arguments_canonical=authority_arguments_canonical,
        authority_arguments_hash=hashlib.sha256(authority_arguments_canonical.encode()).hexdigest(),
    )

    context = _acquire_compose_authority(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        owner="planner-malformed-hash-test",
    )
    try:
        with pytest.raises(AuditIntegrityError, match="content hash is malformed"):
            await composer_service_with_real_sessions._persist_pipeline_planner_audit(  # type: ignore[attr-defined]
                session_id=UUID(result_session_id),
                current_state_id=None,
                llm_calls=(),
                invocations=(invocation,),
                session_operation_context=context,
            )
    finally:
        composer_service_with_real_sessions._require_sessions_service().session_operation_authority.release(  # type: ignore[attr-defined]
            context
        )


@pytest.mark.asyncio
async def test_planner_audit_takeover_fences_stale_owner_before_message_insert(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    sessions_service = composer_service_with_real_sessions._require_sessions_service()  # type: ignore[attr-defined]
    predecessor = _acquire_compose_authority(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        owner="planner-predecessor",
    )
    successor = _expire_and_take_over_compose_authority(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        successor_owner="planner-successor",
    )
    now = datetime(2026, 8, 2, tzinfo=UTC)
    call = ComposerLLMCall(
        model_requested="test/planner",
        model_returned="test/planner",
        status=ComposerLLMCallStatus.SUCCESS,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        latency_ms=1,
        provider_request_id="planner-takeover",
        messages_hash="m" * 64,
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=None,
        error_message=None,
        temperature=0.0,
        seed=1,
    )

    try:
        with pytest.raises(SessionOperationFenceLost):
            await composer_service_with_real_sessions._persist_pipeline_planner_audit(  # type: ignore[attr-defined]
                session_id=UUID(result_session_id),
                current_state_id=None,
                llm_calls=(call,),
                invocations=(),
                session_operation_context=predecessor,
            )
    finally:
        sessions_service.session_operation_authority.release(successor)

    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            select(chat_messages_table.c.id)
            .where(chat_messages_table.c.session_id == result_session_id)
            .where(chat_messages_table.c.role == "audit")
        ).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_tool_batch_rejects_duplicate_ids_before_real_handlers_or_blob_state_side_effects(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_calls = _record_real_tool_handlers(monkeypatch)
    batch_progress_calls = 0
    real_emit_progress = tool_batch_module.emit_progress

    async def _record_batch_progress(*args: Any, **kwargs: Any) -> Any:
        nonlocal batch_progress_calls
        batch_progress_calls += 1
        return await real_emit_progress(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "emit_progress", _record_batch_progress)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            (
                "call_duplicate",
                "create_blob",
                {
                    "filename": "duplicate-tripwire.txt",
                    "mime_type": "text/plain",
                    "content": "must never be written",
                },
            ),
            (
                "call_duplicate",
                "set_metadata",
                {"patch": {"name": "must never mutate state"}},
            ),
        ),
    )

    assert handler_calls == []
    assert batch_progress_calls == 0
    assert getattr(composer_service_with_real_sessions, "_phase3_last_tool_outcomes", ()) == ()
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert (
            conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == result_session_id)).fetchall()
            == []
        )
        assert (
            conn.execute(
                select(chat_messages_table.c.id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).fetchall()
            == []
        )
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch contains duplicate provider tool-call IDs"


@pytest.mark.asyncio
async def test_tool_batch_rejects_duplicate_ids_before_durable_proposal_creation(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))

    proposal_calls = 0
    real_create_proposal = sessions_service.create_composition_proposal

    async def _record_real_proposal(*args: Any, **kwargs: Any) -> Any:
        nonlocal proposal_calls
        proposal_calls += 1
        return await real_create_proposal(*args, **kwargs)

    monkeypatch.setattr(sessions_service, "create_composition_proposal", _record_real_proposal)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            ("call_duplicate_proposal", "set_metadata", {"patch": {"name": "first"}}),
            ("call_duplicate_proposal", "set_metadata", {"patch": {"name": "second"}}),
        ),
    )

    assert proposal_calls == 0
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert (
            conn.execute(
                select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
            ).fetchall()
            == []
        )
        assert (
            conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).fetchall() == []
        )
    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch contains duplicate provider tool-call IDs"


@pytest.mark.parametrize("proposal_count", [10, 11])
@pytest.mark.asyncio
async def test_explicit_approval_batch_preflights_proposal_cap_before_creation(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    proposal_count: int,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))

    batch_progress_calls = 0
    real_emit_progress = tool_batch_module.emit_progress

    async def _record_batch_progress(*args: Any, **kwargs: Any) -> Any:
        nonlocal batch_progress_calls
        batch_progress_calls += 1
        return await real_emit_progress(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "emit_progress", _record_batch_progress)
    proposal_calls = 0
    real_create_proposal = sessions_service.create_composition_proposal

    async def _record_real_proposal(*args: Any, **kwargs: Any) -> Any:
        nonlocal proposal_calls
        proposal_calls += 1
        return await real_create_proposal(*args, **kwargs)

    monkeypatch.setattr(sessions_service, "create_composition_proposal", _record_real_proposal)
    response = _tool_batch_response(
        *((f"call_proposal_{index}", "set_metadata", {"patch": {"name": f"proposal {index}"}}) for index in range(proposal_count))
    )

    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=response,
    )

    if proposal_count == 10:
        assert caught is None
        expected_proposals = 10
    else:
        assert type(caught) is ComposerServiceError
        assert str(caught) == "Composer produced too many pending tool proposals in one turn (10 maximum)."
        expected_proposals = 0
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        proposal_ids = conn.execute(
            select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
        ).fetchall()
        proposal_event_ids = conn.execute(
            select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)
        ).fetchall()
    assert len(proposal_ids) == expected_proposals
    assert len(proposal_event_ids) == expected_proposals
    assert proposal_calls == expected_proposals
    assert (batch_progress_calls > 0) is (proposal_count == 10)

    if proposal_count == 11:
        retry_caught = await _capture_tool_batch_rejection(
            composer_service_with_real_sessions,
            session_id=result_session_id,
            response=response,
        )
        assert type(retry_caught) is ComposerServiceError
        assert str(retry_caught) == "Composer produced too many pending tool proposals in one turn (10 maximum)."
        assert proposal_calls == 0
        with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
            assert (
                conn.execute(
                    select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
                ).fetchall()
                == []
            )
            assert (
                conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).fetchall()
                == []
            )


@pytest.mark.asyncio
async def test_proposal_attempt_cap_rejects_mixed_schema_and_semantic_invalid_calls_before_dispatch(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))

    handler_calls = _record_real_tool_handlers(monkeypatch)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            *((f"call_valid_attempt_{index}", "set_metadata", {"patch": {"name": f"proposal {index}"}}) for index in range(9)),
            ("call_schema_invalid_attempt", "set_metadata", {}),
            (
                "call_semantic_invalid_attempt",
                "set_pipeline",
                {
                    "source": None,
                    "nodes": [],
                    "edges": [],
                    "outputs": [],
                    "metadata": {"name": "semantically incomplete"},
                },
            ),
        ),
    )

    assert type(caught) is ComposerServiceError
    assert str(caught) == "Composer produced too many pending tool proposals in one turn (10 maximum)."
    assert handler_calls == []
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert (
            conn.execute(
                select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
            ).fetchall()
            == []
        )
        assert (
            conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).fetchall() == []
        )


@pytest.mark.asyncio
async def test_proposal_attempt_cap_excludes_discovery_and_immediate_create_blob(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))

    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            ("call_discovery_boundary", "list_transforms", {}),
            (
                "call_create_blob_boundary",
                "create_blob",
                {
                    "filename": "immediate.txt",
                    "mime_type": "text/plain",
                    "content": "immediate non-proposal content",
                },
            ),
            *((f"call_boundary_proposal_{index}", "set_metadata", {"patch": {"name": f"proposal {index}"}}) for index in range(10)),
        ),
    )

    assert caught is None
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert set(
            conn.execute(
                select(composition_proposals_table.c.tool_call_id).where(composition_proposals_table.c.session_id == result_session_id)
            ).scalars()
        ) == {f"call_boundary_proposal_{index}" for index in range(10)}
        assert (
            len(conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).fetchall())
            == 10
        )


@pytest.mark.asyncio
async def test_proposal_attempt_cap_includes_approval_required_blob_only_mutation(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))

    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            *((f"call_blob_boundary_proposal_{index}", "set_metadata", {"patch": {"name": f"proposal {index}"}}) for index in range(10)),
            ("call_approval_required_blob_only", "delete_blob", {}),
        ),
    )

    assert type(caught) is ComposerServiceError
    assert str(caught) == "Composer produced too many pending tool proposals in one turn (10 maximum)."
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert (
            conn.execute(
                select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
            ).fetchall()
            == []
        )
        assert (
            conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).fetchall() == []
        )


@pytest.mark.parametrize(
    ("call_id", "expected_message"),
    [
        ("", "Composer tool batch contains a blank provider tool-call ID"),
        ("x" * 257, "Composer tool batch contains an oversized provider tool-call ID"),
    ],
)
@pytest.mark.asyncio
async def test_tool_batch_rejects_invalid_id_before_real_handler_or_blob_side_effects(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call_id: str,
    expected_message: str,
) -> None:
    handler_calls = _record_real_tool_handlers(monkeypatch)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            (
                call_id,
                "create_blob",
                {
                    "filename": "invalid-id-tripwire.txt",
                    "mime_type": "text/plain",
                    "content": "must never be written",
                },
            ),
        ),
    )

    assert handler_calls == []
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    assert type(caught) is AuditIntegrityError
    assert str(caught) == expected_message


@pytest.mark.asyncio
async def test_tool_batch_snapshots_calls_before_first_await(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted_call = SimpleNamespace(
        id="call_admitted",
        function=SimpleNamespace(
            name="set_metadata",
            arguments=json.dumps({"patch": {"name": "admitted"}}),
        ),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[admitted_call],
                )
            )
        ],
    )
    responses = [response, _text_response("Done.")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    handler_calls = _record_real_tool_handlers(monkeypatch)
    real_emit_progress = tool_batch_module.emit_progress
    mutated = False

    async def _mutate_provider_call_after_admission(*args: Any, **kwargs: Any) -> Any:
        nonlocal mutated
        if not mutated:
            mutated = True
            admitted_call.id = "call_mutated"
            admitted_call.function.name = "create_blob"
            admitted_call.function.arguments = json.dumps(
                {
                    "filename": "toctou-tripwire.txt",
                    "mime_type": "text/plain",
                    "content": "must never be written",
                }
            )
        return await real_emit_progress(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "emit_progress", _mutate_provider_call_after_admission)

    sessions_service = composer_service_with_real_sessions._require_sessions_service()  # type: ignore[attr-defined]
    operation_context = _acquire_compose_authority(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        owner="final-response-test",
    )
    try:
        result = await _run_one_turn(
            composer_service_with_real_sessions,
            llm=_fake_llm,
            session_id=result_session_id,
            session_operation_context=operation_context,
        )
        tool_state = await sessions_service.get_current_state(UUID(result_session_id))
        assert tool_state is not None
        assert result.final_persisted_state_id == tool_state.id
        settlement = await sessions_service.commit_composition_response(
            session_id=UUID(result_session_id),
            expected_current_state_id=result.final_persisted_state_id,
            state=CompositionStateData(metadata_={"name": "admitted"}),
            assistant_content="Done.",
            raw_content=None,
            session_operation_context=operation_context,
        )
    finally:
        sessions_service.session_operation_authority.release(operation_context)

    assert mutated is True
    assert handler_calls == ["set_metadata"]
    assert result.tool_outcomes[0].call.id == "call_admitted"
    assert result.tool_outcomes[0].call.function.name == "set_metadata"
    assert result.tool_outcomes[0].response.updated_state.metadata.name == "admitted"
    assert result.persisted_assistant_tool_calls[0]["id"] == "call_admitted"
    assert settlement.message.composition_state_id == settlement.state.id
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )


@pytest.mark.asyncio
async def test_tool_batch_snapshots_calls_before_preference_await(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted_call = SimpleNamespace(
        id="call_admitted_before_preferences",
        function=SimpleNamespace(
            name="set_metadata",
            arguments=json.dumps({"patch": {"name": "admitted before preferences"}}),
        ),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[admitted_call],
                )
            )
        ],
    )
    responses = [response, _text_response("Done.")]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    handler_calls = _record_real_tool_handlers(monkeypatch)
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    real_get_preferences = sessions_service.get_composer_preferences
    mutated = False

    async def _mutate_provider_call_during_preferences(session_id: UUID) -> ComposerSessionPreferencesRecord:
        nonlocal mutated
        preferences = await real_get_preferences(session_id)
        mutated = True
        admitted_call.id = "call_mutated_during_preferences"
        admitted_call.function.name = "create_blob"
        admitted_call.function.arguments = json.dumps(
            {
                "filename": "preference-toctou-tripwire.txt",
                "mime_type": "text/plain",
                "content": "must never be written",
            }
        )
        return preferences

    monkeypatch.setattr(
        sessions_service,
        "get_composer_preferences",
        _mutate_provider_call_during_preferences,
    )

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=_fake_llm,
        session_id=result_session_id,
    )

    assert mutated is True
    assert handler_calls == ["set_metadata"]
    assert result.tool_outcomes[0].call.id == "call_admitted_before_preferences"
    assert result.tool_outcomes[0].call.function.name == "set_metadata"
    assert result.tool_outcomes[0].response.updated_state.metadata.name == "admitted before preferences"
    assert result.persisted_assistant_tool_calls[0]["id"] == "call_admitted_before_preferences"
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )


@pytest.mark.asyncio
async def test_tool_batch_rejects_session_reused_id_before_current_turn_proposal_or_blob_effects(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(update(sessions_table).where(sessions_table.c.id == result_session_id).values(trust_mode="explicit_approve"))

    responses = [
        _tool_batch_response(
            ("call_session_reuse", "set_metadata", {"patch": {"name": "prior proposal"}}),
        ),
        _tool_batch_response(
            ("call_fresh_proposal", "set_metadata", {"patch": {"name": "must not propose"}}),
            (
                "call_session_reuse",
                "create_blob",
                {
                    "filename": "session-reuse-tripwire.txt",
                    "mime_type": "text/plain",
                    "content": "must never be written",
                },
            ),
        ),
        _text_response("Done."),
    ]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    handler_calls = _record_real_tool_handlers(monkeypatch)
    proposal_calls = 0
    real_create_proposal = sessions_service.create_composition_proposal

    async def _record_real_proposal(*args: Any, **kwargs: Any) -> Any:
        nonlocal proposal_calls
        proposal_calls += 1
        return await real_create_proposal(*args, **kwargs)

    monkeypatch.setattr(sessions_service, "create_composition_proposal", _record_real_proposal)

    caught: BaseException | None = None
    try:
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=_fake_llm,
            session_id=result_session_id,
        )
    except BaseException as exc:
        caught = exc

    assert handler_calls == []
    assert proposal_calls == 1
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert list(
            conn.execute(
                select(composition_proposals_table.c.tool_call_id)
                .where(composition_proposals_table.c.session_id == result_session_id)
                .order_by(composition_proposals_table.c.created_at)
            ).scalars()
        ) == ["call_session_reuse"]
        assert list(
            conn.execute(
                select(chat_messages_table.c.tool_call_id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role == "tool")
                .order_by(chat_messages_table.c.sequence_no)
            ).scalars()
        ) == ["call_session_reuse"]
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch reuses a provider tool-call ID already persisted in this session"


@pytest.mark.asyncio
async def test_tool_batch_rejects_id_reserved_by_prior_proposal_without_tool_row(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    session_id = UUID(result_session_id)
    context = await sessions_service._run_sync(
        lambda: sessions_service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions_service.session_operation_owner_instance_id,
            lease_seconds=sessions_service.session_operation_lease_seconds,
        )
    )
    try:
        await sessions_service.create_composition_proposal(
            session_id=session_id,
            tool_call_id="call_orphaned_proposal",
            tool_name="set_metadata",
            summary="Prior durable proposal.",
            rationale="Tripwire for proposal-only ID ownership.",
            affects=("metadata",),
            arguments_json={"patch": {"name": "prior"}},
            arguments_redacted_json={"patch": {"name": "prior"}},
            base_state_id=None,
            actor="test",
            session_operation_context=context,
        )
    finally:
        await sessions_service._run_sync(sessions_service.session_operation_authority.release, context)
    handler_calls = _record_real_tool_handlers(monkeypatch)

    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            (
                "call_orphaned_proposal",
                "create_blob",
                {
                    "filename": "proposal-only-reuse-tripwire.txt",
                    "mime_type": "text/plain",
                    "content": "must never be written",
                },
            ),
        ),
    )

    assert handler_calls == []
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert list(
            conn.execute(
                select(composition_proposals_table.c.tool_call_id).where(composition_proposals_table.c.session_id == result_session_id)
            ).scalars()
        ) == ["call_orphaned_proposal"]
        assert (
            conn.execute(
                select(chat_messages_table.c.id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).fetchall()
            == []
        )
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch reuses a provider tool-call ID already persisted in this session"


@pytest.mark.parametrize(
    "durable_choice",
    [
        InterpretationChoice.PENDING,
        InterpretationChoice.ACCEPTED_AS_DRAFTED,
    ],
)
@pytest.mark.asyncio
async def test_tool_batch_rejects_id_owned_only_by_prior_interpretation_event(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_choice: InterpretationChoice,
) -> None:
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    session_uuid = UUID(result_session_id)
    state = await sessions_service.save_composition_state(
        session_uuid,
        CompositionStateData(
            nodes=[_interpretation_review_node()],
            metadata_={"name": "Interpretation ID ownership", "description": ""},
            is_valid=True,
        ),
        provenance="tool_call",
    )
    event = await sessions_service.create_pending_interpretation_event(
        session_id=session_uuid,
        composition_state_id=state.id,
        affected_node_id="interpretation_node",
        tool_call_id="call_interpretation_orphan",
        user_term="cool",
        kind=InterpretationKind.VAGUE_TERM,
        llm_draft="Stylish and appealing.",
        model_identifier="test/composer",
        model_version="test-v1",
        provider="test",
        composer_skill_hash="a" * 64,
    )
    current_state_id = state.id
    if durable_choice is InterpretationChoice.ACCEPTED_AS_DRAFTED:
        event, resolved_state = await sessions_service.resolve_interpretation_event(
            session_id=session_uuid,
            event_id=event.id,
            choice=durable_choice,
            amended_value=None,
            actor="user:test",
            runtime_model_identifier=None,
            runtime_model_version=None,
        )
        current_state_id = resolved_state.id
    assert event.choice is durable_choice
    assert [
        row.choice
        for row in await sessions_service.list_interpretation_events(
            session_uuid,
            status="all",
        )
    ] == [durable_choice]

    handler_calls = _record_real_tool_handlers(monkeypatch)
    progress_calls = 0
    real_emit_progress = tool_batch_module.emit_progress

    async def _record_progress(*args: Any, **kwargs: Any) -> Any:
        nonlocal progress_calls
        progress_calls += 1
        return await real_emit_progress(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "emit_progress", _record_progress)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        current_state_id=str(current_state_id),
        response=_tool_batch_response(
            (
                "call_interpretation_orphan",
                "create_blob",
                {
                    "filename": "interpretation-reuse-tripwire.txt",
                    "mime_type": "text/plain",
                    "content": "must never be written",
                },
            ),
        ),
    )

    assert handler_calls == []
    assert progress_calls == 0
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert (
            conn.execute(
                select(chat_messages_table.c.id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).fetchall()
            == []
        )
        assert (
            conn.execute(
                select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
            ).fetchall()
            == []
        )
        assert list(
            conn.execute(
                select(interpretation_events_table.c.tool_call_id).where(interpretation_events_table.c.session_id == result_session_id)
            ).scalars()
        ) == ["call_interpretation_orphan"]
    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch reuses a provider tool-call ID already persisted in this session"


@pytest.mark.asyncio
async def test_tool_batch_accepts_provider_id_at_exact_length_boundary(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_calls = _record_real_tool_handlers(monkeypatch)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            ("x" * 256, "get_pipeline_state", {}),
        ),
    )

    assert caught is None
    assert handler_calls == ["get_pipeline_state"]


@pytest.mark.parametrize(
    ("call_id", "expected_message"),
    [
        (7, "Composer tool batch contains a non-string provider tool-call ID"),
        ("\u2003", "Composer tool batch contains a blank provider tool-call ID"),
    ],
)
@pytest.mark.asyncio
async def test_tool_batch_rejects_non_string_or_unicode_whitespace_id(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    call_id: object,
    expected_message: str,
) -> None:
    handler_calls = _record_real_tool_handlers(monkeypatch)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=_tool_batch_response(
            (call_id, "get_pipeline_state", {}),
        ),
    )

    assert handler_calls == []
    assert type(caught) is AuditIntegrityError
    assert str(caught) == expected_message


@pytest.mark.asyncio
async def test_tool_batch_rejects_missing_id_with_leak_safe_audit_error(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            function=SimpleNamespace(
                                name="get_pipeline_state",
                                arguments="{}",
                            )
                        )
                    ],
                )
            )
        ],
    )
    handler_calls = _record_real_tool_handlers(monkeypatch)
    caught = await _capture_tool_batch_rejection(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        response=response,
    )

    assert handler_calls == []
    assert type(caught) is AuditIntegrityError
    assert str(caught) == "Composer tool batch is missing a provider tool-call ID"


@pytest.mark.asyncio
async def test_step1_three_tools_all_succeed_accumulates_three_outcomes(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_three_tool_calls: Any,
    result_session_id: str,
) -> None:
    """Three successful tools produce three outcomes with response set."""

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_three_tool_calls,
        session_id=result_session_id,
    )

    outcomes = result.tool_outcomes_for_assertion
    assert len(outcomes) == 3
    assert all(outcome.error_class is None for outcome in outcomes)
    assert all(outcome.response is not None for outcome in outcomes)
    assert outcomes[0].post_version <= outcomes[1].post_version <= outcomes[2].post_version


@pytest.mark.asyncio
async def test_step1_tool_argument_error_continues_loop(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_tool_argument_error_on_second: Any,
    result_session_id: str,
) -> None:
    """ToolArgumentError on call 2 of 3 records an error and continues."""

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_tool_argument_error_on_second,
        session_id=result_session_id,
    )

    outcomes = result.tool_outcomes_for_assertion
    assert len(outcomes) == 3
    assert outcomes[0].error_class is None
    assert outcomes[1].error_class == "ToolArgumentError"
    assert outcomes[2].error_class is None


@pytest.mark.asyncio
async def test_current_loop_schema_valid_semantic_arg_error_persists_only_closed_argument_projection(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename_canary = "RAW_CURRENT_FILENAME_/private/operator/path_sk-secret.csv"
    description_canary = "RAW_CURRENT_DESCRIPTION_/private/operator/path_sk-secret"
    arguments = {
        "filename": filename_canary,
        "mime_type": "text/csv",
        "content": "safe content",
        "description": description_canary,
    }

    def _semantic_arg_error(*_args: Any, **_kwargs: Any) -> ToolResult:
        raise ToolArgumentError(argument="content", expected="semantic constraint", actual_type="str")

    monkeypatch.setattr("elspeth.web.composer.tool_batch.execute_tool", _semantic_arg_error)
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call_current_create_blob_semantic_arg_error",
                                function=SimpleNamespace(name="create_blob", arguments=json.dumps(arguments)),
                            )
                        ],
                    )
                )
            ]
        ),
        _text_response("Recovered after the semantic argument error."),
    ]

    async def _llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=_llm,
        session_id=result_session_id,
    )

    expected_arguments = {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "ToolArgumentError",
        "field_count": 4,
    }
    assert len(result.persisted_assistant_tool_calls) == 1
    persisted_call = result.persisted_assistant_tool_calls[0]
    assert json.loads(persisted_call["function"]["arguments"]) == expected_arguments
    expected_canonical = canonical_json(expected_arguments)
    _content, audit_envelope = redacted_tool_invocation_content_and_envelope(result.tool_invocations[0])
    persisted_invocation = audit_envelope["invocation"]
    assert persisted_invocation["arguments_canonical"] == expected_canonical
    assert persisted_invocation["arguments_hash"] == hashlib.sha256(expected_canonical.encode()).hexdigest()
    persisted_blob = json.dumps(
        {
            "assistant_tool_calls": result.persisted_assistant_tool_calls,
            "tool_rows": result.persisted_tool_row_content,
        },
        sort_keys=True,
    )
    assert filename_canary not in persisted_blob
    assert description_canary not in persisted_blob


@pytest.mark.asyncio
async def test_current_loop_non_object_arg_error_matches_durable_projection_and_hash(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call_current_set_source_non_object",
                                function=SimpleNamespace(name="set_source", arguments="[]"),
                            )
                        ],
                    )
                )
            ]
        ),
        _text_response("Recovered after the non-object argument error."),
    ]

    async def _llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=_llm,
        session_id=result_session_id,
    )

    expected_arguments = {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "TypeError",
        "field_count": 1,
    }
    expected_canonical = canonical_json(expected_arguments)
    assert len(result.persisted_assistant_tool_calls) == 1
    persisted_call = result.persisted_assistant_tool_calls[0]
    assert json.loads(persisted_call["function"]["arguments"]) == expected_arguments

    _content, audit_envelope = redacted_tool_invocation_content_and_envelope(result.tool_invocations[0])
    persisted_invocation = audit_envelope["invocation"]
    assert persisted_invocation["arguments_canonical"] == expected_canonical
    assert persisted_invocation["arguments_hash"] == hashlib.sha256(expected_canonical.encode()).hexdigest()


@pytest.mark.asyncio
async def test_step1_assertion_error_reraises_before_persist(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_assertion_error_on_second: Any,
    result_session_id: str,
) -> None:
    """AssertionError is re-raised before any compose-turn DB write runs."""

    with pytest.raises(AssertionError):
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=fake_llm_assertion_error_on_second,
            session_id=result_session_id,
        )

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            text("SELECT id FROM chat_messages WHERE session_id = :session_id AND role IN ('assistant', 'tool')"),
            {"session_id": result_session_id},
        ).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_step1_plugin_bug_captures_crash_breaks_loop(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_runtime_error_on_second: Any,
    result_session_id: str,
) -> None:
    """RuntimeError on call 2 of 3 records the crash and skips call 3."""

    with pytest.raises(ComposerPluginCrashError) as excinfo:
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=fake_llm_runtime_error_on_second,
            session_id=result_session_id,
        )

    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    outcomes = composer_service_with_real_sessions._phase3_last_tool_outcomes  # type: ignore[attr-defined]
    assert len(outcomes) == 2
    assert outcomes[0].error_class is None
    assert outcomes[1].error_class == "RuntimeError"
    assert outcomes[1].error_message == "RuntimeError"
    assert "phase3 synthetic runtime error" not in (outcomes[1].error_message or "")


@pytest.mark.asyncio
async def test_current_loop_plugin_crash_with_invalid_arguments_uses_closed_class_and_matches_durable_projection(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_class_canary = "RAW_PLUGIN_CLASS_/private/operator/path_sk-secret"
    plugin_error = type(error_class_canary, (RuntimeError,), {})

    def _plugin_crash(*_args: Any, **_kwargs: Any) -> ToolResult:
        raise plugin_error("RAW_PLUGIN_MESSAGE_/private/operator/path_sk-secret")

    monkeypatch.setattr("elspeth.web.composer.tool_batch.execute_tool", _plugin_crash)
    invalid_arguments = {
        "plugin": "csv",
        "options": [],
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="call_invalid_set_source_plugin_crash",
                                function=SimpleNamespace(name="set_source", arguments=json.dumps(invalid_arguments)),
                            )
                        ],
                    )
                )
            ]
        )
    ]

    async def _llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    with pytest.raises(ComposerPluginCrashError):
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=_llm,
            session_id=result_session_id,
        )

    expected_arguments = {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "<redacted-plugin-crash-class>",
        "field_count": 4,
    }
    persisted_call = composer_service_with_real_sessions._phase3_last_redacted_assistant_tool_calls[0]  # type: ignore[attr-defined]
    assert json.loads(persisted_call["function"]["arguments"]) == expected_arguments
    arguments_canonical = canonical_json(invalid_arguments)
    durable_invocation = ComposerToolInvocation(
        tool_call_id="call_invalid_set_source_plugin_crash",
        tool_name="set_source",
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode()).hexdigest(),
        result_canonical=None,
        result_hash=None,
        status=ComposerToolStatus.PLUGIN_CRASH,
        error_class=error_class_canary,
        error_message=error_class_canary,
        version_before=1,
        version_after=None,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    _content, audit_envelope = redacted_tool_invocation_content_and_envelope(durable_invocation)
    persisted_invocation = audit_envelope["invocation"]
    expected_canonical = canonical_json(expected_arguments)
    assert persisted_invocation["arguments_canonical"] == expected_canonical
    assert persisted_invocation["arguments_hash"] == hashlib.sha256(expected_canonical.encode()).hexdigest()
    assert error_class_canary not in json.dumps(
        {
            "assistant_tool_calls": composer_service_with_real_sessions._phase3_last_redacted_assistant_tool_calls,  # type: ignore[attr-defined]
            "invocation": persisted_invocation,
        },
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_step2_redacts_via_manifest_walker(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_with_sensitive_tool_call: Any,
    result_session_id: str,
) -> None:
    """Assistant tool_calls are redacted with the Phase 2 manifest walker."""

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_with_sensitive_tool_call,
        session_id=result_session_id,
    )

    expected = tuple(
        redact_tool_call_arguments(
            outcome.call.function.name,
            json.loads(outcome.call.function.arguments),
            telemetry=composer_service_with_real_sessions._redaction_telemetry,  # type: ignore[attr-defined]
        )
        for outcome in result.tool_outcomes
    )
    persisted = tuple(json.loads(call["function"]["arguments"]) for call in result.persisted_assistant_tool_calls)
    assert persisted == expected


@pytest.mark.asyncio
async def test_step2_redacts_response_with_summarizer(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_summarizer_active: Any,
    result_session_id: str,
) -> None:
    """Tool-row content is serialized from redact_tool_call_response output."""

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_summarizer_active,
        session_id=result_session_id,
    )

    expected_content = redact_tool_call_response(
        tool_name=result.tool_outcomes[0].call.function.name,
        response=result.tool_outcomes[0].response.to_dict(),
        telemetry=composer_service_with_real_sessions._redaction_telemetry,  # type: ignore[attr-defined]
    )
    assert json.loads(result.persisted_tool_row_content[0]) == expected_content


@pytest.mark.asyncio
async def test_step2_persists_intercepted_advisor_tool_call_rows(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intercepted advisor calls still persist assistant tool_calls and tool rows."""

    service = composer_service_with_real_sessions
    service._settings = service._settings.model_copy(  # type: ignore[attr-defined]
        update={
            "composer_advisor_max_calls_per_compose": 3,
        }
    )
    responses = [
        _advisor_tool_call_response("call_advisor_phase3"),
        _text_response("Done."),
    ]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    async def _fake_advisor(**_kwargs: Any) -> Any:
        return _advisor_model_response()

    monkeypatch.setattr("elspeth.web.composer.service._litellm_acompletion", _fake_advisor)

    result = await _run_one_turn(
        service,
        llm=_fake_llm,
        session_id=result_session_id,
    )

    advisor_invocations = [inv for inv in result.tool_invocations if inv.tool_name == "request_advisor_hint"]
    assert len(advisor_invocations) == 1
    assert len(result.persisted_assistant_tool_calls) == 1
    assert len(result.persisted_tool_row_content) == 1
    persisted_call = result.persisted_assistant_tool_calls[0]
    assert persisted_call["id"] == "call_advisor_phase3"
    assert persisted_call["function"]["name"] == "request_advisor_hint"
    persisted_args = json.loads(persisted_call["function"]["arguments"])
    assert persisted_args["problem_summary"].startswith("<advisor-problem-summary:")
    persisted_content = json.loads(result.persisted_tool_row_content[0])
    assert persisted_content["status"] == "SUCCESS"
    assert persisted_content["guidance"] == "<redacted>"
    assert persisted_content["model"] == "<redacted-response-text>"
    assert "anthropic/claude-sonnet-4-6" not in result.persisted_tool_row_content[0]

    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            select(
                chat_messages_table.c.role,
                chat_messages_table.c.tool_calls,
                chat_messages_table.c.tool_call_id,
                chat_messages_table.c.content,
            )
            .where(chat_messages_table.c.session_id == result_session_id)
            .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            .order_by(chat_messages_table.c.sequence_no)
        ).mappings()
        persisted_rows = list(rows)

    assert [row["role"] for row in persisted_rows] == ["assistant", "tool"]
    assert persisted_rows[0]["tool_calls"][0]["id"] == "call_advisor_phase3"
    assert persisted_rows[0]["tool_calls"][0]["function"]["name"] == "request_advisor_hint"
    assert persisted_rows[1]["tool_call_id"] == "call_advisor_phase3"
    assert json.loads(persisted_rows[1]["content"])["guidance"] == "<redacted>"


@pytest.mark.asyncio
async def test_step2_redacts_intercepted_advisor_unknown_arguments_before_persist(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advisor ARG_ERROR rows must not mirror unknown LLM argument values."""

    service = composer_service_with_real_sessions
    raw_extra_context = "RAW_EXTRA_CONTEXT: private traceback and source excerpt"
    responses = [
        _advisor_tool_call_response(
            "call_advisor_extra_arg",
            extra_args={"full_context": raw_extra_context},
        ),
        _text_response("Done."),
    ]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    async def _fake_advisor(**_kwargs: Any) -> Any:
        return _advisor_model_response()

    monkeypatch.setattr("elspeth.web.composer.service._litellm_acompletion", _fake_advisor)

    result = await _run_one_turn(
        service,
        llm=_fake_llm,
        session_id=result_session_id,
    )

    assert len(result.persisted_assistant_tool_calls) == 1
    persisted_call = result.persisted_assistant_tool_calls[0]
    persisted_args = json.loads(persisted_call["function"]["arguments"])
    assert persisted_args == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "ValueError",
        "field_count": 5,
    }
    persisted_blob = json.dumps(
        {
            "assistant_tool_calls": result.persisted_assistant_tool_calls,
            "tool_rows": result.persisted_tool_row_content,
        },
        sort_keys=True,
    )
    assert "full_context" not in persisted_blob
    assert raw_extra_context not in persisted_blob


@pytest.mark.asyncio
async def test_step2_unknown_tool_canary_is_absent_from_actual_persistence(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    canary_key = "private_unknown_argument"
    canary_value = "UNKNOWN_TOOL_PERSISTENCE_CANARY_91c36b"
    responses = [
        _unknown_tool_response("call_unknown_canary", arguments={canary_key: canary_value}),
        _text_response("Recovered after the unknown tool failure."),
    ]

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    result = await _run_one_turn(
        composer_service_with_real_sessions,
        llm=_fake_llm,
        session_id=result_session_id,
    )

    assert len(result.persisted_assistant_tool_calls) == 1
    persisted_call = result.persisted_assistant_tool_calls[0]
    assert json.loads(persisted_call["function"]["arguments"]) == {"_redaction_status": "unknown_tool"}
    persisted_content = json.loads(result.persisted_tool_row_content[0])
    assert persisted_content == {
        "_redaction_status": "unknown_tool",
        "success": False,
        "data": {"error": "Unknown tool"},
    }

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = list(
            conn.execute(
                select(
                    chat_messages_table.c.role,
                    chat_messages_table.c.content,
                    chat_messages_table.c.tool_calls,
                )
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
                .order_by(chat_messages_table.c.sequence_no)
            ).mappings()
        )

    durable_blob = json.dumps([dict(row) for row in rows], sort_keys=True)
    result_blob = json.dumps([invocation.to_dict() for invocation in result.tool_invocations], sort_keys=True)
    assert canary_key not in durable_blob
    assert canary_value not in durable_blob
    assert canary_key not in result_blob
    assert canary_value not in result_blob
    assert "Unknown tool" in durable_blob


@pytest.mark.asyncio
async def test_step2_registered_tool_missing_manifest_fails_before_persistence(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import elspeth.web.composer.redaction as redaction_module

    manifest_without_set_metadata = MappingProxyType(
        {name: entry for name, entry in redaction_module.MANIFEST.items() if name != "set_metadata"}
    )
    monkeypatch.setattr(redaction_module, "MANIFEST", manifest_without_set_metadata)
    response = _metadata_tool_response("call_manifest_drift", "must-not-persist")

    async def _fake_llm(_messages: Any, _tools: Any) -> Any:
        return response

    with pytest.raises(AuditIntegrityError, match="missing from the redaction manifest"):
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=_fake_llm,
            session_id=result_session_id,
        )

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            select(chat_messages_table.c.id)
            .where(chat_messages_table.c.session_id == result_session_id)
            .where(chat_messages_table.c.role.in_(("assistant", "tool")))
        ).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_step2_preserves_absent_raw_content_as_none(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_tool_call_with_no_content: Any,
    result_session_id: str,
) -> None:
    """Missing assistant content remains NULL in raw_content."""

    await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_tool_call_with_no_content,
        session_id=result_session_id,
    )

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    audit_outcome = composer_service_with_real_sessions._phase3_last_audit_outcome  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            text("SELECT raw_content FROM chat_messages WHERE id = :id"),
            {"id": audit_outcome.assistant_id},
        ).one()
    assert row.raw_content is None


@pytest.mark.asyncio
async def test_step2_first_tool_turn_uses_existing_current_state_id(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_two_tool_calls: Any,
    result_session_id: str,
) -> None:
    """First tool-call persistence must guard against the current state row."""

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    state_record = await sessions_service.save_composition_state(
        result_session_id,
        CompositionStateData(is_valid=False),
        provenance="session_seed",
    )

    await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_two_tool_calls,
        session_id=result_session_id,
        current_state_id=str(state_record.id),
    )

    assert composer_service_with_real_sessions._phase3_last_expected_current_state_id == str(state_record.id)  # type: ignore[attr-defined]
    audit_outcome = composer_service_with_real_sessions._phase3_last_audit_outcome  # type: ignore[attr-defined]
    assert audit_outcome.current_state_id == str(state_record.id)


@pytest.mark.asyncio
async def test_step2_dispatches_one_persist_compose_turn_async_per_turn(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_two_tool_calls: Any,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tool-call turn is committed by one persist_compose_turn_async call.

    The invariant is the service call boundary, not the incidental number
    of SQLAlchemy transactions opened by adjacent preference or audit
    bookkeeping.  Count the production persistence method directly so
    legitimate neighbouring DB reads/writes do not make this test brittle.
    """

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    _patch_auto_commit_preferences(monkeypatch, sessions_service)
    persist_calls = 0
    original_persist = sessions_service.persist_compose_turn_async

    async def _count_persist_call(*args: Any, **kwargs: Any) -> Any:
        nonlocal persist_calls
        persist_calls += 1
        return await original_persist(*args, **kwargs)

    monkeypatch.setattr(sessions_service, "persist_compose_turn_async", _count_persist_call)

    await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_two_tool_calls,
        session_id=result_session_id,
    )

    assert persist_calls == 1


@pytest.mark.asyncio
async def test_cancellation_during_sync_tool_waits_for_result_audit_persist(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled awaiter must not split a sync side effect from P4 persist."""

    service = composer_service_with_real_sessions
    sessions_service = service._sessions_service  # type: ignore[attr-defined]
    _patch_auto_commit_preferences(monkeypatch, sessions_service)

    loop = asyncio.get_running_loop()
    worker_started = asyncio.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    worker_invocations = 0

    def _blocking_tool(*args: Any, **_kwargs: Any) -> ToolResult:
        nonlocal worker_invocations
        worker_invocations += 1
        state = cast(Any, args[2])
        loop.call_soon_threadsafe(worker_started.set)
        if not release_worker.wait(timeout=5.0):
            raise TimeoutError("test worker was never released")
        worker_finished.set()
        return ToolResult(
            success=True,
            updated_state=replace(state, version=state.version + 1),
            validation=ValidationSummary(
                is_valid=True,
                errors=(),
                warnings=(),
                suggestions=(),
                semantic_contracts=(),
            ),
            affected_nodes=(),
        )

    monkeypatch.setattr("elspeth.web.composer.tool_batch.execute_tool", _blocking_tool)

    llm_calls = 0

    async def _llm(_messages: Any, _tools: Any) -> Any:
        nonlocal llm_calls
        llm_calls += 1
        if llm_calls == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    id="call_cancel_during_worker",
                                    function=SimpleNamespace(
                                        name="set_metadata",
                                        arguments=json.dumps({"patch": {"name": "Committed before cancel"}}),
                                    ),
                                ),
                                SimpleNamespace(
                                    id="call_must_not_start_after_cancel",
                                    function=SimpleNamespace(
                                        name="set_metadata",
                                        arguments=json.dumps({"patch": {"name": "Must not run"}}),
                                    ),
                                ),
                            ],
                        )
                    )
                ]
            )
        return _text_response("must not be reached after cancellation")

    compose_task = asyncio.create_task(
        _run_one_turn(
            service,
            llm=_llm,
            session_id=result_session_id,
        )
    )
    await asyncio.wait_for(worker_started.wait(), timeout=2.0)
    compose_task.cancel()
    await asyncio.sleep(0)

    try:
        assert not compose_task.done(), "cancellation escaped while the synchronous tool still owned an in-flight side effect"
    finally:
        release_worker.set()
        await asyncio.to_thread(worker_finished.wait, 5.0)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(compose_task, timeout=5.0)

    assert worker_invocations == 1, "deferred cancellation must not start another tool"
    assert llm_calls == 1, "deferred cancellation must stop before another model turn"
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        persisted_rows = list(
            conn.execute(
                select(
                    chat_messages_table.c.role,
                    chat_messages_table.c.tool_calls,
                    chat_messages_table.c.tool_call_id,
                    chat_messages_table.c.content,
                )
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
                .order_by(chat_messages_table.c.sequence_no)
            ).mappings()
        )

    assert [row["role"] for row in persisted_rows] == ["assistant", "tool"]
    assert len(persisted_rows[0]["tool_calls"]) == 1
    assert persisted_rows[0]["tool_calls"][0]["id"] == "call_cancel_during_worker"
    assert persisted_rows[1]["tool_call_id"] == "call_cancel_during_worker"
    assert json.loads(persisted_rows[1]["content"])["success"] is True
    assert await sessions_service.get_current_state(UUID(result_session_id)) is not None


@pytest.mark.asyncio
async def test_takeover_during_tool_work_fences_stale_p4_before_any_audit_or_state_write(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = composer_service_with_real_sessions
    sessions_service = service._require_sessions_service()  # type: ignore[attr-defined]
    _patch_auto_commit_preferences(monkeypatch, sessions_service)
    predecessor = _acquire_compose_authority(
        service,
        session_id=result_session_id,
        owner="tool-predecessor",
    )
    worker_started = threading.Event()
    release_worker = threading.Event()
    real_execute_tool = tool_batch_module.execute_tool

    def _blocking_tool(*args: Any, **kwargs: Any) -> ToolResult:
        worker_started.set()
        if not release_worker.wait(timeout=5.0):
            raise TimeoutError("test worker was never released")
        return real_execute_tool(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", _blocking_tool)
    responses = [
        _metadata_tool_response("call_stale_after_takeover", "must-not-persist"),
        _text_response("must not reach a successor model turn"),
    ]

    async def _llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    compose_task = asyncio.create_task(
        _run_one_turn(
            service,
            llm=_llm,
            session_id=result_session_id,
            session_operation_context=predecessor,
        )
    )
    assert await asyncio.to_thread(worker_started.wait, 5.0)
    successor = _expire_and_take_over_compose_authority(
        service,
        session_id=result_session_id,
        successor_owner="tool-successor",
    )
    try:
        release_worker.set()
        with pytest.raises(SessionOperationFenceLost):
            await asyncio.wait_for(compose_task, timeout=5.0)
    finally:
        release_worker.set()
        if not compose_task.done():
            compose_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await compose_task
        sessions_service.session_operation_authority.release(successor)

    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        chat_rows = conn.execute(
            select(chat_messages_table.c.id)
            .where(chat_messages_table.c.session_id == result_session_id)
            .where(chat_messages_table.c.role.in_(("assistant", "tool")))
        ).fetchall()
        state_rows = conn.execute(
            select(composition_states_table.c.id).where(composition_states_table.c.session_id == result_session_id)
        ).fetchall()
    assert chat_rows == []
    assert state_rows == []


@pytest.mark.asyncio
async def test_deferred_cancellation_survives_child_failure() -> None:
    """A child failure after cancellation is deferred must not swallow the cancel.

    When a disconnect or external cancellation arrives while the shielded
    dispatch/persist section runs and the child then raises (e.g. an audit
    persistence failure), the exception from ``asyncio.shield(task)`` would
    bypass ``deferred``. Python never redelivers a caught CancelledError on
    its own, so the route would finish on the child's error path with the
    task's cancellation requests still pending — swallowing an operator or
    shutdown cancel. Cancellation must win; the child failure rides along
    as ``__cause__`` for diagnosis.
    """
    from elspeth.web.composer.service import _await_tool_turn_with_deferred_cancellation

    cancellation_requested = asyncio.Event()
    proceed_to_fail = asyncio.Event()

    async def child() -> str:
        await proceed_to_fail.wait()
        raise RuntimeError("audit persistence failed")

    captured: dict[str, BaseException] = {}

    async def awaiter() -> None:
        try:
            await _await_tool_turn_with_deferred_cancellation(
                child(),
                cancellation_requested=cancellation_requested,
            )
        except BaseException as exc:
            captured["exc"] = exc
            raise
        raise AssertionError("unreachable — the child never succeeds")

    task = asyncio.get_running_loop().create_task(awaiter())
    await asyncio.sleep(0)  # awaiter parked on the shield
    task.cancel()
    # The helper catches the CancelledError, defers it, and re-awaits the
    # shielded child; cancellation_requested is the deterministic sync point.
    await asyncio.wait_for(cancellation_requested.wait(), timeout=2.0)
    proceed_to_fail.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    exc = captured["exc"]
    assert isinstance(exc, asyncio.CancelledError), f"the child failure replaced the deferred cancellation: {exc!r}"
    assert isinstance(exc.__cause__, RuntimeError), "the child failure must stay diagnosable as the cancellation's __cause__"
    assert task.cancelled(), "the awaiting task must finish as genuinely cancelled"


@pytest.mark.asyncio
async def test_step2_does_not_call_legacy_add_message_inside_loop(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_two_tool_calls: Any,
    result_session_id: str,
    add_message_spy: list[str],
) -> None:
    """The compose loop does not use SessionService.add_message for tool rows."""

    await _run_one_turn(
        composer_service_with_real_sessions,
        llm=fake_llm_two_tool_calls,
        session_id=result_session_id,
    )

    assert not any(frame.endswith(":_compose_loop") for frame in add_message_spy)


@pytest.mark.asyncio
async def test_step2_plugin_crash_carries_failed_turn_metadata(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_runtime_error_on_second: Any,
    result_session_id: str,
) -> None:
    """Plugin crashes raised after dispatch expose the persisted assistant id."""

    with pytest.raises(ComposerPluginCrashError) as excinfo:
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=fake_llm_runtime_error_on_second,
            session_id=result_session_id,
        )

    failed_turn = excinfo.value.failed_turn
    assert failed_turn is not None
    assert failed_turn.assistant_message_id is not None
    assert failed_turn.tool_calls_attempted == 3


@pytest.mark.asyncio
async def test_step2_audit_integrity_error_carries_failed_turn_metadata(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_two_tool_calls: Any,
    result_session_id: str,
    inject_commit_OperationalError: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit failures from the single dispatch keep route-visible turn context."""

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    _patch_auto_commit_preferences(monkeypatch, sessions_service)
    # Phase 5b Task 5 follow-on: skip the F-5c skill_markdown_history
    # upsert so the next-commit-OperationalError listener catches the
    # persist_compose_turn_async commit (the test's actual target), not
    # the audit-archive upsert that fires once per service instance.
    composer_service_with_real_sessions._skill_markdown_history_upserted = True  # type: ignore[attr-defined]
    context = _acquire_compose_authority(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        owner="audit-integrity-failure-test",
    )
    inject_commit_OperationalError(sessions_service._engine)  # type: ignore[attr-defined]

    try:
        with pytest.raises(AuditIntegrityError) as excinfo:
            await _run_one_turn(
                composer_service_with_real_sessions,
                llm=fake_llm_two_tool_calls,
                session_id=result_session_id,
                session_operation_context=context,
            )
    finally:
        sessions_service.session_operation_authority.release(context)

    assert excinfo.value.failed_turn is not None
    assert excinfo.value.failed_turn.assistant_message_id is None
    assert excinfo.value.failed_turn.tool_calls_attempted == 2
    assert excinfo.value.failed_turn.tool_responses_persisted == 0


@pytest.mark.asyncio
async def test_plugin_crash_unwind_commit_failure_remains_unpersisted_and_retains_current_invocations(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_runtime_error_on_second: Any,
    result_session_id: str,
    inject_commit_OperationalError: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rolled-back unwind write cannot suppress the crash audit evidence."""

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    _patch_auto_commit_preferences(monkeypatch, sessions_service)
    composer_service_with_real_sessions._skill_markdown_history_upserted = True  # type: ignore[attr-defined]

    persisted_flags: list[bool] = []
    original_persist = composer_service_with_real_sessions._persist_turn_audit  # type: ignore[attr-defined]

    async def _capture_persist_outcome(**kwargs: Any) -> Any:
        outcome = await original_persist(**kwargs)
        persisted_flags.append(outcome.persisted_tool_call_turn)
        return outcome

    monkeypatch.setattr(composer_service_with_real_sessions, "_persist_turn_audit", _capture_persist_outcome)
    context = _acquire_compose_authority(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        owner="plugin-crash-audit-failure-test",
    )
    inject_commit_OperationalError(sessions_service._engine)  # type: ignore[attr-defined]

    try:
        with pytest.raises(ComposerPluginCrashError) as excinfo:
            await _run_one_turn(
                composer_service_with_real_sessions,
                llm=fake_llm_runtime_error_on_second,
                session_id=result_session_id,
                session_operation_context=context,
            )
    finally:
        sessions_service.session_operation_authority.release(context)

    assert persisted_flags == [False]
    assert [invocation.tool_call_id for invocation in excinfo.value.tool_invocations] == ["call_ok", "call_crash"]
    assert excinfo.value.failed_turn is not None
    assert excinfo.value.failed_turn.assistant_message_id is None
    assert excinfo.value.failed_turn.tool_calls_attempted == 3
    assert excinfo.value.failed_turn.tool_responses_persisted == 0

    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            text("SELECT role, tool_call_id FROM chat_messages WHERE session_id = :session_id AND role IN ('assistant', 'tool')"),
            {"session_id": result_session_id},
        ).fetchall()
    assert rows == []


@pytest.mark.asyncio
async def test_unwind_failure_retains_only_current_turn_after_committed_prefix(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    inject_commit_OperationalError: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Earlier committed invocations are not duplicated on unwind recovery."""

    from elspeth.web.composer import tool_batch

    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    _patch_auto_commit_preferences(monkeypatch, sessions_service)
    composer_service_with_real_sessions._skill_markdown_history_upserted = True  # type: ignore[attr-defined]

    original_execute = tool_batch.execute_tool
    execute_calls = 0

    def _execute(tool_name: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal execute_calls
        execute_calls += 1
        if execute_calls == 2:
            raise RuntimeError("second-turn plugin crash")
        return original_execute(tool_name, *args, **kwargs)

    monkeypatch.setattr(tool_batch, "execute_tool", _execute)

    original_persist = sessions_service.persist_compose_turn_async
    persist_calls = 0

    async def _fail_second_persist(**kwargs: Any) -> Any:
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 2:
            inject_commit_OperationalError(sessions_service._engine)  # type: ignore[attr-defined]
        return await original_persist(**kwargs)

    monkeypatch.setattr(sessions_service, "persist_compose_turn_async", _fail_second_persist)

    responses = [
        _metadata_tool_response("call_committed", "committed"),
        _metadata_tool_response("call_unpersisted_crash", "crash"),
    ]

    async def _llm(_messages: Any, _tools: Any) -> Any:
        return responses.pop(0)

    with pytest.raises(ComposerPluginCrashError) as excinfo:
        await _run_one_turn(
            composer_service_with_real_sessions,
            llm=_llm,
            session_id=result_session_id,
        )

    assert persist_calls == 2
    assert [invocation.tool_call_id for invocation in excinfo.value.tool_invocations] == ["call_unpersisted_crash"]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            text(
                "SELECT role, tool_call_id FROM chat_messages WHERE session_id = :session_id AND role IN ('assistant', 'tool') ORDER BY sequence_no"
            ),
            {"session_id": result_session_id},
        ).fetchall()
    assert rows == [("assistant", None), ("tool", "call_committed")]
