"""Rejection scope is a producer decision, separate from saved graph validity."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import ValidationEntry, ValidationSummary
from elspeth.web.composer.tools._common import (
    ToolResult,
    _candidate_rejection_result,
    _saved_graph_rejection_result,
    normalize_tool_result_validation,
)
from elspeth.web.composer.tools.sources import _resolve_source_blob
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer._helpers import _empty_state, _mock_catalog


def test_candidate_refusal_has_only_its_own_findings_across_snapshots() -> None:
    state = _empty_state()
    standing = state.validate()
    assert standing.errors
    result = _candidate_rejection_result(
        state,
        "Candidate node options are invalid.",
        error_code="plugin_options_invalid",
        rejected_component="node:scorer",
    )
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    for current in (snapshot, replace(snapshot, snapshot_hash="different-request-snapshot")):
        result = normalize_tool_result_validation(result, PolicyCatalogView.for_trained_operator(catalog, current))
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.affected_nodes == ()
        assert result.prior_validation is None
        assert "validation_delta" not in result.to_dict()
        assert [(entry.component, entry.error_code, entry.rejected_component) for entry in result.validation.errors] == [
            ("rejected_mutation", "plugin_options_invalid", "node:scorer")
        ]
        assert all(entry not in result.validation.errors for entry in standing.errors)


def test_saved_graph_refusal_keeps_standing_findings() -> None:
    state = _empty_state()
    standing = state.validate()
    result = _saved_graph_rejection_result(
        state,
        "Requested source is unavailable.",
        error_code="plugin_not_installed",
        rejected_component="source",
    )
    assert result.updated_state is state
    assert result.validation.errors[1:] == standing.errors
    assert result.validation.warnings == standing.warnings
    assert result.validation.suggestions == standing.suggestions
    assert result.validation.edge_contracts == standing.edge_contracts
    assert result.validation.semantic_contracts == standing.semantic_contracts
    assert result.validation.errors[0].rejected_component == "source"


@pytest.mark.parametrize("code", [None, "", "   ", 42])
def test_candidate_operation_requires_a_nonempty_owned_code(code: object) -> None:
    with pytest.raises(AuditIntegrityError, match="nonempty producer-owned error code"):
        _candidate_rejection_result(_empty_state(), "Refused.", error_code=code, rejected_component=None)


def test_candidate_operation_requires_explicit_code_and_subject() -> None:
    with pytest.raises(TypeError, match="error_code"):
        _candidate_rejection_result(_empty_state(), "Refused.", rejected_component=None)
    with pytest.raises(TypeError, match="rejected_component"):
        _candidate_rejection_result(_empty_state(), "Refused.", error_code="plugin_options_invalid")


def test_blob_resolver_rejects_structural_operation_impostor_before_work() -> None:
    class Impostor:
        def reject(self, *args: object, **kwargs: object) -> None:
            pytest.fail("a structural method must never select rejection semantics")

    with pytest.raises(AuditIntegrityError, match="owned rejection operation"):
        _resolve_source_blob(
            blob_id="invalid",
            explicit_plugin="csv",
            caller_options={},
            rejection_operation=Impostor(),
            on_validation_failure="discard",
            state=_empty_state(),
            context=None,
            session_engine=None,
            session_id=None,
        )


def test_feedback_compiler_refuses_unowned_variant() -> None:
    with pytest.raises(AuditIntegrityError, match="Unknown planner feedback operation"):
        pipeline_planner._compile_planner_feedback(object())


def test_feedback_compiler_keeps_schema_and_argument_wire_distinct() -> None:
    schema = pipeline_planner._compile_planner_feedback(pipeline_planner._SchemaFeedback((), 0))
    argument = pipeline_planner._compile_planner_feedback(
        pipeline_planner._ArgumentFeedback(ToolArgumentError(argument="options", expected="object", actual_type="list"))
    )
    assert schema == {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {"component": "pipeline", "severity": "high", "error_code": "canonical_schema", "error_class": "SchemaValidationError"}
            ],
        },
    }
    assert argument["validation"]["errors"][0]["error_class"] == "ToolArgumentError"
    assert "repeat_notice" not in schema
    assert "rejected_component" not in schema["validation"]["errors"][0]


def test_codeless_state_finding_is_distinct_from_codeless_candidate_refusal() -> None:
    finding = ValidationEntry(component="pipeline", message="legacy state finding", severity="high")
    state = _empty_state()
    result = ToolResult(
        success=False, updated_state=state, validation=ValidationSummary(is_valid=False, errors=(finding,)), affected_nodes=()
    )
    assert pipeline_planner._allowlisted_candidate_feedback(result)["validation"]["errors"][0]["error_code"] == "validation_error"
    uncoded_rejection = replace(finding, component="rejected_mutation", rejected_component="source")
    result = replace(result, validation=ValidationSummary(is_valid=False, errors=(uncoded_rejection,)))
    with pytest.raises(AuditIntegrityError, match="producer-owned error code"):
        pipeline_planner._allowlisted_candidate_feedback(result)


def test_withheld_generic_guidance_does_not_infer_impossibility_from_classifiers() -> None:
    result = _candidate_rejection_result(
        _empty_state(), "PRIVATE-VALIDATOR-CANARY", error_code="plugin_options_invalid", rejected_component="node:shield"
    )
    feedback = pipeline_planner._allowlisted_candidate_feedback(
        result,
        repeated_fingerprint=True,
        finalizer_owned=pipeline_planner._FinalizerOwnedRefs(config=frozenset({"node:shield"})),
    )
    entry = feedback["validation"]["errors"][0]
    assert entry["component"] == "pipeline"
    assert "detail" not in entry
    assert "PRIVATE-VALIDATOR-CANARY" not in str(feedback)
    assert "matching classifiers alone do not prove the cause is unchanged or that repair is impossible" in entry["suggested_fix"]
    assert "cannot succeed" not in entry["suggested_fix"]


@pytest.mark.parametrize("code", ["source_path_policy_invalid", "sink_path_policy_invalid", "provider_config_path_policy_invalid"])
def test_planner_path_policy_projection_has_no_raw_detail_exception(code: str) -> None:
    result = _candidate_rejection_result(_empty_state(), "PRIVATE-PATH-PROJECTION-CANARY", error_code=code, rejected_component="source")
    feedback = pipeline_planner._allowlisted_candidate_feedback(result)
    entry = feedback["validation"]["errors"][0]
    assert entry["error_code"] == code
    assert entry["suggested_fix"]
    assert "detail" not in entry
    assert "PRIVATE-PATH-PROJECTION-CANARY" not in str(feedback)
