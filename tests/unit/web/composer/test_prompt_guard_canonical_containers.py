"""Live common-context JSON and tool-result producers preserve complete values."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import httpx
import litellm
import pytest

from elspeth.contracts.composer_content import CompleteValueManifest, ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.core.canonical import canonical_json as core_canonical_json
from elspeth.web.composer import prompts as composer_prompts
from elspeth.web.composer.discovery_cache import serialize_tool_result, tool_result_content_parts
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer._helpers import _empty_state
from tests.unit.web.composer._scripted_sdk import _pipeline, _response, _ScriptedCompletion, _text_response
from tests.unit.web.composer.test_prompt_guard_wiring import _guarded_service

pytest_plugins = ("tests.unit.web.composer.test_prompt_guard_wiring",)
_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean


def _azure_client(inspected):
    def inspect(request):
        body = json.loads(request.content)
        inspected.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    return AzurePromptGuardClient(
        PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="offline-guard"),
        http_transport=httpx.MockTransport(inspect),
    )


def _state_wire(messages):
    return next(
        row["content"]
        for row in messages
        if isinstance(row["content"], str) and row["content"].startswith(composer_prompts.STATE_CONTEXT_PREFIX)
    )


def _long_payload():
    # Canonical UTF-16 key ordering differs from Python code-point ordering.
    # Integral doubles outside the safe integer range need the paired inverse.
    return {
        "\ue000": {"small": 0.00005, "double": 1e20, "text": '"field",' * 450 + "ASCII-TAIL-CANARY"},
        "\U0001f600": {"text": "界" * 700 + "UNICODE-TAIL-CANARY", "empty": []},
    }


def _state_with_payload(tmp_path, payload):
    source = _pipeline(tmp_path)["source"]
    return replace(
        _empty_state(),
        sources={"source": SourceSpec(**{**source, "options": {**source["options"], "canonical_fixture": payload}})},
    )


def test_long_canonical_renderer_requires_matching_wire_format_and_preserves_all_units():
    payload = _long_payload()
    serialized = canonical_json(payload)
    assert len(serialized) > 2048
    assert serialized.index("\U0001f600") < serialized.index("\ue000")
    assert "0.00005" in serialized and "100000000000000000000" in serialized
    # Deliberately mismatched formatting remains an integrity error; the exact
    # wire assertion is a control and must not be removed to admit the request.
    with pytest.raises(AuditIntegrityError, match="exact wire serialization"):
        json_content_parts(serialized, encoder=canonical_json, decoder=canonical_json_loads)
    parts = json_content_parts(
        serialized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        encoder=canonical_json,
        decoder=canonical_json_loads,
    )
    assert "".join(part.text for part in parts) == serialized
    assert all(part.trust is None for part in parts)
    projection = "".join(part.inspection_text if part.inspection_text is not None else part.text for part in parts)
    assert payload["\ue000"]["text"] in projection
    assert payload["\U0001f600"]["text"] in projection
    assert "0.00005" in projection and "100000000000000000000" in projection


def test_live_tool_result_producer_preserves_long_json_unicode_keys_and_numbers():
    state = _empty_state()
    payload = _long_payload()
    result = ToolResult(success=False, updated_state=state, validation=state.validate(), affected_nodes=(), data=payload)
    serialized = serialize_tool_result(result)
    assert len(serialized) > 2048
    parts = tool_result_content_parts(result, serialized)
    assert "".join(part.text for part in parts) == serialized
    assert all(part.trust is None for part in parts)
    assert json.loads(serialized)["data"] == payload


@pytest.mark.asyncio
async def test_live_common_context_reads_complete_value_exactly(tmp_path, invocation, monkeypatch):
    inspected = []
    payload = _long_payload()
    state = _state_with_payload(tmp_path, payload)
    owner = invocation[2]
    context = owner.required_work.coordinator.authority.context
    session_id = context.fence.session_id
    scope = ComposerContentScope(UUID(session_id), composition_content_hash(state))
    service = _guarded_service(invocation, monkeypatch, composer_client=_azure_client(inspected), catalog=create_catalog_service())

    class ReadCompleteValueCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs):
            if not self.requests:
                view = json.loads(_state_wire(kwargs["messages"]).split("\n", 1)[1])["current_state"]
                manifest = CompleteValueManifest.from_payload(view["content_manifest"])
                assert manifest.scope == scope and len(manifest.values) == 1
                reference = manifest.values[0]
                assert reference.path == ("sources", "source", "options", "canonical_fixture", "\ue000", "text")
                self._responses.insert(0, _response(("get_complete_pipeline_value", {"reference_id": reference.reference_id})))
            return await super().__call__(**kwargs)

    completion = ReadCompleteValueCompletion(_text_response("The complete value is retained."))
    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await service.compose(
        "Read the complete current value without editing the pipeline.",
        [],
        state,
        session_id=session_id,
        user_id="alice",
        session_operation_context=context,
        required_work=owner.required_work,
        provider_owner=owner,
    )
    assert result.state is state
    assert len(completion.requests) == len(inspected) == 2
    first_wire = _state_wire(completion.requests[0]["messages"])
    view = json.loads(first_wire.split("\n", 1)[1])["current_state"]
    manifest = CompleteValueManifest.from_payload(view["content_manifest"])
    displayed = view["data"]["sources"]["source"]["options"]["canonical_fixture"]
    reference = manifest.values[0]
    assert displayed["\ue000"]["text"] == {
        "complete_value_ref": reference.reference_id,
        "content_hash": reference.content_hash,
        "utf8_bytes": reference.utf8_bytes,
    }
    assert displayed["\U0001f600"] == payload["\U0001f600"]
    assert displayed["\ue000"]["small"] == payload["\ue000"]["small"]
    assert displayed["\ue000"]["double"] == payload["\ue000"]["double"]
    assert payload["\ue000"]["text"] not in first_wire
    issued = [call for message in completion.requests[1]["messages"] if message["role"] == "assistant" for call in message["tool_calls"]]
    assert len(issued) == 1 and issued[0]["function"]["name"] == "get_complete_pipeline_value"
    selected = [
        message
        for message in completion.requests[1]["messages"]
        if message["role"] == "tool" and message["tool_call_id"] == issued[0]["id"]
    ]
    assert len(selected) == 1
    response = json.loads(selected[0]["content"])
    assert response["success"] is True
    complete = response["data"]
    assert complete == {
        "session_id": session_id,
        "composition_content_hash": scope.composition_content_hash,
        "reference_id": reference.reference_id,
        "path": list(reference.path),
        "content_hash": reference.content_hash,
        "utf8_bytes": reference.utf8_bytes,
        "value": payload["\ue000"]["text"],
    }
    initial_projection = "\n".join(inspected[0]["documents"])
    selected_projection = "\n".join(inspected[1]["documents"])
    assert payload["\ue000"]["text"] not in initial_projection
    assert payload["\ue000"]["text"] in selected_projection
    projection = "\n".join(document for request in inspected for document in request["documents"])
    assert payload["\U0001f600"]["text"] in projection
    assert "ASCII-TAIL-CANARY" in projection and "UNICODE-TAIL-CANARY" in projection
    invocation[3].assert_completed()


def test_live_initial_context_inspects_complete_credential_tail_before_projection(tmp_path, tool_context, monkeypatch):
    payload = _long_payload()
    payload["\ue000"]["text"] += " https://alice:synthetic-password@example.test/private"
    state = _state_with_payload(tmp_path, payload)
    inspected_states = []
    real_scan = composer_prompts.require_no_credential_material_in_state

    def inspect_complete_state(current, **kwargs):
        assert current is state
        inspected_states.append(current.to_dict())
        return real_scan(current, **kwargs)

    def forbidden_projection(*_args, **_kwargs):
        raise AssertionError("Credential-bearing complete state must refuse before bounded projection")

    monkeypatch.setattr(composer_prompts, "require_no_credential_material_in_state", inspect_complete_state)
    monkeypatch.setattr(composer_prompts, "build_bounded_composition_view", forbidden_projection)
    with pytest.raises(CredentialMaterialRefused) as refused:
        composer_prompts.build_messages(
            [],
            state,
            "Inspect the current graph.",
            tool_context.catalog,
            plugin_snapshot=tool_context.plugin_snapshot,
            content_scope=ComposerContentScope(UUID(int=1), composition_content_hash(state)),
        )
    assert refused.value.surface == "composer_bounded_content_projection"
    assert refused.value.finding.category == "credential_url"
    assert inspected_states == [state.to_dict()]
    assert inspected_states[0]["sources"]["source"]["options"]["canonical_fixture"]["\ue000"]["text"] == payload["\ue000"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["__elspeth_canonical_type__", "ordinary_field"], ids=["reserved-tag", "ordinary-neighbor"])
async def test_live_initial_context_preserves_core_normalized_state_option(tmp_path, invocation, monkeypatch, field_name):
    inspected, rendered = [], []
    real_render = composer_prompts.json_content_parts

    def observe_render(serialized, **kwargs):
        parts = real_render(serialized, **kwargs)
        rendered.append((serialized, parts))
        return parts

    monkeypatch.setattr(composer_prompts, "json_content_parts", observe_render)
    source = _pipeline(tmp_path)["source"]
    field_mapping = {field_name: "x"}
    state = replace(
        _empty_state(), sources={"source": SourceSpec(**{**source, "options": {**source["options"], "field_mapping": field_mapping}})}
    )
    before = state.to_dict()
    expected_wire_state = json.loads(core_canonical_json(before))
    owner = invocation[2]
    context = owner.required_work.coordinator.authority.context
    service = _guarded_service(invocation, monkeypatch, composer_client=_azure_client(inspected), catalog=create_catalog_service())
    completion = _ScriptedCompletion(_text_response("The current graph is retained."))
    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await service.compose(
        "Describe the current graph without editing it.",
        [],
        state,
        session_id=context.fence.session_id,
        user_id="alice",
        session_operation_context=context,
        required_work=owner.required_work,
        provider_owner=owner,
    )
    assert result.state is state and state.to_dict() == before
    assert len(completion.requests) == len(inspected) == 1
    wire = _state_wire(completion.requests[0]["messages"])
    serialized = wire.split("\n", 1)[1]
    parts = next(parts for text, parts in rendered if text == serialized)
    assert "".join(part.text for part in parts) == serialized
    assert any("field_mapping" in part.text for part in parts)
    assert all(part.trust is None for part in parts if "field_mapping" in part.text or field_name in part.text)
    view = json.loads(serialized)["current_state"]
    manifest = CompleteValueManifest.from_payload(view["content_manifest"])
    assert manifest.scope == ComposerContentScope(UUID(context.fence.session_id), composition_content_hash(state))
    assert manifest.values == ()
    assert view["data"] == expected_wire_state
    assert view["data"]["sources"]["source"]["options"]["field_mapping"] == json.loads(core_canonical_json(field_mapping))
    invocation[3].assert_completed()
