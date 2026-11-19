"""The duplicate-consumer facts through the real compose loop.

Two things a unit-level size check cannot show:

* the SECOND provider request of a turn, after one edit that leaves the
  duplicate-consumer error standing, fits under the request byte ceiling
  (``composer_planner_max_request_bytes``, 983,040 by default). Before the
  facts-only form, the edit's result carried every consumer again as whole
  ``upsert_node`` arguments, so a 25-step graph of long LLM prompts went over
  the ceiling and the turn was refused before the call;
* what explicit approval does to the fork the skill teaches (one gate and one
  re-pointed step per consumer, all through ``upsert_node``). This is a
  stated cost recorded for the handoff, not approval behaviour under change.

The provider is faked (``litellm.acompletion`` or the gateway's ``_call_llm``
replays scripted tool calls); the compose loop, tool batch, dispatcher,
serializers and context-budget guard are real.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from elspeth.core.canonical import canonical_json
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.composer._fork_facts_graph import SHARED, chain
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _composer_service_with_session
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean as _stub_advisor_end_gate_clean
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM
from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response, _make_settings, _passing_preflight
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state

_REQUEST_BYTE_CEILING = 960 * 1024  # 983,040: WebSettings.composer_planner_max_request_bytes default
_GENEROUS_TOKENS = 50_000_000
_STEPS = 25
_PROMPT_ROWS = 170  # about 20 KB of prompt per step: a 25-step graph of about 510 KB


class _RecordingProvider:
    def __init__(self, *responses: Any) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.requests.append(deepcopy(kwargs))
        return next(self._responses)


def _wire_bytes(request: dict[str, Any]) -> int:
    return len(canonical_json({"messages": request["messages"], "tools": request["tools"]}).encode("utf-8"))


@pytest.mark.asyncio
async def test_the_second_request_after_an_edit_that_leaves_the_error_fits_under_the_byte_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)
    settings = _make_settings(composer_context_max_prompt_tokens=_GENEROUS_TOKENS)
    assert settings.composer_planner_max_request_bytes == _REQUEST_BYTE_CEILING
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=settings)
    state = chain(steps=_STEPS, shared=_STEPS, llm_count=_STEPS, prompt_rows=_PROMPT_ROWS)
    assert {entry.error_code for entry in state.validate().errors} >= {"duplicate_connection_consumer"}

    provider = _RecordingProvider(
        # An edit that leaves every one of the 25 steps on the shared connection.
        _make_llm_response(
            tool_calls=[{"id": "call_rename", "name": "set_metadata", "arguments": {"patch": {"name": "renamed"}}}],
            request_id="chatcmpl-edit",
        ),
        _make_llm_response(content="Done.", request_id="chatcmpl-final"),
        *(_make_llm_response(content="Done.", request_id=f"chatcmpl-spare-{index}") for index in range(4)),
    )
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        await service.compose("Rename the pipeline.", [], state, session_id=session_id)

    sizes = [_wire_bytes(request) for request in provider.requests]
    assert len(sizes) >= 2, f"only {len(sizes)} provider request(s) sent; request bytes {sizes}"
    second = provider.requests[1]
    tool_results = [message for message in second["messages"] if message["role"] == "tool"]
    assert tool_results, "the second request must carry the edit's result"
    assert '"graph_repair_suggestions"' in tool_results[-1]["content"]
    assert "duplicate_connection_consumer" in tool_results[-1]["content"]
    assert sizes[1] < _REQUEST_BYTE_CEILING, f"second request {sizes[1]} bytes, ceiling {_REQUEST_BYTE_CEILING}"


# --- Explicit approval: the cost of the taught fork ------------------------------------------------------------


def _passthrough(node_id: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input=SHARED,
        on_success=f"{node_id}_out",
        on_error="discard",
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _three_consumer_state() -> CompositionState:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success=SHARED,
            on_validation_failure="discard",
            options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
        )
    )
    for index in range(3):
        state = state.with_node(_passthrough(f"step_{index}"))
    for index in range(3):
        state = state.with_output(
            OutputSpec(
                name=f"step_{index}_out",
                plugin="json",
                options={"path": f"outputs/step_{index}.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
    return state


def _taught_calls() -> list[dict[str, Any]]:
    """The skill block's fork for three steps, as N+1 upsert_node calls with model-chosen names."""
    gate = {
        "id": "fan_out",
        "node_type": "gate",
        "input": SHARED,
        "condition": "True",
        "routes": {"true": "fork", "false": "fork"},
        "fork_to": [f"{SHARED}_for_step_{index}" for index in range(3)],
    }
    steps = [
        {
            "id": f"step_{index}",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": f"{SHARED}_for_step_{index}",
            "on_success": f"step_{index}_out",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
        for index in range(3)
    ]
    return [gate, *steps]


async def _seed_explicit_session(service: ComposerServiceImpl, session_id: str) -> tuple[Any, CompositionState]:
    sessions = service._require_sessions_service()
    session_uuid = UUID(session_id)
    data = _three_consumer_state().to_dict()
    saved = await _save_composition_state(
        sessions,
        session_uuid,
        CompositionStateData(
            sources=data["sources"], nodes=data["nodes"], edges=data["edges"], outputs=data["outputs"], metadata_=data["metadata"]
        ),
        provenance="session_seed",
    )
    await sessions.update_composer_preferences(
        session_uuid, trust_mode="explicit_approve", density_default="high", actor="user:phase3-test-user"
    )
    return saved, state_from_record(saved)


def _outcome_summary(outcome: Any) -> tuple[str, bool | None, str | None]:
    response = outcome.response
    code = None
    success = None
    if isinstance(response, ToolResult):
        success = response.success
        code = response.validation.errors[0].error_code if not response.success and response.validation.errors else None
    return outcome.call.function.name, success, code if code is not None else outcome.error_class


@pytest.mark.asyncio
async def test_under_explicit_approval_the_taught_fork_in_one_response_is_recorded(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
) -> None:
    """All N+1 calls in one response: what approval does with them (recorded cost, not changed here)."""
    service = composer_service_with_real_sessions
    saved, state = await _seed_explicit_session(service, result_session_id)
    llm = _FakeComposeLLM(
        (
            _fake_llm_response(
                tool_calls=tuple(
                    {"id": f"fork_{index}", "name": "upsert_node", "arguments": call} for index, call in enumerate(_taught_calls())
                )
            ),
            _fake_llm_response(content="The fork has been proposed."),
        )
    )
    result = await service._run_one_turn_for_test(
        llm=llm, session_id=result_session_id, current_state_id=str(saved.id), initial_state=state
    )

    sessions = service._require_sessions_service()
    proposals = await sessions.list_composition_proposals(UUID(result_session_id))
    summary = [_outcome_summary(outcome) for outcome in result.tool_outcomes]
    current = await sessions.get_current_state(UUID(result_session_id))

    assert current is not None and current.id == saved.id, "nothing is applied under explicit approval"
    assert summary == _ONE_RESPONSE_EXPECTED, summary
    assert len(proposals) == _ONE_RESPONSE_PROPOSALS, [(p.tool_name, p.status) for p in proposals]


@pytest.mark.asyncio
async def test_under_explicit_approval_the_taught_fork_one_call_per_response_is_recorded(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
    tmp_path: Path,
) -> None:
    """One call per response, N+1 responses: what approval does with each (recorded cost, not changed here)."""
    service = composer_service_with_real_sessions
    saved, state = await _seed_explicit_session(service, result_session_id)
    llm = _FakeComposeLLM(
        (
            *(
                _fake_llm_response(tool_calls=({"id": f"fork_{index}", "name": "upsert_node", "arguments": call},))
                for index, call in enumerate(_taught_calls())
            ),
            _fake_llm_response(content="The fork has been proposed."),
        )
    )
    result = await service._run_one_turn_for_test(
        llm=llm, session_id=result_session_id, current_state_id=str(saved.id), initial_state=state
    )

    sessions = service._require_sessions_service()
    proposals = await sessions.list_composition_proposals(UUID(result_session_id))
    summary = [_outcome_summary(outcome) for outcome in result.tool_outcomes]
    current = await sessions.get_current_state(UUID(result_session_id))

    assert current is not None and current.id == saved.id, "nothing is applied under explicit approval"
    assert summary == _SERIAL_EXPECTED, summary
    assert len(proposals) == _SERIAL_PROPOSALS, [(p.tool_name, p.status) for p in proposals]


# Measured, and recorded as the stated cost of a model-built fork under explicit approval:
# * N+1 composition-changing calls in one response are refused as a batch, none proposed;
# * one call per response stages the first call (the gate) as a pending proposal and the turn ends there,
#   the next call in the same turn is refused while that proposal is pending;
# * accepted one at a time, every step commits, through states that do not validate until the last
#   (test_under_explicit_approval_the_fork_is_staged_and_accepted_one_call_at_a_time).
# Approval behaviour is not changed here.
_ONE_RESPONSE_EXPECTED: list[tuple[str, bool | None, str | None]] = [("upsert_node", False, "approval_batch_not_atomic")] * 4
_ONE_RESPONSE_PROPOSALS = 0
_SERIAL_EXPECTED: list[tuple[str, bool | None, str | None]] = [("upsert_node", False, "approval_proposal_pending")]
_SERIAL_PROPOSALS = 1


# --- Explicit approval: the whole chain, staged by the compose loop and accepted through the route --------------

_JSONL_SINK: dict[str, Any] = {"schema": {"mode": "observed"}, "format": "jsonl", "mode": "write", "collision_policy": "auto_increment"}
_RELAY_OPTIONS: dict[str, Any] = {"schema": {"mode": "observed"}, "mapping": {}}


def _chain_node(node_id: str, node_type: str, plugin: str, *, input_connection: str, **fields: Any) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=plugin,
        input=input_connection,
        on_success=f"{node_id}_out",
        on_error="discard",
        options=fields.pop("options"),
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        **fields,
    )


def _chain_state(input_path: Path, scenario: str) -> CompositionState:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success=SHARED,
            on_validation_failure="discard",
            options={"path": str(input_path), "schema": {"mode": "observed"}},
        )
    )
    first = _chain_node("step_0", "transform", "passthrough", input_connection=SHARED, options={"schema": {"mode": "observed"}})
    if scenario == "transforms":
        second = _chain_node("step_1", "transform", "passthrough", input_connection=SHARED, options={"schema": {"mode": "observed"}})
    else:
        second = _chain_node(
            "step_1",
            "aggregation",
            "batch_stats",
            input_connection=SHARED,
            options={"schema": {"mode": "observed"}, "value_field": "score"},
            trigger={"count": 2},
        )
    for node in (first, second):
        state = state.with_node(node)
        state = state.with_output(
            OutputSpec(
                name=f"{node.id}_out",
                plugin="json",
                options={"path": f"outputs/{node.id}.jsonl", **_JSONL_SINK},
                on_write_failure="discard",
            )
        )
    return state


def _chain_calls(state: CompositionState, scenario: str) -> list[dict[str, Any]]:
    """The fork as the model sends it: names it chose, steps re-sent whole as it read them."""
    data = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog()).to_dict()["data"]
    nodes = {node["id"]: dict(node) for node in data["nodes"]}
    branches = [f"{SHARED}_for_step_0", f"{SHARED}_for_step_1"]
    gate = {
        "id": "fan_out",
        "node_type": "gate",
        "input": SHARED,
        "condition": "True",
        "routes": {"true": "fork", "false": "fork"},
        "fork_to": branches,
    }
    first = {**nodes["step_0"], "input": branches[0]}
    if scenario == "transforms":
        return [gate, first, {**nodes["step_1"], "input": branches[1]}]
    relay = {
        "id": "relay_step_1",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": branches[1],
        "on_success": "step_1_rows",
        "on_error": "discard",
        "options": _RELAY_OPTIONS,
    }
    return [gate, first, relay, {**nodes["step_1"], "input": "step_1_rows"}]


def _codes(errors: Any) -> list[str]:
    return sorted({error.error_code or error.component for error in errors or ()})


def _model_visible_codes(turn: Any) -> list[str]:
    [outcome] = turn.tool_outcomes
    assert isinstance(outcome.response, ToolResult)
    return _codes(outcome.response.validation.errors)


@pytest.mark.parametrize("scenario", ["transforms", "aggregation_relay"])
def test_under_explicit_approval_the_fork_is_staged_and_accepted_one_call_at_a_time(tmp_path: Path, scenario: str) -> None:
    """Two consumers on a graph whose duplicate-consumer error already stands.

    Each response carries ONE upsert_node; the real compose loop stages it as a
    pending proposal; the real accept route (dispatch, required-control
    finalisation, runtime preflight, real plugin catalog, commit) applies it.
    Recorded per step: what the model sees on the staged result, proposal
    staged, accept status, committed validity and error codes.
    """
    import asyncio

    from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
    from tests.unit.web.sessions.test_routes import _make_app

    app, sessions = _make_app(tmp_path)
    client = TestClient(app)
    session_id = client.post("/api/sessions", json={"title": "Fork under approval"}).json()["id"]
    session_uuid = UUID(session_id)
    input_path = tmp_path / "blobs" / session_id / "input.csv"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("text,score\nhello,1\n", encoding="utf-8")
    seed_state = _chain_state(input_path, scenario)
    data = seed_state.to_dict()
    asyncio.run(
        _save_composition_state(
            sessions,
            session_uuid,
            CompositionStateData(
                sources=data["sources"], nodes=data["nodes"], edges=data["edges"], outputs=data["outputs"], metadata_=data["metadata"]
            ),
            provenance="session_seed",
        )
    )
    asyncio.run(
        sessions.update_composer_preferences(session_uuid, trust_mode="explicit_approve", density_default="high", actor="user:alice")
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path), sessions_service=sessions
    )
    seeded = asyncio.run(sessions.get_current_state(session_uuid))
    assert seeded is not None
    seed_summary = state_from_record(seeded).validate()
    steps: list[tuple[Any, ...]] = [("seed", None, None, None, seed_summary.is_valid, _codes(seed_summary.errors))]

    for index, call in enumerate(_chain_calls(_chain_state(Path("/data/in.csv"), scenario), scenario)):
        current = asyncio.run(sessions.get_current_state(session_uuid))
        assert current is not None
        llm = _FakeComposeLLM(
            (
                _fake_llm_response(tool_calls=({"id": f"fork_{index}", "name": "upsert_node", "arguments": call},)),
                _fake_llm_response(content="Proposed."),
            )
        )
        turn = asyncio.run(
            composer._run_one_turn_for_test(
                llm=llm, session_id=session_id, current_state_id=str(current.id), initial_state=state_from_record(current)
            )
        )
        staged = [_outcome_summary(outcome) for outcome in turn.tool_outcomes]
        pending = [p for p in asyncio.run(sessions.list_composition_proposals(session_uuid)) if p.status == "pending"]
        assert len(pending) == 1, (call["id"], staged, [(p.tool_name, p.status) for p in pending])
        staged_only = asyncio.run(sessions.get_current_state(session_uuid))
        assert staged_only is not None and staged_only.id == current.id, "staging applies nothing"
        accepted = client.post(f"/api/sessions/{session_id}/proposals/{pending[0].id}/accept")
        after = asyncio.run(sessions.get_current_state(session_uuid))
        assert after is not None
        assert after.id != current.id, "the accept committed a new state"
        steps.append(
            (call["id"], staged, _model_visible_codes(turn), accepted.status_code, after.is_valid, _codes(after.validation_errors))
        )

    assert steps == _APPROVAL_CHAIN_EXPECTED[scenario], steps


_DUPLICATE = "duplicate_connection_consumer"
_UNREAD_BRANCH = "fork_branch_no_destination"
_STAGED = [("upsert_node", True, None)]
# Executed: every staging records one pending proposal and applies nothing. The staged result the model sees
# carries the SAVED state's codes (the change is not applied yet), so the unread fork branch first shows on the
# result of the call after the gate. Every accept commits (200) although the in-between states are invalid; the
# last accept validates.
_DANGLING_RELAY = "transform_on_success_dangling"
_APPROVAL_CHAIN_EXPECTED: dict[str, list[tuple[Any, ...]]] = {
    "transforms": [
        ("seed", None, None, None, False, [_DUPLICATE]),
        ("fan_out", _STAGED, [_DUPLICATE], 200, False, [_DUPLICATE, _UNREAD_BRANCH]),
        ("step_0", _STAGED, [_DUPLICATE, _UNREAD_BRANCH], 200, False, [_DUPLICATE, _UNREAD_BRANCH]),
        ("step_1", _STAGED, [_DUPLICATE, _UNREAD_BRANCH], 200, True, []),
    ],
    "aggregation_relay": [
        ("seed", None, None, None, False, [_DUPLICATE]),
        ("fan_out", _STAGED, [_DUPLICATE], 200, False, [_DUPLICATE, _UNREAD_BRANCH]),
        ("step_0", _STAGED, [_DUPLICATE, _UNREAD_BRANCH], 200, False, [_DUPLICATE, _UNREAD_BRANCH]),
        ("relay_step_1", _STAGED, [_DUPLICATE, _UNREAD_BRANCH], 200, False, [_DUPLICATE, _DANGLING_RELAY]),
        ("step_1", _STAGED, [_DUPLICATE, _DANGLING_RELAY], 200, True, []),
    ],
}
