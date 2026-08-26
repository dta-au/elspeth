"""Tests for FieldMapper transform."""

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.testing import make_field, make_pipeline_row
from tests.fixtures.factories import make_context

# Common schema config for dynamic field handling (accepts any fields)
DYNAMIC_SCHEMA = {"mode": "observed"}

# Observed mode forbids explicit field definitions, so declared output field
# metadata reaches the emitted contract only under fixed/flexible mode.
DECLARED_SCHEMA = {"mode": "fixed", "fields": ["dish: str", "cuisine: str"]}


def _run_post_emission_check(transform: BaseTransform, emitted_row: PipelineRow) -> None:
    """Run the ADR-014 post-emission check that the transform executor runs on emission."""
    from elspeth.engine.executors.schema_config_mode import verify_schema_config_mode

    assert transform._output_schema_config is not None
    verify_schema_config_mode(
        output_schema_config=transform._output_schema_config,
        emitted_rows=(emitted_row,),
        plugin_name=transform.name,
        node_id="field_mapper-1",
        run_id="run-1",
        row_id="row-1",
        token_id="token-1",
    )


class TestFieldMapper:
    """Tests for FieldMapper transform plugin."""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    def test_has_required_attributes(self) -> None:
        """FieldMapper has name and schemas."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        assert FieldMapper.name == "field_mapper"

    def test_rename_single_field(self, ctx: PluginContext) -> None:
        """Rename a single field."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"old_name": "new_name"},
            }
        )
        row = {"old_name": "value", "other": 123}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"new_name": "value", "other": 123}
        # Original name remains accessible via contract metadata lineage.
        assert "old_name" in result.row
        assert result.row["old_name"] == "value"

    def test_rename_multiple_fields(self, ctx: PluginContext) -> None:
        """Rename multiple fields at once."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {
                    "first_name": "firstName",
                    "last_name": "lastName",
                },
            }
        )
        row = {"first_name": "Alice", "last_name": "Smith", "id": 1}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"firstName": "Alice", "lastName": "Smith", "id": 1}

    def test_mapping_source_can_use_original_field_name(self, ctx: PluginContext) -> None:
        """Mapping sources resolve original names through the input contract."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"Amount USD": "price"},
            }
        )
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(make_field("amount_usd", float, original_name="Amount USD", required=False, source="inferred"),),
            locked=True,
        )
        row = PipelineRow({"amount_usd": 12.5}, contract)

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"price": 12.5}

    def test_rename_over_existing_field_updates_output_contract(self, ctx: PluginContext) -> None:
        """An overwrite rename must not keep the overwritten field's stale type."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"name": "age"},
            }
        )
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                make_field("name", str, original_name="Name", required=False, source="inferred"),
                make_field("age", int, original_name="Age", required=False, source="inferred"),
            ),
            locked=True,
        )
        row = PipelineRow({"name": "Alice", "age": 40}, contract)

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"age": "Alice"}
        age_field = result.row.contract.get_field("age")
        assert age_field.original_name == "Name"
        assert age_field.python_type is str
        assert result.row.contract.validate(result.row.to_dict()) == []

    def test_select_fields_only(self, ctx: PluginContext) -> None:
        """Only include specified fields (drop others)."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"id": "id", "name": "name"},
                "select_only": True,
            }
        )
        row = {"id": 1, "name": "alice", "secret": "password", "extra": "data"}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"id": 1, "name": "alice"}
        assert "secret" not in result.row
        assert "extra" not in result.row

    def test_missing_field_error(self, ctx: PluginContext) -> None:
        """Error when required field is missing and strict mode enabled."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"required_field": "output"},
                "strict": True,
            }
        )
        row = {"other_field": "value"}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "error"
        assert "required_field" in str(result.reason)

    def test_missing_field_skip_non_strict(self, ctx: PluginContext) -> None:
        """Skip missing fields when strict mode disabled."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"maybe_field": "output"},
                "strict": False,
            }
        )
        row = {"other_field": "value"}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"other_field": "value"}
        assert "output" not in result.row

    def test_default_is_non_strict(self, ctx: PluginContext) -> None:
        """Default behavior is non-strict (skip missing)."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"missing": "output"},
            }
        )
        row = {"exists": "value"}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"

    def test_nested_field_access(self, ctx: PluginContext) -> None:
        """Access nested fields with dot notation."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"meta.source": "origin"},
            }
        )
        row = {"id": 1, "meta": {"source": "api", "timestamp": 123}}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["origin"] == "api"
        assert "meta" in result.row  # Original nested structure preserved

    def test_nested_field_type_mismatch_routes_in_non_strict_mode(self, ctx: PluginContext) -> None:
        """Non-dict intermediate on a dotted path routes to on_error — it does not crash.

        ELSPETH contracts are flat: they describe top-level fields, never nested shape.
        A ``mapping: {"user.name": ...}`` is a config-level expectation that ``user`` is
        a dict; the input contract neither promises nor can promise that. When ``user``
        is a string, no type *contract* was violated — the dotted-path navigation
        *operation* failed on this row's value (the same class as a divide-by-zero on a
        type-valid divisor). That is operation-unsafe Tier-2 data, so the row routes to
        on_error and is recorded; one malformed nested value must not abort the run.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"user.name": "origin"},
                "strict": False,
            }
        )

        result = transform.process(make_pipeline_row({"user": "string_not_dict"}), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "type_mismatch"
        assert result.reason["field"] == "user.name"

    def test_nested_field_type_mismatch_routes_in_strict_mode(self, ctx: PluginContext) -> None:
        """Strict mode must never mask a type mismatch as a missing field (silent skip).

        The original concern this test guarded — that a type mismatch must not be
        swallowed as an absent field — is preserved: a non-navigable dotted path routes
        to on_error with a distinct ``type_mismatch`` reason in BOTH modes. The only
        behaviour change is that the failure no longer crashes the entire run on a single
        malformed row; it is recorded and routed like any other per-row data fault.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"user.name": "origin"},
                "strict": True,
            }
        )

        result = transform.process(make_pipeline_row({"user": "string_not_dict"}), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "type_mismatch"
        assert result.reason["field"] == "user.name"

    def test_empty_mapping_passthrough(self, ctx: PluginContext) -> None:
        """Empty mapping acts as passthrough."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {},
            }
        )
        row = {"a": 1, "b": 2}

        result = transform.process(make_pipeline_row(row), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == row

    def test_backward_probe_rows_drop_mapped_source_field(self, ctx: PluginContext) -> None:
        """Backward invariant probe keeps the real rename/drop path under test."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(FieldMapper.probe_config())
        probe = make_pipeline_row({"baseline": "kept"})

        result = transform.execute_backward_invariant_probe(
            transform.backward_invariant_probe_rows(probe),
            ctx,
        )

        assert result.status == "success"
        assert result.row is not None
        assert result.row["baseline"] == "kept"
        assert result.row["field_mapper_probe_target"] == "mapped"
        assert "field_mapper_probe_source" not in result.row.to_dict()

    def test_requires_schema_config(self) -> None:
        """FieldMapper requires schema configuration."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="schema"):
            FieldMapper({"mapping": {"a": "b"}})

    def test_no_validate_input_attribute(self) -> None:
        """FieldMapper does not carry a validate_input attribute.

        Input validation is unconditional in the executor — plugins
        no longer control this via a flag.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": {"mode": "fixed", "fields": ["count: int"]},
                "mapping": {},
            }
        )

        assert "validate_input" not in dir(transform)

    def test_dynamic_schema_accepts_any_types(self, ctx: PluginContext) -> None:
        """Dynamic schema imposes no type constraints on input.

        The executor validates unconditionally, but dynamic schemas
        accept everything — validation is a no-op.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": {"mode": "observed"},
                "mapping": {},
            }
        )

        result = transform.process(make_pipeline_row({"anything": "goes", "count": "string"}), ctx)
        assert result.status == "success"


class TestFieldMapperDuplicateTargetRejection:
    """Tests for duplicate target field name rejection.

    When multiple source fields map to the same target, the last write wins
    and earlier values are silently lost. This also corrupts contract metadata
    (type/original_name lineage from the wrong source field). The fix rejects
    such mappings at config time.
    """

    def test_duplicate_targets_rejected_at_config_time(self) -> None:
        """Two sources mapping to the same target raises PluginConfigError."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="duplicate target"):
            FieldMapper(
                {
                    "schema": DYNAMIC_SCHEMA,
                    "mapping": {"a": "x", "b": "x"},
                }
            )

    def test_triple_duplicate_targets_rejected(self) -> None:
        """Three sources mapping to the same target raises PluginConfigError."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="duplicate target"):
            FieldMapper(
                {
                    "schema": DYNAMIC_SCHEMA,
                    "mapping": {"a": "z", "b": "z", "c": "z"},
                }
            )

    def test_multiple_distinct_duplicate_targets_rejected(self) -> None:
        """Multiple groups of duplicate targets are all reported."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="duplicate target"):
            FieldMapper(
                {
                    "schema": DYNAMIC_SCHEMA,
                    "mapping": {"a": "x", "b": "x", "c": "y", "d": "y"},
                }
            )

    def test_unique_targets_accepted(self) -> None:
        """Mappings with unique targets are accepted normally."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        # Should not raise
        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"a": "x", "b": "y", "c": "z"},
            }
        )
        assert transform._mapping == {"a": "x", "b": "y", "c": "z"}

    def test_identity_mapping_accepted(self) -> None:
        """Identity mappings (source == target) are accepted."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        # Should not raise
        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"a": "a", "b": "b"},
            }
        )
        assert transform._mapping == {"a": "a", "b": "b"}

    def test_error_message_includes_collision_details(self) -> None:
        """Error message includes which sources collide on which target."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="silent data loss"):
            FieldMapper(
                {
                    "schema": DYNAMIC_SCHEMA,
                    "mapping": {"first_name": "name", "last_name": "name"},
                }
            )

    @pytest.mark.parametrize(
        "mapping",
        [
            {"a": "b", "b": "a"},
            {"a": "b", "b": "c"},
            {"b": "c", "a": "b"},
        ],
    )
    def test_overlapping_rename_graphs_rejected_at_config_time(self, mapping: dict[str, str]) -> None:
        """Targets that are also sources are rejected before they can lose data."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="overlapping rename"):
            FieldMapper(
                {
                    "schema": DYNAMIC_SCHEMA,
                    "mapping": mapping,
                }
            )


class TestFieldMapperOutputSchema:
    """Tests for output schema behavior of shape-changing transforms.

    Per P1-2026-01-19-shape-changing-transforms-output-schema-mismatch:
    Shape-changing transforms must use dynamic output_schema because their
    output shape depends on config (mapping, select_only), not input schema.
    """

    def test_select_only_uses_dynamic_output_schema(self) -> None:
        """FieldMapper with select_only=True uses dynamic output_schema.

        When select_only=True, the output only includes mapped fields,
        which depends on config, not the input schema. Therefore output_schema
        must be dynamic (accepts any fields) to avoid false schema validation.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        # Explicit schema: expects a, b, c
        transform = FieldMapper(
            {
                "schema": {"mode": "fixed", "fields": ["a: str", "b: int", "c: float"]},
                "mapping": {"a": "a"},  # Only select field 'a'
                "select_only": True,
            }
        )

        # Output schema should be dynamic (accepts any fields)
        # because output shape depends on mapping config, not input schema
        output_fields = transform.output_schema.model_fields

        # The fix: output_schema should be dynamic (empty required fields, extra="allow")
        # Currently fails because output_schema = input_schema, which has a, b, c
        assert len(output_fields) == 0, f"Expected dynamic schema with no required fields, got: {list(output_fields.keys())}"

        # Additionally verify extra fields are allowed (dynamic schema behavior)
        config = transform.output_schema.model_config
        assert config.get("extra") == "allow", "Output schema should allow extra fields (dynamic)"


class TestFieldMapperContractPropagation:
    """Tests for FieldMapper contract propagation."""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    def test_contract_contains_renamed_field(self, ctx: PluginContext) -> None:
        """Output contract contains renamed field, not original field name."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"old_field": "new_field"},
            }
        )

        row = make_pipeline_row({"old_field": "value", "other": 42})
        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)

        field_names = {f.normalized_name for f in result.row.contract.fields}
        assert "new_field" in field_names
        assert "old_field" not in field_names
        assert "other" in field_names

    def test_contract_reflects_field_removal(self, ctx: PluginContext) -> None:
        """Output contract doesn't contain removed fields when select_only=True."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"keep_me": "kept"},
                "select_only": True,
            }
        )

        row = make_pipeline_row({"keep_me": "value", "remove_me": 42, "also_remove": "bye"})
        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)

        field_names = {f.normalized_name for f in result.row.contract.fields}
        assert field_names == {"kept"}  # Only the mapped field should remain

    def test_downstream_can_access_renamed_field(self, ctx: PluginContext) -> None:
        """Downstream transforms can access renamed fields via contract."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        mapper = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"source": "target"},
            }
        )

        row = make_pipeline_row({"source": "value", "other": 42})
        result = mapper.process(row, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert isinstance(result.row, PipelineRow)

        # result.row IS already a PipelineRow with contract
        output_row = result.row

        # Downstream access via contract should work
        assert output_row["target"] == "value"
        assert output_row["other"] == 42

        # Original field name remains accessible via contract lineage.
        assert output_row["source"] == "value"

    def test_renamed_field_preserves_original_name_metadata(self, ctx: PluginContext) -> None:
        """Renamed fields preserve source original_name lineage in contract."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DYNAMIC_SCHEMA,
                "mapping": {"amount_usd": "price"},
            }
        )

        input_contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                make_field("amount_usd", float, original_name="Amount USD", required=True, source="declared"),
                make_field("other", int, original_name="Other", required=False, source="inferred"),
            ),
            locked=True,
        )
        row = PipelineRow({"amount_usd": 12.5, "other": 1}, input_contract)

        result = transform.process(row, ctx)
        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)

        renamed = result.row.contract.get_field("price")
        assert renamed is not None
        assert renamed.original_name == "Amount USD"
        assert renamed.python_type is float
        assert renamed.required is True
        assert renamed.source == "declared"


class TestOutputSchemaConfig:
    def test_guaranteed_fields_from_mapping_targets(self):
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"old_name": "new_name", "source": "target"},
                "schema": {"mode": "observed"},
                "strict": True,
            }
        )
        assert transform._output_schema_config is not None
        assert frozenset(transform._output_schema_config.guaranteed_fields) == frozenset({"new_name", "target"})

    def test_non_strict_optional_mapping_does_not_declare_or_guarantee_target(self) -> None:
        """A skipped non-strict mapping target is not present on every successful row."""
        from elspeth.engine.executors.declared_output_fields import verify_declared_output_fields
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"maybe_field": "output"},
                "schema": {"mode": "observed"},
                "strict": False,
            }
        )
        assert transform.declared_output_fields == frozenset()
        assert transform._output_schema_config is not None
        assert transform._output_schema_config.guaranteed_fields is None

        result = transform.process(make_pipeline_row({"other_field": "value"}), make_context())

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"other_field": "value"}
        verify_declared_output_fields(
            declared_output_fields=transform.declared_output_fields,
            emitted_rows=(result.row,),
            plugin_name=transform.name,
            node_id="node",
            run_id="run",
            row_id="row",
            token_id="token",
        )

    def test_non_strict_mapping_from_guaranteed_source_declares_target(self) -> None:
        """A non-strict mapping can guarantee its target when the source is guaranteed upstream."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"source": "target"},
                "schema": {"mode": "observed", "guaranteed_fields": ["source", "kept"]},
                "strict": False,
            }
        )

        assert transform.declared_output_fields == frozenset({"target"})
        assert transform._output_schema_config is not None
        assert transform._output_schema_config.guaranteed_fields is not None
        assert frozenset(transform._output_schema_config.guaranteed_fields) == frozenset({"target", "kept"})

    def test_select_only_output_schema_does_not_declare_renamed_away_source(self) -> None:
        """A renamed source is CONSUMED, not emitted, so it is not an output field.

        Regression: elspeth-a2bf676e6f. ``_build_field_mapper_output_schema_config``
        carried the AUTHORED INPUT ``fields`` straight onto the output config, so a
        source the mapper renames away stayed declared as a required OUTPUT field it
        provably never emits. ``get_effective_guaranteed_fields()`` then demanded it
        and the composer's transform-contract rule rejected a correct pipeline.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"id": "id", "rating_text": "rating"},
                "select_only": True,
                "schema": {
                    "mode": "flexible",
                    "fields": ["id: int", "rating_text: str"],
                    "guaranteed_fields": ["id", "rating_text"],
                },
            }
        )

        output_config = transform._output_schema_config
        assert output_config is not None
        declared = {field.name for field in output_config.fields or ()}
        assert "rating_text" not in declared
        assert frozenset(output_config.get_effective_guaranteed_fields()) == frozenset({"id", "rating"})

    def test_select_only_renamed_target_keeps_the_source_declared_type(self) -> None:
        """The rename moves a value, so the target carries the source's authored type.

        Regression: elspeth-a2bf676e6f. Dropping the source declaration must not
        downgrade the target to ``any`` — the mapper copies the value unchanged,
        so its declared type is known.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"id": "id", "rating_text": "rating"},
                "select_only": True,
                "schema": {
                    "mode": "flexible",
                    "fields": ["id: int", "rating_text: str"],
                    "guaranteed_fields": ["id", "rating_text"],
                },
            }
        )

        output_config = transform._output_schema_config
        assert output_config is not None
        by_name = {field.name: field for field in output_config.fields or ()}
        assert by_name["rating"].field_type == "str"

    def test_select_only_emitted_row_satisfies_its_own_output_contract(self) -> None:
        """The ADR-014 post-emission check passes on the row the mapper actually emits.

        Regression: elspeth-a2bf676e6f. This is the end-to-end statement of the
        defect — the transform's own output contract must accept its own row.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"id": "id", "rating_text": "rating"},
                "select_only": True,
                "schema": {
                    "mode": "flexible",
                    "fields": ["id: int", "rating_text: str"],
                    "guaranteed_fields": ["id", "rating_text"],
                },
            }
        )

        result = transform.process(make_pipeline_row({"id": 1, "rating_text": "4"}), make_context())

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"id": 1, "rating": "4"}
        _run_post_emission_check(transform, result.row)

    def test_passthrough_output_schema_does_not_declare_renamed_away_source(self) -> None:
        """Without select_only, process() still renames the source away.

        Regression: elspeth-a2bf676e6f. The deep-copy branch keeps UNMAPPED
        fields but a renamed source is deleted from the row, so its authored
        declaration must not survive onto the output contract either.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"rating_text": "rating"},
                "select_only": False,
                "schema": {
                    "mode": "flexible",
                    "fields": ["id: int", "rating_text: str"],
                    "guaranteed_fields": ["id", "rating_text"],
                },
            }
        )

        result = transform.process(make_pipeline_row({"id": 1, "rating_text": "4"}), make_context())

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"id": 1, "rating": "4"}
        _run_post_emission_check(transform, result.row)

    def test_identity_mapped_original_header_abstains_passthrough_guarantees(self) -> None:
        """An identity-mapped original header renames a normalized field away.

        ``{"Amount USD": "Amount USD"}`` deletes the normalized ``amount_usd``
        key at runtime and writes the literal ``"Amount USD"`` key, so the
        constructor cannot name the removed field. Claiming the upstream
        guarantees as pass-through would promise ``amount_usd`` on rows that
        no longer carry it — the abstain arm must cover identity mappings,
        not only renames to a different target.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"Amount USD": "Amount USD"},
                "schema": {"mode": "observed", "guaranteed_fields": ["amount_usd", "kept"]},
            }
        )

        assert transform._output_schema_config is not None
        guarantees = frozenset(transform._output_schema_config.guaranteed_fields or ())
        assert "amount_usd" not in guarantees
        assert "kept" not in guarantees

    def test_unresolved_original_header_abstains_from_passthrough_declarations(self) -> None:
        """The unnameable removal costs the whole passthrough declaration set.

        Regression: elspeth-a2bf676e6f. ``{"Amount USD": "Amount USD"}`` deletes a
        normalized key only ``contract.resolve_name`` can name at runtime, so the
        constructor cannot say WHICH forwarded column stopped being emitted.

        Dropping the declarations is NOT the safe direction, and an earlier
        revision of this fix did exactly that. ``fields`` feeds two OPPOSED
        limbs: declared-REQUIRED names union into
        ``get_effective_guaranteed_fields`` (over-declaring promises rows that
        may not arrive), while the declared NAMES are the fixed-mode extras
        allow-list in ``verify_schema_config_mode`` (under-declaring rejects
        rows that do). Declaring the forwarded columns OPTIONAL satisfies both.
        Pinned under ``mode: fixed`` on purpose — under ``flexible`` the extras
        limb never runs, which is how the regression hid.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"Amount USD": "Amount USD"},
                "select_only": False,
                "schema": {"mode": "fixed", "fields": ["amount_usd: float", "kept: str"]},
            }
        )

        assert transform._output_schema_config is not None
        by_name = {field.name: field for field in transform._output_schema_config.fields or ()}
        assert set(by_name) == {"amount_usd", "kept"}
        assert not any(field.required for field in by_name.values())
        assert transform._output_schema_config.get_effective_guaranteed_fields() == frozenset()

        result = transform.process(make_pipeline_row({"amount_usd": 1.5, "kept": "x"}), make_context())

        assert result.status == "success"
        assert result.row is not None
        _run_post_emission_check(transform, result.row)

    def test_rename_target_declared_without_its_source_keeps_that_declaration(self) -> None:
        """An author may declare the EMITTED name instead of the consumed one.

        Regression: elspeth-a2bf676e6f. The projection prefers the source's
        declaration because a rename moves that value, but an author who
        declared only the target still declared the emitted column — so the
        target's own declaration is the fallback, not a discard. That fallback
        limb had no coverage: deleting it left 10,213 tests green, while the
        comment above it asserts the shape is supported.

        Distinct from ``test_rename_collision_is_described_by_the_source_that_lands_there``,
        where BOTH sides are declared and the source must win.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"a": "b"},
                "select_only": True,
                "schema": {"mode": "flexible", "fields": ["b: str"]},
            }
        )

        assert transform._output_schema_config is not None
        by_name = {field.name: field for field in transform._output_schema_config.fields or ()}
        assert by_name["b"].field_type == "str", "the target's authored declaration must survive when the source carries none"

    def test_rename_collision_is_described_by_the_source_that_lands_there(self) -> None:
        """When a rename target is also an authored field, the SOURCE wins.

        Regression: elspeth-a2bf676e6f. ``{"a": "b"}`` overwrites ``b`` with
        ``a``'s value, so ``a``'s declaration describes the emitted column.
        Preferring the target's authored declaration silently re-typed the
        column and let the declared-output restamp stamp the wrong type over it.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"a": "b"},
                "select_only": False,
                "schema": {"mode": "fixed", "fields": ["a: int", "b: str"], "guaranteed_fields": ["a", "b"]},
            }
        )

        assert transform._output_schema_config is not None
        by_name = {field.name: field for field in transform._output_schema_config.fields or ()}
        assert by_name["b"].field_type == "int"

        result = transform.process(make_pipeline_row({"a": 1, "b": "orig"}), make_context())

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict() == {"b": 1}
        _run_post_emission_check(transform, result.row)

    def test_passthrough_keeps_unmapped_declarations_when_every_source_is_nameable(self) -> None:
        """The abstention above is scoped to unnameable removals, not to renames.

        Regression: elspeth-a2bf676e6f. With only statically-nameable sources the
        constructor knows exactly which field is renamed away, so the surviving
        columns keep their authored declarations.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"rating_text": "rating"},
                "select_only": False,
                "schema": {"mode": "flexible", "fields": ["kept: str", "rating_text: str"]},
            }
        )

        assert transform._output_schema_config is not None
        by_name = {field.name: field for field in transform._output_schema_config.fields or ()}
        assert set(by_name) == {"kept", "rating"}
        assert by_name["rating"].field_type == "str"

    def test_guaranteed_fields_empty_mapping(self):
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {},
                "schema": {"mode": "observed"},
            }
        )
        assert transform._output_schema_config is not None
        # Empty mapping with no upstream guaranteed_fields → abstain (None)
        assert transform._output_schema_config.guaranteed_fields is None

    def test_upstream_none_guaranteed_with_mapping_produces_explicit(self):
        """Strict mapping with upstream guaranteed_fields=None can declare target guarantees."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"old": "new"},
                "schema": {"mode": "observed"},
                "strict": True,
                # No guaranteed_fields key → upstream is None (abstain)
            }
        )
        assert transform._output_schema_config is not None
        # Transform adds "new" via mapping, so it CAN guarantee something
        assert transform._output_schema_config.guaranteed_fields is not None
        assert "new" in transform._output_schema_config.guaranteed_fields

    def test_upstream_declared_empty_produces_explicit_empty(self):
        """Upstream guaranteed_fields=[] (parsed as None) + empty mapping → abstain.

        When upstream has no guaranteed_fields AND the mapping adds nothing,
        the transform should abstain (None), not declare empty guarantees.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {},
                "schema": {"mode": "observed", "guaranteed_fields": ["x"]},
                "select_only": True,
            }
        )
        assert transform._output_schema_config is not None
        # select_only with empty mapping produces no fields, but upstream declared → ()
        # Actually mapping is empty so output_fields is empty, but upstream declared
        # so we should get explicit empty tuple
        assert transform._output_schema_config.guaranteed_fields is not None
        assert transform._output_schema_config.guaranteed_fields == ()

    def test_declared_output_fields_set_from_mapping(self):
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"a": "b", "c": "d"},
                "schema": {"mode": "observed"},
                "strict": True,
            }
        )
        assert transform.declared_output_fields == frozenset({"b", "d"})

    def test_declared_output_fields_excludes_identity_mappings(self):
        """Identity mappings (same source and target) are excluded from declared fields."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"score": "score", "name": "display_name"},
                "schema": {"mode": "observed"},
                "strict": True,
            }
        )
        # "score" → "score" is identity (excluded), "name" → "display_name" is a rename (included)
        assert transform.declared_output_fields == frozenset({"display_name"})

    def test_original_header_rename_does_not_retain_unresolved_source_guarantee(self) -> None:
        """Original-header sources are not treated as normalized guarantee keys."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"Amount USD": "price"},
                "schema": {"mode": "observed", "guaranteed_fields": ["amount_usd"]},
            }
        )

        assert transform.declared_output_fields == frozenset()
        assert transform._output_schema_config is not None
        assert transform._output_schema_config.guaranteed_fields == ()

    def test_original_header_identity_mapping_does_not_declare_target_as_new_field(self) -> None:
        """Original-name identity mappings must not trigger false collision checks."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"Amount USD": "amount_usd"},
                "schema": {"mode": "observed"},
            }
        )

        assert transform.declared_output_fields == frozenset()


class TestFieldMapperOutputSchemaContract:
    """Tests for FieldMapper _output_schema_config reflecting actual output shape.

    Bug fix: FieldMapper called _build_output_schema_config() which copies input
    fields into output guarantees. But FieldMapper removes/renames fields, so the
    output shape differs from input. The fix builds a custom output schema config.
    """

    def test_select_only_output_guarantees_are_only_targets(self):
        """select_only=True: guaranteed fields are ONLY the mapping targets."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"a": "x", "b": "y"},
                "select_only": True,
                "schema": {"mode": "observed", "guaranteed_fields": ["a", "b", "c"]},
            }
        )
        assert transform._output_schema_config is not None
        guaranteed = frozenset(transform._output_schema_config.guaranteed_fields)
        # Only mapping targets, not input fields
        assert guaranteed == frozenset({"x", "y"})
        # Input fields that were dropped should NOT be present
        assert "a" not in guaranteed
        assert "b" not in guaranteed
        assert "c" not in guaranteed

    def test_rename_removes_source_adds_target_in_guarantees(self):
        """Rename mapping removes source field and adds target in guaranteed_fields."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"old_name": "new_name"},
                "schema": {"mode": "observed", "guaranteed_fields": ["old_name", "keep_me"]},
            }
        )
        assert transform._output_schema_config is not None
        guaranteed = frozenset(transform._output_schema_config.guaranteed_fields)
        assert "new_name" in guaranteed
        assert "keep_me" in guaranteed
        assert "old_name" not in guaranteed

    def test_identity_mapping_preserves_field_in_guarantees(self):
        """Identity mapping (source == target) keeps the field in guaranteed_fields."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "mapping": {"id": "id"},
                "schema": {"mode": "observed", "guaranteed_fields": ["id", "name"]},
            }
        )
        assert transform._output_schema_config is not None
        guaranteed = frozenset(transform._output_schema_config.guaranteed_fields)
        assert "id" in guaranteed
        assert "name" in guaranteed


class TestFieldMapperDeclaredOutputFieldContracts:
    """Tests for declared output field metadata on emitted contracts (elspeth-ed2c2315d7).

    Contract propagation infers a field created by an upstream transform as
    ``required=False, source="inferred"``. When the author also declares that
    field in ``schema.fields``, FieldMapper copies the declaration into its
    output schema config, so ADR-014's post-emission check compares declared
    ``required``/``nullable``/``python_type`` against the inferred metadata and
    fails the run unless FieldMapper restamps the declaration on emission.
    """

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    def test_select_only_restamps_declared_metadata_over_inferred_upstream_field(self, ctx: PluginContext) -> None:
        """A declared field created upstream emits with its declared metadata."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DECLARED_SCHEMA,
                "mapping": {"dish": "dish", "cuisine": "cuisine"},
                "select_only": True,
            }
        )
        row = PipelineRow(
            {"dish": "laksa", "cuisine": "Malaysian"},
            SchemaContract(
                mode="FIXED",
                fields=(
                    make_field("dish", str, required=True, source="declared"),
                    # An upstream LLM transform created 'cuisine', so contract
                    # propagation inferred it as optional.
                    make_field("cuisine", str, required=False, source="inferred"),
                ),
                locked=True,
            ),
        )

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)
        _run_post_emission_check(transform, result.row)
        emitted = result.row.contract.get_field("cuisine")
        assert emitted.required is True
        assert emitted.nullable is False
        assert emitted.python_type is str

    def test_rename_without_select_only_restamps_declared_metadata(self, ctx: PluginContext) -> None:
        """Renames carry source metadata forward, so they need the same restamp."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DECLARED_SCHEMA,
                "mapping": {"raw_cuisine": "cuisine"},
            }
        )
        row = PipelineRow(
            {"dish": "laksa", "raw_cuisine": "Malaysian"},
            SchemaContract(
                mode="FIXED",
                fields=(
                    make_field("dish", str, required=True, source="declared"),
                    make_field("raw_cuisine", str, required=False, source="inferred"),
                ),
                locked=True,
            ),
        )

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)
        _run_post_emission_check(transform, result.row)
        assert result.row.contract.get_field("cuisine").required is True

    def test_restamp_does_not_mask_a_declared_field_missing_from_the_row(self, ctx: PluginContext) -> None:
        """Restamping declared metadata must not invent an absent declared field.

        ``cuisine`` is a MAPPING TARGET here, so it is genuinely declared on the
        output contract; the upstream row simply never carried it. Before
        elspeth-a2bf676e6f this scenario was written with ``cuisine`` merely
        declared in the authored INPUT ``schema.fields`` and whitelisted away by
        the mapping — which the output contract no longer claims, so it could no
        longer exercise the restamp. The guard being pinned is the restamp, not
        the declaration copying, so it moves onto a field the output really does
        promise.
        """
        from elspeth.contracts.errors import SchemaConfigModeViolation
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DECLARED_SCHEMA,
                "mapping": {"dish": "dish", "cuisine": "cuisine"},
                "select_only": True,
            }
        )
        row = PipelineRow(
            {"dish": "laksa"},
            SchemaContract(
                mode="FIXED",
                fields=(make_field("dish", str, required=True, source="declared"),),
                locked=True,
            ),
        )

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)
        assert "cuisine" not in result.row.to_dict()

        with pytest.raises(SchemaConfigModeViolation) as exc_info:
            _run_post_emission_check(transform, result.row)

        assert tuple(exc_info.value.payload["missing_required_fields"]) == ("cuisine",)
        # Absence is reported as a missing guarantee, not as metadata drift:
        # the mismatch collector only inspects fields the row actually carries.
        assert "field_metadata_mismatches" not in exc_info.value.payload

    def test_select_only_whitelist_drops_a_non_selected_field_from_the_output_contract(self, ctx: PluginContext) -> None:
        """Cleanup is the documented purpose of select_only, so it must validate.

        Regression: elspeth-a2bf676e6f. ``schema.fields`` is the INPUT contract,
        so declaring a field there and omitting it from the whitelist is the
        ordinary "save only these fields" gesture — not a contradiction. The
        output contract must stop promising the dropped field, and the emitted
        row must satisfy its own contract.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": DECLARED_SCHEMA,
                "mapping": {"dish": "dish"},
                "select_only": True,
            }
        )
        row = PipelineRow(
            {"dish": "laksa", "cuisine": "Malaysian"},
            SchemaContract(
                mode="FIXED",
                fields=(
                    make_field("dish", str, required=True, source="declared"),
                    make_field("cuisine", str, required=False, source="inferred"),
                ),
                locked=True,
            ),
        )

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)
        assert result.row.to_dict() == {"dish": "laksa"}
        assert transform._output_schema_config is not None
        assert {field.name for field in transform._output_schema_config.fields or ()} == {"dish"}
        _run_post_emission_check(transform, result.row)


def test_select_only_assistance_requires_every_downstream_field() -> None:
    """Discovery guidance names the contract obligation behind a whitelist."""
    from elspeth.plugins.transforms.field_mapper import FieldMapper

    assistance = FieldMapper.get_agent_assistance()
    assert assistance is not None
    rendered = " ".join(assistance.composer_hints)

    assert "every field required by the downstream sink" in rendered


class TestFieldMapperOriginalHeaderClassification:
    """Which mapping sources name a row key, and which only resolve at runtime.

    Regression: elspeth-f262a8c678. ``_is_unresolved_original_source`` used
    ``not source.isidentifier()`` as its proxy for "an original header resolved
    only at runtime". ``normalize_field_name`` LOWERCASES and keyword-suffixes,
    so every case-variant header ('B', 'Name', 'userID', 'class') is an
    identifier that a normalized header never yields — the proxy caught only
    the visibly messy 'First Name' class. Four declarations consume the
    predicate, and each one then described a field the transform does not emit.

    The parametrize column reads ``names_a_row_key`` for symmetry with the
    helper, but what it pins is "is a normalization fixed point". A row CAN be
    keyed 'B' — headerless ``columns`` and a source's ``field_mapping`` values
    are identifier-checked but never lowercased — so False here means "may not
    name a row key", which is what makes abstention the only safe answer, not
    "no row is keyed by this" (elspeth-bb470636d1).
    """

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    @pytest.mark.parametrize(
        ("source", "names_a_row_key"),
        [
            ("name", True),
            ("first_name", True),
            ("field2", True),
            ("café", True),
            ("B", False),
            ("Name", False),
            ("userID", False),
            ("ID", False),
            ("class", False),
            ("Ünicode", False),
            ("First Name", False),
            ("2fast", False),
            ("!!!", False),
            ("", False),
        ],
        ids=repr,
    )
    def test_only_normalization_fixed_points_name_a_row_key(self, source: str, names_a_row_key: bool) -> None:
        """The guard tests normalization's fixed points, not Python's identifier alphabet."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        assert FieldMapper._is_static_normalized_source(source) is names_a_row_key
        assert FieldMapper._is_unresolved_original_source(source) is not names_a_row_key

    @pytest.mark.parametrize("source", ["meta.name", "meta.Name", "Meta.name", "a.b.c"])
    def test_dotted_sources_are_neither_row_key_nor_original_header(self, source: str) -> None:
        """A dotted source is a nested read, so neither predicate claims it."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        assert FieldMapper._is_static_normalized_source(source) is False
        assert FieldMapper._is_unresolved_original_source(source) is False

    @pytest.mark.parametrize(
        "header",
        ["name", "first_name", "field2", "B", "Name", "userID", "ID", "class", "First Name", "2fast"],
        ids=repr,
    )
    def test_static_classification_agrees_with_the_runtime_resolver(self, header: str) -> None:
        """The predicate answers the question ``process`` actually asks of a row.

        ``process`` deletes ``row.contract.resolve_name(source)``, so a source
        is statically nameable exactly when the resolver returns the literal
        unchanged. Cross-checking against the resolver — rather than against
        ``normalize_field_name`` again — keeps this from restating the
        implementation.
        """
        from elspeth.plugins.sources.field_normalization import normalize_field_name
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(make_field(normalize_field_name(header), str, original_name=header, required=True, source="declared"),),
            locked=True,
        )

        assert FieldMapper._is_static_normalized_source(header) is (contract.resolve_name(header) == header)

    def test_case_variant_source_does_not_promise_the_name_it_deletes(self, ctx: PluginContext) -> None:
        """A case-variant rename abstains instead of guaranteeing a deleted column.

        At HEAD the guard called 'Name' statically nameable, so the output
        config kept 'name' as a passthrough guarantee while ``process``
        deleted it — the transform's own post-emission check then rejected its
        own row.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper(
            {
                "schema": {"mode": "flexible", "fields": ["name: str", "keep: str"], "guaranteed_fields": ["name", "keep"]},
                "mapping": {"Name": "full_name"},
                "select_only": False,
                "strict": True,
            }
        )
        row = PipelineRow(
            {"name": "Ada", "keep": "kept"},
            SchemaContract(
                mode="FLEXIBLE",
                fields=(
                    make_field("name", str, original_name="Name", required=True, source="declared"),
                    make_field("keep", str, original_name="keep", required=True, source="declared"),
                ),
                locked=True,
            ),
        )

        assert transform._output_schema_config is not None
        assert "name" not in (transform._output_schema_config.guaranteed_fields or ())
        assert transform.forwards_input_fields is False
        assert transform.removed_input_fields == frozenset()
        assert transform.declared_output_fields == frozenset()

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)
        assert result.row.to_dict() == {"full_name": "Ada", "keep": "kept"}
        _run_post_emission_check(transform, result.row)

    @pytest.mark.parametrize("case_variant_source", ["Name", "userID", "ID", "class"], ids=repr)
    def test_case_variant_sources_declare_exactly_like_the_messy_header_class(self, case_variant_source: str) -> None:
        """The fix makes the two halves of "original header" agree.

        'First Name' already abstained at HEAD. A case variant resolves through
        the same ``contract.resolve_name`` path, so every declaration it drives
        must match — that equivalence is the whole content of the fix.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        def declarations(source: str) -> tuple[object, ...]:
            transform = FieldMapper(
                {
                    "schema": {"mode": "flexible", "fields": ["name: str", "keep: str"], "guaranteed_fields": ["name", "keep"]},
                    "mapping": {source: "mapped"},
                    "select_only": False,
                    "strict": True,
                }
            )
            assert transform._output_schema_config is not None
            return (
                transform.forwards_input_fields,
                transform.removed_input_fields,
                transform.declared_output_fields,
                transform._output_schema_config.guaranteed_fields,
            )

        assert declarations(case_variant_source) == declarations("First Name")

    @pytest.mark.parametrize("source", ["", "!!!", "   "], ids=repr)
    def test_an_empty_normalizing_mapping_key_still_constructs(self, source: str) -> None:
        """``normalize_field_name`` raising must not change the construction contract.

        It raises ``ExternalHeaderError`` for a key that normalizes to nothing.
        The guard answers it the way ``value_transform._row_key_aliases`` does —
        such a literal names no field, so it is an unresolved original header —
        rather than letting a new exception class escape ``__init__``.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": {"mode": "observed"}, "mapping": {source: "mapped"}})

        assert FieldMapper._is_unresolved_original_source(source) is True
        assert transform.forwards_input_fields is False


class TestFieldMapperOverlapNormalization:
    """The overlap rule reads the row key ``process`` deletes (elspeth-bb470636d1).

    ``_reject_overlapping_rename_graphs`` compared LITERAL config strings while
    ``process`` deletes a renamed source by a key it picks at runtime, so a
    case-variant source slipped a rename chain past construction and then lost
    data exactly like the literal chain that is rejected.
    """

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    @pytest.mark.parametrize(
        ("literal_chain", "aliased_chain"),
        [
            ({"a": "b", "b": "c"}, {"a": "b", "B": "c"}),
            ({"a": "b", "b": "c"}, {"A": "b", "b": "c"}),
            ({"b": "c", "a": "b"}, {"b": "c", "a": "B"}),
            ({"a": "b", "b": "a"}, {"a": "b", "B": "a"}),
            ({"a": "class_", "class_": "c"}, {"a": "class_", "class": "c"}),
            ({"a": "first_name", "first_name": "c"}, {"a": "first_name", "First Name": "c"}),
        ],
        ids=repr,
    )
    def test_an_aliased_chain_is_rejected_like_its_literal_twin(
        self,
        literal_chain: dict[str, str],
        aliased_chain: dict[str, str],
    ) -> None:
        """Two chains that behave identically at runtime must be judged identically.

        The aliased half is the defect: it reached ``process`` and deleted the
        normalized key another entry had just written.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        for mapping in (literal_chain, aliased_chain):
            with pytest.raises(PluginConfigError, match="overlapping rename"):
                FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": mapping})

    @pytest.mark.parametrize(
        "mapping",
        [
            {"A": "a"},
            {"Name": "name"},
            {"First Name": "first_name"},
            {"class": "class_"},
            {"A": "a", "b": "c"},
        ],
        ids=repr,
    )
    def test_a_canonical_identity_mapping_is_not_an_overlap(self, mapping: dict[str, str]) -> None:
        """An original header mapped onto its own row key is a no-op, not a chain.

        ``process`` deletes the normalized key and writes the same value back
        under the same name. Judging identity on the LITERAL strings would call
        every one of these a rename onto a live source and reject a working
        config at construction.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": mapping})

        assert transform._mapping == mapping

    @pytest.mark.parametrize(
        "mapping",
        [
            {"meta.source": "a", "meta_source": "b"},
            {"meta.source": "x", "y": "meta_source"},
            {"a.b": "x", "y": "a_b"},
        ],
        ids=repr,
    )
    def test_a_dotted_source_is_not_normalized_into_a_false_overlap(self, mapping: dict[str, str]) -> None:
        """A dotted source is a nested read, so it is keyed by its literal text.

        ``normalize_field_name('meta.source')`` is ``'meta_source'``, so
        canonicalising a dotted name would make the nested read answer to a
        sibling's plain field name and reject a config ``process`` runs
        correctly — the second case is the one that discriminates, since it puts
        that invented key on the TARGET side where membership is tested.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": mapping})

        assert transform._mapping == mapping

    def test_a_dotted_source_still_overlaps_on_its_literal_target(self) -> None:
        """The nested read writes a key a later entry then deletes — still a chain."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="overlapping rename"):
            FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": {"meta.name": "x", "x": "y"}})

    @pytest.mark.parametrize(
        "mapping",
        [
            {"": "mapped", "kept": "moved"},
            {"!!!": "mapped", "kept": "moved"},
            {"   ": "mapped", "kept": "moved"},
            {"!!!": "mapped", "a": "???"},
            {"": "mapped", "a": "   "},
        ],
        ids=repr,
    )
    def test_a_key_that_normalizes_to_nothing_is_its_own_overlap_key(self, mapping: dict[str, str]) -> None:
        """Such a literal names no field, so it can alias nothing and constructs.

        It must also keep its OWN identity rather than collapsing onto a shared
        empty key: the last two cases put an unnormalizable literal on the target
        side, where a collapsed key would match an unrelated source and reject a
        config that names no common field at all.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": mapping})

        assert transform._mapping == mapping

    @pytest.mark.parametrize(
        "header",
        ["name", "first_name", "field2", "B", "Name", "userID", "ID", "class", "First Name", "2fast"],
        ids=repr,
    )
    def test_the_overlap_key_agrees_with_the_runtime_resolver(self, header: str) -> None:
        """The key the rule compares is the key ``process`` asks the row for.

        Cross-checked against ``SchemaContract.resolve_name`` rather than against
        ``normalize_field_name`` again, so the assertion cannot restate the
        implementation.
        """
        from elspeth.plugins.sources.field_normalization import normalize_field_name
        from elspeth.plugins.transforms.field_mapper import _canonical_row_key

        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(make_field(normalize_field_name(header), str, original_name=header, required=True, source="declared"),),
            locked=True,
        )

        assert _canonical_row_key(header) == contract.resolve_name(header)

    def test_a_non_canonical_target_colliding_with_a_source_is_rejected(self) -> None:
        """A target is canonicalised too, and the over-rejection is deliberate.

        ``{'a': 'B', 'b': 'c'}`` is measurably lossless — two keys in, two keys
        out, in either order. It is rejected anyway because no contract exists at
        construction to say whether the row keys that field under ``'B'`` or
        under ``'b'``, and the ``'b'`` reading is ``{'a': 'B', 'B': 'c'}``, which
        destroys a value through ``process``'s literal ``source in output``
        branch. Fail closed.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="overlapping rename"):
            FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": {"a": "B", "b": "c"}})

    @pytest.mark.parametrize("mapping", [{"B": "b", "b": "y"}, {"b": "B", "B": "y"}], ids=repr)
    def test_a_literal_source_that_is_itself_a_row_key_still_overlaps(self, mapping: dict[str, str]) -> None:
        """A row can carry ``'B'`` and ``'b'`` as two DISTINCT keys, so this is a real chain.

        A source's ``field_mapping`` values bypass ``normalize_field_name``
        (``resolve_field_names`` validates them with ``isidentifier()`` alone)
        and headerless ``columns`` are taken as already-clean identifiers, so
        neither is ever lowercased. Judging ``{'B': 'b'}`` a canonical no-op
        therefore waves through a two-field rename chain that destroys one value
        — measured: ``{'B': '1', 'b': '2'}`` emits ``{'y': '2'}``, and the
        reversed spelling emits ``{'y': '1'}`` from the same input. The LITERAL
        comparison is what catches it, which is why canonicalising cannot
        replace it.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        with pytest.raises(PluginConfigError, match="overlapping rename"):
            FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": mapping})

    def test_an_accepted_canonical_identity_is_safe_at_runtime(self, ctx: PluginContext) -> None:
        """The relief this rule grants must be exercised, not merely constructed.

        ``{'A': 'a'}`` is accepted because ``process`` deletes the normalized key
        and writes the same value straight back. Asserting only that the config
        constructs would pin the EXISTENCE of the relief and say nothing about
        its safety, so run the row through and check arity and values.
        """
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": DYNAMIC_SCHEMA, "mapping": {"A": "a"}})
        row = PipelineRow(
            {"a": "charlie", "z": "delta"},
            SchemaContract(
                mode="OBSERVED",
                fields=(
                    make_field("a", str, original_name="A", required=False, source="inferred"),
                    make_field("z", str, original_name="z", required=False, source="inferred"),
                ),
                locked=True,
            ),
        )

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert isinstance(result.row, PipelineRow)
        assert result.row.to_dict() == {"a": "charlie", "z": "delta"}


class TestFieldMapperInputGuaranteesUseTheDocumentedPredicate:
    """The two halves of ONE output config must not contradict each other.

    ``SchemaConfig`` documents the type system as a second, implicit source of
    guarantees: "A schema with mode=fixed, fields=(id, x) where both are
    required, but guaranteed_fields=None, still guarantees {id, x}".
    ``get_effective_guaranteed_fields`` is the predicate that says so.

    field_mapper read the RAW ``guaranteed_fields`` tuple instead, at two sites,
    collapsing ABSTAIN (``None``) into explicit-zero (``()``). A ``mode: fixed``
    field_mapper then computed no guaranteed targets and abstained in its own
    output ``guaranteed_fields``, while ``_project_field_declarations_onto_output``
    still declared the target REQUIRED — so the effective half of the very same
    config claimed the target the raw half disowned.

    The composer's Rule C (``transform_declared_output_not_guaranteed``) compares
    exactly those two halves, so it rejected a pipeline the engine builds and
    RUNS: composer stricter than engine, a FALSE REJECT on a correct pipeline.

    The pin is the AGREEMENT, not the specific field set — that is the invariant
    a future edit to either half must not break. Rule C is deliberately left
    alone: making it abstain on ``None`` would silence the true positives below,
    so the honest emit prediction has to come from the plugin.
    """

    @staticmethod
    def _built(mapping: dict[str, str], fields: list[str], *, select_only: bool = True) -> "object":
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        return FieldMapper(
            {
                "select_only": select_only,
                "mapping": mapping,
                "schema": {"mode": "fixed", "fields": fields},
            }
        )

    @staticmethod
    def _halves(transform: "object") -> tuple[frozenset[str], frozenset[str]]:
        config = transform._output_schema_config
        raw = frozenset(config.guaranteed_fields or ())
        return raw, frozenset(config.get_effective_guaranteed_fields())

    @pytest.mark.parametrize(
        ("mapping", "fields"),
        [
            ({"a": "z"}, ["a: str"]),
            ({"a": "a"}, ["a: str"]),
            ({"a": "z", "b": "y"}, ["a: str", "b: str"]),
        ],
    )
    def test_a_closed_emit_set_does_not_disown_what_the_input_guarantees(
        self,
        mapping: dict[str, str],
        fields: list[str],
    ) -> None:
        """Each of these had the raw half abstaining while the effective half claimed."""
        transform = self._built(mapping, fields)

        raw, effective = self._halves(transform)

        assert raw == effective, f"raw={sorted(raw)} contradicts effective={sorted(effective)}"

    @pytest.mark.parametrize(
        ("mapping", "fields"),
        [
            ({"a": "z"}, ["a: str", "b: str"]),
            ({}, ["a: str"]),
        ],
    )
    def test_an_open_emit_set_is_a_strict_no_op(
        self,
        mapping: dict[str, str],
        fields: list[str],
    ) -> None:
        """``select_only: false`` keeps HEAD's answer, bit for bit.

        The wider predicate is gated on ``_emit_set_is_closed`` because applying
        it here is a CATEGORY ERROR (elspeth-c84fa33f75): this branch's emitted
        set is ``(upstream - removed) | targets``, knowable only by the ADR-007
        walk, and ``process`` resolves removal names at RUNTIME. A review panel
        reproduced four defects in the ungated form — a FALSE build-time
        rejection of a working pipeline and a runtime
        ``DeclaredOutputFieldsViolation`` among them — so this asymmetry is the
        fix, not an omission from it.
        """
        transform = self._built(mapping, fields, select_only=False)

        assert transform._output_schema_config.guaranteed_fields is None
        assert transform.declared_output_fields == frozenset()

    def test_a_guaranteed_target_is_now_derived_from_a_fixed_input_schema(self) -> None:
        """The concrete consequence: a declared, required source guarantees its target."""
        transform = self._built({"a": "z"}, ["a: str"])

        assert transform._output_schema_config.guaranteed_fields == ("z",)
        assert transform.declared_output_fields == frozenset({"z"})

    def test_an_unresolved_original_source_still_abstains(self) -> None:
        """TRUE POSITIVE, preserved. 'Name' is not a normalization fixed point.

        Which row key it names is unknowable until a row arrives, so the target
        is not guaranteed however the INPUT contract is declared. Widening the
        base guarantee set must not reach this case (elspeth-f262a8c678).
        """
        transform = self._built({"Name": "nm"}, ["Name: str"])

        assert transform._output_schema_config.guaranteed_fields is None
        assert transform.declared_output_fields == frozenset()

    def test_an_observed_input_schema_is_unaffected(self) -> None:
        """``fields is None`` makes effective == explicit, so observed abstains as before."""
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"select_only": True, "mapping": {"a": "z"}, "schema": DYNAMIC_SCHEMA})

        raw, effective = self._halves(transform)
        assert raw == effective == frozenset()
