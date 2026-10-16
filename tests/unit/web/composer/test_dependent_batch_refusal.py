"""Dependent incremental calls must not publish work on refused prerequisites."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer import tool_batch
from elspeth.web.composer.batch_dependencies import promised_resources, referenced_resources
from elspeth.web.composer.prompts import project_server_owned_option_metadata
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.dependencies import create_catalog_service
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer.test_compose_loop_persistence import _text_response, _tool_batch_response
from tests.unit.web.composer.test_prompt_patch_reviews import _amended_approved_state

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean


def _node(node_id, input_name, output_name="main"):
    return {
        "id": node_id,
        "node_type": "transform",
        "plugin": "passthrough",
        "input": input_name,
        "on_success": output_name,
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
    }


def _predecessor(state, *, refused=True, output_name="refused_connection"):
    node = project_server_owned_option_metadata(state.to_dict())["nodes"][0]
    node["on_success"] = output_name
    if refused:
        # Omit required wiring while retaining accepted vague-term identities.
        del node["options"]["prompt_template_parts"]
    return node


async def _run_batch(state, calls):
    service, session_id = composer_helpers._composer_service_with_session(create_catalog_service(), composer_helpers._make_settings())
    requests = []

    async def completion(*args, **kwargs):
        requests.append(kwargs)
        if len(requests) > 1:
            return _text_response("The saved results remain available for inspection.")
        framed = [
            (call_id, name, encode_semantic_arguments(name, ToolContractDialect.NONE, arguments)) for call_id, name, arguments in calls
        ]
        return _admit_composer_llm_completion(_tool_batch_response(*framed))

    with (
        patch.object(service._provider_gateway, "_call_llm", new=completion),
        patch.object(tool_batch, "execute_tool", wraps=tool_batch.execute_tool) as handlers,
    ):
        result = await service._run_one_turn_for_test(session_id=session_id, initial_state=state)
    return result, [call.args[0] for call in handlers.call_args_list]


@pytest.mark.asyncio
async def test_refused_reviewed_route_does_not_admit_dependent_successor():
    state = _amended_approved_state()
    before = state.to_dict()
    result, handlers = await _run_batch(
        state,
        [
            ("refused_predecessor", "upsert_node", _predecessor(state)),
            ("dependent_successor", "upsert_node", _node("dependent", "refused_connection")),
            ("independent_metadata", "set_metadata", {"patch": {"description": "independent survives"}}),
            ("independent_read", "get_pipeline_state", {}),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert not outcomes[0].response.success
    assert outcomes[0].response.validation.errors[0].error_code == "review_reconciliation_failed"
    assert not outcomes[1].response.success
    assert outcomes[1].response.validation.errors[0].error_code == "batch_dependency_refused"
    assert outcomes[1].pre_version == outcomes[1].post_version
    assert outcomes[2].response.success and outcomes[3].response.success
    updated = outcomes[2].response.updated_state
    assert updated.nodes == state.nodes
    assert updated.nodes[0].options == state.nodes[0].options
    assert updated.metadata.description == "independent survives"
    assert handlers == ["upsert_node", "set_metadata", "get_pipeline_state"]
    assert state.to_dict() == before
    persisted = json.loads(result.persisted_tool_row_content[1])
    assert not persisted["success"]
    assert persisted["validation"]["errors"][0]["error_code"] == "batch_dependency_refused"


@pytest.mark.asyncio
async def test_transitive_connection_and_component_dependencies_remain_unapplied():
    state = _amended_approved_state()
    result, handlers = await _run_batch(
        state,
        [
            ("refused_predecessor", "upsert_node", _predecessor(state)),
            ("dependent_one", "upsert_node", _node("dependent", "refused_connection", "dependent_out")),
            ("dependent_two", "upsert_node", _node("last", "dependent_out")),
            ("dependent_patch", "patch_node_options", {"node_id": "dependent", "patch": {"description": "ignored"}}),
            (
                "dependent_edge",
                "upsert_edge",
                {"id": "edge", "from_node": "code_themes", "to_node": "dependent", "edge_type": "on_success"},
            ),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert len(outcomes) == 5 and handlers == ["upsert_node"]
    for outcome in outcomes[1:]:
        assert not outcome.response.success
        assert outcome.response.validation.errors[0].error_code == "batch_dependency_refused"
        assert outcome.pre_version == outcome.post_version
        assert outcome.response.updated_state is state
    assert result.audit_outcome.checkpoint_disposition == "empty"


@pytest.mark.asyncio
async def test_existing_connection_and_same_component_remain_independently_editable():
    state = _amended_approved_state()
    current = project_server_owned_option_metadata(state.to_dict())["nodes"][0]
    current["description"] = "Independent saved-node description"
    result, handlers = await _run_batch(
        state,
        [
            ("refused_existing_connection", "upsert_node", _predecessor(state, output_name="source_out")),
            ("independent_existing_node", "upsert_node", current),
            ("independent_existing_connection", "upsert_node", _node("independent", "source_out")),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert not outcomes[0].response.success
    assert outcomes[1].response.success and outcomes[2].response.success
    assert handlers == ["upsert_node", "upsert_node", "upsert_node"]
    assert outcomes[1].response.updated_state.nodes[0].options == state.nodes[0].options


@pytest.mark.asyncio
async def test_fully_admitted_dependency_and_fresh_turn_repair_are_allowed():
    state = _amended_approved_state()
    failed, _ = await _run_batch(
        state,
        [
            ("refused_predecessor", "upsert_node", _predecessor(state)),
            ("blocked_successor", "upsert_node", _node("dependent", "refused_connection")),
        ],
    )
    assert not failed.tool_outcomes_for_assertion[1].response.success
    result, handlers = await _run_batch(
        state,
        [
            ("repaired_predecessor", "upsert_node", _predecessor(state, refused=False)),
            ("admitted_successor", "upsert_node", _node("dependent", "refused_connection")),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert all(outcome.response.success for outcome in outcomes)
    assert handlers == ["upsert_node", "upsert_node"]
    assert outcomes[0].response.updated_state.nodes[0].options == state.nodes[0].options


@pytest.mark.asyncio
async def test_new_semantics_after_refusal_still_require_fresh_review():
    state = _amended_approved_state()
    options = project_server_owned_option_metadata(state.to_dict())["nodes"][0]["options"]
    parts = deepcopy(options["prompt_template_parts"])
    parts[0]["text"] = "Assess "
    result, handlers = await _run_batch(
        state,
        [
            ("refused_predecessor", "upsert_node", _predecessor(state)),
            ("semantic_edit", "patch_node_options", {"node_id": "code_themes", "patch": {"prompt_template_parts": parts}}),
        ],
    )
    outcome = result.tool_outcomes_for_assertion[1]
    assert outcome.response.success and handlers == ["upsert_node", "patch_node_options"]
    reviews = outcome.response.updated_state.nodes[0].options["interpretation_requirements"]
    assert next(row for row in reviews if row["kind"] == "llm_prompt_template")["status"] == "pending"
    assert "approved_prompt_artifact_hash" not in outcome.response.updated_state.nodes[0].options


@pytest.mark.asyncio
async def test_component_name_collision_with_live_connection_and_discard_are_independent():
    state = _amended_approved_state()
    refused_node = _node("source_out", "source_out", "unavailable")
    refused_node["plugin"] = "missing_plugin"
    result, handlers = await _run_batch(
        state,
        [
            ("refused_component", "upsert_node", refused_node),
            (
                "refused_discard_sink",
                "set_output",
                {"sink_name": "discard", "plugin": "missing_plugin", "options": {}, "on_write_failure": "discard"},
            ),
            ("independent_connection", "upsert_node", _node("independent", "source_out")),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert not outcomes[0].response.success and not outcomes[1].response.success
    assert outcomes[2].response.success
    assert handlers == ["upsert_node", "set_output", "upsert_node"]


@pytest.mark.asyncio
async def test_full_replacement_dependency_refusal_withholds_unrelated_saved_findings():
    state = _amended_approved_state()
    state = state.with_node(
        replace(state.nodes[0], id="disconnected", plugin="passthrough", input="missing_saved", options={"schema": {"mode": "observed"}})
    )
    assert not state.validate().is_valid
    candidate, error = _serialize_set_pipeline_arguments(state)
    assert error is None and candidate is not None
    candidate["nodes"].append(_node("dependent", "refused_connection"))
    result, handlers = await _run_batch(
        state,
        [
            ("refused_predecessor", "upsert_node", _predecessor(state)),
            ("dependent_replacement", "set_pipeline", candidate),
        ],
    )
    outcome = result.tool_outcomes_for_assertion[1]
    assert handlers == ["upsert_node"]
    assert not outcome.response.success and outcome.response.updated_state is state
    assert outcome.pre_version == outcome.post_version
    assert [entry.error_code for entry in outcome.response.validation.errors] == ["batch_dependency_refused"]
    assert not outcome.response.validation.warnings


@pytest.mark.asyncio
async def test_independent_producer_can_restore_failed_promise_within_same_batch():
    state = _amended_approved_state()
    result, handlers = await _run_batch(
        state,
        [
            ("refused_predecessor", "upsert_node", _predecessor(state)),
            ("independent_producer", "upsert_node", _node("restored", "source_out", "refused_connection")),
            ("dependent_successor", "upsert_node", _node("dependent", "refused_connection")),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert not outcomes[0].response.success
    assert outcomes[1].response.success and outcomes[2].response.success
    assert handlers == ["upsert_node", "upsert_node", "upsert_node"]


@pytest.mark.asyncio
async def test_refused_sink_does_not_block_independent_connection_authoring_or_prerequisite_repair():
    state = _amended_approved_state()
    missing_sink = {"sink_name": "new_stream", "plugin": "missing_plugin", "options": {}, "on_write_failure": "discard"}
    refused_node = _node("new_node", "source_out", "main")
    refused_node["plugin"] = "missing_plugin"
    refused_node["on_error"] = "repair_sink"
    sink = {
        "sink_name": "repair_sink",
        "plugin": "json",
        "options": {"path": "outputs/repair.jsonl", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
        "on_write_failure": "discard",
    }
    result, handlers = await _run_batch(
        state,
        [
            ("refused_sink", "set_output", missing_sink),
            ("independent_connection", "upsert_node", _node("independent", "source_out", "new_stream")),
        ],
    )
    outcomes = result.tool_outcomes_for_assertion
    assert not outcomes[0].response.success and outcomes[1].response.success
    assert handlers == ["set_output", "upsert_node"]
    repaired, repair_handlers = await _run_batch(
        state,
        [
            ("refused_node", "upsert_node", refused_node),
            ("independent_prerequisite_sink", "set_output", sink),
        ],
    )
    outcomes = repaired.tool_outcomes_for_assertion
    assert not outcomes[0].response.success
    assert outcomes[1].response.success, outcomes[1].response.validation.errors
    assert repair_handlers == ["upsert_node", "set_output"]


@pytest.mark.parametrize("tool", ["upsert_node", "set_pipeline", "splice_transform", "wire_secret_ref"])
def test_dependency_projection_ignores_malformed_fields_without_inventing_resources(tool):
    malformed = {
        "id": [],
        "node_id": {},
        "node_type": [],
        "input": {},
        "on_success": [],
        "on_error": {},
        "routes": ["not-a-map"],
        "fork_to": [None, 1, {}],
        "node": 1,
        "source": [None],
        "sources": {"source": None},
        "nodes": [None, 1, [], {"id": None}],
        "outputs": [None],
        "edges": [False],
        "target": {},
        "target_id": [],
    }
    # A sources-map key is a genuine source identifier even if its value fails
    # later schema admission. All other malformed fields contribute nothing.
    assert promised_resources(tool, malformed) == (
        frozenset({("source", "source"), ("component", "source")}) if tool == "set_pipeline" else frozenset()
    )
    assert referenced_resources(tool, malformed) == frozenset()
