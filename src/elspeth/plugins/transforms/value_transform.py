"""ValueTransform transform plugin.

Applies expressions to compute new or modified field values.

A computed value is written as evaluated, never coerced. Every operation
target is DECLARED before the first row (ADR-050): a target the node's schema
types (``fields: ["a: int"]`` with ``target: a``) carries that type. Otherwise
the expression's provable result type over build-declared inputs becomes the
plugin type; an unknown result stays nullable ``any``. The declaration is
stamped on every emitted row's contract and fixed before row 1. A
typed target is pinned: a row whose computed value does not satisfy the
declared type is returned as a ``type_mismatch`` error naming the target and
both type names, never the value. An expression whose value can be a set is
rejected at construction: a set has no canonical order to emit. An expression
that fails to evaluate on a row returns that row as an error.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.field_spelling import header_spelling_canonical
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig, declare_missing_guaranteed_fields
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract, declared_type_name_admits
from elspeth.contracts.type_normalization import classify_runtime_type
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.expression_parser import (
    ExpressionEvaluationError,
    ExpressionParser,
    ExpressionSecurityError,
    ExpressionSyntaxError,
)
from elspeth.core.expression_types import ResultKinds, declared_result, expression_kinds, input_kinds
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sources.field_normalization import ExternalHeaderError, normalize_field_name


def _row_key_aliases(name: str) -> frozenset[str]:
    """Every spelling under which ``name`` can resolve on a ``PipelineRow``.

    A row resolves a field under BOTH its normalized_name and its original_name
    (``SchemaContract.find_name``), and sources derive the former from the
    latter with ``normalize_field_name``. So an operation target and an
    expression literal name the same field whenever their alias sets meet, and
    string equality is not that test.

    A literal that normalizes to nothing names no field at all: the source
    boundary rejects such a header, so no contract can carry one.
    """
    try:
        return frozenset({name, normalize_field_name(name)})
    except ExternalHeaderError:
        return frozenset({name})


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
        if parser.result_can_be_set():
            raise ValueError(
                f"The expression for target {self.target!r} can produce a set, which has no canonical "
                f"order: the value written to the row, and the row's output hash, would differ between "
                f"runs of identical input. Use a list [...] or tuple (...) literal. A set literal is "
                f"allowed only where it is consumed, such as a membership test (row['x'] in {{'a', 'b'}}) "
                f"or a function call (len({{row['a'], row['b']}}))."
            )
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

    @model_validator(mode="after")
    def _reject_targets_spelling_one_another(self) -> ValueTransformConfig:
        """Refuse a target that is a header spelling of another target (field-name spelling rule).

        Operations run in order on one working row, so ``target: name`` then
        ``target: Name`` writes two keys that name one field — the created-name
        shadow the rule refuses (operator ruling 2026-09-25, 2026-09-26 Q4
        amendment (c)). A target against the ARRIVING row is settled by the
        build and the executor preflight, which see upstream; a target against
        its own node's other targets is decidable from this config alone, so it
        is refused here, with the same predicate.
        """
        targets = frozenset(op.target for op in self.operations)
        spellings = sorted(
            (target, canonical) for target in targets if (canonical := header_spelling_canonical(target, targets - {target})) is not None
        )
        if spellings:
            raise ValueError(
                "; ".join(
                    f"target '{target}' is a header spelling of target '{canonical}': both would be written as separate "
                    f"fields of one row. Name both '{canonical}'"
                    for target, canonical in spellings
                )
            )
        return self

    def created_before_read_targets(self) -> frozenset[str]:
        """Targets this transform CREATES: assigned before any operation reads them.

        The 'schema' block is this transform's INPUT contract, but authors also
        use it to name the emitted shape. A target assigned before any
        operation reads it is created here, so it must not be required on
        input — ``BaseTransform.input_schema`` demotes these to optional
        (elspeth-d6eeb3a71d).

        An operation that reads its own target (``row['price'] * 1.1``, or the
        same field under its original header) — or any target read before its
        first assignment — is a genuine input consumer and is NOT reported, so
        its input requirement survives.

        Conservative, and scoped PER TARGET: a read that cannot be statically
        resolved to a literal key (``row[row['k']]``) leaves undecidable only
        the target of the operation carrying it, and merely unproven — required
        but not rejected — those assigned after it. See
        ``_analyse_operation_reads`` for why, and for the residuals that scoping
        accepts. An undecidable target that is also declared required is
        rejected at construction by
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
        """Classify every target as read, created, undecidable, or unproven.

        Returns ``(created_before_read, {undecidable_target: blocking_expression})``.
        A target in NEITHER result is UNPROVEN: not demoted and not rejected, so
        the author's declaration stands. Only a proof demotes; only a proof of
        the target's own unanalysability rejects.

        Names are compared across every spelling a row resolves, not by string
        equality. Targets and ``schema.fields`` are written in the normalized
        name space while an expression literal may be the source's original
        header, and a row resolves both — so ``row['Price USD']`` reads the very
        field ``target: price_usd`` overwrites. Comparing the raw strings called
        that a creation and dropped a genuine input requirement
        (elspeth-f605f0a94e). Both sides expand to ``_row_key_aliases``, on
        ASSIGNMENTS as well as reads: a later read of a field an earlier
        operation wrote sees the computed value whichever spelling it uses, and
        matching aliases on reads alone would lose that demotion.

        Abstention is scoped to the TARGET, and only the target's OWN expression
        can make it undecidable. An unresolvable subscript elsewhere in the list
        means some earlier operation might have consumed this target from the
        input row, so it is not provably created — but neither is it provably
        broken, and rejecting on that basis made the verdict depend on operation
        ORDER: the same pair listed the other way round built (elspeth-f6ddcebbe3).
        Such a target is left unproven, which withholds the demotion without
        inventing an unsatisfiability — the same false claim the 7a5d72d34 guard
        was retracted for.

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
          target's assignment, and demotion is exactly what that shape does not
          get: the earlier dynamic read leaves the later target unproven, so it
          keeps its declared requirement. Swapping the two operations above
          turns ``total`` from demoted into required — the hazard is closed by
          withholding the demotion, not by rejecting the config.

        What is left is a dynamic key that resolves to a demoted field never
        assigned earlier — which fails as a KeyError from the dynamic key
        itself, a hazard inherent to non-literal subscripts rather than one
        demotion introduces. No shipped config uses a dynamic key at all
        (``row[row[`` appears nowhere in examples/, tests/fixtures/ or docs/).

        Alias resolution has its own residual, accepted on the same terms. It
        reproduces the SOURCE boundary's original-to-normalized mapping, which
        is the shipped way two spellings come to name one field; a
        ``field_mapping`` rename can break that correspondence, so a literal
        whose normalization matches no target could still resolve to one. The
        upstream mapping does not exist at config time, so the only sound
        alternative is to treat every unrecognised literal as a possible read of
        every target — which withholds the demotion from the ordinary
        ``{total: "row['price'] * row['qty']"}`` shape and restores the
        elspeth-d6eeb3a71d trap it exists to prevent. A live false requirement
        is not worth trading for a theoretical missed one.
        """
        assigned_keys: set[str] = set()
        read_keys: set[str] = set()
        created: set[str] = set()
        undecidable: dict[str, str] = {}
        classified: set[str] = set()
        dynamic_read_seen = False

        for op in self.operations:
            reads = op.get_parser().static_field_reads()
            for literal in reads.fields:
                literal_keys = _row_key_aliases(literal)
                # Literal reads resolve even in an expression that is incomplete
                # overall. A read of an already-assigned field sees the computed
                # value, so it is not a read of the input row.
                if literal_keys.isdisjoint(assigned_keys):
                    read_keys |= literal_keys
            target_keys = _row_key_aliases(op.target)
            # The FIRST assignment settles a target: it fixes the window of
            # operations that could have read the field off the input row.
            if op.target not in classified:
                classified.add(op.target)
                if target_keys.isdisjoint(read_keys):
                    if not reads.complete:
                        undecidable[op.target] = op.expression
                    elif not dynamic_read_seen:
                        created.add(op.target)
            assigned_keys |= target_keys
            if not reads.complete:
                dynamic_read_seen = True

        return frozenset(created), undecidable

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
        target whose OWN assigning expression carries the non-literal subscript.
        A provable read, a provable creation, and a target merely stranded by
        some OTHER operation's dynamic key are all left alone — the last of
        those keeps its declared requirement instead, since an unprovable config
        is not a broken one (elspeth-f6ddcebbe3).
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
    source_file_hash: str | None = "sha256:60e856c2e2543429"
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
        self._input_types: dict[str, FieldDefinition] = {field.name: field for field in cfg.schema_config.fields or ()}
        self._upstream_bound = False
        self._process_started = False

        # declared_output_fields intentionally empty — we can't statically know which
        # targets are new vs overwrites, and overwrites are an intentional feature.
        # The executor's field collision check only runs when this is non-empty.
        self.declared_output_fields: frozenset[str] = frozenset()

        # ...so the input-demotion set is computed separately: the targets this
        # transform creates rather than consumes (elspeth-d6eeb3a71d).
        self._self_created_input_fields = cfg.created_before_read_targets()

        self._output_schema_config = self._build_value_transform_output_schema_config(cfg)

        self._refresh_target_contracts()

        self.input_schema = create_schema_from_config(
            cfg.schema_config,
            "ValueTransformInput",
            allow_coercion=False,
        )
        self.output_schema = create_schema_from_config(
            self._output_schema_config,
            "ValueTransformOutput",
            allow_coercion=False,
        )

    def _derived_kinds(self) -> dict[str, ResultKinds]:
        """Type ordered expressions against the node's currently bound input types."""
        previous: dict[str, ResultKinds] = {}
        derived: dict[str, ResultKinds] = {}

        def lookup(name: str) -> ResultKinds:
            for alias in _row_key_aliases(name):
                if alias in previous:
                    return previous[alias]
            for field in self._input_types.values():
                if _row_key_aliases(name) & _row_key_aliases(field.name):
                    return input_kinds(field)
            return None

        for operation in self._operations:
            kinds = expression_kinds(operation.get_parser(), lookup)
            derived[operation.target] = kinds
            for alias in _row_key_aliases(operation.target):
                previous[alias] = kinds
        return derived

    def _derived_targets(self) -> dict[str, FieldDefinition]:
        return {
            target: FieldDefinition(
                name=target,
                field_type=declared_result(kinds)[0],
                required=True,
                nullable=declared_result(kinds)[1],
            )
            for target, kinds in self._derived_kinds().items()
        }

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        """Publish the one derived plugin tier; authored target types retain precedence."""
        derived = self._derived_targets()
        return tuple(derived[target] for target in sorted(derived))

    def _refresh_target_contracts(self) -> None:
        stamped = self._stamped_output_field_contracts()
        self._target_contracts = SchemaContract(
            mode="FLEXIBLE",
            fields=tuple(stamped[target] for target in sorted(self._configured_targets)),
            locked=True,
        )

    def bind_upstream_input_types(self, fields: Mapping[str, FieldDefinition], *, component_id: str | None = None) -> None:
        """Complete the graph tier once before rows can be processed.

        A graph or Composer caller supplies its configured node ID for a
        contradiction; a standalone plugin probe has no graph node to name.
        """
        if self._upstream_bound or self._process_started:
            raise FrameworkBugError("value_transform input types can be bound only once before process")
        for name, field in fields.items():
            self._input_types.setdefault(name, field)
        authored = {field.name: field for field in self._schema_config.fields or ()} if self._schema_config is not None else {}
        derived_kinds = self._derived_kinds()
        for operation in self._operations:
            kinds = derived_kinds[operation.target]
            declaration = authored.get(operation.target)
            if declaration is None or declaration.field_type == "any" or not isinstance(kinds, frozenset) or not kinds:
                continue
            if any(
                (kind == "none" and declaration.nullable)
                or (kind in {"int", "float", "str", "bool"} and declared_type_name_admits(declaration.field_type, kind))
                for kind in kinds
            ):
                continue
            computed_type = "null" if kinds == {"none"} else declared_result(kinds)[0]
            raise GraphValidationError(
                f"Transform 'value_transform' target '{operation.target}' declares {declaration.field_type}, "
                f"but its expression computes {computed_type}. Change the target declaration or expression.",
                component_id=component_id,
                component_type="transform",
            )
        cfg = ValueTransformConfig.from_dict(self.config, plugin_name=self.name)
        self._output_schema_config = self._build_value_transform_output_schema_config(cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "ValueTransformOutput", allow_coercion=False)
        self._refresh_target_contracts()
        self._upstream_bound = True

    @property
    def upstream_types_bound(self) -> bool:
        return self._upstream_bound

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

        declared_fields = declare_missing_guaranteed_fields(cfg.schema_config.fields, guaranteed_fields_result)
        if declared_fields is not None:
            # Authored types, including authored ``any``, retain precedence.
            # Placeholder target declarations take the derived plugin type.
            derived = self._derived_targets()
            authored = {field.name for field in cfg.schema_config.fields or ()}
            declared_fields = tuple(
                derived[field.name]
                if field.name in self._configured_targets and field.name not in authored
                else replace(field, required=True, nullable=True)
                if field.name in self._configured_targets and field.field_type == "any"
                else field
                for field in declared_fields
            )

        return SchemaConfig(
            mode=cfg.schema_config.mode,
            # Configured targets are guaranteed on output but may be absent
            # from the authored input fields; declare them so the config
            # satisfies the guaranteed-fields-are-declared invariant
            # (elspeth-97487736ca).
            fields=declared_fields,
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
        self._process_started = True
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
                # The evaluator's message is value-free (a computed key or index
                # prints as a placeholder); ``kind`` is its stable classification.
                return TransformResult.error(
                    {
                        "reason": "invalid_input",
                        "field": target,
                        "error_type": e.kind,
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

            # Write result to working copy. A new target enters the working
            # contract so later operations resolve it; its type, like every
            # target's, is the DECLARATION stamped on emission below, never
            # this row's value (ADR-050).
            working_data[target] = result
            if working_contract.find_field(target) is None:
                working_contract = working_contract.with_field(target, target, result)

        # A target the node's schema types is pinned to that type: the emitted
        # row must satisfy the declaration downstream validation relies on. A
        # computed value that does not is this row's error, routed to on_error
        # — never coerced, and never an abort of the run. The reason names the
        # target and the two type names only. (An 'any' target passes: the
        # contract's validate() skips it.)
        target_values = {field.normalized_name: working_data[field.normalized_name] for field in self._target_contracts.fields}
        violations = self._target_contracts.validate(target_values)
        if violations:
            pinned = self._target_contracts.get_field(violations[0].normalized_name)
            expected_name = pinned.python_type.__name__
            actual_name = classify_runtime_type(target_values[pinned.normalized_name]).__name__
            declared_by = self.output_field_declared_by()[pinned.normalized_name]
            declarer = "this node's schema" if declared_by == "operator" else "the plugin's expression type"
            return TransformResult.error(
                {
                    "reason": "type_mismatch",
                    "field": pinned.normalized_name,
                    "expected": expected_name,
                    "actual": actual_name,
                    "declared_by": declared_by,
                    "message": (
                        f"Operation target '{pinned.normalized_name}' computed a value of type {actual_name}, "
                        f"but {declarer} declares it {expected_name}{'' if pinned.required and not pinned.nullable else ' (or None)'}. "
                        f"Declare the target 'any' (or the scalar type it computes) to store it."
                    ),
                }
            )
        # The one stamp: every target (and every field the node's schema
        # declares) carries its declared contract on emission.
        output_contract = self._align_output_contract(self._apply_declared_output_field_contracts(working_contract))
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
                    "Achievable here: arithmetic (row['a'] + row['b'], abs(row['n'])), field copies and overwrites, string "
                    "concatenation, case folding/trimming via lower(row['x']), upper(row['x']), strip(row['x']), "
                    "casefold(row['x']), membership tests, conditionals. For type changes use type_coerce.",
                    "NOT achievable here: regex (pattern extraction, splitting), title-casing, and method-call syntax "
                    "(row['x'].lower() is rejected; use lower(row['x'])). Only len, abs, lower, upper, strip, casefold "
                    "are callable. Route text rewriting beyond case folding through an llm transform.",
                    "Declare arriving types for consumed fields and output types for new targets. A provably incompatible authored target is refused at build; a value-dependent mismatch routes to on_error.",
                    "Treat any as its own type at typed consumers and union branches, never as a wildcard.",
                    "A target the schema does not type uses the expression's provable output type over declared inputs; "
                    "an unknown expression stays 'any'. Add a declaration or type_coerce before a typed consumer when needed.",
                    "When reading or overwriting an existing field, use its carried name (normally normalized header or source mapping target), not another spelling. A genuinely new target may use any valid new name.",
                    "Rows always pass through: an expression that evaluates to False just stores False — it does not drop or "
                    "error-route the row. Conditional row filtering is a gate node, not this transform.",
                    "A result may be nested (row['meta'], or a list, tuple or dict literal); its field is typed 'any'. A set "
                    "cannot be stored (no canonical order): use a list; keep set literals to membership tests or len().",
                    "Expressions are sandboxed — file I/O, imports, and external calls are rejected at parse time.",
                ),
            )
        return None
