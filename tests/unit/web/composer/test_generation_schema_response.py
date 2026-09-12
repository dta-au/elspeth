"""Canonical schema admission is distinct from bounded planner projection."""

import json
from dataclasses import FrozenInstanceError

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer.planner_authoring_aids import (
    SchemaContractProjectionUnsupported,
    planner_plugin_contract,
    planner_plugin_contract_from_snapshot,
)
from elspeth.web.composer.tools._generation_schema_response import (
    PLUGIN_SCHEMA_RESPONSE_CONTRACT,
    AdmittedPluginSchemaResponse,
)


def schema() -> PluginSchemaInfo:
    return PluginSchemaInfo(
        name="example",
        plugin_type="transform",
        description="Private catalogue prose",
        json_schema={
            "title": "Example",
            "type": "object",
            "properties": {
                "odd/name ☃": {"type": "string", "default": "é"},
                "hidden": {"type": "string", "composer_hidden": True, "description": "Internal"},
            },
            "required": ["hidden", "odd/name ☃"],
        },
        knob_schema={
            "fields": [
                {"name": "odd/name ☃", "kind": "text", "required": True, "label": "A label", "default": {"nested": [None, 2.5, True]}}
            ]
        },
        composer_hints=("Use exact fields",),
    )


def test_snapshot_preserves_wire_and_detaches_producer() -> None:
    original = schema()
    expected_wire = json.dumps(original.model_dump(mode="json"))
    expected_projection = planner_plugin_contract(original).to_dict()
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)
    assert type(admitted) is AdmittedPluginSchemaResponse
    original.json_schema["title"] = "tampered"
    original.knob_schema["fields"][0]["default"] = "tampered"
    assert json.dumps(admitted.to_wire()) == expected_wire
    assert planner_plugin_contract_from_snapshot(admitted.snapshot).to_dict() == expected_projection
    assert json.dumps(admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT).to_wire()) == expected_wire
    with pytest.raises(FrozenInstanceError):
        admitted.snapshot.name = "tampered"
    with pytest.raises(TypeError):
        admitted.snapshot.json_schema.keywords["title"] = "tampered"


@pytest.mark.parametrize("value", [None, {}, {"name": "example"}, object()])
def test_schema_response_requires_owned_nominal_root(value: object) -> None:
    with pytest.raises(FrameworkBugError):
        PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(value)


@pytest.mark.parametrize(
    "fragment",
    [
        {"default": object()},
        {"default": float("nan")},
        {"properties": {"x": 7}},
        {"type": "nonsense"},
        {"minimum": "zero"},
        {"required": [True]},
    ],
)
def test_corrupt_schema_keyword_is_framework_bug(fragment: dict[str, object]) -> None:
    original = schema()
    original.json_schema = fragment
    with pytest.raises(FrameworkBugError):
        PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)


@pytest.mark.parametrize(
    "field",
    [
        {"name": "x", "kind": "text", "required": "yes"},
        {"name": "x", "kind": "text", "required": True, "unexpected": 1},
        {"name": "x", "kind": "text", "required": True, "visible_when": {"field": "y", "equals": 1, "extra": 2}},
    ],
)
def test_corrupt_knob_field_is_framework_bug(field: dict[str, object]) -> None:
    original = schema()
    original.knob_schema = {"fields": [field]}
    with pytest.raises(FrameworkBugError):
        PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)


def test_valid_large_schema_keeps_projection_refusal_separate() -> None:
    original = schema()
    original.json_schema["description"] = "x" * (49 * 1024)
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)
    assert admitted.to_wire() == original.model_dump(mode="json")
    with pytest.raises(SchemaContractProjectionUnsupported):
        planner_plugin_contract_from_snapshot(admitted.snapshot)


def test_unknown_finite_schema_extension_is_independent_projection_refusal() -> None:
    original = schema()
    original.json_schema["vendor:constraint"] = {"odd/name": ["é", None, False]}
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)
    assert json.dumps(admitted.to_wire()) == json.dumps(original.model_dump(mode="json"))
    with pytest.raises(SchemaContractProjectionUnsupported):
        planner_plugin_contract_from_snapshot(admitted.snapshot)
    original.json_schema["vendor:constraint"] = object()
    with pytest.raises(FrameworkBugError):
        PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)


def test_actual_catalogue_schema_wire_bytes_are_preserved() -> None:
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.web.catalog.service import CatalogServiceImpl

    manager = PluginManager()
    manager.register_builtin_plugins()
    catalog = CatalogServiceImpl(manager)
    entries = [*catalog.list_sources(), *catalog.list_transforms(), *catalog.list_sinks()]
    assert entries
    for item in entries:
        original = catalog.get_schema(item.plugin_type, item.name)
        admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)
        assert json.dumps(admitted.to_wire()) == json.dumps(original.model_dump(mode="json")), item.name


def test_nominal_root_corruption_and_substitution_are_rejected() -> None:
    from pydantic import BaseModel

    class Unrelated(BaseModel):
        name: str = "example"

    class SchemaSubclass(PluginSchemaInfo):
        pass

    original = schema()
    for invalid in (
        Unrelated(),
        SchemaSubclass(**dict(original)),
        original.model_copy(update={"extra": True}),
        original.model_copy(update={"name": 7}),
        original.model_copy(update={"composer_hints": (False,)}),
    ):
        with pytest.raises(FrameworkBugError):
            PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(invalid)


def test_catalogue_root_growth_requires_snapshot_contract_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic.fields import FieldInfo

    original = schema().model_copy(update={"new_metadata": "must not be silently discarded"})
    monkeypatch.setitem(PluginSchemaInfo.model_fields, "new_metadata", FieldInfo(annotation=str))
    with pytest.raises(FrameworkBugError):
        PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)


def test_cached_snapshot_cannot_be_replaced_by_raw_catalogue_model() -> None:
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(schema())
    object.__setattr__(admitted, "snapshot", schema())
    with pytest.raises(FrameworkBugError):
        admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)


def test_cached_secret_cannot_be_replaced_by_raw_catalogue_model() -> None:
    from elspeth.web.catalog.schemas import PluginSecretRequirement

    original = schema()
    original.secret_requirements = (PluginSecretRequirement(field="token", candidates=("TOKEN",)),)
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(original)
    assert admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT).to_wire() == admitted.to_wire()
    object.__setattr__(admitted.snapshot, "secret_requirements", original.secret_requirements)
    with pytest.raises(FrameworkBugError):
        admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)


@pytest.mark.parametrize("corruption", ["schema_root", "schema_property", "schema_default", "knob_fields", "knob_field", "knob_default"])
def test_cached_schema_containers_cannot_be_repaired_by_wire_conversion(corruption: str) -> None:
    from types import MappingProxyType

    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(schema())
    if corruption == "schema_root":
        object.__setattr__(admitted.snapshot.json_schema, "keywords", dict(admitted.snapshot.json_schema.keywords))
    elif corruption in {"schema_property", "schema_default"}:
        keywords = dict(admitted.snapshot.json_schema.keywords)
        keywords["properties" if corruption == "schema_property" else "default"] = {} if corruption == "schema_property" else []
        object.__setattr__(admitted.snapshot.json_schema, "keywords", MappingProxyType(keywords))
    elif corruption == "knob_fields":
        object.__setattr__(admitted.snapshot.knob_schema, "fields", list(admitted.snapshot.knob_schema.fields))
    else:
        field = dict(admitted.snapshot.knob_schema.fields[0])
        if corruption == "knob_default":
            field["default"] = []
            replacement = (MappingProxyType(field),)
        else:
            replacement = (field,)
        object.__setattr__(admitted.snapshot.knob_schema, "fields", replacement)
    with pytest.raises(FrameworkBugError):
        admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)


@pytest.mark.parametrize("field", ["json_schema", "knob_schema"])
def test_cached_schema_leaf_requires_exact_owned_model(field: str) -> None:
    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(schema())
    object.__setattr__(admitted.snapshot, field, schema())
    with pytest.raises(FrameworkBugError):
        admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)


@pytest.mark.parametrize("corruption", ["schema_keyword", "schema_nested_model", "knob_required"])
def test_cached_immutable_containers_still_require_valid_domains(corruption: str) -> None:
    from types import MappingProxyType

    admitted = PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(schema())
    if corruption == "knob_required":
        field = dict(admitted.snapshot.knob_schema.fields[0])
        field["required"] = "yes"
        object.__setattr__(admitted.snapshot.knob_schema, "fields", (MappingProxyType(field),))
    else:
        keywords = dict(admitted.snapshot.json_schema.keywords)
        keywords["type" if corruption == "schema_keyword" else "default"] = (
            "not-a-schema-type" if corruption == "schema_keyword" else schema()
        )
        object.__setattr__(admitted.snapshot.json_schema, "keywords", MappingProxyType(keywords))
    with pytest.raises(FrameworkBugError):
        admitted.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)
