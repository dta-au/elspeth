"""Tests for BatchReplicate aggregation transform.

BatchReplicate replicates rows based on a copies field. It is batch-aware,
meaning it receives lists of rows when aggregation triggers fire.

Contract enforcement tests verify that a wrongly-typed copies value is never
coerced: it fails the whole batch with a recorded, value-free reason
(elspeth-d5034647f0), which the aggregation's on_error then routes.
"""

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context

# Common schema config for dynamic field handling (accepts any fields)
DYNAMIC_SCHEMA = {"mode": "observed"}


def _assert_wrong_type_batch_failure(result: TransformResult, *, found: str, row_index: int, value_text: str | None) -> None:
    """The returned-error shape of a wrongly-typed copies value (template: 890a6aca7).

    ``value_text`` is the offending value's rendering, which must be absent
    from the reason; None where the value has no rendering beyond its type
    name (a null is fully described by ``NoneType``).
    """
    assert result.status == "error"
    assert result.retryable is False
    assert result.rows is None and result.row is None
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "wrong_type"
    assert result.reason["field"] == "copies"
    assert result.reason["expected"] == "int"
    assert result.reason["actual_type"] == found
    assert result.reason["error"] == f"must be int, got {found} in row {row_index}"
    # The offending VALUE is row content and must not reach the audit trail.
    if value_text is not None:
        assert value_text not in repr(sorted(result.reason.items()))


class TestBatchReplicateHappyPath:
    """Happy path tests for BatchReplicate transform."""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    def test_has_required_attributes(self) -> None:
        """BatchReplicate has name and is_batch_aware."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        assert BatchReplicate.name == "batch_replicate"
        assert BatchReplicate.is_batch_aware is True

    def test_declares_emitted_rows_as_pass_through(self) -> None:
        """Replicas preserve their source fields even when peers quarantine."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        assert BatchReplicate.passes_through_input is True
        assert BatchReplicate.can_drop_rows is False

    def test_replicates_rows_by_copies_field(self, ctx: PluginContext) -> None:
        """BatchReplicate creates N copies of each row based on copies field."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [
            make_pipeline_row({"id": 1, "copies": 2}),
            make_pipeline_row({"id": 2, "copies": 3}),
            make_pipeline_row({"id": 3, "copies": 1}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.rows is not None
        assert len(result.rows) == 6  # 2 + 3 + 1
        # Check copy indices
        assert result.rows[0]["copy_index"] == 0
        assert result.rows[1]["copy_index"] == 1
        assert result.rows[2]["copy_index"] == 0
        assert result.rows[3]["copy_index"] == 1
        assert result.rows[4]["copy_index"] == 2

    def test_uses_default_copies_when_field_missing(self, ctx: PluginContext) -> None:
        """Missing copies field uses default_copies value."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "default_copies": 2,
            }
        )

        rows = [
            make_pipeline_row({"id": 1}),  # No copies field - use default
            make_pipeline_row({"id": 2, "copies": 3}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.rows is not None
        assert len(result.rows) == 5  # 2 (default) + 3

    def test_empty_batch_returns_error(self, ctx: PluginContext) -> None:
        """Empty batch returns error — not fabricated data."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        result = transform.process([], ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "empty_batch"
        assert not result.retryable


class TestBatchReplicateTypeEnforcement:
    """Contract enforcement tests - transforms must not coerce types.

    A present copies value of the wrong type is row data (an observed upstream
    does not type it), so it is never coerced and never aborts the run: the
    whole batch fails with a reason naming the batch row index, the field, the
    expected and the found type, and never the value (elspeth-d5034647f0).
    """

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create minimal plugin context."""
        return make_context()

    def test_string_copies_fails_the_whole_batch_with_a_recorded_reason(self, ctx: PluginContext) -> None:
        """A str copies value fails the batch (no coercion: "3" is not 3)."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [
            make_pipeline_row({"id": 1, "copies": 2}),
            make_pipeline_row({"id": 2, "copies": "SENTINEL-copies-3"}),  # str instead of int
            make_pipeline_row({"id": 3, "copies": 1}),
        ]

        _assert_wrong_type_batch_failure(
            transform.process(rows, ctx),
            found="str",
            row_index=1,
            value_text="SENTINEL-copies-3",
        )

    def test_float_copies_fails_the_whole_batch_with_a_recorded_reason(self, ctx: PluginContext) -> None:
        """A float copies value fails the batch (no coercion)."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1, "copies": 2}), make_pipeline_row({"id": 2, "copies": 3.25})]

        _assert_wrong_type_batch_failure(transform.process(rows, ctx), found="float", row_index=1, value_text="3.25")

    def test_none_copies_fails_the_whole_batch(self, ctx: PluginContext) -> None:
        """A present None copies value fails the batch.

        batch_replicate has no missing-value branch: an ABSENT copies field is
        the missing case (default_copies). A present null is a wrong type, and
        no skip branch is invented for it (ruling default B6).
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1}), make_pipeline_row({"id": 2, "copies": None})]

        _assert_wrong_type_batch_failure(transform.process(rows, ctx), found="NoneType", row_index=1, value_text=None)

    def test_bool_true_copies_fails_the_whole_batch(self, ctx: PluginContext) -> None:
        """Bool True in copies field fails the batch (not silently treated as 1).

        Python's isinstance(True, int) returns True because bool is a subclass
        of int. The guard uses `type(x) is int` for strict type checking, so
        True/False are rejected as distinct logical types.
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1, "copies": True})]

        _assert_wrong_type_batch_failure(transform.process(rows, ctx), found="bool", row_index=0, value_text="True")

    def test_bool_false_copies_fails_the_whole_batch(self, ctx: PluginContext) -> None:
        """Bool False in copies field fails the batch (not silently treated as 0).

        Without strict type checking, False would be treated as 0 copies,
        which would then be quarantined as invalid (< 1). The bug is that
        the type check passes at all - bool is not int for contract purposes.
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1, "copies": False})]

        _assert_wrong_type_batch_failure(transform.process(rows, ctx), found="bool", row_index=0, value_text="False")

    def test_zero_copies_returns_error_when_all_invalid(self, ctx: PluginContext) -> None:
        """All rows with zero copies returns error result (no valid output to expand)."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1, "copies": 0})]

        result = transform.process(rows, ctx)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "all_rows_failed"
        assert "1 rows quarantined" in result.reason["error"]
        assert result.reason["row_errors"][0]["reason"] == "invalid_copies"

    def test_negative_copies_returns_error_when_all_invalid(self, ctx: PluginContext) -> None:
        """All rows with negative copies returns error result."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1, "copies": -1})]

        result = transform.process(rows, ctx)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "all_rows_failed"
        assert result.reason["row_errors"][0]["reason"] == "invalid_copies"

    def test_invalid_copies_quarantined_alongside_valid_rows(self, ctx: PluginContext) -> None:
        """Rows with invalid copies are quarantined; valid rows still replicated."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [
            make_pipeline_row({"id": 1, "copies": -1}),
            make_pipeline_row({"id": 2, "copies": 2}),
        ]

        result = transform.process(rows, ctx)
        assert result.status == "success"
        assert result.rows is not None
        assert len(result.rows) == 2  # Only valid copies of row 2
        assert result.rows[0]["id"] == 2
        assert result.rows[0]["copy_index"] == 0
        assert result.rows[1]["id"] == 2
        assert result.rows[1]["copy_index"] == 1
        # Quarantine info in success_reason.metadata records the row INDEX, not the
        # row body — Tier-2/3 row content must not be embedded in audit metadata
        # (plugins review Batch 4 item 6, prior row-content-leak family).
        assert result.success_reason is not None
        assert result.success_reason["metadata"]["quarantined_count"] == 1
        entry = result.success_reason["metadata"]["quarantined"][0]
        assert entry["reason"] == "invalid_copies"
        assert entry["row_index"] == 0
        assert entry["field"] == "copies"
        # Neither the row body nor the offending copies VALUE is recorded: the
        # count is row content like any other field (elspeth-d5034647f0).
        assert set(entry) == {"reason", "field", "row_index"}

    def test_wrong_type_reason_states_the_disposition_not_an_upstream_bug(self, ctx: PluginContext) -> None:
        """The reason records the batch failure; it no longer blames an "upstream bug".

        A wrongly-typed copies value is row data under an observed upstream,
        so the reason carries no "upstream validation bug" diagnosis and no
        value — only which row, which field, expected and found.
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
            }
        )

        rows = [make_pipeline_row({"id": 1, "copies": "invalid"})]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert "upstream" not in repr(result.reason)
        assert result.reason["error"] == "must be int, got str in row 0"


class TestBatchReplicateConfigValidation:
    """Config validation tests."""

    def test_default_copies_zero_rejected(self) -> None:
        """Config with default_copies=0 is rejected at validation time."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        with pytest.raises(PluginConfigError, match="default_copies"):
            BatchReplicate(
                {
                    "schema": {"mode": "observed"},
                    "default_copies": 0,
                }
            )

    def test_default_copies_negative_rejected(self) -> None:
        """Config with negative default_copies is rejected at validation time."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        with pytest.raises(PluginConfigError, match="default_copies"):
            BatchReplicate(
                {
                    "schema": {"mode": "observed"},
                    "default_copies": -1,
                }
            )

    def test_default_copies_above_max_rejected(self) -> None:
        """Config with default_copies > max_copies is rejected at validation time."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        with pytest.raises(PluginConfigError, match="exceeds max_copies"):
            BatchReplicate(
                {
                    "schema": {"mode": "observed"},
                    "default_copies": 11,
                    "max_copies": 10,
                }
            )

    def test_explicit_schema_declaring_copy_index_is_rejected(self) -> None:
        """Fixed/flexible schemas must fail closed when copy_index would collide."""
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        with pytest.raises(PluginConfigError, match="copy_index"):
            BatchReplicate(
                {
                    "schema": {
                        "mode": "fixed",
                        "fields": [
                            "id: int",
                            "copies: int",
                            "copy_index: int",
                        ],
                    },
                    "include_copy_index": True,
                }
            )

    def test_copies_field_naming_copy_index_is_rejected_on_both_validation_paths(self) -> None:
        """copies_field may not name the column batch_replicate creates.

        ``process`` reads ``copies_field`` on every buffered row and writes
        ``copy_index`` onto each emitted copy of that same row, so aiming one
        at the other reads the column it is about to overwrite. Worse, the read
        puts ``copy_index`` into ``consumed_input_fields``, so ``input_schema``
        stops demoting it and every row lacking it is rejected for missing the
        field the transform exists to create (elspeth-d6eeb3a71d).

        Asserted on BOTH paths: the engine constructs the plugin, while
        pre-validation runs the config model alone and RETURNS errors rather
        than raising. A guard that lived only in ``__init__`` would let
        ``validate_transform_config`` report the config clean and then reject
        it at run time.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.infrastructure.validation import validate_transform_config
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        options = {
            "schema": {"mode": "observed"},
            "copies_field": "copy_index",
            "include_copy_index": True,
        }

        with pytest.raises(PluginConfigError, match="copies_field"):
            BatchReplicate(options)

        errors = validate_transform_config("batch_replicate", options)
        assert errors, "pre-validation accepted a config the engine rejects"
        assert any("copies_field" in error.message for error in errors), (
            f"the pre-validation error does not name the option to repoint: {[e.message for e in errors]}"
        )

    def test_copies_field_may_name_copy_index_when_the_index_is_disabled(self) -> None:
        """The guard is scoped to what is actually created, not to the name.

        With ``include_copy_index: false`` nothing is created, ``copy_index``
        is an ordinary arriving column, and reading the copy count from it is
        legal. A guard that rejected the name unconditionally would refuse a
        valid config — the failure mode that is worse than the defect it closes.
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": {"mode": "observed"},
                "copies_field": "copy_index",
                "include_copy_index": False,
            }
        )

        assert transform.declared_output_fields == frozenset()
        assert "copy_index" in transform.consumed_input_fields


class TestBatchReplicateSchemaContract:
    """Schema contract tests."""

    def test_output_schema_is_observed_when_copy_index_enabled(self) -> None:
        """Output schema is dynamic to accommodate copy_index field."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": {"fields": [{"id": "int"}], "mode": "fixed"},
                "include_copy_index": True,
            }
        )

        # Output schema should accept the copy_index field (dynamic schema)
        output_schema = transform.output_schema
        # Dynamic schemas accept any fields
        validated = output_schema.model_validate({"id": 1, "copy_index": 0})
        assert validated.copy_index == 0  # type: ignore[attr-defined]

    def test_output_schema_accepts_copy_index_field(self) -> None:
        """Output schema validation passes for rows with copy_index."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": {"mode": "observed"},
                "include_copy_index": True,
            }
        )

        # Simulate what process() outputs
        output_row = {"original_field": "value", "copy_index": 2}
        # This should not raise
        transform.output_schema.model_validate(output_row)


class TestBatchReplicateDeepCopy:
    """Verify replicated rows don't share mutable nested references."""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        return make_context()

    def test_nested_list_mutation_does_not_cross_contaminate(self, ctx: PluginContext) -> None:
        """Mutating a nested list in one copy must not affect other copies."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate({"schema": DYNAMIC_SCHEMA, "copies_field": "copies", "include_copy_index": True})
        row = make_pipeline_row({"id": 1, "tags": ["a", "b"], "copies": 3})

        result = transform.process([row], ctx)

        assert result.status == "success"
        assert result.rows is not None
        assert len(result.rows) == 3

        # Mutate nested list in first copy
        first = result.rows[0].to_dict()
        first["tags"].append("MUTATED")

        # Other copies must be unaffected
        for copy_row in result.rows[1:]:
            assert "MUTATED" not in copy_row["tags"]

    def test_nested_dict_mutation_does_not_cross_contaminate(self, ctx: PluginContext) -> None:
        """Mutating a nested dict in one copy must not affect other copies."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate({"schema": DYNAMIC_SCHEMA, "copies_field": "copies", "include_copy_index": True})
        row = make_pipeline_row({"id": 1, "meta": {"source": "csv"}, "copies": 2})

        result = transform.process([row], ctx)

        assert result.rows is not None
        first = result.rows[0].to_dict()
        first["meta"]["injected"] = True

        second = result.rows[1].to_dict()
        assert "injected" not in second["meta"]

    def test_runtime_collision_on_copy_index_fails_the_whole_batch(self, ctx: PluginContext) -> None:
        """An incoming copy_index fails the batch instead of being overwritten.

        Under an observed upstream only the row data decides whether
        copy_index arrives, so the collision is a row fault routed like any
        failed batch (ruling on elspeth-d90495084c), not a run abort. The
        reason names the batch row and the colliding field, never its value.
        """
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate({"schema": DYNAMIC_SCHEMA, "copies_field": "copies", "include_copy_index": True})
        rows = [
            make_pipeline_row({"id": 1, "copies": 2}),
            make_pipeline_row({"id": 2, "copies": 2, "copy_index": "SENTINEL-copy-index-9"}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.retryable is False
        assert result.rows is None and result.row is None
        assert result.reason is not None
        assert result.reason["reason"] == "field_collision"
        assert result.reason["collisions"] == ["copy_index"]
        assert result.reason["error"] == "would overwrite existing input fields ['copy_index'] in row 1"
        assert "SENTINEL-copy-index-9" not in repr(sorted(result.reason.items()))


class TestBatchReplicateDeclaredOutputFields:
    """Tests for declared_output_fields — centralized collision detection support.

    Field collision detection is enforced centrally by TransformExecutor
    (see TestTransformExecutor in test_executors.py). These tests verify
    that BatchReplicate correctly declares its output fields so the executor
    can perform pre-execution collision checks.
    """

    def test_declared_output_fields_contains_copy_index_when_enabled(self) -> None:
        """declared_output_fields includes copy_index when include_copy_index is True."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "include_copy_index": True,
            }
        )

        assert "copy_index" in transform.declared_output_fields

    def test_declared_output_fields_empty_when_copy_index_disabled(self) -> None:
        """declared_output_fields is empty when include_copy_index is False."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "include_copy_index": False,
            }
        )

        assert len(transform.declared_output_fields) == 0

    def test_declared_output_fields_drives_schema_evolution(self) -> None:
        """declared_output_fields is non-empty when include_copy_index=True, enabling schema evolution."""
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "include_copy_index": True,
            }
        )

        assert transform.declared_output_fields


class TestOutputSchemaConfig:
    def test_guaranteed_fields_with_copy_index(self):
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "include_copy_index": True,
                "schema": {"mode": "observed"},
            }
        )
        assert transform._output_schema_config is not None
        assert frozenset(transform._output_schema_config.guaranteed_fields) == frozenset({"copy_index"})

    def test_guaranteed_fields_without_copy_index(self):
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "include_copy_index": False,
                "schema": {"mode": "observed"},
            }
        )
        assert transform._output_schema_config is not None
        # No upstream guaranteed_fields + no declared fields → abstain (None)
        assert transform._output_schema_config.guaranteed_fields is None


class TestBatchReplicateContractPreservation:
    """Tests that output rows preserve input contract metadata.

    Bug fix: BatchReplicate was building SchemaContract from scratch with
    mode=OBSERVED, python_type=object, source=inferred for ALL keys. This
    discards the input contracts' mode, original_name, python_type, and field types.
    The fix merges input row contracts to preserve metadata.
    """

    @pytest.fixture
    def ctx(self) -> PluginContext:
        return make_context()

    def test_output_contract_mode_aligns_to_declared_output_schema(self, ctx: PluginContext) -> None:
        """Emitted contract mode follows the transform's declared output schema."""
        from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "include_copy_index": True,
            }
        )

        contract = SchemaContract(
            mode="FIXED",
            fields=(
                FieldContract(normalized_name="id", original_name="id", python_type=int, required=True, source="declared"),
                FieldContract(normalized_name="copies", original_name="copies", python_type=int, required=True, source="declared"),
            ),
            locked=True,
        )
        rows = [PipelineRow({"id": 1, "copies": 2}, contract)]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.rows is not None
        output_contract = result.rows[0].contract
        assert output_contract.mode == "OBSERVED"
        assert output_contract.locked is True

    def test_output_preserves_field_python_type(self, ctx: PluginContext) -> None:
        """Output contract fields preserve python_type from input contract."""
        from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "include_copy_index": True,
            }
        )

        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(normalized_name="id", original_name="ID", python_type=int, required=True, source="declared"),
                FieldContract(normalized_name="copies", original_name="copies", python_type=int, required=False, source="inferred"),
            ),
            locked=True,
        )
        rows = [PipelineRow({"id": 1, "copies": 1}, contract)]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.rows is not None
        output_contract = result.rows[0].contract

        id_field = output_contract.get_field("id")
        assert id_field is not None
        assert id_field.python_type is int
        assert id_field.original_name == "ID"
        assert id_field.source == "declared"

    def test_output_copy_index_has_int_type(self, ctx: PluginContext) -> None:
        """copy_index field in output contract has python_type=int, not object."""
        from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
        from elspeth.plugins.transforms.batch_replicate import BatchReplicate

        transform = BatchReplicate(
            {
                "schema": DYNAMIC_SCHEMA,
                "copies_field": "copies",
                "include_copy_index": True,
            }
        )

        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(normalized_name="id", original_name="id", python_type=int, required=False, source="inferred"),
                FieldContract(normalized_name="copies", original_name="copies", python_type=int, required=False, source="inferred"),
            ),
            locked=True,
        )
        rows = [PipelineRow({"id": 1, "copies": 2}, contract)]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.rows is not None
        copy_idx_field = result.rows[0].contract.get_field("copy_index")
        assert copy_idx_field is not None
        assert copy_idx_field.python_type is int
