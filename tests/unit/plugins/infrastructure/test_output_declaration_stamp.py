"""The one output-declaration stamp (ADR-050): ``BaseTransform._apply_declared_output_field_contracts``.

Every field a transform creates carries a contract DECLARED before the first
row — the operator's ``schema.fields`` type, else the type the plugin declares
in ``created_output_fields()``, else ``any`` (nullable) — and the stamp
rewrites the emitted contract to that declaration on every emission, so a
node's recorded output contract never depends on which row arrived first.
These tests pin the precedence, the nullable rule, lineage preservation, the
carried-field exclusion, the dynamic-column arm, and the plugin hooks of
value_transform, json_explode, field_mapper and blob_csv_expand.
"""

from __future__ import annotations

import hashlib
from typing import Any

from elspeth.contracts import Determinism, PluginSchema, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.testing import make_field, make_pipeline_row
from tests.fixtures.factories import make_context

DYNAMIC_SCHEMA = {"mode": "observed"}


class _DeclaringTransform(BaseTransform):
    """A field-adding transform whose plugin declaration is configurable per test."""

    name = "declaring_probe"
    determinism = Determinism.DETERMINISTIC
    input_schema = PluginSchema
    output_schema = PluginSchema
    plugin_version = "1.0.0"

    def __init__(self, config: dict[str, Any], *, created: tuple[FieldDefinition, ...] = (), carried: frozenset[str] = frozenset()) -> None:
        super().__init__(config)
        cfg = TransformDataConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._created = created
        self._carried = carried
        self.declared_output_fields = frozenset({"score", "note"}) | carried
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        return self._created

    def carried_output_fields(self) -> frozenset[str]:
        return self._carried

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _emitted(contract_fields: tuple[FieldContract, ...]) -> SchemaContract:
    return SchemaContract(mode="OBSERVED", fields=contract_fields, locked=True)


class TestStampPrecedence:
    def test_a_created_field_known_only_by_name_is_any_nullable_required_declared(self) -> None:
        transform = _DeclaringTransform({"schema": DYNAMIC_SCHEMA})
        stamped = transform._apply_declared_output_field_contracts(_emitted((make_field("score", int, source="inferred", required=False),)))
        score = stamped.get_field("score")
        assert (score.python_type, score.nullable, score.required, score.source) == (object, True, True, "declared")

    def test_the_plugin_type_beats_the_any_default(self) -> None:
        transform = _DeclaringTransform(
            {"schema": DYNAMIC_SCHEMA}, created=(FieldDefinition(name="score", field_type="int", required=True),)
        )
        stamped = transform._apply_declared_output_field_contracts(_emitted((make_field("score", str, source="inferred"),)))
        score = stamped.get_field("score")
        assert (score.python_type, score.nullable, score.required, score.source) == (int, False, True, "declared")

    def test_the_operator_type_beats_the_plugin_type(self) -> None:
        transform = _DeclaringTransform(
            {"schema": {"mode": "flexible", "fields": ["id: int", "score: float"]}},
            created=(FieldDefinition(name="score", field_type="int", required=True),),
        )
        stamped = transform._apply_declared_output_field_contracts(_emitted((make_field("score", int, source="inferred"),)))
        assert stamped.get_field("score").python_type is float

    def test_an_operator_any_beats_the_plugin_type_and_is_nullable(self) -> None:
        """B7: an authored ``any`` is honoured, and ``any`` is nullable even when the string form says otherwise."""
        transform = _DeclaringTransform(
            {"schema": {"mode": "flexible", "fields": ["id: int", "score: any"]}},
            created=(FieldDefinition(name="score", field_type="int", required=True),),
        )
        stamped = transform._apply_declared_output_field_contracts(_emitted((make_field("score", str, source="inferred"),)))
        score = stamped.get_field("score")
        assert (score.python_type, score.nullable, score.source) == (object, True, "declared")

    def test_the_stamp_keeps_the_emitted_original_name(self) -> None:
        """The declaration types a field; it does not rename it."""
        transform = _DeclaringTransform({"schema": {"mode": "flexible", "fields": ["amount: int"]}})
        stamped = transform._apply_declared_output_field_contracts(
            _emitted((make_field("amount", int, original_name="Amount USD", source="inferred"),))
        )
        amount = stamped.get_field("amount")
        assert (amount.original_name, amount.source, amount.python_type) == ("Amount USD", "declared", int)

    def test_a_carried_field_keeps_the_input_contract(self) -> None:
        transform = _DeclaringTransform({"schema": DYNAMIC_SCHEMA}, carried=frozenset({"price"}))
        carried = make_field("price", float, original_name="Price", source="declared", required=True)
        stamped = transform._apply_declared_output_field_contracts(_emitted((carried, make_field("score", int, source="inferred"))))
        assert stamped.get_field("price") == carried
        assert stamped.get_field("score").python_type is object

    def test_a_field_neither_created_nor_declared_is_untouched(self) -> None:
        transform = _DeclaringTransform({"schema": DYNAMIC_SCHEMA})
        passthrough = make_field("id", int, source="inferred", required=False)
        stamped = transform._apply_declared_output_field_contracts(_emitted((passthrough, make_field("score", int, source="inferred"))))
        assert stamped.get_field("id") == passthrough

    def test_dynamic_created_fields_are_stamped_with_the_passed_type(self) -> None:
        transform = _DeclaringTransform({"schema": DYNAMIC_SCHEMA})
        stamped = transform._apply_declared_output_field_contracts(
            _emitted((make_field("col_a", int, source="inferred"), make_field("score", int, source="inferred"))),
            dynamic_created_fields=(FieldDefinition(name="col_a", field_type="str", required=False, nullable=True),),
        )
        col = stamped.get_field("col_a")
        assert (col.python_type, col.required, col.nullable, col.source) == (str, False, True, "declared")

    def test_a_static_declaration_beats_a_dynamic_one(self) -> None:
        transform = _DeclaringTransform(
            {"schema": DYNAMIC_SCHEMA}, created=(FieldDefinition(name="score", field_type="int", required=True),)
        )
        stamped = transform._apply_declared_output_field_contracts(
            _emitted((make_field("score", int, source="inferred"),)),
            dynamic_created_fields=(FieldDefinition(name="score", field_type="str", required=False, nullable=True),),
        )
        assert stamped.get_field("score").python_type is int

    def test_the_stamp_table_is_one_authority(self) -> None:
        """The table the stamp writes is the table a plugin-side pin reads (value_transform's)."""
        transform = _DeclaringTransform(
            {"schema": {"mode": "flexible", "fields": ["id: int", "score: float"]}},
            created=(FieldDefinition(name="note", field_type="str", required=True),),
        )
        table = transform._stamped_output_field_contracts()
        assert {name: (fc.python_type, fc.nullable, fc.source) for name, fc in table.items()} == {
            "id": (int, False, "declared"),
            "score": (float, False, "declared"),
            "note": (str, False, "declared"),
        }


class TestValueTransformDeclaration:
    def test_every_target_is_declared_any_by_the_plugin(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {"schema": DYNAMIC_SCHEMA, "operations": [{"target": "b", "expression": "1"}, {"target": "a", "expression": "2"}]}
        )
        assert transform.created_output_fields() == (
            FieldDefinition(name="a", field_type="any", required=True, nullable=True),
            FieldDefinition(name="b", field_type="any", required=True, nullable=True),
        )
        assert transform.declared_output_fields == frozenset()

    def test_an_explicit_any_target_stores_a_str_where_a_prior_row_stored_an_int(self) -> None:
        """B7 (panel ``vt_str_explicit_any``): both emissions carry the same declared contract."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": ["id: any", "copies: any"]},
                "operations": [{"target": "copies", "expression": "row['meta']['copies']"}],
            }
        )
        ctx = make_context()
        first = transform.process(make_pipeline_row({"id": 1, "meta": {"copies": 2}}), ctx)
        second = transform.process(make_pipeline_row({"id": 2, "meta": {"copies": "two"}}), ctx)
        assert first.status == second.status == "success"
        assert first.row is not None and second.row is not None
        assert (first.row["copies"], second.row["copies"]) == (2, "two")
        assert first.row.contract.version_hash() == second.row.contract.version_hash()
        copies = first.row.contract.get_field("copies")
        assert (copies.python_type, copies.nullable, copies.source) == (object, True, "declared")

    def test_an_overwritten_upstream_field_keeps_its_original_name(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform({"schema": DYNAMIC_SCHEMA, "operations": [{"target": "a", "expression": "row['a'] * 1.5"}]})
        contract = SchemaContract(mode="OBSERVED", fields=(make_field("a", int, original_name="A", source="inferred"),), locked=True)
        result = transform.process(PipelineRow({"a": 10}, contract), make_context())
        assert result.row is not None
        a = result.row.contract.get_field("a")
        assert (a.original_name, a.python_type, a.source) == ("A", object, "declared")

    def test_the_identity_shortcut_never_hides_a_created_field(self) -> None:
        """F1: an emission that types a field from THIS row would make the record unrepresentative.

        ``TransformExecutor`` records the node contract only when the emitted
        contract is not the input contract object. With the stamp, every
        emission of a field-adding node is a new object that carries the same
        declared types, so any recorded emission describes every row.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "score", "expression": "row['score'] * 0.5 if row['half'] else row['score']"}],
            }
        )
        ctx = make_context()
        contract = SchemaContract(
            mode="OBSERVED", fields=(make_field("score", int, source="inferred"), make_field("half", bool, source="inferred")), locked=True
        )
        int_row = transform.process(PipelineRow({"score": 4, "half": False}, contract), ctx)
        float_row = transform.process(PipelineRow({"score": 4, "half": True}, contract), ctx)
        assert int_row.row is not None and float_row.row is not None
        assert int_row.row.contract is not contract
        assert int_row.row.contract.version_hash() == float_row.row.contract.version_hash()
        assert isinstance(int_row.row["score"], int) and isinstance(float_row.row["score"], float)


class TestJsonExplodeDeclaration:
    def test_the_plugin_declares_its_created_fields_in_every_mode(self) -> None:
        from elspeth.plugins.transforms.json_explode import JSONExplode

        observed = JSONExplode({"schema": DYNAMIC_SCHEMA, "array_field": "items", "output_field": "item"})
        assert observed.created_output_fields() == (
            FieldDefinition(name="item", field_type="any", required=True, nullable=True),
            FieldDefinition(name="item_index", field_type="int", required=True, nullable=False),
        )
        no_index = JSONExplode({"schema": DYNAMIC_SCHEMA, "array_field": "items", "output_field": "item", "include_index": False})
        assert no_index.created_output_fields() == (FieldDefinition(name="item", field_type="any", required=True, nullable=True),)

    def test_the_flexible_output_config_reads_the_plugin_declaration_unless_authored(self) -> None:
        from elspeth.plugins.transforms.json_explode import JSONExplode

        transform = JSONExplode(
            {"schema": {"mode": "flexible", "fields": ["doc: any", "items: any"]}, "array_field": "items", "output_field": "item"}
        )
        config = transform._output_schema_config
        assert config is not None and config.fields is not None
        assert {field.name: (field.field_type, field.nullable) for field in config.fields} == {
            "doc": ("any", False),
            "item": ("any", True),
            "item_index": ("int", False),
        }
        authored = JSONExplode(
            {
                "schema": {"mode": "flexible", "fields": ["doc: any", "items: any", "item: int"]},
                "array_field": "items",
                "output_field": "item",
            }
        )
        config = authored._output_schema_config
        assert config is not None and config.fields is not None
        assert next(field for field in config.fields if field.name == "item").field_type == "int"

    def test_a_null_element_is_recorded_nullable_and_a_mixed_array_records_one_contract(self) -> None:
        from elspeth.plugins.transforms.json_explode import JSONExplode

        transform = JSONExplode({"schema": DYNAMIC_SCHEMA, "array_field": "items", "output_field": "item"})
        ctx = make_context()
        mixed = transform.process(make_pipeline_row({"id": 1, "items": [3, None, "x"]}), ctx)
        homogeneous = transform.process(make_pipeline_row({"id": 2, "items": [4]}), ctx)
        assert mixed.rows is not None and homogeneous.rows is not None
        item = mixed.rows[0].contract.get_field("item")
        assert (item.python_type, item.nullable, item.required, item.source) == (object, True, True, "declared")
        assert mixed.rows[1]["item"] is None
        assert mixed.rows[0].contract.version_hash() == homogeneous.rows[0].contract.version_hash()

    def test_an_operator_int_is_stamped_and_left_for_the_engine_to_enforce(self) -> None:
        """The plugin emits; the engine's value check routes the parent (T2, integration)."""
        from elspeth.plugins.transforms.json_explode import JSONExplode

        transform = JSONExplode(
            {"schema": {"mode": "flexible", "fields": ["items: any", "item: int"]}, "array_field": "items", "output_field": "item"}
        )
        result = transform.process(make_pipeline_row({"items": [1, "two"]}), make_context())
        assert result.rows is not None
        item = result.rows[1].contract.get_field("item")
        assert (item.python_type, item.source) == (int, "declared")
        violations = result.rows[1].contract.validate(result.rows[1].to_dict())
        assert [violation.normalized_name for violation in violations] == ["item"]


class TestFieldMapperDeclaration:
    def test_a_dotted_extraction_is_created_any_and_a_flat_rename_is_carried(self) -> None:
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": {"meta.copies": "copies", "amount_usd": "price", "keep": "keep"}})
        assert transform.created_output_fields() == (FieldDefinition(name="copies", field_type="any", required=True, nullable=True),)
        assert transform.carried_output_fields() == frozenset({"price"})

    def test_a_dotted_extraction_records_one_contract_whatever_the_nested_value(self) -> None:
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": {"meta.copies": "copies"}})
        ctx = make_context()
        first = transform.process(make_pipeline_row({"id": 1, "meta": {"copies": 2}}), ctx)
        second = transform.process(make_pipeline_row({"id": 2, "meta": {"copies": "two"}}), ctx)
        assert first.row is not None and second.row is not None
        copies = first.row.contract.get_field("copies")
        assert (copies.python_type, copies.nullable, copies.source) == (object, True, "declared")
        assert first.row.contract.version_hash() == second.row.contract.version_hash()


class TestBlobCsvExpandDeclaration:
    def test_headered_csv_columns_are_declared_str_on_emission(self) -> None:
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
        from tests.unit.plugins.transforms.test_blob_csv_expand import _PayloadStoreFake

        body = b"id,name\n1,alice\n2,bob\n"
        blob_ref = hashlib.sha256(body).hexdigest()
        transform = BlobCSVExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref"})
        transform._payload_store = _PayloadStoreFake({blob_ref: body})
        result = transform.process(make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": blob_ref}), make_context())
        assert result.rows is not None
        contract = result.rows[0].contract
        for column in ("id", "name"):
            field = contract.get_field(column)
            assert (field.python_type, field.required, field.nullable, field.source) == (str, False, True, "declared")
        index = contract.get_field("csv_row_index")
        assert (index.python_type, index.required, index.source) == (int, True, "declared")
        assert contract.get_field("url").source == "inferred"

    def test_declared_columns_keep_their_static_declaration(self) -> None:
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
        from tests.unit.plugins.transforms.test_blob_csv_expand import _PayloadStoreFake

        body = b"1,alice\n2,bob\n"
        blob_ref = hashlib.sha256(body).hexdigest()
        transform = BlobCSVExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref", "columns": ["id", "name"]})
        transform._payload_store = _PayloadStoreFake({blob_ref: body})
        result = transform.process(make_pipeline_row({"blob_ref": blob_ref}), make_context())
        assert result.rows is not None
        name = result.rows[0].contract.get_field("name")
        assert (name.python_type, name.required, name.nullable, name.source) == (str, True, False, "declared")


def test_declare_missing_guaranteed_fields_declares_any_nullable() -> None:
    """D4: a created field known only by name is ``any`` and nullable in the output config too."""
    from elspeth.contracts.schema import declare_missing_guaranteed_fields

    fields = declare_missing_guaranteed_fields((FieldDefinition(name="id", field_type="int"),), ("id", "score"))
    assert fields == (
        FieldDefinition(name="id", field_type="int"),
        FieldDefinition(name="score", field_type="any", required=True, nullable=True),
    )
    assert SchemaConfig(mode="flexible", fields=fields, guaranteed_fields=("id", "score")).get_effective_guaranteed_fields() == {
        "id",
        "score",
    }
