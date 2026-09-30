"""Structured refusal at Web and Composer credential-material boundaries."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any, TypedDict

from elspeth.contracts.credential_material import (
    CREDENTIAL_REFUSAL_DETAIL as CREDENTIAL_REFUSAL_DETAIL,
)
from elspeth.contracts.credential_material import (
    CredentialMaterialFinding,
    CredentialTraversalPolicy,
    find_credential_material,
)
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES, JsonBoundaryError, bounded_json_loads
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


def credential_material_tool_arguments_projection(tool_name: str, arguments: Any) -> Any:
    """Project data-plane blob bodies out of a tool admission scan.

    Blob bytes remain governed by the data-plane policy. Their surrounding
    control metadata stays visible to the detector, and the original
    arguments are never mutated.
    """
    if type(arguments) is not dict:
        return arguments
    projected = dict(arguments)
    if tool_name in {"create_blob", "update_blob"} and "content" in projected:
        projected["content"] = _OUT_OF_SCOPE_DATA_PLANE_CONTENT
        return projected
    if tool_name != "set_pipeline":
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
) -> None:
    """Apply the control-plane detector to one semantic tool invocation."""
    require_no_credential_material(
        credential_material_tool_arguments_projection(tool_name, arguments),
        surface=surface,
    )


def require_no_credential_material_in_tool_wire(
    tool_name: str,
    raw_arguments: Any,
    *,
    surface: str,
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
    require_no_credential_material_for_tool(tool_name, decoded, surface=surface)


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
    require_no_credential_material(
        state.to_dict(),
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
    for node in state.nodes:
        if node.plugin is None:
            continue
        require_no_credential_material(
            node.options,
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
