"""Honesty-gate tests for the raising Tier-3 schema-projection boundaries.

``planner_authoring_aids._contract_discriminator`` / ``_contract_json_schema``
/ ``_contract_knob_schema`` each parse a plugin-declared JSON-Schema-shaped
fragment (the raw output of a plugin's ``ConfigModel.model_json_schema()``,
surfaced via ``PolicyCatalogView.get_schema``) and are decorated
``@trust_boundary``. The ``trust_boundary.tests`` elspeth-lints rule requires
a ``test_ref`` whose own body directly calls the decorated function through
its declared ``source_param`` inside a raising assertion — these tests are
that direct call, kept separate from the exemplar-validation suite in
``test_planner_authoring_aids.py`` so each stays a thin, focused unit test.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer import planner_authoring_aids
from elspeth.web.composer.planner_authoring_aids import (
    _contract_discriminator,
    _contract_json_schema,
    _contract_knob_schema,
    _SchemaContractProjectionUnsupported,
)


def test_contract_discriminator_rejects_unknown_key() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_discriminator({"propertyName": "kind", "unexpected": True})


def test_contract_discriminator_accepts_known_shape() -> None:
    projected = _contract_discriminator({"propertyName": "kind", "mapping": {"a": "b"}})
    assert projected == {"propertyName": "kind", "mapping": {"a": "b"}}


def test_contract_json_schema_rejects_non_dict_non_bool() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_json_schema("not-a-schema")


def test_contract_json_schema_accepts_bare_bool() -> None:
    assert _contract_json_schema(True) is True
    assert _contract_json_schema(False) is False


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "not-a-json-type"},
        {"type": ["string", "string"]},
        {"pattern": 123},
        {"minimum": "zero"},
        {"multipleOf": 0},
        {"minItems": -1},
        {"uniqueItems": "yes"},
        {"enum": "not-an-array"},
        {"$vocabulary": {"https://example.invalid/vocab": "required"}},
    ],
)
def test_contract_json_schema_rejects_invalid_scalar_keyword_domains(schema: dict[str, object]) -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_json_schema(schema)


def test_contract_knob_schema_rejects_missing_fields_key() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_knob_schema({"not_fields": []})


def test_contract_knob_schema_accepts_empty_fields() -> None:
    assert _contract_knob_schema({"fields": []}) == {"fields": []}


def test_planner_plugin_contract_is_a_bounded_owned_projection() -> None:
    admitted = PluginSchemaInfo(
        name="bounded_transform",
        plugin_type="transform",
        description="A representative admitted plugin.",
        json_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean", "description": "UI prose"}},
            "required": ["enabled"],
        },
        knob_schema={
            "fields": [
                {
                    "name": "enabled",
                    "kind": "boolean",
                    "required": True,
                    "description": "UI prose",
                }
            ]
        },
        composer_hints=("Use this only after selecting it.",),
    )

    contract = planner_authoring_aids.planner_plugin_contract(admitted)
    payload = contract.to_dict()

    assert payload["plugin_id"] == "transform/bounded_transform"
    assert payload["composer_hints"] == ["Use this only after selecting it."]
    assert payload["json_schema"] == {
        "type": "object",
        "properties": {"enabled": {"type": "boolean"}},
        "required": ["enabled"],
    }
    assert payload["knob_schema"] == {"fields": [{"name": "enabled", "kind": "boolean", "required": True}]}


def test_planner_plugin_contract_rejects_recursive_projection_overflow() -> None:
    nested: dict[str, object] = {"type": "string"}
    for _ in range(80):
        nested = {"items": nested}
    admitted = PluginSchemaInfo(
        name="recursive_transform",
        plugin_type="transform",
        description="A recursive projection fixture.",
        json_schema=nested,
        knob_schema={"fields": []},
    )

    with pytest.raises(planner_authoring_aids.SchemaContractProjectionUnsupported):
        planner_authoring_aids.planner_plugin_contract(admitted)


def test_planner_plugin_contract_rejects_more_than_4096_input_nodes() -> None:
    admitted = PluginSchemaInfo(
        name="wide_transform",
        plugin_type="transform",
        description="A wide projection fixture.",
        json_schema={"type": "object", "enum": [None] * 4096},
        knob_schema={"fields": []},
    )

    with pytest.raises(planner_authoring_aids.SchemaContractProjectionUnsupported):
        planner_authoring_aids.planner_plugin_contract(admitted)


def test_planner_plugin_contract_rejects_noncanonical_admitted_scalar() -> None:
    admitted = PluginSchemaInfo(
        name="noncanonical_transform",
        plugin_type="transform",
        description="A schema whose admitted default is outside the JSON domain.",
        json_schema={"type": "object", "default": object()},
        knob_schema={"fields": []},
    )

    with pytest.raises(planner_authoring_aids.SchemaContractProjectionUnsupported):
        planner_authoring_aids.planner_plugin_contract(admitted)


@pytest.mark.parametrize(
    "bad_default",
    [float("nan"), float("inf"), float("-inf"), {"unordered"}, frozenset({"unordered"})],
)
def test_planner_plugin_contract_rejects_every_noncanonical_scalar_domain(bad_default: object) -> None:
    admitted = PluginSchemaInfo(
        name="noncanonical_transform",
        plugin_type="transform",
        description="A schema outside the canonical JSON scalar domain.",
        json_schema={"type": "object", "default": bad_default},
        knob_schema={"fields": []},
    )

    with pytest.raises(planner_authoring_aids.SchemaContractProjectionUnsupported):
        planner_authoring_aids.planner_plugin_contract(admitted)


@pytest.mark.parametrize(
    ("json_schema", "composer_hints"),
    [
        ({"type": "object", "default": "x" * (49 * 1024)}, ()),
        ({"type": "object"}, ("h" * (49 * 1024),)),
    ],
)
def test_planner_plugin_contract_rejects_oversize_scalar_or_hint_before_contract(
    json_schema: dict[str, object],
    composer_hints: tuple[str, ...],
) -> None:
    admitted = PluginSchemaInfo(
        name="oversize_transform",
        plugin_type="transform",
        description="Oversize public contract fixture.",
        json_schema=json_schema,
        knob_schema={"fields": []},
        composer_hints=composer_hints,
    )

    with pytest.raises(planner_authoring_aids.SchemaContractProjectionUnsupported):
        planner_authoring_aids.planner_plugin_contract(admitted)


def test_planner_plugin_contract_is_deeply_frozen_and_thaws_only_on_export() -> None:
    admitted = PluginSchemaInfo(
        name="immutable_transform",
        plugin_type="transform",
        description="Immutable contract fixture.",
        json_schema={"type": "object", "properties": {"enabled": {"type": "boolean"}}},
        knob_schema={
            "fields": [
                {
                    "name": "enabled",
                    "kind": "boolean",
                    "required": True,
                    "visible_when": {"field": "mode", "equals": {"one_of": ["active"]}},
                }
            ]
        },
    )

    contract = planner_authoring_aids.planner_plugin_contract(admitted)
    original_hash = contract.schema_hash

    assert isinstance(contract.json_schema, MappingProxyType)
    with pytest.raises(TypeError):
        contract.json_schema["type"] = "string"  # type: ignore[index]
    properties = contract.json_schema["properties"]
    assert isinstance(properties, MappingProxyType)
    with pytest.raises(TypeError):
        properties["enabled"] = {"type": "string"}  # type: ignore[index]
    knob_fields = contract.knob_schema["fields"]
    assert isinstance(knob_fields, tuple)
    visible_when = knob_fields[0]["visible_when"]  # type: ignore[index]
    assert isinstance(visible_when, MappingProxyType)
    assert isinstance(visible_when["equals"], MappingProxyType)
    one_of = visible_when["equals"]["one_of"]
    assert isinstance(one_of, tuple)
    with pytest.raises(TypeError):
        knob_fields[0] = {"name": "replacement"}  # type: ignore[index]
    with pytest.raises(TypeError):
        one_of[0] = "inactive"  # type: ignore[index]
    with pytest.raises(TypeError):
        visible_when["equals"] = {"one_of": ["inactive"]}  # type: ignore[index]
    exported = contract.to_dict()
    exported["json_schema"]["type"] = "string"  # type: ignore[index]
    assert contract.schema_hash == original_hash
    assert contract.to_dict()["json_schema"]["type"] == "object"  # type: ignore[index]
