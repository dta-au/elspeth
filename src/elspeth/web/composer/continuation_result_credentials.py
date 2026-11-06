"""Retained result admission through actual response and field-domain owners.

Every projection is inspection-only. The original result text remains the
retained value; admission does not confer disclosure or current authorization.
"""

from __future__ import annotations

from pydantic import JsonValue, ValidationError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.credential_material import is_secret_field
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer.bounded_json import _validate_json_structure
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_schema_credentials import require_retained_plugin_schema_credentials
from elspeth.web.composer.discovery_response import require_discovery_result_data
from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT
from elspeth.web.composer.tools._registry import ASYNC_TOOL_EFFECTS, response_contract_for
from elspeth.web.credential_guard import (
    credential_material_tool_arguments_projection,
    field_mapper_credential_options_projection,
    require_no_credential_material,
)
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields


def restore_retained_plugin_schema_data(value: JsonValue, *, env_ref_names: frozenset[str] = frozenset()) -> PluginSchemaInfo:
    """Restore the actual producer model, with complete selected wire parity."""
    encoded = canonical_json(value)
    try:
        result = PluginSchemaInfo.model_validate_json(encoded, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("retained plugin schema violates its actual producer model") from error
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(result)
    if canonical_json(admitted.to_wire()) != encoded:
        raise AuditIntegrityError("retained plugin schema lost or normalized producer fields")
    require_retained_plugin_schema_credentials(admitted, env_ref_names=env_ref_names)
    return result


def _scan(value: JsonValue, env_ref_names: frozenset[str]) -> None:
    require_no_credential_material(value, surface="continuation P4 retained result", env_ref_names=env_ref_names)


def _component(value: JsonValue, kind: str, env_ref_names: frozenset[str], *, splice_transform: bool = False) -> None:
    # Called only on actual component-bearing response/argument positions.
    if not isinstance(value, dict) or "plugin" not in value or "options" not in value:
        _scan(value, env_ref_names)
        return
    plugin, options = value["plugin"], value["options"]
    if not isinstance(plugin, str) or not isinstance(options, dict):
        _scan(value, env_ref_names)
        return
    projected = (
        field_mapper_credential_options_projection(options)
        if kind == "transform" and plugin == "field_mapper" and (value.get("node_type") == "transform" or splice_transform)
        else options
    )
    require_no_credential_material(
        projected,
        surface="continuation P4 retained component options",
        env_ref_names=env_ref_names,
        additional_credential_fields=allowed_secret_ref_fields(kind, plugin),
    )
    _scan({key: item for key, item in value.items() if key != "options"}, env_ref_names)


def require_retained_graph_credentials(value: JsonValue, *, env_ref_names: frozenset[str]) -> None:
    """Inspect complete known graph/component positions, retaining labels."""
    _validate_json_structure(value, label="retained graph response")
    if not isinstance(value, dict):
        _scan(value, env_ref_names)
        return
    remaining = dict(value)
    for key, kind in (("source", "source"), ("node", "transform"), ("output", "sink")):
        if key in remaining:
            _component(remaining.pop(key), kind, env_ref_names)
    if "sources" in remaining:
        sources = remaining.pop("sources")
        if not isinstance(sources, dict):
            raise AuditIntegrityError("retained graph sources lost their object domain")
        for name, source in sources.items():
            _scan(name, env_ref_names)
            _component(source, "source", env_ref_names)
    for key, kind in (("nodes", "transform"), ("outputs", "sink")):
        if key in remaining:
            items = remaining.pop(key)
            if not isinstance(items, list):
                raise AuditIntegrityError("retained graph components lost their array domain")
            for item in items:
                _component(item, kind, env_ref_names)
    _scan(remaining, env_ref_names)


def require_retained_argument_credentials(tool_name: str, value: JsonValue, *, env_ref_names: frozenset[str]) -> None:
    projected = owned_json(credential_material_tool_arguments_projection(tool_name, value))
    # The actual wire projection owns data-plane content exclusions and whole
    # FieldMapper options. Extra plugin secret slots still need their owner.
    if tool_name in {"set_pipeline", "emit_pipeline_proposal"} and isinstance(projected, dict):
        graph = projected["pipeline"] if "pipeline" in projected else projected
        require_retained_graph_credentials(graph, env_ref_names=env_ref_names)
        if "pipeline" in projected:
            _scan({key: item for key, item in projected.items() if key != "pipeline"}, env_ref_names)
    elif tool_name in {"upsert_node", "splice_transform", "set_source", "set_output"} and isinstance(projected, dict):
        kind = "source" if tool_name == "set_source" else "sink" if tool_name == "set_output" else "transform"
        if tool_name == "splice_transform" and "node" in projected:
            _component(projected["node"], kind, env_ref_names, splice_transform=True)
            _scan({key: item for key, item in projected.items() if key != "node"}, env_ref_names)
        else:
            _component(projected, kind, env_ref_names)
    else:
        _scan(projected, env_ref_names)


def _selected_data(tool_name: str, data: JsonValue, env_ref_names: frozenset[str]) -> None:
    if tool_name == "get_pipeline_state":
        require_retained_graph_credentials(data, env_ref_names=env_ref_names)
        return
    if tool_name in {"get_blob_content", "read_attachment"}:
        # The actual READ owner scans the complete payload, including the
        # original content string, before slicing/disclosure. Do not import
        # the create/update argument data-plane exclusion into this result.
        _scan(data, env_ref_names)
        return
    if tool_name in {"list_sources", "list_transforms", "list_sinks"} and isinstance(data, dict):
        available = data["available"]
        if not isinstance(available, list):
            raise AuditIntegrityError("retained inventory lost its admitted plugin array")
        for plugin in available:
            if not isinstance(plugin, dict) or not isinstance(plugin["plugin_type"], str) or not isinstance(plugin["name"], str):
                raise AuditIntegrityError("retained inventory lost its admitted plugin shape")
            requirements = plugin["secret_requirements"]
            fields = plugin["config_fields"]
            if not isinstance(requirements, list) or not isinstance(fields, list):
                raise AuditIntegrityError("retained inventory lost its admitted field arrays")
            declared = allowed_secret_ref_fields(plugin["plugin_type"], plugin["name"])
            for requirement in requirements:
                if not isinstance(requirement, dict) or not isinstance(requirement["field"], str):
                    raise AuditIntegrityError("retained inventory lost its declared secret field")
                declared = declared | {requirement["field"]}
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field["name"], str):
                    raise AuditIntegrityError("retained inventory lost its admitted config field")
                default = field["default"]
                require_no_credential_material(
                    {"password": default} if is_secret_field(field["name"], declared) else default,
                    surface="continuation P4 inventory default",
                    env_ref_names=env_ref_names,
                    additional_credential_fields=declared,
                )
                _scan({key: item for key, item in field.items() if key != "default"}, env_ref_names)
            _scan({key: item for key, item in plugin.items() if key != "config_fields"}, env_ref_names)
        _scan({key: item for key, item in data.items() if key != "available"}, env_ref_names)
        return
    if tool_name == "get_plugin_assistance" and isinstance(data, dict):
        examples, kind, plugin = data["examples"], data["plugin_type"], data["plugin_name"]
        if not isinstance(examples, list) or not isinstance(kind, str) or not isinstance(plugin, str):
            raise AuditIntegrityError("retained assistance lost its actual selected domain")
        for example in examples:
            if not isinstance(example, dict):
                raise AuditIntegrityError("retained assistance lost its actual example")
            for key, item in example.items():
                if key in {"before", "after"} and isinstance(item, dict):
                    projected = (
                        field_mapper_credential_options_projection(item) if kind == "transform" and plugin == "field_mapper" else item
                    )
                    require_no_credential_material(
                        projected,
                        surface="continuation P4 assistance example",
                        env_ref_names=env_ref_names,
                        additional_credential_fields=allowed_secret_ref_fields(kind, plugin),
                    )
                else:
                    _scan(item, env_ref_names)
        _scan({key: item for key, item in data.items() if key != "examples"}, env_ref_names)
        return
    structural_maps = (
        ("field_name_mapping", "inferred_types")
        if tool_name == "inspect_source"
        else ("sources",)
        if tool_name == "preview_pipeline"
        else ("providers",)
        if tool_name == "list_models"
        else ()
    )
    if isinstance(data, dict) and structural_maps:
        remaining = dict(data)
        for key in structural_maps:
            if key in remaining and isinstance(remaining[key], dict):
                mapping = remaining.pop(key)
                if not isinstance(mapping, dict):
                    raise AuditIntegrityError("retained structural response lost its admitted map")
                _scan([[name, item] for name, item in mapping.items()], env_ref_names)
        _scan(remaining, env_ref_names)
    else:
        _scan(data, env_ref_names)


def require_retained_result_credentials(tool_name: str, value: JsonValue, *, env_ref_names: frozenset[str], display: bool = False) -> None:
    """Inspect actual selected response domains and every remaining field."""
    _validate_json_structure(value, label="retained tool result")
    try:
        contract = None if tool_name in ASYNC_TOOL_EFFECTS else response_contract_for(tool_name)
    except KeyError as error:
        raise AuditIntegrityError("retained result names no current tool declaration") from error
    if not isinstance(value, dict):
        _scan(value, env_ref_names)
        if contract is not None and not display:
            raise AuditIntegrityError("retained discovery result lost its object envelope")
        return
    remaining = dict(value)
    if contract is not None and not display:
        # Full discovery results must satisfy the actual owner before any
        # selected wire is restored to a nominal producer model. ARG_ERROR
        # text is admitted separately from its retained invocation status.
        success = remaining.get("success")
        if type(success) is not bool:
            raise AuditIntegrityError("retained discovery result lost its boolean success flag")
        require_discovery_result_data(success=success, data=remaining.get("data"))
    if display and "data" in remaining and (isinstance(remaining["data"], str) or tool_name in {"get_blob_content", "read_attachment"}):
        # Redacted bodies are not complete producer bodies: their content may
        # be a value-free summary with the original byte/range metadata. No
        # data-plane exclusion applies to this display arm.
        _scan(remaining.pop("data"), env_ref_names)
    if "data" in remaining and remaining.get("success") is True:
        data = remaining.pop("data")
        if contract is PLUGIN_SCHEMA_RESPONSE_CONTRACT:
            restore_retained_plugin_schema_data(data, env_ref_names=env_ref_names)
        else:
            if contract is None:
                _scan(data, env_ref_names)
            else:
                # Source inspection explicitly carries the order of its field
                # maps in headers. Canonical audit JSON sorts object keys; use
                # those retained arrays to restore the owner's map-order check.
                admitted_input = data
                if tool_name == "inspect_source" and isinstance(data, dict):
                    admitted_input = dict(data)
                    headers = data.get("observed_headers")
                    mapping = data.get("field_name_mapping")
                    if isinstance(headers, list) and all(isinstance(name, str) for name in headers) and isinstance(mapping, dict):
                        if len(set(headers)) != len(headers) or set(headers) != set(mapping):
                            raise AuditIntegrityError("retained source labels lost their exact header cohort")
                        admitted_input["field_name_mapping"] = {str(name): mapping[str(name)] for name in headers}
                admitted_data = owned_json(contract.admit(admitted_input).to_wire())
                if canonical_json(admitted_data) != canonical_json(data):
                    raise AuditIntegrityError("retained response lost complete selected wire fields")
                _selected_data(tool_name, admitted_data, env_ref_names)
    if "plugin_schemas" in remaining:
        schemas = remaining.pop("plugin_schemas")
        if display and isinstance(schemas, str):
            _scan(schemas, env_ref_names)
            schemas = {}
        if not isinstance(schemas, dict):
            raise AuditIntegrityError("retained inline schemas lost their actual map")
        for identity, schema in schemas.items():
            restored = restore_retained_plugin_schema_data(schema, env_ref_names=env_ref_names)
            if identity != f"{restored.plugin_type}/{restored.name}":
                raise AuditIntegrityError("retained inline schema changed its selected plugin identity")
    if "applied_component" in remaining:
        # The producer emits the actual set_pipeline component wire. There is
        # no exclusion of an arbitrary subtree: every field is inspected by
        # the graph/domain guard, including unrecognized keys.
        require_retained_graph_credentials(remaining.pop("applied_component"), env_ref_names=env_ref_names)
    _scan(remaining, env_ref_names)


def require_retained_result_text(
    tool_name: str,
    text: str | None,
    *,
    env_ref_names: frozenset[str],
    display: bool = False,
    outcome_status: ComposerToolStatus | None = None,
) -> None:
    if text is None:
        return
    parsed, error = parse_json_strict(text)
    value = text if error is not None else owned_json(parsed)
    if outcome_status is ComposerToolStatus.ARG_ERROR:
        # The actual dispatch owner may retain generic error/status payloads
        # before producing a ToolResult. No selected-schema or field-label
        # exemption is granted to that separately typed diagnostic channel.
        _scan(value, env_ref_names)
        return
    require_retained_result_credentials(tool_name, value, env_ref_names=env_ref_names, display=display)
