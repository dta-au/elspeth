"""Structured refusal at Web and Composer credential-material boundaries."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypedDict

from pydantic import ValidationError

from elspeth.contracts.credential_material import (
    CREDENTIAL_REFUSAL_DETAIL as CREDENTIAL_REFUSAL_DETAIL,
)
from elspeth.contracts.credential_material import (
    DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
    CredentialMaterialFinding,
    CredentialTraversalPolicy,
    find_credential_material,
    is_secret_field,
)
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.bounded_json import (
    JSON_MAX_TOTAL_UTF8_BYTES,
    JsonBoundaryError,
    _validate_json_structure,
    bounded_json_loads,
)
from elspeth.web.composer.protocol import ComposerAdmissionRefused
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields

if TYPE_CHECKING:
    from elspeth.web.composer.state import CompositionState

_OUT_OF_SCOPE_DATA_PLANE_CONTENT = "<data-plane-content>"
_MALFORMED_TOOL_WIRE = object()


class CredentialMaterialRefusalPayload(TypedDict):
    """Stable, candidate-free response and audit evidence."""

    error_type: str
    failure_code: str
    detail: str
    category: str
    surface: str
    detector_version: str


class CredentialMaterialRefused(ComposerAdmissionRefused):
    """In-scope control content contained recognized credential material."""

    def __init__(self, *, surface: str, finding: CredentialMaterialFinding) -> None:
        self.surface = surface
        self.finding = finding
        super().__init__(CREDENTIAL_REFUSAL_DETAIL)

    def to_payload(self) -> CredentialMaterialRefusalPayload:
        return {
            "error_type": "credential_material_rejected",
            "failure_code": "credential_material_rejected",
            "detail": CREDENTIAL_REFUSAL_DETAIL,
            "category": self.finding.category,
            "surface": self.surface,
            "detector_version": self.finding.detector_version,
        }


def require_no_credential_material(
    value: Any,
    *,
    surface: str,
    env_ref_names: Collection[str] = frozenset(),
    additional_credential_fields: Collection[str] = frozenset(),
    max_string_bytes: int = 262_144,
) -> None:
    """Atomically reject one bounded control-plane value before side effects."""
    finding = find_credential_material(
        value,
        CredentialTraversalPolicy(
            env_ref_names=env_ref_names,
            additional_credential_fields=additional_credential_fields,
            max_string_bytes=max_string_bytes,
        ),
    )
    if finding is not None:
        raise CredentialMaterialRefused(surface=surface, finding=finding)


def require_no_credential_material_in_llm_metadata(
    *,
    content: str | None,
    tool_calls: Sequence[tuple[str, str]],
    reasoning_content: str | None,
    reasoning_details: Any | None,
    thinking_blocks: Any | None,
    model_returned: str | None,
    provider_request_id: str | None,
    finish_reason: str | None,
    provider_served: str | None,
    surface: str,
) -> None:
    """Reject every provider-authored text field that can be audited or replayed."""
    require_no_credential_material(
        {
            "content": content,
            "tool_calls": [{"id": call_id, "name": name} for call_id, name in tool_calls],
            "reasoning_content": reasoning_content,
            "reasoning_details": reasoning_details,
            "thinking_blocks": thinking_blocks,
            "model_returned": model_returned,
            "provider_request_id": provider_request_id,
            "finish_reason": finish_reason,
            "provider_served": provider_served,
        },
        surface=surface,
    )


def field_mapper_credential_options_projection(options: Any) -> Any:
    """Scan validated FieldMapper labels as data, retaining every string.

    The detector enforces its resource/content bounds on the candidate before
    plugin validation. Invalid options retain the ordinary conservative walk.
    """
    if (
        type(options) not in (dict, MappingProxyType)
        or len(options) > DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width
        or "mapping" not in options
    ):
        return options
    mapping = options["mapping"]
    if (
        type(mapping) not in (dict, MappingProxyType)
        or len(mapping) > DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width
        or not any(is_secret_field(key) for key in mapping if type(key) is str)
    ):
        return options
    if any(type(key) is not str or type(value) is not str for key, value in mapping.items()):
        return options
    projected = dict(options) | {"mapping": [[key, value] for key, value in mapping.items()]}
    if find_credential_material(projected) is not None:
        return options
    from elspeth.plugins.transforms.field_mapper import FieldMapperConfig
    from elspeth.web.interpretation_state import strip_authoring_options

    try:
        FieldMapperConfig.model_validate(strip_authoring_options(deep_thaw(options)))
    except ValidationError:
        return options
    return projected


def field_mapper_credential_runtime_projection(config: Any) -> Any:
    """Project only serialized runtime transform options for credential scans."""
    if type(config) is not dict or "transforms" not in config or type(config["transforms"]) is not list:
        return config
    try:
        _validate_json_structure(config, label="credential runtime field-domain projection")
    except ValueError:
        return config
    transforms = []
    for transform in config["transforms"]:
        if type(transform) is dict and "plugin" in transform and transform["plugin"] == "field_mapper" and "options" in transform:
            transforms.append(transform | {"options": field_mapper_credential_options_projection(transform["options"])})
        else:
            transforms.append(transform)
    return config | {"transforms": transforms}


def _credential_node_projection(value: Any, *, splice: bool = False) -> Any:
    if (
        type(value) is not dict
        or len(value) > DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width
        or "plugin" not in value
        or value["plugin"] != "field_mapper"
    ):
        return value
    if not splice and ("node_type" not in value or value["node_type"] != "transform"):
        return value
    if "options" not in value:
        return value
    return value | {"options": field_mapper_credential_options_projection(value["options"])}


def credential_state_for_tool_batch(state: CompositionState | None, tool_names: Sequence[str]) -> CompositionState | None:
    """Withhold stale identity whenever a batch might replace its nodes."""
    from elspeth.web.composer.tools._registry import _DECLARATIONS_BY_NAME
    from elspeth.web.composer.tools.declarations import ToolKind

    discovery_kinds = {ToolKind.DISCOVERY, ToolKind.BLOB_DISCOVERY, ToolKind.SECRET_DISCOVERY}
    for name in tool_names:
        if name in {"patch_node_options", "set_metadata"}:
            continue
        if name not in _DECLARATIONS_BY_NAME or _DECLARATIONS_BY_NAME[name].kind not in discovery_kinds:
            return None
    return state


def credential_material_tool_arguments_projection(
    tool_name: str, arguments: Any, *, credential_state: CompositionState | None = None
) -> Any:
    """Project data-plane blob bodies out of a tool admission scan.

    Blob bytes remain governed by the data-plane policy. Their surrounding
    control metadata stays visible to the detector, and the original
    arguments are never mutated.
    """
    if type(arguments) is not dict:
        return arguments
    projected = dict(arguments)
    if tool_name in {"upsert_node", "splice_transform", "patch_node_options"}:
        try:
            _validate_json_structure(projected, label="credential field-domain projection")
        except ValueError:
            return projected
    if tool_name == "upsert_node":
        return _credential_node_projection(projected)
    if tool_name == "splice_transform" and "node" in projected:
        projected["node"] = _credential_node_projection(projected["node"], splice=True)
        return projected
    if tool_name == "patch_node_options" and credential_state is not None:
        from elspeth.web.composer.state import CompositionState

        if type(credential_state) is not CompositionState:
            raise TypeError("Credential patch admission requires an owned state")
        if len(credential_state.nodes) > DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width:
            return projected
        node_id = projected["node_id"] if "node_id" in projected else None
        patch = projected["patch"] if "patch" in projected else None
        if type(node_id) is not str or type(patch) is not dict:
            return projected
        matches = [node for node in credential_state.nodes if node.id == node_id]
        if len(matches) != 1 or matches[0].node_type != "transform" or matches[0].plugin != "field_mapper":
            return projected
        serialized_nodes = credential_state.to_dict()["nodes"]
        stored = next(node["options"] for node in serialized_nodes if node["id"] == node_id)
        merged = dict(stored)
        for key, value in patch.items():
            if value is None:
                if key in merged:
                    del merged[key]
            else:
                merged[key] = value
        mapped = field_mapper_credential_options_projection(merged)
        if mapped is not merged and "mapping" in patch:
            projected["patch"] = patch | {"mapping": mapped["mapping"]}
        return projected
    if tool_name == "wire_secret_ref" and "option_key" in projected:
        projected["structural_option_name"] = projected.pop("option_key")
        return projected
    if tool_name in {"create_blob", "update_blob"} and "content" in projected:
        projected["content"] = _OUT_OF_SCOPE_DATA_PLANE_CONTENT
        return projected
    if tool_name not in {"set_pipeline", "emit_pipeline_proposal"}:
        return projected

    def project_source(value: Any) -> Any:
        if type(value) is not dict:
            return value
        source = dict(value)
        inline_blob = source["inline_blob"] if "inline_blob" in source else None
        if type(inline_blob) is dict:
            projected_blob = dict(inline_blob)
            if "content" in projected_blob:
                projected_blob["content"] = _OUT_OF_SCOPE_DATA_PLANE_CONTENT
            source["inline_blob"] = projected_blob
        return source

    def project_pipeline(value: Any) -> Any:
        if type(value) is not dict:
            return value
        pipeline = dict(value)
        if "source" in pipeline:
            pipeline["source"] = project_source(pipeline["source"])
        if "sources" in pipeline and type(pipeline["sources"]) is dict:
            pipeline["sources"] = {name: project_source(source) for name, source in pipeline["sources"].items()}
        try:
            _validate_json_structure(pipeline, label="credential field-domain projection")
        except ValueError:
            return pipeline
        if (
            "nodes" in pipeline
            and type(pipeline["nodes"]) is list
            and len(pipeline["nodes"]) <= DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width
        ):
            pipeline["nodes"] = [_credential_node_projection(node) for node in pipeline["nodes"]]
        return pipeline

    if "pipeline" in projected:
        projected["pipeline"] = project_pipeline(projected["pipeline"])
    else:
        projected = project_pipeline(projected)
    return projected


def require_no_credential_material_for_tool(
    tool_name: str,
    arguments: Any,
    *,
    surface: str,
    credential_state: CompositionState | None = None,
) -> None:
    """Apply the control-plane detector to one semantic tool invocation."""
    require_no_credential_material(
        credential_material_tool_arguments_projection(tool_name, arguments, credential_state=credential_state),
        surface=surface,
    )


def require_no_credential_material_in_tool_wire(
    tool_name: str,
    raw_arguments: Any,
    *,
    surface: str,
    credential_state: CompositionState | None = None,
) -> None:
    """Scan one provider tool-argument wire value before audit or dispatch.

    Valid JSON is decoded before scanning so JSON escapes cannot hide a
    credential. Malformed bounded text is scanned as text. Oversized wire
    values are left to the established JSON boundary, which rejects them
    without persisting or echoing their bytes.
    """
    if type(raw_arguments) is not str:
        return
    raw_size = len(raw_arguments.encode("utf-8", errors="replace"))
    if raw_size > JSON_MAX_TOTAL_UTF8_BYTES:
        return
    decoded = _decode_bounded_tool_wire(raw_arguments)
    if decoded is _MALFORMED_TOOL_WIRE:
        require_no_credential_material(
            raw_arguments,
            surface=surface,
            max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
        )
        return
    require_no_credential_material_for_tool(tool_name, decoded, surface=surface, credential_state=credential_state)


def _decode_bounded_tool_wire(raw_arguments: str) -> Any:
    """Return a decoded value or an explicit malformed-wire sentinel."""
    try:
        return bounded_json_loads(raw_arguments, label="composer tool arguments")
    except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError):
        return _MALFORMED_TOOL_WIRE


def require_no_credential_material_in_state(
    state: CompositionState,
    *,
    surface: str,
    env_ref_names: Collection[str] = frozenset(),
) -> None:
    """Reject credential material across one owned composition state.

    The whole-state walk catches credential shapes under ordinary keys and
    every heuristic credential field. Component walks add the small,
    plugin-owned field vocabulary whose names are structurally ordinary (the
    database sink's ``url`` is the current example). Exact references from the
    caller's secret inventory remain admissible in both passes.
    """
    serialized = state.to_dict()
    require_no_credential_material(
        credential_material_tool_arguments_projection("set_pipeline", serialized),
        surface=surface,
        env_ref_names=env_ref_names,
    )
    for source in state.sources.values():
        require_no_credential_material(
            source.options,
            surface=surface,
            env_ref_names=env_ref_names,
            additional_credential_fields=allowed_secret_ref_fields("source", source.plugin),
        )
    for index, node in enumerate(state.nodes):
        if node.plugin is None:
            continue
        require_no_credential_material(
            field_mapper_credential_options_projection(serialized["nodes"][index]["options"])
            if node.node_type == "transform" and node.plugin == "field_mapper"
            else node.options,
            surface=surface,
            env_ref_names=env_ref_names,
            additional_credential_fields=allowed_secret_ref_fields("transform", node.plugin),
        )
    for output in state.outputs:
        require_no_credential_material(
            output.options,
            surface=surface,
            env_ref_names=env_ref_names,
            additional_credential_fields=allowed_secret_ref_fields("sink", output.plugin),
        )
