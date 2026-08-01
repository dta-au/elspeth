"""Typed handoff artifacts for the execution-validation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from elspeth.contracts.secrets import SecretScope
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import (
    SemanticEdgeContractResponse,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationWarning,
)

if TYPE_CHECKING:
    from elspeth.core.config import ElspethSettings
    from elspeth.core.dag.graph import ExecutionGraph
    from elspeth.plugins.infrastructure.runtime_factory import PluginBundle
    from elspeth.web.execution._validation_ledger import ValidationLedger
    from elspeth.web.execution.schemas import ValidationResult


class PhaseTermination(Exception):
    """Typed early termination after a phase has finalized the run ledger."""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("execution validation terminated")
        self.result = result


def _blocked_readiness(
    *,
    code: str,
    detail: str,
    component_id: str | None = None,
    component_type: str | None = None,
    authoring_valid: bool = False,
    completion_ready: bool = False,
) -> ValidationReadiness:
    """Single-source blocked-readiness constructor for every validation phase.

    Previously copied into four modules, two of which had silently dropped the
    ``authoring_valid`` / ``completion_ready`` axes — a fork that could not
    express the interpretation-review-pending shape. One definition, full
    signature, defaults preserving the reduced copies' behavior.
    """
    return ValidationReadiness(
        authoring_valid=authoring_valid,
        execution_ready=False,
        completion_ready=completion_ready,
        blockers=[
            ValidationReadinessBlocker(
                code=code,
                component_id=component_id,
                component_type=component_type,
                detail=detail,
            )
        ],
    )


@dataclass(frozen=True, slots=True)
class PhaseReport[T]:
    """One successful phase artifact and its ordered passing checks."""

    artifact: T
    checks: tuple[ValidationCheck, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(check.model_copy(deep=True) for check in self.checks))

    def apply(self, ledger: ValidationLedger) -> T:
        """Record successful evidence and return the phase artifact."""
        for check in self.checks:
            ledger.record_pass(check)
        return self.artifact


@dataclass(frozen=True, slots=True)
class PhaseFailure:
    """One failed phase, including any passes produced before its failure."""

    passed_checks: tuple[ValidationCheck, ...]
    failed_check: ValidationCheck
    errors: tuple[ValidationError, ...]
    readiness: ValidationReadiness
    semantic_contracts: tuple[SemanticEdgeContractResponse, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "passed_checks", tuple(check.model_copy(deep=True) for check in self.passed_checks))
        object.__setattr__(self, "failed_check", self.failed_check.model_copy(deep=True))
        object.__setattr__(self, "errors", tuple(error.model_copy(deep=True) for error in self.errors))
        object.__setattr__(self, "readiness", self.readiness.model_copy(deep=True))
        object.__setattr__(
            self,
            "semantic_contracts",
            tuple(contract.model_copy(deep=True) for contract in self.semantic_contracts),
        )

    def apply(self, ledger: ValidationLedger) -> Never:
        """Finalize the ledger and terminate the ordered phase sequence."""
        for check in self.passed_checks:
            ledger.record_pass(check)
        raise PhaseTermination(
            ledger.finish_failure(
                self.failed_check,
                errors=self.errors,
                readiness=self.readiness,
                semantic_contracts=self.semantic_contracts,
            )
        )


@dataclass(frozen=True, slots=True)
class PolicyLoweredState:
    """Composer state after plugin policy has lowered operator-owned options."""

    state: CompositionState
    operator_resolved_model_node_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class AuthoredValidatedState:
    """Policy-lowered state plus evidence collected by authored checks."""

    policy: PolicyLoweredState
    all_secret_refs: tuple[tuple[str, SecretScope | None], ...]
    env_ref_names: frozenset[str]
    semantic_contracts: tuple[SemanticEdgeContractResponse, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "semantic_contracts",
            tuple(contract.model_copy(deep=True) for contract in self.semantic_contracts),
        )


def _snapshot_authored_evidence(authored: AuthoredValidatedState) -> AuthoredValidatedState:
    """Detach mutable evidence while preserving immutable policy/state identity."""
    return AuthoredValidatedState(
        policy=authored.policy,
        all_secret_refs=authored.all_secret_refs,
        env_ref_names=authored.env_ref_names,
        semantic_contracts=authored.semantic_contracts,
    )


@dataclass(frozen=True, slots=True)
class InterpretationValidatedState:
    """Authored evidence plus the state selected by interpretation review."""

    authored: AuthoredValidatedState
    materialized_state: CompositionState

    def __post_init__(self) -> None:
        object.__setattr__(self, "authored", _snapshot_authored_evidence(self.authored))


@dataclass(frozen=True, slots=True)
class MaterializedYaml:
    """Exact materialized state and YAML admitted to runtime loading."""

    authored: AuthoredValidatedState
    materialized_state: CompositionState
    pipeline_yaml: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "authored", _snapshot_authored_evidence(self.authored))


@dataclass(frozen=True, slots=True)
class LoadedRuntime:
    """Materialized validation input with parsed engine settings."""

    materialized: MaterializedYaml
    settings: ElspethSettings

    def __post_init__(self) -> None:
        object.__setattr__(self, "materialized", _snapshot_materialized_evidence(self.materialized))


def _snapshot_materialized_evidence(materialized: MaterializedYaml) -> MaterializedYaml:
    """Detach validation evidence while preserving admitted state identity."""
    return MaterializedYaml(
        authored=materialized.authored,
        materialized_state=materialized.materialized_state,
        pipeline_yaml=materialized.pipeline_yaml,
    )


@dataclass(frozen=True, slots=True)
class InstantiatedRuntime:
    """Loaded settings with their exact instantiated plugin bundle."""

    loaded: LoadedRuntime
    bundle: PluginBundle

    def __post_init__(self) -> None:
        object.__setattr__(self, "loaded", _snapshot_loaded_evidence(self.loaded))


def _snapshot_loaded_evidence(loaded: LoadedRuntime) -> LoadedRuntime:
    """Detach the loaded envelope without cloning engine settings."""
    return LoadedRuntime(materialized=loaded.materialized, settings=loaded.settings)


@dataclass(frozen=True, slots=True)
class GraphedRuntime:
    """Instantiated runtime with the admitted graph and ordered warnings."""

    instantiated: InstantiatedRuntime
    graph: ExecutionGraph
    graph_warnings: tuple[ValidationWarning, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "instantiated", _snapshot_instantiated_evidence(self.instantiated))
        object.__setattr__(
            self,
            "graph_warnings",
            tuple(warning.model_copy(deep=True) for warning in self.graph_warnings),
        )


def _snapshot_instantiated_evidence(instantiated: InstantiatedRuntime) -> InstantiatedRuntime:
    """Detach the instantiated envelope without cloning live plugins."""
    return InstantiatedRuntime(loaded=instantiated.loaded, bundle=instantiated.bundle)
