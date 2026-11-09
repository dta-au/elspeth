"""Graph-only conversational recovery contracts and local checkpoint qualification.

Qualification proves that the editor can represent a checkpoint, never that it
can execute. It performs no plugin construction, resource discovery or preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    _composer_node_id_validation_message,
    queue_node_contract_error,
    structural_node_plugin_error,
    validate_composer_output_name,
    validate_composer_source_name,
)
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_in_state

GraphRecoveryAction = Literal["park", "reset_to_turn_start", "reset_to_safe_checkpoint", "restore_parked_graph"]


def _require_recovery_action(value: object) -> None:
    """Check the runtime constructor boundary independently of Literal typing."""
    if type(value) is not str or value not in ("park", "reset_to_turn_start", "reset_to_safe_checkpoint", "restore_parked_graph"):
        raise ValueError("Graph recovery action is invalid")


@dataclass(frozen=True, slots=True)
class GraphRecoveryIntent:
    """One provider-authored terminal control, awaiting fenced settlement."""

    action: GraphRecoveryAction
    message: str
    park_id: str | None
    tool_call_id: str

    def __post_init__(self) -> None:
        _require_recovery_action(self.action)
        for value, limit, field_name in ((self.message, 16000, "message"), (self.tool_call_id, 1024, "tool_call_id")):
            if type(value) is not str or not value.strip() or len(value) > limit:
                raise ValueError(f"Graph recovery {field_name} must be a bounded nonblank string")
        if self.action == "restore_parked_graph":
            if type(self.park_id) is not str or not self.park_id.strip() or len(self.park_id) > 128:
                raise ValueError("Graph recovery restore requires a bounded nonblank park_id")
        elif self.park_id is not None:
            raise ValueError("Only parked graph restoration accepts park_id")


@dataclass(frozen=True, slots=True)
class GraphRecoveryPark:
    """Session-scoped bookmark identity; contains no graph option values."""

    park_id: str
    state_id: str
    version: int


@dataclass(frozen=True, slots=True)
class GraphRecoveryContext:
    """Trusted admission identities, usable even when graph hydration fails."""

    turn_start_state_id: str | None
    turn_start_version: int
    current_state_id: str | None
    current_version: int
    malformed_head: bool
    parked_graphs: tuple[GraphRecoveryPark, ...] = ()


@dataclass(frozen=True, slots=True)
class EditableCheckpointQualification:
    """A local editor verdict with categorical, non-disclosing failure codes."""

    eligible: bool
    reason_codes: tuple[str, ...] = ()


_SOURCE_SHAPE = TypeAdapter(SourceSpec)
_NODE_SHAPE = TypeAdapter(NodeSpec)
_EDGE_SHAPE = TypeAdapter(EdgeSpec)
_OUTPUT_SHAPE = TypeAdapter(OutputSpec)
_METADATA_SHAPE = TypeAdapter(PipelineMetadata)


def _checkpoint_has_owned_sections(state: CompositionState) -> bool:
    """Reject malformed section objects before calling their serializers."""
    return (
        type(state) is CompositionState
        and type(state.version) is int
        and state.version >= 1
        and type(state.metadata) is PipelineMetadata
        and type(state.sources) is MappingProxyType
        and all(type(name) is str and type(source) is SourceSpec for name, source in state.sources.items())
        and type(state.nodes) is tuple
        and all(type(node) is NodeSpec for node in state.nodes)
        and type(state.edges) is tuple
        and all(type(edge) is EdgeSpec for edge in state.edges)
        and type(state.outputs) is tuple
        and all(type(output) is OutputSpec for output in state.outputs)
    )


def _reconstruct_checkpoint_shapes(state: CompositionState) -> CompositionState:
    """Validate spec annotations without importing or constructing any plugin.

    The normal state loader trusts persisted scalar annotations. Recovery cannot
    trust a possibly malformed checkpoint, so it validates those annotations
    and then requires a byte-exact canonical round trip. That comparison rejects
    Pydantic coercions instead of silently repairing checkpoint content.
    """
    # ``deep_thaw`` also turns a frozenset into a list, which would hide an
    # unrepresentable authored value. Check option bags before that projection.
    for source in state.sources.values():
        canonical_json(source.options)
    for node in state.nodes:
        canonical_json(node.options)
        canonical_json(node.trigger)
    for output in state.outputs:
        canonical_json(output.options)
    payload = state.to_dict()
    # These six NodeSpec fields are required by its Python constructor but
    # represented by omission when absent on the canonical persistence wire.
    absent_node_fields = {"condition": None, "routes": None, "fork_to": None, "branches": None, "policy": None, "merge": None}
    reconstructed = CompositionState(
        version=state.version,
        metadata=_METADATA_SHAPE.validate_python(payload["metadata"]),
        sources={name: _SOURCE_SHAPE.validate_python(source) for name, source in payload["sources"].items()},
        nodes=tuple(_NODE_SHAPE.validate_python(absent_node_fields | node) for node in payload["nodes"]),
        edges=tuple(_EDGE_SHAPE.validate_python(edge) for edge in payload["edges"]),
        outputs=tuple(_OUTPUT_SHAPE.validate_python(output) for output in payload["outputs"]),
    )
    if canonical_json(payload) != canonical_json(reconstructed.to_dict()):
        raise ValueError("Recovery checkpoint shape does not round-trip exactly")
    return reconstructed


def _checkpoint_identity_failures(state: CompositionState) -> tuple[str, ...]:
    """Check intrinsic identities and editor references, allowing partial drafts."""
    failures: set[str] = set()
    source_names = set(state.sources)
    node_ids = {node.id for node in state.nodes}
    output_names = {output.name for output in state.outputs}
    if len(node_ids) != len(state.nodes):
        failures.add("duplicate_node_id")
    if len(output_names) != len(state.outputs):
        failures.add("duplicate_output_name")
    if len({edge.id for edge in state.edges}) != len(state.edges):
        failures.add("duplicate_edge_id")
    if source_names & node_ids or source_names & output_names or node_ids & output_names:
        failures.add("graph_namespace_collision")
    for source_name in source_names:
        try:
            validate_composer_source_name(source_name)
        except ValueError:
            failures.add("source_name_invalid")
    for output_name in output_names:
        try:
            validate_composer_output_name(output_name)
        except ValueError:
            failures.add("output_name_invalid")
    for node in state.nodes:
        if _composer_node_id_validation_message(node.id, node.node_type) is not None:
            failures.add("node_id_invalid")
        if node.id == "source" or node.id.startswith("source:"):
            failures.add("reserved_node_id")
        if structural_node_plugin_error(node) is not None:
            failures.add("structural_node_plugin_forbidden")
        if queue_node_contract_error(node) is not None:
            failures.add("queue_shape_invalid")
    for edge in state.edges:
        if not edge.id or not edge.id.strip():
            failures.add("edge_id_invalid")
        if edge.from_node not in source_names | node_ids or edge.to_node not in node_ids | output_names:
            failures.add("edge_unknown_node")
    return tuple(sorted(failures))


def qualify_editable_checkpoint(state: CompositionState) -> EditableCheckpointQualification:
    """Qualify an owned snapshot for graph editing, not execution or approval.

    Missing sources, sinks, routes, credentials, external resources and review
    decisions are legitimate unfinished work. Duplicate identities, malformed
    spec fields, dangling rendered edges and non-serializable option values
    cannot be faithfully restored into the editor. No candidate content or raw
    exception text is included in the returned diagnostic codes.

    An ineligible candidate is an explicit result: callers may abandon it and
    recover another checkpoint without capturing or persisting the bad draft.
    """
    if not _checkpoint_has_owned_sections(state):
        return EditableCheckpointQualification(False, ("checkpoint_sections_malformed",))
    try:
        reconstructed = _reconstruct_checkpoint_shapes(state)
    except ValidationError:
        return EditableCheckpointQualification(False, ("checkpoint_spec_malformed",))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return EditableCheckpointQualification(False, ("checkpoint_not_representable",))
    try:
        require_no_credential_material_in_state(reconstructed, surface="composer_recovery_checkpoint")
    except CredentialMaterialRefused:
        return EditableCheckpointQualification(False, ("checkpoint_credential_material",))
    failures = _checkpoint_identity_failures(reconstructed)
    return EditableCheckpointQualification(not failures, failures)
