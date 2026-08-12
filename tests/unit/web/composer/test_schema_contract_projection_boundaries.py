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
