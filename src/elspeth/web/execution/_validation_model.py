"""Typed handoff artifacts for the execution-validation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.secrets import SecretScope
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import SemanticEdgeContractResponse, ValidationWarning

if TYPE_CHECKING:
    from elspeth.core.config import ElspethSettings
    from elspeth.core.dag.graph import ExecutionGraph
    from elspeth.plugins.infrastructure.runtime_factory import PluginBundle


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
