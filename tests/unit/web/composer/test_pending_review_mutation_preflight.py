"""Pending prompt reviews must not hide graph failures in tool feedback."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import service as service_module
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.service import ComposerServiceImpl, _admit_composer_llm_completion
from elspeth.web.composer.tools import ToolResult
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.execution.test_validation_complaint_triage import complaint_settings, complaint_triage_state

from .conftest import _fake_llm_response
from .test_preview_policy_snapshot import _service


@pytest.mark.anyio
@pytest.mark.parametrize("narrow_consumer", [True, False])
@pytest.mark.parametrize("tool_name", ["set_metadata", "request_interpretation_review", "finalize"])
async def test_mutation_feedback_checks_real_graph_while_reviews_pending(
    tmp_path: Path,
    narrow_consumer: bool,
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tool_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    complaint_settings(tmp_path)
    service = _service(tmp_path, snapshot)
    service._sessions_service = composer_service_with_real_sessions._sessions_service
    state = complaint_triage_state(pending=True, narrow_consumer=narrow_consumer)
    session_id = result_session_id
    source_dir = tmp_path / "blobs" / session_id
    source_dir.mkdir(parents=True)
    (source_dir / "input.csv").write_text("id,complaint\nC-1,My bill is wrong\n")
    classifier = state.nodes[0]
    state = replace(
        state,
        sources={
            "source": replace(state.sources["source"], options={**state.sources["source"].options, "path": str(source_dir / "input.csv")})
        },
        nodes=(
            replace(
                classifier,
                options={
                    **classifier.options,
                    "system_prompt": "Categorize complaints. Reply with the single category word.",
                    "interpretation_requirements": [
                        {
                            **requirement,
                            "event_id": None,
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        }
                        for requirement in classifier.options["interpretation_requirements"]
                    ],
                    "prompt_template": "Classify {{ row.complaint }} using pending interpretation",
                    "prompt_template_parts": [
                        {"kind": "text", "text": "Classify {{ row.complaint }} using "},
                        {"kind": "interpretation_ref", "requirement_id": "category:classify_category"},
                    ],
                },
            ),
            *state.nodes[1:],
        ),
    )
    review_calls = []

    async def review_handler(**kwargs: Any) -> ToolResult:
        review_calls.append(kwargs)
        return ToolResult(success=True, updated_state=state, validation=state.validate(), affected_nodes=())

    monkeypatch.setitem(service_module._SESSION_AWARE_TOOL_HANDLERS, "request_interpretation_review", review_handler)
    if tool_name == "finalize":

        async def surface(*_args: Any, **kwargs: Any) -> None:
            review_calls.append(kwargs)

        async def missing(*_args: Any, **_kwargs: Any) -> tuple[()]:
            return ()

        monkeypatch.setattr(service, "surface_pending_interpretation_reviews", surface)
        monkeypatch.setattr(service, "_missing_pending_interpretation_review_sites", missing)
        result = await service._surface_and_finalize_no_tools(
            assistant_message=_admit_composer_llm_completion(_fake_llm_response(content="The pipeline is ready.")).message,
            state=state,
            session_id=session_id,
            current_state_id=str(uuid4()),
            progress=None,
            recorder=BufferingRecorder(),
            initial_version=0,
            user_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache={},
            session_scope="pending-finalize",
            message="Finish the pipeline",
            mutation_success_seen=True,
            repair_turns_used=2,
            plugin_snapshot=snapshot,
            session_operation_context=SessionOperationContext(
                SessionOperationFence(session_id, str(uuid4()), str(uuid4()), 1), SessionOperationKind.COMPOSE
            ),
        )
        assert bool(review_calls) is not narrow_consumer
        if narrow_consumer:
            assert result.runtime_preflight is not None
            assert result.runtime_preflight.readiness.completion_ready is False
            assert any(error.component_id == "tidy_columns" for error in result.runtime_preflight.errors)
        return
    arguments = (
        {"patch": {"name": "Complaint triage"}}
        if tool_name == "set_metadata"
        else {
            "affected_node_id": "classify_category",
            "kind": "vague_term",
            "user_term": "category",
            "llm_draft": "billing, outage, or other",
        }
    )
    completion = _admit_composer_llm_completion(
        _fake_llm_response(tool_calls=({"id": "request", "name": tool_name, "arguments": arguments},))
    )
    messages = []
    dispatch, _ = await service._dispatch_tool_batch(
        call_model=_CallModelOutcome(completion=completion),
        state=state,
        last_validation=None,
        last_runtime_preflight=None,
        llm_messages=messages,
        recorder=BufferingRecorder(),
        anti_anchor=AntiAnchorTracker(),
        discovery_cache={},
        runtime_preflight_cache={},
        session_id=session_id,
        user_id=None,
        user_message_id=None,
        user_message_content=None,
        current_state_id=str(uuid4()),
        actor="test",
        initial_version=state.version,
        deadline=asyncio.get_running_loop().time() + 60,
        progress=None,
        session_scope="pending-mutation",
        advisor_calls_used=0,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        cancellation_requested=asyncio.Event(),
        plugin_snapshot=snapshot,
        policy_catalog=PolicyCatalogView(catalog, snapshot, service._operator_profile_registry),
        session_operation_context=SessionOperationContext(
            SessionOperationFence(session_id, str(uuid4()), str(uuid4()), 1), SessionOperationKind.COMPOSE
        ),
    )
    assert dispatch.plugin_crash is None
    if tool_name == "set_metadata":
        assert dispatch.state.version > state.version, messages
    feedback = json.loads(next(message["content"] for message in messages if message["role"] == "tool"))
    if narrow_consumer:
        preflight = feedback["runtime_preflight"]
        assert preflight["is_valid"] is False
        assert any(
            error["component_id"] == "tidy_columns" and "producer emits 'Any'" in error["message"] for error in preflight["errors"]
        ), preflight["errors"]
        if tool_name == "set_metadata":
            assert dispatch.last_runtime_preflight is not None
        else:
            assert feedback["success"] is False
            assert not review_calls
    else:
        assert "runtime_preflight" not in feedback
        if tool_name == "request_interpretation_review":
            assert len(review_calls) == 1
