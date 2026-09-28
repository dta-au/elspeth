"""Batch rank transform plugin.

Annotates every buffered row with its rank and percentile of a numeric
``value_field`` within the batch, and emits exactly one row per buffered row,
in buffered order. That shape is what ``output_mode: passthrough`` carries:
each buffered token continues with its own, annotated row. The same plugin also
runs under ``output_mode: transform``, where each emitted row becomes a new
child token of the batch.

Definitions (for one batch):

* A row is RANKED when its ``value_field`` value is a finite ``int`` or
  ``float``. A row whose field is absent, ``None``, or a non-finite float is
  UNRANKED: it is still emitted, unchanged apart from the annotations, with
  ``rank`` and ``percentile`` ``None``. A present value of any other type
  (``str``, ``bool``, ``Decimal`` …) fails the whole batch, value-free.
* ``order: descending`` (the default) ranks the highest value first;
  ``ascending`` ranks the lowest first. "Better" and "worse" below follow the
  configured order.
* ``ties: competition`` (the default): rank = 1 + the number of ranked rows
  strictly better, so equal values share a rank and the next rank is skipped
  (1, 2, 2, 4). ``ties: dense``: rank = 1 + the number of DISTINCT ranked
  values strictly better (1, 2, 2, 3).
* ``percentile`` = 100 * (ranked rows strictly worse) / ``ranked_count``: the
  share of the batch's ranked rows this row beats. It lies in [0, 100), is the
  same under both ties modes, is 0.0 for a batch with one ranked row, and is
  never 100 (a row never beats itself).
* Values are compared as they arrive, never converted: ``1`` and ``1.0`` tie.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.errors import TransformErrorReason
from elspeth.contracts.field_collision import detect_field_collisions
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.contracts.union_merge import join_batch_contracts
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import PluginConfigError, TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms._batch_row_types import BatchRowFieldCollisionError, BatchRowTypeError

type RankOrder = Literal["descending", "ascending"]
type RankTies = Literal["competition", "dense"]

# Every annotation suffix with the type this plugin's code fixes (ADR-050).
# ``rank`` is an int computed by counting and ``percentile`` a float computed by
# true division; both are None exactly when the row is unranked. The two counts
# are ints and always present.
_RANK_CREATED_SUFFIXES: tuple[FieldDefinition, ...] = (
    FieldDefinition("rank", "int", nullable=True),
    FieldDefinition("percentile", "float", nullable=True),
    FieldDefinition("ranked_count", "int"),
    FieldDefinition("batch_size", "int"),
)
_RANK_FIELD_SUFFIXES = tuple(field.name for field in _RANK_CREATED_SUFFIXES)
_MAX_BATCH_ROWS = 4096


def _rank_fields(output_prefix: str) -> frozenset[str]:
    return frozenset(f"{output_prefix}_{suffix}" for suffix in _RANK_FIELD_SUFFIXES)


class BatchRankConfig(TransformDataConfig):
    """Configuration for the batch rank transform."""

    value_field: str = Field(description="Name of the numeric field to rank rows by")
    output_prefix: Annotated[
        str,
        EmittedToOutput(
            "batch_rank builds its emitted annotation field NAMES from this prefix, "
            "so the value becomes a key in row data and a column in the artifact header"
        ),
    ] = Field(
        default="rank",
        description="Prefix used for the emitted <prefix>_rank, _percentile, _ranked_count and _batch_size fields",
    )
    order: RankOrder = Field(
        default="descending",
        description="descending ranks the highest value 1; ascending ranks the lowest value 1",
    )
    ties: RankTies = Field(
        default="competition",
        description="competition gives equal values one rank and skips the next (1, 2, 2, 4); dense does not skip (1, 2, 2, 3)",
    )

    @field_validator("value_field")
    @classmethod
    def _reject_empty_value_field(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("value_field must not be empty")
        return v

    @field_validator("output_prefix")
    @classmethod
    def _reject_invalid_output_prefix(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("output_prefix must not be empty")
        if not v.isidentifier():
            raise ValueError("output_prefix must be a valid Python identifier prefix")
        return v

    @model_validator(mode="after")
    def _reject_value_field_collision(self) -> BatchRankConfig:
        output_fields = _rank_fields(self.output_prefix)
        if self.value_field in output_fields:
            raise ValueError(
                f"value_field '{self.value_field}' collides with a batch_rank output field. "
                f"Choose a value_field that is not one of: {', '.join(sorted(output_fields))}"
            )
        return self


class BatchRank(BaseTransform):
    """Rank every row of a batch by a numeric field; one output row per input row, in order."""

    name = "batch_rank"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:227225a4dc42b7f9"
    config_model = BatchRankConfig
    is_batch_aware = True
    # Passthrough-capable: every successful flush is a success_multi of exactly
    # one row per buffered row, in buffered order (an unranked row is emitted
    # with null rank), with no quarantined_indices.
    flush_emits_one_row_per_buffered_row = True
    # Every emitted row is the input row plus the four annotations, and an input
    # row already carrying an annotation name fails the whole batch rather than
    # being overwritten, so every input field survives with its value unchanged.
    passes_through_input = True
    preserves_input_values = True
    usage_when_to_use: str = (
        "Use to add each row's rank and percentile of a numeric field within its aggregation batch while "
        "keeping every row: the only batch plugin output_mode: passthrough accepts, so the same tokens "
        "continue downstream (for example to a gate that keeps the top-ranked rows)."
    )
    usage_when_not_to_use: str = (
        "Not for ranking across the whole run or across batches (ranks restart in every batch), not for "
        "selecting only the top rows by itself (route on the rank with a gate), and not for text or "
        "boolean scores, which fail the batch."
    )
    example_use: str = """aggregations:
  - name: rank_candidates
    plugin: batch_rank
    input: scored_candidates
    on_success: ranked
    on_error: discard
    trigger:
      count: 4
    output_mode: passthrough
    options:
      value_field: judge_score
      output_prefix: score
      order: descending
      ties: competition
      schema:
        mode: observed
"""
    capability_tags: tuple[str, ...] = ("batch", "rank", "annotation")

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Adds each row's rank and percentile of a numeric field within its batch, keeping every row.",
                composer_hints=(
                    "batch_rank is the batch plugin to use under an aggregation with output_mode: passthrough; "
                    "it emits exactly one row per buffered row, in order, so the same tokens continue downstream.",
                    "It also runs under output_mode: transform, where each emitted row becomes a new child token.",
                    "Ranks restart in every batch: pick the trigger (count, timeout or condition) so a batch is the group to rank.",
                    "A null, absent or non-finite value keeps its row with null rank and percentile; "
                    "a present text or boolean value fails the whole batch.",
                    "order: descending ranks the highest value 1; ties: competition gives 1, 2, 2, 4 and dense gives 1, 2, 2, 3.",
                    "To keep only the top rows, route on <prefix>_rank with a gate after the aggregation; "
                    "test the rank for null before comparing it.",
                ),
            )
        return None

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the ADR-009 invariant harness."""
        return {
            "schema": {"mode": "observed"},
            "value_field": "batch_rank_probe_value",
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = BatchRankConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._value_field = cfg.value_field
        self._output_prefix = cfg.output_prefix
        self._descending = cfg.order == "descending"
        self._dense = cfg.ties == "dense"
        self.declared_output_fields = _rank_fields(cfg.output_prefix)

        # value_field is NOT folded into schema.required_fields: a row without
        # it is emitted unranked, like a null value (the batch_replicate
        # copies_field precedent, read only when present). It is still a
        # consumed input column through the *_field config-option surface.
        self._schema_config = cfg.schema_config
        self._reject_explicit_output_field_collision(cfg)
        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "BatchRank",
            adds_fields=True,
        )
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject a numeric batch so the harness reaches a ranked emission (one value: the harness keys inputs by field name)."""
        return [self._augment_invariant_probe_row(probe, field_name=self._value_field, value=value) for value in (2, 2, 2)]

    def _reject_explicit_output_field_collision(self, cfg: BatchRankConfig) -> None:
        """Reject an explicit schema that declares a field the annotations would overwrite."""
        if cfg.schema_config.fields is None:
            return

        declared_schema_fields = {field.name for field in cfg.schema_config.fields}
        collisions = detect_field_collisions(declared_schema_fields, self.declared_output_fields)
        if collisions is None:
            return

        cause = (
            f"BatchRank schema declares field(s) {collisions!r}, but the rank annotations would overwrite them. "
            "Remove the colliding field from the explicit schema or choose a different output_prefix."
        )
        raise PluginConfigError(
            cause,
            cause=cause,
            plugin_class=self.config_model.__name__,
            plugin_name=self.name,
            component_type="transform",
        )

    def _field(self, suffix: str) -> str:
        return f"{self._output_prefix}_{suffix}"

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        """The typed suffix table above under the configured prefix (ADR-050)."""
        return tuple(
            FieldDefinition(self._field(field.name), field.field_type, required=field.required, nullable=field.nullable)
            for field in _RANK_CREATED_SUFFIXES
        )

    def _reject_runtime_output_field_collision(self, rows: list[PipelineRow]) -> None:
        """Fail the batch when a buffered row already carries an annotation field.

        Under an observed upstream only the row data decides whether such a
        field arrives, so this is a row fault routed like any failed batch;
        an explicit schema declaring one is refused at construction.
        """
        for row_index, row in enumerate(rows):
            collisions = detect_field_collisions(set(row.keys()), self.declared_output_fields)
            if collisions is not None:
                raise BatchRowFieldCollisionError(row_index=row_index, collisions=collisions)

    def _rankable_values(self, rows: list[PipelineRow]) -> list[int | float | None]:
        """Each row's rankable value, or None for an unranked row, in buffered order.

        No coercion: a str that looks like a number is not a number, and bool
        is not a number here (``type`` is exact). A present wrong-typed value
        fails the WHOLE batch with a value-free reason.
        """
        values: list[int | float | None] = []
        for row_index, row in enumerate(rows):
            if self._value_field not in row:
                values.append(None)
                continue
            raw_value = row[self._value_field]
            if raw_value is None:
                values.append(None)
                continue
            if type(raw_value) is int:
                values.append(raw_value)
                continue
            if type(raw_value) is float:
                values.append(raw_value if math.isfinite(raw_value) else None)
                continue
            raise BatchRowTypeError(
                field=self._value_field,
                row_index=row_index,
                expected="numeric (int or float)",
                found=type(raw_value).__name__,
            )
        return values

    def _ranks(self, values: list[int | float | None]) -> list[tuple[int | None, float | None]]:
        """Each row's (rank, percentile), in buffered order; (None, None) for an unranked row."""
        ranked = sorted(value for value in values if value is not None)
        distinct = sorted(set(ranked))
        ranked_count = len(ranked)

        ranks: list[tuple[int | None, float | None]] = []
        for value in values:
            if value is None:
                ranks.append((None, None))
                continue
            smaller = bisect_left(ranked, value)
            larger = ranked_count - bisect_right(ranked, value)
            better, worse = (larger, smaller) if self._descending else (smaller, larger)
            if self._dense:
                distinct_smaller = bisect_left(distinct, value)
                distinct_larger = len(distinct) - bisect_right(distinct, value)
                better = distinct_larger if self._descending else distinct_smaller
            ranks.append((1 + better, 100.0 * worse / ranked_count))
        return ranks

    def _output_contract_for(self, rows: list[PipelineRow]) -> SchemaContract:
        """One output contract for every emitted row: the buffered rows' fields plus the annotations.

        ``success_multi`` requires one shared contract instance, so the carried
        fields are the J1 description join of the buffered rows' own contracts
        (``join_batch_contracts``): buffered rows can come from several
        producers (two sources on one queue, row_union branches), and a field
        they type differently is ``object``, nullable is OR and a field some
        row lacks is optional. Every emitted row's contract therefore admits
        the carried values it holds, which ``preserves_input_values`` keeps
        exactly as they arrived. Rows sharing one contract join to that
        contract unchanged, so a single-producer batch keeps its exact types.
        """
        joined = join_batch_contracts(list({id(row.contract): row.contract for row in rows}.values()))
        merged_fields: dict[str, FieldContract] = {field.normalized_name: field for field in joined.fields}

        # Each annotation enters as a placeholder; the ONE stamp rewrites it to
        # the plugin's declaration (ADR-050) and the batch postflight checks
        # every declared concrete type against the emitted values.
        for field_name in sorted(self.declared_output_fields):
            merged_fields[field_name] = FieldContract(
                normalized_name=field_name,
                original_name=field_name,
                python_type=object,
                required=False,
                source="inferred",
            )

        output_contract = SchemaContract(
            mode=joined.mode,
            fields=tuple(merged_fields.values()),
            locked=True,
        )
        return self._align_output_contract(self._apply_declared_output_field_contracts(output_contract))

    @staticmethod
    def _error_for_batch_too_large(*, batch_size: int) -> TransformResult:
        reason: TransformErrorReason = {
            "reason": "validation_failed",
            "cause": "batch_too_large",
            "batch_size": batch_size,
            "expected": f"at most {_MAX_BATCH_ROWS} rows",
        }
        return TransformResult.error(reason, retryable=False)

    def process(  # type: ignore[override] # Batch signature: list[PipelineRow] instead of PipelineRow
        self, rows: list[PipelineRow], ctx: TransformContext
    ) -> TransformResult:
        """Emit every buffered row, in order, with its rank annotations (always a success_multi)."""
        if not rows:
            return TransformResult.error({"reason": "empty_batch"}, retryable=False)
        if len(rows) > _MAX_BATCH_ROWS:
            return self._error_for_batch_too_large(batch_size=len(rows))

        try:
            self._reject_runtime_output_field_collision(rows)
            values = self._rankable_values(rows)
        except (BatchRowFieldCollisionError, BatchRowTypeError) as exc:
            # The batch records that it failed and WHY — which row, which
            # field(s) — never a row value. The caller owns disposition: an
            # aggregation's on_error receives every buffered row, a collector
            # fails the group.
            return TransformResult.error(exc.as_reason(), retryable=False)

        output_contract = self._output_contract_for(rows)
        ranked_count = sum(1 for value in values if value is not None)
        emitted = [
            PipelineRow(
                {
                    **row.to_dict(),
                    self._field("rank"): rank,
                    self._field("percentile"): percentile,
                    self._field("ranked_count"): ranked_count,
                    self._field("batch_size"): len(rows),
                },
                output_contract,
            )
            for row, (rank, percentile) in zip(rows, self._ranks(values), strict=True)
        ]
        # success_multi even for a one-row batch: passthrough carries only a
        # success_multi of one row per buffered row.
        return TransformResult.success_multi(
            emitted,
            success_reason={"action": "processed", "fields_added": sorted(self.declared_output_fields)},
        )

    def close(self) -> None:
        """No resources to release."""
        pass
