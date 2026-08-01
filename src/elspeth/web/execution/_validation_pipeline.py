"""Dependency carrier and runner for execution validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from elspeth.contracts import SinkProtocol, SourceProtocol, TransformProtocol
from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.core.config import AggregationSettings, ElspethSettings
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.engine.orchestrator.types import PipelineConfig
from elspeth.plugins.infrastructure.runtime_factory import PluginBundle
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution._validation_model import PhaseTermination
from elspeth.web.execution.protocol import ValidationSettings, YamlGenerator
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry


class _YamlLoader(Protocol):
    def __call__(self, yaml_content: str) -> Any: ...


class _YamlSettingsLoader(Protocol):
    def __call__(self, yaml_content: str, *, expand_env_vars: bool = False) -> ElspethSettings: ...


class _DictSettingsLoader(Protocol):
    def __call__(self, config_dict: Mapping[str, object], *, expand_env_vars: bool = False) -> ElspethSettings: ...


class _PluginInstantiator(Protocol):
    def __call__(self, settings: ElspethSettings, *, plugin_snapshot: PluginAvailabilitySnapshot) -> PluginBundle: ...


class _GraphBuilder(Protocol):
    def __call__(self, settings: ElspethSettings, bundle: PluginBundle) -> ExecutionGraph: ...


class _RouteValidator(Protocol):
    def __call__(
        self,
        *,
        sources: Mapping[str, SourceProtocol],
        transforms: Sequence[WiredTransform],
        sinks: Mapping[str, SinkProtocol],
        aggregations: Mapping[str, tuple[TransformProtocol, AggregationSettings]],
        settings: ElspethSettings,
        graph: ExecutionGraph,
    ) -> PipelineConfig: ...


@dataclass(frozen=True, slots=True)
class ValidationDependencies:
    """Runtime functions captured from the compatibility facade per call."""

    load_yaml: _YamlLoader
    load_settings_yaml: _YamlSettingsLoader
    load_settings_dict: _DictSettingsLoader
    instantiate_plugins: _PluginInstantiator
    build_graph: _GraphBuilder
    validate_routes: _RouteValidator


@dataclass(frozen=True, slots=True)
class ValidationPipeline:
    """Own one execution-validation run without changing legacy behavior."""

    dependencies: ValidationDependencies

    def run(
        self,
        state: CompositionState,
        settings: ValidationSettings,
        yaml_generator: YamlGenerator,
        *,
        plugin_snapshot: PluginAvailabilitySnapshot,
        profile_registry: OperatorProfileRegistry | None,
        catalog: CatalogService,
        secret_service: WebSecretResolver | None = None,
        user_id: str | None = None,
        blob_get_metadata: Callable[[UUID], BlobRecord | None] | None = None,
        allow_pending_interpretation_placeholders: bool = False,
        session_id: str | None = None,
    ) -> ValidationResult:
        """Delegate to the behavior-preserving private implementation."""
        from elspeth.web.execution.validation import _validate_pipeline_impl

        try:
            return _validate_pipeline_impl(
                state,
                settings,
                yaml_generator,
                plugin_snapshot=plugin_snapshot,
                profile_registry=profile_registry,
                catalog=catalog,
                secret_service=secret_service,
                user_id=user_id,
                blob_get_metadata=blob_get_metadata,
                allow_pending_interpretation_placeholders=allow_pending_interpretation_placeholders,
                session_id=session_id,
                dependencies=self.dependencies,
            )
        except PhaseTermination as termination:
            return termination.result
