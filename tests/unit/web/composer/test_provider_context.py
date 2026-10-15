"""Safe reconstructable logical request context with fake providers only."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import litellm
import pytest

from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.provider_context import capture_provider_context
from tests.unit.contracts.test_composer_llm_audit import _make_call
from tests.unit.web.composer.test_private_evidence_persistence import private_writer as _private_writer

private_writer = _private_writer


def _messages(arguments: str = '{ "component": "summary" }', result: str = '{ "success": true }') -> list[dict[str, object]]:
    return [
        {"role": "system", "content": [{"type": "text", "text": "Input data: ordinary text.", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "Inspect the current pipeline."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_pipeline_state", "arguments": arguments}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": result},
        {"role": "user", "content": "Transient validation feedback: inspect required input."},
    ]


def _tools() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_pipeline_state",
                "description": "Read current state.",
                "strict": True,
                "parameters": {"type": "object", "properties": {"api_key": {"type": "string", "description": "Managed reference."}}},
            },
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_safe_context_preserves_order_markers_schema_vocabulary_and_original_json_strings() -> None:
    messages, tools = _messages(), _tools()
    expected = deepcopy((messages, tools))
    context = capture_provider_context(messages=messages, tools=tools)
    assert context.disposition == "retained"
    payload = json.loads(context.canonical)
    assert payload["messages"] == expected[0]
    assert payload["tools"] == expected[1]
    assert payload["schema_version"] == "composer.provider-context.v1"
    assert payload["capture_boundary"] == "prepared_litellm_messages_and_tools"
    messages[-1]["content"] = "Later mutation"
    tools.clear()
    assert json.loads(context.canonical)["messages"] == expected[0]
    assert json.loads(context.canonical)["tools"] == expected[1]


@pytest.mark.parametrize("tools", [None, []])
def test_tools_absence_and_empty_are_distinct(tools) -> None:
    context = capture_provider_context(messages=[{"role": "user", "content": "hi"}], tools=tools)
    assert json.loads(context.canonical)["tools"] == tools


@pytest.mark.parametrize(
    "arguments,result,reason",
    [
        ('{"password":"synthetic-private-value"}', "{}", "credential_field"),
        ('{"pass\\u0077ord":"synthetic-private-value"}', "{}", "credential_field"),
        ('{"note":"password\\u003dsynthetic-private-value"}', "{}", "recognized_credential"),
        ('{"note":"https://synthetic-user:synthetic-pass@example.invalid"}', "{}", "recognized_credential"),
        ("{}", '{"api_key":"synthetic-private-value"}', "credential_field"),
        ('{"a":1,"a":2}', "{}", "duplicate_keys"),
        ('{"a":', "{}", "malformed_json"),
        ("{}", "not JSON", "malformed_json"),
        ('{"x":"data:image/png;base64,c3ludGhldGlj"}', "{}", "unsupported"),
    ],
)
def test_encoded_slots_fail_closed_without_raw_fallback(arguments: str, result: str, reason: str) -> None:
    context = capture_provider_context(messages=_messages(arguments, result), tools=_tools())
    assert context.disposition == "unavailable"
    assert context.reasons == (reason,)
    assert context.canonical is None
    assert "synthetic-private-value" not in repr(context)


@pytest.mark.parametrize("slot", ["default", "examples"])
def test_schema_credential_values_are_withheld_but_the_property_name_is_safe(slot: str) -> None:
    tools = _tools()
    tools[0]["function"]["parameters"]["properties"]["api_key"][slot] = "synthetic-private-value"
    context = capture_provider_context(messages=[{"role": "user", "content": "hi"}], tools=tools)
    assert context.disposition == "unavailable"
    assert context.reasons == ("credential_field",)


@pytest.mark.parametrize(
    "message",
    [
        {"content": "missing role"},
        {"role": [], "content": "hi"},
        {"role": {}, "content": "hi"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}]},
        {"role": "user", "content": [{"text": "missing type"}]},
        {"role": "user", "content": "data:image/png;base64,c3ludGhldGlj"},
        {"role": "user", "content": "hi", "opaque_extension": "private"},
        {"role": "user", "content": "hi", "tool_call_id": "password=synthetic-private-value"},
        {"role": "assistant", "content": "hi", "tool_call_id": "password=synthetic-private-value"},
        {"role": "tool", "tool_call_id": "unknown", "content": "{}"},
    ],
)
def test_images_and_unknown_protocol_shapes_are_closed_unavailable(message) -> None:
    context = capture_provider_context(messages=[message], tools=None)
    assert context.disposition == "unavailable"
    assert context.canonical is None


@pytest.mark.parametrize("role", ["system", "developer", "user", "assistant"])
@pytest.mark.parametrize("value", ["ordinary-id", "password=synthetic-private-value", {"api_key": "opaque"}, [], None])
def test_inapplicable_tool_call_ids_are_never_retained(role, value) -> None:
    context = capture_provider_context(messages=[{"role": role, "content": "hello", "tool_call_id": value}], tools=None)
    assert context.disposition == "unavailable"
    assert context.canonical is None


def test_tool_definition_without_type_is_unavailable() -> None:
    context = capture_provider_context(messages=[{"role": "user", "content": "hi"}], tools=[{"function": {"name": "list_sources"}}])
    assert context.disposition == "unavailable"
    assert context.reasons == ("unsupported",)


def test_recognized_credential_text_and_declared_plugin_slots_are_withheld() -> None:
    text = capture_provider_context(messages=[{"role": "user", "content": "password=synthetic-private-value"}], tools=None)
    assert text.disposition == "unavailable"
    messages = _messages('{"plugin":"database","options":{"url":"postgresql://opaque-private-location"}}')
    messages[2]["tool_calls"][0]["function"]["name"] = "set_output"
    context = capture_provider_context(messages=messages, tools=None)
    assert context.reasons == ("credential_field",)


def test_managed_reference_is_not_treated_as_a_literal_credential() -> None:
    context = capture_provider_context(messages=_messages('{"api_key":{"secret_ref":"managed_key"}}'), tools=None)
    assert context.disposition == "retained"


@pytest.mark.parametrize("content", ["x" * 1_048_577, "☃" * 349_526])
def test_aggregate_string_bound_has_no_retained_prefix(content: str) -> None:
    context = capture_provider_context(messages=[{"role": "user", "content": content}], tools=None)
    assert context.disposition == "unavailable"
    assert context.reasons == ("bounds",)


def test_aggregate_and_depth_bounds_apply_across_the_request() -> None:
    under = capture_provider_context(messages=[{"role": "user", "content": "☃" * 65_537}], tools=None)
    assert under.disposition == "retained"
    over = capture_provider_context(messages=[{"role": "user", "content": "x" * 60_000} for _ in range(18)], tools=None)
    assert over.reasons == ("bounds",)
    deep = []
    for _ in range(70):
        deep = [deep]
    assert capture_provider_context(messages=deep, tools=None).reasons == ("bounds",)
    assert capture_provider_context(messages=[None] * 10_001, tools=None).reasons == ("bounds",)


def test_hostile_objects_are_not_stringified() -> None:
    class Hostile:
        def __repr__(self):
            raise AssertionError("repr called")

        def __str__(self):
            raise AssertionError("str called")

    context = capture_provider_context(messages=[{"role": "user", "content": Hostile()}], tools=None)
    assert context.reasons == ("unsupported",)


@pytest.mark.parametrize("value,reason", [(2**60, "unsupported"), ("\ud800", "bounds")])
def test_noncanonical_scalar_values_are_closed_unavailable(value, reason) -> None:
    context = capture_provider_context(messages=[{"role": "user", "content": value}], tools=None)
    assert context.disposition == "unavailable"
    assert context.canonical is None
    assert context.reasons == (reason,)


def test_numeric_canonical_domain_is_enforced_in_valid_schema_position() -> None:
    tools = _tools()
    tools[0]["function"]["parameters"]["properties"]["count"] = {"type": "integer", "default": 2**60}
    context = capture_provider_context(messages=[{"role": "user", "content": "hi"}], tools=tools)
    assert context.disposition == "unavailable"
    assert context.reasons == ("unsupported",)
    assert context.canonical is None


def test_encoded_slot_bounds_apply_after_decoding() -> None:
    # The encoded and decoded representations share the request budget.
    context = capture_provider_context(messages=_messages(json.dumps({"note": "x" * 600_000})), tools=None)
    assert context.disposition == "unavailable"
    assert context.reasons == ("bounds",)


def test_private_context_does_not_change_public_call_record_or_repr() -> None:
    context = capture_provider_context(messages=[{"role": "user", "content": "PRIVATE CONTEXT CANARY"}], tools=None)
    original = _make_call()
    call = replace(original, private_request=context)
    assert call.to_dict() == original.to_dict()
    assert llm_call_audit_envelope(call) == llm_call_audit_envelope(original)
    assert "PRIVATE CONTEXT CANARY" not in repr((context, call))


@pytest.mark.asyncio
async def test_unavailable_context_persists_no_unsafe_content_or_digest(private_writer) -> None:
    from elspeth.web.composer.audit_storage import private_diagnostic_drafts
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence

    context = capture_provider_context(
        messages=[{"role": "user", "content": "hello", "tool_call_id": {"api_key": "synthetic-private-canary"}}], tools=None
    )
    call = replace(_make_call(), private_request=context)
    sessions, session, operation, _ = private_writer
    drafts = private_diagnostic_drafts(call, context=operation)
    await sessions.add_messages_atomic(
        session.id, drafts, writer_principal="compose_loop", session_operation_context=operation, audit_only=True
    )
    rows = await sessions.get_messages(session.id, limit=None)
    assert len(rows) == 1
    evidence = ComposerDiagnosticEvidence.from_envelope(rows[0].tool_calls[0], expected_session_id=str(session.id))
    assert evidence.arguments.disposition == "unavailable"
    assert evidence.arguments.content is None
    assert evidence.arguments.retained_sha256 is None
    assert "synthetic-private-canary" not in repr((call, context, evidence, rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", ["replay", "epoch", "role"])
async def test_provider_context_keeps_existing_replay_and_fence_checks(private_writer, violation) -> None:
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.composer.audit_storage import private_diagnostic_drafts

    sessions, session, operation, _ = private_writer
    context = capture_provider_context(messages=[{"role": "user", "content": "first request"}], tools=None)
    call = replace(_make_call(), private_request=context)
    drafts = private_diagnostic_drafts(call, context=operation)
    await sessions.add_messages_atomic(
        session.id, drafts, writer_principal="compose_loop", session_operation_context=operation, audit_only=True
    )
    if violation == "replay":
        changed = capture_provider_context(messages=[{"role": "user", "content": "changed request"}], tools=None)
        drafts = private_diagnostic_drafts(replace(call, private_request=changed), context=operation)
    elif violation == "role":
        drafts = (replace(drafts[0], role="assistant"),)
    else:
        wrong_operation = replace(operation, fence=replace(operation.fence, operation_epoch=operation.fence.operation_epoch + 1))
        drafts = private_diagnostic_drafts(call, context=wrong_operation)
    with pytest.raises(AuditIntegrityError):
        await sessions.add_messages_atomic(
            session.id, drafts, writer_principal="compose_loop", session_operation_context=operation, audit_only=True
        )
    assert len(await sessions.get_messages(session.id, limit=None)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "timeout", "cancelled", "failure", "malformed"])
async def test_gateway_terminal_paths_keep_the_preawait_context(tmp_path, monkeypatch, mode) -> None:
    from elspeth.web.composer.provider_gateway import ProviderGateway, _MalformedLLMResponseError
    from tests.unit.web.composer.test_llm_sampling_config import _settings
    from tests.unit.web.composer.test_service import _make_llm_response

    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    recorder = BufferingRecorder()
    messages = [{"role": "user", "content": "PRIVATE REQUEST BEFORE AWAIT"}]
    expected = deepcopy(messages)

    async def completion(wire_messages, wire_tools, *, provider_custody: object | None = None, credential_state: object | None = None):
        assert wire_messages == expected
        wire_messages[0]["content"] = "MUTATED AFTER SNAPSHOT"
        if mode == "timeout":
            raise TimeoutError
        if mode == "cancelled":
            raise asyncio.CancelledError
        if mode == "failure":
            raise RuntimeError("synthetic provider failure")
        if mode == "malformed":
            raise _MalformedLLMResponseError(
                "synthetic malformed response", provider_metadata=_make_llm_response(content="x").provider_metadata
            )
        return _make_llm_response(content="Reply.")

    monkeypatch.setattr(gateway, "_call_llm", completion)
    if mode == "success":
        await gateway._call_llm_with_audit_body(messages, [], timeout=5, recorder=recorder)
    else:
        with pytest.raises((TimeoutError, asyncio.CancelledError, RuntimeError, _MalformedLLMResponseError)):
            await gateway._call_llm_with_audit_body(messages, [], timeout=5, recorder=recorder)
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.private_request is not None
    assert json.loads(call.private_request.canonical)["messages"] == expected
    assert "PRIVATE REQUEST BEFORE AWAIT" not in json.dumps(call.to_dict())


@pytest.mark.asyncio
async def test_planner_context_matches_each_ordered_request_including_feedback(tmp_path, tool_context) -> None:
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    completion = _ScriptedCompletion(
        _response(("list_sources", {})), _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}))
    )
    recorder = BufferingRecorder()
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)
    assert len(recorder.llm_calls) == len(completion.requests) == 2
    for call, request in zip(recorder.llm_calls, completion.requests, strict=True):
        assert call.private_request is not None
        assert call.private_request.disposition == "retained"
        context = json.loads(call.private_request.canonical)
        assert context["messages"] == request["messages"]
        assert context["tools"] == request["tools"]
    assert recorder.llm_calls[0].private_request != recorder.llm_calls[1].private_request


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["primary", "text", "advisor"])
async def test_real_rendered_composer_prompt_is_reconstructable_on_covered_surfaces(
    tmp_path, monkeypatch, surface, private_writer, test_client
) -> None:
    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.web.composer import provider_gateway
    from elspeth.web.composer.audit_storage import private_diagnostic_drafts
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
    from tests.unit.web.composer.test_llm_sampling_config import _service
    from tests.unit.web.sessions.test_record_audit_grade_view import _get

    service = _service(tmp_path)
    recorder = BufferingRecorder()
    requests = []

    async def completion(**kwargs):
        requests.append(deepcopy(kwargs))
        return litellm.ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "Ordinary reply.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    prompt = service._composer_skill_text
    assert len(prompt) > 65_536
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "Explain the required input."}]
    if surface == "primary":
        tools = provider_gateway.composer_loop_tool_definitions(ToolContractDialect.NONE)
        await service._provider_gateway._call_llm_with_audit_body(messages, tools, timeout=5, recorder=recorder)
    elif surface == "text":
        await service._provider_gateway._call_text_llm_with_audit_body(messages, timeout=5, recorder=recorder)
    else:
        await service._advisor_checkpoint._call_advisor_with_audit_body(
            {"trigger": "user_request", "problem_summary": "Inspect the draft.", "recent_errors": [], "attempted_actions": []},
            recorder=recorder,
        )
    assert len(requests) == len(recorder.llm_calls) == 1
    context = recorder.llm_calls[0].private_request
    assert context is not None and context.disposition == "retained"
    captured = json.loads(context.canonical)
    assert captured["messages"] == requests[0]["messages"]
    assert captured["tools"] == requests[0].get("tools")
    sessions, session, operation, _ = private_writer
    drafts = private_diagnostic_drafts(recorder.llm_calls[0], context=operation)
    for _ in range(2):
        await sessions.add_messages_atomic(
            session.id, drafts, writer_principal="compose_loop", session_operation_context=operation, audit_only=True
        )
    rows = await sessions.get_messages(session.id, limit=None)
    assert len(rows) == len(drafts)
    restored = [ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(session.id)) for row in rows]
    request = next(item for item in restored if item.status == "provider_request")
    assert request.arguments.content == context.canonical
    assert all(row.composition_state_id is None for row in rows)
    monkeypatch.setattr(test_client.app.state, "session_service", sessions)
    monkeypatch.setattr(test_client.app.state, "phase3_engine", sessions._engine)
    private = await _get(test_client, f"/api/sessions/{session.id}/diagnostic-evidence?limit=5")
    assert private.status_code == 200
    assert "prepared_litellm_messages_and_tools" in json.dumps(private.json())
    public = await _get(test_client, f"/api/sessions/{session.id}/messages")
    assert public.status_code == 200
    assert "prepared_litellm_messages_and_tools" not in json.dumps(public.json())
