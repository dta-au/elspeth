"""Gate options that YAML lowering erases are refused at Stage 1 (finding #30).

``GateSettings`` has no options field and ``_lower_gate_nodes`` never emits
``NodeSpec.options``, so any runtime-looking gate option is authored state that
silently vanishes before the pipeline runs. The coalesce analogue is
``coalesce_config_invalid``. Gates differ in one way: a gate legitimately
carries web-only authoring metadata (``interpretation_requirements`` holding a
``gate_condition_authored`` pipeline_decision row), which lowering strips by
design, so only keys outside ``AUTHORING_METADATA_OPTION_KEYS`` are refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import _MUTATION_BLOCKING_INVARIANT_CODES
from elspeth.web.composer.tools.generation import _CLOSED_VALIDATION_ERROR_CODES, explain_validation_code
from elspeth.web.interpretation_state import (
    GATE_CONDITION_AUTHORED_USER_TERM,
    INTERPRETATION_REQUIREMENTS_KEY,
)
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

_CODE = "gate_config_invalid"
_MARKER = "PROBE_MARKER_VALUE_123"


def _gate(options: dict[str, Any]) -> NodeSpec:
    return NodeSpec(
        id="g1",
        node_type="gate",
        plugin=None,
        input="rows",
        on_success=None,
        on_error=None,
        options=options,
        condition="row['x'] > 1",
        routes={"true": "main", "false": "main"},
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(gate: NodeSpec) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(gate,),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="json",
                options={"path": "outputs/main.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _staged_gate_decision() -> list[dict[str, Any]]:
    return [
        {
            "id": "g1:gate_condition_authored",
            "kind": InterpretationKind.PIPELINE_DECISION.value,
            "user_term": GATE_CONDITION_AUTHORED_USER_TERM,
            "status": "pending",
            "draft": "I chose the cutoff 1; you did not state one.",
            "event_id": None,
            "accepted_value": None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None,
        }
    ]


def _gate_arguments(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "g1",
        "node_type": "gate",
        "plugin": None,
        "input": "rows",
        "on_success": None,
        "on_error": None,
        "options": options,
        "condition": "row['x'] > 1",
        "routes": {"true": "main", "false": "main"},
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }


def _set_pipeline_arguments(gate_options: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [_gate_arguments(gate_options)],
        "edges": [],
        "outputs": [
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {"path": "outputs/main.json", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    }


class TestGateOptionsValidation:
    def test_runtime_looking_gate_option_is_refused_naming_the_key_not_the_value(self) -> None:
        validation = _state(_gate({"threshold": _MARKER})).validate()

        entries = [entry for entry in validation.errors if entry.error_code == _CODE]
        assert len(entries) == 1, [(entry.error_code, entry.message) for entry in validation.errors]
        assert entries[0].component == "node:g1"
        assert "threshold" in entries[0].message
        assert _MARKER not in entries[0].message
        assert validation.is_valid is False

    def test_empty_gate_options_are_not_refused(self) -> None:
        validation = _state(_gate({})).validate()

        assert _CODE not in {entry.error_code for entry in validation.errors}

    def test_gate_condition_authored_review_row_in_gate_options_is_not_refused(self) -> None:
        """The documented carrier for a gate's pipeline_decision review must survive.

        A blanket "gate options must be empty" rule turns this red: the planner
        brief stages ``gate_condition_authored`` in the gate node's own options.
        """
        validation = _state(_gate({INTERPRETATION_REQUIREMENTS_KEY: _staged_gate_decision()})).validate()

        assert _CODE not in {entry.error_code for entry in validation.errors}

    def test_metadata_alongside_a_runtime_looking_key_still_refuses_only_the_stray_key(self) -> None:
        options = {INTERPRETATION_REQUIREMENTS_KEY: _staged_gate_decision(), "threshold": _MARKER}
        validation = _state(_gate(options)).validate()

        [entry] = [entry for entry in validation.errors if entry.error_code == _CODE]
        assert "threshold" in entry.message
        assert INTERPRETATION_REQUIREMENTS_KEY not in entry.message


class TestGateOptionsMutationsRollBack:
    def test_the_code_blocks_every_mutation_path(self) -> None:
        assert _CODE in _MUTATION_BLOCKING_INVARIANT_CODES

    @pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"])
    def test_create_paths_refuse_stray_gate_options_without_mutating(self, tool_name: str) -> None:
        state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        options = {"threshold": _MARKER}
        arguments = _gate_arguments(options) if tool_name == "upsert_node" else _set_pipeline_arguments(options)

        result = execute_tool(tool_name, arguments, state, _mock_catalog())

        assert result.success is False, result.to_dict()
        assert result.updated_state is state
        assert result.validation is not None
        assert result.validation.errors[0].error_code == _CODE
        assert state.nodes == ()

    def test_patch_node_options_refuses_stray_gate_options_without_mutating(self) -> None:
        state = _state(_gate({}))

        result = execute_tool("patch_node_options", {"node_id": "g1", "patch": {"threshold": _MARKER}}, state, _mock_catalog())

        assert result.success is False, result.to_dict()
        assert result.updated_state is state
        assert result.validation is not None
        assert result.validation.errors[0].error_code == _CODE
        [gate] = state.nodes
        assert dict(gate.options) == {}


class TestGateConfigInvalidIsCatalogued:
    def test_code_is_closed_and_explainable(self) -> None:
        assert _CODE in _CLOSED_VALIDATION_ERROR_CODES
        resolved = explain_validation_code(_CODE)
        assert resolved is not None
        explanation, suggested_fix = resolved
        assert "options" in explanation
        assert "condition" in suggested_fix
        assert "routes" in suggested_fix


class TestStoredGateWithStrayOptionsRecovers:
    """A session persisted before the rule is blocked, but not stuck (freeform).

    Blocking is deliberate (the b4774fc81 coalesce posture): a mutation must
    not persist a state that still carries inert gate config. The block is only
    acceptable because the refusal names the offending gate and key, and a
    single ``patch_node_options`` null-patch clears it, after which the
    previously refused, unrelated mutation succeeds.
    """

    def _unrelated_upsert(self) -> dict[str, Any]:
        return {
            "id": "t1",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "main",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        }

    def test_unrelated_mutation_is_refused_naming_the_gate_until_the_key_is_patched_out(self) -> None:
        stored = _state(_gate({"schema": {"mode": "observed"}}))
        catalog = _mock_catalog()

        refused = execute_tool("upsert_node", self._unrelated_upsert(), stored, catalog)

        assert refused.success is False, refused.to_dict()
        assert refused.updated_state is stored
        assert refused.validation is not None
        gate_entries = [entry for entry in refused.validation.errors if entry.error_code == _CODE]
        assert gate_entries, [(entry.error_code, entry.message) for entry in refused.validation.errors]
        assert all("'g1'" in entry.message and "schema" in entry.message for entry in gate_entries)

        cleared = execute_tool("patch_node_options", {"node_id": "g1", "patch": {"schema": None}}, stored, catalog)

        assert cleared.success is True, cleared.to_dict()
        [gate] = cleared.updated_state.nodes
        assert dict(gate.options) == {}

        retried = execute_tool("upsert_node", self._unrelated_upsert(), cleared.updated_state, catalog)

        assert retried.success is True, retried.to_dict()
        assert {node.id for node in retried.updated_state.nodes} == {"g1", "t1"}
