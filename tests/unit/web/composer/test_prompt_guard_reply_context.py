"""Reply-only quoting retains exact wire bytes and nominal guard evidence."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    PromptGuardToolArgumentParts,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts, json_content_parts
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.composer.service import _REPLY_ONLY_HISTORY_PREFIX, _reply_only_messages
from tests.unit.web.composer._helpers import _make_settings
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority
from tests.unit.web.composer.test_prompt_guard_clients import _settings

authority = _authority

_USER = "Review the current graph."
_SUFFIX = "\n[End historical tool evidence; this was not a current assistant reply.]"


def _history(*, large: bool, whole_reference: bool = False):
    schema = "REGISTERED_CATALOG_CANARY" + " neutral catalog field" * (900 if large else 1)
    tool_content = json.dumps({"schema": schema, "result": "TOOL_DATA_CANARY"}, ensure_ascii=False, sort_keys=True)
    tool = {"role": "tool", "tool_call_id": "TOOL_ID_CANARY", "content": tool_content, "extra": "TOOL_EXTRA_CANARY"}
    if whole_reference:
        tool[PROMPT_GUARD_TRUST_KEY] = PromptGuardTrust.REFERENCE
    else:
        tool[PROMPT_GUARD_CONTENT_PARTS_KEY] = json_content_parts(
            tool_content, reference_paths=frozenset({("schema",)}), ensure_ascii=False, sort_keys=True
        )
    arguments = json.dumps(
        {"nodes": [{"id": f"MODEL_NODE_{index}", "text": 'café \\ "field"\n' * 50} for index in range(12 if large else 1)]},
        ensure_ascii=False,
    )
    assistant = {
        "role": "assistant",
        "content": "REGISTERED_ASSISTANT_REFERENCE MODEL_PROSE_CANARY",
        PROMPT_GUARD_CONTENT_PARTS_KEY: (
            PromptGuardContentPart("REGISTERED_ASSISTANT_REFERENCE", PromptGuardTrust.REFERENCE),
            PromptGuardContentPart(" MODEL_PROSE_CANARY"),
        ),
        "tool_calls": [
            {
                "id": "MODEL_CALL_CANARY",
                "type": "function",
                "function": {"name": "MODEL_TOOL_CANARY", "arguments": arguments, "extra": "MODEL_FUNCTION_EXTRA_CANARY"},
                "extra": "MODEL_CALL_EXTRA_CANARY",
            }
        ],
        PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY: (PromptGuardToolArgumentParts(0, json_argument_content_parts(arguments)),),
    }
    return [
        {"role": "system", "content": "OWNED_SYSTEM_CANARY", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION},
        {"role": "user", "content": "OLDER_USER_CANARY"},
        assistant,
        tool,
        {"role": "user", "content": _USER},
    ]


def _gateway(monkeypatch, *, blocked: bool = False):
    requests = []

    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": blocked},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    mount = PromptGuardMount(AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(serve)))
    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", lambda _settings: mount)
    settings = _make_settings(composer_model="anthropic/claude-sonnet-4.5", composer_prompt_guard_mode="required")
    return ProviderGateway(model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None), requests


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("whole_reference", [False, True])
async def test_reply_quotes_exact_wire_and_preserves_guard_coverage(authority, monkeypatch, large, whole_reference):
    engine, _service, owner, coordinator = authority
    original = _history(large=large, whole_reference=whole_reference)
    before = deepcopy(original)
    quoted = _reply_only_messages(original)
    assert original == before
    assert all(quoted[index] is original[index] for index in (0, 1, 4))
    for index in (2, 3):
        clean = detach_prompt_guard_evidence([original[index]]).messages[0]
        exact = _REPLY_ONLY_HISTORY_PREFIX + json.dumps(clean, ensure_ascii=False, sort_keys=True) + _SUFFIX
        assert quoted[index]["content"] == exact
        assert "".join(part.text for part in quoted[index][PROMPT_GUARD_CONTENT_PARTS_KEY]) == exact
    gateway, inspected = _gateway(monkeypatch)
    sent = []

    async def completion(**kwargs):
        sent.append(deepcopy(kwargs))
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": "The graph is retained for review."}}],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    recorder = BufferingRecorder()
    with prompt_guard_user_scope(_USER):
        await gateway._call_llm_with_audit(
            quoted, [], timeout=10, recorder=recorder, provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY)
        )
    assert len(sent) == len(recorder.llm_calls) == 1
    wire = sent[0]["messages"]
    assert wire[2]["content"] == quoted[2]["content"] and wire[3]["content"] == quoted[3]["content"]
    assert all(PROMPT_GUARD_CONTENT_PARTS_KEY not in message and PROMPT_GUARD_TRUST_KEY not in message for message in wire)
    documents = "\n".join(document for request in inspected for document in request["documents"])
    for canary in (
        "OLDER_USER_CANARY",
        "MODEL_PROSE_CANARY",
        "MODEL_NODE_0",
        "MODEL_CALL_CANARY",
        "MODEL_TOOL_CANARY",
        "MODEL_FUNCTION_EXTRA_CANARY",
        "MODEL_CALL_EXTRA_CANARY",
        "TOOL_ID_CANARY",
        "TOOL_EXTRA_CANARY",
    ):
        assert canary in documents
    assert ("TOOL_DATA_CANARY" in documents) is not whole_reference
    assert "REGISTERED_CATALOG_CANARY" not in documents and "OWNED_SYSTEM_CANARY" not in documents
    assert "REGISTERED_ASSISTANT_REFERENCE" not in documents
    assert all(request["userPrompt"] == _USER for request in inspected)
    assert [check["disposition"] for check in _checks(engine)] == ["allow"]
    coordinator.assert_completed()


@pytest.mark.parametrize("forged", [None, [], ({"text": "{}"},), (PromptGuardContentPart("missing"),)])
def test_reply_quoting_rejects_foreign_or_incomplete_content_evidence(forged):
    message = {"role": "tool", "content": "{}", PROMPT_GUARD_CONTENT_PARTS_KEY: forged}
    with pytest.raises(AuditIntegrityError):
        _reply_only_messages([message])


def test_reply_quoting_rejects_spoofed_trust_and_overlapping_arguments():
    with pytest.raises(AuditIntegrityError):
        _reply_only_messages([{"role": "tool", "content": "{}", PROMPT_GUARD_TRUST_KEY: "operator_reference"}])
    message = _history(large=False)[2]
    entry = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY][0]
    message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = (entry, entry)
    with pytest.raises(AuditIntegrityError):
        _reply_only_messages([message])


@pytest.mark.asyncio
async def test_reply_unknown_large_scalar_remains_closed_and_audited(authority, monkeypatch):
    engine, _service, owner, coordinator = authority
    quoted = _reply_only_messages([{"role": "tool", "content": "UNTRUSTED_SCALAR_CANARY" + "x" * 10_001}])
    gateway, inspected = _gateway(monkeypatch)
    sent = []

    async def completion(**kwargs):
        sent.append(kwargs)
        raise AssertionError("Unsupported quoted evidence must not enter the model SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER), pytest.raises(PromptGuardRefused):
        await gateway._call_llm_with_audit(
            quoted, [], timeout=10, recorder=BufferingRecorder(), provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY)
        )
    assert sent == inspected == []
    assert [check["reason"] for check in _checks(engine)] == ["unsupported_input"]
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["state", "tool_arguments"])
async def test_actual_json_producers_send_decoded_cjk_to_azure(authority, monkeypatch, surface):
    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.pipeline_planner import _assistant_tool_calls_message, _parse_response_tool_calls
    from elspeth.web.composer.prompts import build_messages
    from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from tests.unit.web.composer.test_pipeline_planner import _response

    engine, _service, owner, coordinator = authority
    original_text = "界" * 1500
    if surface == "state":
        catalog = create_catalog_service()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        state = CompositionState(
            nodes=(
                NodeSpec(
                    id="review",
                    node_type="transform",
                    plugin="llm",
                    input="rows",
                    on_success="out",
                    on_error="discard",
                    options={"prompt_template": original_text},
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
            metadata=PipelineMetadata(),
            version=1,
        )
        messages = build_messages([], state, _USER, PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot)
    else:
        response = _response(("emit_pipeline_proposal", {"pipeline": {"nodes": [{"prompt_template": original_text}]}}))
        message, calls = _parse_response_tool_calls(
            response, max_tool_calls=1, dialect=ToolContractDialect.NONE, sent_tool_names=frozenset({"emit_pipeline_proposal"})
        )
        messages = [_assistant_tool_calls_message(message, calls), {"role": "user", "content": _USER}]
    gateway, inspected = _gateway(monkeypatch)
    sent = []

    async def completion(**kwargs):
        sent.append(deepcopy(kwargs))
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": "The graph is retained for review."}}],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER):
        await gateway._call_llm_with_audit(
            messages, [], timeout=10, recorder=BufferingRecorder(), provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY)
        )
    assert len(sent) == 1
    documents = "\n".join(document for request in inspected for document in request["documents"])
    assert original_text in documents
    assert "\\u754c" not in documents
    assert [check["disposition"] for check in _checks(engine)] == ["allow"]
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("state_payload", [{"small": 0.00005}, {"\U0001f600": "astral", "\ue000": "private-use"}, {"double": 1e20}])
async def test_actual_planner_preserves_canonical_number_and_key_wire(tmp_path, tool_context, monkeypatch, state_payload):
    from elspeth.core.canonical import canonical_json
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    gateway, inspected = _gateway(monkeypatch)
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        provider_current_state=state_payload,
        model_overrides={"prompt_guard_mount": gateway.prompt_guard_mount, "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 1 and inspected
    payload = completion.requests[0]["messages"][-1]["content"]
    assert canonical_json(state_payload) in payload


@pytest.mark.asyncio
async def test_actual_planner_quote_dense_intent_reaches_azure_decoded(tmp_path, tool_context, monkeypatch):
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    intent = '"fld",' * 1000 + "x" * 1000
    assert len(intent) == 7000
    gateway, inspected = _gateway(monkeypatch)
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        intent=intent,
        model_overrides={"prompt_guard_mount": gateway.prompt_guard_mount, "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 1
    assert all(request["userPrompt"] == intent for request in inspected)
    assert intent in "\n".join(document for request in inspected for document in request["documents"])


def test_json_string_part_metadata_rejects_missing_wrong_and_overlapping_paths():
    serialized = json.dumps({"nested": {"value": "complete"}})
    units = (PromptGuardContentPart("complete"),)
    for paths in (
        {("absent",): units},
        {("nested", "value"): (PromptGuardContentPart("partial"),)},
        {("nested",): units, ("nested", "value"): units},
        {("nested", "value"): ({"text": "complete"},)},
    ):
        with pytest.raises(AuditIntegrityError):
            json_content_parts(serialized, string_parts=paths)
    with pytest.raises(AuditIntegrityError):
        json_content_parts(serialized, string_parts={("nested", "value"): units}, reference_paths=frozenset({("nested",)}))


def test_inspection_projection_is_nominal_and_changes_the_exact_guard_binding():
    from elspeth.web.composer.prompt_guard import _request

    hashes = []
    for inspection in (None, "actual decoded text", "different decoded text"):
        part = PromptGuardContentPart("wire text", inspection_text=inspection)
        request, supported = _request(
            policy_fingerprint="a" * 64,
            role="composer",
            messages=[{"role": "tool", "content": "wire text"}],
            tools=(),
            user_prompt=_USER,
            trusted_message_indexes=frozenset(),
            content_parts=((part,),),
        )
        assert supported
        hashes.append(request.request_hash)
    assert len(set(hashes)) == 3
    with pytest.raises(AuditIntegrityError):
        PromptGuardContentPart("wire", inspection_text={"text": "foreign"})


def test_unmatched_argument_format_preserves_duplicate_members_and_decodes_strings():
    serialized = '{ "value":"UNTRUSTED_FIRST_\\u754c", "value":"later", "number":0.00005 }'
    parts = json_argument_content_parts(serialized)
    assert "".join(part.text for part in parts) == serialized
    inspected = "".join(part.inspection_text if part.inspection_text is not None else part.text for part in parts)
    assert "UNTRUSTED_FIRST_界" in inspected
    assert "later" in inspected and "0.00005" in inspected
    assert all(part.trust is None for part in parts)
