"""Canonical prompt JSON stays byte-identical across recursive container boundaries."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.core.canonical import canonical_json as core_canonical_json
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.pipeline_planner import _feedback_content_parts
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompt_guard_rendering import json_content_parts
from elspeth.web.composer.state import SourceSpec
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.unit.web.composer._helpers import _empty_state
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion


def _long_payload():
    # Canonical UTF-16 key ordering differs from Python code-point ordering.
    # Integral doubles outside the safe integer range need the paired inverse.
    return {
        "\ue000": {"small": 0.00005, "double": 1e20, "text": '"field",' * 450},
        "\U0001f600": {"text": "界" * 700, "empty": []},
    }


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
async def test_existing_initial_planner_producer_sends_long_canonical_state_exactly(tmp_path, tool_context, monkeypatch):
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
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        provider_current_state=payload,
        model_overrides={"prompt_guard_mount": mount, "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 1
    assert inspected
    assert canonical_json(payload) in completion.requests[0]["messages"][-1]["content"]
    projection = "\n".join(document for request in inspected for document in request["documents"])
    assert payload["\ue000"]["text"] in projection
    assert payload["\U0001f600"]["text"] in projection


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
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        current_state=state,
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
    actual_wire_state = json.loads(wire)["current_state"]
    assert actual_wire_state == expected_wire_state
    assert json.loads(core_canonical_json(field_mapping)) == actual_wire_state["sources"]["source"]["options"]["field_mapping"]
