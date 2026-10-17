"""Real producer payloads fit Azure without changing their provider wire text."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from unittest.mock import patch

import httpx
import litellm
import pytest
from pydantic import SecretStr

from elspeth.contracts import prompt_guard as guard_contracts
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import prompt_guard
from elspeth.web.composer.audit import BufferingRecorder
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
    original = build_context_string(state, policy, plugin_snapshot=snapshot, schemas_loaded=loaded)
    messages = build_messages([], state, _USER, policy, plugin_snapshot=snapshot, schemas_loaded=loaded)
    assert messages[-2]["content"] == original
    decision, request, evidence, sent = await _inspect(azure, messages)
    assert decision.disposition == "allow"
    assert sent
    assert evidence.messages[-2]["content"] == original
    assert '"json_schema"' not in "\n".join(request.documents)
    assert "current_state" in "\n".join(request.documents)


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
        arguments = advisor._build_checkpoint_arguments(phase="end", state=state, user_message=_USER)
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
    messages = build_messages([], state, _USER, policy, plugin_snapshot=snapshot, schemas_loaded=frozenset({("transform", "llm")}))
    decision, request, evidence, sent = await _inspect(azure, messages)
    assert decision.disposition == "allow" and sent
    inspected = "\n".join(request.documents)
    assert "graph_reference_canary" in inspected and "graph_omission_canary" in inspected
    assert "not_loaded_this_session" in inspected
    assert "json_schema" not in inspected
    assert evidence.messages[-2]["content"] == messages[-2]["content"]
