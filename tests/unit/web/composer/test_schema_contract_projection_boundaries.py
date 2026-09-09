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


def test_contract_json_schema_drops_composer_tier_as_prose() -> None:
    # ``composer_tier`` is a UI-disclosure hint (elspeth-9cca900d41): pydantic
    # bakes json_schema_extra={"composer_tier": ...} as a sibling key onto the
    # property's own generated JSON Schema, the same way it does for
    # composer_description/composer_placeholder. Tier is presentational, not
    # audit-bearing (see knob_schema._attach_tier's own docstring), and the
    # planner's raw json_schema contract already carries no other UI-prose
    # key — this must project like composer_description does (dropped), not
    # trip the closed-vocabulary fail-closed branch that guards genuinely
    # unmodelled schema semantics.
    projected = _contract_json_schema(
        {
            "type": "object",
            "properties": {
                "temperature": {"type": "number", "composer_tier": "advanced"},
            },
        }
    )
    assert projected == {
        "type": "object",
        "properties": {"temperature": {"type": "number"}},
    }


def test_contract_knob_schema_rejects_missing_fields_key() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_knob_schema({"not_fields": []})


def test_contract_knob_schema_accepts_empty_fields() -> None:
    assert _contract_knob_schema({"fields": []}) == {"fields": []}


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        # ``choices`` alone is the legacy spelling: it is renamed to ``enum``.
        ({"choices": ["a", "b"]}, {"enum": ["a", "b"]}),
        # Both spellings present (and equal, or the guard above would have
        # raised): ``enum`` wins and ``choices`` never reaches the projection.
        ({"enum": ["a", "b"], "choices": ["a", "b"]}, {"enum": ["a", "b"]}),
        # Neither spelling present: no enum key is invented.
        ({}, {}),
    ],
    ids=["choices-only", "both-spellings", "neither"],
)
def test_contract_knob_schema_projects_one_enum_spelling(declared: dict[str, object], expected: dict[str, object]) -> None:
    """``choices`` is folded into ``enum`` and never survives the projection."""
    projected = _contract_knob_schema({"fields": [{"name": "mode", "kind": "enum", "required": True, **declared}]})
    assert projected == {"fields": [{"name": "mode", "kind": "enum", "required": True, **expected}]}


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


def _variant_knobs(
    discriminator_enum: list[str],
    coverage: list[object],
    *,
    name: str = "shared_knob",
) -> dict[str, object]:
    """One lowered discriminated union: a discriminator plus per-variant repeats."""
    fields: list[dict[str, object]] = [
        {"name": "provider", "kind": "enum", "required": True, "enum": discriminator_enum},
    ]
    fields.extend(
        {
            "name": name,
            "kind": "text",
            "required": False,
            "visible_when": {"field": "provider", "equals": variant},
        }
        for variant in coverage
    )
    return {"fields": fields}


def test_contract_knob_schema_collapses_repeats_covering_the_whole_discriminator() -> None:
    """Full coverage means the knob applies whatever the variant is."""
    projected = _contract_knob_schema(_variant_knobs(["azure", "bedrock"], ["azure", "bedrock"]))

    assert projected == {
        "fields": [
            {"name": "provider", "kind": "enum", "required": True, "enum": ["azure", "bedrock"]},
            {"name": "shared_knob", "kind": "text", "required": False},
        ]
    }


def test_contract_knob_schema_keeps_partial_coverage_per_variant() -> None:
    """``region_name`` under ``bedrock`` alone is a fact, not repetition."""
    projected = _contract_knob_schema(_variant_knobs(["azure", "bedrock", "gateway"], ["azure", "bedrock"]))
    fields = projected["fields"]
    assert isinstance(fields, list)

    assert [field["visible_when"] for field in fields[1:]] == [
        {"field": "provider", "equals": "azure"},
        {"field": "provider", "equals": "bedrock"},
    ]


def test_contract_knob_schema_keeps_repeats_whose_discriminator_is_not_projected() -> None:
    """No visible enum means no variant set, so nothing is known to be covered."""
    projected = _contract_knob_schema(
        {
            "fields": [
                {
                    "name": "shared_knob",
                    "kind": "text",
                    "required": False,
                    "visible_when": {"field": "provider", "equals": variant},
                }
                for variant in ("azure", "bedrock")
            ]
        }
    )
    fields = projected["fields"]
    assert isinstance(fields, list)

    assert len(fields) == 2


def test_contract_knob_schema_keeps_repeats_predicated_on_a_non_string() -> None:
    """A non-string ``equals`` matches no member of a projected enum."""
    projected = _contract_knob_schema(_variant_knobs(["azure", "bedrock"], ["azure", {"one_of": ["bedrock"]}]))
    fields = projected["fields"]
    assert isinstance(fields, list)

    assert len(fields) == 3


def test_contract_knob_schema_keeps_repeats_when_the_discriminator_enum_disagrees() -> None:
    """Two enums under one name name no single variant set."""
    lowered = _variant_knobs(["azure", "bedrock"], ["azure", "bedrock"])
    fields = lowered["fields"]
    assert isinstance(fields, list)
    fields.insert(1, {"name": "provider", "kind": "enum", "required": True, "enum": ["azure"]})

    projected = _contract_knob_schema(lowered)
    projected_fields = projected["fields"]
    assert isinstance(projected_fields, list)

    assert len(projected_fields) == 4


def test_contract_knob_schema_collapses_inside_an_item_schema() -> None:
    """Each field list is its own scope, so a nested union collapses too."""
    projected = _contract_knob_schema(
        {
            "fields": [
                {
                    "name": "queries",
                    "kind": "list",
                    "required": False,
                    "item_schema": _variant_knobs(["azure", "bedrock"], ["azure", "bedrock"]),
                }
            ]
        }
    )
    fields = projected["fields"]
    assert isinstance(fields, list)
    item_schema = fields[0]["item_schema"]
    assert isinstance(item_schema, dict)

    assert item_schema == {
        "fields": [
            {"name": "provider", "kind": "enum", "required": True, "enum": ["azure", "bedrock"]},
            {"name": "shared_knob", "kind": "text", "required": False},
        ]
    }


@pytest.mark.parametrize(
    "authority",
    [
        {"name": "provider", "kind": "enum", "required": False, "enum": ["azure", "bedrock"]},
        {"name": "provider", "kind": "enum", "required": True, "nullable": True, "enum": ["azure", "bedrock"]},
    ],
)
def test_contract_knob_schema_keeps_repeats_under_an_unset_discriminator(authority: dict[str, object]) -> None:
    """Unset, an enum satisfies none of its members, so full coverage is not always-on."""
    lowered = _variant_knobs(["azure", "bedrock"], ["azure", "bedrock"])
    fields = lowered["fields"]
    assert isinstance(fields, list)
    fields[0] = authority

    projected = _contract_knob_schema(lowered)
    projected_fields = projected["fields"]
    assert isinstance(projected_fields, list)

    assert len(projected_fields) == 3
