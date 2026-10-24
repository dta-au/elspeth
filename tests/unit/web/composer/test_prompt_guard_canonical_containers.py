"""Canonical prompt JSON stays byte-identical across recursive container boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from elspeth.contracts.composer_content import CompleteValueManifest, ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.core.canonical import canonical_json as core_canonical_json
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer import prompts as composer_prompts
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import _feedback_content_parts
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tools import _common as tool_common
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.unit.web.composer._helpers import _empty_state
from tests.unit.web.composer.test_pipeline_planner import _origin, _pipeline, _plan, _response, _ScriptedCompletion


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


def test_existing_feedback_producer_preserves_long_canonical_unicode_keys_and_numbers():
    # A direct unit test of the existing feedback renderer, not a claim that
    # every synthetic validation detail is emitted by the current compiler.
    feedback = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": "pipeline",
                    "severity": "high",
                    "error_code": "canonical_schema",
                    "error_class": "SchemaValidationError",
                    "validation_errors": [{"nested": _long_payload()}],
                }
            ],
        },
    }
    serialized = canonical_json(feedback)
    assert len(serialized) > 2048
    parts = _feedback_content_parts(feedback)
    assert "".join(part.text for part in parts) == serialized
    assert all(part.trust is None for part in parts)


@pytest.mark.asyncio
async def test_existing_initial_planner_producer_sends_bounded_state_and_reads_complete_value_exactly(tmp_path, tool_context):
    inspected = []

    def inspect(request):
        body = json.loads(request.content)
        inspected.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in body["documents"]],
            },
        )

    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://planner.example.test", subscription_key="offline-planner"),
            http_transport=httpx.MockTransport(inspect),
        )
    )
    payload = _long_payload()
    state = _state_with_payload(tmp_path, payload)
    origin = _origin()

    class ReadCompleteValueCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs):
            if not self.requests:
                view = json.loads(kwargs["messages"][2]["content"])["current_state"]
                manifest = CompleteValueManifest.from_payload(view["content_manifest"])
                assert manifest.scope == ComposerContentScope(UUID(origin.session_id), composition_content_hash(state))
                assert len(manifest.values) == 1
                reference = manifest.values[0]
                assert reference.path == ("sources", "source", "options", "canonical_fixture", "\ue000", "text")
                self._responses.insert(0, _response(("get_complete_pipeline_value", {"reference_id": reference.reference_id})))
            return await super().__call__(**kwargs)

    completion = ReadCompleteValueCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        current_state=state,
        originating_message=origin,
        provider_current_state={"unowned": "MUST_NOT_REPLACE_CURRENT_GRAPH"},
        model_overrides={"prompt_guard_mount": mount, "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == len(inspected) == 2
    first_wire = completion.requests[0]["messages"][2]["content"]
    view = json.loads(first_wire)["current_state"]
    assert view["schema_version"] == "composer.bounded-view.v1"
    manifest = CompleteValueManifest.from_payload(view["content_manifest"])
    displayed = view["data"]["sources"]["source"]["options"]["canonical_fixture"]
    assert displayed["\ue000"]["text"] == {
        "complete_value_ref": manifest.values[0].reference_id,
        "content_hash": manifest.values[0].content_hash,
        "utf8_bytes": manifest.values[0].utf8_bytes,
    }
    assert displayed["\U0001f600"] == payload["\U0001f600"]
    assert displayed["\ue000"]["small"] == payload["\ue000"]["small"]
    assert displayed["\ue000"]["double"] == payload["\ue000"]["double"]
    assert list(displayed) == ["\U0001f600", "\ue000"]
    assert canonical_json(canonical_json_loads(first_wire)) == first_wire
    assert "0.00005" in first_wire and "100000000000000000000" in first_wire
    assert "MUST_NOT_REPLACE_CURRENT_GRAPH" not in first_wire
    assert payload["\ue000"]["text"] not in first_wire

    issued = [call for message in completion.requests[1]["messages"] if message["role"] == "assistant" for call in message["tool_calls"]]
    assert len(issued) == 1
    assert issued[0]["function"]["name"] == "get_complete_pipeline_value"
    selected = [
        message
        for message in completion.requests[1]["messages"]
        if message["role"] == "tool" and message["tool_call_id"] == issued[0]["id"]
    ]
    assert len(selected) == 1
    response = json.loads(selected[0]["content"])
    assert response["success"] is True
    complete = response["data"]
    assert complete["session_id"] == origin.session_id
    assert complete["composition_content_hash"] == composition_content_hash(state)
    assert complete["reference_id"] == manifest.values[0].reference_id
    assert complete["path"] == list(manifest.values[0].path)
    assert complete["content_hash"] == manifest.values[0].content_hash
    assert complete["utf8_bytes"] == manifest.values[0].utf8_bytes
    assert complete["value"] == payload["\ue000"]["text"]
    initial_projection = "\n".join(inspected[0]["documents"])
    selected_projection = "\n".join(inspected[1]["documents"])
    assert payload["\ue000"]["text"] not in initial_projection
    assert payload["\ue000"]["text"] in selected_projection
    projection = "\n".join(document for request in inspected for document in request["documents"])
    assert payload["\ue000"]["text"] in projection
    assert payload["\U0001f600"]["text"] in projection
    assert "ASCII-TAIL-CANARY" in projection and "UNICODE-TAIL-CANARY" in projection


@pytest.mark.asyncio
async def test_initial_planner_inspects_complete_credential_tail_before_projection_or_sdk(tmp_path, tool_context, monkeypatch):
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
    monkeypatch.setattr(tool_common, "build_bounded_composition_view", forbidden_projection)
    completion = _ScriptedCompletion()
    recorder = BufferingRecorder()
    with pytest.raises(CredentialMaterialRefused) as refused:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            current_state=state,
            provider_current_state=state.to_dict(),
        )
    assert refused.value.surface == "composer_bounded_content_projection"
    assert refused.value.finding.category == "credential_url"
    assert inspected_states == [state.to_dict()]
    assert inspected_states[0]["sources"]["source"]["options"]["canonical_fixture"]["\ue000"]["text"] == payload["\ue000"]["text"]
    assert completion.requests == []
    assert recorder.llm_calls == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["__elspeth_canonical_type__", "ordinary_field"], ids=["reserved-tag", "ordinary-neighbor"])
async def test_real_initial_planner_preserves_core_normalized_state_option(tmp_path, tool_context, monkeypatch, field_name):
    inspected = []
    rendered = []

    def inspect(request):
        body = json.loads(request.content)
        inspected.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in body["documents"]],
            },
        )

    real_render = pipeline_planner.json_content_parts

    def observe_render(serialized, **kwargs):
        # Forward the actual producer's serializer/codec arguments unchanged;
        # never catch its integrity failure or manufacture nominal evidence.
        parts = real_render(serialized, **kwargs)
        rendered.append((serialized, parts))
        return parts

    monkeypatch.setattr(pipeline_planner, "json_content_parts", observe_render)
    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://planner.example.test", subscription_key="offline-planner"),
            http_transport=httpx.MockTransport(inspect),
        )
    )
    source = _pipeline(tmp_path)["source"]
    field_mapping = {field_name: "x"}
    state = replace(
        _empty_state(),
        sources={"source": SourceSpec(**{**source, "options": {**source["options"], "field_mapping": field_mapping}})},
    )
    provider_state = state.to_dict()
    assert provider_state["sources"]["source"]["options"]["field_mapping"] == field_mapping
    expected_wire_state = json.loads(core_canonical_json(provider_state))
    origin = _origin()
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        current_state=state,
        originating_message=origin,
        provider_current_state=provider_state,
        information_aware=True,
        model_overrides={"prompt_guard_mount": mount, "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 1
    assert inspected
    initial = [(wire, parts) for wire, parts in rendered if "current_state" in json.loads(wire)]
    assert len(initial) == 1
    wire, parts = initial[0]
    assert "".join(part.text for part in parts) == wire
    assert all(part.trust is None for part in parts)
    # The real seed places state after system and reference messages; later
    # planner instructions can append another message before the SDK call.
    assert completion.requests[0]["messages"][2]["role"] == "user"
    assert completion.requests[0]["messages"][2]["content"] == wire
    view = json.loads(wire)["current_state"]
    assert view["schema_version"] == "composer.bounded-view.v1"
    manifest = CompleteValueManifest.from_payload(view["content_manifest"])
    assert manifest.scope == ComposerContentScope(UUID(origin.session_id), composition_content_hash(state))
    assert manifest.values == ()
    actual_wire_state = view["data"]
    assert actual_wire_state == expected_wire_state
    assert json.loads(core_canonical_json(field_mapping)) == actual_wire_state["sources"]["source"]["options"]["field_mapping"]
