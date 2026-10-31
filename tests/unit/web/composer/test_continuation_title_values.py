"""Pure historical DATA proofs; no provider, settlement or SQL authority."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from elspeth.contracts.composer_audit import PrivateFailureFacts, PrivateReceivedDiagnostic
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.composer_provider_context import PrivateProviderContext
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
    _AdmittedToolBatch,
)
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.audit_storage import private_diagnostic_drafts, private_diagnostic_drafts_for_source
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk
from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call
from elspeth.web.composer.continuation_title_values import capture_title_provider_result, decode_title_provider_result
from elspeth.web.composer.provider_audit_source import (
    audit_drafts_json,
    capture_provider_audit_source,
    decode_audit_drafts_json,
    provider_attempt_audit_drafts,
    provider_attempt_audit_drafts_for_source,
)
from elspeth.web.composer.visible_tool_event import visible_response_gap
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import AuditMessageDraft


def title_result_inputs():
    context = SessionOperationContext(
        SessionOperationFence("session-original", "operation-original", "live-lease-not-portable", 3),
        SessionOperationKind.COMPOSE,
    )
    metadata = _AdmittedLLMProviderMetadata(
        model_returned="model-actual",
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=13, completion_tokens=5, reported_total=18),
        provider_cost=None,
        provider_cost_source="not_available",
        provider_request_id="request-original",
        reasoning_content="Retained reasoning",
        reasoning_details=[{"text": "safe detail"}],
        thinking_blocks=None,
        provider_served=None,
    )
    completion = _AdmittedLLMCompletion(_AdmittedAssistantMessage("Pipeline title"), _AdmittedToolBatch((), frozenset()), metadata)
    started = datetime(2026, 10, 23, tzinfo=UTC)
    call = ComposerLLMCall(
        model_requested="model-requested",
        model_returned=metadata.model_returned,
        status=ComposerLLMCallStatus.SUCCESS,
        prompt_tokens=13,
        completion_tokens=5,
        total_tokens=18,
        latency_ms=1000,
        provider_request_id=metadata.provider_request_id,
        messages_hash="a" * 64,
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        error_class=None,
        error_message=None,
        temperature=0.0,
        seed=7,
        finish_reason="stop",
        reasoning_content=metadata.reasoning_content,
        reasoning_details=metadata.reasoning_details,
        call_id="attempt-original",
        max_completion_tokens_requested=40,
        private_request=PrivateProviderContext("retained", canonical_json({"messages": [{"role": "user", "content": "safe prompt"}]})),
        private_diagnostics=(
            PrivateReceivedDiagnostic("diagnostic-original", "call-original", "list_sources", "{}", '{"status":"received"}', "{}"),
        ),
    )
    drafts = provider_attempt_audit_drafts(call, context)
    return {
        "logical": LogicalRequestBinding(
            session_id="session-original",
            operation_id="durable-original",
            original_user_message_id="message-original",
            request_hash="b" * 64,
            kind="compose_message",
            base_state_id=None,
        ),
        "actor_user_id": "actor-original",
        "effect_key": "provider:3:0",
        "input_sha256": "c" * 64,
        "creator_sequence": 2,
        "creator_generation": 4,
        "completion": completion,
        "audit_call": call,
        "context": context,
        "audit_drafts": drafts,
        "env_ref_names": frozenset(),
    }


def test_title_result_retains_original_objects_and_complete_cold_values():
    values = title_result_inputs()
    captured = capture_title_provider_result(**values)
    assert captured.completion is values["completion"]
    assert captured.audit_call is values["audit_call"]
    assert captured.audit_drafts is values["audit_drafts"]
    decoded = decode_title_provider_result(captured.chunk, env_ref_names=frozenset())
    assert decoded.snapshot == captured.snapshot
    assert decoded.completion == captured.completion
    assert encode_physical_audit_call(decoded.audit_call) == encode_physical_audit_call(captured.audit_call)
    assert audit_drafts_json(decoded.audit_drafts) == audit_drafts_json(captured.audit_drafts)
    assert decoded.source == captured.source
    assert decoded.source.operation_id == "operation-original"
    assert "live-lease-not-portable" not in captured.chunk.canonical
    assert len(decoded.audit_drafts) == 3
    assert decoded.audit_drafts[1].inherit_composition_state is False
    assert decoded.audit_drafts[2].inherit_composition_state is False
    assert decoded.audit_drafts[0].inherit_composition_state is True


def test_shared_projector_matches_original_finisher_and_historical_source():
    values = title_result_inputs()
    call, context = values["audit_call"], values["context"]
    public = llm_call_audit_envelope(call)
    original = (
        AuditMessageDraft(
            role="audit", content="Provider call result recorded.", tool_calls=(public, visible_response_gap(call, context, public))
        ),
        *private_diagnostic_drafts(call, context=context),
    )
    assert audit_drafts_json(original) == audit_drafts_json(values["audit_drafts"])
    source = capture_provider_audit_source(context)
    assert audit_drafts_json(provider_attempt_audit_drafts_for_source(call, source)) == audit_drafts_json(original)
    assert private_diagnostic_drafts_for_source(call, source=source) == private_diagnostic_drafts(call, context=context)
    failure = replace(call, private_failure=PrivateFailureFacts('{"message":"diagnostic only"}'))
    assert len(private_diagnostic_drafts_for_source(failure, source=source)) == 3
    with pytest.raises(AuditIntegrityError, match="COMPOSE"):
        provider_attempt_audit_drafts(call, replace(context, operation_kind=SessionOperationKind.PROPOSAL))


def test_all_seven_draft_fields_null_empty_order_and_shared_values_are_retained():
    shared = {"nested": [None, True, 1, "safe"]}
    original = (
        AuditMessageDraft(
            role="tool",
            content="first",
            tool_calls=(shared, shared),
            tool_call_id="tool-original",
            parent_assistant_id="assistant-original",
            composition_state_id="state-original",
        ),
        AuditMessageDraft(role="audit", content="second", tool_calls=None, inherit_composition_state=False),
        AuditMessageDraft(role="audit", content="third", tool_calls=()),
    )
    encoded = audit_drafts_json(original)
    decoded = decode_audit_drafts_json(encoded)
    assert decoded == original
    assert decoded[1].tool_calls is None
    assert decoded[2].tool_calls == ()
    assert audit_drafts_json(decoded) == encoded
    rows = json.loads(encoded)
    del rows[0]["parent_assistant_id"]
    with pytest.raises(AuditIntegrityError, match="complete owned field"):
        decode_audit_drafts_json(canonical_json(rows))


@pytest.mark.parametrize("mutation", ["private_drop", "order", "state", "gap_source", "gap_hash", "public", "inherit"])
def test_title_result_refuses_changed_original_draft_cohort(mutation):
    values = title_result_inputs()
    captured = capture_title_provider_result(**values)
    decode_title_provider_result(captured.chunk, env_ref_names=frozenset())
    body = json.loads(captured.chunk.canonical)
    rows = json.loads(body["audit_drafts_json"])
    if mutation == "private_drop":
        rows.pop()
    elif mutation == "order":
        rows.reverse()
    elif mutation == "state":
        rows[0]["composition_state_id"] = "foreign-state"
    elif mutation == "gap_source":
        rows[0]["tool_calls"][1]["operation_id"] = "fresh-writer"
    elif mutation == "gap_hash":
        rows[0]["tool_calls"][1]["public_call_hash"] = "d" * 64
    elif mutation == "public":
        rows[0]["tool_calls"][0]["call"]["model_requested"] = "other-model"
    else:
        rows[0]["inherit_composition_state"] = False
    body["audit_drafts_json"] = canonical_json(rows)
    changed = encode_private_chunk(codec="composer.title-provider-result.v1", canonical=canonical_json(body))
    with pytest.raises(AuditIntegrityError, match="original complete audit draft cohort"):
        decode_title_provider_result(changed, env_ref_names=frozenset())


def test_capture_refuses_changed_actual_tuple_without_copying_it():
    values = title_result_inputs()
    capture_title_provider_result(**values)
    values["audit_drafts"] = values["audit_drafts"][:-1]
    with pytest.raises(AuditIntegrityError, match="original complete audit draft cohort"):
        capture_title_provider_result(**values)


def test_title_result_refuses_response_audit_usage_disagreement():
    values = title_result_inputs()
    capture_title_provider_result(**values)
    values["completion"] = replace(
        values["completion"],
        provider_metadata=replace(
            values["completion"].provider_metadata, usage=TokenUsage(prompt_tokens=14, completion_tokens=5, reported_total=19)
        ),
    )
    with pytest.raises(AuditIntegrityError, match="physical identity, finish, usage"):
        capture_title_provider_result(**values)


@pytest.mark.parametrize("mutation", ["source_session", "missing", "unknown", "outer_default", "response_defaults", "response_audit"])
def test_title_result_refuses_coherently_rehashed_shape_and_inner_changes(mutation):
    captured = capture_title_provider_result(**title_result_inputs())
    decode_title_provider_result(captured.chunk, env_ref_names=frozenset())
    body = json.loads(captured.chunk.canonical)
    if mutation == "source_session":
        body["source"]["session_id"] = "foreign-session"
        body["audit_drafts_json"] = audit_drafts_json(
            provider_attempt_audit_drafts_for_source(captured.audit_call, replace(captured.source, session_id="foreign-session"))
        )
    elif mutation == "missing":
        del body["creator_generation"]
    elif mutation == "unknown":
        body["unowned"] = True
    elif mutation == "outer_default":
        del body["schema_version"]
    else:
        response = json.loads(body["response_canonical"])
        if mutation == "response_defaults":
            del response["advisor_result_metadata"]
        else:
            response["audit_call_json"] = None
        nested = encode_private_chunk(codec="composer.response.v1", canonical=canonical_json(response))
        body["response_reference"] = nested.reference.model_dump(mode="json")
        body["response_canonical"] = nested.canonical
    changed = encode_private_chunk(codec="composer.title-provider-result.v1", canonical=canonical_json(body))
    with pytest.raises(AuditIntegrityError):
        decode_title_provider_result(changed, env_ref_names=frozenset())


def test_title_result_digest_and_complete_tail_credentials_are_not_waived():
    values = title_result_inputs()
    captured = capture_title_provider_result(**values)
    with pytest.raises(AuditIntegrityError, match="bytes differ"):
        decode_title_provider_result(
            PrivateChunk(captured.chunk.reference, captured.chunk.canonical.replace("Pipeline title", "Different title")),
            env_ref_names=frozenset(),
        )
    values["actor_user_id"] = "postgresql://example:secret@db.example/database"  # secret-scan: allow-this-line
    with pytest.raises(CredentialMaterialRefused):
        capture_title_provider_result(**values)


def test_ordinary_tool_private_failure_projection_preserves_state_and_source():
    from tests.unit.contracts.test_composer_audit import _make_invocation

    values = title_result_inputs()
    invocation = replace(_make_invocation(), private_failure=PrivateFailureFacts('{"message":"ordinary tool failure"}'))
    source = capture_provider_audit_source(values["context"])
    warm = private_diagnostic_drafts(invocation, context=values["context"], composition_state_id="state-original")
    historical = private_diagnostic_drafts_for_source(invocation, source=source, composition_state_id="state-original")
    assert historical == warm
    assert len(historical) == 1
    assert historical[0].inherit_composition_state is True
    assert historical[0].composition_state_id == "state-original"
    assert historical[0].tool_calls[0]["operation_id"] == source.operation_id


def test_pure_title_result_retains_complete_calls_without_granting_title_eligibility():
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction

    values = title_result_inputs()
    call = _AdmittedToolCall("call-1", _AdmittedToolFunction("list_sources", "{}"))
    values["completion"] = replace(values["completion"], tool_batch=_AdmittedToolBatch((call,), frozenset({call.id})))
    captured = capture_title_provider_result(**values)
    decoded = decode_title_provider_result(captured.chunk, env_ref_names=frozenset())
    assert decoded.completion.tool_batch == values["completion"].tool_batch
    assert captured.completion is values["completion"]


def test_title_result_requires_received_success_instead_of_failure_as_empty_success():
    values = title_result_inputs()
    capture_title_provider_result(**values)
    failed = replace(
        values["audit_call"], status=ComposerLLMCallStatus.TIMEOUT, error_class="TimeoutError", error_message="original timeout"
    )
    values["audit_call"] = failed
    values["audit_drafts"] = provider_attempt_audit_drafts(failed, values["context"])
    with pytest.raises(AuditIntegrityError, match="physical identity, finish, usage"):
        capture_title_provider_result(**values)
