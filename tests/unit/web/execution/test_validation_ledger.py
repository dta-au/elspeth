"""Direct tests for the ordered execution-validation ledger."""

from __future__ import annotations

from dataclasses import fields

import pytest

from elspeth.web.execution._validation_ledger import CORE_VALIDATION_CHECK_NAMES, ValidationLedger
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    GraphedRuntime,
    InstantiatedRuntime,
    LoadedRuntime,
    MaterializedYaml,
    PolicyLoweredState,
)
from elspeth.web.execution.schemas import (
    CHECK_IDENTITY_NODE_ADVISORY,
    CHECK_OUTCOME_SECRET_REFS_NO_REFS,
    CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
    CHECK_PLUGIN_ENABLEMENT,
    CHECK_SECRET_REFS,
    CHECK_SETTINGS,
    CHECK_STATE_EXISTS,
    VALIDATION_BLOCKING_CHECK_NAMES,
    SemanticEdgeContractResponse,
    ValidationCheck,
    ValidationCheckName,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
    ValidationWarning,
)


def _check(name: ValidationCheckName, *, passed: bool) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        passed=passed,
        detail=f"{name} {'passed' if passed else 'failed'}",
        affected_nodes=(),
        outcome_code=(CHECK_OUTCOME_SECRET_REFS_NO_REFS if name == CHECK_SECRET_REFS and passed else None),
    )


def _error(message: str) -> ValidationError:
    return ValidationError(
        component_id=None,
        component_type=None,
        message=message,
        suggestion=None,
        error_code=None,
    )


def _blocked_readiness() -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=False,
        execution_ready=False,
        completion_ready=False,
        blockers=[
            ValidationReadinessBlocker(
                code="blocked",
                component_id=None,
                component_type=None,
                detail="Validation failed.",
            )
        ],
    )


def _ready_readiness() -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=True,
        execution_ready=True,
        completion_ready=True,
        blockers=[],
    )


def _warning(code: str) -> ValidationWarning:
    return ValidationWarning(
        component_id=None,
        component_type="graph",
        message="Graph warning.",
        suggestion=None,
        warning_code=code,
    )


def _semantic_contract() -> SemanticEdgeContractResponse:
    return SemanticEdgeContractResponse(
        from_id="source",
        to_id="sink",
        consumer_plugin="json",
        producer_plugin="csv",
        producer_field="value",
        consumer_field="value",
        outcome="satisfied",
        requirement_code="field.required",
    )


def _record_all_core_passes(ledger: ValidationLedger) -> None:
    for name in CORE_VALIDATION_CHECK_NAMES:
        ledger.record_pass(_check(name, passed=True))


@pytest.mark.parametrize(
    ("carrier_type", "field_names"),
    [
        (PolicyLoweredState, ("state", "operator_resolved_model_node_ids")),
        (AuthoredValidatedState, ("policy", "all_secret_refs", "env_ref_names", "semantic_contracts")),
        (MaterializedYaml, ("authored", "materialized_state", "pipeline_yaml")),
        (LoadedRuntime, ("materialized", "settings")),
        (InstantiatedRuntime, ("loaded", "bundle")),
        (GraphedRuntime, ("instantiated", "graph", "graph_warnings")),
    ],
)
def test_validation_carriers_are_frozen_slotted_dataclasses(
    carrier_type: type[object],
    field_names: tuple[str, ...],
) -> None:
    assert carrier_type.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert carrier_type.__slots__ == field_names  # type: ignore[attr-defined]
    assert tuple(field.name for field in fields(carrier_type)) == field_names


def test_core_names_are_the_24_name_canonical_prefix() -> None:
    assert len(CORE_VALIDATION_CHECK_NAMES) == 24
    assert VALIDATION_BLOCKING_CHECK_NAMES[:24] == CORE_VALIDATION_CHECK_NAMES
    assert CORE_VALIDATION_CHECK_NAMES[-1] == "schema_compatibility"


def test_finish_failure_preserves_prefix_and_completes_canonical_cascade() -> None:
    ledger = ValidationLedger()
    ledger.record_pass(_check("plugin_enablement", passed=True))
    ledger.record_pass(_check("operator_profile_options", passed=True))

    result = ledger.finish_failure(
        _check("required_control_availability", passed=False),
        errors=(_error("blocked"),),
        readiness=_blocked_readiness(),
    )

    assert [check.name for check in result.checks] == list(VALIDATION_BLOCKING_CHECK_NAMES)
    assert result.checks[0].passed is True
    assert result.checks[1].passed is True
    assert result.checks[2].passed is False
    assert result.checks[2].outcome_code is None
    assert all(check.passed is False for check in result.checks[3:])
    assert all(check.outcome_code == CHECK_OUTCOME_SKIPPED_AFTER_FAILURE for check in result.checks[3:])
    assert result.errors == [_error("blocked")]
    assert result.readiness == _blocked_readiness()
    assert result.is_valid is False


def test_duplicate_core_name_raises() -> None:
    ledger = ValidationLedger()
    ledger.record_pass(_check(CHECK_PLUGIN_ENABLEMENT, passed=True))

    with pytest.raises(RuntimeError, match="duplicate"):
        ledger.record_pass(_check(CHECK_PLUGIN_ENABLEMENT, passed=True))


def test_out_of_order_core_name_raises() -> None:
    ledger = ValidationLedger()

    with pytest.raises(RuntimeError, match="out of order"):
        ledger.record_pass(_check("operator_profile_options", passed=True))


def test_pass_after_terminal_failure_raises() -> None:
    ledger = ValidationLedger()
    ledger.finish_failure(
        _check(CHECK_PLUGIN_ENABLEMENT, passed=False),
        errors=(_error("blocked"),),
        readiness=_blocked_readiness(),
    )

    with pytest.raises(RuntimeError, match="finished"):
        ledger.record_pass(_check("operator_profile_options", passed=True))


def test_advisory_before_all_core_passes_raises() -> None:
    ledger = ValidationLedger()

    with pytest.raises(RuntimeError, match="all 24 core checks"):
        ledger.record_advisory(_check(CHECK_IDENTITY_NODE_ADVISORY, passed=True))


@pytest.mark.parametrize("advisory_count", [0, 2])
def test_finish_success_permits_zero_or_more_identity_advisories(advisory_count: int) -> None:
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)
    for index in range(advisory_count):
        ledger.record_advisory(
            ValidationCheck(
                name=CHECK_IDENTITY_NODE_ADVISORY,
                passed=True,
                detail=f"identity advisory {index}",
                affected_nodes=(f"identity_{index}",),
                outcome_code=None,
            )
        )

    result = ledger.finish_success(
        readiness=_ready_readiness(),
        warnings=(_warning("graph.warning"),),
        semantic_contracts=(_semantic_contract(),),
    )

    assert result.is_valid is True
    assert [check.name for check in result.checks[:24]] == list(CORE_VALIDATION_CHECK_NAMES)
    assert [check.name for check in result.checks[24:]] == [CHECK_IDENTITY_NODE_ADVISORY] * advisory_count
    assert result.errors == []
    assert result.warnings == [_warning("graph.warning")]
    assert result.readiness == _ready_readiness()
    assert result.semantic_contracts == [_semantic_contract()]


def test_finish_success_before_all_core_passes_raises() -> None:
    ledger = ValidationLedger()
    ledger.record_pass(_check(CHECK_PLUGIN_ENABLEMENT, passed=True))

    with pytest.raises(RuntimeError, match="all 24 core checks"):
        ledger.finish_success(readiness=_ready_readiness())


def test_empty_pipeline_shape_remains_an_explicit_producer_outside_the_ledger() -> None:
    empty_check = _check(CHECK_SETTINGS, passed=False)
    explicit_result = ValidationResult(
        is_valid=False,
        checks=[empty_check],
        errors=[_error("empty pipeline")],
        readiness=_blocked_readiness(),
    )
    assert explicit_result.checks == [empty_check]

    with pytest.raises(RuntimeError, match="out of order"):
        ValidationLedger().finish_failure(
            empty_check,
            errors=tuple(explicit_result.errors),
            readiness=explicit_result.readiness,
        )


def test_outer_layer_synthetic_shape_remains_an_explicit_producer_outside_the_ledger() -> None:
    outer_check = _check(CHECK_STATE_EXISTS, passed=True)
    explicit_result = ValidationResult(
        is_valid=True,
        checks=[outer_check],
        errors=[],
        readiness=_ready_readiness(),
    )
    assert explicit_result.checks == [outer_check]

    with pytest.raises(RuntimeError, match="not a core validation check"):
        ValidationLedger().record_pass(outer_check)
