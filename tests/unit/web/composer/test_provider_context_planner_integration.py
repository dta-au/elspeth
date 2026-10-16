"""Public planner snapshots provider context before effects."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from elspeth.core.canonical import canonical_json
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.planning_application import _PlannerPreviewPreflightCallbacks
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX, build_context_string, build_messages
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext
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
    messages = build_messages([], state, "Inspect the current wording.", policy, plugin_snapshot=snapshot)
    content = next(row["content"] for row in messages if row["content"].startswith(STATE_CONTEXT_PREFIX))
    options = json.loads(content.split("\n", 1)[1])["current_state"]["nodes"][0]["options"]
    assert options["prompt_template_parts"][0]["text"] == prefix + "Classify "
    assert options["prompt_template"].startswith(prefix + "Classify support categories")
    captured = capture_provider_context(messages=messages, tools=None)
    if large:
        assert captured.disposition == "unavailable" and captured.reasons == ("bounds",)
        assert captured.canonical is None
    else:
        assert captured.disposition == "retained"
        assert json.loads(captured.canonical)["messages"] == messages
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
        patch.object(
            service._planning_application,
            "_plan_and_stage_empty_pipeline",
            autospec=True,
            side_effect=AssertionError("nonempty graph selected empty planner"),
        ) as empty_planner,
    ):
        result = await service.compose(
            "Describe the current approved wording without editing it.",
            [],
            state,
            session_id=session_id,
            user_id="test-user",
            session_operation_context=operation,
        )
    empty_planner.assert_not_called()
    assert sdk.await_count == len(completion.requests) == 1
    state_message = next(
        row["content"]
        for row in completion.requests[0]["messages"]
        if isinstance(row["content"], str) and row["content"].startswith(STATE_CONTEXT_PREFIX)
    )
    visible = json.loads(state_message.split("\n", 1)[1])["current_state"]
    options = visible["nodes"][0]["options"]
    assert options["prompt_template_parts"] == before["nodes"][0]["options"]["prompt_template_parts"]
    assert options["prompt_template"] == before["nodes"][0]["options"]["prompt_template"]
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    assert category["historical_draft"] == "old rejected broad categories" and "draft" not in category
    assert "accepted_value" not in category and "event_id" not in category
    assert result.state is state and state.to_dict() == before


@pytest.mark.asyncio
async def test_application_projection_boundary_preserves_parts_and_complete_byte_admission() -> None:
    """Exercise shared caller wiring; production selects this owner for empty graphs."""
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    settings = _make_settings(composer_planner_max_request_bytes=1_000_000)
    service, session_id = _composer_service_with_session(catalog, settings)
    application = service._planning_application
    state = _amended_approved_state()
    before = state.to_dict()
    before_hash = composition_content_hash(state)
    completion = _ScriptedCompletion(_response(("handoff_to_user", {"action": "park", "message": "Keep the approved wording."})))

    async def request(recorder):
        return await application._plan_and_stage_empty_pipeline(
            message="Inspect this approved prompt before editing its route.",
            messages=[],
            state=state,
            session_id=session_id,
            current_state_id=None,
            user_id="test-user",
            session_operation_context=operation,
            progress=None,
            user_message_id=str(uuid4()),
            recorder=recorder,
            plugin_snapshot=snapshot,
            policy_catalog=policy,
        )

    with (
        fenced_operation_context(service._session_engine, session_id) as operation,
        patch.object(application, "_planner_preview_preflight", autospec=True, return_value=_PlannerPreviewPreflightCallbacks()),
        patch("litellm.acompletion", autospec=True, side_effect=completion.__call__) as sdk,
    ):
        recorder = BufferingRecorder()
        result = await request(recorder)
        assert result.state is state and result.graph_recovery_intent.action == "park"
        assert sdk.await_count == len(completion.requests) == 1
        wire = completion.requests[0]
        payload = json.loads(wire["messages"][1]["content"])
        options = payload["current_state"]["nodes"][0]["options"]
        assert options["prompt_template_parts"] == before["nodes"][0]["options"]["prompt_template_parts"]
        assert options["prompt_template"] == before["nodes"][0]["options"]["prompt_template"]
        category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
        assert category["historical_draft"] == "old rejected broad categories" and "draft" not in category
        assert category["display_title"] == "Category definition"
        assert all(
            not ({"accepted_value", "event_id", "accepted_artifact_hash", "resolved_prompt_template_hash"} & set(row))
            for row in options["interpretation_requirements"]
        )
        assert "approved_prompt_artifact_hash" not in options
        assert all("source_authoring" not in row["options"] for row in payload["current_state"]["sources"].values())
        assert len(recorder.llm_calls) == 1
        actual_size = len(canonical_json({"messages": wire["messages"], "tools": wire["tools"]}).encode("utf-8"))
        without_parts = deepcopy(wire["messages"])
        smaller_payload = json.loads(without_parts[1]["content"])
        del smaller_payload["current_state"]["nodes"][0]["options"]["prompt_template_parts"]
        without_parts[1]["content"] = canonical_json(smaller_payload)
        smaller_size = len(canonical_json({"messages": without_parts, "tools": wire["tools"]}).encode("utf-8"))
        assert smaller_size < actual_size - 1
        # This cap would admit the old lossy view, but the complete real view
        # must fail before another physical SDK call. No clipping or cap lift.
        bounded = settings.model_copy(update={"composer_planner_max_request_bytes": actual_size - 1})
        rejected = BufferingRecorder()
        with patch.object(application, "_settings", bounded), pytest.raises(PipelinePlannerError) as caught:
            await request(rejected)
        assert caught.value.code == "REQUEST_BYTES_EXHAUSTED"
        assert sdk.await_count == 1 and rejected.llm_calls == ()
    assert state.to_dict() == before and composition_content_hash(state) == before_hash


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
    context = build_context_string(state, policy, plugin_snapshot=snapshot)
    current = json.loads(context.split("\n", 1)[1])["current_state"]
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
async def test_public_plan_freezes_original_nested_context_before_first_provider_call(tmp_path: Path, tool_context: ToolContext) -> None:
    state = _current_state()
    context = state.to_dict()
    context["version"] = 37
    expected = deepcopy(context)
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
    initial = json.loads(completion.requests[0]["messages"][1]["content"])
    assert initial["current_state"] == expected
    tool_payload = next(json.loads(message["content"]) for message in completion.requests[1]["messages"] if message["role"] == "tool")
    assert "PRIVATE-MUTATED" not in json.dumps(tool_payload)
    assert tool_payload["version"] == state.version == 4
    assert context["version"] == 999
    assert source["plugin"] == "PRIVATE-MUTATED-PLUGIN"
    assert "PRIVATE-MUTATED-KEY" in options
