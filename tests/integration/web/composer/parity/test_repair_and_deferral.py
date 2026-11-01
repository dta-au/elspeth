"""Common-loop correction, bounded refusal, and live policy feedback regressions.

The provider is finite and scripted; tool validation, actual END review,
proposal acceptance, accounting, and durable failure disposition remain real.
The historical native test names are retained across the planner retirement.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.auth.models import UserIdentity
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer._required_paths_validator import _TOOL_REQUIRED_PATHS, _find_missing_required_paths
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_batch import (
    BatchAccumulator,
    ToolBatchContext,
    ToolBatchCustody,
    ToolBatchProvenance,
    _pre_dispatch_argument_error,
    run_tool_batch,
)
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools._dispatch import require_schema_valid_arguments
from elspeth.web.composer.tools.generation import explain_validation_code
from tests.helpers.composer_graphs import assert_isomorphic
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import FakeChoice, FakeFunction, FakeLLMResponse, FakeMessage, FakeToolCall

from .conftest import PARITY_FIXTURES, ParityEnv, _empty_state, rewrite_source_paths

_LINEAR = next(fixture for fixture in PARITY_FIXTURES if fixture["class"] == "linear_transform")
_POLICY_DENIED_TRANSFORM = "truncate"
_REPLY = "The corrected pipeline is waiting for approval."


def _valid_pipeline(env: ParityEnv, fixture: dict[str, Any], session_id: str) -> dict[str, Any]:
    return rewrite_source_paths(fixture["canonical_arguments"], env.data_dir, session_id)


def _malformed_missing_edges(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Drop a required semantic field; the actual set_pipeline S-gate must reject it."""
    return {key: value for key, value in pipeline.items() if key != "edges"}


def _policy_denied_variant(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Use an installed transform outside the parity deployment's allowed set."""
    denied = copy.deepcopy(pipeline)
    denied["nodes"][0]["plugin"] = _POLICY_DENIED_TRANSFORM
    return denied


def _last_tool_feedback(request: dict[str, Any]) -> dict[str, Any]:
    tool_messages = [message for message in request["messages"] if message.get("role") == "tool"]
    assert tool_messages, "scripted request carried no actual tool feedback"
    return json.loads(tool_messages[-1]["content"])


def _set_pipeline_completion(call_id: str, pipeline: dict[str, Any]) -> _CallModelOutcome:
    """One admitted primary completion carrying a set_pipeline call, as the parity CommonSDK scripted it."""
    arguments = json.dumps({"pipeline": pipeline})
    response = FakeLLMResponse(
        choices=[
            FakeChoice(
                message=FakeMessage(content=None, tool_calls=[FakeToolCall(id=call_id, function=FakeFunction("set_pipeline", arguments))])
            )
        ]
    )
    return _CallModelOutcome(completion=_admit_composer_llm_completion(response))


async def _dispatch_batch(
    env: ParityEnv,
    *,
    call_model: _CallModelOutcome,
    state: CompositionState,
    llm_messages: list[dict[str, Any]],
    recorder: BufferingRecorder,
    session_id: UUID,
    context: SessionOperationContext,
) -> Any:
    """Drive the shared dispatch owner ``tool_batch.run_tool_batch``.

    Continuation runs the same ``_initialize/_prepare/_execute/_finish``
    batch arms from its restored runtime
    (``continuation_runtime.build_runtime_tool_batch_context``); the deleted V1
    ``compose()`` reached them through ``_dispatch_tool_batch``.
    """
    service = env.composer
    preferences = await env.sessions.get_composer_preferences(session_id)
    ceiling = ComposerMutationCeiling(str(session_id), context.fence.operation_id, preferences.trust_mode)
    ceiling.validate_context(context)
    user = UserIdentity(user_id="alice", username="alice")
    snapshot = env.app.state.plugin_snapshot_factory(user)
    ctx = ToolBatchContext(
        session_tools=service._session_tools,
        provenance=ToolBatchProvenance(
            model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
        ),
        custody=ToolBatchCustody(
            data_dir=service._data_dir,
            session_engine=service._session_engine,
            secret_service=service._secret_service,
            secret_wiring_policy=service._secret_wiring_policy,
            max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
        ),
        redaction_telemetry=service._redaction_telemetry,
        preflight=service._preflight,
        schema_disclosure=service._schema_disclosure,
        advisor_checkpoint=service._advisor_checkpoint,
        advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
        advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
        recorder=recorder,
        anti_anchor=AntiAnchorTracker(),
        discovery_cache={},
        runtime_preflight_cache={},
        session_id=str(session_id),
        session_operation_context=context,
        user_id="alice",
        user_message_id=None,
        user_message_content=_LINEAR["intent"],
        current_state_id=None,
        actor="alice",
        initial_version=state.version,
        deadline=asyncio.get_running_loop().time() + 60,
        progress=None,
        session_scope="parity-repair",
        turn_sessions_service=env.sessions,
        turn_session_uuid=session_id,
        turn_preferences=preferences,
        mutation_ceiling=ceiling,
        review_required=not ceiling.permits_auto(preferences.trust_mode),
        cancellation_requested=asyncio.Event(),
        plugin_snapshot=snapshot,
        policy_catalog=PolicyCatalogView(env.app.state.catalog_service, snapshot, env.app.state.operator_profile_registry),
        tool_contract_dialect=service._planner_dialect,
        session_operation_authority=env.sessions.session_operation_authority,
    )
    acc = BatchAccumulator(state=state, last_validation=None, last_runtime_preflight=None, advisor_calls_used=0)
    dispatch, _advisor_calls = await run_tool_batch(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages)
    return dispatch


async def _drive_freeform_batches(
    env: ParityEnv, session: Any, bad: dict[str, Any], valid: dict[str, Any], *, expected_argument_error: ToolArgumentError | None = None
) -> tuple[CompositionState, dict[str, Any]]:
    """Reject ``bad`` then stage ``valid`` through the shared dispatch owner, then accept it.

    Returns the committed state and the tool feedback the model would read
    for the rejected candidate. The provider reply and END review that
    follow a staged proposal are the Continuation completion owners (U-40).
    """
    await env.sessions.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="test")
    recorder = BufferingRecorder()
    rejected_messages: list[dict[str, Any]] = []
    state = _empty_state()
    with fenced_operation_context(env.app.state.session_engine, str(session.id)) as context:
        rejected_dispatch = await _dispatch_batch(
            env,
            call_model=_set_pipeline_completion("candidate-1", bad),
            state=state,
            llm_messages=rejected_messages,
            recorder=recorder,
            session_id=session.id,
            context=context,
        )
        assert rejected_dispatch.plugin_crash is None
        repaired_dispatch = await _dispatch_batch(
            env,
            call_model=_set_pipeline_completion("candidate-2", valid),
            state=rejected_dispatch.state,
            llm_messages=[],
            recorder=recorder,
            session_id=session.id,
            context=context,
        )
        assert repaired_dispatch.plugin_crash is None
    rejected, repaired = recorder.invocations
    if expected_argument_error is not None:
        assert rejected.status is ComposerToolStatus.ARG_ERROR
        assert rejected.error_class == type(expected_argument_error).__name__
        assert rejected.error_category is expected_argument_error.category
    assert repaired.status is ComposerToolStatus.SUCCESS
    # The rejected and pending candidates have not changed the authoritative graph.
    assert await env.sessions.get_current_state(session.id) is None
    proposals = await env.sessions.list_composition_proposals(session.id, status="pending")
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.pipeline_metadata.repair_count == 0  # Common proposal metadata is not a planner repair counter.
    async with env._client() as client:
        response = await client.post(
            f"/api/sessions/{session.id}/proposals/{proposal.id}/accept", json={"draft_hash": proposal.pipeline_metadata.draft_hash}
        )
    assert response.status_code == 200, response.text
    return await env._committed_state(session.id), _last_tool_feedback({"messages": rejected_messages})


@pytest.mark.asyncio
async def test_freeform_one_repair_converges_to_reference_graph(parity_env: ParityEnv) -> None:
    reference = parity_env.reference_state(_LINEAR)
    session = await parity_env.sessions.create_session("alice", "Alice", "local")
    valid = _valid_pipeline(parity_env, _LINEAR, str(session.id))
    malformed = _malformed_missing_edges(valid)
    # The direct S-gate retains its structured repair signal, while the real
    # compose call reaches the schema-required-path arm before that later gate.
    with pytest.raises(ToolArgumentError) as rejected:
        require_schema_valid_arguments("set_pipeline", malformed)
    schema_expected = arg_error_payload(rejected.value, "set_pipeline")
    assert any(item["loc"] == ["edges"] and item["type"] == "missing" for item in schema_expected["validation_errors"])
    require_schema_valid_arguments("set_pipeline", valid)
    required_paths = _TOOL_REQUIRED_PATHS["set_pipeline"]
    assert _find_missing_required_paths(valid, required_paths) == []
    missing = _find_missing_required_paths(malformed, required_paths)
    assert missing and set(missing) == {"edges"}
    expected_error = _pre_dispatch_argument_error("set_pipeline", ToolArgumentErrorCategory.MISSING_REQUIRED_PATH)
    assert type(expected_error) is ToolArgumentError
    assert expected_error.category is ToolArgumentErrorCategory.MISSING_REQUIRED_PATH
    assert expected_error.category is not rejected.value.category
    assert expected_error.schema_violations == ()
    # This arm owns its display text (tool_batch.py), separately from the S
    # serializer. Preserve every compiled path, including shared ancestors.
    expected = {"error": f"Tool 'set_pipeline' missing required argument(s): {', '.join(missing)}"}
    committed, feedback = await _drive_freeform_batches(parity_env, session, malformed, valid, expected_argument_error=expected_error)

    assert_isomorphic(committed, reference, left="freeform-one-repair:linear_transform", right="reference")
    assert feedback == expected


@pytest.mark.asyncio
async def test_freeform_policy_denied_candidate_is_rejected_with_allowlisted_shape(parity_env: ParityEnv) -> None:
    reference = parity_env.reference_state(_LINEAR)
    session = await parity_env.sessions.create_session("alice", "Alice", "local")
    valid = _valid_pipeline(parity_env, _LINEAR, str(session.id))
    denied = _policy_denied_variant(valid)
    require_schema_valid_arguments("set_pipeline", denied)
    committed, feedback = await _drive_freeform_batches(parity_env, session, denied, valid)

    assert_isomorphic(committed, reference, left="freeform-policy-rejection:linear_transform", right="reference")
    assert feedback["success"] is False
    assert feedback["validation"]["is_valid"] is False
    (rejection,) = [entry for entry in feedback["validation"]["errors"] if entry["error_code"] == "plugin_not_enabled"]
    assert rejection["component"] == "rejected_mutation"
    assert rejection["rejected_component"] == f"node:{valid['nodes'][0]['id']}"
    guidance = feedback["validation_guidance"]["codes"]["plugin_not_enabled"]
    assert (guidance["explanation"], guidance["suggested_fix"]) == explain_validation_code("plugin_not_enabled")
    # The actual policy producer gives a closed generic refusal, not the denied
    # identifier. This is a live envelope check, not the removed planner wrapper.
    assert _POLICY_DENIED_TRANSFORM not in json.dumps(feedback)
