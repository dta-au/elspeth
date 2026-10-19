"""Composer preview, input proof and prevalidation tools."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec
from elspeth.web.composer.tools import _prevalidate_plugin_options
from elspeth.web.composer.tools.generation import ComposerProofContext, ProofEvaluated, ProofUnavailable
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.schemas import ValidationCheck, ValidationError, ValidationReadiness, ValidationResult
from elspeth.web.sessions.models import blobs_table
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.composer.test_tools import (
    _PREVIEW_DATA_KEYS,
    _blob_operation,
    _default_source,
    _empty_state,
    _handoff_strict_preflight,
    _llm_options_with_api_key,
    _mock_catalog,
    _pending_prompt_template_llm_node,
    _pipeline_state_default_source,
    _stage1_valid_preview_state,
    _SyncCallRecorder,
    _tolerant_result,
    _trained_tool_context,
    _valid_pipeline_args,
    execute_tool,
)


@pytest.fixture
def passing_runtime_preflight() -> _SyncCallRecorder:
    """A wired Stage-2 runtime preflight that passes.

    ``preview_pipeline`` fails closed when no preflight callback is wired
    (an un-run stage may not ride the success side), so a test that wants
    a preview to publish ``preview_is_valid: True`` must wire one. Deliberately
    NOT autouse: a blanket always-passing stub would hide exactly the
    un-run-stage state this guard exists to expose.
    """
    return _SyncCallRecorder(
        ValidationResult(
            is_valid=True,
            checks=[ValidationCheck(name="settings_load", passed=True, detail="Settings loaded.", affected_nodes=(), outcome_code=None)],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        )
    )


class TestPreviewPipelineSemanticContracts:
    """A preview's envelope ``validation`` carries semantic_contracts; ``data`` does not twin them (R4)."""

    def test_envelope_validation_includes_semantic_contracts(self) -> None:
        from elspeth.web.composer.tools import _execute_preview_pipeline
        from tests.unit.web.composer.test_semantic_validator import _wardline_state

        state = _wardline_state(text_separator=" ")
        result = _execute_preview_pipeline({}, state, _trained_tool_context())
        contracts = result.to_dict()["validation"]["semantic_contracts"]
        assert len(contracts) == 1
        assert contracts[0]["outcome"] == "conflict"
        assert contracts[0]["consumer_plugin"] == "line_explode"
        assert "semantic_contracts" not in result.data


class TestPreviewPipeline:
    @pytest.mark.parametrize("destination_kind", ["transform", "gate", "sink", "coalesce", "row_union"])
    def test_fork_destination_guidance_matches_legal_consumers(self, destination_kind: str) -> None:
        from elspeth.web.composer.tools.generation import build_validation_guidance

        leaf = NodeSpec(
            id="left",
            node_type="transform",
            plugin="passthrough",
            input="left_branch",
            on_success="left_out",
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        fork = replace(
            leaf,
            id="fanout",
            node_type="gate",
            plugin=None,
            input="inbound",
            on_success=None,
            on_error=None,
            options={},
            condition="True",
            routes={"true": "fork", "false": "fork"},
            fork_to=("left_branch", "right_branch"),
        )
        state = _empty_state().with_source(
            SourceSpec(
                plugin="csv",
                on_success="inbound",
                on_validation_failure="discard",
                options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
            )
        )
        state = state.with_node(fork)
        output_names = ("left_out", "right_out")
        if destination_kind == "sink":
            output_names = ("left_branch", "right_branch")
        elif destination_kind in ("transform", "gate"):
            for side in ("left", "right"):
                consumer = replace(leaf, id=side, input=f"{side}_branch", on_success=f"{side}_out")
                if destination_kind == "gate":
                    consumer = replace(
                        consumer,
                        node_type="gate",
                        plugin=None,
                        on_success=None,
                        on_error=None,
                        options={},
                        condition="True",
                        routes={"true": f"{side}_out", "false": f"{side}_out"},
                    )
                state = state.with_node(consumer)
        else:
            output_names = ("main",)
            state = state.with_node(
                replace(
                    leaf,
                    id="rejoin",
                    node_type=destination_kind,
                    plugin=None,
                    on_success="main" if destination_kind == "coalesce" else "union_out",
                    on_error=None,
                    options={},
                    branches={"left_branch": "left_branch", "right_branch": "right_branch"},
                    policy="require_all" if destination_kind == "coalesce" else None,
                    merge="union" if destination_kind == "coalesce" else None,
                )
            )
            if destination_kind == "row_union":
                state = state.with_node(replace(leaf, id="after_union", input="union_out", on_success="main"))
        for name in output_names:
            state = state.with_output(
                OutputSpec(
                    name=name,
                    plugin="csv",
                    options={"path": f"outputs/{name}.csv", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        validation = state.validate()
        assert validation.is_valid, [entry.message for entry in validation.errors]
        if destination_kind in ("transform", "gate", "sink"):
            assert all(node.node_type not in ("coalesce", "row_union") for node in state.nodes)

        # Control the validator predicate: an unconsumed branch really fails.
        broken = state.with_node(replace(fork, fork_to=(*fork.fork_to, "unclaimed_branch")))
        preview = execute_tool("preview_pipeline", {}, broken, _mock_catalog())
        assert "fork_branch_no_destination" in {entry.error_code for entry in preview.validation.errors}
        catalogue = build_validation_guidance(entry.error_code for entry in preview.validation.errors)
        assert catalogue is not None
        inline = catalogue["codes"]["fork_branch_no_destination"]
        explained = execute_tool("explain_validation_error", {"error_text": "fork_branch_no_destination"}, broken, _mock_catalog())
        assert explained.success is True
        assert explained.updated_state is broken
        assert {key: explained.data[key] for key in ("explanation", "suggested_fix")} == inline
        guidance = f"{inline['explanation']} {inline['suggested_fix']}"
        assert "transform/gate" in guidance
        assert "row_union" in guidance
        assert "coalesce" in guidance
        assert "sink" in guidance
        assert "does not require coalesce" in guidance
        assert "input" in inline["suggested_fix"]

    def test_preview_empty_pipeline(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("preview_pipeline", {}, state, catalog)
        assert result.success is True
        assert result.data["preview_is_valid"] is False
        assert _pipeline_state_default_source(result.data) is None
        assert result.data["node_count"] == 0

    def test_preview_valid_pipeline(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # Build a minimal valid pipeline
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "t1",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r2.updated_state,
            catalog,
        )
        result = execute_tool("preview_pipeline", {}, r3.updated_state, catalog)
        assert result.success is True
        assert _pipeline_state_default_source(result.data)["plugin"] == "csv"
        assert _pipeline_state_default_source(result.data)["has_schema_config"] is True
        assert result.data["node_count"] == 1
        assert result.data["output_count"] == 1

    def test_preview_pipeline_includes_edge_contracts(self, passing_runtime_preflight: _SyncCallRecorder) -> None:
        """preview_pipeline includes raw edge contract evidence from validation."""
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "value_transform",
                "input": "t1",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "required_input_fields": ["text"],
                    "operations": [{"target": "out", "expression": "row['text']"}],
                    "schema": {"mode": "observed"},
                },
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r2.updated_state,
            catalog,
        )

        result = execute_tool("preview_pipeline", {}, r3.updated_state, catalog, runtime_preflight=passing_runtime_preflight)

        assert result.success is True
        assert "edge_contracts" in result.data
        contracts = result.data["edge_contracts"]
        assert len(contracts) >= 1
        source_to_t1 = next(c for c in contracts if c["to"] == "t1")
        assert source_to_t1["from"] == "source"
        assert "text" in source_to_t1["producer_guarantees"]
        assert "text" in source_to_t1["consumer_requires"]
        assert source_to_t1["satisfied"] is True
        assert result.data["preview_is_valid"] is True

    def test_reserved_connection_names_covers_every_implicit_self_publisher(self) -> None:
        """A repair branch may not be minted onto a name an implicit publisher owns.

        ``_reserved_connection_names`` read ``node.on_success`` directly, so a
        node publishing under its OWN id contributed nothing. The hole only
        opens for a DANGLING one — a consumed publisher's id arrives via its
        consumer's ``input``, and a queue's id is always its own ``input`` —
        which is exactly why it survived: every ordinary pipeline hides it.

        Enumerated FROM ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` so a fourth
        node kind cannot skip this site, and so the partial fix (restating the
        set as coalesce+aggregation) fails here.
        """
        from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES, published_success_connection
        from elspeth.web.composer.tools._common import _reserved_connection_names

        for kind in sorted(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES):
            publisher = NodeSpec(
                id="publisher",
                node_type=kind,
                plugin="batch_stats" if kind == "aggregation" else None,
                # A queue's input IS its own id; the others consume upstream.
                input="publisher" if kind == "queue" else "feed_out",
                on_success=None,
                on_error="main" if kind == "aggregation" else None,
                options={"schema": {"mode": "observed"}} if kind == "aggregation" else {},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"only": "feed_out"} if kind == "coalesce" else None,
                policy="all" if kind == "coalesce" else None,
                merge="row_union" if kind == "coalesce" else None,
                **({"trigger": {"count": 2}} if kind == "aggregation" else {}),
            )
            assert published_success_connection(publisher) == "publisher", kind
            state = (
                _empty_state()
                .with_source(
                    SourceSpec(
                        plugin="csv",
                        on_success="rows",
                        options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                        on_validation_failure="discard",
                    )
                )
                .with_node(
                    NodeSpec(
                        id="feeder",
                        node_type="transform",
                        plugin="passthrough",
                        input="rows",
                        on_success="feed_out",
                        on_error="main",
                        options={"schema": {"mode": "observed"}},
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(publisher)
                .with_output(OutputSpec(name="main", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"))
            )
            assert "publisher" in _reserved_connection_names(state), (
                f"A dangling '{kind}' publishing under its own id is not a reserved connection name, so a "
                "repair branch can be minted onto it. Applying that repair introduces a fresh "
                "duplicate_connection_producer — the planner's own repair breaks the pipeline. "
                "Ask published_success_connection; do not restate the set here."
            )

    def test_duplicate_consumer_repair_does_not_mint_a_branch_onto_a_publisher_id(self) -> None:
        """The consequence, end to end: the repair must not create a new error.

        Measured before the fix on this exact shape — the generated branch
        name collided with the dangling aggregation's id and applying the
        suggestion produced:

            Duplicate producer for connection 'shared_to_t1':
            aggregation 'shared_to_t1' and gate 'fork_shared' fork 'shared_to_t1'

        One error in, a different error out, with the planner having done
        exactly what it was told.
        """
        from elspeth.web.composer.tools._common import _graph_repair_suggestions

        def _passthrough(node_id: str, input_label: str, on_success: str) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="transform",
                plugin="passthrough",
                input=input_label,
                on_success=on_success,
                on_error="main",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )

        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="shared",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(_passthrough("t1", "shared", "main"))
            .with_node(_passthrough("t2", "shared", "main"))
            .with_node(_passthrough("feeder", "shared", "agg_in"))
            .with_node(
                NodeSpec(
                    # id deliberately equals the branch name the repair generates
                    id="shared_to_t1",
                    node_type="aggregation",
                    plugin="batch_stats",
                    input="agg_in",
                    on_success=None,
                    on_error="main",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 2},
                )
            )
            .with_output(OutputSpec(name="main", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"))
        )

        validation = state.validate()
        assert any(error.error_code == "duplicate_connection_consumer" for error in validation.errors)

        generated_branches: list[str] = []
        for suggestion in _graph_repair_suggestions(state, validation):
            for tool_call in suggestion["tool_sequence"]:
                branches = tool_call.get("arguments", {}).get("fork_to")
                if branches:
                    generated_branches.extend(branches)

        assert generated_branches, "the duplicate-consumer repair produced no fork branches to check"
        assert "shared_to_t1" not in generated_branches, (
            "The repair minted a branch named 'shared_to_t1', which is already the implicit connection id of "
            f"the dangling aggregation of that name. Generated: {generated_branches}. Applying this repair "
            "creates a duplicate_connection_producer the pipeline did not have."
        )

    def test_preview_pipeline_suggests_fork_gate_for_duplicate_consumers(self) -> None:
        """Duplicate consumers get a copyable fork-gate repair skeleton."""
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="classified_rows",
                    options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
                    on_validation_failure="quarantine",
                )
            )
            .with_node(
                NodeSpec(
                    id="fraud_filter",
                    node_type="transform",
                    plugin="value_transform",
                    input="classified_rows",
                    on_success="fraud_rows",
                    on_error="discard",
                    options={
                        "operations": [{"target": "fraud_flag", "expression": "row['text']"}],
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="regular_filter",
                    node_type="transform",
                    plugin="value_transform",
                    input="classified_rows",
                    on_success="regular_rows",
                    on_error="discard",
                    options={
                        "operations": [{"target": "regular_flag", "expression": "row['text']"}],
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(OutputSpec(name="fraud_rows", plugin="csv", options={"path": "outputs/fraud.csv"}, on_write_failure="discard"))
            .with_output(OutputSpec(name="regular_rows", plugin="csv", options={"path": "outputs/regular.csv"}, on_write_failure="discard"))
        )

        result = execute_tool("preview_pipeline", {}, state, _mock_catalog())
        payload = result.to_dict()["validation"]

        assert result.success is True
        assert any("Duplicate consumer for connection 'classified_rows'" in err["message"] for err in payload["errors"])
        repairs = payload["graph_repair_suggestions"]
        assert len(repairs) == 1
        repair = repairs[0]
        assert repair["code"] == "duplicate_consumer_connection"
        assert repair["connection"] == "classified_rows"
        assert repair["strategy"] == "insert_fork_gate"
        assert repair["tool_sequence"][0]["arguments"]["input"] == "classified_rows_to_fraud_filter"
        assert repair["tool_sequence"][1]["arguments"]["input"] == "classified_rows_to_regular_filter"
        assert repair["tool_sequence"][2] == {
            "tool": "upsert_node",
            "arguments": {
                "id": "fork_classified_rows",
                "node_type": "gate",
                "plugin": None,
                "input": "classified_rows",
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["classified_rows_to_fraud_filter", "classified_rows_to_regular_filter"],
                "branches": None,
                "policy": None,
                "merge": None,
                "trigger": None,
                "output_mode": None,
                "expected_output_count": None,
            },
        }
        assert repair["tool_sequence"][-1] == {"tool": "preview_pipeline", "arguments": {}}

        fixed_state = state
        for step in repair["tool_sequence"][:-1]:
            step_result = execute_tool(step["tool"], step["arguments"], fixed_state, _mock_catalog())
            fixed_state = step_result.updated_state
        fixed_validation = execute_tool("preview_pipeline", {}, fixed_state, _mock_catalog()).to_dict()["validation"]

        assert not any("Duplicate consumer for connection 'classified_rows'" in err["message"] for err in fixed_validation["errors"])

    @pytest.mark.parametrize("duplicate_branch", ["control", "treatment"])
    def test_duplicate_consumer_repair_patches_row_union_branch_and_validates(self, duplicate_branch: str) -> None:
        """Every suggested mutation succeeds and repairs the real branch slot.

        The deliberately minimal fork skeleton does not synthesize an inner
        correlated barrier. It therefore leaves the pre-existing independent
        fork-destination/topology issue visible for the planner's next repair
        turn instead of silently changing downstream semantics.
        """
        branch_connections = {
            "control": "control_done",
            "treatment": "treatment_done",
        }
        duplicate_connection = branch_connections[duplicate_branch]
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(
                NodeSpec(
                    id="fan_out",
                    node_type="gate",
                    plugin=None,
                    input="rows",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("control", "treatment"),
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="control_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="control",
                    on_success="control_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="treatment_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="treatment",
                    on_success="treatment_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="variant_union",
                    node_type="row_union",
                    plugin=None,
                    input="control_done",
                    on_success="unioned_rows",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=branch_connections,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="duplicate_reader",
                    node_type="transform",
                    plugin="passthrough",
                    input=duplicate_connection,
                    on_success="duplicate_rows",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="consume_union",
                    node_type="transform",
                    plugin="passthrough",
                    input="unioned_rows",
                    on_success="main",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={"path": "outputs/main.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
            .with_output(
                OutputSpec(
                    name="duplicate_rows",
                    plugin="json",
                    options={"path": "outputs/duplicate.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )

        preview = execute_tool("preview_pipeline", {}, state, _mock_catalog())
        repair = preview.to_dict()["validation"]["graph_repair_suggestions"][0]
        union_step = next(
            step for step in repair["tool_sequence"] if step["tool"] == "upsert_node" and step["arguments"]["id"] == "variant_union"
        )
        repaired_branches = union_step["arguments"]["branches"]
        repaired_connection = repaired_branches[duplicate_branch]

        assert repaired_connection != duplicate_connection
        assert union_step["arguments"]["input"] == (repaired_connection if duplicate_branch == "control" else "control_done")

        repaired_state = state
        for step in repair["tool_sequence"][:-1]:
            step_result = execute_tool(step["tool"], dict(step["arguments"]), repaired_state, _mock_catalog())
            assert step_result.success is True, step_result.to_dict()
            repaired_state = step_result.updated_state

        repaired_preview = execute_tool("preview_pipeline", {}, repaired_state, _mock_catalog())
        remaining_codes = {entry.error_code for entry in repaired_preview.validation.errors}
        assert "duplicate_connection_consumer" not in remaining_codes
        assert "row_union_input_mismatch" not in remaining_codes
        assert "fork_branch_no_destination" in remaining_codes

    def test_duplicate_consumer_repair_excludes_row_union_identity_branch(self) -> None:
        """Identity barrier branches are direct COPY edges, not connection consumers."""

        def _consumer(node_id: str) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="transform",
                plugin="passthrough",
                input="control",
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

        state = (
            _empty_state()
            .with_node(_consumer("ordinary_a"))
            .with_node(_consumer("ordinary_b"))
            .with_node(
                NodeSpec(
                    id="variant_union",
                    node_type="row_union",
                    plugin=None,
                    input="control",
                    on_success="unioned_rows",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches={"control": "control", "treatment": "treatment"},
                    policy=None,
                    merge=None,
                )
            )
        )

        preview_validation = execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()["validation"]
        duplicate_error = next(entry for entry in preview_validation["errors"] if entry["error_code"] == "duplicate_connection_consumer")
        assert "ordinary_a" in duplicate_error["message"]
        assert "ordinary_b" in duplicate_error["message"]
        assert "variant_union" not in duplicate_error["message"]

        repair = next(entry for entry in preview_validation["graph_repair_suggestions"] if entry["connection"] == "control")
        assert [consumer["id"] for consumer in repair["affected_consumers"]] == [
            "ordinary_a",
            "ordinary_b",
        ]
        assert [step["arguments"]["id"] for step in repair["tool_sequence"] if step["tool"] == "upsert_node"] == [
            "ordinary_a",
            "ordinary_b",
            "fork_control",
        ]

    def test_duplicate_consumer_repair_patches_one_node_once_for_two_branch_bindings(self) -> None:
        """Two aliases of one row_union sharing a connection collapse to one upsert.

        A repair skeleton rebuilt from the original node per binding emits two
        ``upsert_node`` calls for the same id, and the second reverts the first
        — applying the suggested sequence leaves the duplicate in place. The
        patches must accumulate onto one payload per node id while the fork
        gate still publishes one distinct branch per binding.
        """
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(
                NodeSpec(
                    id="fan_out",
                    node_type="gate",
                    plugin=None,
                    input="rows",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("control", "treatment"),
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="control_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="control",
                    on_success="control_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="treatment_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="treatment",
                    on_success="treatment_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            # Inner barrier: its release connection is downstream of BOTH fork
            # branches, so the outer row_union can bind both aliases to it.
            .with_node(
                NodeSpec(
                    id="inner_union",
                    node_type="row_union",
                    plugin=None,
                    input="control_done",
                    on_success="union_out",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches={"control": "control_done", "treatment": "treatment_done"},
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="outer_union",
                    node_type="row_union",
                    plugin=None,
                    input="union_out",
                    on_success="union_out2",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    # BOTH aliases claim the one connection: one node, two bindings.
                    branches={"control": "union_out", "treatment": "union_out"},
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="consume_union",
                    node_type="transform",
                    plugin="passthrough",
                    input="union_out2",
                    on_success="main",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={"path": "outputs/main.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )

        preview_validation = execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()["validation"]
        assert "duplicate_connection_consumer" in {entry["error_code"] for entry in preview_validation["errors"]}
        repair = next(entry for entry in preview_validation["graph_repair_suggestions"] if entry["connection"] == "union_out")

        upsert_ids = [step["arguments"]["id"] for step in repair["tool_sequence"] if step["tool"] == "upsert_node"]
        assert len(upsert_ids) == len(set(upsert_ids)), repair["tool_sequence"]
        assert upsert_ids.count("outer_union") == 1

        union_step = next(step for step in repair["tool_sequence"] if step["arguments"].get("id") == "outer_union")
        patched_branches = union_step["arguments"]["branches"]
        assert patched_branches["control"] != patched_branches["treatment"]
        assert "union_out" not in patched_branches.values()
        assert union_step["arguments"]["input"] == patched_branches["control"]

        gate_step = repair["tool_sequence"][-2]
        assert gate_step["arguments"]["node_type"] == "gate"
        assert sorted(gate_step["arguments"]["fork_to"]) == sorted(patched_branches.values())
        assert repair["tool_sequence"][-1] == {"tool": "preview_pipeline", "arguments": {}}

        repaired_state = state
        for step in repair["tool_sequence"][:-1]:
            step_result = execute_tool(step["tool"], dict(step["arguments"]), repaired_state, _mock_catalog())
            assert step_result.success is True, step_result.to_dict()
            repaired_state = step_result.updated_state

        repaired_preview = execute_tool("preview_pipeline", {}, repaired_state, _mock_catalog())
        remaining_codes = {entry.error_code for entry in repaired_preview.validation.errors}
        assert "duplicate_connection_consumer" not in remaining_codes
        assert "row_union_input_mismatch" not in remaining_codes

    def test_preview_source_with_schema_config_field_name(self) -> None:
        state = _empty_state().with_source(
            SourceSpec(
                plugin="csv",
                on_success="t1",
                options={"path": "/data/in.csv", "schema_config": {"mode": "observed"}},
                on_validation_failure="quarantine",
            )
        )
        catalog = _mock_catalog()

        result = execute_tool("preview_pipeline", {}, state, catalog)

        assert result.success is True
        assert _pipeline_state_default_source(result.data)["plugin"] == "csv"
        assert _pipeline_state_default_source(result.data)["has_schema_config"] is True

    def test_preview_pipeline_surfaces_runtime_preflight_failure(self) -> None:
        state = _stage1_valid_preview_state()
        catalog = _mock_catalog()
        runtime_preflight = _SyncCallRecorder(
            ValidationResult(
                is_valid=False,
                checks=[
                    ValidationCheck(
                        name="settings_load",
                        passed=False,
                        detail="Forbidden name: 'end_of_source'",
                        affected_nodes=(),
                        outcome_code=None,
                    )
                ],
                errors=[
                    ValidationError(
                        component_id="agg1",
                        component_type="aggregation",
                        message="Forbidden name: 'end_of_source'",
                        suggestion="Omit trigger for end-of-source-only aggregation.",
                        error_code=None,
                    )
                ],
                readiness=ValidationReadiness(authoring_valid=False, execution_ready=False, completion_ready=False, blockers=[]),
            )
        )

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            catalog,
            data_dir="/data",
            runtime_preflight=runtime_preflight,
        )

        assert result.success is True
        assert result.runtime_preflight is not None
        assert result.validation.is_valid is True
        assert result.runtime_preflight.is_valid is False
        assert result.data["preview_is_valid"] is False
        assert result.runtime_preflight.errors[0].message == "Forbidden name: 'end_of_source'"
        # The preflight rides ONLY on the envelope field; no nested twin.
        assert "runtime_preflight" not in result.data
        runtime_preflight.assert_called_once_with(state)

    def test_preview_pipeline_without_runtime_preflight_keeps_the_authoring_verdict_on_the_envelope(self) -> None:
        state = _stage1_valid_preview_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=None,
        )

        assert result.success is True
        assert result.runtime_preflight is None
        assert result.validation.is_valid is True
        assert "runtime_preflight" not in result.data
        assert result.data["preview_is_valid"] is False

    def test_preview_pipeline_absent_runtime_preflight_fails_closed(self) -> None:
        """An un-run Stage 2 may not ride the success side of the conjunct.

        Mutation caught: restoring the silent-drop conjunct
        (``if runtime_result is not None: is_valid = is_valid and ...``)
        republishes a bare ``true`` for a pipeline nothing ran the runtime
        checks against, and the marker disappears.
        """
        state = _stage1_valid_preview_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=None,
        )

        assert result.success is True
        assert result.validation.is_valid is True
        assert result.data["preview_is_valid"] is False
        assert "runtime_preflight" not in result.data

        markers = [entry for entry in result.data["preview_errors"] if entry.get("error_code") == "runtime_preflight_not_run"]
        assert len(markers) == 1, result.data["preview_errors"]
        assert markers[0]["severity"] == "high"
        assert "did not run" in markers[0]["message"]

        # The authoring check's own report stays true to itself: the marker
        # is a preview-stage entry under ``data``, never an entry on the
        # envelope's ``validation``.
        authoring_codes = [entry.error_code for entry in result.validation.errors]
        assert "runtime_preflight_not_run" not in authoring_codes

    def test_preview_pipeline_runtime_conjunct_three_directions(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """Wired-pass, wired-fail, and absent must stay three distinct verdicts.

        Mutation caught: any future 'unification' that folds the absent case
        into either wired semantics — e.g.
        ``is_valid and (runtime_result is None or runtime_result.is_valid)``
        (absent collapses into pass) or dropping the marker while keeping
        ``is_valid=False`` (absent collapses into wired-fail, indistinguishable
        from a real runtime rejection) — breaks at least one arm loudly.
        """
        state = _stage1_valid_preview_state()
        failing_preflight = _SyncCallRecorder(
            ValidationResult(
                is_valid=False,
                checks=[
                    ValidationCheck(
                        name="path_allowlist",
                        passed=False,
                        detail="Path traversal blocked: sink 'main'",
                        affected_nodes=(),
                        outcome_code=None,
                    )
                ],
                errors=[
                    ValidationError(
                        component_id="main",
                        component_type="sink",
                        message="Path traversal blocked: sink 'main'",
                        suggestion="Write inside the configured data directory.",
                        error_code=None,
                    )
                ],
                readiness=ValidationReadiness(authoring_valid=True, execution_ready=False, completion_ready=False, blockers=[]),
            )
        )

        def preview(runtime_preflight: _SyncCallRecorder | None) -> dict[str, Any]:
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir="/data",
                runtime_preflight=runtime_preflight,
            )
            assert result.success is True
            return result.to_dict()

        wired_pass = preview(passing_runtime_preflight)
        wired_fail = preview(failing_preflight)
        absent = preview(None)

        def marker_codes(envelope: dict[str, Any]) -> list[str]:
            return [
                entry.get("error_code")
                for entry in envelope["data"]["preview_errors"]
                if entry.get("error_code") == "runtime_preflight_not_run"
            ]

        # The preflight is an envelope field, never a nested twin under data.
        assert all("runtime_preflight" not in envelope["data"] for envelope in (wired_pass, wired_fail, absent))

        assert wired_pass["data"]["preview_is_valid"] is True
        assert wired_pass["runtime_preflight"]["is_valid"] is True
        assert marker_codes(wired_pass) == []

        assert wired_fail["data"]["preview_is_valid"] is False
        assert wired_fail["runtime_preflight"]["is_valid"] is False
        assert marker_codes(wired_fail) == []

        assert absent["data"]["preview_is_valid"] is False
        assert "runtime_preflight" not in absent
        assert marker_codes(absent) == ["runtime_preflight_not_run"]

    def test_preview_data_carries_exactly_the_preview_stage_facts(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """``data`` is the preview stage's own facts and the overview — nothing the envelope already carries.

        Mutation caught: re-hoisting the authoring verdict under ``data``
        (``is_valid`` / ``errors`` / ``warnings`` / ``suggestions`` /
        ``semantic_contracts`` / ``graph_repair_suggestions``, or a nested
        ``authoring_validation`` copy) puts byte-twins of the envelope's own
        ``validation`` back on the wire under a second name, and revives the
        ``is_valid`` homonym (the 3-stage conjunct wearing the authoring
        verdict's key). The key set is exact so an addition is a deliberate
        teaching change, not a drift (elspeth-e405ad7cd2 R4).
        """
        state = _stage1_valid_preview_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
        )
        assert result.success is True
        assert frozenset(result.data) == _PREVIEW_DATA_KEYS
        assert "is_valid" not in result.data
        assert "authoring_validation" not in result.data
        assert "errors" not in result.data

        with_structural = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
            structural_preflight=_SyncCallRecorder(_tolerant_result(valid=True)),
        )
        assert with_structural.success is True
        assert frozenset(with_structural.data) == _PREVIEW_DATA_KEYS | {"structural_preview"}

    def test_preview_errors_is_only_the_preview_stage(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """``preview_errors`` carries entries the preview stage itself mints, never authoring errors.

        Mutation caught: seeding ``preview_errors`` from the authoring
        errors (the pre-R4 ``summary_errors = authoring errors + marker``
        shape) makes every authoring error appear twice on the wire —
        once under ``validation.errors`` and once under ``data`` — and
        hides which entries the envelope's ``validation`` does NOT know.
        """
        # An empty pipeline is authoring-invalid: at least one error rides on
        # the envelope's own validation, and none of them may leak into data.
        state = _empty_state()

        absent = execute_tool("preview_pipeline", {}, state, _mock_catalog(), data_dir="/data", runtime_preflight=None)
        assert absent.success is True
        assert absent.validation.is_valid is False
        assert len(absent.validation.errors) >= 1
        assert [entry["error_code"] for entry in absent.data["preview_errors"]] == ["runtime_preflight_not_run"]
        assert absent.data["preview_errors"][0]["severity"] == "high"
        assert "did not run" in absent.data["preview_errors"][0]["message"]
        authoring_codes = {entry.error_code for entry in absent.validation.errors}
        assert "runtime_preflight_not_run" not in authoring_codes

        wired = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
        )
        assert wired.success is True
        assert wired.validation.is_valid is False
        assert list(wired.data["preview_errors"]) == []

    def test_preview_is_valid_keeps_the_authoring_leg_of_the_conjunct(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """Authoring INVALID with the runtime stage wired-and-passing and no blocking proof still yields ``preview_is_valid`` false.

        Mutation caught: ``preview_is_valid = True`` in place of
        ``validation.is_valid`` (dropping the authoring leg of the three-stage
        conjunct). Every other authoring-invalid pin runs with the runtime
        callback absent, where the un-run branch forces the conjunct false
        regardless of the authoring verdict — so only this three-leg state
        (authoring the sole mover) can see the leg go missing
        (elspeth-e405ad7cd2 R4-fix1).
        """
        state = _empty_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
        )
        assert result.success is True
        # The two other legs are green: the runtime verdict rode through and
        # nothing in the source proof blocks.
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.is_valid is True
        assert [d for d in result.data["proof_diagnostics"] if d["severity"] == "blocking"] == []
        assert list(result.data["preview_errors"]) == []
        # Authoring is the only red leg, and it alone must fail the conjunct.
        assert result.validation.is_valid is False
        assert result.data["preview_is_valid"] is False


class TestPreviewPipelineStructuralPreview:
    """The additive ``data["structural_preview"]`` block (elspeth-229e9e8195).

    Four handler branches: no callback → block absent; tolerant green →
    block with empty findings; tolerant red with ``masking_applied=False``
    → "equivalent" framing; tolerant red with ``masking_applied=True`` →
    "provisional" report-don't-blindly-repair framing. ``is_valid`` stays
    the strict conjunct in every branch — the block never joins it.
    """

    def _preview(
        self,
        state: CompositionState,
        *,
        structural_preflight: _SyncCallRecorder | None,
    ) -> dict[str, Any]:
        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=_handoff_strict_preflight(),
            structural_preflight=structural_preflight,
        )
        assert result.success is True
        return result.data

    def test_no_structural_callback_leaves_block_absent(self) -> None:
        data = self._preview(_stage1_valid_preview_state(), structural_preflight=None)
        assert "structural_preview" not in data
        assert data["preview_is_valid"] is False  # strict handoff verdict governs

    def test_tolerant_green_yields_block_with_empty_findings(self) -> None:
        data = self._preview(
            _stage1_valid_preview_state(),
            structural_preflight=_SyncCallRecorder(_tolerant_result(valid=True)),
        )
        block = data["structural_preview"]
        assert block["is_valid"] is True
        assert list(block["failing_checks"]) == []
        assert list(block["errors"]) == []
        # No prompt-bearing node: masking was a no-op, findings are exact.
        assert block["masking_applied"] is False
        assert block["confidence"] == "equivalent"
        # The strict conjunct is untouched by a green tolerant pass.
        assert data["preview_is_valid"] is False

    def test_tolerant_red_without_masking_is_framed_equivalent(self) -> None:
        data = self._preview(
            _stage1_valid_preview_state(),
            structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)),
        )
        block = data["structural_preview"]
        assert block["is_valid"] is False
        assert block["masking_applied"] is False
        assert block["confidence"] == "equivalent"
        assert [check["name"] for check in block["failing_checks"]] == ["plugin_instantiation"]
        assert block["errors"][0]["message"] == "consumer requires ['colour'], producer guarantees (none)"
        # The skipped-after-failure stamp is derived noise and must be dropped.
        assert all(check["outcome_code"] != "validation.skipped_after_failure" for check in block["failing_checks"])
        assert data["preview_is_valid"] is False

    def test_tolerant_red_with_masking_is_framed_provisional(self) -> None:
        """The ticket's false-positive shape: a pending prompt-template ref.

        ``materialize_state_for_authoring`` substitutes placeholder text for
        the pending interpretation ref, so a tolerant red at
        ``plugin_instantiation`` may be an artifact of the placeholder — the
        block must say so and instruct report-not-repair.
        """
        from elspeth.web.interpretation_state import materialize_state_for_authoring

        state = _stage1_valid_preview_state().with_node(_pending_prompt_template_llm_node())
        # Premise check: this state genuinely materializes to a new object.
        assert materialize_state_for_authoring(state) is not state

        data = self._preview(state, structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)))
        block = data["structural_preview"]
        assert block["is_valid"] is False
        assert block["masking_applied"] is True
        assert block["confidence"] == "provisional"
        assert "placeholder" in block["note"]
        assert "Report" in block["note"]
        assert [check["name"] for check in block["failing_checks"]] == ["plugin_instantiation"]
        assert data["preview_is_valid"] is False

    def test_masking_noop_state_is_framed_equivalent_with_prompt_node(self) -> None:
        """A resolved-parts prompt renders identically → identical state object."""
        from dataclasses import replace as dc_replace

        from elspeth.web.interpretation_state import materialize_state_for_authoring

        node = _pending_prompt_template_llm_node()
        resolved_options = dict(node.options)
        resolved_options["interpretation_requirements"] = [
            {
                "id": "req-1",
                "kind": "vague_term",
                "user_term": "cool",
                "status": "resolved",
                "accepted_value": "a 1-10 coolness scale",
            }
        ]
        resolved_options["prompt_template"] = "Rate {{ row.colour }} as a 1-10 coolness scale"
        resolved_node = dc_replace(node, options=resolved_options)
        state = _stage1_valid_preview_state().with_node(resolved_node)
        assert materialize_state_for_authoring(state) is state

        data = self._preview(state, structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)))
        assert data["structural_preview"]["masking_applied"] is False
        assert data["structural_preview"]["confidence"] == "equivalent"


class TestPrevalidatePluginOptions:
    """Direct unit tests for _prevalidate_plugin_options.

    Covers the 6 code paths identified in the architecture review:
    structured rejection (unknown plugin), success, config error with prefix stripping,
    injected_fields merge, MappingProxyType deep-thaw, and ValueError surfacing.
    Also covers the absence-is-evidence contract: missing required fields (like
    path) must produce validation errors, not be papered over by placeholders.
    """

    def test_valid_options_returns_none(self) -> None:
        """Valid config returns None (no pre-validation error)."""
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            {"schema": {"mode": "observed"}},
        )
        assert result is None

    def test_invalid_options_returns_error_string(self) -> None:
        """Missing required field returns a descriptive error string."""
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            {},  # missing required 'schema'
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'passthrough':")

    def test_unknown_transform_plugin_returns_actionable_error(self) -> None:
        """Unregistered transform name surfaces as an actionable error string."""
        result = _prevalidate_plugin_options(
            "transform",
            "this_plugin_does_not_exist",
            {"some": "options"},
        )
        assert result is not None
        assert "this_plugin_does_not_exist" in result
        assert "transform" in result.lower()
        assert "list_transforms" in result

    def test_unknown_source_plugin_returns_actionable_error(self) -> None:
        """Unregistered source name surfaces as an actionable error string."""
        result = _prevalidate_plugin_options(
            "source",
            "no_such_source_plugin",
            {"path": "/data/blobs/test-session/in.csv"},
        )
        assert result is not None
        assert "no_such_source_plugin" in result
        assert "source" in result.lower()
        assert "list_sources" in result

    def test_unknown_sink_plugin_returns_actionable_error(self) -> None:
        """Unregistered sink name surfaces as an actionable error string."""
        result = _prevalidate_plugin_options(
            "sink",
            "no_such_sink_plugin",
            {"path": "/data/outputs/test-session/out.csv"},
        )
        assert result is not None
        assert "no_such_sink_plugin" in result
        assert "sink" in result.lower()
        assert "list_sinks" in result

    def test_injected_fields_satisfy_required_options(self) -> None:
        """Injected fields are merged in for validation only — not stored in state."""
        # csv source requires on_validation_failure + path (injected) plus schema (in options)
        result = _prevalidate_plugin_options(
            "source",
            "csv",
            {"schema": {"mode": "observed"}},
            injected_fields={"on_validation_failure": "discard", "path": "/tmp/test.csv"},
        )
        assert result is None

    def test_frozen_mappingproxy_options_are_thawed(self) -> None:
        """MappingProxyType options from CompositionState are deep-thawed before Pydantic sees them."""
        from types import MappingProxyType

        frozen_options = MappingProxyType({"schema": MappingProxyType({"mode": "observed"})})
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            frozen_options,  # type: ignore[arg-type]
        )
        assert result is None

    def test_config_class_prefix_stripped_from_error(self) -> None:
        """'Invalid configuration for XConfig:' prefix is stripped so the LLM sees only the problem."""
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            {},  # missing required 'schema'
        )
        assert result is not None
        # Internal class name should not appear — LLM gets the validation detail only
        assert "Invalid configuration for PassThroughConfig" not in result
        assert result.startswith("Invalid options for transform 'passthrough':")

    def test_llm_unknown_provider_surfaced_not_swallowed(self) -> None:
        """ValueError from get_config_model (unknown LLM provider) becomes an error, not silent None."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {"provider": "nonexistent_provider", "schema": {"mode": "observed"}},
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'llm':")
        assert "nonexistent_provider" in result

    def test_llm_valid_provider_missing_required_fields_surfaces_errors(self) -> None:
        """Valid provider with missing required fields reports them — not silent None.

        Verifies Phase 2 of LLM dispatch: after provider resolution succeeds,
        the provider-specific Pydantic model validates required fields. Without
        this test, a regression that returns the base LLMConfig (which lacks
        deployment_name/endpoint) instead of AzureOpenAIConfig would be invisible.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {"provider": "azure", "schema": {"mode": "observed"}},
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'llm':")
        # Azure-specific required fields must be reported
        assert "deployment_name" in result
        assert "endpoint" in result
        assert "api_key" in result

    def test_llm_openrouter_missing_required_fields_surfaces_errors(self) -> None:
        """OpenRouter with missing required fields reports provider-specific missing fields."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {"provider": "openrouter", "schema": {"mode": "observed"}},
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'llm':")
        # OpenRouter-specific required fields — model is required (no deployment_name fallback)
        assert "model" in result
        assert "api_key" in result

    def test_llm_secret_ref_does_not_hide_prompt_field_declaration_error(self) -> None:
        """A deferred credential must not suppress unrelated model validation."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "model": "openai/gpt-4o",
                "prompt_template": "Summarise {{ row.text }}",
                "schema": {"mode": "observed"},
            },
        )

        assert result is not None
        assert "required_input_fields is not declared" in result
        assert "api_key" not in result

    def test_llm_openrouter_invalid_model_surfaces_structural_hint_without_raw_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Composer prevalidation rejects unknown OpenRouter models without echoing the value.

        Catalog membership is now enforced via the value-source walker
        (check_config_value_sources), so patch the catalog at the walker's lookup
        site rather than the (removed) construction-time validator's.
        """
        monkeypatch.setattr(
            "elspeth.engine.orchestrator.preflight.get_catalog_values",
            lambda catalog_id: frozenset({"openai/gpt-4o"}),
        )

        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "api_key": "sk-test-key",
                "model": "anthropic/claude-3-opus",
                "prompt_template": "Analyze: {{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
            },
        )
        assert result is not None
        assert result == (
            "Invalid options for transform 'llm': configured value is not in catalog "
            "'openrouter'; pick a valid value via the list_models composer tool"
        )
        assert "anthropic/claude-3-opus" not in result

    def test_llm_secret_ref_still_runs_value_source_validation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Placeholder validation must continue into catalog-backed checks."""
        monkeypatch.setattr(
            "elspeth.engine.orchestrator.preflight.get_catalog_values",
            lambda catalog_id: frozenset({"openai/gpt-4o"}),
        )

        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "model": "anthropic/claude-3-opus",
                "prompt_template": "Analyze: {{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
            },
        )

        assert result == (
            "Invalid options for transform 'llm': configured value is not in catalog "
            "'openrouter'; pick a valid value via the list_models composer tool"
        )
        assert "anthropic/claude-3-opus" not in result

    def test_unreachable_plugin_type_raises_assertion(self) -> None:
        """Passing an invalid plugin_type triggers the unreachable-branch assertion (not silent bypass)."""
        with pytest.raises(AssertionError, match="unexpected plugin_type"):
            _prevalidate_plugin_options(
                "unknown_kind",  # type: ignore[arg-type]
                "csv",
                {},
            )

    def test_absent_source_path_returns_error_not_none(self) -> None:
        """Absence of path is evidence of a missing required field — not a gap to fill with a placeholder.

        Pre-validates csv source options without path. The function must return a descriptive
        error, not None. This guards against regression where callers inject a fake placeholder
        path to suppress the error (violating the data manifesto's 'absence is evidence' rule).
        """
        result = _prevalidate_plugin_options(
            "source",
            "csv",
            {"schema": {"mode": "observed"}},
            injected_fields={"on_validation_failure": "quarantine"},
            # path deliberately absent — caller did not provide it
        )
        assert result is not None
        assert "path" in result

    def test_absent_sink_path_returns_error_not_none(self) -> None:
        """Absence of path for a sink plugin is a validation error, not a placeholder opportunity.

        Pre-validates csv sink options without path. The function must return a descriptive
        error, not None. Regression guard for the symmetric case on sinks.
        """
        result = _prevalidate_plugin_options(
            "sink",
            "csv",
            {},
            # path deliberately absent, no injected_fields
        )
        assert result is not None
        assert "path" in result

    def test_null_source_no_config_model_returns_none(self) -> None:
        """NullSource is registered as None in the source registry — no config validation needed.

        Exercises the ``config_cls is None`` branch in
        ``_prevalidate_plugin_options``. This is distinct from
        UnknownPluginTypeError: 'null' is a known, valid plugin that explicitly has no config
        class (it is a resume-only source with no options).
        """
        result = _prevalidate_plugin_options(
            "source",
            "null",
            {},
        )
        assert result is None

    def test_batch_stats_valid_options(self) -> None:
        """Aggregation plugin (batch_stats) with valid options passes pre-validation.

        batch_stats is an aggregation plugin dispatched as plugin_type="transform".
        This exercises the aggregation-as-transform path in _prevalidate_plugin_options.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "batch_stats",
            {
                "schema": {"mode": "observed"},
                "value_field": "amount",
            },
        )
        assert result is None

    def test_batch_stats_missing_value_field(self) -> None:
        """Aggregation plugin with missing required field returns error."""
        result = _prevalidate_plugin_options(
            "transform",
            "batch_stats",
            {
                "schema": {"mode": "observed"},
                # value_field deliberately absent
            },
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'batch_stats':")
        assert "value_field" in result

    def test_batch_stats_empty_value_field_rejected(self) -> None:
        """batch_stats rejects empty string value_field via field_validator."""
        result = _prevalidate_plugin_options(
            "transform",
            "batch_stats",
            {
                "schema": {"mode": "observed"},
                "value_field": "",
            },
        )
        assert result is not None
        assert "value_field" in result

    def test_upsert_node_aggregation_type_validates_as_transform(self) -> None:
        """upsert_node with node_type='aggregation' routes through _prevalidate_transform.

        Regression guard: the upsert_node guard checks
        ``node_type in ("transform", "aggregation")``. If someone narrowed this
        to ``node_type == "transform"``, aggregation nodes would bypass
        pre-validation. This test exercises the aggregation path end-to-end.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        # Add batch_stats to the mock catalog's transform list
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        # Missing value_field should be caught by pre-validation
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
                # value_field deliberately absent
            },
            state,
            catalog,
        )
        assert result.success is False

    def test_upsert_node_aggregation_valid_options_succeeds(self) -> None:
        """upsert_node with node_type='aggregation' and valid options succeeds."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "value_field": "amount",
                },
            },
            state,
            catalog,
        )
        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.id == "agg1"
        assert node.node_type == "aggregation"
        assert node.plugin == "batch_stats"
        assert deep_thaw(node.options) == {"schema": {"mode": "observed"}, "value_field": "amount"}

    def test_patch_node_options_collector_routes_through_prevalidation(self) -> None:
        """patch_node_options with node_type='collector' pre-validates the patch.

        Regression guard (2026-08-26 systems review): the patch guard checked
        ``node_type in ("transform", "aggregation")``, so patching a collector's
        options skipped plugin prevalidation entirely — invalid options were
        accepted and failed only later at lowering/preflight. Collectors are
        plugin-bearing (batch-transform contract) and take the same path.
        """
        state = CompositionState.from_dict(
            {
                "version": 1,
                "sources": {
                    "source": {
                        "plugin": "csv",
                        "on_success": "pages",
                        "options": {"path": "cases.csv"},
                        "on_validation_failure": "discard",
                    }
                },
                "nodes": [
                    {
                        "id": "page_stitcher",
                        "node_type": "collector",
                        "plugin": "batch_stats",
                        "input": "pages",
                        "on_success": "out",
                        "on_error": None,
                        "options": {"schema": {"mode": "observed"}, "value_field": "amount"},
                        "scope_name": "document_pages",
                        "scope_opener": "explode",
                        "scope_policy": "require_all",
                    }
                ],
                "edges": [],
                "outputs": [{"name": "out", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
                "metadata": {"name": "Collector patch", "description": ""},
            }
        )
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]

        # value_field must be a string; prevalidation must refuse the patch.
        result = execute_tool(
            "patch_node_options",
            {"node_id": "page_stitcher", "patch": {"value_field": 123}},
            state,
            catalog,
        )
        assert result.success is False

        # A valid patch still lands.
        result = execute_tool(
            "patch_node_options",
            {"node_id": "page_stitcher", "patch": {"value_field": "page_size_bytes"}},
            state,
            catalog,
        )
        assert result.success is True

    def test_upsert_node_batch_stats_group_by_keeps_expected_count_open(self) -> None:
        """Grouped rollups may emit one row per group, so cardinality stays unset."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "value_field": "amount",
                    "group_by": "customer_tier",
                },
            },
            state,
            catalog,
        )
        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.expected_output_count is None

    def test_upsert_node_aggregation_rejects_required_input_fields(self) -> None:
        """ADR-013 declared input fields are not valid for batch-aware aggregation nodes."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]

        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "value_field": "amount",
                    "required_input_fields": ["amount"],
                },
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.data is None
        messages = result.validation.errors[0].message
        assert "required_input_fields" in messages
        assert "batch-aware" in messages

    def test_upsert_node_rejects_batch_replicate_as_plain_transform(self) -> None:
        """Batch-only fan-out plugins must use the aggregation path, not row-mode transforms."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_replicate",
                description="Batch replicate aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]

        result = execute_tool(
            "upsert_node",
            {
                "id": "replicate",
                "node_type": "transform",
                "plugin": "batch_replicate",
                "input": "source",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "replications": [
                        {"source_field": "region", "output_field": "region_copy"},
                    ],
                },
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.data is None
        messages = result.validation.errors[0].message
        assert "batch_replicate" in messages
        assert "aggregation" in messages
        assert "output_mode: transform" in messages

    def test_secret_ref_field_passes_prevalidation(self) -> None:
        """Options with secret_ref markers pass prevalidation.

        A secret-ref'd field is provisioned (the user called wire_secret_ref),
        just deferred to execution time. Prevalidation must not reject it.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "azure_content_safety",
            {
                "api_key": {"secret_ref": "AZURE_API_KEY"},
                "endpoint": "https://test.cognitiveservices.azure.com",
                "schema": {"mode": "observed"},
                "fields": "text",
                "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2},
            },
        )
        assert result is None

    def test_secret_ref_field_non_secret_errors_still_reported(self) -> None:
        """Secret-ref'd fields are forgiven but other errors are still reported.

        api_key has a secret_ref (valid), but fields and thresholds are missing
        (real errors). The error message must mention the missing fields but
        NOT api_key.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "azure_content_safety",
            {
                "api_key": {"secret_ref": "AZURE_API_KEY"},
                "endpoint": "https://test.cognitiveservices.azure.com",
                "schema": {"mode": "observed"},
                # fields and thresholds deliberately absent
            },
        )
        assert result is not None
        assert "fields" in result
        assert "thresholds" in result
        assert "api_key" not in result

    def test_secret_ref_in_source_passes_prevalidation(self) -> None:
        """Source options with a secret_ref marker pass prevalidation."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "model": "openai/gpt-4o",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "prompt_template": "Classify: {{ row.text }}",
                "required_input_fields": ["text"],
                "schema": {"mode": "observed"},
            },
        )
        assert result is None

    def test_malformed_secret_ref_marker_still_reports_field_error(self) -> None:
        """Only syntactically valid secret_ref markers are stripped.

        A non-string secret_ref value is malformed and must remain in the
        options payload so the plugin config model reports the field error
        during composer-time validation.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "azure_content_safety",
            {
                "api_key": {"secret_ref": 123},
                "endpoint": "https://test.cognitiveservices.azure.com",
                "schema": {"mode": "observed"},
                "fields": "text",
                "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2},
            },
        )
        assert result is not None
        assert "api_key" in result


class TestInspectSourceTool:
    """``inspect_source`` returns bounded structural facts about a blob."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_orders.csv"
        self.content_bytes = b"order_id,customer,price\nO-1,Alice,49.95\nO-2,Bob,150.00\n"
        self.content_hash_hex = _content_hash(self.content_bytes)
        self.storage_path.write_bytes(self.content_bytes)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.content_bytes),
                    content_hash=self.content_hash_hex,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _set_status(self, status: str) -> None:
        from elspeth.web.sessions.models import blobs_table

        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(status=status))

    def test_returns_csv_facts_for_ready_blob(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is True
        data = result.data
        assert data["source_kind"] == "csv"
        # Data is deep-frozen by ToolResult.__post_init__: lists become tuples,
        # dicts become MappingProxyType. Compare against the frozen forms.
        assert tuple(data["observed_headers"]) == ("order_id", "customer", "price")
        assert dict(data["inferred_types"]) == {
            "order_id": "str",
            "customer": "str",
            "price": "float",
        }
        assert data["sample_row_count"] == 2
        assert data["redacted_identity"]["filename"] == "orders.csv"
        assert data["redacted_identity"]["mime_type"] == "text/csv"
        # Identity must NOT include storage_path
        assert "storage_path" not in data["redacted_identity"]

    def test_returns_url_candidates_when_present(self) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        # Add a second blob with URL content
        url_blob_id = str(uuid4())
        url_path = self.storage_path.parent / f"{url_blob_id}_urls.txt"
        url_bytes = b"https://example.com/api\n"
        url_path.write_bytes(url_bytes)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=url_blob_id,
                    session_id=self.session_id,
                    filename="urls.txt",
                    mime_type="text/plain",
                    size_bytes=len(url_bytes),
                    content_hash=_content_hash(url_bytes),
                    storage_path=str(url_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": url_blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is True
        assert result.data["source_kind"] == "text"
        # url_candidates are redacted to scheme + host (+ port): userinfo and path
        # are dropped because they can carry credentials / reset tokens / PII.
        assert tuple(result.data["url_candidates"]) == ("https://example.com",)
        assert any("compatible HTTP fetch transform" in w for w in result.data["warnings"])
        assert all("web_scrape" not in w for w in result.data["warnings"])

    def test_pending_blob_refused(self) -> None:
        self._set_status("pending")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        # Failure messages live in result.validation.errors[0].message (set by _failure_result).
        assert "pending" in result.validation.errors[0].message.lower()

    def test_missing_blob_returns_failure(self) -> None:
        from uuid import uuid4

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": str(uuid4())},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        assert "not found" in result.validation.errors[0].message.lower()

    def test_without_session_context_returns_failure(self) -> None:
        result = execute_tool(
            "inspect_source",
            {"blob_id": self.blob_id},
            _empty_state(),
            _mock_catalog(),
        )
        assert result.success is False

    def test_missing_blob_id_raises_tool_argument_error(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        from elspeth.web.composer.protocol import ToolArgumentError

        with (
            pytest.raises(
                ToolArgumentError,
                # A missing field is not a JSON-type fault, so the type
                # guidance is not appended (only *_type/json_* causes get it).
                match=(r"^'inspect_source arguments' must be object conforming to InspectSourceArgumentsModel, got ValidationError$"),
            ) as exc_info,
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "inspect_source",
                {},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_inspect_source_does_not_use_unbounded_read_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        large_content = b"order_id,customer,price\n" + (b"O-1,Alice,49.95\n" * 2048)
        self.storage_path.write_bytes(large_content)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.update()
                .where(blobs_table.c.id == self.blob_id)
                .values(size_bytes=len(large_content), content_hash=_content_hash(large_content))
            )

        original_read_bytes = Path.read_bytes

        def _forbid_storage_read_bytes(path: Path) -> bytes:
            if path == self.storage_path:
                raise AssertionError("inspect_source must not slurp the full blob with Path.read_bytes")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", _forbid_storage_read_bytes)

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert result.success is True
        assert result.data["byte_range_inspected"][1] <= 8 * 1024

    def test_hash_mismatch_raises_blob_integrity_error(self) -> None:
        """Tier-1 invariant — corrupted blob must escalate, not return facts."""
        from elspeth.web.blobs.protocol import BlobIntegrityError

        # Tamper with the on-disk bytes so SHA-256 mismatches the stored hash.
        self.storage_path.write_bytes(b"tampered,content\n9,9\n")
        with (
            pytest.raises(BlobIntegrityError),
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

    def test_non_uuid_blob_id_rejected_before_lookup(self) -> None:
        """LLM placeholder identifiers must fail at the blob-id boundary."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": "__missing__"},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert result.success is False
        assert "not a valid UUID" in result.validation.errors[0].message
        assert "upload" in result.validation.errors[0].message
        assert "list_blobs" in result.validation.errors[0].message
        assert "not found" not in result.validation.errors[0].message.lower()


class TestPreviewProofStep:
    """preview_pipeline must surface input-shape diagnostics when session context is supplied."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.csv_blob_id = str(uuid4())
        self.url_blob_id = str(uuid4())
        now = datetime.now(UTC)

        # CSV blob with three observed columns
        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.csv_storage_path = storage_dir / f"{self.csv_blob_id}_orders.csv"
        csv_content = b"order_id,customer,price\nO-1,Alice,49.95\nO-2,Bob,150.00\n"
        self.csv_storage_path.write_bytes(csv_content)

        # Text blob with a single URL
        self.url_storage_path = storage_dir / f"{self.url_blob_id}_url.txt"
        url_content = b"https://example.com/data.json\n"
        self.url_storage_path.write_bytes(url_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.csv_blob_id,
                    session_id=self.session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                    storage_path=str(self.csv_storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.url_blob_id,
                    session_id=self.session_id,
                    filename="url.txt",
                    mime_type="text/plain",
                    size_bytes=len(url_content),
                    content_hash=_content_hash(url_content),
                    storage_path=str(self.url_storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _state_with_csv_source(
        self,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
        schema_mode: str = "fixed",
        fields: tuple[object, ...] = (),
        on_validation_failure: str = "discard",
        columns: tuple[str, ...] | None = None,
    ):
        """Build a state with a CSV blob source via the composer tool API."""
        schema: dict[str, object] = {"mode": schema_mode}
        if fields:
            schema["fields"] = list(fields)

        state = _empty_state()
        catalog = _mock_catalog()
        # Wire source via set_source_from_blob — this is the canonical way to
        # produce a state with source.options.blob_ref set.
        source_options: dict[str, object] = {"schema": schema}
        if columns is not None:
            source_options["columns"] = list(columns)
        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.csv_blob_id,
                "on_success": "rows",
                "on_validation_failure": on_validation_failure,
                "options": source_options,
            },
            state,
            catalog,
            data_dir=str(self.data_dir),
            session_operation_authority=session_operation_authority,
            session_operation_context=session_operation_context,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert result.success, result.data
        state = result.updated_state

        result = execute_tool(
            "set_output",
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {
                    "path": "outputs/out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success, result.data
        return result.updated_state

    def _state_with_text_url_source(
        self,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
        fetch_plugin: str | None,
    ):
        """Build a state with a text URL blob source and optional HTTP fetcher."""
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.url_blob_id,
                "on_success": "url_rows" if fetch_plugin is not None else "content",
                "on_validation_failure": "discard",
                "options": {
                    "column": "url",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                },
            },
            state,
            catalog,
            data_dir=str(self.data_dir),
            session_operation_authority=session_operation_authority,
            session_operation_context=session_operation_context,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert result.success, result.data
        state = result.updated_state

        if fetch_plugin is not None:
            options = (
                {
                    "url_field": "url",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                    "content_field": "content",
                    "fingerprint_field": "content_fingerprint",
                    "format": "text",
                    "text_separator": "\n",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "test",
                        "allowed_hosts": "public_only",
                    },
                }
                if fetch_plugin == "web_scrape"
                else {
                    "url_field": "url",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                    "http": {
                        "abuse_contact": "test@example.com",
                        "fetch_reason": "test",
                        "allowed_hosts": "public_only",
                    },
                }
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "fetch",
                    "node_type": "transform",
                    "plugin": fetch_plugin,
                    "input": "url_rows",
                    "on_success": "content",
                    "on_error": "discard",
                    "options": options,
                },
                state,
                catalog,
            )
            assert result.success, result.data
            state = result.updated_state

        result = execute_tool(
            "set_output",
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {
                    "path": "outputs/out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success, result.data
        return result.updated_state

    # -- Empty / no-op cases ------------------------------------------------

    def test_proof_empty_when_no_blob_source(self) -> None:
        """Path-based source has no blob to inspect — diagnostics empty."""
        # Build a state via set_pipeline using a path-based source (no blob_ref)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            args = _valid_pipeline_args()
            args["source"]["on_validation_failure"] = "discard"
            result = execute_tool("set_pipeline", args, _empty_state(), _mock_catalog())
            assert result.success, result.data

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            assert result.success is True
            assert result.data["proof_diagnostics"] == ()

    def test_proof_unavailable_when_no_session_context(self) -> None:
        """A claimed blob without authenticated context is not a performed proof."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            )
            result = execute_tool("preview_pipeline", {}, state, _mock_catalog())
            assert result.success is True
            assert result.data["preview_is_valid"] is False
            assert [diagnostic["code"] for diagnostic in result.data["proof_diagnostics"]] == ["source_blob_unavailable"]

    def test_proof_inspects_named_sources_beyond_compatibility_source(self) -> None:
        """A non-first named source must not bypass proof diagnostics."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            ).with_named_source(
                "url_source",
                SourceSpec(
                    plugin="text",
                    on_success="content",
                    options={
                        "blob_ref": self.url_blob_id,
                        "column": "url",
                        "schema": {"mode": "fixed", "fields": ["url: str"]},
                    },
                    on_validation_failure="discard",
                ),
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

            diagnostics = result.data["proof_diagnostics"]
            matching = [d for d in diagnostics if d["code"] == "text_source_url_without_web_scrape"]
            assert matching
            assert matching[0]["evidence_locator"]["source_name"] == "url_source"

    def test_unrelated_branch_fetcher_does_not_suppress_text_url_blocker(self) -> None:
        """Fetcher membership is necessary but must be downstream of this source."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            ).with_named_source(
                "url_source",
                SourceSpec(
                    plugin="text",
                    on_success="url_content",
                    options={
                        "blob_ref": self.url_blob_id,
                        "column": "url",
                        "schema": {"mode": "fixed", "fields": ["url: str"]},
                    },
                    on_validation_failure="discard",
                ),
            )
            state = state.with_node(
                NodeSpec(
                    id="unrelated_fetch",
                    node_type="transform",
                    plugin="web_scrape",
                    input="rows",
                    on_success="unrelated_content",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

            matching = [
                diagnostic for diagnostic in result.data["proof_diagnostics"] if diagnostic["code"] == "text_source_url_without_web_scrape"
            ]
            assert matching
            assert matching[0]["evidence_locator"]["source_name"] == "url_source"

    def test_same_blob_sources_share_one_metadata_and_verified_content_read(self) -> None:
        """Composer preview evaluates each alias without re-reading its blob."""
        from elspeth.web.composer.tools import generation as generation_module

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            base = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            )
            source = base.sources["source"]
            gate = NodeSpec(
                id="orders_gate",
                node_type="gate",
                plugin=None,
                input="order_rows",
                on_success=None,
                on_error=None,
                options={},
                condition="row['price'] > 100",
                routes={"true": "high_value", "false": "standard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
            state = replace(
                base,
                sources={
                    "orders": replace(source, on_success="order_rows"),
                    "refunds": replace(source, on_success="refund_rows"),
                },
                nodes=(
                    gate,
                    replace(gate, id="refunds_gate", input="refund_rows"),
                ),
                outputs=(
                    OutputSpec(
                        name="high_value",
                        plugin="json",
                        options={
                            "path": "outputs/high.json",
                            "schema": {"mode": "observed"},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        on_write_failure="discard",
                    ),
                    OutputSpec(
                        name="standard",
                        plugin="json",
                        options={
                            "path": "outputs/standard.json",
                            "schema": {"mode": "observed"},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        on_write_failure="discard",
                    ),
                ),
            )
            original_locked_read = generation_module._locked_read_ready_blob
            original_read_bytes = Path.read_bytes
            metadata_reads: list[str] = []
            content_reads: list[Path] = []

            def counted_locked_read(
                engine: Any,
                session_id: str,
                blob_id: str,
                *,
                data_dir: str,
                session_operation_authority: SQLiteLocalSessionOperationAuthority,
                session_operation_context: SessionOperationContext,
            ) -> Any:
                metadata_reads.append(blob_id)
                return original_locked_read(
                    engine,
                    session_id,
                    blob_id,
                    data_dir=data_dir,
                    session_operation_authority=session_operation_authority,
                    session_operation_context=session_operation_context,
                )

            def counted_read_bytes(path: Path) -> bytes:
                content_reads.append(path)
                return original_read_bytes(path)

            with (
                patch.object(generation_module, "_locked_read_ready_blob", side_effect=counted_locked_read),
                patch.object(Path, "read_bytes", counted_read_bytes),
            ):
                result = execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    data_dir=str(self.data_dir),
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    session_engine=self.engine,
                    session_id=self.session_id,
                )

            assert result.validation.is_valid is True
            matching = [
                diagnostic
                for diagnostic in result.data["proof_diagnostics"]
                if diagnostic["code"] == "gate_expression_type_mismatch_against_source_schema"
            ]
            assert [
                (
                    diagnostic["evidence_locator"]["source_name"],
                    diagnostic["evidence_locator"]["node_id"],
                )
                for diagnostic in matching
            ] == [("orders", "orders_gate"), ("refunds", "refunds_gate")]
            assert metadata_reads == [self.csv_blob_id]
            assert content_reads == [self.csv_storage_path]

    @pytest.mark.parametrize("status", ["pending", "error"])
    def test_non_ready_blob_metadata_is_unavailable_before_any_path_or_integrity_access(
        self,
        status: str,
    ) -> None:
        """Lifecycle status is checked before touching concurrent blob bytes."""
        from sqlalchemy import update

        from elspeth.web.composer.tools import blobs as blobs_module
        from elspeth.web.composer.tools import generation as generation_module

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            )
        with self.engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == self.csv_blob_id).values(status=status, content_hash=None))

        with (
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
            patch.object(
                Path,
                "exists",
                side_effect=AssertionError("non-ready proof blob must not access its backing path"),
            ) as exists,
            patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("non-ready proof blob must not read bytes"),
            ) as read_bytes,
            patch.object(
                blobs_module,
                "_verify_blob_content_integrity",
                side_effect=AssertionError("non-ready proof blob must not verify content"),
            ) as verify,
        ):
            proof = generation_module.compute_proof_diagnostics(
                state,
                context=ComposerProofContext(self.engine, self.session_id, str(self.data_dir), blob_operation_context, blob_authority),
            )

        assert type(proof) is ProofUnavailable
        assert [diagnostic["code"] for diagnostic in proof.diagnostics] == ["source_blob_unavailable"]
        exists.assert_not_called()
        read_bytes.assert_not_called()
        verify.assert_not_called()

    # -- csv_fixed_schema_omits_observed_columns ----------------------------

    def test_fixed_csv_omits_columns_with_discard_blocks(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                schema_mode="fixed",
                fields=("order_id: str",),
                on_validation_failure="discard",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            diagnostics = result.data["proof_diagnostics"]
            codes = [d["code"] for d in diagnostics]
            assert "csv_fixed_schema_omits_observed_columns" in codes
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert blocking, "expected a blocking diagnostic for omitted observed columns"
            # is_valid is forced False by the blocking proof diagnostic.
            assert result.data["preview_is_valid"] is False

    def test_fixed_csv_with_all_columns_does_not_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                schema_mode="fixed",
                fields=("order_id: str", "customer: str", "price: float"),
                on_validation_failure="discard",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_fixed_schema_omits_observed_columns" not in codes

    def test_fixed_csv_with_structured_fields_does_not_crash_or_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                schema_mode="fixed",
                fields=(
                    {"name": "order_id", "field_type": "str"},
                    {"name": "customer", "field_type": "str"},
                    {"name": "price", "field_type": "float"},
                ),
                on_validation_failure="discard",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            assert result.success is True
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
        assert "csv_fixed_schema_omits_observed_columns" not in codes

    def test_flexible_csv_does_not_block(self) -> None:
        """Flexible mode accepts extra columns by design."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="flexible", fields=("order_id: str",)
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_fixed_schema_omits_observed_columns" not in codes

    def test_fixed_csv_with_routed_failures_does_not_block(self) -> None:
        """on_validation_failure routes to a sink → not silent discard, not blocking."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="fixed",
                fields=("order_id: str",),
                on_validation_failure="quarantine_sink",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_fixed_schema_omits_observed_columns" not in codes

    # -- text_source_url_without_web_scrape ---------------------------------

    def test_text_url_without_web_scrape_blocks(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin=None
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            diagnostics = result.data["proof_diagnostics"]
            codes = [d["code"] for d in diagnostics]
            assert "text_source_url_without_web_scrape" in codes
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert blocking
            assert result.data["preview_is_valid"] is False

    def test_text_url_with_web_scrape_does_not_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin="web_scrape"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "text_source_url_without_web_scrape" not in codes

    def test_text_url_with_blob_fetch_does_not_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin="blob_fetch"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "text_source_url_without_web_scrape" not in codes

    # -- inspection warnings surfaced as info -------------------------------

    def test_inspection_warnings_surfaced_as_info(self) -> None:
        """The text source's web_scrape warning is mirrored in proof_diagnostics as info."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin="web_scrape"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            diagnostics = result.data["proof_diagnostics"]
            info = [d for d in diagnostics if d["severity"] == "info"]
            # web_scrape warning from inspection should be mirrored — as info, not blocking
            assert any(d["code"] == "source_inspection_warning" for d in info)

    # -- csv_duplicate_headers (promoted to blocking) -----------------------
    # Duplicate CSV headers cause silent column collapse in csv.DictReader
    # (last-write-wins) and similar libraries, fabricating a single column
    # from multiple source columns. That is a Tier-1 audit-integrity
    # violation and must force the repair loop, not pass through as
    # advisory info.

    def _replace_csv_blob_with_duplicate_headers(self, *, headerless: bool = False) -> str:
        """Overwrite the seeded CSV blob's bytes + content_hash so it has
        duplicate headers. Must update content_hash to match the new bytes
        or the proof step's BlobIntegrityError check will fire instead.
        """
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        sentinel = "ELSPETH_DUPLICATE_HEADER_SENTINEL_7F3A"
        if headerless:
            new_bytes = (f"O-1,{sentinel},{sentinel},49.95\nO-2,Bob,Jones,150.00\n").encode()
        else:
            new_bytes = (f"order_id,{sentinel},{sentinel},price\nO-1,Alice,Smith,49.95\nO-2,Bob,Jones,150.00\n").encode()
        self.csv_storage_path.write_bytes(new_bytes)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(new_bytes),
                    content_hash=_content_hash(new_bytes),
                )
            )
        return sentinel

    def test_csv_duplicate_headers_blocks(self) -> None:
        """Duplicate CSV headers must surface as a blocking proof diagnostic."""
        sentinel = self._replace_csv_blob_with_duplicate_headers()
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            diagnostics = result.data["proof_diagnostics"]
            codes = [d["code"] for d in diagnostics]
            assert "csv_duplicate_headers" in codes, diagnostics
            # Severity is blocking, not info.
            dup = next(d for d in diagnostics if d["code"] == "csv_duplicate_headers")
            assert dup["severity"] == "blocking", dup
            # Must carry an actionable suggested_repair string (not None) so the
            # forced-repair loop has a concrete remedy to relay to the LLM.
            assert isinstance(dup["suggested_repair"], str) and dup["suggested_repair"], dup
            model_visible_payload = json.dumps(deep_thaw(diagnostics), sort_keys=True)
            assert sentinel not in model_visible_payload
            assert "1 duplicate header value class(es)" in dup["message"]
            assert "2 duplicate column position(s)" in dup["message"]
            assert dup["evidence_locator"]["observed_header_count"] == 4
            assert dup["evidence_locator"]["duplicate_header_class_count"] == 1
            assert dup["evidence_locator"]["duplicate_header_column_count"] == 2
            assert tuple(dup["evidence_locator"]["duplicate_header_positions"]) == (2, 3)
            assert dup["evidence_locator"]["header_values_redacted"] is True
            repair = dup["suggested_repair"]
            assert "field_mapping" not in repair
            assert "on_validation_failure" not in repair
            assert "quarantine" not in repair
            assert "correct" in repair
            assert "re-upload" in repair
            assert "headerless" in repair
            assert "explicit unique `columns`" in repair
            # is_valid is forced False by the blocking proof diagnostic.
            assert result.data["preview_is_valid"] is False

    def test_explicit_unique_columns_clear_duplicate_warning_for_headerless_input(self) -> None:
        self._replace_csv_blob_with_duplicate_headers(headerless=True)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="observed",
                columns=("order_id", "given_name", "family_name", "price"),
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_duplicate_headers" not in codes

    def test_preview_redacts_regressed_duplicate_warning_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Generation must not trust even a regressed source warning string."""
        import elspeth.web.composer.tools.generation as generation_module
        from elspeth.web.composer.source_inspection import SourceInspectionFacts

        raw_warning_sentinel = "RAW_GENERATION_SENTINEL_91D2"
        raw_facts = SourceInspectionFacts(
            source_kind="csv",
            redacted_identity={"filename": "orders.csv", "mime_type": "text/csv", "byte_size": "1"},
            byte_range_inspected=(0, 1),
            sample_row_count=1,
            observed_headers=("order_id", "repeated", "repeated", "price"),
            inferred_types=None,
            url_candidates=(),
            warnings=(f"csv_duplicate_headers: {raw_warning_sentinel}",),
        )
        monkeypatch.setattr(generation_module, "inspect_csv_source_content", lambda **_kwargs: raw_facts)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            serialized = json.dumps(deep_thaw(diagnostics), sort_keys=True)
            duplicate = next(d for d in diagnostics if d["code"] == "csv_duplicate_headers")
            assert raw_warning_sentinel not in serialized
            assert duplicate["severity"] == "blocking"
            assert duplicate["evidence_locator"]["duplicate_header_class_count"] == 1
            assert duplicate["evidence_locator"]["duplicate_header_column_count"] == 2
            assert tuple(duplicate["evidence_locator"]["duplicate_header_positions"]) == (2, 3)
            assert duplicate["evidence_locator"]["header_values_redacted"] is True
            assert "re-upload" in duplicate["suggested_repair"]

    def test_csv_without_duplicate_headers_does_not_block(self) -> None:
        """Clean headers must not produce a csv_duplicate_headers diagnostic."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_duplicate_headers" not in codes

    def test_observed_csv_numeric_gate_type_mismatch_blocks(self) -> None:
        """Observed CSV leaves values stringy; numeric gate predicates must
        fail preview before runtime hits ExpressionEvaluationError.
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "price_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "condition": "row['price'] >= 100",
                    "routes": {"true": "out", "false": "out"},
                    "options": {},
                },
                state,
                _mock_catalog(),
            )
            assert result.success, result.data

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "gate_expression_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "price_gate"
            assert mismatch[0]["evidence_locator"]["field"] == "price"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_string_amplification_gate_blocks_without_evaluating(self, monkeypatch) -> None:
        """A Mult over an observed (string-typed) field must be diagnosed
        BEFORE evaluation, never evaluated at preview (THREAT-001).

        ``row['price'] * 100000`` on observed CSV is str-repetition: sampling
        would allocate the amplified string per sampled row inside the web
        process. The amplification diagnostic replaces evaluation entirely.
        """
        from elspeth.contracts import PipelineRow
        from elspeth.core.expression_parser import ExpressionParser

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "amp_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "condition": "row['price'] * 100000",
                    "routes": {"true": "out", "false": "out"},
                    "options": {},
                },
                state,
                _mock_catalog(),
            )
            assert result.success, result.data

            evaluated_expressions: list[str] = []
            original_evaluate = ExpressionParser.evaluate

            def counting_evaluate(self: ExpressionParser, context: dict[str, Any] | PipelineRow) -> Any:
                evaluated_expressions.append(repr(self))
                return original_evaluate(self, context)

            monkeypatch.setattr(ExpressionParser, "evaluate", counting_evaluate)

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            amplification = [d for d in diagnostics if d["code"] == "gate_expression_unbounded_string_amplification"]
            assert amplification, diagnostics
            assert amplification[0]["severity"] == "blocking"
            assert amplification[0]["evidence_locator"]["node_id"] == "amp_gate"
            assert result.data["preview_is_valid"] is False
            # The mechanism, not just the symptom: the amplifying condition was
            # never evaluated against sampled rows.
            assert not any("100000" in expr for expr in evaluated_expressions), evaluated_expressions
            # And the new branch did not misfile the finding as a type mismatch.
            mismatch = [d for d in diagnostics if d["code"] == "gate_expression_type_mismatch_against_source_schema"]
            assert not mismatch, mismatch

    def test_observed_csv_memory_error_yields_resource_code(self, monkeypatch) -> None:
        """MemoryError during sampled-row evaluation gets its own resource
        diagnostic, not the schema-typing repair (belt-and-braces arm)."""
        from elspeth.contracts import PipelineRow
        from elspeth.core.expression_parser import ExpressionParser

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "oom_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "condition": "row['price'] >= 100",
                    "routes": {"true": "out", "false": "out"},
                    "options": {},
                },
                state,
                _mock_catalog(),
            )
            assert result.success, result.data

            def exploding_evaluate(self: ExpressionParser, context: dict[str, Any] | PipelineRow) -> Any:
                raise MemoryError("simulated allocation failure")

            monkeypatch.setattr(ExpressionParser, "evaluate", exploding_evaluate)

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            resource = [d for d in diagnostics if d["code"] == "gate_expression_preview_memory_exhaustion"]
            assert resource, diagnostics
            assert resource[0]["severity"] == "blocking"
            assert resource[0]["evidence_locator"]["node_id"] == "oom_gate"
            # A resource event must not receive the schema-typing repair.
            mismatch = [d for d in diagnostics if d["code"] == "gate_expression_type_mismatch_against_source_schema"]
            assert not mismatch, mismatch

    def test_observed_csv_batch_stats_string_value_field_blocks_through_transform(self) -> None:
        """Observed CSV strings must not reach numeric batch_stats at runtime.

        This mirrors the hard-mode p2_t2_edge failure: a string-ish field
        survives an observed-schema value_transform and batch_stats rejects it
        only when the aggregation executes.
        """
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"respondent_id,community,financial_barrier\nR-1,Community-A,yes\nR-2,Community-B,no\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    NodeSpec(
                        id="classify",
                        node_type="transform",
                        plugin="value_transform",
                        input="rows",
                        on_success="classified_rows",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "operations": [{"target": "financial_only", "expression": "row['financial_barrier']"}],
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(
                    NodeSpec(
                        id="summarize",
                        node_type="aggregation",
                        plugin="batch_stats",
                        input="classified_rows",
                        on_success="out",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "value_field": "financial_barrier",
                            "group_by": "community",
                            "compute_mean": True,
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "financial_barrier"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            assert result.data["preview_is_valid"] is False

    def test_observed_named_csv_batch_stats_string_value_field_blocks_through_transform(self) -> None:
        """Named CSV sources must participate in source-field proof walk-back."""
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"respondent_id,community,financial_barrier\nR-1,Community-A,yes\nR-2,Community-B,no\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            base_state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            named_csv_source = SourceSpec(
                plugin="csv",
                on_success="survey_rows",
                options={
                    "blob_ref": self.csv_blob_id,
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            )
            state = (
                base_state.without_source()
                .with_named_source("survey_csv", named_csv_source)
                .with_node(
                    NodeSpec(
                        id="classify",
                        node_type="transform",
                        plugin="value_transform",
                        input="survey_rows",
                        on_success="classified_rows",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "operations": [{"target": "financial_only", "expression": "row['financial_barrier']"}],
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(
                    NodeSpec(
                        id="summarize",
                        node_type="aggregation",
                        plugin="batch_stats",
                        input="classified_rows",
                        on_success="out",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "value_field": "financial_barrier",
                            "group_by": "community",
                            "compute_mean": True,
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "financial_barrier"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_numeric_aggregation_does_not_block_after_field_overwrite(self) -> None:
        """The proof step abstains once an upstream transform overwrites the field."""
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"respondent_id,community,financial_barrier\nR-1,Community-A,yes\nR-2,Community-B,no\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    NodeSpec(
                        id="coerce_flag",
                        node_type="transform",
                        plugin="value_transform",
                        input="rows",
                        on_success="coerced_rows",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "operations": [{"target": "financial_barrier", "expression": "1"}],
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(
                    NodeSpec(
                        id="summarize",
                        node_type="aggregation",
                        plugin="batch_stats",
                        input="coerced_rows",
                        on_success="out",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "value_field": "financial_barrier",
                            "group_by": "community",
                            "compute_mean": True,
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "aggregation_numeric_value_field_type_mismatch_against_source_schema" not in codes

    # -- the same numeric proof for a COLLECTOR-hosted batch plugin (elspeth-1016a47e8f) --

    def _batch_barrier_behind_expand_opener(
        self,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
        barrier_node_type: str,
        plugin: str = "batch_stats",
        options: dict[str, Any] | None = None,
    ):
        """csv(observed) -> pdf_rasterize (scope opener) -> batch barrier -> out.

        The two barrier kinds differ in EXACTLY ONE field, `node_type` (a
        collector additionally carries the scope binding it cannot legally omit).
        Both compositions validate clean, so any difference in the emitted
        diagnostics is caused by the barrier kind and nothing else.
        """
        barrier_options: dict[str, Any] = (
            options
            if options is not None
            else {
                "schema": {"mode": "observed"},
                "value_field": "price",
                "compute_mean": True,
            }
        )
        scope_binding = (
            {"scope_name": "doc_pages", "scope_opener": "explode", "scope_policy": "require_all"}
            if barrier_node_type == "collector"
            else {}
        )
        return (
            self._state_with_csv_source(
                session_operation_authority=session_operation_authority,
                session_operation_context=session_operation_context,
                schema_mode="observed",
            )
            .with_node(
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="pdf_rasterize",
                    input="rows",
                    on_success="pages",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="summarize",
                    node_type=barrier_node_type,  # type: ignore[arg-type]
                    plugin=plugin,
                    input="pages",
                    on_success="out",
                    on_error="discard",
                    options=barrier_options,
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    **scope_binding,
                )
            )
        )

    def _proof_codes(
        self,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
        state: CompositionState,
    ) -> list[str]:
        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
            data_dir=str(self.data_dir),
            session_operation_authority=authority,
            session_operation_context=context,
        )
        return result.data["proof_diagnostics"]

    @pytest.mark.parametrize("barrier_node_type", ["aggregation", "collector"])
    def test_observed_csv_numeric_value_field_blocks_for_every_batch_barrier_kind(self, barrier_node_type: str) -> None:
        """The numeric proof must not vanish when the plugin is hosted as a collector.

        Asserting the CODE is load-bearing, not stylistic: `is_valid` is False
        in BOTH arms of this harness anyway (`runtime_preflight_not_run` is a
        preview artefact), so a test asserting `is_valid` would pass against the
        defect. Before the fix the collector arm emitted ZERO diagnostics while
        the aggregation arm emitted this one — a blocking export diagnostic
        silently lost to a `node_type` gate (archived issue elspeth-1016a47e8f).
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            diagnostics = self._proof_codes(
                authority,
                context,
                self._batch_barrier_behind_expand_opener(
                    session_operation_authority=authority, session_operation_context=context, barrier_node_type=barrier_node_type
                ),
            )

            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, [d["code"] for d in diagnostics]
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "price"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            # The detector records the REAL node kind so the preflight can label the
            # blocker without guessing it back out of the (historically named) code.
            assert mismatch[0]["evidence_locator"]["node_type"] == barrier_node_type
            # Kind-accurate prose: a collector must not be told to fix a node it does
            # not have, and only a collector is told about its scope.
            assert mismatch[0]["message"].startswith(f"{barrier_node_type.capitalize()} 'summarize'")
            assert f"upstream of the {barrier_node_type}" in mismatch[0]["suggested_repair"]
            assert ("EXPAND scope" in mismatch[0]["suggested_repair"]) is (barrier_node_type == "collector")

    def test_collector_hosting_a_non_numeric_batch_plugin_emits_no_numeric_proof(self) -> None:
        """NEGATIVE control: the test above must not pass merely because a
        collector produces diagnostics at all. `batch_top_k` is batch-aware and
        collector-hostable but is NOT a numeric value_field plugin — it is the
        very plugin this diagnostic's repair text recommends instead — so it
        must stay silent on the identical topology.
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            diagnostics = self._proof_codes(
                authority,
                context,
                self._batch_barrier_behind_expand_opener(
                    session_operation_authority=authority,
                    session_operation_context=context,
                    barrier_node_type="collector",
                    plugin="batch_top_k",
                    options={"schema": {"mode": "observed"}, "field": "customer"},
                ),
            )

            assert "aggregation_numeric_value_field_type_mismatch_against_source_schema" not in [d["code"] for d in diagnostics]

    def test_batch_rank_numeric_value_field_blocks_behind_observed_csv(self) -> None:
        """batch_rank ranks a numeric value_field: an observed CSV string there fails every batch, so the proof fires."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            diagnostics = self._proof_codes(
                authority,
                context,
                self._batch_barrier_behind_expand_opener(
                    session_operation_authority=authority,
                    session_operation_context=context,
                    barrier_node_type="aggregation",
                    plugin="batch_rank",
                    options={"schema": {"mode": "observed"}, "value_field": "price"},
                ),
            )

            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, [d["code"] for d in diagnostics]
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "price"

    # -- declared-input-type mismatch against observed CSV (elspeth-e6e552ce34) --

    @staticmethod
    def _plain_transform(
        node_id: str,
        *,
        plugin: str,
        input_connection: str,
        on_success: str,
        options: dict[str, object],
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin=plugin,
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def test_observed_csv_typed_transform_input_declaration_blocks(self) -> None:
        """A concrete non-str input declaration on a directly-fed transform blocks."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: float", "customer: str"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "declared_input_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert len(mismatch) == 1
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "tidy"
            assert mismatch[0]["evidence_locator"]["field"] == "price"
            assert mismatch[0]["evidence_locator"]["declared_type"] == "float"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_declared_input_proof_is_request_bounded(self) -> None:
        """Wide schemas stop at a request-wide work/result bound and fail closed."""
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        field_names = tuple(f"field_{index}" for index in range(300))
        csv_content = (",".join(field_names) + "\n" + ",".join("1" for _ in field_names) + "\n").encode()
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(size_bytes=len(csv_content), content_hash=_content_hash(csv_content))
            )

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "wide_consumer",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": [f"{name}: int" for name in field_names]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

        diagnostics = result.data["proof_diagnostics"]
        mismatches = [item for item in diagnostics if item["code"] == "declared_input_type_mismatch_against_source_schema"]
        budget_failures = [item for item in diagnostics if item["code"] == "source_inspection_failed"]
        assert len(mismatches) == 16
        assert len(budget_failures) == 1
        assert budget_failures[0]["evidence_locator"]["max_declared_input_type_checks"] == 256
        assert budget_failures[0]["evidence_locator"]["max_declared_input_type_diagnostics"] == 16
        assert result.data["preview_is_valid"] is False

        from elspeth.web.composer.discovery_response import admit_discovery_result

        admitted = admit_discovery_result("preview_pipeline", result).to_dict()
        admitted_budget_failures = [item for item in admitted["data"]["proof_diagnostics"] if item["code"] == "source_inspection_failed"]
        assert admitted_budget_failures[0]["evidence_locator"] == budget_failures[0]["evidence_locator"]

    def test_observed_csv_str_input_declaration_does_not_block(self) -> None:
        """str/any declarations match what an observed CSV actually delivers."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: str", "customer: any"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "declared_input_type_mismatch_against_source_schema" not in codes

    def test_observed_csv_fork_llm_coalesce_typed_declaration_blocks(self) -> None:
        """The elspeth-e6e552ce34 incident shape: fork → llm branches → coalesce →
        field_mapper declaring `id: int` over an observed CSV whose values are str.

        preview_pipeline passed twice on this exact topology in the live
        incident (session 94bdae4f, run cc2279a3) and every row quarantined at
        the field_mapper's input preflight. The walk must reach through the
        pass-through llm nodes and the union coalesce.
        """
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"id,question\n1,What causes the seasons on Earth?\n2,Why is the sky blue?\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        def _branch_llm(node_id: str, branch: str, response_field: str) -> NodeSpec:
            options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
            options["response_field"] = response_field
            return self._plain_transform(
                node_id,
                plugin="llm",
                input_connection=branch,
                on_success=f"answered_{branch}",
                options=options,
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    NodeSpec(
                        id="fan_out",
                        node_type="gate",
                        plugin=None,
                        input="rows",
                        on_success=None,
                        on_error=None,
                        options={},
                        condition="True",
                        routes={"true": "fork", "false": "fork"},
                        fork_to=("branch_a", "branch_b"),
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(_branch_llm("llm_a", "branch_a", "answer_a"))
                .with_node(_branch_llm("llm_b", "branch_b", "answer_b"))
                .with_node(
                    NodeSpec(
                        id="merge_branches",
                        node_type="coalesce",
                        plugin=None,
                        input="answered_branch_a",
                        on_success="merged_rows",
                        on_error=None,
                        options={},
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches={"branch_a": "answered_branch_a", "branch_b": "answered_branch_b"},
                        policy="require_all",
                        merge="union",
                    )
                )
                .with_node(
                    self._plain_transform(
                        "tidy_columns",
                        plugin="field_mapper",
                        input_connection="merged_rows",
                        on_success="out",
                        options={
                            "schema": {
                                "mode": "flexible",
                                "fields": ["id: int", "question: str", "answer_a: str", "answer_b: str"],
                                "guaranteed_fields": ["id", "question", "answer_a", "answer_b"],
                            },
                            "mapping": {
                                "id": "id",
                                "question": "question",
                                "answer_a": "answer_a",
                                "answer_b": "answer_b",
                            },
                            "select_only": True,
                        },
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "declared_input_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert len(mismatch) == 1, mismatch
            assert mismatch[0]["evidence_locator"]["node_id"] == "tidy_columns"
            assert mismatch[0]["evidence_locator"]["field"] == "id"
            assert mismatch[0]["evidence_locator"]["declared_type"] == "int"
            assert mismatch[0]["evidence_locator"]["inferred_sample_type"] == "int"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_typed_sink_schema_blocks(self) -> None:
        """The same contradiction one node further on: a fixed sink schema
        declaring `order_id: int` is armed even when every transform is clean."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    self._plain_transform(
                        "forward",
                        plugin="passthrough",
                        input_connection="rows",
                        on_success="typed_out",
                        options={"schema": {"mode": "observed"}},
                    )
                )
                .with_output(
                    OutputSpec(
                        name="typed_out",
                        plugin="json",
                        options={
                            "path": "outputs/typed.json",
                            "schema": {"mode": "fixed", "fields": ["order_id: int", "customer: str", "price: str"]},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        on_write_failure="discard",
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "declared_input_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert len(mismatch) == 1, mismatch
            assert mismatch[0]["evidence_locator"]["output_name"] == "typed_out"
            assert mismatch[0]["evidence_locator"]["field"] == "order_id"
            assert mismatch[0]["evidence_locator"]["declared_type"] == "int"
            assert result.data["preview_is_valid"] is False

    def test_malformed_node_schema_block_abstains_without_crashing(self) -> None:
        """A node whose schema block cannot be parsed is an abstention, not a
        diagnostic: contract-config validation owns reporting it, and the proof
        step must not double-report or escape as a ValueError through
        preview_pipeline (the tool dispatcher only catches ToolArgumentError)."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: not_a_real_type"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "declared_input_type_mismatch_against_source_schema" not in codes

    def test_unparseable_schema_abstention_is_carried_structurally(self) -> None:
        """The abstention is a distinct fact from "declares nothing typed" —
        collapsing both into an empty tuple made the detector's own abstention
        invisible to its tests (lint R6 swallow, review finding)."""
        from elspeth.web.composer.tools.generation import _DeclaredConcreteFields

        unparseable = _DeclaredConcreteFields(fields=(), schema_unparseable=True)
        nothing_declared = _DeclaredConcreteFields(fields=(), schema_unparseable=False)

        assert unparseable != nothing_declared
        assert unparseable.abstains and nothing_declared.abstains

    def test_declared_source_schema_types_do_not_trip_the_observed_arm(self) -> None:
        """A source that declares its types coerces at ingestion — no diagnostic."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="flexible",
                fields=("order_id: str", "customer: str", "price: float"),
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: float"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "declared_input_type_mismatch_against_source_schema" not in codes

    def test_csv_duplicate_headers_registered_as_blocking_code(self) -> None:
        """Registry membership ripples — the constructor would crash if the
        emission site used an unregistered code, so this test pins the
        canonical-vocabulary contract independently of emission."""
        from elspeth.web.composer.tools import _BLOCKING_DIAGNOSTIC_CODES

        assert "csv_duplicate_headers" in _BLOCKING_DIAGNOSTIC_CODES
        assert "csv_source_field_resolution_error" in _BLOCKING_DIAGNOSTIC_CODES
        assert "declared_input_type_mismatch_against_source_schema" in _BLOCKING_DIAGNOSTIC_CODES

    # -- missing/unreadable blob --------------------------------------------

    def test_missing_storage_file_blocks(self) -> None:
        self.csv_storage_path.unlink()
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "source_blob_unavailable" in codes
            assert result.data["preview_is_valid"] is False

    def test_blocking_proof_overrides_authoring_validation(self, passing_runtime_preflight: _SyncCallRecorder) -> None:
        """Authoring is valid but a blocking proof diagnostic still flips ``preview_is_valid``.

        The runtime check is held green so the proof stage is the only stage
        that can move the verdict: with authoring valid on the envelope and
        the preflight passing, a false ``preview_is_valid`` can only be the
        blocking diagnostic (pin 3 of elspeth-e405ad7cd2 R4).
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="fixed",
                fields=("order_id: str",),
                on_validation_failure="discard",
            )
            # Route the source's ``rows`` connection into the sink so the
            # authoring check is genuinely green; the helper's ``out`` sink leaves
            # ``rows`` unconsumed, which would make authoring — not the proof
            # stage — the reason for the verdict below.
            removed = execute_tool("remove_output", {"sink_name": "out"}, state, _mock_catalog())
            assert removed.success, removed.data
            routed = execute_tool(
                "set_output",
                {
                    "sink_name": "rows",
                    "plugin": "json",
                    "options": {
                        "path": "outputs/out.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                },
                removed.updated_state,
                _mock_catalog(),
            )
            assert routed.success, routed.data
            state = routed.updated_state
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
                runtime_preflight=passing_runtime_preflight,
            )
            assert result.validation.is_valid is True
            assert result.runtime_preflight is not None
            assert result.runtime_preflight.is_valid is True
            assert any(d["severity"] == "blocking" for d in result.data["proof_diagnostics"])
            assert result.data["preview_is_valid"] is False
            assert list(result.data["preview_errors"]) == []

    # -- Tier-3 persisted-option boundaries: malformed source.options ---------
    # ``source.options`` is composer/operator-authored config re-read from
    # persisted session state — Tier-3 origin. A drifted / hand-edited / stale
    # store can carry a malformed value that the helpers re-validate on read,
    # raising ``ValueError``. ``compute_proof_diagnostics`` is called UNWRAPPED
    # from the preview tool and the dispatcher only catches ``ToolArgumentError``,
    # so an unhandled ``ValueError`` would crash the tool. These pin the boundary
    # at the ``compute_proof_diagnostics`` level: each malformed option must yield
    # a blocking diagnostic and NO exception may escape. The tampered options are
    # injected via ``dataclasses.replace`` to model persisted state that drifted
    # after the set_source_from_blob write-time validation that originally
    # produced it (the "validated once is not validated forever" T3 read-back).

    def _state_with_tampered_source_options(
        self,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
        options: dict[str, object],
    ):
        """Build a valid CSV blob state, then overwrite source.options.

        Models persisted external-origin options that drifted between the
        write-time validation and this read.
        """
        import dataclasses

        state = self._state_with_csv_source(
            session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
        )
        source = _default_source(state)
        assert source is not None
        tampered = dataclasses.replace(source, options=options)
        return state.with_source(tampered)

    def test_proof_malformed_schema_block_yields_blocking_diagnostic(self) -> None:
        """A malformed ``schema`` block (get_raw_schema_config raises) blocks,

        does not crash the tool.
        """
        from elspeth.web.composer.tools import compute_proof_diagnostics

        # schema.mode is not a recognised mode → get_raw_schema_config raises
        # ValueError. blob_ref must survive so the proof step inspects the blob.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_tampered_source_options(
                authority, context, {"blob_ref": self.csv_blob_id, "schema": {"mode": "not_a_real_mode"}}
            )
            proof = compute_proof_diagnostics(
                state,
                context=ComposerProofContext(self.engine, self.session_id, str(self.data_dir), context, authority),
            )
            assert type(proof) is ProofEvaluated
            diagnostics = proof.diagnostics
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert any(d["code"] == "csv_source_field_resolution_error" for d in blocking), diagnostics
            # The schema-block builder's repair text must point at the schema knob,
            # not at header/field_mapping resolution.
            schema_diag = next(d for d in blocking if d["code"] == "csv_source_field_resolution_error")
            assert "schema" in schema_diag["suggested_repair"]
            assert "schema.mode" in schema_diag["suggested_repair"]

    def test_proof_malformed_columns_option_yields_blocking_diagnostic(self) -> None:
        """A malformed ``columns`` option (inspect wrap raises) blocks, no crash."""
        from elspeth.web.composer.tools import compute_proof_diagnostics

        # columns must be a sequence of str; an int member raises ValueError
        # inside _csv_source_columns, surfaced by the inspect-call wrap.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_tampered_source_options(
                authority, context, {"blob_ref": self.csv_blob_id, "columns": ["order_id", 123]}
            )
            proof = compute_proof_diagnostics(
                state,
                context=ComposerProofContext(self.engine, self.session_id, str(self.data_dir), context, authority),
            )
            assert type(proof) is ProofEvaluated
            diagnostics = proof.diagnostics
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert any(d["code"] == "csv_source_field_resolution_error" for d in blocking), diagnostics

    def test_proof_malformed_delimiter_option_yields_blocking_diagnostic(self) -> None:
        """A malformed ``delimiter`` option (inspect wrap raises) blocks, no crash."""
        from elspeth.web.composer.tools import compute_proof_diagnostics

        # delimiter must be a single character; a multi-char string raises
        # ValueError inside _csv_source_delimiter, surfaced by the inspect wrap.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_tampered_source_options(authority, context, {"blob_ref": self.csv_blob_id, "delimiter": "||"})
            proof = compute_proof_diagnostics(
                state,
                context=ComposerProofContext(self.engine, self.session_id, str(self.data_dir), context, authority),
            )
            assert type(proof) is ProofEvaluated
            diagnostics = proof.diagnostics
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert any(d["code"] == "csv_source_field_resolution_error" for d in blocking), diagnostics

    # -- proof step integrity verification -----------------------------------
    # The proof step reads blob bytes through the same Tier-1 invariants as
    # _execute_get_blob_content and _execute_inspect_source: NULL stored
    # content_hash escalates via AuditIntegrityError; SHA-256 mismatch
    # escalates via BlobIntegrityError. Without these the audit trail would
    # accept LLM repair turns driven by unverified bytes.

    def test_proof_raises_blob_integrity_error_on_hash_mismatch(self) -> None:
        """Tampering with on-disk bytes after upload must raise, not soft-fail.

        The proof step reads the blob's bytes; if SHA-256 of the bytes
        does not match the stored content_hash, that's a Tier-1 anomaly
        (filesystem corruption, tampering, or write-path bug) and must
        ESCALATE — not silently let downstream LLM repair turns act on
        garbage.
        """
        from elspeth.web.blobs.protocol import BlobIntegrityError

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            # Tamper with the on-disk bytes after the row was inserted.
            self.csv_storage_path.write_bytes(b"tampered,data\nX,Y\n")
            with pytest.raises(BlobIntegrityError):
                execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    data_dir=str(self.data_dir),
                    session_operation_authority=authority,
                    session_operation_context=context,
                )

    def test_proof_raises_audit_integrity_error_on_null_content_hash(self) -> None:
        """A ``ready`` blob with NULL content_hash is a DB-integrity anomaly.

        Enforced at write time by the ``ck_blobs_ready_hash`` CHECK
        constraint. If the proof step ever observes NULL here, the
        constraint was bypassed (or the database is corrupt). Must
        ESCALATE via AuditIntegrityError; cannot soft-degrade to "no
        diagnostics" because that would let an unverified blob drive
        repair turns.
        """
        from sqlalchemy import update

        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.web.sessions.models import blobs_table

        # Bypass the CHECK constraint by suspending it; sqlite respects
        # ``PRAGMA defer_foreign_keys`` but not check toggles inline,
        # so we drop and recreate without the constraint for the test
        # row. Simpler: directly patch via raw SQL with a workaround
        # — but the cleanest test path is to set status='ready' and
        # NULL the hash explicitly via UPDATE; sqlite will reject the
        # CHECK if defined. Use a raw SQL UPDATE that violates the
        # check guard if present, else falls through; the row's
        # presence on read with NULL hash is what we need.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            with self.engine.begin() as conn:
                # Disable the row-level check by toggling pragma; sqlite
                # 3.x respects this for the connection. Then null the hash.
                conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
                conn.execute(update(blobs_table).where(blobs_table.c.id == self.csv_blob_id).values(content_hash=None))
                conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

            with pytest.raises(AuditIntegrityError, match="NULL content_hash"):
                execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    data_dir=str(self.data_dir),
                    session_operation_authority=authority,
                    session_operation_context=context,
                )


class TestFieldPreservationWalk:
    """Direct tests of the proof step's field-preservation walk arms.

    Adversarial cases for the fail-closed detector (the walk feeds a BLOCKING
    diagnostic, so a wrong positive wedges a valid pipeline): each arm is
    exercised in both the preserve and the abstain direction, against the
    real plugin registry — no catalog or blob custody needed.
    """

    @staticmethod
    def _observed_csv_state():
        return _empty_state().with_source(
            SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )

    @staticmethod
    def _node(node_id: str, **overrides: Any) -> NodeSpec:
        base: dict[str, Any] = {
            "id": node_id,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "done",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        base.update(overrides)
        return NodeSpec(**base)

    def _walk(self, state, connection: str, field: str) -> bool:
        from elspeth.web.composer.tools.generation import _source_field_reaches_connection_without_type_change

        return _source_field_reaches_connection_without_type_change(
            state,
            connection,
            source_name="source",
            field_name=field,
        )

    def test_type_coerce_declared_fields_abstain(self) -> None:
        """type_coerce re-declares every input field in its OUTPUT config, so
        membership abstention covers both the conversion target (the value
        provably changed type) and the untouched declared field. The second
        abstention is deliberate conservatism, not a defect: the walk is
        shared with the gate-sampling arm, which needs VALUE preservation —
        a declared field may have been rewritten to a different value of the
        same type, so mere same-type declarations cannot clear it."""
        state = self._observed_csv_state().with_node(
            self._node(
                "coerce",
                plugin="type_coerce",
                options={
                    "schema": {"mode": "flexible", "fields": ["price: str", "customer: str"]},
                    "conversions": [{"field": "price", "to": "float"}],
                },
            )
        )
        assert self._walk(state, "done", "price") is False
        assert self._walk(state, "done", "customer") is False

    def test_llm_pass_through_preserves_undeclared_fields(self) -> None:
        """An llm declares only its ADDED fields (response_field and its
        siblings) in its output config, so a source field recurses through —
        the arm that closed the elspeth-e6e552ce34 walk gap."""
        options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
        options["response_field"] = "answer_a"
        state = self._observed_csv_state().with_node(self._node("branch_llm", plugin="llm", options=options))
        assert self._walk(state, "done", "price") is True
        assert self._walk(state, "done", "answer_a") is False

    def test_observed_mode_type_coerce_abstains_entirely(self) -> None:
        """A pass-through transform that declares NO output fields has opted out
        of the declaration discipline — silence is not type-preservation
        (the observed-mode type_coerce trap, elspeth-85e8afa2f5)."""
        state = self._observed_csv_state().with_node(
            self._node(
                "coerce",
                plugin="type_coerce",
                options={
                    "schema": {"mode": "observed"},
                    "conversions": [{"field": "price", "to": "float"}],
                },
            )
        )
        assert self._walk(state, "done", "price") is False
        assert self._walk(state, "done", "customer") is False

    def _fork_coalesce_state(self, *, merge: str, branch_b_plugin: str = "passthrough", branch_b_options: dict[str, Any] | None = None):
        return (
            self._observed_csv_state()
            .with_node(
                self._node(
                    "fan_out",
                    node_type="gate",
                    plugin=None,
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("branch_a", "branch_b"),
                )
            )
            .with_node(self._node("path_a", input="branch_a", on_success="done_a"))
            .with_node(
                self._node(
                    "path_b",
                    plugin=branch_b_plugin,
                    input="branch_b",
                    on_success="done_b",
                    options=branch_b_options or {"schema": {"mode": "observed"}},
                )
            )
            .with_node(
                self._node(
                    "merge_it",
                    node_type="coalesce",
                    plugin=None,
                    input="done_a",
                    on_success="merged",
                    on_error=None,
                    options={},
                    branches={"branch_a": "done_a", "branch_b": "done_b"},
                    policy="require_all",
                    merge=merge,
                )
            )
        )

    def test_union_coalesce_preserves_when_all_branches_preserve(self) -> None:
        state = self._fork_coalesce_state(merge="union")
        assert self._walk(state, "merged", "price") is True

    def test_nested_coalesce_abstains(self) -> None:
        """A nested merge rewrites the top level to branch names — no top-level
        source field survives it."""
        state = self._fork_coalesce_state(merge="nested")
        assert self._walk(state, "merged", "price") is False

    def test_union_coalesce_abstains_when_one_branch_overwrites(self) -> None:
        """Unanimity: one branch overwriting the field collapses the vote."""
        state = self._fork_coalesce_state(
            merge="union",
            branch_b_plugin="value_transform",
            branch_b_options={
                "schema": {"mode": "observed"},
                "operations": [{"target": "price", "expression": "1"}],
            },
        )
        assert self._walk(state, "merged", "price") is False

    def test_shared_queue_predecessors_are_evaluated_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Layered queue fan-in remains linear in the authored graph size."""
        from elspeth.web.composer._producer_resolver import ProducerResolver

        state = self._observed_csv_state().with_named_source(
            "other",
            SourceSpec(
                plugin="csv",
                on_success="queue_0_a",
                options={"path": "/data/other.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
        )
        state = state.with_node(self._node("queue_0_a", node_type="queue", plugin=None, input="queue_0_a", on_success=None))
        state = state.with_node(self._node("queue_0_b", node_type="queue", plugin=None, input="queue_0_b", on_success=None))
        depth = 8
        for layer in range(1, depth + 1):
            previous = layer - 1
            destinations = (f"queue_{layer}_a", f"queue_{layer}_b")
            for branch in ("a", "b"):
                state = state.with_node(
                    self._node(
                        f"gate_{layer}_{branch}",
                        node_type="gate",
                        plugin=None,
                        input=f"queue_{previous}_{branch}",
                        on_success=None,
                        condition="True",
                        routes={"true": destinations[0], "false": destinations[1]},
                        options={},
                    )
                )
            for destination in destinations:
                state = state.with_node(self._node(destination, node_type="queue", plugin=None, input=destination, on_success=None))

        calls = 0
        original = ProducerResolver.queue_predecessors

        def counting_queue_predecessors(resolver: ProducerResolver, queue_id: str):
            nonlocal calls
            calls += 1
            return original(resolver, queue_id)

        monkeypatch.setattr(ProducerResolver, "queue_predecessors", counting_queue_predecessors)

        assert self._walk(state, f"queue_{depth}_a", "price") is False
        assert calls <= (2 * depth) + 1


class TestBlockingDiagnosticRegistry:
    """``_blocking_diagnostic`` enforces the canonical-codes invariant.

    The skill markdown that drives the composer LLM cites these codes by
    name; if a contributor adds a new blocker without registering the code
    in ``_BLOCKING_DIAGNOSTIC_CODES``, the LLM's repair vocabulary drifts
    silently. The constructor's runtime assertion turns that drift into a
    crash at the construction site.
    """

    def test_unregistered_code_raises_at_construction(self) -> None:
        from elspeth.web.composer.tools import _blocking_diagnostic

        with pytest.raises(AssertionError, match="not registered in _BLOCKING_DIAGNOSTIC_CODES"):
            _blocking_diagnostic(
                code="this_code_was_never_registered",
                message="msg",
                suggested_repair="repair",
                evidence_locator={},
            )

    def test_registered_codes_construct_successfully(self) -> None:
        from elspeth.web.composer.tools import _BLOCKING_DIAGNOSTIC_CODES, _blocking_diagnostic

        for code in _BLOCKING_DIAGNOSTIC_CODES:
            d = _blocking_diagnostic(
                code=code,
                message="msg",
                suggested_repair="repair",
                evidence_locator={"source": "blob", "blob_id": "abc"},
            )
            # Construction sets severity blocking and preserves the inputs.
            assert d["code"] == code
            assert d["severity"] == "blocking"
            assert d["message"] == "msg"
            assert d["suggested_repair"] == "repair"
            assert d["evidence_locator"] == {"source": "blob", "blob_id": "abc"}
