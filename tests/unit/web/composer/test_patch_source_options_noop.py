"""Source patches report progress only when owned composition content changes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_cache import tool_result_mutated_composition_state
from elspeth.web.composer.source_demand import SOURCE_DATA_CONTRACT_USER_TERM, build_source_data_contract_draft
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext, ToolResult, execute_tool
from elspeth.web.composer.tools.sources import _handle_patch_source_options
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    approved_prompt_artifact_hash_from_options,
    model_choice_artifact_hash,
    prompt_review_anchor_hash_from_options,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_authoring_reconciliation import _reconciler_stale_hash_state

Surface = Literal["handler", "dispatch"]


def _context(**kwargs: object) -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot, **kwargs)


def _state(*, source_name: str = "source", options: dict[str, object] | None = None) -> CompositionState:
    return CompositionState(
        sources={
            source_name: SourceSpec(
                plugin="csv",
                on_success="out",
                on_validation_failure="discard",
                options={
                    "path": "data/input.csv",
                    "schema": {"mode": "observed"},
                    "encoding": "utf-8",
                    **(options if options is not None else {}),
                },
            )
        },
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="out",
                plugin="json",
                on_write_failure="discard",
                options={
                    "path": "outputs/result.jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            ),
        ),
        metadata=PipelineMetadata(name="Source patch progress"),
        version=7,
    )


def _patch(surface: Surface, arguments: dict[str, object], state: CompositionState, context: ToolContext) -> ToolResult:
    if surface == "handler":
        return _handle_patch_source_options(arguments, state, context)
    return execute_tool(
        "patch_source_options",
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
    )


def _resolved_prompt_state(*, stamped: bool) -> CompositionState:
    base = _state()
    prompt = "Return the word acknowledged."
    model = "bedrock/example.model"
    options: dict[str, object] = {
        "provider": "bedrock",
        "model": model,
        "system_prompt": "You acknowledge requests. Return only the requested word.",
        "prompt_template": prompt,
        "response_field": "llm_response",
        "schema": {"mode": "observed"},
    }
    anchor = prompt_review_anchor_hash_from_options(options)
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": "prompt_template_review:reviewed",
            "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
            "user_term": "llm_prompt_template:reviewed",
            "status": "resolved",
            "draft": prompt,
            "event_id": "smk10-prompt-review-event",
            "accepted_value": prompt,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": anchor if anchor is not None else stable_hash(prompt),
        },
        {
            "id": "model_choice_review:reviewed",
            "kind": InterpretationKind.LLM_MODEL_CHOICE.value,
            "user_term": "llm_model_choice:reviewed",
            "status": "resolved",
            "draft": model,
            "event_id": "smk10-model-review-event",
            "accepted_value": model,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": model_choice_artifact_hash(model),
        },
    ]
    if stamped:
        options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    node = NodeSpec(
        id="reviewed",
        node_type="transform",
        plugin="llm",
        input="source_out",
        on_success="out",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    return replace(
        base,
        sources={"source": replace(base.sources["source"], on_success="source_out")},
        nodes=(node,),
    )


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("source_name", ["source", "orders"])
@pytest.mark.parametrize(
    "patch",
    [{}, {"encoding": "utf-8"}, {"skip_rows": None}, {"schema": {"mode": "observed"}}],
    ids=["empty", "same-value", "delete-absent", "same-nested-value"],
)
def test_no_effect_patch_preserves_state_and_does_not_report_progress(surface: Surface, source_name: str, patch: dict[str, object]) -> None:
    original = _state(source_name=source_name)
    before = original.to_dict()

    result = _patch(surface, {"source_name": source_name, "patch": patch}, original, _context())

    assert result.success, result.validation
    assert original.to_dict() == before
    assert result.updated_state is original
    assert result.updated_state.version == original.version
    assert result.affected_nodes == ()
    assert result.applied_component is None
    assert not tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_schema_object_key_order_does_not_report_progress(surface: Surface) -> None:
    original = _state(options={"schema": {"mode": "flexible", "fields": ["record_id: str"]}})

    result = _patch(
        surface,
        {"patch": {"schema": {"fields": ["record_id: str"], "mode": "flexible"}}},
        original,
        _context(),
    )

    assert result.success, result.validation
    assert result.updated_state is original
    assert result.affected_nodes == () and result.applied_component is None
    assert not tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_real_option_edit_advances_version_and_reports_progress(surface: Surface) -> None:
    original = _state()

    result = _patch(surface, {"patch": {"encoding": "utf-8-sig"}}, original, _context())

    assert result.success, result.validation
    assert original.sources["source"].options["encoding"] == "utf-8"
    assert result.updated_state.sources["source"].options["encoding"] == "utf-8-sig"
    assert result.updated_state.version == original.version + 1
    assert result.affected_nodes == ("source",)
    assert tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize(
    ("previous", "proposed"),
    [(1, True), (True, 1), (1, 1.0), (1.0, 1)],
    ids=["int-to-bool", "bool-to-int", "int-to-float", "float-to-int"],
)
def test_equal_numeric_values_keep_an_authored_type_change(surface: Surface, previous: object, proposed: object) -> None:
    # Prevalidation validates a projected config; the authoring state retains
    # the original option values instead of Pydantic's coerced projection.
    assert previous == proposed and type(previous) is not type(proposed)
    original = _state(options={"skip_rows": previous})

    result = _patch(surface, {"patch": {"skip_rows": proposed}}, original, _context())

    assert result.success, result.validation
    assert type(original.sources["source"].options["skip_rows"]) is type(previous)
    assert type(result.updated_state.sources["source"].options["skip_rows"]) is type(proposed)
    assert result.updated_state.sources["source"].options["skip_rows"] == proposed
    assert result.updated_state.version == original.version + 1
    assert tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize(
    ("invalid_value", "error_type", "error_message"),
    [
        pytest.param(object(), TypeError, "not JSON serializable", id="non-json-object"),
        pytest.param(float("nan"), ValueError, "not JSON compliant", id="non-finite-number"),
    ],
)
def test_patch_propagates_owned_state_serialization_failure(
    surface: Surface,
    invalid_value: object,
    error_type: type[Exception],
    error_message: str,
) -> None:
    original = _state()
    original = replace(
        original,
        outputs=(replace(original.outputs[0], options={**original.outputs[0].options, "invariant_violation": invalid_value}),),
    )

    with pytest.raises(error_type, match=error_message):
        _patch(surface, {"patch": {}}, original, _context())

    assert original.version == 7
    assert original.sources["source"].options["encoding"] == "utf-8"
    assert original.outputs[0].options["invariant_violation"] is invalid_value


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_review_only_patch_remains_a_versioned_change(surface: Surface) -> None:
    original = _state()
    draft = build_source_data_contract_draft(["record_id"], None)
    requirement = {
        "kind": InterpretationKind.SOURCE_DATA_CONTRACT.value,
        "user_term": SOURCE_DATA_CONTRACT_USER_TERM,
        "draft": draft,
    }

    result = _patch(surface, {"patch": {INTERPRETATION_REQUIREMENTS_KEY: [requirement]}}, original, _context())

    assert result.success, result.validation
    assert INTERPRETATION_REQUIREMENTS_KEY not in original.sources["source"].options
    options = result.updated_state.sources["source"].options
    assert options["path"] == original.sources["source"].options["path"]
    assert options["encoding"] == original.sources["source"].options["encoding"]
    review = options[INTERPRETATION_REQUIREMENTS_KEY][0]
    assert review["kind"] == InterpretationKind.SOURCE_DATA_CONTRACT.value
    assert review["draft"] == draft and review["status"] == "pending"
    assert result.updated_state.version == original.version + 1
    assert tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("stamped", [False, True], ids=["restore-artifact", "preserve-artifact"])
def test_empty_patch_preserves_real_node_review_reconciliation(surface: Surface, stamped: bool) -> None:
    original = _resolved_prompt_state(stamped=stamped)
    before = original.to_dict()
    expected_stamp = approved_prompt_artifact_hash_from_options(original.nodes[0].options)
    assert isinstance(expected_stamp, str) and expected_stamp

    result = _patch(surface, {"patch": {}}, original, _context())

    assert result.success, result.validation
    assert result.validation.is_valid, result.validation
    assert original.to_dict() == before
    assert result.updated_state.sources["source"].options == original.sources["source"].options
    if stamped:
        assert original.nodes[0].options["approved_prompt_artifact_hash"] == expected_stamp
        assert result.updated_state is original
        assert result.updated_state.to_dict() == before
        assert result.affected_nodes == () and result.applied_component is None
        assert not tool_result_mutated_composition_state(version_before=original.version, result=result)
    else:
        expected = original.to_dict()
        expected["version"] = original.version + 1
        expected["nodes"][0]["options"]["approved_prompt_artifact_hash"] = expected_stamp
        assert result.updated_state is not original
        assert result.updated_state.to_dict() == expected
        assert tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_empty_patch_still_refuses_corrupt_authoritative_review(surface: Surface) -> None:
    original = _reconciler_stale_hash_state()

    result = _patch(surface, {"patch": {}}, original, _context())

    assert not result.success
    assert result.updated_state is original
    assert result.validation.errors[0].error_code == "review_reconciliation_failed"
    assert "hash drifted" in result.validation.errors[0].message
    assert not tool_result_mutated_composition_state(version_before=original.version, result=result)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_empty_patch_still_checks_existing_source_path(surface: Surface, tmp_path: Path) -> None:
    original = _state(options={"path": "/outside-session/input.csv"})

    result = _patch(surface, {"patch": {}}, original, _context(data_dir=str(tmp_path), session_id="test-session"))

    assert not result.success
    assert result.updated_state is original
    assert result.validation.errors[0].error_code == "source_path_policy_invalid"


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_empty_patch_still_checks_existing_plugin_options(surface: Surface) -> None:
    original = _state(options={"encoding": "not-an-encoding"})

    result = _patch(surface, {"patch": {}}, original, _context())

    assert not result.success
    assert result.updated_state is original
    assert result.validation.errors[0].error_code == "plugin_options_invalid"


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_empty_patch_still_refuses_a_missing_source(surface: Surface) -> None:
    original = _state()

    result = _patch(surface, {"source_name": "absent", "patch": {}}, original, _context())

    assert not result.success
    assert result.updated_state is original
    assert result.validation.errors[0].error_code == "source_not_found"
