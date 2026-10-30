"""Common-loop correction, bounded refusal, and live policy feedback regressions.

The provider is finite and scripted; tool validation, actual END review,
proposal acceptance, accounting, and durable failure disposition remain real.
The historical native test names are retained across the planner retirement.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools import require_schema_valid_arguments
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.sessions.models import chat_messages_table
from tests.helpers.composer_common_sdk import CommonSDK, Step, assert_actual_audits
from tests.helpers.composer_graphs import assert_isomorphic
from tests.helpers.composer_operations import submit_and_settle

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


def _repair_script(env: ParityEnv, bad: dict[str, Any], valid: dict[str, Any]) -> CommonSDK:
    script = CommonSDK(
        request=_LINEAR["intent"],
        candidate=_REPLY,
        steps=(
            Step("primary", tool_name="set_pipeline", arguments={"pipeline": bad}),
            Step("primary", tool_name="set_pipeline", arguments={"pipeline": valid}),
            Step("primary", content=_REPLY),
            Step("end"),
        ),
    )
    script.bind(env.composer, env.monkeypatch)
    env.monkeypatch.setattr("litellm.acompletion", script)
    return script


async def _drive_freeform_scripted(env: ParityEnv, completion: CommonSDK, session: Any) -> tuple[Any, Any]:
    await env.sessions.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="test")
    user_message = await env.sessions.add_message(session.id, "user", _LINEAR["intent"], writer_principal="route_user_message")
    result = await env.composer.compose(
        _LINEAR["intent"], [], _empty_state(), session_id=str(session.id), user_id="alice", user_message_id=str(user_message.id)
    )
    if result.raw_assistant_content is not None:
        assert result.raw_assistant_content == _REPLY
    else:
        assert result.message == _REPLY
    completion.assert_done()
    assert completion.phases == ["end"]
    assert_actual_audits(
        env.app.state.session_engine, completion.requests, ("success", "success", "success", "success"), session_id=str(session.id)
    )
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
    return await env._committed_state(session.id), proposal


def _disposition_rows(engine: Any) -> list[Any]:
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.tool_calls)).all()
    return [
        row for row in rows if row.role == "audit" and row.tool_calls and row.tool_calls[0].get("_kind") == "planner_failure_disposition"
    ]


@pytest.mark.asyncio
async def test_freeform_one_repair_converges_to_reference_graph(parity_env: ParityEnv) -> None:
    reference = parity_env.reference_state(_LINEAR)
    session = await parity_env.sessions.create_session("alice", "Alice", "local")
    valid = _valid_pipeline(parity_env, _LINEAR, str(session.id))
    malformed = _malformed_missing_edges(valid)
    # Independent controls: the omitted field trips the live S-gate, the valid
    # neighbor passes it, and the actual feedback is produced by that same owner.
    with pytest.raises(ToolArgumentError) as rejected:
        require_schema_valid_arguments("set_pipeline", malformed)
    expected = arg_error_payload(rejected.value, "set_pipeline")
    assert any(item["loc"] == ["edges"] and item["type"] == "missing" for item in expected["validation_errors"])
    require_schema_valid_arguments("set_pipeline", valid)
    completion = _repair_script(parity_env, malformed, valid)

    committed, _proposal = await _drive_freeform_scripted(parity_env, completion, session)

    assert_isomorphic(committed, reference, left="freeform-one-repair:linear_transform", right="reference")
    assert _last_tool_feedback(completion.requests[1]) == expected


@pytest.mark.asyncio
async def test_freeform_repair_exhaustion_is_translated_to_a_safe_disposition(parity_env: ParityEnv) -> None:
    """The existing common call allowance bounds repeated failed tool attempts.

    This ports the public bounded-failure control; the retired hatch and its
    planner repair counter are deliberately not represented as live evidence.
    """
    bounded = parity_env.app.state.settings.model_copy(update={"composer_planner_max_provider_calls": 2})
    parity_env.monkeypatch.setattr(parity_env.composer, "_settings", bounded)
    async with parity_env._client() as client:
        created = await client.post("/api/sessions", json={"title": "freeform repair exhaustion"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        valid = _valid_pipeline(parity_env, _LINEAR, session_id)
        malformed = _malformed_missing_edges(valid)
        completion = CommonSDK(
            request=_LINEAR["intent"],
            candidate=_REPLY,
            steps=(
                Step("primary", tool_name="set_pipeline", arguments={"pipeline": malformed}),
                Step("primary", tool_name="set_pipeline", arguments={"pipeline": malformed}),
            ),
        )
        completion.bind(parity_env.composer, parity_env.monkeypatch)
        parity_env.monkeypatch.setattr("litellm.acompletion", completion)
        settled = await submit_and_settle(
            client,
            parity_env.app,
            path=f"/api/sessions/{session_id}/messages",
            body={"content": _LINEAR["intent"], "operation_id": str(uuid4()), "state_id": None},
        )

    status, body = settled.error()
    assert status == 502, settled.final.text
    assert body["detail"]["error_type"] == "composer_planner_failure"
    assert body["detail"]["failure_code"] == "invalid_provider_response"
    assert body["detail"]["planner_code"] == "PROVIDER_CALLS_EXHAUSTED"
    completion.assert_done()
    assert completion.phases == []
    assert_actual_audits(parity_env.app.state.session_engine, completion.requests, ("success", "success"), session_id=session_id)
    rows = _disposition_rows(parity_env.app.state.session_engine)
    assert len(rows) == 1
    envelope = rows[0].tool_calls[0]
    assert envelope["failure_code"] == "invalid_provider_response"
    assert envelope["planner_code"] == "PROVIDER_CALLS_EXHAUSTED"
    assert envelope["surface"] == "freeform"
    assert await parity_env.sessions.get_current_state(UUID(session_id)) is None
    assert await parity_env.sessions.list_composition_proposals(UUID(session_id)) == []


@pytest.mark.asyncio
async def test_freeform_policy_denied_candidate_is_rejected_with_allowlisted_shape(parity_env: ParityEnv) -> None:
    reference = parity_env.reference_state(_LINEAR)
    session = await parity_env.sessions.create_session("alice", "Alice", "local")
    valid = _valid_pipeline(parity_env, _LINEAR, str(session.id))
    denied = _policy_denied_variant(valid)
    require_schema_valid_arguments("set_pipeline", denied)
    completion = _repair_script(parity_env, denied, valid)

    committed, _proposal = await _drive_freeform_scripted(parity_env, completion, session)

    assert_isomorphic(committed, reference, left="freeform-policy-rejection:linear_transform", right="reference")
    feedback = _last_tool_feedback(completion.requests[1])
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
