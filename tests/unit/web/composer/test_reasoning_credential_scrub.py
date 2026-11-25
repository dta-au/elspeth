"""CDA-18: reasoning withholding retains the provider call, never its value."""

import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ComposerContextBudgetRecord, ComposerLLMCallStatus
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer._helpers import _make_llm_response

pytest_plugins = ("tests.unit.web.test_credential_guard",)

REASONING = '"password": "Client emailed the admin pass..."'
# Visible output is refused only for a recognized pattern; the keyword assignment
# in REASONING is withheld from reasoning but no longer refuses visible output.
VISIBLE_CREDENTIAL = "sk-" + "v" * 30


@pytest.mark.parametrize("surface", ["composer_provider_response", "composer_planner_response", "composer_advisor_response"])
@pytest.mark.parametrize("field", ["reasoning_content", "reasoning_details", "thinking_blocks"])
def test_reasoning_withhold_is_value_free_and_durable(surface: str, field: str) -> None:
    response = _make_llm_response(content="ordinary")
    if field == "thinking_blocks":
        response.choices[0].message.__dict__[field] = [
            {"type": "thinking", "thinking": REASONING, "signature": "opaque"},
            {"type": "redacted_thinking", "data": "opaque"},
        ]
    else:
        response.choices[0].message.__dict__[field] = (
            REASONING if field == "reasoning_content" else [{"type": "reasoning.text", "text": REASONING}]
        )
    refusal = None
    call = None
    try:
        call = build_llm_call_record(
            model_requested="test-model",
            messages=[],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=datetime.now(UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            response=response,
            credential_surface=surface,
        )
    except CredentialMaterialRefused as exc:
        refusal = exc.to_payload()
    assert refusal is None, "reasoning alone must be withheld, not refuse the turn"
    assert call is not None
    assert call.reasoning_content is None
    assert call.reasoning_details is None
    assert call.thinking_blocks is None
    payload = llm_call_audit_envelope(call)
    assert REASONING not in json.dumps(payload)
    assert REASONING not in repr(call)
    assert payload["call"]["reasoning_withholds"] == [
        {
            "surface": surface,
            "category": "token_shape",
            "detector_version": "2",
            "field_class": field,
        }
    ]


def test_gateway_applies_scrub_before_releasing_completion() -> None:
    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = REASONING
    refusal = None
    completion = None
    try:
        completion = _admit_composer_llm_completion(response)
    except CredentialMaterialRefused as exc:
        refusal = exc.to_payload()
    assert refusal is None, "gateway must apply the same reasoning-withhold decision"
    assert completion is not None
    assert completion.provider_metadata.reasoning_content is None
    assert REASONING not in repr(completion)


def test_scrubbed_metadata_keeps_one_event_and_context_budget_in_call_record() -> None:
    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = REASONING
    completion = _admit_composer_llm_completion(response)
    budget = ComposerContextBudgetRecord(
        policy_hash="test-policy",
        bytes_per_token="4.0",
        estimated_tokens_before=1,
        estimated_tokens_after=1,
        request_wire_bytes=4,
        shed_history_indices=(),
        stubbed_tool_call_ids=(),
        shed_hatch_event_indices=(),
    )
    call = build_llm_call_record(
        model_requested="test-model",
        messages=[],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response_metadata=completion.provider_metadata,
        context_budget=budget,
    )
    assert len(call.reasoning_withholds) == 1
    assert call.context_budget == budget
    assert call.reasoning_content is None
    assert REASONING not in json.dumps(call.to_dict())


def test_malformed_completion_carries_scrubbed_metadata() -> None:
    from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError

    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = REASONING
    response.choices[0].message.content = ["malformed"]
    with pytest.raises(_MalformedLLMResponseError) as caught:
        _admit_composer_llm_completion(response)
    assert caught.value.provider_metadata.reasoning_content is None
    assert len(caught.value.provider_metadata.reasoning_withholds) == 1
    assert REASONING not in repr(caught.value.provider_metadata)


@pytest.mark.parametrize("opaque_field", ["encrypted_data", "signature", "redacted_data"])
def test_opaque_provider_bytes_are_not_credential_text(opaque_field: str) -> None:
    blob = "sk-" + "a" * 30
    response = _make_llm_response(content="ordinary")
    message = response.choices[0].message
    if opaque_field == "encrypted_data":
        message.__dict__["reasoning_details"] = [{"type": "reasoning.encrypted", "data": blob}]
    else:
        message.__dict__["thinking_blocks"] = [
            {"type": "thinking", "thinking": "ordinary", "signature": blob},
            {"type": "redacted_thinking", "data": blob},
        ]
    completion = _admit_composer_llm_completion(response)
    call = build_llm_call_record(
        model_requested="test-model",
        messages=[],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response_metadata=completion.provider_metadata,
    )
    assert call.to_dict()["reasoning_withholds"] == []
    if opaque_field != "encrypted_data":
        assert call.to_dict()["thinking_blocks"] == message.thinking_blocks


@pytest.mark.parametrize("field", ["content", "tool_name", "tool_arguments"])
def test_visible_output_still_refuses(field: str) -> None:
    from types import SimpleNamespace

    response = _make_llm_response(content=VISIBLE_CREDENTIAL if field == "content" else "ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = REASONING
    if field != "content":
        response.choices[0].message.tool_calls = [
            SimpleNamespace(
                id="call_a",
                type="function",
                function=SimpleNamespace(
                    name=VISIBLE_CREDENTIAL if field == "tool_name" else "set_metadata",
                    arguments=json.dumps({"patch": {"description": VISIBLE_CREDENTIAL}}) if field == "tool_arguments" else "{}",
                ),
            )
        ]
    from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion
    from elspeth.web.credential_guard import require_no_credential_material_in_tool_wire

    with pytest.raises(CredentialMaterialRefused):
        completion = _admit_composer_llm_completion(response)
        _require_no_credential_material_in_completion(completion, surface="composer_provider_response")
        for call in completion.tool_batch.calls:
            require_no_credential_material_in_tool_wire(call.function.name, call.function.arguments, surface="composer_provider_response")


def test_a_keyword_assignment_is_admitted_in_visible_output_and_withheld_from_reasoning() -> None:
    from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion

    response = _make_llm_response(content=REASONING)
    response.choices[0].message.__dict__["reasoning_content"] = REASONING

    completion = _require_no_credential_material_in_completion(
        _admit_composer_llm_completion(response), surface="composer_provider_response"
    )

    assert completion.message.content == REASONING
    assert completion.provider_metadata.reasoning_content is None


class RequestInspected(Exception):
    """Stop after the physical credential scan, before a provider attempt."""


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_thinking", [False, True])
async def test_current_loop_outgoing_thinking_is_complete_or_request_refused(
    monkeypatch: pytest.MonkeyPatch,
    provider_custody: ProviderCallCustody,
    credential_thinking: bool,
) -> None:
    from elspeth.web.composer import provider_gateway
    from elspeth.web.composer.provider_thinking import attach_provider_thinking
    from elspeth.web.composer.service import _reply_only_messages

    opaque = "sk-" + "a" * 30
    redacted = "sk-" + "b" * 30
    last_signature = "sk-" + "c" * 30
    blocks = [
        {"type": "thinking", "thinking": "first", "signature": opaque},
        {"type": "redacted_thinking", "data": redacted},
        {"type": "thinking", "thinking": REASONING if credential_thinking else "last", "signature": last_signature},
    ]
    assistant = {
        "role": "assistant",
        "content": "ordinary",
        "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "list_plugins", "arguments": "{}"}}],
    }
    # Contaminated history exercises the physical boundary too, independently
    # of admission, which already rejects that same current-loop response.
    if credential_thinking:
        assistant["thinking_blocks"] = blocks
    else:
        assistant = attach_provider_thinking(assistant, blocks)
    history = [
        {"role": "user", "content": "inspect plugins"},
        assistant,
        {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
    ]
    outgoing = history if credential_thinking else _reply_only_messages(history)
    captured = []

    async def inspect_request(**kwargs: Any) -> None:
        from elspeth.web.composer.prompt_guard import _request

        request, supported = _request(
            policy_fingerprint="test-policy",
            role="composer",
            messages=kwargs["messages"],
            tools=(),
            user_prompt="inspect plugins",
            trusted_message_indexes=kwargs["trusted_message_indexes"],
            content_parts=kwargs["content_parts"],
            tool_argument_parts=kwargs["tool_argument_parts"],
            thinking_parts=kwargs["thinking_parts"],
        )
        assert supported
        inspection = "".join(request.documents)
        assert all(blob in inspection for blob in (opaque, redacted, last_signature))
        captured.append(kwargs["messages"])
        raise RequestInspected

    monkeypatch.setattr(provider_gateway, "enforce_prompt_guard", inspect_request)
    with pytest.raises(CredentialMaterialRefused if credential_thinking else RequestInspected) as caught:
        await provider_gateway._litellm_acompletion(
            provider_custody=provider_custody,
            model="anthropic/test-model",
            prompt_guard_mount=None,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            messages=outgoing,
        )
    if credential_thinking:
        assert isinstance(caught.value, CredentialMaterialRefused)
        assert caught.value.to_payload()["cause"] == "current_tool_loop_thinking_credential"
        assert REASONING not in repr(caught.value)
        assert captured == []
    else:
        assert captured[0][1]["thinking_blocks"] == blocks
        assert captured[0][1]["tool_calls"] == assistant["tool_calls"]
        assert captured[0][2] == history[2]


def test_completed_turn_history_has_no_native_thinking_to_shed() -> None:
    from elspeth.web.sessions.protocol import ChatMessageRecord
    from elspeth.web.sessions.routes._helpers import _composer_chat_history

    message = ChatMessageRecord(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Finished.",
        created_at=datetime.now(UTC),
        writer_principal="compose_loop",
    )
    assert _composer_chat_history([message]) == [{"role": "assistant", "content": "Finished."}]


@pytest.mark.asyncio
@pytest.mark.parametrize("text_only", [False, True])
async def test_final_native_thinking_is_withheld(monkeypatch: pytest.MonkeyPatch, text_only: bool) -> None:
    from elspeth.web.composer import provider_gateway
    from tests.unit.web.composer._helpers import _make_settings

    response = _make_llm_response(content="Finished.")
    response.choices[0].message.__dict__.update(
        reasoning_content=REASONING,
        thinking_blocks=[
            {"type": "thinking", "thinking": REASONING, "signature": "opaque"},
            {"type": "redacted_thinking", "data": "opaque"},
        ],
    )

    async def complete(**kwargs: Any) -> object:
        return response

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", complete)
    gateway = provider_gateway.ProviderGateway(
        model="anthropic/test-model",
        settings=_make_settings(composer_model="anthropic/test-model"),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )
    refusal = None
    try:
        if text_only:
            result = await gateway._call_text_llm([{"role": "user", "content": "ordinary"}])
            assert result.choices[0].message.content == "Finished."
        else:
            admitted = await gateway._call_llm([{"role": "user", "content": "ordinary"}], [])
            assert admitted.provider_metadata.thinking_blocks is None
            assert {event.field_class for event in admitted.provider_metadata.reasoning_withholds} == {
                "reasoning_content",
                "thinking_blocks",
            }
            assert REASONING not in repr(admitted)
    except CredentialMaterialRefused as exc:
        refusal = exc.to_payload()
    assert refusal is None, "final native thinking is not replayed and must not refuse the turn"


@pytest.mark.parametrize("kind,key", [("reasoning.text", "data"), ("reasoning.encrypted", "text"), ("reasoning.encrypted", "summary")])
def test_only_encrypted_data_is_excluded(kind: str, key: str) -> None:
    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_details"] = [{"type": kind, key: REASONING}]
    admitted = _admit_composer_llm_completion(response)
    assert admitted.provider_metadata.reasoning_details is None
    assert len(admitted.provider_metadata.reasoning_withholds) == 1


def test_completion_wrapper_and_malformed_constructor_apply_scrub() -> None:
    from dataclasses import replace

    from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError, _require_no_credential_material_in_completion

    clean = _admit_composer_llm_completion(_make_llm_response(content="ordinary"))
    contaminated = replace(clean, provider_metadata=replace(clean.provider_metadata, reasoning_content=REASONING))
    scrubbed = _require_no_credential_material_in_completion(contaminated, surface="composer_provider_response")
    assert scrubbed.provider_metadata.reasoning_content is None
    assert len(scrubbed.provider_metadata.reasoning_withholds) == 1
    malformed = _MalformedLLMResponseError("malformed", provider_metadata=contaminated.provider_metadata)
    assert malformed.provider_metadata.reasoning_content is None
    assert len(malformed.provider_metadata.reasoning_withholds) == 1


@pytest.mark.asyncio
async def test_tool_batch_applies_its_scrub(composer_service_with_real_sessions, result_session_id, monkeypatch) -> None:
    from dataclasses import replace

    from elspeth.web.composer import tool_batch
    from tests.unit.web.composer.test_compose_loop_persistence import _run_one_turn, _text_response, _tool_batch_response

    original_batch = tool_batch.run_tool_batch
    original_replace = tool_batch.replace
    applied = []

    async def contaminated_batch(*, call_model, ctx, acc, llm_messages):
        completion = replace(
            call_model.completion, provider_metadata=replace(call_model.completion.provider_metadata, reasoning_content=REASONING)
        )
        await original_batch(call_model=replace(call_model, completion=completion), ctx=ctx, acc=acc, llm_messages=llm_messages)
        raise RequestInspected

    def capture_replace(obj, **changes):
        result = original_replace(obj, **changes)
        if "provider_metadata" in changes:
            applied.append(result.provider_metadata)
        return result

    monkeypatch.setattr(tool_batch, "run_tool_batch", contaminated_batch)
    monkeypatch.setattr(tool_batch, "replace", capture_replace)
    delivered = False

    async def complete(*args, **kwargs):
        nonlocal delivered
        if delivered:
            return _text_response("Saved.")
        delivered = True
        return _admit_composer_llm_completion(
            _tool_batch_response(("call_metadata", "set_metadata", {"patch": {"description": "ordinary"}}))
        )

    with pytest.raises(RequestInspected):
        await _run_one_turn(composer_service_with_real_sessions, llm=complete, session_id=result_session_id)
    assert len(applied) == 1
    assert applied[0].reasoning_content is None
    assert applied[0].reasoning_withholds[0].surface == "composer_tool_batch"
    assert REASONING not in repr(applied)


@pytest.mark.asyncio
async def test_text_audit_scrubs_before_call_record(monkeypatch) -> None:
    from elspeth.web.composer import provider_gateway
    from elspeth.web.composer.audit import BufferingRecorder
    from tests.unit.web.composer._helpers import _make_settings

    response = _make_llm_response(content="ordinary")
    response.choices[0].message.__dict__["reasoning_content"] = REASONING

    async def complete(*args, **kwargs):
        return response

    original_build = provider_gateway.build_llm_call_record
    captured = []

    def capture_build(**kwargs):
        metadata = kwargs["response_metadata"]
        assert metadata.reasoning_content is None
        assert len(metadata.reasoning_withholds) == 1
        captured.append(metadata)
        return original_build(**kwargs)

    monkeypatch.setattr(provider_gateway.ProviderGateway, "_call_text_llm", complete)
    monkeypatch.setattr(provider_gateway, "build_llm_call_record", capture_build)
    gateway = provider_gateway.ProviderGateway(
        model="openai/test-model",
        settings=_make_settings(composer_model="openai/test-model"),
        endpoint_base_url=None,
        endpoint_api_key=None,
    )
    result = await gateway._call_text_llm_with_audit([{"role": "user", "content": "ordinary"}], timeout=5, recorder=BufferingRecorder())
    assert result == "ordinary"
    assert len(captured) == 1


def test_tool_call_completion_cannot_withhold_native_replay() -> None:
    response = _make_llm_response(content="ordinary", tool_calls=[{"id": "call_a", "name": "list_plugins", "arguments": {}}])
    response.choices[0].message.__dict__["thinking_blocks"] = [
        {"type": "thinking", "thinking": REASONING, "signature": "opaque"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]
    with pytest.raises(CredentialMaterialRefused) as caught:
        _admit_composer_llm_completion(response)
    assert caught.value.to_payload()["cause"] == "current_tool_loop_thinking_credential"
    assert REASONING not in repr(caught.value.to_payload())


def test_planner_tool_call_record_refuses_credential_thinking_replay() -> None:
    response = _make_llm_response(content="ordinary", tool_calls=[{"id": "call_a", "name": "list_plugins", "arguments": {}}])
    response.choices[0].message.__dict__["thinking_blocks"] = [
        {"type": "thinking", "thinking": REASONING, "signature": "opaque"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]
    with pytest.raises(CredentialMaterialRefused) as caught:
        build_llm_call_record(
            model_requested="test-model",
            messages=[{"role": "user", "content": "ordinary"}],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=datetime.now(UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            response=response,
            credential_surface="composer_planner_response",
        )
    assert caught.value.to_payload()["cause"] == "current_tool_loop_thinking_credential"
    assert REASONING not in repr(caught.value.to_payload())
