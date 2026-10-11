"""Multi-table reference joins preserve atomic enrichment and load-time validation."""

from copy import deepcopy
from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms import reference_join
from elspeth.plugins.transforms.reference_join import ReferenceJoin, ReferenceJoinConfig
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_source_context


def options() -> dict[str, Any]:
    return {
        "schema": {"mode": "observed"},
        "tables": {
            "products": {"reference_content": "sku,description,code\nhat,A hat,H\n", "reference_format": "csv"},
            "regions": {"reference_content": "postcode,region\n2000,Sydney\n", "reference_format": "csv"},
        },
        "lookups": [
            {"table": "products", "key_field": "product", "reference_key_name": "sku", "output": {"description": "ref['description']"}},
            {"table": "regions", "key_field": "postcode", "reference_key_name": "postcode", "output": {"region": "ref['region']"}},
        ],
    }


def test_distinct_tables_enrich_atomically_with_per_lookup_audit() -> None:
    transform = ReferenceJoin(options())
    row = make_pipeline_row({"product": "hat", "postcode": "2000"})
    result = transform.process(row, make_source_context())
    assert result.row is not None
    assert result.row.to_dict() == {"product": "hat", "postcode": "2000", "description": "A hat", "region": "Sydney"}
    assert row.to_dict() == {"product": "hat", "postcode": "2000"}
    assert transform.declared_output_fields == frozenset({"description", "region"})
    assert transform.declared_input_fields == frozenset({"product", "postcode"})
    assert result.success_reason is not None
    assert result.success_reason["metadata"]["lookups"] == [
        {"lookup_index": 0, "table": "products", "fields": ["description"], "matched": True, "unresolved_fields": [], "on_miss": "fail"},
        {"lookup_index": 1, "table": "regions", "fields": ["region"], "matched": True, "unresolved_fields": [], "on_miss": "fail"},
    ]


def test_later_miss_returns_no_partial_row_and_value_free_attribution() -> None:
    transform = ReferenceJoin(options())
    original = {"product": "hat", "postcode": "private-missing-key"}
    row = make_pipeline_row(original)
    result = transform.process(row, make_source_context())
    assert result.status == "error"
    assert result.row is None
    assert row.to_dict() == original
    assert result.reason is not None
    assert result.reason["reason"] == "reference_miss"
    assert result.reason["lookup_index"] == 1
    assert result.reason["table"] == "regions"
    assert result.reason["unresolved_fields"] == ["region"]
    assert "private-missing-key" not in repr(result.reason)
    assert "A hat" not in repr(result.reason)


@pytest.mark.parametrize("policy,expected", [("default", "Unknown"), ("null", None)])
def test_each_lookup_applies_its_own_fallback(policy: str, expected: str | None) -> None:
    cfg = options()
    cfg["lookups"][1].update(on_miss=policy, default_values={"region": "Unknown"})
    result = ReferenceJoin(cfg).process(make_pipeline_row({"product": "hat", "postcode": "unknown"}), make_source_context())
    assert result.row is not None
    assert result.row["region"] == expected
    assert result.row["description"] == "A hat"


@pytest.mark.parametrize("value", [None, [], {}, float("inf")])
def test_invalid_input_never_uses_default(value: object) -> None:
    cfg = options()
    cfg["lookups"][1].update(on_miss="default", default_values={"region": "Unknown"})
    result = ReferenceJoin(cfg).process(make_pipeline_row({"product": "hat", "postcode": value}), make_source_context())
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["lookup_index"] == 1
    assert result.row is None


def test_shared_table_parsed_once_but_distinct_match_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = options()
    del cfg["tables"]["regions"]
    cfg["lookups"][1] = {
        "table": "products",
        "key_field": "product_code",
        "reference_key_name": "code",
        "output": {"alternate": "ref['description']"},
    }
    calls: list[str] = []
    parse = reference_join._parse_reference_entries

    def capture(table: reference_join.ReferenceTableConfig) -> list[Any]:
        calls.append(table.reference_content)
        return parse(table)

    monkeypatch.setattr(reference_join, "_parse_reference_entries", capture)
    result = ReferenceJoin(cfg).process(make_pipeline_row({"product": "hat", "product_code": "H"}), make_source_context())
    assert result.row is not None
    assert result.row["alternate"] == "A hat"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change,error",
    [
        ("duplicate_output", "duplicate output"),
        ("chaining", "key_field"),
        ("unknown_table", "unknown table"),
        ("empty_lookups", "at least one lookup"),
        ("mixed", "mutually exclusive"),
        ("unused", "unused"),
        ("bad_csv", "more cells"),
        ("duplicate_keys", "duplicate reference key"),
        ("bad_expression", "broken expression"),
    ],
)
def test_invalid_multi_configuration_rejected_at_load(change: str, error: str) -> None:
    cfg = options()
    if change == "duplicate_output":
        cfg["lookups"][1]["output"] = {"description": "ref['region']"}
    elif change == "chaining":
        cfg["lookups"][1]["key_field"] = "description"
    elif change == "unknown_table":
        cfg["lookups"][1]["table"] = "absent"
    elif change == "empty_lookups":
        cfg["lookups"] = []
    elif change == "mixed":
        cfg["key_field"] = "product"
    elif change == "unused":
        cfg["tables"]["extra"] = deepcopy(cfg["tables"]["products"])
    elif change == "bad_csv":
        cfg["tables"]["products"]["reference_content"] = "sku,description\nhat,A hat,surplus\n"
    elif change == "duplicate_keys":
        cfg["tables"]["products"]["reference_content"] = "sku,description\nhat,A hat\nhat,Other\n"
    else:
        cfg["lookups"][0]["output"] = {"description": "1 / 0"}
    with pytest.raises(PluginConfigError, match=error):
        ReferenceJoinConfig.from_dict(cfg)


@pytest.mark.parametrize("limit_name,limit", [("MAX_REFERENCE_INDEX_CELLS", 1), ("MAX_REFERENCE_INDEX_BYTES", 10)])
def test_limits_apply_across_all_lookups(monkeypatch: pytest.MonkeyPatch, limit_name: str, limit: int) -> None:
    monkeypatch.setattr(reference_join, limit_name, limit)
    with pytest.raises(PluginConfigError, match="limit"):
        ReferenceJoin(options())


def test_multi_inferred_output_types_respect_each_policy() -> None:
    cfg = options()
    cfg["lookups"][1].update(on_miss="null")
    transform = ReferenceJoin(cfg)
    fields = {field.name: field for field in transform._output_schema_config.fields or ()}
    assert fields["description"].field_type == "str"
    assert not fields["description"].nullable
    assert fields["region"].field_type == "str"
    assert fields["region"].nullable


def test_many_independent_lookups_have_no_fixed_count_limit() -> None:
    cfg = options()
    del cfg["tables"]["regions"]
    cfg["lookups"] = [
        {"table": "products", "key_field": "product", "reference_key_name": "sku", "output": {f"description_{i}": "ref['description']"}}
        for i in range(64)
    ]
    result = ReferenceJoin(cfg).process(make_pipeline_row({"product": "hat"}), make_source_context())
    assert result.row is not None
    assert len(result.row.to_dict()) == 65
    assert result.row["description_63"] == "A hat"


def test_sparse_json_default_is_per_field_and_lookup() -> None:
    cfg = options()
    cfg["tables"]["products"] = {
        "reference_content": '[{"sku":"hat","description":"A hat"},{"sku":"coat","description":"A coat","price":10}]',
        "reference_format": "json",
    }
    cfg["lookups"][0].update(
        output={"description": "ref['description']", "price": "ref['price']"},
        on_miss="default",
        default_values={"description": "Unknown", "price": 0},
    )
    result = ReferenceJoin(cfg).process(make_pipeline_row({"product": "hat", "postcode": "2000"}), make_source_context())
    assert result.row is not None
    assert result.row["description"] == "A hat"
    assert result.row["price"] == 0
    assert result.row["region"] == "Sydney"
    assert result.success_reason is not None
    assert result.success_reason["metadata"]["lookups"][0]["unresolved_fields"] == ["price"]


def test_missing_second_input_is_invalid_even_with_default() -> None:
    cfg = options()
    cfg["lookups"][1].update(on_miss="default", default_values={"region": "Unknown"})
    original = make_pipeline_row({"product": "hat"})
    result = ReferenceJoin(cfg).process(original, make_source_context())
    assert result.row is None
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["lookup_index"] == 1
    assert original.to_dict() == {"product": "hat"}


def test_aggregate_cell_budget_rejects_before_evaluating_over_budget_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reference_join, "MAX_REFERENCE_INDEX_CELLS", 1)
    original = reference_join.ExpressionParser.evaluate
    evaluated: list[str] = []

    def track(self: reference_join.ExpressionParser, context: dict[str, Any]) -> Any:
        evaluated.append(str(context))
        return original(self, context)

    monkeypatch.setattr(reference_join.ExpressionParser, "evaluate", track)
    with pytest.raises(PluginConfigError, match="limit"):
        ReferenceJoin(options())
    assert len(evaluated) == 1


@pytest.mark.parametrize("mode", ["python", "json"])
def test_multi_configuration_round_trip_omits_inactive_legacy_fields(mode: str) -> None:
    cfg = ReferenceJoinConfig.from_dict(options())
    serialized = cfg.model_dump(mode=mode)
    assert "key_field" not in serialized
    assert "reference_content" not in serialized
    rebuilt = ReferenceJoinConfig.model_validate(serialized)
    result = ReferenceJoin(rebuilt.model_dump(mode=mode)).process(
        make_pipeline_row({"product": "hat", "postcode": "2000"}), make_source_context()
    )
    assert result.row is not None
    assert result.row["description"] == "A hat"
    assert result.row["region"] == "Sydney"


@pytest.mark.parametrize("mode", ["python", "json"])
def test_single_configuration_round_trip_omits_inactive_multi_fields(mode: str) -> None:
    original = {
        "schema": {"mode": "observed"},
        "reference_content": "sku,description\nhat,A hat\n",
        "reference_format": "csv",
        "key_field": "product",
        "reference_key_name": "sku",
        "output": {"description": "ref['description']"},
    }
    cfg = ReferenceJoinConfig.from_dict(original)
    serialized = cfg.model_dump(mode=mode)
    assert "tables" not in serialized
    assert "lookups" not in serialized
    rebuilt = ReferenceJoinConfig.model_validate(serialized)
    result = ReferenceJoin(rebuilt.model_dump(mode=mode)).process(make_pipeline_row({"product": "hat"}), make_source_context())
    assert result.row is not None
    assert result.row["description"] == "A hat"


@pytest.mark.parametrize("mode", ["python", "json"])
def test_multi_serializer_preserves_dump_selection_and_alias_options(mode: str) -> None:
    cfg = ReferenceJoinConfig.from_dict(options())
    assert set(cfg.model_dump(mode=mode, include={"tables"})) == {"tables"}
    assert "lookups" not in cfg.model_dump(mode=mode, exclude={"lookups"})
    serialized = cfg.model_dump(mode=mode, by_alias=True, exclude_none=True, exclude_defaults=True)
    assert "schema" in serialized
    assert "schema_config" not in serialized
    rebuilt = ReferenceJoinConfig.model_validate(serialized)
    assert rebuilt.model_dump(mode=mode, by_alias=True, exclude_none=True, exclude_defaults=True) == serialized
    assert ReferenceJoinConfig.model_validate_json(cfg.model_dump_json()).model_dump() == cfg.model_dump()


def test_single_serializer_preserves_dump_selection_and_exclude_unset() -> None:
    original = {
        "schema": {"mode": "observed"},
        "reference_content": "sku,description\nhat,A hat\n",
        "reference_format": "csv",
        "key_field": "product",
        "reference_key_name": "sku",
        "output": {"description": "ref['description']"},
    }
    cfg = ReferenceJoinConfig.from_dict(original)
    assert cfg.model_dump(include={"key_field"}) == {"key_field": "product"}
    assert cfg.model_dump(by_alias=True, exclude_unset=True) == {
        **original,
        "schema": {"mode": "observed", "fields": None, "guaranteed_fields": None, "required_fields": None, "audit_fields": None},
    }
    assert ReferenceJoinConfig.model_validate_json(cfg.model_dump_json()).model_dump() == cfg.model_dump()


@pytest.mark.parametrize("mode", ["python", "json"])
def test_serializer_preserves_declared_schema_field_wire_shape(mode: str) -> None:
    original = options()
    original["schema"] = {"mode": "flexible", "fields": ["product: str", "postcode: str"]}
    cfg = ReferenceJoinConfig.from_dict(original)
    serialized = cfg.model_dump(mode=mode, by_alias=True)
    expected_fields = [
        {"name": "product", "field_type": "str", "required": True, "nullable": False},
        {"name": "postcode", "field_type": "str", "required": True, "nullable": False},
    ]
    assert serialized["schema"]["fields"] == (tuple(expected_fields) if mode == "python" else expected_fields)
    assert ReferenceJoinConfig.model_validate(serialized).model_dump(mode=mode, by_alias=True) == serialized


def test_serializer_wire_inventory_and_modes_match_canonical_owned_models() -> None:
    from dataclasses import fields
    from typing import get_args, get_type_hints

    from elspeth.contracts.schema import FieldDefinition, SchemaConfig

    schema_hints = get_type_hints(reference_join._ReferenceSchemaWire)
    canonical_schema_hints = get_type_hints(SchemaConfig)
    assert set(schema_hints) == {field.name for field in fields(SchemaConfig)}
    assert schema_hints["mode"] == canonical_schema_hints["mode"]
    assert get_type_hints(reference_join._ReferenceFieldWire) == get_type_hints(FieldDefinition)
    assert set(get_type_hints(reference_join._ReferenceTableWire)) == set(reference_join.ReferenceTableConfig.model_fields)
    assert set(get_type_hints(reference_join._ReferenceLookupWire)) == set(reference_join.ReferenceLookupConfig.model_fields)
    assert set(get_type_hints(reference_join._ReferenceJoinConfigWire)) == set(ReferenceJoinConfig.model_fields) | {"schema"}
    serialization_schema = ReferenceJoinConfig.model_json_schema(mode="serialization")
    assert serialization_schema["$defs"]["_ReferenceSchemaWire"]["properties"]["mode"]["enum"] == list(
        get_args(canonical_schema_hints["mode"])
    )
