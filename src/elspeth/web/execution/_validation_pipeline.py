"""Dependency carrier and runner for execution validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution._validation_model import PhaseTermination
from elspeth.web.execution._validation_runtime import (
    _DictSettingsLoader,
    _GraphBuilder,
    _PluginInstantiator,
    _RouteValidator,
    _YamlLoader,
    _YamlSettingsLoader,
)
from elspeth.web.execution.protocol import ValidationSettings, YamlGenerator
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry


class _ValidationRunImpl(Protocol):
    """One full execution-validation run, terminated phases already applied."""

    def __call__(
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
        dependencies: ValidationDependencies,
    ) -> ValidationResult: ...


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
    """Own one execution-validation run without changing legacy behavior.

    The implementation is injected at construction (the facade passes its
    ``_validate_pipeline_impl``) so this module never imports back into the
    facade — imports stay top-level in both directions.
    """

    dependencies: ValidationDependencies
    run_impl: _ValidationRunImpl

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
        """Delegate to the injected implementation, converting terminations."""
        try:
            return self.run_impl(
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
