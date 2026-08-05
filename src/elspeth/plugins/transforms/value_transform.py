"""ValueTransform transform plugin.

Applies expressions to compute new or modified field values.

IMPORTANT: Transforms use allow_coercion=False to catch upstream bugs.
If the source outputs wrong types, the transform crashes immediately.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.contracts.type_normalization import classify_runtime_type, require_supported_contract_type
from elspeth.core.expression_parser import (
    ExpressionEvaluationError,
    ExpressionParser,
    ExpressionSecurityError,
    ExpressionSyntaxError,
)
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult


def _retype_contract_field(
    contract: SchemaContract,
    field: FieldContract,
    value: object,
) -> SchemaContract:
    """Return a contract with ``field`` rebuilt to match ``value``'s type.

    Used when an operation overwrites an existing typed field with a value of a
    different type, so the emitted row continues to satisfy its own contract.
    """
    retyped = FieldContract(
        normalized_name=field.normalized_name,
        original_name=field.original_name,
        python_type=require_supported_contract_type(value),
        required=field.required,
        source=field.source,
        nullable=field.nullable,
    )
    new_fields = tuple(retyped if f.normalized_name == field.normalized_name else f for f in contract.fields)
    return SchemaContract(mode=contract.mode, fields=new_fields, locked=contract.locked)


class OperationSpec(BaseModel):
    """Single value transform operation specification."""

    model_config = {"extra": "forbid", "frozen": True}

    target: str
    expression: str
    # Parsed expression stored after validation
    _parsed_expression: ExpressionParser | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("target field name must not be empty")
        return v

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("expression must not be empty")
        return v

    @model_validator(mode="after")
    def _parse_expression(self) -> OperationSpec:
        """Parse and validate expression at config time."""
        try:
            parser = ExpressionParser(self.expression)
            # Store the parsed expression for later use
            object.__setattr__(self, "_parsed_expression", parser)
        except ExpressionSyntaxError as e:
            raise ValueError(f"Expression syntax error: {e}") from e
        except ExpressionSecurityError as e:
            raise ValueError(f"Expression contains forbidden constructs: {e}") from e
        return self

    def get_parser(self) -> ExpressionParser:
        """Get the pre-parsed expression parser."""
        if self._parsed_expression is None:
            # Re-parse if needed (shouldn't happen after validation)
            return ExpressionParser(self.expression)
        return self._parsed_expression


class ValueTransformConfig(TransformDataConfig):
    """Configuration for value transform.

    Requires 'schema' in config to define input/output expectations.
    Use 'schema: {mode: observed}' for dynamic field handling.
    """

    operations: list[OperationSpec] = Field(
        ...,
        description="List of operations to apply (target + expression pairs)",
    )

    @model_validator(mode="after")
    def _validate_operations_not_empty(self) -> ValueTransformConfig:
        if not self.operations:
            raise ValueError("operations must contain at least one operation")
        return self

    def created_before_read_targets(self) -> frozenset[str]:
        """Targets this transform CREATES: assigned before any operation reads them.

        The 'schema' block is this transform's INPUT contract, but authors also
        use it to name the emitted shape. A target assigned before any
        operation reads it is created here, so it must not be required on
        input — ``BaseTransform.input_schema`` demotes these to optional
        (elspeth-d6eeb3a71d).

        An operation that reads its own target (``row['price'] * 1.1``) — or
        any target read before its first assignment — is a genuine input
        consumer and is NOT reported, so its input requirement survives.

        Conservative, and scoped PER TARGET: a read that cannot be statically
        resolved to a literal key (``row[row['k']]``) leaves undecidable only
        those targets not already proven to be read — never the whole operation
        list. See ``_analyse_operation_reads`` for why, and for the residual
        that scoping accepts. An undecidable target that is also declared
        required is rejected at construction by
        ``_reject_unanalysable_reads_over_required_targets``.

        Supersedes the construction-time rejection landed as 7a5d72d34. That
        guard applied this same predicate to REJECT the config, but the
        rejection was wrong: for an overwrite that does not read its target
        (``total = row['price'] * row['qty']`` over a source that already
        carries ``total``) an upstream row DOES satisfy the contract, so the
        guard's "no upstream row can ever satisfy" claim was false and it
        rejected canonical data cleaning. Demotion is correct on both arms:
        absent -> created, present -> overwritten.
        """
        created, _undecidable = self._analyse_operation_reads()
        return created

    def _analyse_operation_reads(self) -> tuple[frozenset[str], dict[str, str]]:
        """Classify every target as read, created, or undecidable.

        Returns ``(created_before_read, {undecidable_target: blocking_expression})``.

        Abstention is scoped to the TARGET, never to the whole operation list.
        An unresolvable subscript makes it impossible to know which fields THAT
        expression reads, so it can only cast doubt on targets not yet proven to
        be read; a target with a literal read of its own name is settled, and a
        sibling operation's dynamic key cannot unsettle it. Abandoning the whole
        analysis on the first unresolvable read (as this did originally) rejected
        provably-fine configs — the same false-unsatisfiability claim the
        7a5d72d34 guard was retracted for.

        KNOWN RESIDUAL, ACCEPTED DELIBERATELY — DO NOT ADD A GUARD FOR IT.
        Per-target scoping means a dynamic read is no longer treated as reading
        everything, and a dynamic key can name ANY field at runtime, including
        one that was demoted. Concretely::

            [{total: "row['price']*2"}, {x: "row[row['k']]"}]   # total demoted, BUILDS

        Two properties keep that safe, and they are why no guard is warranted:

        * Operations apply SEQUENTIALLY to a mutating working copy (``process``
          deep-copies the row, then writes each target back before the next
          expression is evaluated). In the ordering above ``total`` already
          exists when the dynamic read runs, so that read sees the COMPUTED
          value, never the input — demotion cannot affect it.
        * The genuinely unsafe shape is a dynamic read ordered BEFORE a demoted
          target's assignment, and that shape is ALREADY REJECTED here: such a
          read leaves the later target undecidable, so a declared-required
          target trips the validator below. Swapping the two operations above
          turns the config from BUILDS into a construction error.

        What is left is a dynamic key that resolves to a demoted field never
        assigned earlier — which fails as a KeyError from the dynamic key
        itself, a hazard inherent to non-literal subscripts rather than one
        demotion introduces. No shipped config uses a dynamic key at all
        (``row[row[`` appears nowhere in examples/, tests/fixtures/ or docs/).
        The conservative alternative is whole-list abstention, which is exactly
        the behaviour removed above for rejecting configs that work today: a
        live false-reject is not worth trading for a theoretical one.
        """
        assigned: set[str] = set()
        read_before_assign: set[str] = set()
        undecidable: dict[str, str] = {}
        blocking_expression: str | None = None

        for op in self.operations:
            reads = op.get_parser().static_field_reads()
            # Literal reads resolve even in an expression that is incomplete
            # overall, and they count before this operation's own assignment.
            read_before_assign |= reads.fields - assigned
            if not reads.complete and blocking_expression is None:
                blocking_expression = op.expression
            if op.target not in read_before_assign and blocking_expression is not None:
                # Some earlier-or-current expression could have read this target
                # before it was assigned; we cannot prove it is created here.
                undecidable[op.target] = blocking_expression
            assigned.add(op.target)

        created = frozenset(assigned - read_before_assign - set(undecidable))
        return created, undecidable

    @model_validator(mode="after")
    def _reject_unanalysable_reads_over_required_targets(self) -> ValueTransformConfig:
        """Fail at construction when abstention would strand a required target.

        A dynamic subscript (``row[row['k']]``) makes it impossible to prove
        whether a target is read or created, so the target cannot safely be
        demoted and stays required on input. Left alone that rejects EVERY row
        at runtime with a generic "field required" and no hint that the analysis
        abstained. Reject here instead, naming the expression and the fixes.

        Deliberately much narrower than the guard removed from 7a5d72d34: that
        one fired when the analysis SUCCEEDED and proved the field was created,
        which is exactly the case demotion now handles. This one fires only for a
        target whose OWN status is undecidable — a provable read or a provable
        creation is never rejected, whatever a sibling operation does.
        """
        declared_fields = self.schema_config.fields
        if not declared_fields:
            return self
        required_on_input = {field.name for field in declared_fields if field.required}
        if not required_on_input:
            return self

        _created, undecidable = self._analyse_operation_reads()
        stranded = sorted(required_on_input & set(undecidable))
        if not stranded:
            return self

        blocking = undecidable[stranded[0]]
        raise ValueError(
            f"schema.fields declares {stranded} as required input, and these are also "
            f"operation targets whose read/create status cannot be determined: the "
            f"expression {blocking!r} has a non-literal subscript key, so it is "
            f"impossible to prove whether these fields arrive on the row or are created "
            f"here. They are therefore left required, and if they are in fact created "
            f"every row will fail input validation at runtime. Either use a literal key "
            f"so the reads can be resolved, or declare these fields with "
            f"'required': false."
        )


# =============================================================================
# ValueTransform Plugin Class
# =============================================================================


class ValueTransform(BaseTransform):
    """Apply expressions to compute new or modified field values.

    Operations are evaluated in order on a working copy of the row.
    Each operation sees the results of prior operations (sequential visibility).
    If all operations succeed, the updated row is emitted.
    If any operation fails, the original row is returned as an error
    and no partial changes are emitted on the success path.

    Config options:
        schema: Required. Schema for input/output (use {mode: observed} for any fields)
        operations: List of {target, expression} specs defining field computations
    """

    name = "value_transform"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:69c86168099b080d"
    config_model = ValueTransformConfig
    passes_through_input = True
    usage_when_to_use: str = (
        "Use for ordered expression-based field calculation when every input row follows pass-through semantics "
        "and each operation may read fields computed by an earlier operation."
    )
    usage_when_not_to_use = (
        "Row filtering or routing — value_transform never drops rows; every row passes "
        "through with its computed fields. Use a gate node for conditional row filtering "
        "(or keyword_filter for regex pattern blocking)."
    )
    example_use: str = """transform:
  plugin: value_transform
  options:
    operations:
      - target: total
        expression: "row['price'] * row['quantity']"
      - target: discounted_total
        expression: "row['total'] * 0.9"
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("expressions", "calculation", "fields")

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = ValueTransformConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._operations = cfg.operations
        self._configured_targets = frozenset(op.target for op in self._operations)
        self._schema_config = cfg.schema_config

        # declared_output_fields intentionally empty — we can't statically know which
        # targets are new vs overwrites, and overwrites are an intentional feature.
        # The executor's field collision check only runs when this is non-empty.
        self.declared_output_fields: frozenset[str] = frozenset()

        # ...so the input-demotion set is computed separately: the targets this
        # transform creates rather than consumes (elspeth-d6eeb3a71d).
        self._self_created_input_fields = cfg.created_before_read_targets()

        self._output_schema_config = self._build_value_transform_output_schema_config(cfg)

        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "ValueTransform",
            adds_fields=True,
        )

    @property
    def self_created_input_fields(self) -> frozenset[str]:
        """Override: value_transform keeps declared_output_fields empty by design.

        The demotion set is the operation targets created rather than consumed,
        which the base class default (``declared_output_fields``) cannot see.
        """
        return self._self_created_input_fields

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "schema": {"mode": "observed"},
            "operations": [
                {
                    "target": "value_transform_probe_added_1",
                    "expression": "1",
                }
            ],
        }

    def _build_value_transform_output_schema_config(
        self,
        cfg: ValueTransformConfig,
    ) -> SchemaConfig:
        """Build output guarantees for configured targets without forcing collision checks."""

        base_guaranteed = set(cfg.schema_config.guaranteed_fields or ())
        output_fields = base_guaranteed | self._configured_targets

        # Preserve None-vs-empty-tuple semantics: None = abstain, () = explicitly empty.
        # If upstream declared guarantees or this transform always writes targets,
        # declare the effective guarantees explicitly for DAG validation.
        upstream_declared = cfg.schema_config.guaranteed_fields is not None
        if upstream_declared or output_fields:
            guaranteed_fields_result = tuple(sorted(output_fields))
        else:
            guaranteed_fields_result = None

        return SchemaConfig(
            mode=cfg.schema_config.mode,
            fields=cfg.schema_config.fields,
            guaranteed_fields=guaranteed_fields_result,
            audit_fields=cfg.schema_config.audit_fields,
            required_fields=cfg.schema_config.required_fields,
        )

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        """Apply expression operations to row.

        Args:
            row: Input row data
            ctx: Plugin context

        Returns:
            TransformResult with computed field values, or error if any operation fails
        """
        # Work on a copy to support atomic rollback
        working_data = copy.deepcopy(row.to_dict())
        working_contract = row.contract
        fields_modified: list[str] = []
        fields_added: list[str] = []
        original_fields = set(row.to_dict().keys())

        for op in self._operations:
            target = op.target
            parser = op.get_parser()

            # Create PipelineRow for evaluation to preserve dual-name access
            # (expressions can use original headers like row['Price USD'])
            working_row = PipelineRow(working_data, working_contract)

            try:
                result = parser.evaluate(working_row)
            except ExpressionEvaluationError as e:
                return TransformResult.error(
                    {
                        "reason": "invalid_input",
                        "field": target,
                        "message": str(e),
                    }
                )

            # Track field changes
            if target in original_fields:
                if target not in fields_modified:
                    fields_modified.append(target)
            else:
                if target not in fields_added:
                    fields_added.append(target)

            # Write result to working copy
            working_data[target] = result
            existing_field = working_contract.find_field(target)
            if existing_field is None:
                working_contract = working_contract.with_field(target, target, result)
            elif existing_field.python_type is not object and classify_runtime_type(result) is not existing_field.python_type:
                # Overwriting a typed field with a different-typed result: retype the
                # field so the emitted row satisfies its OWN contract. Keeping the stale
                # python_type produces a self-contradictory audit record (the row fails
                # out.contract.validate(out.to_dict())) — mirrors type_coerce's
                # _build_output_contract. 'object'/'any' fields accept any value, so
                # they never need retyping.
                working_contract = _retype_contract_field(working_contract, existing_field, result)

        output_contract = self._align_output_contract(working_contract)
        return TransformResult.success(
            PipelineRow(working_data, output_contract),
            success_reason={
                "action": "transformed",
                "fields_modified": fields_modified,
                "fields_added": fields_added,
                "metadata": {
                    "operations_applied": len(self._operations),
                },
            },
        )

    def close(self) -> None:
        """No resources to release."""
        pass

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name="value_transform",
                issue_code=None,
                summary=(
                    "Compute new or overwritten field values with per-row expressions using a "
                    "restricted expression grammar. Assignment-only: every row passes through — "
                    "it cannot drop, keep, or route rows. Stateless and pure."
                ),
                composer_hints=(
                    "Call get_expression_grammar to see the allowed operations — only stdlib-safe expressions are permitted.",
                    "Use this for field-level transformations (uppercase, regex extract, arithmetic). For type changes use type_coerce.",
                    "Rows always pass through: an expression that evaluates to False just stores False — it does not drop or "
                    "error-route the row. Conditional row filtering is a gate node, not this transform.",
                    "Expressions are sandboxed — file I/O, imports, and external calls are rejected at parse time.",
                ),
            )
        return None
