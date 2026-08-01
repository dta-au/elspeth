"""Ordered ledger and terminal result construction for core validation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from elspeth.web.execution.schemas import (
    CHECK_IDENTITY_NODE_ADVISORY,
    CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
    RUNTIME_CHECK_SCHEMA_COMPATIBILITY,
    VALIDATION_BLOCKING_CHECK_NAMES,
    SemanticEdgeContractResponse,
    ValidationCheck,
    ValidationCheckName,
    ValidationError,
    ValidationReadiness,
    ValidationResult,
    ValidationWarning,
)

_CORE_CHECK_COUNT: Final = 24
_SCHEMA_CHECK_INDEX = VALIDATION_BLOCKING_CHECK_NAMES.index(RUNTIME_CHECK_SCHEMA_COMPATIBILITY)
CORE_VALIDATION_CHECK_NAMES: Final[tuple[ValidationCheckName, ...]] = VALIDATION_BLOCKING_CHECK_NAMES[: _SCHEMA_CHECK_INDEX + 1]
if len(CORE_VALIDATION_CHECK_NAMES) != _CORE_CHECK_COUNT:
    raise AssertionError("core validation checks must be the 24-name canonical prefix through schema_compatibility")


@dataclass(slots=True)
class ValidationLedger:
    """Accumulate core checks in canonical order and build one terminal result."""

    _checks: list[ValidationCheck] = field(default_factory=list, init=False)
    _errors: list[ValidationError] = field(default_factory=list, init=False)
    _core_count: int = field(default=0, init=False)
    _finished: bool = field(default=False, init=False)

    @property
    def checks(self) -> tuple[ValidationCheck, ...]:
        """Return an immutable snapshot for the not-yet-extracted legacy tail."""
        return tuple(check.model_copy(deep=True) for check in self._checks)

    def record_pass(self, check: ValidationCheck) -> None:
        """Record the next successful core check."""
        self._ensure_open()
        if not check.passed:
            raise RuntimeError(f"record_pass requires a passed check; {check.name} failed")
        self._require_next_core(check.name)
        self._checks.append(check.model_copy(deep=True))
        self._core_count += 1

    def record_advisory(self, check: ValidationCheck) -> None:
        """Record one successful identity advisory after all core checks."""
        self._ensure_open()
        if self._core_count != len(CORE_VALIDATION_CHECK_NAMES):
            raise RuntimeError("identity advisories require all 24 core checks to pass first")
        if check.name != CHECK_IDENTITY_NODE_ADVISORY:
            raise RuntimeError(f"{check.name} is not an identity_node_advisory check")
        if not check.passed:
            raise RuntimeError("identity_node_advisory records must pass")
        self._checks.append(check.model_copy(deep=True))

    def finish_failure(
        self,
        failed_check: ValidationCheck,
        *,
        errors: Sequence[ValidationError],
        readiness: ValidationReadiness,
        semantic_contracts: Sequence[SemanticEdgeContractResponse] = (),
    ) -> ValidationResult:
        """Record the next core failure and append its full canonical skipped tail."""
        self._ensure_open()
        if failed_check.passed:
            raise RuntimeError(f"finish_failure requires a failed check; {failed_check.name} passed")
        self._require_next_core(failed_check.name)
        if readiness.execution_ready:
            raise RuntimeError("failed validation readiness must set execution_ready=False")
        if not readiness.blockers:
            raise RuntimeError("failed validation readiness requires at least one blocker")

        failed_check_snapshot = failed_check.model_copy(deep=True)
        readiness_snapshot = readiness.model_copy(deep=True)
        semantic_contract_snapshots = [contract.model_copy(deep=True) for contract in semantic_contracts]
        self._checks.append(failed_check_snapshot)
        self._core_count += 1
        failed_index = VALIDATION_BLOCKING_CHECK_NAMES.index(failed_check_snapshot.name)
        self._checks.extend(
            ValidationCheck(
                name=name,
                passed=False,
                detail=f"Skipped: {failed_check_snapshot.name} failed",
                affected_nodes=(),
                outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
            )
            for name in VALIDATION_BLOCKING_CHECK_NAMES[failed_index + 1 :]
        )
        self._errors.extend(error.model_copy(deep=True) for error in errors)
        result = ValidationResult(
            is_valid=False,
            checks=[check.model_copy(deep=True) for check in self._checks],
            errors=[error.model_copy(deep=True) for error in self._errors],
            readiness=readiness_snapshot,
            semantic_contracts=semantic_contract_snapshots,
        )
        self._finished = True
        return result

    def finish_success(
        self,
        *,
        readiness: ValidationReadiness,
        warnings: Sequence[ValidationWarning] = (),
        semantic_contracts: Sequence[SemanticEdgeContractResponse] = (),
    ) -> ValidationResult:
        """Build a successful result after the complete core prefix and advisories."""
        self._ensure_open()
        if self._core_count != len(CORE_VALIDATION_CHECK_NAMES):
            raise RuntimeError("successful validation requires all 24 core checks to pass")
        if not (readiness.authoring_valid and readiness.execution_ready and readiness.completion_ready) or readiness.blockers:
            raise RuntimeError("successful readiness requires all readiness axes true and no blockers")
        result = ValidationResult(
            is_valid=True,
            checks=[check.model_copy(deep=True) for check in self._checks],
            errors=[],
            warnings=[warning.model_copy(deep=True) for warning in warnings],
            readiness=readiness.model_copy(deep=True),
            semantic_contracts=[contract.model_copy(deep=True) for contract in semantic_contracts],
        )
        self._finished = True
        return result

    def _ensure_open(self) -> None:
        if self._finished:
            raise RuntimeError("validation ledger is already finished")

    def _require_next_core(self, name: ValidationCheckName) -> None:
        if name not in CORE_VALIDATION_CHECK_NAMES:
            raise RuntimeError(f"{name} is not a core validation check")
        if any(check.name == name for check in self._checks):
            raise RuntimeError(f"duplicate core validation check: {name}")
        if self._core_count == len(CORE_VALIDATION_CHECK_NAMES):
            raise RuntimeError("all core validation checks have already been recorded")
        expected = CORE_VALIDATION_CHECK_NAMES[self._core_count]
        if name != expected:
            raise RuntimeError(f"core validation check out of order: expected {expected}, got {name}")
