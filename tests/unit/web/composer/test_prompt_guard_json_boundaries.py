"""Decoded JSON evidence preserves credential boundaries through real producers."""

from __future__ import annotations

import json
from copy import deepcopy

import litellm
import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY, PromptGuardToolArgumentParts, detach_prompt_guard_messages
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
from elspeth.web.composer.pipeline_planner import _assistant_tool_calls_message, _parse_response_tool_calls
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts
from elspeth.web.composer.prompts import build_messages
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority
from tests.unit.web.composer.test_prompt_guard_reply_context import _gateway

authority = _authority

_USER = "Review this retained graph configuration."


def _options(shape, following):
    if shape == "dataverse":
        options = {
            "environment_url": "https://tenant.crm.dynamics.com",
            "auth": {"method": "managed_identity"},
            "entity": "contact",
            "select": ["contactid", "fullname"],
            "schema": {"mode": "observed"},
        }
        list_key = "select"
    else:
        options = {
            "endpoint": "https://my-resource.cognitiveservices.azure.com",
            "fields": ["content", "title"],
            "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 0},
            "schema": {"mode": "observed"},
        }
        list_key = "fields"
    if following == "at_sign":
        del options[list_key]
        options["z_following"] = "analyst@example.test"
    options["notes"] = "境界の確認"
    return options


def _state(shape, options):
    nodes = ()
    sources = {}
    if shape == "dataverse":
        sources["source"] = SourceSpec(plugin="dataverse", on_success="rows", options=options, on_validation_failure="discard")
    else:
        nodes = (
            NodeSpec(
                id="review",
                node_type="transform",
                plugin="azure_content_safety",
                input="rows",
                on_success="out",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        )
    return CompositionState(sources=sources, nodes=nodes, edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _tool_messages(options):
    arguments = json.dumps({"pipeline": {"options": options}}, separators=(",", ":"))
    response = _response(("emit_pipeline_proposal", {}))
    response.choices[0].message.tool_calls[0].function.arguments = arguments
    message, calls = _parse_response_tool_calls(
        response, max_tool_calls=1, dialect=ToolContractDialect.NONE, sent_tool_names=frozenset({"emit_pipeline_proposal"})
    )
    return [_assistant_tool_calls_message(message, calls), {"role": "user", "content": _USER}]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["freeform_state", "canonical_planner", "compact_tool_arguments"])
@pytest.mark.parametrize("shape", ["dataverse", "content_safety"])
@pytest.mark.parametrize("following", ["array", "at_sign"])
async def test_public_urls_keep_json_token_boundaries(authority, tmp_path, tool_context, monkeypatch, surface, shape, following):
    engine, _service, owner, coordinator = authority
    options = _options(shape, following)
    state = _state(shape, options)
    gateway, inspected = _gateway(monkeypatch)
    if surface == "canonical_planner":
        from elspeth.core.canonical import canonical_json

        completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            provider_current_state=state.to_dict(),
            model_overrides={"prompt_guard_mount": gateway.prompt_guard_mount, "prompt_guard_mode": "required"},
        )
        assert len(completion.requests) == 1
        assert canonical_json(state.to_dict()) in completion.requests[0]["messages"][-1]["content"]
    else:
        if surface == "freeform_state":
            catalog = create_catalog_service()
            snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
            messages = build_messages([], state, _USER, PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot)
        else:
            messages = _tool_messages(options)
        expected_wire, _trusted = detach_prompt_guard_messages(messages)
        expected_wire, _tools = apply_anthropic_cache_markers(expected_wire, [], mark_history_tail=True)
        sent = []

        async def completion(**kwargs):
            sent.append(deepcopy(kwargs))
            return litellm.ModelResponse(
                model=kwargs["model"],
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                choices=[{"message": {"role": "assistant", "content": "Configuration retained for review."}}],
            )

        monkeypatch.setattr(litellm, "acompletion", completion)
        recorder = BufferingRecorder()
        with prompt_guard_user_scope(_USER):
            await gateway._call_llm_with_audit(
                messages, [], timeout=10, recorder=recorder, provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY)
            )
        assert len(sent) == len(recorder.llm_calls) == 1
        assert sent[0]["messages"] == expected_wire
        assert [check["disposition"] for check in _checks(engine)] == ["allow"]
    coordinator.assert_completed()
    documents = "\n".join(document for request in inspected for document in request["documents"])
    url = options["environment_url" if shape == "dataverse" else "endpoint"]
    assert url in documents and "境界の確認" in documents
    assert "\\u5883" not in documents
    if following == "at_sign":
        assert "analyst@example.test" in documents
    else:
        assert all(value in documents for value in options["select" if shape == "dataverse" else "fields"])


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://fixture-user:fixture-pass@example.test", "https://example.test?api_key=fixture-value"])
async def test_decoded_credential_tokens_remain_closed_at_physical_guard(authority, monkeypatch, url):
    engine, _service, owner, coordinator = authority
    gateway, inspected = _gateway(monkeypatch)
    serialized = json.dumps({"endpoint": url, "fields": ["content", "title"]}, separators=(",", ":"))
    serialized = serialized.replace("https:", "https\\u003a").replace("@", "\\u0040").replace("api_key", "api\\u005fkey")
    assert json.loads(serialized)["endpoint"] == url
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "prior-call", "type": "function", "function": {"name": "emit_pipeline_proposal", "arguments": serialized}}
            ],
            PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY: (PromptGuardToolArgumentParts(0, json_argument_content_parts(serialized)),),
        },
        {"role": "user", "content": _USER},
    ]
    sent = []

    async def completion(**kwargs):
        sent.append(kwargs)
        raise AssertionError("Decoded credential evidence must not enter the SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER), pytest.raises(CredentialMaterialRefused) as caught:
        await gateway._call_llm_with_audit(
            messages, [], timeout=10, recorder=BufferingRecorder(), provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY)
        )
    assert caught.value.surface == "composer_prompt_guard_request"
    assert caught.value.finding.category in {"credential_url", "sensitive_url_parameter"}
    assert sent == inspected == _checks(engine) == []
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://fixture-user:fixture-pass@example.test", "https://example.test?api_key=fixture-value"])
async def test_actual_credentials_still_refuse_before_detector_and_sdk(authority, monkeypatch, url):
    engine, _service, owner, coordinator = authority
    gateway, inspected = _gateway(monkeypatch)
    sent = []

    async def completion(**kwargs):
        sent.append(kwargs)
        raise AssertionError("Credential-bearing authoring data must not enter the SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER), pytest.raises(CredentialMaterialRefused) as caught:
        messages = _tool_messages({"endpoint": url, "fields": ["content", "title"]})
        await gateway._call_llm_with_audit(
            messages, [], timeout=10, recorder=BufferingRecorder(), provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY)
        )
    assert caught.value.finding.category in {"credential_url", "sensitive_url_parameter"}
    assert sent == inspected == _checks(engine) == []
    coordinator.assert_completed()
