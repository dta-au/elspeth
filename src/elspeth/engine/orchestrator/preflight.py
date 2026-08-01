"""Single-source-of-truth pipeline assembly + route-target preflight.

Both the composer ``/validate`` endpoint and the execution service's run path
must assemble :class:`PipelineConfig` identically and run the same four
route-target validators that the orchestrator runs at run-init. This module
owns that contract.

**Layer note.** This module sits in ``engine/`` (L2). It cannot import the L3
runtime plugin factory and therefore takes primitives instead of
``PluginBundle``. Both call sites unpack their bundle locally before calling.

**Mutation note.** Aggregation transforms have ``node_id`` assigned in this
helper (mirrors ``service.py`` runtime path). The mutation is intentional —
the orchestrator depends on ``node_id`` being set before run, and folding
aggregations into the transforms list is the canonical wiring step. Reorder
with care.

**Idempotency note.** The four validators are pure (no I/O, no mutation of
their inputs). The orchestrator runs them again at run-init
(``core.py:1746-1777`` and the resume site at ``:1940-1967``). Calling them
here in the composer/service surfaces failures earlier with a cleaner error
surface; the second call at run-init either passes again or raises the same
error.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast, final

from elspeth.contracts.errors import SinkEffectCapabilityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    AuditExportFormat,
    MemberSinkEffectCapability,
    ResolvedSinkEffectMode,
    RestagingSinkEffectCapability,
    SinkEffectContract,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectRuntimeBinding,
)
from elspeth.contracts.types import AggregationName
from elspeth.contracts.value_source import (
    CatalogValueSource,
    DerivedFromSiblingValueSource,
    UnknownCatalogIdError,
    ValueSource,
    find_value_source_config,
    get_catalog_missing_dep_hint,
    get_catalog_values,
)
from elspeth.core.config import resolve_config
from elspeth.engine.orchestrator.types import (
    PipelineConfig,
)
from elspeth.engine.orchestrator.validation import (
    validate_pipeline_route_targets,
)
from elspeth.engine.orchestrator.value_source_validation import ValueSourceFinding, ValueSourceValidationError

if TYPE_CHECKING:
    from elspeth.contracts import SinkEffectProtocol, SinkProtocol, SourceProtocol, TransformProtocol
    from elspeth.core.config import AggregationSettings, ElspethSettings
    from elspeth.core.dag.graph import ExecutionGraph
    from elspeth.core.dag.wiring import WiredTransform


def _effect_mode_guidance(sink_type: type[object]) -> str:
    """Read the required BaseSink remediation declaration without probing."""
    remediation = cast("type[SinkEffectProtocol]", sink_type).effect_mode_remediation
    if remediation is None:
        return ""
    if type(remediation) is not str or not remediation.strip():
        raise SinkEffectCapabilityError("Sink effect_mode_remediation must be None or a non-empty exact string")
    return f"; remediation: {remediation}"


def _validate_instance_extension_capabilities(sink: object, sink_name: object) -> None:
    if isinstance(sink, MemberSinkEffectCapability) and (
        type(sink).commit_member_effect is MemberSinkEffectCapability.commit_member_effect
        or type(sink).reconcile_member_effect is MemberSinkEffectCapability.reconcile_member_effect
        or not callable(sink.commit_member_effect)
        or not callable(sink.reconcile_member_effect)
    ):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} nominally declares durable member effects but does not implement "
            "callable commit_member_effect and reconcile_member_effect methods"
        )
    if isinstance(sink, RestagingSinkEffectCapability) and (
        type(sink).restage_effect is RestagingSinkEffectCapability.restage_effect or not callable(sink.restage_effect)
    ):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} nominally declares effect restaging but does not implement a callable restage_effect method"
        )


def _validate_type_extension_capabilities(sink_type: type[object], sink_name: object) -> None:
    if issubclass(sink_type, MemberSinkEffectCapability) and (
        sink_type.commit_member_effect is MemberSinkEffectCapability.commit_member_effect
        or sink_type.reconcile_member_effect is MemberSinkEffectCapability.reconcile_member_effect
    ):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} nominally declares durable member effects but does not implement "
            "commit_member_effect and reconcile_member_effect"
        )
    if issubclass(sink_type, RestagingSinkEffectCapability) and sink_type.restage_effect is RestagingSinkEffectCapability.restage_effect:
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} nominally declares effect restaging but does not implement restage_effect")


@final
class _SinkEffectCapabilityAdmission:
    """Opaque weak-key handle for closure-held admission authority.

    Same-process Python introspection is not a cryptographic boundary. The
    ordinary module-private surface nevertheless carries no issuer token,
    mutable authority slot, or constructor path that can mint a receipt.
    """

    __slots__ = ("__weakref__",)
    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(self) -> None:
        raise TypeError("Sink effect admission is validator-issued only")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("Sink effect admission is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise TypeError("Sink effect admission is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("Sink effect admission cannot be copied")

    def __deepcopy__(self, _memo: dict[int, object]) -> NoReturn:
        raise TypeError("Sink effect admission cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("Sink effect admission cannot be serialized")

    def __repr__(self) -> str:
        return "<SinkEffectCapabilityAdmission validator-issued>"


def validate_sink_effect_capability(
    sink: object,
    mode: str,
    required_input_kind: SinkEffectInputKind,
) -> None:
    """Validate only the sink's local, declarative effect capability surface.

    This check deliberately performs no plugin lifecycle call, credential
    resolution, target inspection, audit write, or external I/O.
    """
    if not isinstance(sink, SinkEffectContract):
        raise SinkEffectCapabilityError(
            f"Sink {type(sink).__name__!r} does not declare the required effect protocol "
            f"{SINK_EFFECT_PROTOCOL_VERSION!r}; legacy sink execution is unsafe"
        )
    effect_sink = cast("SinkEffectProtocol", sink)
    sink_type = cast("type[SinkEffectProtocol]", type(sink))
    sink_name = sink_type.name
    if not isinstance(required_input_kind, SinkEffectInputKind):
        raise SinkEffectCapabilityError("Sink effect required input kind must be an exact SinkEffectInputKind")
    protocol_version = sink_type.effect_protocol_version
    if protocol_version != SINK_EFFECT_PROTOCOL_VERSION:
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not declare the required effect protocol "
            f"{SINK_EFFECT_PROTOCOL_VERSION!r}; legacy sink execution is unsafe"
        )

    if not callable(effect_sink._validate_sink_effect_capability_configuration):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} local effect capability validator must be callable")
    effect_sink._validate_sink_effect_capability_configuration(
        mode=mode,
        required_input_kind=required_input_kind,
    )

    supported_modes = sink_type.supported_effect_modes
    if not isinstance(supported_modes, frozenset):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} supported_effect_modes must be an exact frozenset declaration")
    if not supported_modes or any(not isinstance(declared, str) or not declared.strip() for declared in supported_modes):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} must declare at least one non-empty supported effect mode")
    if not isinstance(mode, str) or not mode.strip():
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} requires a non-empty configured effect mode")
    if mode not in supported_modes:
        guidance = _effect_mode_guidance(sink_type)
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not support configured effect mode {mode!r}; declared modes: {sorted(supported_modes)!r}{guidance}"
        )

    supported_input_kinds = sink_type.supported_effect_input_kinds
    if not isinstance(supported_input_kinds, frozenset):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} supported_effect_input_kinds must be an exact frozenset declaration")
    if not supported_input_kinds:
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} must declare at least one supported effect input kind")
    if any(not isinstance(declared, SinkEffectInputKind) for declared in supported_input_kinds):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} supported_effect_input_kinds entries must be exact SinkEffectInputKind values")
    if required_input_kind not in supported_input_kinds:
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not support required effect input kind {required_input_kind.value!r}; "
            f"declared input kinds: {sorted(kind.value for kind in supported_input_kinds)!r}"
        )

    if not callable(effect_sink.inspect_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but inspect_effect is not callable"
        )
    if not callable(effect_sink.prepare_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but prepare_effect is not callable"
        )
    if not callable(effect_sink.commit_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but commit_effect is not callable"
        )
    if not callable(effect_sink.reconcile_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but reconcile_effect is not callable"
        )
    _validate_instance_extension_capabilities(sink, sink_name)


def validate_sink_effect_type_capability(
    sink_type: type[object],
    mode: str,
    required_input_kind: SinkEffectInputKind,
) -> None:
    """Validate an adapter class without constructing it or reading secrets."""
    if not issubclass(sink_type, SinkEffectContract):
        raise SinkEffectCapabilityError(
            f"Sink {sink_type.__name__!r} does not declare the required effect protocol "
            f"{SINK_EFFECT_PROTOCOL_VERSION!r}; legacy sink execution is unsafe"
        )
    effect_sink_type = cast("type[SinkEffectProtocol]", sink_type)
    sink_name = effect_sink_type.name
    if not isinstance(required_input_kind, SinkEffectInputKind):
        raise SinkEffectCapabilityError("Sink effect required input kind must be an exact SinkEffectInputKind")
    protocol_version = effect_sink_type.effect_protocol_version
    if protocol_version != SINK_EFFECT_PROTOCOL_VERSION:
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not declare the required effect protocol "
            f"{SINK_EFFECT_PROTOCOL_VERSION!r}; legacy sink execution is unsafe"
        )
    supported_modes = effect_sink_type.supported_effect_modes
    if not isinstance(supported_modes, frozenset):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} supported_effect_modes must be an exact frozenset declaration")
    if not supported_modes or any(not isinstance(declared, str) or not declared.strip() for declared in supported_modes):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} must declare at least one non-empty supported effect mode")
    if not isinstance(mode, str) or not mode.strip():
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} requires a non-empty configured effect mode")
    if mode not in supported_modes:
        guidance = _effect_mode_guidance(sink_type)
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not support configured effect mode {mode!r}; declared modes: {sorted(supported_modes)!r}{guidance}"
        )
    supported_input_kinds = effect_sink_type.supported_effect_input_kinds
    if not isinstance(supported_input_kinds, frozenset):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} supported_effect_input_kinds must be an exact frozenset declaration")
    if not supported_input_kinds:
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} must declare at least one supported effect input kind")
    if any(not isinstance(declared, SinkEffectInputKind) for declared in supported_input_kinds):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} supported_effect_input_kinds entries must be exact SinkEffectInputKind values")
    if required_input_kind not in supported_input_kinds:
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not support required effect input kind {required_input_kind.value!r}; "
            f"declared input kinds: {sorted(kind.value for kind in supported_input_kinds)!r}"
        )
    if not callable(effect_sink_type.inspect_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but inspect_effect is not callable"
        )
    if not callable(effect_sink_type.prepare_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but prepare_effect is not callable"
        )
    if not callable(effect_sink_type.commit_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but commit_effect is not callable"
        )
    if not callable(effect_sink_type.reconcile_effect):
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} declares effect protocol {SINK_EFFECT_PROTOCOL_VERSION!r} but reconcile_effect is not callable"
        )
    _validate_type_extension_capabilities(sink_type, sink_name)


def validate_audit_export_sink_type_capability(
    sink_type: type[object],
    export_format: AuditExportFormat,
) -> None:
    """Require one explicit closed audit-export serialization declaration."""
    if type(export_format) is not AuditExportFormat:
        raise SinkEffectCapabilityError("Audit export format must be an exact AuditExportFormat")
    sink_name = cast("type[SinkEffectProtocol]", sink_type).name
    supported = sink_type.supported_audit_export_formats  # type: ignore[attr-defined]
    if not isinstance(supported, frozenset) or any(type(item) is not AuditExportFormat for item in supported):
        raise SinkEffectCapabilityError(f"Sink {sink_name!r} must explicitly declare exact supported_audit_export_formats")
    if export_format not in supported:
        raise SinkEffectCapabilityError(
            f"Sink {sink_name!r} does not support audit export format {export_format.value!r}; "
            f"declared formats: {sorted(item.value for item in supported)!r}"
        )


def sink_effect_modes_from_runtime_bindings(
    sinks: Mapping[str, SinkProtocol],
    bindings: Mapping[str, SinkEffectRuntimeBinding],
    *,
    purpose: SinkEffectExecutionPurpose,
    configured_options: Mapping[str, Mapping[str, object]],
) -> Mapping[str, str]:
    """Derive modes only from bindings owned by the real runtime factory."""
    if set(bindings) != set(sinks) or set(configured_options) != set(sinks):
        raise SinkEffectCapabilityError("Sink effect runtime bindings and configured options must exactly match runtime sink names")
    modes: dict[str, str] = {}
    for sink_name, sink in sinks.items():
        binding = bindings[sink_name]
        options = dict(configured_options[sink_name])
        if (
            type(binding) is not SinkEffectRuntimeBinding
            or binding.sink_name != sink_name
            or binding.sink is not sink
            or binding.sink_type is not type(sink)
            or binding.purpose is not purpose
            or binding.config_fingerprint != stable_hash(options)
        ):
            raise SinkEffectCapabilityError(
                f"Sink effect runtime binding for {sink_name!r} does not bind the exact sink, type, name, config, and execution purpose"
            )
        if not isinstance(sink, SinkEffectContract):
            raise SinkEffectCapabilityError(
                f"Sink {sink_name!r} does not declare the required effect protocol "
                f"{SINK_EFFECT_PROTOCOL_VERSION!r}; legacy sink execution is unsafe"
            )
        sink_type = cast("type[SinkEffectProtocol]", type(sink))
        resolved_mode = sink_type._resolve_sink_effect_mode(options, purpose=purpose)
        if resolved_mode is not None and type(resolved_mode) is not ResolvedSinkEffectMode:
            raise SinkEffectCapabilityError(f"Sink effect mode resolver for {sink_name!r} must return ResolvedSinkEffectMode or None")
        if binding.effect_mode != resolved_mode:
            raise SinkEffectCapabilityError(
                f"Sink effect runtime binding for {sink_name!r} claimed effect mode does not match adapter-resolved config authority"
            )
        if resolved_mode is not None:
            modes[sink_name] = resolved_mode.value
    return modes


def _capability_fingerprint(sink: object) -> tuple[object, ...]:
    effect_sink = cast("SinkEffectProtocol", sink)
    sink_type = cast("type[SinkEffectProtocol]", type(sink))
    member_methods: tuple[object, ...]
    if isinstance(sink, MemberSinkEffectCapability):
        member_capable = True
        member_methods = (sink.commit_member_effect, sink.reconcile_member_effect)
    else:
        member_capable = False
        member_methods = ()
    restaging_methods: tuple[object, ...]
    if isinstance(sink, RestagingSinkEffectCapability):
        restaging_capable = True
        restaging_methods = (sink.restage_effect,)
    else:
        restaging_capable = False
        restaging_methods = ()
    return (
        sink_type.effect_protocol_version,
        sink_type.supported_effect_modes,
        sink_type.supported_effect_input_kinds,
        sink_type.effect_mode_remediation,
        member_capable,
        restaging_capable,
        effect_sink._validate_sink_effect_capability_configuration,
        effect_sink.inspect_effect,
        effect_sink.prepare_effect,
        effect_sink.commit_effect,
        effect_sink.reconcile_effect,
        *member_methods,
        *restaging_methods,
    )


def _build_sink_effect_admission_authority() -> tuple[
    Callable[..., _SinkEffectCapabilityAdmission],
    Callable[..., bool],
]:
    """Create process-local issue/lookup closures around hidden authority."""

    @dataclass(frozen=True, slots=True, repr=False)
    class _AdmissionBinding:
        name: str
        sink: object
        mode: str
        capability_fingerprint: tuple[object, ...]

    @dataclass(frozen=True, slots=True, repr=False)
    class _AdmissionRecord:
        required_input_kind: SinkEffectInputKind
        bindings: tuple[_AdmissionBinding, ...]

    registry: weakref.WeakKeyDictionary[_SinkEffectCapabilityAdmission, _AdmissionRecord] = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def issue(
        sinks: Mapping[str, SinkProtocol],
        *,
        configured_modes: Mapping[str, str],
        required_input_kind: SinkEffectInputKind,
    ) -> _SinkEffectCapabilityAdmission:
        extra_modes = set(configured_modes) - set(sinks)
        if extra_modes:
            raise SinkEffectCapabilityError(f"Sink effect configured modes contain non-runtime sink names: {sorted(extra_modes)!r}")
        bindings: list[_AdmissionBinding] = []
        for sink_name, sink in sinks.items():
            if sink_name in configured_modes:
                mode = configured_modes[sink_name]
            else:
                mode = ""
            validate_sink_effect_capability(
                sink,
                mode=mode,
                required_input_kind=required_input_kind,
            )
            bindings.append(
                _AdmissionBinding(
                    name=sink_name,
                    sink=sink,
                    mode=mode,
                    capability_fingerprint=_capability_fingerprint(sink),
                )
            )
        receipt = object.__new__(_SinkEffectCapabilityAdmission)
        record = _AdmissionRecord(
            required_input_kind=required_input_kind,
            bindings=tuple(bindings),
        )
        with lock:
            registry[receipt] = record
        return receipt

    def lookup(
        receipt: object,
        sinks: Mapping[str, SinkProtocol],
        configured_modes: Mapping[str, str],
        required_input_kind: SinkEffectInputKind,
    ) -> bool:
        if type(receipt) is not _SinkEffectCapabilityAdmission:
            return False
        with lock:
            if receipt not in registry:
                return False
            record = registry[receipt]
        if record.required_input_kind is not required_input_kind:
            return False
        if set(configured_modes) != set(sinks) or len(record.bindings) != len(sinks):
            return False
        for binding, (sink_name, sink) in zip(record.bindings, sinks.items(), strict=True):
            if sink_name in configured_modes:
                mode = configured_modes[sink_name]
            else:
                mode = ""
            if (
                binding.name != sink_name
                or binding.sink is not sink
                or binding.mode != mode
                or binding.capability_fingerprint != _capability_fingerprint(sink)
            ):
                return False
        return True

    return issue, lookup


_issue_sink_effect_admission, _lookup_sink_effect_admission = _build_sink_effect_admission_authority()
del _build_sink_effect_admission_authority


def validate_pipeline_sink_effect_capabilities(
    sinks: Mapping[str, SinkProtocol],
    *,
    configured_modes: Mapping[str, str],
    required_input_kind: SinkEffectInputKind,
) -> _SinkEffectCapabilityAdmission:
    """Validate every resolved sink before per-run context/lifecycle setup."""
    return _issue_sink_effect_admission(
        sinks,
        configured_modes=configured_modes,
        required_input_kind=required_input_kind,
    )


def require_sink_effect_admission(
    sinks: Mapping[str, SinkProtocol],
    *,
    configured_modes: Mapping[str, str],
    required_input_kind: SinkEffectInputKind,
    admission: object | None,
) -> _SinkEffectCapabilityAdmission:
    """Accept one exact prior proof or perform the one production validation."""
    if admission is None:
        return validate_pipeline_sink_effect_capabilities(
            sinks,
            configured_modes=configured_modes,
            required_input_kind=required_input_kind,
        )
    if _lookup_sink_effect_admission(admission, sinks, configured_modes, required_input_kind):
        return cast(_SinkEffectCapabilityAdmission, admission)
    raise SinkEffectCapabilityError(
        "Sink effect admission is not validator-issued and does not bind the exact runtime sinks, modes, capability, and input kind"
    )


def execution_sinks_for_runtime(
    settings: ElspethSettings,
    sinks: Mapping[str, SinkProtocol],
) -> Mapping[str, SinkProtocol]:
    """Project out the delayed post-run export sink from pipeline execution."""
    export = settings.landscape.export
    if export.enabled and export.sink:
        return {name: sink for name, sink in sinks.items() if name != export.sink}
    return dict(sinks)


def execution_sink_modes_for_runtime(
    settings: ElspethSettings,
    configured_modes: Mapping[str, str],
) -> Mapping[str, str]:
    """Apply the same delayed-export projection to explicit resolved modes."""
    export = settings.landscape.export
    if export.enabled and export.sink:
        return {name: mode for name, mode in configured_modes.items() if name != export.sink}
    return dict(configured_modes)


def execution_sink_bindings_for_runtime(
    settings: ElspethSettings,
    bindings: Mapping[str, SinkEffectRuntimeBinding],
) -> Mapping[str, SinkEffectRuntimeBinding]:
    """Apply the delayed-export projection to factory-owned bindings."""
    export = settings.landscape.export
    if export.enabled and export.sink:
        return {name: binding for name, binding in bindings.items() if name != export.sink}
    return dict(bindings)


def assemble_and_validate_pipeline_config(
    *,
    sources: Mapping[str, SourceProtocol],
    transforms: Sequence[WiredTransform],
    sinks: Mapping[str, SinkProtocol],
    aggregations: Mapping[str, tuple[TransformProtocol, AggregationSettings]],
    settings: ElspethSettings,
    graph: ExecutionGraph,
    sink_effect_modes: Mapping[str, str] | None = None,
    sink_effect_admission: object | None = None,
) -> PipelineConfig:
    """Fold aggregations into transforms, build :class:`PipelineConfig`, and
    run the four orchestrator route-target validators.

    Mirrors the assembly logic in ``src/elspeth/web/execution/service.py`` and
    the four-validator pre-init call site in
    ``src/elspeth/engine/orchestrator/core.py``.

    Args:
        sources: Named source plugin instances (one or more) per ADR-025 §1.
        transforms: Wired transforms from ``PluginBundle.transforms``.
        sinks: Sink instances keyed by name from ``PluginBundle.sinks``.
        aggregations: Aggregation transforms + settings keyed by aggregation
            name (from ``PluginBundle.aggregations``).
        settings: Full ``ElspethSettings`` (provides ``gates`` and
            ``coalesce`` settings).
        graph: ``ExecutionGraph`` (provides ``aggregation_id_map`` for
            ``node_id`` assignment).

    Returns:
        Assembled ``PipelineConfig`` with aggregations folded into transforms.

    Raises:
        RouteValidationError: A route target references a non-existent sink,
            or a sink failsink violates one of the failsink rules.
        OrchestrationInvariantError: A framework invariant has been broken
            (e.g. a transform ``on_error`` is ``None`` when ``TransformSettings``
            requires it). This is a programmer-bug class — callers in the
            composer/service path should let it propagate to a 500 rather than
            translating it to a per-pipeline validation failure.
    """
    all_transforms: list[TransformProtocol] = [t.plugin for t in transforms]
    aggregation_settings: dict[str, AggregationSettings] = {}
    agg_id_map = graph.get_aggregation_id_map()

    for agg_name, (transform, agg_config) in aggregations.items():
        node_id = agg_id_map[AggregationName(agg_name)]
        aggregation_settings[node_id] = agg_config
        # Intentional mutation: aggregation transforms need node_id set so
        # the orchestrator can index aggregation_settings by node_id at run
        # time. Mirrors service.py:663-667.
        transform.node_id = node_id
        all_transforms.append(transform)

    execution_sinks = execution_sinks_for_runtime(settings, sinks)
    execution_modes = execution_sink_modes_for_runtime(settings, sink_effect_modes or {})
    pipeline_config = PipelineConfig(
        sources=sources,
        transforms=all_transforms,
        sinks=execution_sinks,
        config=resolve_config(settings),
        gates=list(settings.gates),
        aggregation_settings=aggregation_settings,
        coalesce_settings=(list(settings.coalesce) if settings.coalesce else []),
        sink_effect_modes=execution_modes,
        sink_effect_admission=sink_effect_admission,
    )

    validate_pipeline_route_targets(
        config=pipeline_config,
        route_resolution_map=graph.get_route_resolution_map(),
        transform_id_map=graph.get_transform_id_map(),
        config_gate_id_map=graph.get_config_gate_id_map(),
    )

    # NB: Value-source compliance is enforced upstream in
    # ``runtime_factory.instantiate_plugins_from_config`` — by the time the
    # bundle reaches this function, declared field values have already
    # been validated against their VALUE_SOURCES contracts. Re-running
    # the walker here would be redundant.

    return pipeline_config


def validate_value_source_compliance(transforms: Sequence[WiredTransform]) -> None:
    """Reject pipelines whose plugin configs violate VALUE_SOURCES declarations.

    Walks each transform's typed config (via the plugin's public ``config``
    accessor when present) and dispatches each
    :class:`elspeth.contracts.value_source.ValueSource` declaration:

    * :class:`CatalogValueSource` — ``getattr(config, field_name)`` must
      appear in the catalog resolved by
      :func:`elspeth.contracts.value_source.get_catalog_values`. An
      empty catalog (e.g. optional dependency missing) is itself a
      structured failure, never a silent pass.
    * :class:`DerivedFromSiblingValueSource` — the field value must equal
      the sibling field's value. With ``allow_empty_default=True``, an
      empty (``""`` / ``None``) value is also accepted.

    Plugins that do not expose a ``config`` attribute, or whose config
    class has no ``VALUE_SOURCES`` ClassVar, are skipped (they have no
    declarations to enforce).

    Raises:
        ValueSourceValidationError: One or more declared sources rejected
            their field's value. The exception's ``findings`` tuple
            carries one :class:`ValueSourceFinding` per offending field,
            with structured ``component_id`` / ``field_name`` / ``reason``
            attributes; the composer ``/validate`` path reads them
            directly to build per-component ``ValidationError`` records.
        UnknownCatalogIdError: A declaration references a catalog id with
            no registered reader. This is a programmer-bug class
            (declaration was wired but plugin pack didn't register the
            reader); callers should let it propagate to a 500.
    """
    findings: list[ValueSourceFinding] = []
    for wired in transforms:
        # The L0 registry returns the typed config for plugins that have
        # explicitly opted into value-source compliance via
        # ``register_value_source_plugin``; ``None`` for everything else.
        # Explicit opt-in instead of duck-typing avoids defensive
        # getattr/hasattr/isinstance patterns and makes the contract
        # discoverable at plugin-pack import time.
        config = find_value_source_config(wired.plugin)
        if config is None:
            continue
        config_cls = type(config)
        # The discriminated-union declarations live on the config class
        # as a ``VALUE_SOURCES`` ClassVar. A registered plugin whose
        # config class has no declarations is a plugin-pack contract
        # bug — let AttributeError surface rather than silently passing.
        # mypy narrows ``type(config)`` to ``type[object]`` and cannot
        # see the ClassVar declared by L3 plugin packs; ``# type: ignore``
        # documents the intentional contract break.
        declarations: tuple[ValueSource, ...] = config_cls.VALUE_SOURCES  # type: ignore[attr-defined]
        if not declarations:
            continue
        # ``settings.name`` is the operator-facing transform identifier
        # (e.g. ``"openrouter_llm_node_1"``) — pinned into each finding
        # so the composer can attribute errors to a specific component
        # without re-walking the bundle.
        component_id = wired.settings.name
        for declaration in declarations:
            finding = _check_value_source(declaration, config, component_id)
            if finding is not None:
                findings.append(finding)
    if findings:
        message = f"{len(findings)} field(s) violated value-source declarations: " + "; ".join(f.format() for f in findings)
        raise ValueSourceValidationError(message, findings=tuple(findings))


def check_config_value_sources(config: object, *, component_id: str) -> tuple[ValueSourceFinding, ...]:
    """Run one already-constructed config's ``VALUE_SOURCES`` declarations.

    Per-config counterpart to :func:`validate_value_source_compliance` (which
    walks a wired bundle). It lets per-node callers — notably the composer's
    pre-wiring option prevalidation — surface the same structured findings
    (catalog membership, sibling derivation) without constructing a bundle, so a
    hallucinated catalog value (e.g. an unknown OpenRouter ``model``) is caught
    at authoring time with an actionable ``list_models`` hint rather than only at
    instantiation. Returns ``()`` when the config's class declares no protocols.
    """
    return tuple(
        finding
        for declaration in _declared_value_sources(type(config))
        if (finding := _check_value_source(declaration, config, component_id)) is not None
    )


def _declared_value_sources(config_cls: type) -> tuple[ValueSource, ...]:
    """Return ``config_cls``'s ``VALUE_SOURCES`` ClassVar, or ``()`` if it declares none.

    ``VALUE_SOURCES`` is an optional ClassVar — config classes with no
    value-source protocols omit it entirely (see
    :mod:`elspeth.contracts.value_source`). Walk the MRO's class ``__dict__``
    directly so a genuinely-absent declaration is distinguished from a
    present-but-empty one, without ``getattr(..., default)`` defensively
    suppressing the attribute.
    """
    for klass in config_cls.__mro__:
        if "VALUE_SOURCES" in klass.__dict__:
            return cast("tuple[ValueSource, ...]", klass.__dict__["VALUE_SOURCES"])
    return ()


def _check_value_source(
    declaration: ValueSource,
    config: object,
    component_id: str,
) -> ValueSourceFinding | None:
    """Run a single declaration against ``config``; return None on pass.

    Dispatches on the concrete variant of the discriminated union via
    ``match``/``case`` (structural dispatch). Returns a structured
    :class:`ValueSourceFinding` so the L3 consumer can read
    ``component_id``/``field_name``/``reason`` directly without parsing
    a formatted string.
    """
    match declaration:
        case CatalogValueSource():
            return _check_catalog_membership(declaration, config, component_id)
        case DerivedFromSiblingValueSource():
            return _check_derived_from_sibling(declaration, config, component_id)
        case _:
            raise TypeError(f"Unknown ValueSource variant {type(declaration).__name__} on {component_id!r}: {declaration!r}")


def _check_catalog_membership(
    declaration: CatalogValueSource,
    config: object,
    component_id: str,
) -> ValueSourceFinding | None:
    # ``applies_when`` predicate: catalog membership is conditional on
    # sibling field values. If any predicate pair doesn't match, the
    # catalog isn't authoritative for this config (e.g. OpenRouter
    # base_url overridden to a chaos test endpoint) — skip the check
    # rather than rejecting values the upstream endpoint would accept.
    for sibling_field, expected_value in declaration.applies_when:
        actual_value = _read_field(config, sibling_field)
        if actual_value != expected_value:
            return None
    value = _read_field(config, declaration.field_name)
    catalog = get_catalog_values(declaration.catalog_id)
    if not catalog:
        # Quote the registrar's actionable hint verbatim when present so
        # the operator sees the specific install command instead of a
        # generic "install the optional dependency". Falls back to the
        # generic message when no hint was registered.
        dep_hint = get_catalog_missing_dep_hint(declaration.catalog_id)
        remediation = (
            dep_hint
            if dep_hint is not None
            else ("install the optional dependency that provides the catalog or pin a static catalog snapshot")
        )
        return ValueSourceFinding(
            component_id=component_id,
            field_name=declaration.field_name,
            reason=(f"catalog '{declaration.catalog_id}' is empty or unavailable; cannot verify field value ({remediation})"),
        )
    # ``value`` is a string for the LLM ``model`` field today; for non-string
    # values we structurally reject (type mismatch is a Pydantic-level fault
    # caught earlier — defense-in-depth here).
    match value:
        case str() if value in catalog:
            return None
        case _:
            return ValueSourceFinding(
                component_id=component_id,
                field_name=declaration.field_name,
                reason=(
                    f"value {value!r} is not in catalog '{declaration.catalog_id}' "
                    f"(catalog has {len(catalog)} entries; pick a valid value via the "
                    "list_models composer tool)"
                ),
            )


def _check_derived_from_sibling(
    declaration: DerivedFromSiblingValueSource,
    config: object,
    component_id: str,
) -> ValueSourceFinding | None:
    field_value = _read_field(config, declaration.field_name)
    sibling_value = _read_field(config, declaration.sibling_field)
    if declaration.allow_empty_default and (field_value is None or field_value == ""):
        return None
    if field_value == sibling_value:
        return None
    return ValueSourceFinding(
        component_id=component_id,
        field_name=declaration.field_name,
        reason=(
            f"value {field_value!r} must equal sibling "
            f"'{declaration.sibling_field}' (currently {sibling_value!r})"
            + ("; leave the field empty to inherit the sibling value" if declaration.allow_empty_default else "")
        ),
    )


def _read_field(config: object, field_name: str) -> object:
    """Read a Pydantic config field by name without ``getattr`` defensive default.

    The walker reads field names declared by the plugin's own
    ``VALUE_SOURCES``. A declared field that does not exist on the config
    is a contract bug in the plugin pack — let the AttributeError surface
    rather than silently substituting a default.
    """
    return config.__getattribute__(field_name)


# Re-export for L3 callers that translate structured findings into per-component
# ValidationError records (composer /validate). UnknownCatalogIdError is
# intentionally NOT caught by the walker — it surfaces unconfigured catalogs
# as 500-class programmer bugs, not per-pipeline validation failures.
__all__ = [
    "ResolvedSinkEffectMode",
    "SinkEffectCapabilityError",
    "SinkEffectExecutionPurpose",
    "SinkEffectRuntimeBinding",
    "UnknownCatalogIdError",
    "assemble_and_validate_pipeline_config",
    "execution_sink_bindings_for_runtime",
    "execution_sink_modes_for_runtime",
    "execution_sinks_for_runtime",
    "require_sink_effect_admission",
    "sink_effect_modes_from_runtime_bindings",
    "validate_pipeline_sink_effect_capabilities",
    "validate_sink_effect_capability",
    "validate_sink_effect_type_capability",
    "validate_value_source_compliance",
]
