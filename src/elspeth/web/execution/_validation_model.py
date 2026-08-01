"""Typed handoff artifacts for the execution-validation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.secrets import SecretScope
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import (
    SemanticEdgeContractResponse,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationWarning,
)

if TYPE_CHECKING:
    from elspeth.core.config import ElspethSettings
    from elspeth.core.dag.graph import ExecutionGraph
    from elspeth.plugins.infrastructure.runtime_factory import PluginBundle


@dataclass(frozen=True, slots=True)
class PhaseReport[T]:
    """One successful phase artifact and its ordered passing checks."""

    artifact: T
    checks: tuple[ValidationCheck, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(check.model_copy(deep=True) for check in self.checks))


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


@dataclass(frozen=True, slots=True)
class InterpretationValidatedState:
    """Authored evidence plus the state selected by interpretation review."""

    authored: AuthoredValidatedState
    materialized_state: CompositionState


@dataclass(frozen=True, slots=True)
class MaterializedYaml:
    """Exact materialized state and YAML admitted to runtime loading."""

    authored: AuthoredValidatedState
    materialized_state: CompositionState
    pipeline_yaml: str


@dataclass(frozen=True, slots=True)
class LoadedRuntime:
    """Materialized validation input with parsed engine settings."""

    materialized: MaterializedYaml
    settings: ElspethSettings


@dataclass(frozen=True, slots=True)
class InstantiatedRuntime:
    """Loaded settings with their exact instantiated plugin bundle."""

    loaded: LoadedRuntime
    bundle: PluginBundle


@dataclass(frozen=True, slots=True)
class GraphedRuntime:
    """Instantiated runtime with the admitted graph and ordered warnings."""

    instantiated: InstantiatedRuntime
    graph: ExecutionGraph
    graph_warnings: tuple[ValidationWarning, ...]
