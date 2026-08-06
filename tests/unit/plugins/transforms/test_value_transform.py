"""Tests for ValueTransform transform — behavioral unit tests."""

from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.testing import make_contract, make_pipeline_row, make_row
from tests.fixtures.factories import make_source_context
from tests.fixtures.pipeline import build_linear_pipeline

if TYPE_CHECKING:
    from elspeth.contracts.plugin_context import PluginContext

OBSERVED_SCHEMA_CONFIG = SchemaConfig.from_dict({"mode": "observed"})

DYNAMIC_SCHEMA = {"mode": "observed"}


class TestValueTransformBehavior:
    """Core value transformation mechanics."""

    @pytest.fixture
    def ctx(self) -> "PluginContext":
        return make_source_context()

    def test_arithmetic_expression(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "total", "expression": "row['price'] * row['quantity']"}],
            }
        )
        row = make_pipeline_row({"price": 10, "quantity": 2})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["total"] == 20

    def test_string_concatenation(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "line", "expression": "row['line'] + ' World'"}],
            }
        )
        row = make_pipeline_row({"line": "Hello"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["line"] == "Hello World"

    def test_multiple_operations_sequential(self, ctx: "PluginContext") -> None:
        """Operations see results of prior operations."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [
                    {"target": "subtotal", "expression": "row['price'] * row['quantity']"},
                    {"target": "tax", "expression": "row['subtotal'] * 0.2"},
                    {"target": "total", "expression": "row['subtotal'] + row['tax']"},
                ],
            }
        )
        row = make_pipeline_row({"price": 100, "quantity": 2})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["subtotal"] == 200
        assert result.row["tax"] == 40.0
        assert result.row["total"] == 240.0

    def test_self_reference_overwrite(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "price", "expression": "row['price'] * 1.1"}],
            }
        )
        row = make_pipeline_row({"price": 100})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["price"] == pytest.approx(110.0)

    def test_overwrite_with_different_type_retypes_output_contract(self, ctx: "PluginContext") -> None:
        """Overwriting a typed field with a different-typed result retypes the contract.

        ``price`` is declared int; the expression makes it float. Before this fix
        ``with_field`` was only called for NEW targets, so the output contract kept
        python_type=int and the emitted row FAILED its own contract.validate() — a
        self-contradictory audit record (plugins review Batch 4 item 2).
        """
        from elspeth.contracts.schema_contract import SchemaContract
        from elspeth.plugins.transforms.value_transform import ValueTransform
        from elspeth.testing import make_field, make_row

        fields = (make_field("price", int, original_name="price", required=False, source="inferred"),)
        contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
        row = make_row({"price": 100}, contract=contract)

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "price", "expression": "row['price'] * 1.1"}],
            }
        )
        result = transform.process(row, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert isinstance(result.row["price"], float)
        out_field = result.row.contract.find_field("price")
        assert out_field is not None
        assert out_field.python_type is float
        violations = result.row.contract.validate(result.row.to_dict())
        assert violations == [], f"emitted row must satisfy its own contract, got: {violations}"

    def test_duplicate_targets_sequential_rewrite(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [
                    {"target": "x", "expression": "row['x'] + 1"},
                    {"target": "x", "expression": "row['x'] * 2"},
                ],
            }
        )
        row = make_pipeline_row({"x": 5})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        # (5 + 1) * 2 = 12
        assert result.row["x"] == 12

    def test_creates_new_field(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "new_field", "expression": "row['x'] + 100"}],
            }
        )
        row = make_pipeline_row({"x": 5})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["new_field"] == 105
        assert result.row["x"] == 5  # Original preserved

    def test_ternary_expression(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [
                    {"target": "discount", "expression": "row['price'] * 0.1 if row['price'] > 50 else 0"},
                ],
            }
        )
        row = make_pipeline_row({"price": 100})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["discount"] == 10.0

    def test_missing_field_errors(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "total", "expression": "row['missing'] * 2"}],
            }
        )
        row = make_pipeline_row({"other": 42})
        result = transform.process(row, ctx)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason.get("reason") == "invalid_input"
        assert "missing" in result.reason.get("message", "").lower()

    def test_type_error_in_expression(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "result", "expression": "row['text'] * row['num']"}],
            }
        )
        # Can't multiply string by string (would need int)
        row = make_pipeline_row({"text": "hello", "num": "world"})
        result = transform.process(row, ctx)
        assert result.status == "error"

    def test_atomic_failure_no_partial_mutation(self, ctx: "PluginContext") -> None:
        """If second operation fails, first operation should not be applied."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [
                    {"target": "first", "expression": "row['x'] + 1"},  # Would succeed
                    {"target": "second", "expression": "row['missing'] * 2"},  # Will fail
                ],
            }
        )
        row = make_pipeline_row({"x": 5})
        result = transform.process(row, ctx)
        assert result.status == "error"
        # Original row should be unchanged (error path gets original row)

    def test_audit_trail(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [
                    {"target": "total", "expression": "row['price'] * row['quantity']"},
                    {"target": "line", "expression": "row['line'] + ' modified'"},
                ],
            }
        )
        row = make_pipeline_row({"price": 10, "quantity": 2, "line": "Hello"})
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.success_reason is not None
        assert result.success_reason["action"] == "transformed"
        assert "total" in result.success_reason["fields_added"]
        assert "line" in result.success_reason["fields_modified"]
        # Plugin-specific audit details in metadata
        assert result.success_reason["metadata"]["operations_applied"] == 2

    def test_row_get_with_none_handling(self, ctx: "PluginContext") -> None:
        """row.get() returning None in expression that handles it."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [
                    {"target": "result", "expression": "row.get('optional') if row.get('optional') is not None else 0"},
                ],
            }
        )
        row = make_pipeline_row({"other": 42})  # 'optional' is missing
        result = transform.process(row, ctx)
        assert result.status == "success"
        assert result.row is not None
        assert result.row["result"] == 0

    def test_fixed_schema_sequential_operations_can_read_added_fields(self, ctx: "PluginContext") -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "fixed", "fields": ["price: int", "quantity: int"]},
                "operations": [
                    {"target": "subtotal", "expression": "row['price'] * row['quantity']"},
                    {"target": "tax", "expression": "row['subtotal'] * 0.2"},
                ],
            }
        )
        row = make_row(
            {"price": 100, "quantity": 2},
            contract=make_contract(fields={"price": int, "quantity": int}, mode="FIXED"),
        )

        result = transform.process(row, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["subtotal"] == 200
        assert result.row["tax"] == 40.0
        assert "subtotal" in result.row
        assert "tax" in result.row
        assert result.row.contract.find_field("subtotal") is not None
        assert result.row.contract.find_field("tax") is not None

    def test_output_schema_config_guarantees_configured_targets_for_dag_validation(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        producer = ValueTransform(
            {
                "schema": {"mode": "fixed", "fields": ["price: int", "quantity: int"]},
                "operations": [{"target": "subtotal", "expression": "row['price'] * row['quantity']"}],
            }
        )
        # A fixed-mode producer guarantees its declared inputs as well as its
        # operation targets, so the downstream locked consumer must admit
        # price/quantity too — the negative case below pins why.
        consumer = ValueTransform(
            {
                "schema": {"mode": "fixed", "fields": ["price: int", "quantity: int", "subtotal: float"]},
                "required_input_fields": ["subtotal"],
                "operations": [{"target": "with_tax", "expression": "row['subtotal'] * 1.2"}],
            }
        )

        assert producer._output_schema_config is not None
        assert "subtotal" in producer._output_schema_config.get_effective_guaranteed_fields()
        build_linear_pipeline([{"price": 100, "quantity": 2}], transforms=[producer, consumer])

    def test_locked_consumer_omitting_guaranteed_passthrough_fields_is_rejected_at_build(self) -> None:
        """A narrower fixed consumer is rejected at build, not at row 1.

        ``ValueTransformInput`` is ``extra='forbid'``, so a consumer declaring
        only ``subtotal`` rejects every row the producer emits — it carries
        ``price`` and ``quantity`` as well. Build-time rejection is sound
        because the producer GUARANTEES those fields (elspeth-9615d6c75a).
        """
        from elspeth.core.dag.models import EdgeContractError
        from elspeth.plugins.transforms.value_transform import ValueTransform

        producer = ValueTransform(
            {
                "schema": {"mode": "fixed", "fields": ["price: int", "quantity: int"]},
                "operations": [{"target": "subtotal", "expression": "row['price'] * row['quantity']"}],
            }
        )
        consumer = ValueTransform(
            {
                "schema": {"mode": "fixed", "fields": ["subtotal: float"]},
                "required_input_fields": ["subtotal"],
                "operations": [{"target": "with_tax", "expression": "row['subtotal'] * 1.2"}],
            }
        )

        with pytest.raises(EdgeContractError) as exc_info:
            build_linear_pipeline([{"price": 100, "quantity": 2}], transforms=[producer, consumer])
        message = str(exc_info.value)
        assert "Extra fields rejected by consumer input contract: ['price', 'quantity']" in message

    def test_unexpected_evaluator_exceptions_propagate(self, ctx: "PluginContext", monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": DYNAMIC_SCHEMA,
                "operations": [{"target": "out", "expression": "row['x'] + 1"}],
            }
        )
        parser = transform._operations[0].get_parser()

        def boom(row: object) -> object:
            raise RuntimeError("sentinel plugin bug")

        monkeypatch.setattr(parser, "evaluate", boom)

        with pytest.raises(RuntimeError, match="sentinel plugin bug"):
            transform.process(make_pipeline_row({"x": 1}), ctx)


class TestValueTransformConfig:
    """Pydantic config validation for ValueTransformConfig."""

    def test_valid_config(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        cfg = ValueTransformConfig(
            operations=[
                {"target": "total", "expression": "row['price'] * row['quantity']"},
            ],
            schema_config=OBSERVED_SCHEMA_CONFIG,
        )
        assert len(cfg.operations) == 1
        assert cfg.operations[0].target == "total"

    def test_rejects_empty_operations(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        with pytest.raises(ValidationError, match="at least one"):
            ValueTransformConfig(operations=[], schema_config=OBSERVED_SCHEMA_CONFIG)

    def test_rejects_empty_target(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        with pytest.raises(ValidationError, match="target"):
            ValueTransformConfig(
                operations=[{"target": "", "expression": "row['x']"}],
                schema_config=OBSERVED_SCHEMA_CONFIG,
            )

    def test_rejects_empty_expression(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        with pytest.raises(ValidationError, match="expression"):
            ValueTransformConfig(
                operations=[{"target": "x", "expression": ""}],
                schema_config=OBSERVED_SCHEMA_CONFIG,
            )

    def test_rejects_invalid_expression_syntax(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        with pytest.raises(ValidationError, match=r"syntax|parse"):
            ValueTransformConfig(
                operations=[{"target": "x", "expression": "row['x'"}],  # Missing ]
                schema_config=OBSERVED_SCHEMA_CONFIG,
            )

    def test_rejects_forbidden_expression_constructs(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        # Lambda is forbidden by ExpressionParser
        with pytest.raises(ValidationError, match=r"Lambda|forbidden"):
            ValueTransformConfig(
                operations=[{"target": "x", "expression": "lambda: 1"}],
                schema_config=OBSERVED_SCHEMA_CONFIG,
            )

    def test_rejects_non_finite_expression_literal(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        with pytest.raises(ValidationError, match="Non-finite float literal"):
            ValueTransformConfig(
                operations=[{"target": "huge", "expression": "1e309"}],
                schema_config=OBSERVED_SCHEMA_CONFIG,
            )

    def test_allows_duplicate_targets(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        cfg = ValueTransformConfig(
            operations=[
                {"target": "x", "expression": "row['x'] + 1"},
                {"target": "x", "expression": "row['x'] * 2"},  # Same target
            ],
            schema_config=OBSERVED_SCHEMA_CONFIG,
        )
        assert len(cfg.operations) == 2

    def test_from_dict_factory(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransformConfig

        cfg = ValueTransformConfig.from_dict(
            {
                "schema": {"mode": "observed"},
                "operations": [{"target": "x", "expression": "row['a'] + row['b']"}],
            }
        )
        assert len(cfg.operations) == 1
        assert cfg.operations[0].target == "x"


class TestValueTransformDemotesSelfCreatedInputs:
    """An operation target created before any read is optional on INPUT (elspeth-d6eeb3a71d).

    The ``schema`` block names both what value_transform consumes and what it
    emits. A target assigned before any operation reads it is CREATED here, so
    it cannot be required on input — but it IS guaranteed on output. The
    derived input pydantic model demotes it to optional while the output
    ``SchemaConfig`` keeps it required, which is what makes
    ``guaranteed_fields`` legal at contracts/schema.py.

    Supersedes the construction-time rejection landed as 7a5d72d34: that guard
    used this same predicate to REJECT, and its error message ("no upstream row
    can ever satisfy the input contract") was false for the overwrite case.
    """

    def test_target_assigned_before_read_is_optional_on_input(self) -> None:
        """The live g04 shape: targets hoisted from a nested field, declared required."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": [
                        {"name": "product", "field_type": "str"},
                        {"name": "quantity", "field_type": "int"},
                    ],
                },
                "required_input_fields": ["item"],
                "operations": [
                    {"target": "product", "expression": "row['item']['product']"},
                    {"target": "quantity", "expression": "row['item']['quantity']"},
                ],
            }
        )

        required = {n for n, f in transform.input_schema.model_fields.items() if f.is_required()}
        assert required.isdisjoint({"product", "quantity"})
        transform.input_schema.model_validate(
            {"order_id": "ORD-001", "item": {"product": "Wireless Mouse", "quantity": 2}},
            strict=True,
        )

    def test_recompute_of_an_existing_column_builds_and_accepts_the_upstream_row(self) -> None:
        """Recomputing a column the source already carries is canonical data cleaning."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": ["price: float", "qty: int", "total: float"],
                },
                "operations": [{"target": "total", "expression": "row['price'] * row['qty']"}],
            }
        )

        transform.input_schema.model_validate({"price": 2.5, "qty": 4, "total": 0.0}, strict=True)
        transform.input_schema.model_validate({"price": 2.5, "qty": 4}, strict=True)

    def test_fixed_mode_overwrite_with_downstream_guarantee_now_runs(self) -> None:
        """mode:fixed + overwrite + guaranteed_fields had NO working config before this fix.

        Scope of the claim, precisely: the FRAMEWORK now derives input-optional +
        output-guaranteed, so this config runs. An AUTHOR still cannot DECLARE
        that combination — contracts/schema.py:574-582 is untouched and still
        rejects ``required: false`` alongside ``guaranteed_fields``, so the
        author must write ``required: true`` and rely on the framework demoting
        it underneath. Comment 2377's option C' is NARROWED, not closed.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {
                    "mode": "fixed",
                    "fields": ["price: float", "qty: int", "total: float"],
                    "guaranteed_fields": ["total"],
                },
                "operations": [{"target": "total", "expression": "row['price'] * row['qty']"}],
            }
        )

        # Input: satisfiable with or without the overwritten column.
        transform.input_schema.model_validate({"price": 2.5, "qty": 4, "total": 0.0}, strict=True)
        transform.input_schema.model_validate({"price": 2.5, "qty": 4}, strict=True)
        # Output: the guarantee survives.
        assert "total" in (transform._output_schema_config.guaranteed_fields or ())

    def test_target_read_before_assignment_stays_required(self) -> None:
        """An operation that reads its own target is a genuine input consumer."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "price", "field_type": "float"}]},
                "operations": [{"target": "price", "expression": "row['price'] * 1.1"}],
            }
        )

        assert transform.input_schema.model_fields["price"].is_required()

    def test_field_read_only_after_creation_is_demoted(self) -> None:
        """A later read does not make the field an input: it reads the created value."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": [
                        {"name": "price", "field_type": "int"},
                        {"name": "quantity", "field_type": "int"},
                        {"name": "subtotal", "field_type": "int"},
                    ],
                },
                "operations": [
                    {"target": "subtotal", "expression": "row['price'] * row['quantity']"},
                    {"target": "total", "expression": "row['subtotal'] + 5"},
                ],
            }
        )

        assert not transform.input_schema.model_fields["subtotal"].is_required()
        assert transform.input_schema.model_fields["price"].is_required()

    def test_allows_optional_declared_target(self) -> None:
        """required: false declares the target's type without demanding it on input."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": [{"name": "product", "field_type": "str", "required": False, "nullable": False}],
                },
                "operations": [{"target": "product", "expression": "row['item']['product']"}],
            }
        )
        assert not transform.input_schema.model_fields["product"].is_required()

    def test_dynamic_key_read_is_rejected_at_construction_not_at_every_row(self) -> None:
        """When the read analysis abstains, fail closed where the author can act.

        A dynamic subscript means we cannot prove whether the target is read, so
        we cannot safely demote it — but leaving it required rejected EVERY row
        at runtime with a generic "field required", and nothing told the author
        the analysis had abstained. Reject at construction with the dynamic
        expression named instead.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.value_transform import ValueTransform

        with pytest.raises(PluginConfigError, match="read/create status cannot be determined"):
            ValueTransform(
                {
                    "schema": {"mode": "flexible", "fields": [{"name": "product", "field_type": "str"}]},
                    "operations": [{"target": "product", "expression": "row[row['key']]"}],
                }
            )

    def test_dynamic_read_on_one_target_does_not_poison_a_provable_sibling(self) -> None:
        """Abstention is per-TARGET, never per-config.

        Operation 1's subscript is unresolvable, but operation 2 is a provable
        self-overwrite of ``price`` — the same operation that resolves fine on
        its own. Rejecting the pair would repeat the exact error the 7a5d72d34
        guard was retracted for: asserting an unsatisfiability that is not true.
        ``price`` is read before it is assigned, so an upstream row supplying it
        satisfies the contract.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "price", "field_type": "float"}]},
                "operations": [
                    {"target": "junk", "expression": "row[row['k']]"},
                    {"target": "price", "expression": "row['price'] * 1.1"},
                ],
            }
        )

        assert transform.input_schema.model_fields["price"].is_required()

    def test_dynamic_read_alongside_an_explicit_read_of_the_same_target_builds(self) -> None:
        """An explicit read settles the target's status even beside a dynamic one.

        ``row['price']`` is literally present, so ``price`` is provably read
        before assignment — a genuine input, satisfiable by any upstream row
        that supplies it. The unresolvable sibling subscript cannot make a
        proven read unproven.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "price", "field_type": "float"}]},
                "operations": [{"target": "price", "expression": "row[row['k']] + row['price']"}],
            }
        )

        assert transform.input_schema.model_fields["price"].is_required()

    def test_dynamic_read_before_an_unread_required_target_stays_required(self) -> None:
        """A sibling's dynamic read withholds the demotion — it does not reject.

        ``product``'s own expression reads nothing, so nothing about IT is
        unanalysable; only the earlier unrelated subscript is. Rejecting on that
        basis made the verdict depend on operation ORDER: the identical pair
        listed the other way round builds (see the order test below). The
        earlier dynamic read could still consume ``product`` from the input row,
        so it must not be demoted either — the config is unprovable, not broken,
        and unprovable means honour the author's ``required`` declaration
        (elspeth-f6ddcebbe3, which mandates this inversion).
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "product", "field_type": "str"}]},
                "operations": [
                    {"target": "other", "expression": "row[row['k']]"},
                    {"target": "product", "expression": "'x'"},
                ],
            }
        )

        assert transform.input_schema.model_fields["product"].is_required()

    def test_dynamic_read_position_does_not_change_whether_the_config_builds(self) -> None:
        """Rejection is order-independent; demotion legitimately is not.

        The same two operations in either order describe the same pipeline, so
        one order must not reject while the other builds (elspeth-f6ddcebbe3).
        Demotion is a different question: operations apply SEQUENTIALLY, so
        whether a target is already assigned when a read runs genuinely depends
        on position. Listed first, ``product`` is provably created before any
        read and is demoted; listed after the dynamic subscript, that read could
        have consumed it from the input row, so the requirement stands.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        schema = {"mode": "flexible", "fields": [{"name": "product", "field_type": "str"}]}
        dynamic_read = {"target": "other", "expression": "row[row['k']]"}
        literal_write = {"target": "product", "expression": "'x'"}

        dynamic_first = ValueTransform({"schema": schema, "operations": [dynamic_read, literal_write]})
        literal_first = ValueTransform({"schema": schema, "operations": [literal_write, dynamic_read]})

        assert dynamic_first.input_schema.model_fields["product"].is_required()
        assert not literal_first.input_schema.model_fields["product"].is_required()

    def test_dynamic_key_read_is_legal_when_no_target_is_declared_required(self) -> None:
        """The abstention only matters when a declared required field is at stake."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "other", "field_type": "str"}]},
                "operations": [{"target": "product", "expression": "row[row['key']]"}],
            }
        )

        assert transform.input_schema.model_fields["other"].is_required()

    def test_original_header_read_of_its_own_target_stays_required(self) -> None:
        """A self-overwrite spelled with the ORIGINAL header is still a read.

        ``PipelineRow`` resolves a field under both its normalized_name and its
        original_name, and ``process`` rebuilds the row each operation precisely
        to preserve that. So ``row['Price USD']`` reads the very field the
        operation targets, and comparing the two spellings with string equality
        classified the overwrite as a creation and dropped a genuine input
        requirement (elspeth-f605f0a94e).
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {
                    "mode": "fixed",
                    "fields": [{"name": "price_usd", "type": "float", "required": True, "nullable": False}],
                },
                "operations": [{"target": "price_usd", "expression": "row['Price USD'] * 1.05"}],
            }
        )

        assert transform.self_created_input_fields == frozenset()
        assert transform.demoted_input_fields == frozenset()
        assert transform.input_schema.model_fields["price_usd"].is_required()
        with pytest.raises(ValidationError):
            transform.input_schema.model_validate({}, strict=True)

    def test_original_header_read_settles_a_target_beside_a_dynamic_sibling_read(self) -> None:
        """The alias read CONSUMES the target — it does not merely leave it unproven.

        Discriminates the two ways a target can end up required. An unresolved
        subscript in the target's own expression makes its status undecidable
        and is rejected at construction; a resolved read — including one spelled
        with the original header — settles the target as a genuine input, which
        the sibling subscript cannot unsettle. Building at all is the assertion.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "price_usd", "field_type": "float"}]},
                "operations": [{"target": "price_usd", "expression": "row[row['k']] + row['Price USD']"}],
            }
        )

        assert transform.input_schema.model_fields["price_usd"].is_required()

    def test_original_header_read_of_an_already_created_target_keeps_the_demotion(self) -> None:
        """Alias resolution must not turn a later read of a CREATED field into an input.

        The mirror of the case above: operation 2 reads what operation 1 wrote,
        just spelled with the original header. Sequential visibility means that
        read sees the computed value, so ``price_usd`` is still created here and
        must stay demoted — matching alias spellings on reads alone, without
        matching them on assignments too, would silently lose the demotion.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "price_usd", "field_type": "float"}]},
                "operations": [
                    {"target": "price_usd", "expression": "1.0"},
                    {"target": "with_tax", "expression": "row['Price USD'] * 2"},
                ],
            }
        )

        assert transform.self_created_input_fields == frozenset({"price_usd", "with_tax"})
        assert not transform.input_schema.model_fields["price_usd"].is_required()

    def test_dynamic_reassignment_cannot_unsettle_an_earlier_proven_creation(self) -> None:
        """A target's FIRST assignment settles it; a later dynamic key cannot revoke that.

        ``x`` is provably created by operation 1 — nothing precedes it that could
        read the field off the input row. Operation 2's unresolvable subscript
        runs against a row that already carries the computed ``x``, so it says
        nothing about the input. The old set arithmetic subtracted the
        undecidable set from the created set unconditionally, letting operation 2
        overturn operation 1's proof and reject the config outright.
        """
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "flexible", "fields": [{"name": "x", "field_type": "int"}]},
                "operations": [
                    {"target": "x", "expression": "1"},
                    {"target": "x", "expression": "row[row['k']]"},
                ],
            }
        )

        assert not transform.input_schema.model_fields["x"].is_required()

    def test_observed_mode_targets_stay_legal(self) -> None:
        """Observed mode declares no required inputs, so targets never contradict it."""
        from elspeth.plugins.transforms.value_transform import ValueTransform

        transform = ValueTransform(
            {
                "schema": {"mode": "observed"},
                "operations": [{"target": "product", "expression": "row['item']['product']"}],
            }
        )
        assert transform is not None


class TestComposerHintsMatchSandboxReality:
    """The composer_hints capability claims must be sandbox-true (elspeth-18bcf7dd09).

    Battery r4 g05: the hints advertised uppercase and regex extract, neither
    of which has ANY sandbox-accepted spelling (only ``len``/``abs`` are
    callable and attribute calls are rejected), sending the composer down a
    53-second dead end before an honest decline. Every capability the
    achievable-capabilities hint claims is pinned here against the real
    ``ExpressionParser``; the impossible ones are pinned as rejected so the
    not-achievable hint stays true as the grammar evolves.
    """

    ACHIEVABLE_CLAIMS: ClassVar[dict[str, str]] = {
        "arithmetic": "row['a'] + row['b']",
        "len": "len(row['text']) * 2",
        "abs": "abs(row['n'])",
        "field copy/overwrite": "row['a']",
        "string concatenation": "row['a'] + '-suffix'",
        "membership test": "row['status'] in ['open', 'closed']",
        "conditional value": "1 if row['score'] > 10 else 0",
    }

    IMPOSSIBLE_CLAIMS: tuple[str, ...] = (
        "row['a'].upper()",
        "row['a'].title()",
        "row['a'].lower()",
        "re.match('x', row['a'])",
    )

    def test_capabilities_advertised_as_achievable_parse(self) -> None:
        from elspeth.core.expression_parser import ExpressionParser

        for capability, expression in self.ACHIEVABLE_CLAIMS.items():
            try:
                ExpressionParser(expression)
            except Exception as exc:  # pragma: no cover - failure message only
                raise AssertionError(f"advertised capability {capability!r} has no sandbox-accepted spelling: {exc}") from exc

    def test_string_methods_and_regex_stay_rejected(self) -> None:
        from elspeth.core.expression_parser import ExpressionParser, ExpressionSecurityError

        for expression in self.IMPOSSIBLE_CLAIMS:
            with pytest.raises(ExpressionSecurityError):
                ExpressionParser(expression)

    def test_achievable_hint_names_only_sandbox_true_capabilities(self) -> None:
        from elspeth.plugins.transforms.value_transform import ValueTransform

        assistance = ValueTransform.get_agent_assistance()
        assert assistance is not None
        achievable_hints = [hint for hint in assistance.composer_hints if hint.startswith("Achievable here:")]
        assert len(achievable_hints) == 1, "composer_hints must carry exactly one achievable-capabilities hint"
        hint = achievable_hints[0]
        assert "uppercase" not in hint, "uppercase has no sandbox-accepted spelling (elspeth-18bcf7dd09)"
        assert "regex" not in hint, "regex extraction has no sandbox-accepted spelling (elspeth-18bcf7dd09)"
        not_achievable_hints = [hint for hint in assistance.composer_hints if hint.startswith("NOT achievable here:")]
        assert len(not_achievable_hints) == 1, "composer_hints must state what the sandbox rejects"
