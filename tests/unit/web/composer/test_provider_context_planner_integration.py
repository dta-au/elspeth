"""Public planner snapshots provider context before effects."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
from elspeth.core.canonical import canonical_json
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX, build_context_string, build_messages
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer._helpers import _composer_service_with_session, _make_settings
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _Response, _response, _ScriptedCompletion
from tests.unit.web.composer.test_prompt_patch_reviews import _amended_approved_state

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean


@pytest.mark.parametrize("large", [False, True])
def test_rendered_prompt_parts_are_exact_and_capture_bounds_are_honest(large: bool) -> None:
    prefix = "x" * (JSON_MAX_TOTAL_UTF8_BYTES if large else 17)
    state = _amended_approved_state(literal_prefix=prefix)
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    if large:
        before = state.to_dict()
        with pytest.raises(CredentialMaterialRefused) as refused:
            build_messages(
                [],
                state,
                "Inspect the current wording.",
                policy,
                plugin_snapshot=snapshot,
                content_scope=ComposerContentScope(UUID(int=1), composition_content_hash(state)),
            )
        assert refused.value.surface == "composer_bounded_content_projection"
        assert state.to_dict() == before
        # Independent diagnostic capture must also report unavailable for these
        # complete bytes, rather than retaining a clipped graph or fake view.
        captured = capture_provider_context(messages=[{"role": "user", "content": canonical_json(before)}], tools=None)
        assert captured.disposition == "unavailable" and captured.reasons == ("bounds",)
        assert captured.canonical is None
        return
    messages = build_messages(
        [],
        state,
        "Inspect the current wording.",
        policy,
        plugin_snapshot=snapshot,
        content_scope=ComposerContentScope(UUID(int=1), composition_content_hash(state)),
    )
    content = next(row["content"] for row in messages if row["content"].startswith(STATE_CONTEXT_PREFIX))
    options = json.loads(content.split("\n", 1)[1])["current_state"]["data"]["nodes"][0]["options"]
    assert options["prompt_template_parts"][0]["text"] == prefix + "Classify "
    assert options["prompt_template"].startswith(prefix + "Classify support categories")
    wire_messages = detach_prompt_guard_evidence(messages).messages
    assert wire_messages == [{"role": row["role"], "content": row["content"]} for row in messages]
    captured = capture_provider_context(messages=wire_messages, tools=None)
    assert captured.disposition == "retained"
    assert json.loads(captured.canonical)["messages"] == wire_messages
    # Diagnostic capture bounds do not assert refusal at a provider entrance.


@pytest.mark.asyncio
async def test_nonempty_compose_sends_current_amended_parts_through_real_provider_boundary() -> None:
    state = _amended_approved_state()
    service, session_id = _composer_service_with_session(create_catalog_service(), _make_settings())
    response = _response()
    response.choices[0].message.content = "The approved wording is preserved."
    response.choices[0].message.tool_calls = None
    completion = _ScriptedCompletion(response)
    before = state.to_dict()
    with (
        fenced_operation_context(service._session_engine, session_id) as operation,
        patch("litellm.acompletion", autospec=True, side_effect=completion.__call__) as sdk,
    ):
        result = await service.compose(
            "Describe the current approved wording without editing it.",
            [],
            state,
            session_id=session_id,
            user_id="test-user",
            session_operation_context=operation,
        )
    assert sdk.await_count == len(completion.requests) == 1
    state_message = next(
        row["content"]
        for row in completion.requests[0]["messages"]
        if isinstance(row["content"], str) and row["content"].startswith(STATE_CONTEXT_PREFIX)
    )
    visible = json.loads(state_message.split("\n", 1)[1])["current_state"]["data"]
    options = visible["nodes"][0]["options"]
    assert options["prompt_template_parts"] == before["nodes"][0]["options"]["prompt_template_parts"]
    assert options["prompt_template"] == before["nodes"][0]["options"]["prompt_template"]
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    assert category["historical_draft"] == "old rejected broad categories" and "draft" not in category
    assert "accepted_value" not in category and "event_id" not in category
    assert result.state is state and state.to_dict() == before


def test_normal_context_retains_prompt_parts_and_existing_blob_path_redaction() -> None:
    state = _amended_approved_state()
    source = state.sources["source"]
    state = replace(
        state,
        sources={
            "source": replace(
                source,
                options={
                    **source.options,
                    "blob_ref": "9f2b3c1d-4e5a-4b6c-8d7e-0f1a2b3c4d5e",
                    "path": "/internal/blobs/PRIVATE-STORAGE-CANARY.csv",
                },
            )
        },
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    before = state.to_dict()
    context = build_context_string(
        state, policy, plugin_snapshot=snapshot, content_scope=ComposerContentScope(UUID(int=1), composition_content_hash(state))
    )
    current = json.loads(context.split("\n", 1)[1])["current_state"]["data"]
    assert "PRIVATE-STORAGE-CANARY" not in context
    assert current["sources"]["source"]["options"]["blob_ref"] == "9f2b3c1d-4e5a-4b6c-8d7e-0f1a2b3c4d5e"
    assert current["nodes"][0]["options"]["prompt_template_parts"] == before["nodes"][0]["options"]["prompt_template_parts"]
    assert state.to_dict() == before


def _current_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={"path": "private.csv"}, on_validation_failure="discard"),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=4,
    )


@pytest.mark.asyncio
async def test_public_plan_keeps_authoritative_graph_despite_nested_provider_context_mutation(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    state = _current_state()
    context = state.to_dict()
    context["version"] = 37
    expected = deepcopy(state.to_dict())
    source_rows = context["sources"]
    assert isinstance(source_rows, dict)
    source = next(iter(source_rows.values()))
    assert isinstance(source, dict)
    options = source["options"]
    assert isinstance(options, dict)

    class MutatingCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> _Response:
            if not self.requests:
                source["plugin"] = "PRIVATE-MUTATED-PLUGIN"
                options["PRIVATE-MUTATED-KEY"] = "private"
                context["version"] = 999
            return await super().__call__(**kwargs)

    completion = MutatingCompletion(
        _response(("get_pipeline_state", {})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        current_state=state,
        provider_current_state=context,
    )
    assert len(completion.requests) == 2
    assert [row["role"] for row in completion.requests[0]["messages"]] == ["system", "user", "user"]
    initial = json.loads(completion.requests[0]["messages"][2]["content"])
    assert initial["current_state"]["data"] == expected
    assert initial["current_state"]["content_manifest"]["composition_content_hash"] == composition_content_hash(state)
    tool_payload = next(json.loads(message["content"]) for message in completion.requests[1]["messages"] if message["role"] == "tool")
    assert "PRIVATE-MUTATED" not in json.dumps(tool_payload)
    assert tool_payload["version"] == state.version == 4
    assert context["version"] == 999
    assert source["plugin"] == "PRIVATE-MUTATED-PLUGIN"
    assert "PRIVATE-MUTATED-KEY" in options
