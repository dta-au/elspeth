"""Direct tests for the ordered execution-validation ledger."""

from __future__ import annotations

from dataclasses import fields

import pytest

from elspeth.web.execution._validation_ledger import CORE_VALIDATION_CHECK_NAMES, ValidationLedger
from elspeth.web.execution._validation_model import (
    AuthoredValidatedState,
    GraphedRuntime,
    InstantiatedRuntime,
    InterpretationValidatedState,
    LoadedRuntime,
    MaterializedYaml,
    PhaseFailure,
    PhaseReport,
    PhaseTermination,
    PolicyLoweredState,
    SecretValidatedState,
)
from elspeth.web.execution.schemas import (
    CHECK_GATE_FAN_OUT_ADVISORY,
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


def _pending_readiness() -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=True,
        execution_ready=False,
        completion_ready=True,
        blockers=[
            ValidationReadinessBlocker(
                code="interpretation_review_pending",
                component_id="llm_1",
                component_type="transform",
                detail="Interpretation review is pending.",
            )
        ],
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
        (SecretValidatedState, ("policy", "all_secret_refs", "env_ref_names")),
        (AuthoredValidatedState, ("policy", "all_secret_refs", "env_ref_names", "semantic_contracts")),
        (InterpretationValidatedState, ("authored", "materialized_state")),
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
    """Pin the literal canonical sequence, not properties derivable from the
    definition (the previous length/prefix assertions restated the module's
    own import-time guard). Emission rank is load-bearing — completion-gate
    reconciliation inserts by VALIDATION_CHECK_NAMES.index — so any insertion,
    removal, or reorder must be a conscious edit here too."""
    assert CORE_VALIDATION_CHECK_NAMES == (
        "plugin_enablement",
        "operator_profile_options",
        "required_control_availability",
        "required_control_coverage",
        "path_allowlist",
        "web_scrape_network_policy",
        "web_fetch_resource_policy",
        "secret_refs",
        "semantic_contracts",
        "batch_transform_options",
        "interpretation_review",
        "blob_inline_refs",
        "managed_identity_policy",
        "llm_retry_budget_policy",
        "llm_base_url_policy",
        "llm_tracing_policy",
        "aws_s3_endpoint_url_policy",
        "aws_s3_source_policy",
        "settings_load",
        "plugin_instantiation",
        "value_source_compliance",
        "graph_structure",
        "route_target_resolution",
        "schema_compatibility",
    )


def test_phase_report_snapshots_caller_owned_checks_on_construction() -> None:
    check = _check(CHECK_PLUGIN_ENABLEMENT, passed=True)

    report = PhaseReport(artifact="artifact", checks=(check,))
    check.detail = "mutated caller-owned check"
    check.affected_nodes = ("mutated",)

    assert report.checks[0].detail == "plugin_enablement passed"
    assert report.checks[0].affected_nodes == ()


def test_phase_failure_snapshots_all_caller_owned_evidence_on_construction() -> None:
    passed_check = _check(CHECK_PLUGIN_ENABLEMENT, passed=True)
    failed_check = _check("operator_profile_options", passed=False)
    error = _error("blocked")
    readiness = _blocked_readiness()
    semantic_contract = _semantic_contract()

    failure = PhaseFailure(
        passed_checks=(passed_check,),
        failed_check=failed_check,
        errors=(error,),
        readiness=readiness,
        semantic_contracts=(semantic_contract,),
    )
    passed_check.detail = "mutated pass"
    failed_check.detail = "mutated failure"
    error.message = "mutated error"
    readiness.blockers[0].detail = "mutated blocker"
    readiness.blockers.clear()
    semantic_contract.outcome = "conflict"

    assert failure.passed_checks[0].detail == "plugin_enablement passed"
    assert failure.failed_check.detail == "operator_profile_options failed"
    assert failure.errors[0].message == "blocked"
    assert len(failure.readiness.blockers) == 1
    assert failure.readiness.blockers[0].detail == "Validation failed."
    assert failure.semantic_contracts[0].outcome == "satisfied"


def test_phase_outcomes_apply_without_runner_side_type_discrimination() -> None:
    ledger = ValidationLedger()
    report = PhaseReport(artifact="artifact", checks=(_check(CHECK_PLUGIN_ENABLEMENT, passed=True),))

    assert report.apply(ledger) == "artifact"

    failure = PhaseFailure(
        passed_checks=(),
        failed_check=_check("operator_profile_options", passed=False),
        errors=(_error("blocked"),),
        readiness=_blocked_readiness(),
    )
    with pytest.raises(PhaseTermination) as captured:
        failure.apply(ledger)

    assert captured.value.result.is_valid is False
    assert [check.name for check in captured.value.result.checks] == list(VALIDATION_BLOCKING_CHECK_NAMES)


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


def test_record_pass_snapshots_the_mutable_check() -> None:
    ledger = ValidationLedger()
    original = _check(CHECK_PLUGIN_ENABLEMENT, passed=True)
    ledger.record_pass(original)

    original.detail = "mutated after record_pass"
    original.affected_nodes = ("mutated",)
    result = ledger.finish_failure(
        _check("operator_profile_options", passed=False),
        errors=(_error("blocked"),),
        readiness=_blocked_readiness(),
    )

    assert result.checks[0].detail == "plugin_enablement passed"
    assert result.checks[0].affected_nodes == ()


def test_checks_returns_an_immutable_deep_snapshot_for_incremental_phase_migration() -> None:
    ledger = ValidationLedger()
    original = _check(CHECK_PLUGIN_ENABLEMENT, passed=True)
    ledger.record_pass(original)

    snapshot = ledger.checks
    original.detail = "mutated original"
    snapshot[0].detail = "mutated snapshot"

    assert isinstance(snapshot, tuple)
    assert ledger.checks[0].detail == "plugin_enablement passed"


def test_finish_failure_snapshots_all_mutable_inputs() -> None:
    ledger = ValidationLedger()
    failed_check = _check(CHECK_PLUGIN_ENABLEMENT, passed=False)
    error = _error("blocked")
    readiness = _blocked_readiness()
    semantic_contract = _semantic_contract()

    result = ledger.finish_failure(
        failed_check,
        errors=(error,),
        readiness=readiness,
        semantic_contracts=(semantic_contract,),
    )
    failed_check.detail = "mutated failed check"
    error.message = "mutated error"
    readiness.authoring_valid = True
    readiness.blockers[0].detail = "mutated blocker"
    readiness.blockers.clear()
    semantic_contract.outcome = "conflict"

    assert result.checks[0].detail == "plugin_enablement failed"
    assert result.errors[0].message == "blocked"
    assert result.readiness.authoring_valid is False
    assert len(result.readiness.blockers) == 1
    assert result.readiness.blockers[0].detail == "Validation failed."
    assert result.semantic_contracts[0].outcome == "satisfied"


def test_finish_failure_result_mutation_does_not_flow_back_into_ledger() -> None:
    ledger = ValidationLedger()
    result = ledger.finish_failure(
        _check(CHECK_PLUGIN_ENABLEMENT, passed=False),
        errors=(_error("blocked"),),
        readiness=_blocked_readiness(),
    )

    result.checks[0].detail = "mutated terminal result"
    result.checks[-1].affected_nodes = ("mutated",)
    result.errors[0].message = "mutated terminal error"

    assert ledger.checks[0].detail == "plugin_enablement failed"
    assert ledger.checks[-1].affected_nodes == ()
    assert ledger._errors[0].message == "blocked"


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


def test_record_pass_refuses_a_failed_check() -> None:
    """The guard that stops a failing check being recorded in the passing prefix."""
    ledger = ValidationLedger()

    with pytest.raises(RuntimeError, match="requires a passed check"):
        ledger.record_pass(_check(CHECK_PLUGIN_ENABLEMENT, passed=False))


def test_record_advisory_refuses_an_unregistered_name() -> None:
    """Advisories come from the closed registered set — a core name is refused."""
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)

    with pytest.raises(RuntimeError, match="not a registered advisory check"):
        ledger.record_advisory(_check(CHECK_PLUGIN_ENABLEMENT, passed=True))


def test_record_advisory_refuses_a_failed_advisory() -> None:
    """Advisories are passed=True by contract; a failed one must not be recorded."""
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)

    with pytest.raises(RuntimeError, match="advisory records must pass"):
        ledger.record_advisory(_check(CHECK_IDENTITY_NODE_ADVISORY, passed=False))


def test_finish_failure_refuses_a_passed_check() -> None:
    """The guard that stops a passing check being reported as the terminal failure."""
    ledger = ValidationLedger()

    with pytest.raises(RuntimeError, match="requires a failed check"):
        ledger.finish_failure(
            _check(CHECK_PLUGIN_ENABLEMENT, passed=True),
            errors=(_error("blocked"),),
            readiness=_blocked_readiness(),
        )


def test_record_advisory_snapshots_the_mutable_check() -> None:
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)
    original = ValidationCheck(
        name=CHECK_IDENTITY_NODE_ADVISORY,
        passed=True,
        detail="identity advisory",
        affected_nodes=("identity_1",),
        outcome_code=None,
    )
    ledger.record_advisory(original)

    original.detail = "mutated after record_advisory"
    original.affected_nodes = ("mutated",)
    result = ledger.finish_success(readiness=_ready_readiness())

    assert result.checks[-1].detail == "identity advisory"
    assert result.checks[-1].affected_nodes == ("identity_1",)


def test_finish_success_accepts_each_registered_advisory_name() -> None:
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)
    for advisory_name in (CHECK_IDENTITY_NODE_ADVISORY, CHECK_GATE_FAN_OUT_ADVISORY):
        ledger.record_advisory(_check(advisory_name, passed=True))

    result = ledger.finish_success(readiness=_ready_readiness())

    assert [check.name for check in result.checks[24:]] == [
        CHECK_IDENTITY_NODE_ADVISORY,
        CHECK_GATE_FAN_OUT_ADVISORY,
    ]


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


def test_finish_success_snapshots_all_mutable_inputs() -> None:
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)
    readiness = _ready_readiness()
    warning = _warning("graph.warning")
    semantic_contract = _semantic_contract()

    result = ledger.finish_success(
        readiness=readiness,
        warnings=(warning,),
        semantic_contracts=(semantic_contract,),
    )
    readiness.completion_ready = False
    readiness.blockers.append(
        ValidationReadinessBlocker(
            code="mutated",
            component_id=None,
            component_type=None,
            detail="mutated",
        )
    )
    warning.message = "mutated warning"
    semantic_contract.outcome = "conflict"

    assert result.readiness.completion_ready is True
    assert result.readiness.blockers == []
    assert result.warnings[0].message == "Graph warning."
    assert result.semantic_contracts[0].outcome == "satisfied"


def test_finish_success_result_mutation_does_not_flow_back_into_ledger() -> None:
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)

    result = ledger.finish_success(readiness=_ready_readiness())
    result.checks[0].detail = "mutated terminal result"
    result.checks[-1].affected_nodes = ("mutated",)

    assert ledger.checks[0].detail == "plugin_enablement passed"
    assert ledger.checks[-1].affected_nodes == ()


@pytest.mark.parametrize(
    ("authoring_valid", "execution_ready", "completion_ready", "has_blocker"),
    [
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (True, True, True, True),
    ],
)
def test_finish_success_rejects_inconsistent_readiness(
    authoring_valid: bool,
    execution_ready: bool,
    completion_ready: bool,
    has_blocker: bool,
) -> None:
    ledger = ValidationLedger()
    _record_all_core_passes(ledger)
    readiness = ValidationReadiness(
        authoring_valid=authoring_valid,
        execution_ready=execution_ready,
        completion_ready=completion_ready,
        blockers=([_blocked_readiness().blockers[0]] if has_blocker else []),
    )

    with pytest.raises(RuntimeError, match="successful readiness"):
        ledger.finish_success(readiness=readiness)


def test_finish_failure_rejects_execution_ready_readiness() -> None:
    readiness = _blocked_readiness()
    readiness.execution_ready = True

    with pytest.raises(RuntimeError, match="execution_ready=False"):
        ValidationLedger().finish_failure(
            _check(CHECK_PLUGIN_ENABLEMENT, passed=False),
            errors=(_error("blocked"),),
            readiness=readiness,
        )


def test_finish_failure_requires_a_readiness_blocker() -> None:
    readiness = _blocked_readiness()
    readiness.blockers.clear()

    with pytest.raises(RuntimeError, match="at least one blocker"):
        ValidationLedger().finish_failure(
            _check(CHECK_PLUGIN_ENABLEMENT, passed=False),
            errors=(_error("blocked"),),
            readiness=readiness,
        )


def test_finish_failure_allows_pending_interpretation_readiness() -> None:
    ledger = ValidationLedger()
    failed_name: ValidationCheckName = "interpretation_review"
    for name in CORE_VALIDATION_CHECK_NAMES[: CORE_VALIDATION_CHECK_NAMES.index(failed_name)]:
        ledger.record_pass(_check(name, passed=True))

    result = ledger.finish_failure(
        _check(failed_name, passed=False),
        errors=(_error("pending interpretation"),),
        readiness=_pending_readiness(),
    )

    assert result.readiness.authoring_valid is True
    assert result.readiness.execution_ready is False
    assert result.readiness.completion_ready is True
    assert result.readiness.blockers[0].code == "interpretation_review_pending"


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
