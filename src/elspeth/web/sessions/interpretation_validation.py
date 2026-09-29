"""Closed profile facts consumed while interpretation state rows are locked.

Capture catalog schemas and per-principal profile bindings before mutation.
The retained capability contains immutable scalars and tuples only. Temporary
schema/profile values are reconstructed for the existing pure validator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import final

from pydantic import TypeAdapter

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.state import CompositionState
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import DetachedProfileLowering, LoweredPluginConfig, OperatorProfileRegistry
from elspeth.web.plugin_policy.validation import ProfileAwareValidationResult, validate_authored_composition_state


def _assert_closed_plugin_identity(kind: object, name: object) -> None:
    """Reject runtime-bearing scalar subclasses in a retained owned identity."""
    if type(kind) is not str or type(name) is not str:
        raise TypeError("interpretation validation plugin identity must contain exact strings")


@final
@dataclass(frozen=True, slots=True)
class _PluginValidationFacts:
    plugin_id: PluginId
    full_schema_json: str = field(repr=False)
    public_schema_json: str = field(repr=False)
    profiles: tuple[tuple[str, DetachedProfileLowering], ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.plugin_id) is not PluginId:
            raise TypeError("interpretation validation plugin id must be exact")
        _assert_closed_plugin_identity(self.plugin_id.kind, self.plugin_id.name)
        if type(self.full_schema_json) is not str or type(self.public_schema_json) is not str:
            raise TypeError("interpretation validation schemas must be exact JSON strings")
        if type(self.profiles) is not tuple:
            raise TypeError("interpretation validation profiles must be an exact tuple")
        for item in self.profiles:
            if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not DetachedProfileLowering:
                raise TypeError("interpretation validation profile binding must be exact")
        aliases = tuple(alias for alias, _lowering in self.profiles)
        if len(set(aliases)) != len(aliases):
            raise AuditIntegrityError("interpretation validation profile aliases must be unique")


@final
@dataclass(frozen=True, slots=True)
class SessionInterpretationValidationInputs:
    """Detached immutable authority for one principal's policy snapshot."""

    snapshot_json: str | None = field(repr=False)
    plugins: tuple[_PluginValidationFacts, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.snapshot_json is not None and type(self.snapshot_json) is not str:
            raise TypeError("interpretation validation snapshot must be an exact JSON string")
        if type(self.plugins) is not tuple or any(type(plugin) is not _PluginValidationFacts for plugin in self.plugins):
            raise TypeError("interpretation validation plugins must be an exact tuple")
        ids = tuple(plugin.plugin_id for plugin in self.plugins)
        if ids != tuple(sorted(set(ids))):
            raise AuditIntegrityError("interpretation validation plugin facts must be unique and sorted")
        if self.snapshot_json is None and self.plugins:
            raise AuditIntegrityError("unconfigured interpretation validation cannot retain profile facts")

    @property
    def plugin_snapshot(self) -> PluginAvailabilitySnapshot | None:
        if self.snapshot_json is None:
            return None
        return TypeAdapter(PluginAvailabilitySnapshot).validate_json(self.snapshot_json, strict=True)

    def get_schema(self, plugin_type: PluginKind, name: str) -> PluginSchemaInfo:
        plugin_id = PluginId(plugin_type, name)
        for entry in self.plugins:
            if entry.plugin_id == plugin_id:
                return PluginSchemaInfo.model_validate_json(entry.full_schema_json, strict=True)
        raise ValueError(f"Unknown {plugin_type} plugin: {name}")

    def public_schema(self, plugin_id: PluginId, full_schema: PluginSchemaInfo, *, available_aliases: tuple[str, ...]) -> PluginSchemaInfo:
        for entry in self.plugins:
            if entry.plugin_id == plugin_id:
                if tuple(alias for alias, _lowering in entry.profiles) != available_aliases:
                    raise AuditIntegrityError("interpretation validation aliases disagree with the captured snapshot")
                if full_schema.name != plugin_id.name or full_schema.plugin_type != plugin_id.kind:
                    raise AuditIntegrityError("interpretation validation schema identity disagrees with the captured plugin")
                return PluginSchemaInfo.model_validate_json(entry.public_schema_json, strict=True)
        raise AuditIntegrityError("interpretation validation public schema is absent")

    def lower_options(self, plugin_id: PluginId, *, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        for entry in self.plugins:
            if entry.plugin_id == plugin_id:
                for captured_alias, lowering in entry.profiles:
                    if captured_alias == alias:
                        return lowering.lower_options(alias=alias, safe_options=safe_options)
                raise ValueError("profile_unavailable")
        raise AuditIntegrityError("interpretation validation plugin binding is absent")


def build_interpretation_validation_inputs(
    *,
    profile_aware: bool,
    plugin_snapshot: PluginAvailabilitySnapshot | None,
    profile_registry: OperatorProfileRegistry | None,
    catalog: CatalogService | None,
) -> SessionInterpretationValidationInputs:
    """Copy exact owned services into a closed policy before database mutation."""
    if type(profile_aware) is not bool:
        raise TypeError("profile_aware must be an exact boolean")
    if plugin_snapshot is not None and type(plugin_snapshot) is not PluginAvailabilitySnapshot:
        raise TypeError("plugin_snapshot must be an exact PluginAvailabilitySnapshot")
    if profile_registry is not None and type(profile_registry) is not OperatorProfileRegistry:
        raise TypeError("profile_registry must be an exact OperatorProfileRegistry")
    if catalog is not None and type(catalog) is not CatalogServiceImpl:
        raise TypeError("catalog must be an exact CatalogServiceImpl")
    if not profile_aware:
        return SessionInterpretationValidationInputs(None, ())
    if plugin_snapshot is None or profile_registry is None or catalog is None:
        raise AuditIntegrityError("profile-aware interpretation validation dependencies are unavailable")
    snapshot_json = TypeAdapter(PluginAvailabilitySnapshot).dump_json(plugin_snapshot, warnings="error").decode()
    copied_snapshot = TypeAdapter(PluginAvailabilitySnapshot).validate_json(snapshot_json, strict=True)
    if copied_snapshot != plugin_snapshot:
        raise AuditIntegrityError("interpretation validation snapshot changed during detachment")
    # Re-derive the binding hash, so malformed retained authority cannot enter
    # the mutation through a hand-constructed owned snapshot.
    canonical = PluginAvailabilitySnapshot.create(
        policy_hash=copied_snapshot.policy_hash,
        principal_scope=copied_snapshot.principal_scope,
        available=copied_snapshot.available,
        unavailable=copied_snapshot.unavailable,
        selected=copied_snapshot.selected,
        usable_profile_aliases=copied_snapshot.usable_profile_aliases,
        selected_profile_aliases=copied_snapshot.selected_profile_aliases,
        control_modes=copied_snapshot.control_modes,
        binding_generation_fingerprint=copied_snapshot.binding_generation_fingerprint,
        authority=copied_snapshot.authority,
    )
    if canonical.snapshot_hash != copied_snapshot.snapshot_hash:
        raise AuditIntegrityError("interpretation validation snapshot has invalid canonical authority")
    aliases_by_plugin = dict(copied_snapshot.usable_profile_aliases)
    entries: list[_PluginValidationFacts] = []
    if not copied_snapshot.is_trained_operator:
        for plugin_id in sorted(copied_snapshot.available):
            full_schema = catalog.get_schema(plugin_id.kind, plugin_id.name)
            if type(full_schema) is not PluginSchemaInfo:
                raise TypeError("interpretation validation catalog schema must be exact")
            aliases = aliases_by_plugin[plugin_id] if plugin_id in aliases_by_plugin else ()
            public_schema = profile_registry.public_schema(plugin_id, full_schema, available_aliases=aliases)
            if type(public_schema) is not PluginSchemaInfo:
                raise TypeError("interpretation validation public schema must be exact")
            entries.append(
                _PluginValidationFacts(
                    plugin_id=plugin_id,
                    full_schema_json=full_schema.model_dump_json(warnings="error"),
                    public_schema_json=public_schema.model_dump_json(warnings="error"),
                    profiles=tuple((alias, profile_registry.detach_validation_lowering(plugin_id, alias=alias)) for alias in aliases),
                )
            )
    return SessionInterpretationValidationInputs(snapshot_json, tuple(entries))


def validate_composition_state_with_interpretation_inputs(
    state: CompositionState, inputs: SessionInterpretationValidationInputs
) -> ProfileAwareValidationResult:
    if type(state) is not CompositionState or type(inputs) is not SessionInterpretationValidationInputs:
        raise TypeError("interpretation validation requires exact owned state and inputs")
    snapshot = inputs.plugin_snapshot
    if snapshot is None:
        return ProfileAwareValidationResult(authored_state=state, executable_state=state, policy_findings=(), validation=state.validate())
    return validate_authored_composition_state(state, snapshot=snapshot, profile_registry=inputs, catalog=inputs)
