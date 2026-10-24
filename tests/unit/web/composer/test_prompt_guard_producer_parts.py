"""Real producer payloads fit Azure without changing their provider wire text."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import litellm
import pytest
from pydantic import SecretStr

from elspeth.contracts import prompt_guard as guard_contracts
from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import prompt_guard
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.prompts import build_context_string, build_messages, build_run_diagnostics_messages
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.diagnostics import llm_safe_diagnostics_snapshot
from elspeth.web.execution.schemas import RunDiagnosticsResponse
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.unit.web.composer.test_prompt_guard_wiring import _guarded_service

pytest_plugins = ("tests.unit.web.composer.test_prompt_guard_wiring",)

_USER = "Preserve the neutral record fields."


@pytest.fixture
def catalog():
    registry = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(registry)
    return PolicyCatalogView.for_trained_operator(registry, snapshot), snapshot


@pytest.fixture
def azure():
    sent = []

    def handle(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    client = AzurePromptGuardClient(
        PromptGuardSettings(endpoint="https://guard.example.test", subscription_key=SecretStr("synthetic-test-key")),
        http_transport=httpx.MockTransport(handle),
    )
    return client, sent


def _empty():
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _current_scope(invocation, state):
    context = invocation[2].required_work.coordinator.authority.context
    return ComposerContentScope(UUID(context.fence.session_id), composition_content_hash(state))


def _selected_complete_value_message(invocation, state, path):
    """Read one explicitly selected current value through its real tool owner."""
    from elspeth.web.composer.discovery_cache import tool_result_content_parts
    from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
    from elspeth.web.composer.tools import execute_tool
    from elspeth.web.composer.tools._common import ToolContext, prepare_bounded_pipeline_state

    _engine, sessions, owner, _coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    tool_context = ToolContext(
        catalog=policy,
        plugin_snapshot=snapshot,
        session_id=context.fence.session_id,
        user_id="alice",
        session_operation_context=context,
        session_operation_authority=sessions.session_operation_authority,
    )
    view = prepare_bounded_pipeline_state(state, tool_context)
    selected = next(reference for reference in view.manifest.values if reference.path == path)
    result = execute_tool(
        "get_complete_pipeline_value",
        {"reference_id": selected.reference_id},
        state,
        policy,
        plugin_snapshot=snapshot,
        session_id=context.fence.session_id,
        user_id="alice",
        session_operation_context=context,
        session_operation_authority=sessions.session_operation_authority,
        validate_arguments=True,
    )
    assert result.success
    admitted = admit_discovery_result("get_complete_pipeline_value", result)
    serialized = serialize_admitted_discovery_result(admitted)
    assert json.loads(serialized)["data"]["reference_id"] == selected.reference_id
    return {
        "role": "tool",
        "tool_call_id": "selected-complete-value",
        "content": serialized,
        guard_contracts.PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(admitted, serialized),
    }


def _selected_advisor_transcript(invocation, state, paths):
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner

    transcript = AdvisorTranscriptOwner(request=_USER)
    transcript.begin_messages([])
    for index, path in enumerate(paths):
        message = _selected_complete_value_message(invocation, state, path)
        message["tool_call_id"] = f"selected-complete-value-{index}"
        transcript.observe_messages([message])
    return transcript.snapshot()


async def _inspect(azure, messages, *, user=_USER, role="composer"):
    client, sent = azure
    evidence = guard_contracts.detach_prompt_guard_evidence(messages)
    request, _supported = prompt_guard._request(
        policy_fingerprint=client.policy_fingerprint,
        role=role,
        messages=evidence.messages,
        tools=(),
        user_prompt=user,
        trusted_message_indexes=evidence.trusted_message_indexes,
        content_parts=evidence.content_parts,
        tool_argument_parts=evidence.tool_argument_parts,
    )
    decision = await client.inspect(request)
    return decision, request, evidence, sent


@pytest.mark.asyncio
async def test_compose_loaded_llm_schema_preserves_wire_and_graph_omission_evidence(catalog, azure):
    policy, snapshot = catalog
    state = _empty()
    loaded = frozenset({("transform", "llm")})
    scope = ComposerContentScope(UUID(int=1), composition_content_hash(state))
    original = build_context_string(state, policy, plugin_snapshot=snapshot, schemas_loaded=loaded, content_scope=scope)
    messages = build_messages([], state, _USER, policy, plugin_snapshot=snapshot, schemas_loaded=loaded, content_scope=scope)
    assert messages[-2]["content"] == original
    decision, request, evidence, sent = await _inspect(azure, messages)
    assert decision.disposition == "allow"
    assert sent
    assert evidence.messages[-2]["content"] == original
    assert '"json_schema"' not in "\n".join(request.documents)
    assert "current_state" in "\n".join(request.documents)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["required", "debug-when-unconfigured"])
@pytest.mark.parametrize("configured", [False, True])
async def test_patched_large_scalar_survives_storage_and_short_followup_without_wire_changes(
    invocation, monkeypatch, tmp_path, catalog, azure, mode, configured
):
    from elspeth.web.composer import provider_gateway
    from elspeth.web.composer.provider_quota import ProviderInvocationFamily
    from elspeth.web.composer.tools._common import ToolContext
    from elspeth.web.composer.tools.transforms import _execute_patch_node_options
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.sessions.routes._helpers import _state_from_record
    from tests.unit.web.composer._helpers import _make_settings
    from tests.unit.web.composer.test_prompt_guard_admission import _checks

    policy, snapshot = catalog
    engine, sessions, owner, coordinator = invocation
    context = owner.required_work.coordinator.authority.context
    original = _empty().with_node(
        NodeSpec(
            id="label",
            node_type="transform",
            plugin="value_transform",
            input="records",
            on_success="labelled",
            on_error="discard",
            options={"schema": {"mode": "observed"}, "operations": [{"target": "label", "expression": "'short'"}]},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    expression = "'" + "x" * 25_000 + "境界😀'"
    result = _execute_patch_node_options(
        {"node_id": "label", "patch": {"operations": [{"target": "label", "expression": expression}]}},
        original,
        ToolContext(catalog=policy, plugin_snapshot=snapshot, data_dir=str(tmp_path)),
    )
    assert result.success and result.updated_state.version > original.version
    assert original.nodes[0].options["operations"][0]["expression"] == "'short'"
    state_data = result.updated_state.to_dict()
    saved = await sessions.save_composition_state(
        UUID(context.fence.session_id),
        CompositionStateData(
            sources=state_data["sources"],
            nodes=state_data["nodes"],
            edges=state_data["edges"],
            outputs=state_data["outputs"],
            metadata_=state_data["metadata"],
        ),
        provenance="tool_call",
        session_operation_context=context,
    )
    reloaded = await sessions.get_current_state(UUID(context.fence.session_id))
    assert reloaded.id == saved.id
    restored = _state_from_record(reloaded)
    assert restored.nodes[0].options["operations"][0]["expression"] == expression
    followup = "Keep the saved draft."
    messages = build_messages(
        [],
        restored,
        followup,
        policy,
        plugin_snapshot=snapshot,
        schemas_loaded=frozenset(),
        content_scope=ComposerContentScope(UUID(context.fence.session_id), composition_content_hash(restored)),
    )
    evidence = guard_contracts.detach_prompt_guard_evidence(messages)
    graph_parts = evidence.content_parts[-2]
    assert graph_parts is not None and "".join(part.text for part in graph_parts) == messages[-2]["content"]
    view = json.loads(messages[-2]["content"].split("\n", 1)[1])["current_state"]
    assert expression not in messages[-2]["content"]
    assert [reference["path"] for reference in view["content_manifest"]["values"]] == [
        ["nodes", 0, "options", "operations", 0, "expression"]
    ]
    selected = _selected_complete_value_message(invocation, restored, ("nodes", 0, "options", "operations", 0, "expression"))
    assert json.loads(selected["content"])["data"]["value"] == expression
    messages.insert(
        -1,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": selected["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": "get_complete_pipeline_value",
                        "arguments": json.dumps({"reference_id": json.loads(selected["content"])["data"]["reference_id"]}),
                    },
                }
            ],
        },
    )
    messages.insert(-1, selected)
    evidence = guard_contracts.detach_prompt_guard_evidence(messages)
    scalar_parts = [part for part in selected[guard_contracts.PROMPT_GUARD_CONTENT_PARTS_KEY] if part.inspection_text == expression]
    assert len(scalar_parts) == 1 and scalar_parts[0].trust is None
    client, sent = azure
    monkeypatch.setattr(provider_gateway, "build_prompt_guard_mount", lambda _settings: PromptGuardMount(client) if configured else None)
    settings = _make_settings(composer_model="gpt-4o", composer_prompt_guard_mode=mode)
    gateway = provider_gateway.ProviderGateway(
        model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    transmitted = []

    async def completion(**kwargs):
        assert _checks(engine)[0]["disposition"] == "debug_bypass"
        transmitted.append(kwargs)
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": "The saved draft is retained."}}],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    with prompt_guard_user_scope(followup):
        operation = gateway._call_llm_with_audit(messages, [], timeout=10, recorder=recorder, provider_custody=custody)
        if mode == "debug-when-unconfigured" and not configured:
            await operation
            assert len(transmitted) == len(recorder.llm_calls) == 1
            assert transmitted[0]["messages"] == evidence.messages
        else:
            with pytest.raises(prompt_guard.PromptGuardRefused) as refused:
                await operation
            assert refused.value.http_status == 422
            assert transmitted == [] and recorder.llm_calls == () and custody.attempt is None
    assert sent == []
    current = await sessions.get_current_state(UUID(context.fence.session_id))
    assert current.id == saved.id and _state_from_record(current).nodes[0].options["operations"][0]["expression"] == expression
    check = _checks(engine)[0]
    assert (check["disposition"], check["reason"]) == (
        ("debug_bypass", "guard_not_configured")
        if mode == "debug-when-unconfigured" and not configured
        else ("unavailable", "unsupported_input")
    )
    coordinator.assert_completed()


def _diagnostics(count):
    now = datetime(2026, 10, 18, tzinfo=UTC)
    tokens: list[dict[str, Any]] = []
    for index in range(count):
        token = f"token-{index}"
        tokens.append(
            {
                "token_id": token,
                "row_id": f"row-{index}",
                "row_index": index,
                "lineage": [],
                "join_group_id": None,
                "step_in_pipeline": 1,
                "created_at": now,
                "terminal_outcome": None,
                "states": [
                    {
                        "state_id": f"state-{index}",
                        "token_id": token,
                        "node_id": "neutral_review",
                        "step_index": 1,
                        "attempt": 0,
                        "status": "completed",
                        "duration_ms": 4,
                        "started_at": now,
                        "completed_at": now,
                        "error": None,
                        "success_reason": None,
                    }
                ],
            }
        )
    response = RunDiagnosticsResponse.model_validate(
        {
            "run_id": "synthetic-run",
            "landscape_run_id": "synthetic-landscape",
            "run_status": "completed",
            "summary": {
                "token_count": count,
                "preview_limit": 50,
                "preview_truncated": False,
                "discard_count": 0,
                "state_counts": {"completed": count},
                "operation_counts": {},
                "latest_activity_at": now,
            },
            "tokens": tokens,
            "operations": [],
            "artifacts": [],
            "discards": [],
        }
    )
    return llm_safe_diagnostics_snapshot(response)


@pytest.mark.asyncio
async def test_default_fifty_token_diagnostics_preserve_all_pretty_wire_evidence(azure):
    snapshot = _diagnostics(50)
    messages = build_run_diagnostics_messages(snapshot)
    original = json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False)
    assert messages[1]["content"] == original
    decision, request, evidence, sent = await _inspect(azure, messages, user="")
    assert decision.disposition == "allow"
    assert sent
    assert evidence.messages[1]["content"] == original
    for index in range(50):
        assert f"state-{index}" in "\n".join(request.documents)
    assert all(part.trust is None for part in evidence.content_parts[1])


@pytest.mark.asyncio
async def test_real_planner_discovery_keeps_registered_contract_on_sdk_wire(tmp_path, tool_context, azure):
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    client, sent = azure
    completion = _ScriptedCompletion(
        _response(("get_plugin_schema", {"plugin_type": "transform", "name": "llm"})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        model_overrides={"prompt_guard_mount": PromptGuardMount(client), "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 2
    tools = [message for message in completion.requests[1]["messages"] if message["role"] == "tool"]
    assert "json_schema" in tools[0]["content"]
    assert "json_schema" not in "\n".join(document for body in sent for document in body["documents"])
    assert all(guard_contracts.PROMPT_GUARD_CONTENT_PARTS_KEY not in message for message in completion.requests[1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [False, True], ids=["manual", "end-twelve-nodes"])
async def test_real_advisor_uses_lossless_producer_parts(invocation, monkeypatch, azure, checkpoint):
    from tests.unit.web.composer.test_pipeline_planner import _ScriptedCompletion, _text_response

    client, sent = azure
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    owner = invocation[2]
    advisor = service._advisor_checkpoint
    if checkpoint:
        state = _empty()
        for index in range(12):
            state = state.with_node(
                NodeSpec(
                    id=f"review_{index}",
                    node_type="transform",
                    plugin="llm",
                    input=f"rows_{index}",
                    on_success=f"rows_{index + 1}",
                    on_error="discard",
                    options={"prompt_template": "Inspect the neutral record fields. " * 18 + " {{ row.record }}"},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
        arguments = advisor._build_checkpoint_arguments(
            phase="end", state=state, content_scope=_current_scope(invocation, state), user_message=_USER
        )
    else:
        arguments = {
            "trigger": "proactive_security_safety",
            "problem_summary": "Inspect the routing and field contracts.",
            "recent_errors": ["Neutral validation observation. " * 40] * 3,
            "attempted_actions": ["Checked the authored mapping. " * 20] * 3,
            "schema_excerpt": "Whole field evidence. " * 300,
        }
        admitted = advisor._validate_advisor_arguments(arguments)
        arguments = admitted.to_internal_request()
    from elspeth.web.composer.advisor_context import build_advisor_user_message

    original = build_advisor_user_message(arguments)
    assert len(original) > 10_000
    completion = _ScriptedCompletion(_text_response("Inspect the neutral column declaration."))
    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER):
        await advisor._call_advisor_with_audit(arguments, recorder=BufferingRecorder(), provider_owner=owner)
    assert len(completion.requests) == 1 and sent
    assert completion.requests[0]["messages"][1]["content"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
@pytest.mark.parametrize("shape", ["queries", "required_fields", "required_fields_supported", "interpolated_fields"])
async def test_real_advisor_inspects_individual_evidence_values_without_changing_summary(invocation, monkeypatch, azure, phase, shape):
    from elspeth.plugins.transforms.llm.base import LLMConfig
    from elspeth.web.composer.advisor_context import (
        advisor_user_content_parts,
        build_advisor_user_message,
        summarize_pipeline_for_advisor,
    )
    from tests.unit.web.composer.test_pipeline_planner import _ScriptedCompletion, _text_response

    def template(label):
        prefix = f"{label}: inspect {{{{ row.record }}}}. "
        return prefix + "n" * (1000 - len(prefix))

    options = {
        "provider": "azure",
        "schema": {"mode": "observed"},
        "required_input_fields": ["record"],
        "prompt_template": template("Fallback"),
        "system_prompt": "Inspect neutral records. " + "s" * (1000 - len("Inspect neutral records. ")),
        "queries": {f"q{index}": {"input_fields": {"record": "record"}, "template": template(f"Question {index}")} for index in range(8)},
    }
    if shape in {"required_fields", "required_fields_supported"}:
        options = {
            "provider": "azure",
            "schema": {"mode": "observed"},
            "required_input_fields": [f"f{index:04}" for index in range(1500 if shape == "required_fields" else 500)],
            "prompt_template": "Inspect {{ row }}.",
            "system_prompt": "Inspect the declared neutral fields.",
        }
    elif shape == "interpolated_fields":
        interpolated_fields = [f"f{index:04}_" + "n" * 95 for index in range(100)]
        options = {
            "provider": "azure",
            "schema": {"mode": "observed"},
            "required_input_fields": ["record"],
            "prompt_template": "Inspect {{ row.record }}.",
            "system_prompt": "Inspect the declared neutral fields.",
            "queries": {
                f"q{index}": {
                    "input_fields": dict.fromkeys(interpolated_fields[index * 50 : (index + 1) * 50], "record"),
                    "template": "{{ (" + ",".join(f"row.{field}" for field in interpolated_fields[index * 50 : (index + 1) * 50]) + ") }}",
                }
                for index in range(2)
            },
        }
    config = LLMConfig(**options)
    if shape == "queries":
        assert len(config.resolved_queries()) == 8
    elif shape == "interpolated_fields":
        assert len(config.resolved_queries()) == 2
    else:
        assert config.required_input_fields == options["required_input_fields"]
    state = _empty().with_node(
        NodeSpec(
            id="query_review",
            node_type="transform",
            plugin="llm",
            input="records",
            on_success="answers",
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    client, sent = azure
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    advisor = service._advisor_checkpoint
    summary = summarize_pipeline_for_advisor(state)
    if shape != "required_fields_supported":
        assert len(summary) > 10_000
    if shape == "required_fields":
        from elspeth.web.credential_guard import CredentialMaterialRefused

        before = state.to_dict()
        with pytest.raises(CredentialMaterialRefused) as refused:
            advisor._build_checkpoint_arguments(
                phase=phase, state=state, content_scope=_current_scope(invocation, state), user_message=_USER
            )
        assert refused.value.finding.category == "traversal_limit"
        assert state.to_dict() == before and len(before["nodes"][0]["options"]["required_input_fields"]) == 1500
        assert sent == []
        return
    arguments = advisor._build_checkpoint_arguments(
        phase=phase, state=state, content_scope=_current_scope(invocation, state), user_message=_USER
    )
    displayed = json.loads(arguments["schema_excerpt"].split("\n", 1)[1])
    assert displayed["data"]["nodes"][0]["options"]["required_input_fields"] == options["required_input_fields"]
    if shape == "interpolated_fields":
        from elspeth.web.composer.advisor_context import AdvisorGraphCoverageUnavailable

        with pytest.raises(AdvisorGraphCoverageUnavailable):
            advisor._require_complete_graph_summary(arguments["_elspeth_advisor_summary_parts"])
        arguments["advisor_transcript"] = _selected_advisor_transcript(
            invocation,
            state,
            (("nodes", 0, "options", "queries", "q0", "template"), ("nodes", 0, "options", "queries", "q1", "template")),
        )
    else:
        assert displayed["content_manifest"]["values"] == []
        assert displayed["data"]["nodes"][0]["options"] == state.to_dict()["nodes"][0]["options"]
    original = build_advisor_user_message(arguments)
    parts = advisor_user_content_parts(arguments)
    assert "".join(part.text for part in parts) == original
    assert all(part.trust is None for part in parts)
    completion = _ScriptedCompletion(_text_response("Inspect the declared neutral record fields."))
    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER):
        await advisor._call_advisor_with_audit(
            arguments, recorder=BufferingRecorder(), structured_output=True, provider_owner=invocation[2]
        )
    assert len(completion.requests) == 1 and sent
    assert completion.requests[0]["messages"][1]["content"] == original
    documents = [document for body in sent for document in body["documents"]]
    assert all(len(document) <= guard_contracts.PROMPT_GUARD_MAX_TEXT_CHARACTERS for document in documents)
    inspected = "\n".join(documents)
    assert options["system_prompt"] in inspected
    assert options["prompt_template"] in inspected
    if "queries" in options:
        assert all(query["template"] in inspected for query in options["queries"].values())
        if shape == "interpolated_fields":
            assert all(field in inspected for field in interpolated_fields)
    else:
        assert all(field in inspected for field in options["required_input_fields"])


def _advisor_projection_state(options):
    return _empty().with_node(
        NodeSpec(
            id="decoded_review",
            node_type="transform",
            plugin="llm",
            input="records",
            on_success="answers",
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
@pytest.mark.parametrize("kind", ["unicode", "emoji-expansion"])
async def test_real_advisor_inspects_decoded_prompt_words_on_unchanged_wire(invocation, monkeypatch, azure, phase, kind):
    from elspeth.plugins.transforms.llm.base import LLMConfig
    from elspeth.web.composer.advisor_context import (
        ADVISOR_CHARS_PER_TOKEN,
        advisor_user_content_parts,
        build_advisor_user_message,
    )
    from tests.unit.web.composer.test_pipeline_planner import _ScriptedCompletion, _text_response

    # The literal escape must stay literal; the neighboring Unicode words
    # must reach inspection decoded, including quotes and a genuine newline.
    words = '境界 Пример 😀 "quoted" \\u754c\nsecond line'
    fallback = "Inspect {{ row.record }}. " + words
    system = "Inspect records. " + words
    query = "Question {{ row.record }}. " + words
    if kind == "emoji-expansion":
        prefix = "Question {{ row.record }}. "
        query = prefix + "😀" * (1000 - len(prefix))
        fallback = "Inspect {{ row.record }}."
        system = "Inspect neutral records."
        assert len(query) == 1000 and len(json.dumps(query)) > 10_000
    options = {
        "provider": "azure",
        "schema": {"mode": "observed"},
        "required_input_fields": ["record"],
        "prompt_template": fallback,
        "system_prompt": system,
        "queries": {"question": {"input_fields": {"record": "record"}, "template": query}},
    }
    assert len(LLMConfig(**options).resolved_queries()) == 1
    state = _advisor_projection_state(options)
    client, sent = azure
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    advisor = service._advisor_checkpoint
    arguments = advisor._build_checkpoint_arguments(
        phase=phase, state=state, content_scope=_current_scope(invocation, state), user_message=_USER
    )
    displayed = json.loads(arguments["schema_excerpt"].split("\n", 1)[1])
    assert displayed["data"]["nodes"][0]["options"] == state.to_dict()["nodes"][0]["options"]
    assert displayed["content_manifest"]["values"] == []
    original = build_advisor_user_message(arguments)
    parts = advisor_user_content_parts(arguments)
    assert "".join(part.text for part in parts) == original
    assert all(part.trust is None for part in parts)
    cap = service._settings.composer_advisor_max_prompt_tokens * ADVISOR_CHARS_PER_TOKEN
    assert len(arguments["schema_excerpt"]) < cap
    decoded_parts = "\n".join(part.inspection_text if part.inspection_text is not None else part.text for part in parts)
    for value in (fallback, system, query):
        assert value in decoded_parts
    if kind == "emoji-expansion":
        assert len(json.dumps(query)) > 10_000 and query in original
    completion = _ScriptedCompletion(_text_response("Inspect the declared neutral record fields."))
    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER):
        await advisor._call_advisor_with_audit(
            arguments, recorder=BufferingRecorder(), structured_output=True, provider_owner=invocation[2]
        )
    assert len(completion.requests) == 1 and sent
    assert completion.requests[0]["messages"][1]["content"] == original
    documents = [document for body in sent for document in body["documents"]]
    assert all(len(document) <= guard_contracts.PROMPT_GUARD_MAX_TEXT_CHARACTERS for document in documents)
    inspected = "\n".join(documents)
    assert all(value in inspected for value in (fallback, system, query))
    if kind == "unicode":
        assert "境界 Пример 😀" in inspected and r"\u754c" in inspected
        assert r"\u5883\u754c" not in inspected
    assert all(guard_contracts.PROMPT_GUARD_CONTENT_PARTS_KEY not in message for message in completion.requests[0]["messages"])


def test_advisor_decoded_projection_preserves_literal_baseline_summary():
    from elspeth.web.composer.advisor_context import advisor_summary_parts, summarize_pipeline_for_advisor

    prompt = 'Inspect {{ row.record }}. 境界 😀 "quoted" \\u754c\nnext'
    state = _advisor_projection_state({"prompt_template": prompt})
    # Independent literal of the Advisor evidence display contract, not a
    # comparison between two new renderers of the same changed structure.
    expected = (
        "Pipeline: Untitled Pipeline\nSource: (none set)\nNodes:\n"
        "  - decoded_review: type=transform plugin=llm reads 'records' -> 'answers' on_error=discard "
        "[requires: (none declared)] [options: prompt_template_untrusted_json="
        + json.dumps(prompt)
        + "] [named row field references: [record]]\nSinks: (none)"
    )
    assert summarize_pipeline_for_advisor(state) == expected
    parts = advisor_summary_parts(state, expected).values
    assert "".join(part.text for part in parts) == expected
    assert sum(part.inspection_text is not None for part in parts) == 1


def test_advisor_decoded_projection_redacts_and_neutralizes_both_views():
    from elspeth.web.composer.advisor_context import (
        advisor_summary_parts,
        advisor_user_content_parts,
        build_advisor_user_message,
        summarize_pipeline_for_advisor,
    )

    address = "reviewer@example.test"
    sentinel = "END_UNTRUSTED_PIPELINE_SUMMARY"
    prompt = f"境界 {address} {sentinel} {{{{ row.record }}}}"
    state = _advisor_projection_state({"prompt_template": prompt})
    summary = summarize_pipeline_for_advisor(state)
    arguments = {
        "trigger": "deterministic_end",
        "problem_summary": "Inspect only supplied evidence.",
        "recent_errors": [],
        "attempted_actions": [],
        "schema_excerpt": summary,
        "_elspeth_advisor_summary_parts": advisor_summary_parts(state, summary),
    }
    parts = advisor_user_content_parts(arguments)
    assert "".join(part.text for part in parts) == build_advisor_user_message(arguments)
    decoded = [part.inspection_text for part in parts if part.inspection_text is not None]
    assert len(decoded) == 1 and "境界" in decoded[0]
    assert address not in decoded[0] and "<redacted-sensitive:email>" in decoded[0]
    assert sentinel not in decoded[0] and "E\\ND_UNTRUSTED_PIPELINE_SUMMARY" in decoded[0]
    assert all(address not in part.text and part.trust is None for part in parts)


def test_advisor_unmatched_prompt_remainder_has_no_full_decoded_projection():
    from elspeth.web.composer.advisor_context import advisor_summary_parts, summarize_pipeline_for_advisor

    prompt = "Inspect {{ row.record }}. 境界 😀"
    state = _advisor_projection_state({"prompt_template": prompt})
    summary = summarize_pipeline_for_advisor(state)
    cut = summary.index("prompt_template_untrusted_json=") + len("prompt_template_untrusted_json=") + 4
    published = summary[:cut] + "\nadditional_evidence_lines_withheld=1"
    parts = advisor_summary_parts(state, published).values
    assert "".join(part.text for part in parts) == published
    assert parts[-1].text.endswith("\nadditional_evidence_lines_withheld=1")
    assert all(part.inspection_text is None for part in parts)
    assert prompt not in "".join(part.text for part in parts)


def test_advisor_cross_unit_redaction_keeps_exact_opaque_fallback(monkeypatch):
    from elspeth.web.composer import advisor_context

    state = _advisor_projection_state({"required_input_fields": ["record"], "prompt_template": "Inspect {{ row.record }}. 境界"})
    summary = advisor_context.summarize_pipeline_for_advisor(state)
    arguments = {
        "trigger": "deterministic_end",
        "problem_summary": "Inspect only supplied evidence.",
        "recent_errors": [],
        "attempted_actions": [],
        "schema_excerpt": summary,
        "_elspeth_advisor_summary_parts": advisor_context.advisor_summary_parts(state, summary),
    }
    # A controlled spanning transform must choose the existing exact whole
    # section fallback; the separate real redaction test checks egress policy.
    monkeypatch.setattr(
        advisor_context, "_redact_sensitive_content", lambda text: text.replace("[requires: record]", "[contract redacted]")
    )
    parts = advisor_context.advisor_user_content_parts(arguments)
    assert "".join(part.text for part in parts) == advisor_context.build_advisor_user_message(arguments)
    assert "[contract redacted]" in "".join(part.text for part in parts)
    assert all(part.inspection_text is None and part.trust is None for part in parts)


def test_advisor_summary_carrier_refuses_trusted_units():
    from elspeth.web.composer.advisor_context import AdvisorSummaryParts
    from elspeth.web.composer.invariants import InvariantError

    with pytest.raises(InvariantError, match="owned untrusted"):
        AdvisorSummaryParts((guard_contracts.PromptGuardContentPart("authored", guard_contracts.PromptGuardTrust.REFERENCE),))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
async def test_real_advisor_keeps_oversized_indivisible_summary_scalar_refused(invocation, monkeypatch, azure, phase):
    from elspeth.web.composer.advisor_context import build_advisor_user_message
    from tests.unit.web.composer.test_pipeline_planner import _ScriptedCompletion, _text_response

    state = _empty().with_metadata({"description": "x" * 10_001})
    client, sent = azure
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    advisor = service._advisor_checkpoint
    arguments = advisor._build_checkpoint_arguments(
        phase=phase, state=state, content_scope=_current_scope(invocation, state), user_message=_USER
    )
    displayed = json.loads(arguments["schema_excerpt"].split("\n", 1)[1])
    assert [reference["path"] for reference in displayed["content_manifest"]["values"]] == [["metadata", "description"]]
    assert "x" * 10_001 not in arguments["schema_excerpt"]
    owner = invocation[2]
    context = owner.required_work.coordinator.authority.context
    verdict = await advisor._run_advisor_checkpoint(
        phase=phase,
        state=state,
        session_id=context.fence.session_id,
        session_operation_context=context,
        provider_owner=owner,
        recorder=BufferingRecorder(),
    )
    assert not verdict.ok and verdict.failure_class == "unavailable"
    selected = _selected_complete_value_message(invocation, state, ("metadata", "description"))
    complete_value = json.loads(selected["content"])["data"]["value"]
    assert complete_value == state.metadata.description
    # Selection does not approve graph coverage. Passing the actual complete
    # scalar as ordinary untrusted context still requires final inspection.
    arguments["problem_summary"] += "\nSelected complete value:\n" + complete_value
    assert "x" * 10_001 in build_advisor_user_message(arguments)
    completion = _ScriptedCompletion(_text_response("This response must never be requested."))
    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER), pytest.raises(prompt_guard.PromptGuardRefused) as refused:
        await advisor._call_advisor_with_audit(
            arguments, recorder=BufferingRecorder(), structured_output=True, provider_owner=invocation[2]
        )
    assert refused.value.http_status == 422 and refused.value.disposition == "unavailable"
    assert sent == [] and completion.requests == []


@pytest.mark.asyncio
async def test_real_freeform_batch_and_cache_retain_complete_nominal_schema(invocation, monkeypatch, catalog, azure):
    from sqlalchemy import select

    from elspeth.web.composer import provider_gateway
    from elspeth.web.sessions.models import chat_messages_table
    from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _make_settings
    from tests.unit.web.composer.test_pipeline_planner import _response, _ScriptedCompletion, _text_response

    policy, snapshot = catalog
    client, sent = azure
    service = _guarded_service(
        invocation,
        monkeypatch,
        composer_client=client,
        catalog=create_catalog_service(),
        settings=_make_settings(composer_model="anthropic/claude-sonnet-4.5", composer_prompt_guard_mode="required"),
    )
    owner = invocation[2]
    context = owner.required_work.coordinator.authority.context
    completion = _ScriptedCompletion(
        _response(
            ("get_plugin_schema", {"plugin_type": "transform", "name": "llm"}),
            ("get_plugin_schema", {"plugin_type": "transform", "name": "llm"}),
        ),
        _text_response("The registered schema is available for review."),
    )
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(service._advisor_checkpoint, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    with (
        prompt_guard_user_scope(_USER),
        patch.object(provider_gateway, "enforce_prompt_guard", autospec=True, side_effect=provider_gateway.enforce_prompt_guard) as checks,
    ):
        result = await service._compose_loop(
            _USER,
            [],
            _empty(),
            session_id=context.fence.session_id,
            user_id="alice",
            deadline=asyncio.get_running_loop().time() + 30.0,
            plugin_snapshot=snapshot,
            policy_catalog=policy,
            session_operation_context=context,
            required_work=owner.required_work,
            provider_owner=owner,
        )
    assert len(completion.requests) == 2
    assert [invocation.cache_hit for invocation in result.tool_invocations] == [False, True]
    tools = [message for message in completion.requests[1]["messages"] if message["role"] == "tool"]
    assert len(tools) == 2
    assert all("json_schema" in message["content"] for message in tools)
    assert all(guard_contracts.PROMPT_GUARD_CONTENT_PARTS_KEY not in message for message in tools)
    assert "json_schema" not in "\n".join(document for body in sent for document in body["documents"])
    assert len(checks.call_args_list) == 2
    forwarded = checks.call_args_list[1].kwargs
    assert any(parts is not None for parts in forwarded["content_parts"])
    assert any(parts is not None for parts in forwarded["tool_argument_parts"])
    wire = completion.requests[1]
    assert wire["messages"] == forwarded["messages"]
    assert wire["messages"][0]["cache_control"] == {"type": "ephemeral"}
    assert wire["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all(guard_contracts.PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY not in message for message in wire["messages"])
    hashed = {
        "role": "composer",
        "policy_fingerprint": client.policy_fingerprint,
        "enforce_azure_character_limits": True,
        "user_prompt": _USER,
        "messages": wire["messages"],
        "tools": wire["tools"],
        "trusted_message_indexes": sorted(forwarded["trusted_message_indexes"]),
        "content_parts": [
            None
            if parts is None
            else [
                {"text": part.text, "trust": part.trust.value if part.trust is not None else None, "inspection_text": part.inspection_text}
                for part in parts
            ]
            for parts in forwarded["content_parts"]
        ],
        "tool_argument_parts": [
            None
            if entries is None
            else [
                {
                    "tool_call_index": entry.tool_call_index,
                    "parts": [{"text": part.text, "inspection_text": part.inspection_text} for part in entry.parts],
                }
                for entry in entries
            ]
            for entries in forwarded["tool_argument_parts"]
        ],
    }
    expected = sha256(json.dumps(hashed, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    with invocation[0].connect() as connection:
        rows = connection.execute(select(chat_messages_table.c.tool_calls)).scalars().all()
    audits = [item["check"] for row in rows if row is not None for item in row if item.get("_kind") == "prompt_guard_audit"]
    assert len(audits) == 2
    assert audits[1]["request_hash"] == expected


@pytest.mark.asyncio
async def test_real_planner_repair_keeps_contract_and_authored_detail_separate(tmp_path, tool_context, azure):
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    client, sent = azure
    invalid = _pipeline(tmp_path)
    invalid["nodes"] = [
        {
            "id": "review",
            "node_type": "transform",
            "plugin": "llm",
            "input": "rows",
            "on_success": "output",
            "on_error": "discard",
            "options": {"unexpected_option": "authored_detail_canary"},
        }
    ]
    completion = _ScriptedCompletion(
        _response(("emit_pipeline_proposal", {"pipeline": invalid})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        model_overrides={"prompt_guard_mount": PromptGuardMount(client), "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 2
    wire = completion.requests[1]["messages"]
    assert "plugin_contracts" in "\n".join(message["content"] for message in wire if message["role"] == "tool")
    inspected = "\n".join(document for body in sent for document in body["documents"])
    assert "authored_detail_canary" in inspected
    assert "json_schema" not in inspected


@pytest.mark.asyncio
async def test_real_end_publication_review_survives_actual_format_retry(invocation, monkeypatch, azure):
    from elspeth.web.composer.turn_effects import TurnEvidence
    from tests.unit.web.composer.test_pipeline_planner import _ScriptedCompletion, _text_response

    client, sent = azure
    service = _guarded_service(invocation, monkeypatch, advisor_client=client)
    owner = invocation[2]
    context = owner.required_work.coordinator.authority.context
    state = _empty()
    for index in range(12):
        state = state.with_node(
            NodeSpec(
                id=f"review_{index}",
                node_type="transform",
                plugin="llm",
                input=f"rows_{index}",
                on_success=f"rows_{index + 1}",
                on_error="discard",
                options={"prompt_template": "Inspect the neutral record fields. " * 18 + " {{ row.record }}"},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
    evidence = TurnEvidence(
        session_id=context.fence.session_id,
        session_operation_context=context,
        request_id="neutral-request",
        request=_USER,
        initial_state_id=None,
        initial_state=state,
    )
    review = evidence.review_candidate(candidate="The neutral graph is retained for review. " * 30, request=_USER, current_state=state)
    completion = _ScriptedCompletion(
        _text_response("Not a JSON verdict."),
        _text_response(
            json.dumps({"verdict": "CLEAN", "findings": "", "category": "other", "steps": [], "note": None, "publication_verdict": "ADMIT"})
        ),
    )
    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(_USER):
        verdict = await service._advisor_checkpoint._run_advisor_checkpoint(
            phase="end",
            state=state,
            session_id=context.fence.session_id,
            recorder=BufferingRecorder(),
            user_message=_USER,
            session_operation_context=context,
            provider_owner=owner,
            publication_review=review,
        )
    assert verdict.ok and verdict.publication_binding == review.binding
    assert len(completion.requests) == 2 and sent
    for request in completion.requests:
        text = request["messages"][1]["content"]
        assert "Candidate/request and server-owned" in text
        assert "The neutral graph is retained for review." in text
        assert "BEGIN_UNTRUSTED_PIPELINE_SUMMARY" in text
        assert guard_contracts.PROMPT_GUARD_CONTENT_PARTS_KEY not in request["messages"][1]


@pytest.mark.asyncio
async def test_actual_planner_assistant_argument_producer_keeps_complete_large_graph(tmp_path, azure):
    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.web.composer.pipeline_planner import _assistant_tool_calls_message, _parse_response_tool_calls
    from elspeth.web.composer.tools._dispatch import require_schema_valid_arguments
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _response

    pipeline = _pipeline(tmp_path)
    pipeline["nodes"] = [
        {
            "id": f"clean_{i}",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows" if i == 0 else f"stage_{i}",
            "on_success": "output" if i == 11 else f"stage_{i + 1}",
            "on_error": "discard",
            "options": {"schema": {"mode": "flexible"}},
            "description": "Retain the neutral record title, category and score. " * 12,
        }
        for i in range(12)
    ]
    require_schema_valid_arguments("set_pipeline", pipeline)
    response = _response(("emit_pipeline_proposal", {"pipeline": pipeline}))
    message, calls = _parse_response_tool_calls(
        response, max_tool_calls=1, dialect=ToolContractDialect.NONE, sent_tool_names=frozenset({"emit_pipeline_proposal"})
    )
    produced = _assistant_tool_calls_message(message, calls)
    original = calls[0].raw_arguments
    assert len(original) > 10000
    decision, request, evidence, sent = await _inspect(azure, [produced])
    assert decision.disposition == "allow" and sent
    assert evidence.messages[0]["tool_calls"][0]["function"]["arguments"] == original
    entries = evidence.tool_argument_parts[0]
    assert len(entries) == 1
    assert "".join(part.text for part in entries[0].parts) == original
    assert all(part.trust is None for part in entries[0].parts)
    assert "clean_11" in "\n".join(request.documents)


@pytest.mark.asyncio
async def test_real_planner_initial_large_state_retains_all_authored_nodes(tmp_path, tool_context, azure):
    from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion

    state = _empty()
    for index in range(12):
        state = state.with_node(
            NodeSpec(
                id=f"clean_{index}",
                node_type="transform",
                plugin="passthrough",
                input=f"stage_{index}",
                on_success=f"stage_{index + 1}",
                on_error="discard",
                options={"schema": {"mode": "flexible"}},
                description="Retain the neutral record title, category and score. " * 12,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
    client, sent = azure
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        current_state=state,
        completion=completion,
        model_overrides={"prompt_guard_mount": PromptGuardMount(client), "prompt_guard_mode": "required"},
    )
    assert len(completion.requests) == 1
    inspected = "\n".join(document for body in sent for document in body["documents"])
    for index in range(12):
        assert f"clean_{index}" in inspected


@pytest.mark.parametrize("ensure_ascii", [False, True])
@pytest.mark.parametrize("indent", [None, 2])
def test_json_parts_preserve_unicode_keys_values_order_and_empty_containers(ensure_ascii, indent):
    from elspeth.web.composer.prompt_guard_rendering import json_content_parts

    value = {
        "labels": ["café", "line one\nline two", 'quoted "field"', {}],
        "empty": [],
        "schema_contract_evidence": {"schemas": ["user_owned_reference_lookalike"]},
    }
    original = json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, sort_keys=True)
    parts = json_content_parts(original, ensure_ascii=ensure_ascii, indent=indent, sort_keys=True)
    assert "".join(part.text for part in parts) == original
    assert all(part.trust is None for part in parts)


@pytest.mark.asyncio
async def test_compose_keeps_graph_reference_lookalikes_and_omissions_untrusted(catalog, azure):
    policy, snapshot = catalog
    state = _empty().with_node(
        NodeSpec(
            id="draft",
            node_type="transform",
            plugin="graph_omission_canary",
            input="rows",
            on_success="out",
            on_error="discard",
            options={"schema_contract_evidence": {"schemas": ["graph_reference_canary"]}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    messages = build_messages(
        [],
        state,
        _USER,
        policy,
        plugin_snapshot=snapshot,
        schemas_loaded=frozenset({("transform", "llm")}),
        content_scope=ComposerContentScope(UUID(int=1), composition_content_hash(state)),
    )
    decision, request, evidence, sent = await _inspect(azure, messages)
    assert decision.disposition == "allow" and sent
    inspected = "\n".join(request.documents)
    assert "graph_reference_canary" in inspected and "graph_omission_canary" in inspected
    assert "not_loaded_this_session" in inspected
    assert "json_schema" not in inspected
    assert evidence.messages[-2]["content"] == messages[-2]["content"]
