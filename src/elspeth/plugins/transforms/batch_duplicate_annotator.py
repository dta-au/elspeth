"""Exact duplicate-cell admission over one finite, complete source window.

Every member is returned in order. Malformed comparison data annotates only its
member; write collisions, incomplete windows and capacity failures refuse the
whole batch. A downstream ordinary transform owns per-member rejection.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.contracts.union_merge import join_batch_contracts
from elspeth.core.expression_parser import ExpressionEvaluationError, ExpressionParser, ExpressionSecurityError, ExpressionSyntaxError
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import PluginConfigError, TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult

_MAX_BATCH_ROWS = 4096
_MAX_CELLS = 256
_MAX_COMPARISON_CHARS = 4 * 1024 * 1024
_MAX_CAUSE_CHARS = 128 * 1024


class DuplicateCell(BaseModel):
    """One canonical field in a rule's shared exact-string comparison domain."""

    model_config = {"extra": "forbid", "frozen": True}
    field: str = Field(strict=True, min_length=1, max_length=128, pattern=r"^[a-z_][a-z0-9_]*$")
    when: str | None = Field(default=None, strict=True, min_length=1, max_length=4096)
    _parser: ExpressionParser | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _compile(self) -> Self:
        if self.when is not None:
            try:
                self._parser = ExpressionParser(self.when)
            except (ExpressionSyntaxError, ExpressionSecurityError) as exc:
                raise ValueError(f"Invalid duplicate eligibility expression: {exc}") from exc
            if not self._parser.static_field_reads().complete:
                raise ValueError("Duplicate eligibility expressions require literal row-field keys")
        return self

    @property
    def read_fields(self) -> frozenset[str]:
        return frozenset({self.field}) | (self._parser.static_field_reads().fields if self._parser is not None else frozenset())

    def eligible(self, row: PipelineRow) -> bool | str:
        """A bool decision or a static, value-free row error category."""
        if self._parser is None:
            return True
        try:
            value = self._parser.evaluate(row)
        except ExpressionEvaluationError:
            return "eligibility_evaluation_error"
        return value if type(value) is bool else "eligibility_not_boolean"


class DuplicateRule(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    name: str = Field(strict=True, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    cells: tuple[DuplicateCell, ...] = Field(min_length=1, max_length=_MAX_CELLS)

    @model_validator(mode="after")
    def _unique_cells(self) -> Self:
        if len({cell.field for cell in self.cells}) != len(self.cells):
            raise ValueError("Each rule must select a canonical field only once")
        return self


class BatchDuplicateConfig(TransformDataConfig):
    rules: tuple[DuplicateRule, ...] = Field(
        min_length=1,
        max_length=16,
        description="Independent exact-string domains. Each rule compares all its selected fields across all rows, including different fields in one row. Total selected cells is at most 256.",
    )
    output_prefix: Annotated[str, EmittedToOutput("This prefix forms the emitted admission and cause column names.")] = Field(
        default="duplicates",
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z_][a-z0-9_]*$",
        description="Emits <prefix>_valid (bool) and <prefix>_causes (JSON string of static rule/field/category records).",
    )

    @model_validator(mode="after")
    def _bounded_unique_rules(self) -> Self:
        if len({rule.name for rule in self.rules}) != len(self.rules):
            raise ValueError("Duplicate rule names must be distinct")
        if sum(len(rule.cells) for rule in self.rules) > _MAX_CELLS:
            raise ValueError("At most 256 selected cells across all duplicate rules")
        return self


class BatchDuplicateAnnotator(BaseTransform):
    """Annotate every exact duplicate participant after complete source exhaustion.

    Each rule compares its selected fields together, including cross-field and
    same-row matches. No members are removed. Malformed cells add static causes
    only to their own member. The shared output contract preserves input values
    and adds a strict Boolean admission plus a JSON-string cause list.
    """

    name = "batch_duplicate_annotator"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:ac8e4181bdb2164b"
    config_model = BatchDuplicateConfig
    is_batch_aware = True
    flush_emits_one_row_per_buffered_row = True
    requires_aggregation_batch_context = True
    requires_complete_source_batch = True
    passes_through_input = True
    preserves_input_values = True
    capability_tags = ("batch", "duplicates", "annotation")
    usage_when_to_use = "Annotate exact duplicate cells at end-of-source across one finite complete source, retaining every original member for downstream admission."
    usage_when_not_to_use = "Not for unbounded, append, cross-run, partitioned or early-flush comparison. Does not judge semantic similarity or choose survivors."
    example_use = """aggregations:
  - name: compare_text
    plugin: batch_duplicate_annotator
    input: assessed
    on_success: annotated
    on_error: discard
    output_mode: passthrough
    options:
      schema: {mode: observed}
      rules:
        - name: repeated_text
          cells: [{field: first_text}, {field: second_text}]
"""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = BatchDuplicateConfig.from_dict(config, plugin_name=self.name)
        self._rules = cfg.rules
        self._read_fields = frozenset().union(*(cell.read_fields for rule in cfg.rules for cell in rule.cells))
        self._valid_field = f"{cfg.output_prefix}_valid"
        self._causes_field = f"{cfg.output_prefix}_causes"
        self.declared_output_fields = frozenset({self._valid_field, self._causes_field})
        if self._read_fields & self.declared_output_fields:
            raise PluginConfigError("Duplicate annotations cannot also be comparison or eligibility inputs", plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        if cfg.schema_config.fields is not None and any(field.name in self.declared_output_fields for field in cfg.schema_config.fields):
            raise PluginConfigError("Duplicate annotations collide with the authored input schema", plugin_name=self.name)
        self.input_schema, self.output_schema = self._create_schemas(cfg.schema_config, "BatchDuplicate", adds_fields=True)
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

    def _config_named_input_columns(self) -> frozenset[str]:
        return super()._config_named_input_columns() | self._read_fields

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        return (FieldDefinition(self._valid_field, "bool"), FieldDefinition(self._causes_field, "str"))

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {"schema": {"mode": "observed"}, "rules": [{"name": "duplicate", "cells": [{"field": "text"}]}]}

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is not None:
            return None
        return PluginAssistance(
            plugin_name=cls.name,
            issue_code=None,
            summary="Exact duplicate membership across all selected cells of one complete finite source window.",
            composer_hints=(
                "Use an aggregation with omitted/empty trigger and output_mode: passthrough before any report writer/fork. Every original member continues in order; no filtering or survivor selection.",
                "Each rule compares all its cells together across all rows, including different fields in the same row. Strings are exact: no trimming, case folding, Unicode normalization or semantic matching.",
                "Cells require canonical ASCII field names and actual strings. Optional when expressions use literal row keys and must return bool. False exempts valid text; missing/non-string data or invalid eligibility marks only its member invalid.",
                "Use observed/nonrestrictive batch schema for per-member error isolation. Explicit typed/required batch input schemas retain existing whole-batch failure behavior.",
                "The output Boolean <prefix>_valid and JSON-string <prefix>_causes carry all static causes without copied text. An LLM input_checks predicate can reject invalid members before row model calls; retain row_json in its error sink for all causes.",
                "Bounds: 4096 buffered rows, 16 rules, 256 selected cells total, 4 Mi characters inspected, 128 Ki characters of causes per member. Capacity refusal never splits the source. These caps do not bound memory already buffered before EOF.",
                "Finite serial one-member-per-source-case flow only for unchanged source identity; no append/cross-run or arbitrary forked/partitioned completeness claim. Transform output mode instead creates ordinary child tokens.",
            ),
        )

    @staticmethod
    def _refuse(cause: str) -> TransformResult:
        return TransformResult.error({"reason": "validation_failed", "cause": cause}, retryable=False)

    def process(self, row: PipelineRow | list[PipelineRow], ctx: TransformContext) -> TransformResult:
        if not isinstance(row, list) or not row:
            return self._refuse("nonempty_batch_required")
        rows = row
        if len(rows) > _MAX_BATCH_ROWS:
            return self._refuse("comparison_capacity_exceeded")
        window = ctx.aggregation_batch
        if window is None or not (
            window.trigger_type == "end_of_source"
            and window.is_end_of_source
            and window.flush_index == window.row_start == 1
            and window.batch_size == window.rows_seen_total == window.row_end == len(rows)
        ):
            return self._refuse("complete_source_window_required")
        canonical_rows = [member.to_dict() for member in rows]
        for member, values in zip(rows, canonical_rows, strict=True):
            names = set(values) | {name for field in member.contract.fields for name in (field.normalized_name, field.original_name)}
            if names & self.declared_output_fields:
                return self._refuse("output_field_collision")

        causes: list[set[tuple[str, str, str]]] = [set() for _ in rows]
        inspected_chars = 0
        for rule in self._rules:
            groups: dict[str, list[tuple[int, str]]] = {}
            for index, (member, values) in enumerate(zip(rows, canonical_rows, strict=True)):
                for cell in rule.cells:
                    # Canonical carried keys only; original header aliases cannot
                    # cause the same physical cell to be compared twice.
                    if cell.field not in values or type(values[cell.field]) is not str:
                        causes[index].add((rule.name, cell.field, "invalid_string"))
                        continue
                    value = values[cell.field]
                    inspected_chars += len(value)
                    if inspected_chars > _MAX_COMPARISON_CHARS:
                        return self._refuse("comparison_capacity_exceeded")
                    eligible = cell.eligible(member)
                    if isinstance(eligible, str):
                        causes[index].add((rule.name, cell.field, eligible))
                    elif eligible:
                        groups.setdefault(value, []).append((index, cell.field))
            for group in groups.values():
                if len(group) > 1:
                    for index, field in group:
                        causes[index].add((rule.name, field, "duplicate"))

        serialized = [
            json.dumps(
                [{"rule": rule, "field": field, "category": category} for rule, field, category in sorted(member_causes)],
                separators=(",", ":"),
            )
            for member_causes in causes
        ]
        if any(len(value) > _MAX_CAUSE_CHARS for value in serialized):
            return self._refuse("cause_capacity_exceeded")
        joined = join_batch_contracts(list({id(member.contract): member.contract for member in rows}.values()))
        additions = tuple(FieldContract(name, name, object, False, "inferred") for name in sorted(self.declared_output_fields))
        contract = self._align_output_contract(
            self._apply_declared_output_field_contracts(SchemaContract(mode=joined.mode, fields=(*joined.fields, *additions), locked=True))
        )
        return TransformResult.success_multi(
            [
                PipelineRow({**values, self._valid_field: not member_causes, self._causes_field: message}, contract)
                for values, member_causes, message in zip(canonical_rows, causes, serialized, strict=True)
            ],
            success_reason={"action": "enriched", "fields_added": sorted(self.declared_output_fields)},
        )
