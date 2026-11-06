"""Real schema metadata stays lossless; configured credentials still refuse."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSecretRequirement
from elspeth.web.composer._schema_response_grammar import JSONSchemaSnapshot
from elspeth.web.composer.continuation_schema_credentials import require_retained_plugin_schema_credentials
from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT, AdmittedPluginSchemaResponse
from elspeth.web.credential_guard import CredentialMaterialRefused


def admitted_schema(
    schema: dict[str, object],
    *,
    knobs: dict[str, object] | None = None,
    name: str = "example",
    plugin_type: str = "transform",
    declared_fields: tuple[str, ...] = (),
) -> AdmittedPluginSchemaResponse:
    return PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(
        PluginSchemaInfo(
            name=name,
            plugin_type=plugin_type,
            description="Complete catalogue metadata — é",
            json_schema=schema,
            knob_schema={"fields": []} if knobs is None else knobs,
            secret_requirements=tuple(PluginSecretRequirement(field=field) for field in declared_fields),
        )
    )


def test_structural_property_names_keep_complete_original_wire_bytes() -> None:
    response = admitted_schema(
        {
            "type": "object",
            "title": "All labels retained",
            "properties": {
                "api_key": {"type": "string", "description": "Managed reference", "default": None},
                "customer_key": {"type": "string"},
                "odd/name ☃": {"type": "string", "default": "é"},
            },
            "required": ["api_key", "customer_key"],
            "dependentRequired": {"api_key": ["customer_key"]},
            "discriminator": {"propertyName": "customer_key", "mapping": {"api_key": "#/$defs/entry"}},
            "$defs": {"entry": {"type": "string"}},
            "vendor:metadata": {"preserved": [None, False, 2.5]},
        },
        knobs={"fields": [{"name": "api_key", "kind": "text", "required": True, "label": "Credential reference"}]},
    )
    before = json.dumps(response.to_wire(), ensure_ascii=False)
    require_retained_plugin_schema_credentials(response)
    assert json.dumps(response.to_wire(), ensure_ascii=False) == before
    assert response.snapshot.json_schema.to_wire()["properties"]["api_key"]["default"] is None


@pytest.mark.parametrize("keyword", ["default", "const", "example", "enum", "examples"])
def test_each_literal_under_declared_secret_property_is_checked(keyword: str) -> None:
    clean = None if keyword not in {"enum", "examples"} else [None]
    dirty = "opaque-credential" if keyword not in {"enum", "examples"} else ["opaque-credential"]
    require_retained_plugin_schema_credentials(admitted_schema({"properties": {"api_key": {keyword: clean}}}))
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(admitted_schema({"properties": {"api_key": {keyword: dirty}}}))


def test_local_reference_carries_credential_context_without_merging_definition_uses() -> None:
    response = admitted_schema(
        {
            "$defs": {"setting/with~escape": {"anyOf": [{"type": "string", "default": "${KEY}"}, {"type": "null"}]}},
            "properties": {"api_key": {"$ref": "#/$defs/setting~1with~0escape"}, "ordinary": {"$ref": "#/$defs/setting~1with~0escape"}},
        }
    )
    original = json.dumps(response.to_wire())
    require_retained_plugin_schema_credentials(response, env_ref_names=frozenset({"KEY"}))
    assert json.dumps(response.to_wire()) == original
    for inventory in (frozenset(), frozenset({"OTHER"})):
        with pytest.raises(CredentialMaterialRefused):
            require_retained_plugin_schema_credentials(response, env_ref_names=inventory)


def test_recursive_local_schema_references_terminate_and_still_check_literals() -> None:
    clean = {
        "$defs": {"chain": {"allOf": [{"$ref": "#/$defs/chain"}, {"default": None}]}},
        "properties": {"api_key": {"$ref": "#/$defs/chain"}},
    }
    require_retained_plugin_schema_credentials(admitted_schema(clean))
    clean["$defs"]["chain"]["allOf"][1]["default"] = "opaque-credential"
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(admitted_schema(clean))


@pytest.mark.parametrize("location", ["schema", "knob", "predicate", "declared", "database"])
def test_actual_declared_fields_and_knob_literal_channels_refuse(location: str) -> None:
    schema: dict[str, object] = {}
    knobs: dict[str, object] = {"fields": []}
    name, plugin_type, declared = "example", "transform", ()
    if location == "schema":
        schema = {"default": {"api_key": "opaque-credential"}}
    elif location == "knob":
        knobs = {"fields": [{"name": "api_key", "kind": "text", "required": True, "default": "opaque-credential"}]}
    elif location == "predicate":
        knobs = {
            "fields": [
                {"name": "mode", "kind": "text", "required": False, "visible_when": {"field": "api_key", "equals": "opaque-credential"}}
            ]
        }
    elif location == "declared":
        declared = ("opaque_slot",)
        schema = {"properties": {"opaque_slot": {"default": "opaque-credential"}}}
    else:
        name, plugin_type = "database", "sink"
        schema = {"properties": {"url": {"default": "opaque-credential"}}}
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(
            admitted_schema(schema, knobs=knobs, name=name, plugin_type=plugin_type, declared_fields=declared)
        )


def test_managed_reference_and_exact_environment_inventory_are_preserved() -> None:
    response = admitted_schema({"properties": {"api_key": {"enum": [{"secret_ref": "SERVICE_KEY", "secret_scope": "user"}, "${KEY}"]}}})
    expected = response.to_wire()
    require_retained_plugin_schema_credentials(response, env_ref_names=frozenset({"KEY"}))
    assert response.to_wire() == expected
    changed = admitted_schema({"properties": {"api_key": {"default": "prefix-${KEY}"}}})
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(changed, env_ref_names=frozenset({"KEY"}))


@pytest.mark.parametrize("location", ["description", "extension", "unknown_secret_extension", "structural_name", "knob_placeholder"])
def test_metadata_is_not_a_blanket_schema_exemption(location: str) -> None:
    # Candidate-free fixture; no actual secret is used.
    credential_url = "https://reader:example-password@example.invalid/path"
    schema: dict[str, object] = {}
    knobs: dict[str, object] = {"fields": []}
    if location == "description":
        schema = {"description": credential_url}
    elif location == "extension":
        schema = {"vendor:metadata": {"api_key": "opaque-credential"}}
    elif location == "unknown_secret_extension":
        schema = {"api_key": "opaque-credential"}
    elif location == "structural_name":
        schema = {"properties": {credential_url: {"type": "string"}}}
    else:
        knobs = {"fields": [{"name": "label", "kind": "text", "required": False, "placeholder": credential_url}]}
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(admitted_schema(schema, knobs=knobs))


def test_complete_field_mapper_config_default_uses_actual_config_projection() -> None:
    options = {"schema": {"mode": "observed"}, "mapping": {"customer_key": "customer_id"}}
    response = admitted_schema({"default": options}, name="field_mapper")
    expected = response.to_wire()
    require_retained_plugin_schema_credentials(response)
    assert response.to_wire() == expected
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(admitted_schema({"default": options}))
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(
            admitted_schema({"default": options | {"api_key": "opaque-credential"}}, name="field_mapper")
        )
    # A partial mapping literal lacks the actual config owner's required
    # schema. The boundary does not invent it or waive a generic nested map.
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(
            admitted_schema({"properties": {"mapping": {"default": options["mapping"]}}}, name="field_mapper")
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/schema"},
        {"$ref": "#anchor"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "#/properties/a~2"},
        {"$id": "https://example.invalid/schema"},
        {"$dynamicRef": "#node"},
        {"$anchor": "node"},
    ],
)
def test_unowned_reference_resolution_is_explicitly_refused(schema: dict[str, object]) -> None:
    with pytest.raises(AuditIntegrityError):
        require_retained_plugin_schema_credentials(admitted_schema(schema))


def test_cached_nominal_corruption_is_rejected_before_wire_projection() -> None:
    response = admitted_schema({"type": "string"})
    # Actual immutable nominal owner carrying malformed admitted keywords.
    corrupted = AdmittedPluginSchemaResponse(
        replace(response.snapshot, json_schema=JSONSchemaSnapshot({"type": "nonsense"})), PLUGIN_SCHEMA_RESPONSE_CONTRACT
    )
    with pytest.raises(FrameworkBugError):
        require_retained_plugin_schema_credentials(corrupted)
    with pytest.raises(TypeError):
        require_retained_plugin_schema_credentials(response.to_wire())
    changed_contract = AdmittedPluginSchemaResponse(response.snapshot, type(PLUGIN_SCHEMA_RESPONSE_CONTRACT)())
    with pytest.raises(FrameworkBugError):
        require_retained_plugin_schema_credentials(changed_contract)


def test_actual_field_mapper_catalog_schema_has_an_owned_unchanged_wire() -> None:
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.web.catalog.service import CatalogServiceImpl

    manager = PluginManager()
    manager.register_builtin_plugins()
    catalog = CatalogServiceImpl(manager)
    original = catalog.get_schema("transform", "field_mapper")
    response = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)
    expected = json.dumps(response.to_wire(), ensure_ascii=False)
    require_retained_plugin_schema_credentials(response)
    assert json.dumps(response.to_wire(), ensure_ascii=False) == expected


def test_shallow_schema_with_long_local_reference_chain_does_not_depend_on_python_stack() -> None:
    definitions = {f"item_{index}": {"$ref": f"#/$defs/item_{index + 1}"} for index in range(1100)}
    definitions["item_1100"] = {"default": None}
    response = admitted_schema({"$defs": definitions, "properties": {"api_key": {"$ref": "#/$defs/item_0"}}})
    require_retained_plugin_schema_credentials(response)
    definitions["item_1100"] = {"default": "opaque-credential"}
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(
            admitted_schema({"$defs": definitions, "properties": {"api_key": {"$ref": "#/$defs/item_0"}}})
        )


def test_declared_slot_names_do_not_reclassify_metadata_record_keys() -> None:
    response = admitted_schema({"properties": {"field": {"type": "string"}}}, declared_fields=("field",))
    require_retained_plugin_schema_credentials(response)
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(
            admitted_schema({"properties": {"field": {"default": "opaque-credential"}}}, declared_fields=("field",))
        )


def test_definition_first_noncredential_visit_does_not_clear_later_secret_use() -> None:
    # The worklist sees the last declaration first. Keep properties before
    # defs so the shared node is initially inspected without secret context.
    response = admitted_schema(
        {
            "properties": {"api_key": {"$ref": "#/$defs/shared"}},
            "$defs": {"shared": {"default": "opaque-credential"}},
        }
    )
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(response)


@pytest.mark.parametrize("pattern", ["^api_key$", ".*", "customer_.*"])
def test_nonempty_pattern_properties_has_no_inferred_credential_domain(pattern: str) -> None:
    response = admitted_schema({"patternProperties": {pattern: {"default": "opaque-credential"}}})
    # The actual schema grammar admits this value; retention refuses exactly
    # the unsupported regex domain, without rewriting its original bytes.
    expected = response.to_wire()
    with pytest.raises(AuditIntegrityError, match="patternProperties credential domain is unsupported"):
        require_retained_plugin_schema_credentials(response)
    assert response.to_wire() == expected


def test_empty_pattern_properties_and_literal_names_remain_admissible() -> None:
    response = admitted_schema({"patternProperties": {}, "properties": {"customer_key": {"type": "string"}, "api_key": {"default": None}}})
    expected = response.to_wire()
    require_retained_plugin_schema_credentials(response)
    assert response.to_wire() == expected


@pytest.mark.parametrize("keyword", ["additionalProperties", "unevaluatedProperties"])
def test_unknown_property_value_schema_retains_possible_credential_slot_context(keyword: str) -> None:
    clean = admitted_schema({keyword: {"default": None}}, declared_fields=("opaque_slot",))
    require_retained_plugin_schema_credentials(clean)
    dirty = admitted_schema({keyword: {"default": "opaque-credential"}}, declared_fields=("opaque_slot",))
    expected = dirty.to_wire()
    with pytest.raises(CredentialMaterialRefused):
        require_retained_plugin_schema_credentials(dirty)
    assert dirty.to_wire() == expected


@pytest.mark.parametrize("keyword", ["additionalProperties", "unevaluatedProperties"])
@pytest.mark.parametrize("fragment", [True, False, {}, {"type": "string", "maxLength": 100, "description": "Any field label"}])
def test_unknown_property_boolean_and_structural_schemas_remain_admissible(keyword: str, fragment: object) -> None:
    response = admitted_schema({keyword: fragment}, declared_fields=("opaque_slot",))
    expected = response.to_wire()
    require_retained_plugin_schema_credentials(response)
    assert response.to_wire() == expected
