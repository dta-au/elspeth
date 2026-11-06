"""Tool-batch admission and dispatch through the shared owners (spec §5.2.1).

The V1 compose loop that drove these as Steps 1/2/3 is removed. A PRIMARY call
is admitted by ``ComposerServiceImpl._call_llm_outcome_before_deadline`` and its
batch runs in ``tool_batch.run_tool_batch``, the owners Continuation reuses.
Turn persistence (P4) belongs to Continuation's ``ContinuationLiveP4`` and is
tested with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardToolArgumentParts
from elspeth.core.canonical import canonical_json
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer import turn_audit as turn_audit_module
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerPluginCrashError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion, _MalformedLLMResponseError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.coordination.contracts import SessionOperationContext
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    interpretation_events_table,
    proposal_events_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import ComposerSessionPreferencesRecord, CompositionStateData
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean  # noqa: F401  (autouse end-gate CLEAN stub)


async def _run_one_turn(
    service: ComposerServiceImpl,
    *,
    llm: Any | None,
    session_id: str,
    current_state_id: str | None = None,
    session_operation_context: Any = None,
) -> Any:
    driver = cast(Any, service)
    if llm is not None:
        with patch.object(service._provider_gateway, "_call_llm", new=llm):
            return await driver._run_one_turn_for_test(
                session_id=session_id,
                current_state_id=current_state_id,
                session_operation_context=session_operation_context,
            )
    return await driver._run_one_turn_for_test(
        session_id=session_id,
        current_state_id=current_state_id,
        session_operation_context=session_operation_context,
    )


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


def _raw_text_response(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])


def _text_response(content: str) -> _AdmittedLLMCompletion:
    return _admit_composer_llm_completion(_raw_text_response(content))


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


async def _dispatch_admitted_batch(
    service: ComposerServiceImpl,
    *,
    session_id: str,
    response: Any = None,
    llm: Any | None = None,
    initial_state: CompositionState | None = None,
    current_state_id: str | None = None,
    session_operation_context: SessionOperationContext | None = None,
    recorder: BufferingRecorder | None = None,
) -> Any:
    """One PRIMARY call and its tool batch through the shared owners Continuation reuses.

    ``ComposerServiceImpl._call_llm_outcome_before_deadline`` admits the raw SDK
    completion (``provider_gateway`` -> ``tool_batch._admit_tool_batch``) and
    records the provider call; ``tool_batch.run_tool_batch`` (through the
    ``dispatch_tool_batch`` adapter) then runs the batch preflights and every
    call stage. Turn persistence (P4) is not run: on this branch it is owned by
    Continuation's ``ContinuationLiveP4``. ``llm`` replaces the gateway's
    ``_call_llm`` (an internal, already-admitted completion); otherwise
    ``response`` is the raw SDK completion.
    """
    from elspeth.contracts.composer_mutation import ComposerMutationCeiling
    from elspeth.web.composer.anti_anchor import AntiAnchorTracker
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
    from tests.unit.web.sessions.test_continuation_prompt_producers import dispatch_tool_batch

    state = initial_state or CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    sessions = service._require_sessions_service()
    plugin_snapshot, policy_catalog = service._policy_context.build(None)
    recorder = recorder if recorder is not None else BufferingRecorder()
    deadline = asyncio.get_running_loop().time() + service._timeout_seconds
    responses = [response]

    async def _raw_completion(**_kwargs: Any) -> Any:
        return responses.pop(0)

    seam = (
        patch.object(service._provider_gateway, "_call_llm", new=llm)
        if llm is not None
        else patch("elspeth.web.composer.provider_gateway._litellm_acompletion", new=_raw_completion)
    )
    with seam:
        call_model = await service._call_llm_outcome_before_deadline(
            [{"role": "user", "content": "one tool batch through the shared owners"}],
            composer_loop_tool_definitions(service._planner_dialect),
            state,
            state.version,
            deadline,
            recorder,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
        )
    # The request ceiling is read after admission, as the batch's own
    # preference read is: admission must already have snapshotted the calls.
    preferences = await sessions.get_composer_preferences(UUID(session_id))
    ceiling = ComposerMutationCeiling(session_id, str(uuid4()), preferences.trust_mode)
    outcome, _advisor_calls = await dispatch_tool_batch(
        service,
        call_model=call_model,
        mutation_ceiling=ceiling,
        state=state,
        last_validation=None,
        last_runtime_preflight=None,
        llm_messages=[],
        recorder=recorder,
        anti_anchor=AntiAnchorTracker(),
        discovery_cache={},
        runtime_preflight_cache=service._preflight.new_cache(),
        session_id=session_id,
        session_operation_context=session_operation_context,
        user_id=None,
        user_message_id=None,
        user_message_content="one tool batch through the shared owners",
        current_state_id=current_state_id,
        actor="composer-test",
        initial_version=state.version,
        deadline=deadline,
        progress=None,
        session_scope=session_id,
        advisor_calls_used=0,
        cancellation_requested=asyncio.Event(),
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
    )
    return outcome


async def _capture_tool_batch_rejection(
    service: ComposerServiceImpl,
    *,
    session_id: str,
    response: Any,
    current_state_id: str | None = None,
) -> BaseException | None:
    """Return the refusal raised while admitting or dispatching ``response``, else None.

    The batch runs under a real COMPOSE session-operation context, as every
    Continuation dispatch does.
    """
    try:
        async with acquire_compose_context(service._require_sessions_service(), UUID(session_id)) as context:
            await _dispatch_admitted_batch(
                service, session_id=session_id, response=response, current_state_id=current_state_id, session_operation_context=context
            )
    except BaseException as exc:
        return exc
    return None


@pytest.mark.parametrize("replace_identity", (False, True), ids=("stable", "replace-plugin"))
@pytest.mark.asyncio
async def test_field_mapper_patch_batch_uses_owned_identity_and_refuses_stale_snapshot(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    monkeypatch: pytest.MonkeyPatch,
    replace_identity: bool,
) -> None:
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.sessions.converters import state_from_record

    sessions = composer_service_with_real_sessions._sessions_service
    assert sessions is not None
    service = ComposerServiceImpl.for_trained_operator(
        catalog=create_catalog_service(), settings=composer_service_with_real_sessions._settings, sessions_service=sessions
    )
    _patch_auto_commit_preferences(monkeypatch, sessions)
    node = {
        "id": "rename_customer",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "output",
        "on_error": "discard",
        "options": {"mapping": {"customer_code": "customer_id"}, "schema": {"mode": "observed"}},
    }
    source_path = service._settings.data_dir / "blobs" / result_session_id / "customers.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("customer_code,customer_key\nC1,C1\n")
    sources = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "on_validation_failure": "discard",
            "options": {"path": str(source_path), "schema": {"mode": "fixed", "fields": ["customer_code: str", "customer_key: str"]}},
        }
    }
    outputs = [
        {
            "name": "output",
            "plugin": "csv",
            "on_write_failure": "discard",
            "options": {"path": "outputs/customer_ids.csv", "schema": {"mode": "observed"}},
        }
    ]
    saved = await sessions.save_composition_state(
        UUID(result_session_id),
        CompositionStateData(
            sources=sources, nodes=[node], outputs=outputs, metadata_={"name": "synthetic", "description": ""}, is_valid=False
        ),
        provenance="session_seed",
    )
    before_messages = await sessions.get_messages(UUID(result_session_id), limit=None)
    before_versions = await sessions.get_state_versions(UUID(result_session_id))
    calls = [
        ("rename-metadata", "set_metadata", {"patch": {"name": "Stable identity"}}),
        ("patch-mapping", "patch_node_options", {"node_id": node["id"], "patch": {"mapping": {"customer_key": "customer_id"}}}),
    ]
    if replace_identity:
        calls[0] = (
            "replace-plugin",
            "upsert_node",
            {**node, "plugin": "llm", "options": {"schema": {"mode": "observed"}}},
        )
        calls[1][2]["patch"]["mapping"] = {"api_key": "private_value"}
    handler_calls = _record_real_tool_handlers(monkeypatch)
    responses = [_admit_composer_llm_completion(_tool_batch_response(*calls))]

    async def internal_completion(_messages: Any, _tools: Any, **_kwargs: Any) -> _AdmittedLLMCompletion:
        return responses.pop(0)

    async def run() -> Any:
        async with acquire_compose_context(sessions, UUID(result_session_id)) as context:
            return await _dispatch_admitted_batch(
                service,
                llm=internal_completion,
                session_id=result_session_id,
                current_state_id=str(saved.id),
                initial_state=state_from_record(saved),
                session_operation_context=context,
            )

    if replace_identity:
        with pytest.raises(CredentialMaterialRefused) as caught:
            await run()
        assert caught.value.surface == "composer_tool_wire"
        assert handler_calls == []
        assert await sessions.get_messages(UUID(result_session_id), limit=None) == before_messages
        assert await sessions.get_state_versions(UUID(result_session_id)) == before_versions
    else:
        outcome = await run()
        assert handler_calls == ["set_metadata", "patch_node_options"]
        assert outcome.plugin_crash is None
        assert [item.response.success for item in outcome.tool_outcomes] == [True, True]
        # The batch's state is what P4 publishes as the new current state.
        assert outcome.state.nodes[0].id == node["id"]
        assert deep_thaw(outcome.state.nodes[0].options)["mapping"] == {"customer_key": "customer_id"}


def _assert_provider_tool_batch_rejection(caught: BaseException | None, expected_message: str) -> None:
    assert type(caught) is _MalformedLLMResponseError
    assert str(caught) == f"LLM tool batch failed admission: {expected_message}"
    assert type(caught.__cause__) is AuditIntegrityError
    assert str(caught.__cause__) == expected_message


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


@pytest.mark.asyncio
async def test_mixed_internal_tool_batch_refuses_atomically_before_any_handler_or_persistence(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "sk-" + "a" * 24
    completion = _admit_composer_llm_completion(
        _tool_batch_response(
            (
                "call_clean_blob",
                "create_blob",
                {"filename": "must-not-exist.txt", "mime_type": "text/plain", "content": "ordinary"},
            ),
            ("call_credential", "set_metadata", {"patch": {"description": candidate}}),
        )
    )

    async def _internal_completion(
        _messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        prepared_input: PreparedProviderInput | None = None,
    ) -> _AdmittedLLMCompletion:
        return completion

    handler_calls = _record_real_tool_handlers(monkeypatch)
    sessions_service = composer_service_with_real_sessions._sessions_service
    assert sessions_service is not None
    before_messages = await sessions_service.get_messages(UUID(result_session_id))
    before_state = await sessions_service.get_current_state(UUID(result_session_id))

    with pytest.raises(CredentialMaterialRefused) as caught:
        await _dispatch_admitted_batch(
            composer_service_with_real_sessions,
            llm=_internal_completion,
            session_id=result_session_id,
        )

    assert caught.value.surface == "composer_tool_wire"
    assert candidate not in repr(caught.value.to_payload())
    assert handler_calls == []
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    assert await sessions_service.get_current_state(UUID(result_session_id)) == before_state
    after_messages = await sessions_service.get_messages(UUID(result_session_id))
    assert after_messages == before_messages
    assert candidate not in repr(after_messages)
    with cast(Any, sessions_service)._engine.connect() as conn:
        assert (
            conn.execute(
                select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
            ).all()
            == []
        )
        assert conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).all() == []
        assert (
            conn.execute(
                select(chat_messages_table.c.id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).all()
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_field",
    [
        "content",
        "tool_call_id",
        "tool_name",
        "reasoning_content",
        "reasoning_details",
        "thinking_blocks",
        "model_returned",
        "provider_request_id",
        "finish_reason",
        "provider_served",
    ],
)
async def test_internal_completion_control_metadata_refuses_atomically_before_any_effect(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_field: str,
) -> None:
    candidate = "ghp_" + "a" * 36
    completion = _admit_composer_llm_completion(
        _tool_batch_response(
            (
                "call_clean_blob",
                "create_blob",
                {"filename": "must-not-exist.txt", "mime_type": "text/plain", "content": "ordinary"},
            ),
            ("call_clean_metadata", "set_metadata", {"patch": {"description": "ordinary"}}),
        )
    )
    if credential_field == "content":
        completion = replace(completion, message=replace(completion.message, content=candidate))
    elif credential_field in {"tool_call_id", "tool_name"}:
        calls = list(completion.tool_batch.calls)
        contaminated = calls[1]
        if credential_field == "tool_call_id":
            contaminated = replace(contaminated, id=candidate)
        else:
            contaminated = replace(contaminated, function=replace(contaminated.function, name=candidate))
        calls[1] = contaminated
        completion = replace(
            completion,
            tool_batch=replace(
                completion.tool_batch,
                calls=tuple(calls),
                call_ids=frozenset(call.id for call in calls),
            ),
        )
    else:
        metadata_value: object = candidate
        if credential_field == "reasoning_details":
            metadata_value = {"detail": candidate}
        elif credential_field == "thinking_blocks":
            metadata_value = [{"type": "thinking", "thinking": candidate, "signature": "synthetic-native-signature"}]
        completion = replace(
            completion,
            provider_metadata=replace(completion.provider_metadata, **{credential_field: metadata_value}),
        )

    async def _internal_completion(
        _messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        prepared_input: PreparedProviderInput | None = None,
    ) -> _AdmittedLLMCompletion:
        return completion

    handler_calls = _record_real_tool_handlers(monkeypatch)
    sessions_service = composer_service_with_real_sessions._sessions_service
    assert sessions_service is not None
    before_messages = await sessions_service.get_messages(UUID(result_session_id))
    before_state = await sessions_service.get_current_state(UUID(result_session_id))

    with pytest.raises(CredentialMaterialRefused) as caught:
        await _dispatch_admitted_batch(
            composer_service_with_real_sessions,
            llm=_internal_completion,
            session_id=result_session_id,
        )

    expected_surface = "composer_provider_thinking" if credential_field == "thinking_blocks" else "composer_provider_response"
    assert caught.value.surface == expected_surface
    assert candidate not in repr(caught.value.to_payload())
    llm_calls = caught.value.__dict__["llm_calls"]
    assert len(llm_calls) == 1
    assert llm_calls[0].status.value == "malformed_response"
    assert llm_calls[0].error_message == "credential_material_rejected"
    assert candidate not in canonical_json([call.to_dict() for call in llm_calls])
    assert handler_calls == []
    _assert_no_blob_side_effects(
        composer_service_with_real_sessions,
        session_id=result_session_id,
        tmp_path=tmp_path,
    )
    assert await sessions_service.get_current_state(UUID(result_session_id)) == before_state
    after_messages = await sessions_service.get_messages(UUID(result_session_id))
    assert after_messages == before_messages
    assert candidate not in repr(after_messages)
    with cast(Any, sessions_service)._engine.connect() as conn:
        assert (
            conn.execute(
                select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == result_session_id)
            ).all()
            == []
        )
        assert conn.execute(select(proposal_events_table.c.id).where(proposal_events_table.c.session_id == result_session_id)).all() == []
        assert (
            conn.execute(
                select(chat_messages_table.c.id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).all()
            == []
        )


@pytest.mark.asyncio
async def test_internal_no_tool_completion_is_refused_before_audit_or_history(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    candidate = "ghp_" + "a" * 36
    completion = _text_response("ordinary")
    completion = replace(completion, message=replace(completion.message, content=candidate))

    async def _internal_completion(
        _messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        prepared_input: PreparedProviderInput | None = None,
    ) -> _AdmittedLLMCompletion:
        return completion

    sessions_service = composer_service_with_real_sessions._sessions_service
    assert sessions_service is not None
    before_messages = await sessions_service.get_messages(UUID(result_session_id))
    before_state = await sessions_service.get_current_state(UUID(result_session_id))

    with pytest.raises(CredentialMaterialRefused) as caught:
        await _dispatch_admitted_batch(
            composer_service_with_real_sessions,
            llm=_internal_completion,
            session_id=result_session_id,
        )

    assert caught.value.surface == "composer_provider_response"
    llm_calls = caught.value.__dict__["llm_calls"]
    assert len(llm_calls) == 1
    assert llm_calls[0].status.value == "malformed_response"
    assert llm_calls[0].error_message == "credential_material_rejected"
    assert candidate not in canonical_json([call.to_dict() for call in llm_calls])
    assert await sessions_service.get_current_state(UUID(result_session_id)) == before_state
    assert await sessions_service.get_messages(UUID(result_session_id)) == before_messages
    with cast(Any, sessions_service)._engine.connect() as conn:
        assert (
            conn.execute(
                select(chat_messages_table.c.id)
                .where(chat_messages_table.c.session_id == result_session_id)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
            ).all()
            == []
        )


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


def test_current_loop_arg_error_tool_row_scrubs_arbitrary_error_message(
    composer_service_with_real_sessions: ComposerServiceImpl,
) -> None:
    canary = "RAW_CURRENT_LOOP_ARG_ERROR_/private/operator/path_sk-secret"
    outcome = SimpleNamespace(
        call=SimpleNamespace(function=SimpleNamespace(name="set_source")),
        error_class="ToolArgumentError",
        error_category=ToolArgumentErrorCategory.MODEL_VALIDATION,
        error_message=canary,
    )

    serialized = turn_audit_module._serialize_response_via_walker(
        outcome,
        telemetry=composer_service_with_real_sessions._redaction_telemetry,  # type: ignore[attr-defined]
    )
    payload = json.loads(serialized)

    assert payload["_redaction_status"] == "arg_error"
    assert payload["error_class"] == "ToolArgumentError"
    assert payload["error_category"] == "model_validation"
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

    serialized = turn_audit_module._serialize_response_via_walker(
        outcome,
        telemetry=composer_service_with_real_sessions._redaction_telemetry,  # type: ignore[attr-defined]
        failure_status=failure_status,
    )

    assert json.loads(serialized) == expected
    if error_class != expected["error_class"]:
        assert error_class not in serialized
    assert error_message not in serialized


@pytest.mark.asyncio
async def test_surviving_tool_writer_rejects_malformed_bound_content_hash(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.sessions.routes._helpers import _persist_tool_invocations

    arguments = {
        "source": {"plugin": "null", "on_success": "rows", "on_validation_failure": "discard"},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
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

    sessions = composer_service_with_real_sessions._sessions_service
    assert sessions is not None
    session_uuid = UUID(result_session_id)
    async with acquire_compose_context(sessions, session_uuid) as compose_context:
        with sessions._engine.connect() as connection:
            before = [
                dict(row)
                for row in connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).mappings()
            ]
        with pytest.raises(AuditIntegrityError, match="content hash is malformed"):
            await _persist_tool_invocations(
                sessions,
                session_uuid,
                (invocation,),
                None,
                plugin_crash_pending=False,
                session_operation_context=compose_context,
            )
        with sessions._engine.connect() as connection:
            refused = [
                dict(row)
                for row in connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).mappings()
            ]
        assert refused == before

        # Syntactic projection control; this is not a computed graph digest.
        valid_hash = "0123456789abcdef" * 4
        valid_result = json.loads(result_canonical)
        valid_result["pipeline_content_hash"] = valid_hash
        valid_result_canonical = canonical_json(valid_result)
        valid = replace(
            invocation,
            result_canonical=valid_result_canonical,
            result_hash=hashlib.sha256(valid_result_canonical.encode()).hexdigest(),
        )
        (binding,) = await _persist_tool_invocations(
            sessions,
            session_uuid,
            (valid,),
            None,
            plugin_crash_pending=False,
            session_operation_context=compose_context,
        )
        with sessions._engine.connect() as connection:
            persisted = [
                dict(row)
                for row in connection.execute(
                    select(chat_messages_table)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .order_by(chat_messages_table.c.sequence_no)
                ).mappings()
            ]
        assert persisted[: len(before)] == before
        private_row, public_row = persisted[len(before) :]
        assert private_row["role"] == public_row["role"] == "audit"
        assert private_row["writer_principal"] == public_row["writer_principal"] == "compose_loop"
        assert private_row["composition_state_id"] is public_row["composition_state_id"] is None
        assert public_row["sequence_no"] == private_row["sequence_no"] + 1
        (private_envelope,) = private_row["tool_calls"]
        evidence = ComposerDiagnosticEvidence.from_envelope(
            private_envelope,
            expected_session_id=result_session_id,
            expected_operation_id=compose_context.fence.operation_id,
            expected_tool_call_id=invocation.tool_call_id,
        )
        assert evidence.operation_epoch == compose_context.fence.operation_epoch
        assert evidence.tool_name == "set_pipeline" and evidence.status == "success"
        assert evidence.arguments.disposition == "retained"
        assert evidence.arguments.content == arguments_canonical
        assert evidence.authority_arguments is not None
        assert evidence.authority_arguments.content == authority_arguments_canonical
        assert evidence.result.disposition == "retained"
        assert evidence.result.content == valid_result_canonical
        assert evidence.result.retained_sha256 == valid.result_hash

        (public_envelope,) = public_row["tool_calls"]
        assert public_envelope["_kind"] == "audit"
        public_invocation = public_envelope["invocation"]
        public_result = json.loads(public_row["content"])
        assert public_result["pipeline_content_hash_schema"] == "composer.pipeline-dispatch-result.v1"
        assert public_result["pipeline_content_hash"] == valid_hash
        assert public_invocation["result_canonical"] == public_row["content"]
        assert public_invocation["result_hash"] == hashlib.sha256(public_row["content"].encode()).hexdigest()
        assert public_invocation["arguments_hash"] == hashlib.sha256(public_invocation["arguments_canonical"].encode()).hexdigest()
        assert type(binding) is PipelineDispatchAuditBinding
        assert binding == PipelineDispatchAuditBinding.from_persisted_envelope(public_envelope)
        assert binding.tool_call_id == invocation.tool_call_id and binding.tool_name == "set_pipeline"
        assert binding.status is ComposerToolStatus.SUCCESS
        assert binding.result_hash == public_invocation["result_hash"]
        canary = json.loads(result_canonical)["pipeline_content_hash"]
        assert all(canary not in row["content"] and canary not in json.dumps(row["tool_calls"]) for row in (private_row, public_row))


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
    _assert_provider_tool_batch_rejection(caught, "Composer tool batch contains duplicate provider tool-call IDs")


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
    _assert_provider_tool_batch_rejection(caught, "Composer tool batch contains duplicate provider tool-call IDs")


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
        assert type(caught) is ComposerConvergenceError
        assert dict(caught.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
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
        assert type(retry_caught) is ComposerConvergenceError
        assert dict(retry_caught.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
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

    assert type(caught) is ComposerConvergenceError
    assert dict(caught.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
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

    assert type(caught) is ComposerConvergenceError
    assert dict(caught.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
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
    _assert_provider_tool_batch_rejection(caught, expected_message)


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
    responses = [_admit_composer_llm_completion(response)]

    async def _fake_llm(
        _messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        prepared_input: PreparedProviderInput | None = None,
    ) -> Any:
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

    result = await _dispatch_admitted_batch(
        composer_service_with_real_sessions,
        llm=_fake_llm,
        session_id=result_session_id,
    )

    assert mutated is True
    assert handler_calls == ["set_metadata"]
    assert result.tool_outcomes[0].call.id == "call_admitted"
    assert result.tool_outcomes[0].call.function.name == "set_metadata"
    assert result.tool_outcomes[0].response.updated_state.metadata.name == "admitted"
    # The admitted calls are what the batch hands to turn persistence (P4).
    assert [call.id for call in result.assistant_tool_calls] == ["call_admitted"]
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
    responses = [_admit_composer_llm_completion(response)]

    async def _fake_llm(
        _messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        prepared_input: PreparedProviderInput | None = None,
    ) -> Any:
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

    result = await _dispatch_admitted_batch(
        composer_service_with_real_sessions,
        llm=_fake_llm,
        session_id=result_session_id,
    )

    assert mutated is True
    assert handler_calls == ["set_metadata"]
    assert result.tool_outcomes[0].call.id == "call_admitted_before_preferences"
    assert result.tool_outcomes[0].call.function.name == "set_metadata"
    assert result.tool_outcomes[0].response.updated_state.metadata.name == "admitted before preferences"
    # The admitted calls are what the batch hands to turn persistence (P4).
    assert [call.id for call in result.assistant_tool_calls] == ["call_admitted_before_preferences"]
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
        _admit_composer_llm_completion(
            _tool_batch_response(
                ("call_session_reuse", "set_metadata", {"patch": {"name": "prior proposal"}}),
            )
        ),
        _admit_composer_llm_completion(
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
            )
        ),
    ]

    async def _fake_llm(
        _messages: Any,
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        prepared_input: PreparedProviderInput | None = None,
    ) -> Any:
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
    async with acquire_compose_context(sessions_service, UUID(result_session_id)) as context:
        first = await _dispatch_admitted_batch(
            composer_service_with_real_sessions, llm=_fake_llm, session_id=result_session_id, session_operation_context=context
        )
        assert first.plugin_crash is None and [item.call.id for item in first.tool_outcomes] == ["call_session_reuse"]
        try:
            await _dispatch_admitted_batch(
                composer_service_with_real_sessions, llm=_fake_llm, session_id=result_session_id, session_operation_context=context
            )
        except BaseException as exc:
            caught = exc

    # The first explicit-approval batch performs one pure handler preview;
    # the reused-ID second batch is rejected before any handler or effect.
    assert handler_calls == ["set_metadata"]
    assert proposal_calls == 1
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        assert list(
            conn.execute(
                select(composition_proposals_table.c.tool_call_id)
                .where(composition_proposals_table.c.session_id == result_session_id)
                .order_by(composition_proposals_table.c.created_at)
            ).scalars()
        ) == ["call_session_reuse"]
        # Tool rows are P4's (Continuation) publication; neither batch writes one.
        assert (
            list(
                conn.execute(
                    select(chat_messages_table.c.tool_call_id)
                    .where(chat_messages_table.c.session_id == result_session_id)
                    .where(chat_messages_table.c.role == "tool")
                    .order_by(chat_messages_table.c.sequence_no)
                ).scalars()
            )
            == []
        )
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
    await sessions_service.create_composition_proposal(
        session_id=UUID(result_session_id),
        tool_call_id="call_orphaned_proposal",
        tool_name="set_metadata",
        summary="Prior durable proposal.",
        rationale="Tripwire for proposal-only ID ownership.",
        affects=("metadata",),
        arguments_json={"patch": {"name": "prior"}},
        arguments_redacted_json={"patch": {"name": "prior"}},
        base_state_id=None,
        actor="test",
    )
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
    _assert_provider_tool_batch_rejection(caught, expected_message)


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
    _assert_provider_tool_batch_rejection(caught, "Composer tool batch is missing a provider tool-call ID")


@pytest.mark.asyncio
async def test_step1_three_tools_all_succeed_accumulates_three_outcomes(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_three_tool_calls: Any,
    result_session_id: str,
) -> None:
    """Three successful tools produce three outcomes with response set."""

    result = await _dispatch_admitted_batch(
        composer_service_with_real_sessions,
        llm=fake_llm_three_tool_calls,
        session_id=result_session_id,
    )

    outcomes = result.tool_outcomes
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

    result = await _dispatch_admitted_batch(
        composer_service_with_real_sessions,
        llm=fake_llm_tool_argument_error_on_second,
        session_id=result_session_id,
    )

    outcomes = result.tool_outcomes
    assert len(outcomes) == 3
    assert outcomes[0].error_class is None
    assert outcomes[1].error_class == "ToolArgumentError"
    assert outcomes[2].error_class is None


@pytest.mark.asyncio
async def test_step1_assertion_error_reraises_before_persist(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_assertion_error_on_second: Any,
    result_session_id: str,
) -> None:
    """Fatal dispatch carries its undurable receipts before the route drain."""

    recorder = BufferingRecorder()
    with pytest.raises(AssertionError) as caught:
        await _dispatch_admitted_batch(
            composer_service_with_real_sessions,
            llm=fake_llm_assertion_error_on_second,
            session_id=result_session_id,
            recorder=recorder,
        )

    # The batch owner re-raises the original fatal error unwrapped; its
    # undurable receipts stay on the batch recorder for the failure owner.
    assert type(caught.value) is AssertionError
    assert [item.tool_call_id for item in recorder.invocations] == ["call_ok", "call_assert"]
    assert [item.status for item in recorder.invocations] == [ComposerToolStatus.SUCCESS, ComposerToolStatus.PLUGIN_CRASH]
    assert all(item.private_authoring is not None for item in recorder.invocations)
    sessions_service = composer_service_with_real_sessions._sessions_service  # type: ignore[attr-defined]
    with sessions_service._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            text("SELECT id FROM chat_messages WHERE session_id = :session_id AND role IN ('assistant', 'tool')"),
            {"session_id": result_session_id},
        ).fetchall()
    assert rows == []
    assert await sessions_service.get_current_state(UUID(result_session_id)) is None


@pytest.mark.asyncio
async def test_step1_plugin_bug_captures_crash_breaks_loop(
    composer_service_with_real_sessions: ComposerServiceImpl,
    fake_llm_runtime_error_on_second: Any,
    result_session_id: str,
) -> None:
    """RuntimeError on call 2 of 3 records the crash and skips call 3."""

    result = await _dispatch_admitted_batch(
        composer_service_with_real_sessions,
        llm=fake_llm_runtime_error_on_second,
        session_id=result_session_id,
    )

    # The batch captures the crash for P4 instead of raising, and stops.
    # (the deleted loop raised it ``from`` this retained cause).
    assert type(result.plugin_crash) is ComposerPluginCrashError
    assert result.plugin_crash_cause is not None
    assert isinstance(result.plugin_crash_cause, RuntimeError)
    assert result.plugin_crash.original_exc is result.plugin_crash_cause
    outcomes = result.tool_outcomes
    assert len(outcomes) == 2
    assert outcomes[0].error_class is None
    assert outcomes[1].error_class == "RuntimeError"
    assert outcomes[1].error_message == "RuntimeError"
    assert "phase3 synthetic runtime error" not in (outcomes[1].error_message or "")
