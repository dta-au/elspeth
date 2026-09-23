"""Batch replicate transform plugin.

Demonstrates output_mode: transform deaggregation by replicating rows.
Each input row is replicated based on a 'copies' field, producing more
output rows than input rows (N inputs -> M outputs where M >= N).

IMPORTANT: This transform uses is_batch_aware=True, meaning the engine
will buffer rows and call process() with a list when the trigger fires.

For output_mode: transform, the engine creates NEW tokens for each output
row, with parent linkage to track deaggregation lineage.
"""

import copy
from typing import Any

from pydantic import Field, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.errors import TransformSuccessReason
from elspeth.contracts.field_collision import detect_field_collisions
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import PluginConfigError, TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms._batch_row_types import BatchRowFieldCollisionError, BatchRowTypeError

# The one field this transform creates. Named once so the emission site, the
# declared output contract, and the config-time guard cannot drift apart into
# disagreeing on what ``copies_field`` may not point at.
COPY_INDEX_FIELD = "copy_index"


class BatchReplicateConfig(TransformDataConfig):
    """Configuration for batch replicate transform.

    Requires a field that specifies how many copies of each row to produce.
    """

    copies_field: str = Field(
        default="copies",
        description="Name of the field containing the number of copies to make",
        min_length=1,
    )
    default_copies: int = Field(
        default=1,
        ge=1,
        le=10000,
        description="Default number of copies when copies_field is absent",
    )
    max_copies: int = Field(
        default=10000,
        ge=1,
        le=10000,
        description="Upper bound on copies per row to prevent unbounded replication",
    )
    include_copy_index: bool = Field(
        default=True,
        description="Whether to add a 'copy_index' field (0-based) to each output row",
    )

    @model_validator(mode="after")
    def _default_within_max(self) -> "BatchReplicateConfig":
        if self.default_copies > self.max_copies:
            msg = f"default_copies ({self.default_copies}) exceeds max_copies ({self.max_copies})"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _copies_field_is_not_the_created_field(self) -> "BatchReplicateConfig":
        """Reject a ``copies_field`` naming the column this transform creates.

        ``copies_field`` names a column ``process`` READS on every buffered
        row, and ``copy_index`` is written onto each emitted copy of that same
        row (``passes_through_input``). Pointing one at the other makes the
        transform read the column it is about to overwrite, and because
        ``consumed_input_fields`` then covers ``copy_index``, nothing demotes
        it: it stays required on the input schema and every row that does not
        already carry it is rejected for missing the field the transform exists
        to create (elspeth-d6eeb3a71d).

        Guarded HERE as well as in ``__init__`` so the two validation paths
        agree — pre-validation runs the config model alone, so an
        ``__init__``-only guard would reject on the engine path while
        ``validate_transform_config`` reported the config clean. The created
        set is fully knowable at config time: it is ``copy_index`` exactly when
        ``include_copy_index`` is on, and empty otherwise, so a config that
        disables the index may name it freely.
        """
        if self.include_copy_index and self.copies_field == COPY_INDEX_FIELD:
            msg = f"copies_field {self.copies_field!r} may not name {COPY_INDEX_FIELD!r}, which batch_replicate itself creates; point copies_field at a column that ARRIVES on the row, or set include_copy_index: false"
            raise ValueError(msg)
        return self


class BatchReplicate(BaseTransform):
    """Replicate rows based on a copies field.

    This is a batch-aware transform that demonstrates output_mode: transform
    deaggregation. It receives N input rows and produces M output rows where
    M is the sum of all copies values.

    Example: If input has 3 rows with copies=2,3,1 respectively:
    - Input: 3 rows
    - Output: 6 rows (2 + 3 + 1)
    - Each output row has copy_index showing which copy it is

    Config options:
        schema: Required. Schema for input validation
        copies_field: Field name for copy count (default: "copies")
        default_copies: Fallback if field missing (default: 1)
        include_copy_index: Add copy_index field (default: True)

    Example YAML:
        aggregations:
          - name: replicate_batch
            plugin: batch_replicate
            trigger:
              count: 5
            output_mode: transform  # CRITICAL: Creates new tokens for outputs
            options:
              schema:
                mode: observed
              copies_field: quantity
              default_copies: 1
    """

    name = "batch_replicate"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:7a1cb813e1dfadc0"
    config_model = BatchReplicateConfig
    is_batch_aware = True  # CRITICAL: Engine buffers rows for batch processing
    usage_when_to_use: str = (
        "Use for bounded per-row copy expansion. A missing copies_field uses default_copies, while a valid integer "
        "count controls the emitted copies and optional copy indexes."
    )
    usage_when_not_to_use: str = (
        "Not for random sampling or unbounded fan-out. A present non-integer count (including null) fails the whole "
        "batch with a recorded reason; only integer counts outside 1..max_copies are quarantined."
    )
    example_use: str = """aggregations:
  - name: replicate_rows
    plugin: batch_replicate
    input: counted_rows
    on_success: output
    on_error: discard
    trigger:
      count: 50
    output_mode: transform
    options:
      copies_field: copies
      default_copies: 1
      max_copies: 5
      include_copy_index: true
      schema:
        mode: observed
"""
    capability_tags: tuple[str, ...] = ("batch", "deaggregation", "row-expansion")

    # Every emitted row deep-copies its originating input before adding
    # copy_index. Mixed-validity batches may quarantine inputs, but the
    # pass-through contract applies to emitted rows and batch verification uses
    # the fields shared by every buffered input (ADR-009 Clause 2).
    passes_through_input = True

    # Sound because process() deep-copies each input row and only ADDS
    # copy_index — a row already carrying copy_index fails the whole batch
    # (a field_collision error) rather than being overwritten, so no forwarded
    # value is ever rewritten (elspeth-48aeea6ad9).
    preserves_input_values = True

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Replicates rows within an aggregation batch based on a copy-count field.",
                composer_hints=(
                    "Use batch_replicate under aggregations with output_mode=transform so emitted copies become new tokens.",
                    "copies_field must be an int when present: a row without the field uses default_copies, a present non-int value (null included) fails the whole batch, and integers outside 1..max_copies are quarantined.",
                    "include_copy_index=True adds copy_index and an input row already carrying copy_index fails the whole batch, so avoid input fields with that name or disable it.",
                    "This expands row count and can drop invalid source rows from the successful output.",
                ),
            )
        return None

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the ADR-009 §Clause 4 invariant harness."""
        return {
            "schema": {"mode": "observed"},
            "copies_field": "copies",
            "default_copies": 1,
            "max_copies": 10,
            "include_copy_index": True,
        }

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = BatchReplicateConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        # Declare output fields for centralized collision detection.
        self.declared_output_fields = frozenset([COPY_INDEX_FIELD] if cfg.include_copy_index else [])
        self._copies_field = cfg.copies_field
        self._default_copies = cfg.default_copies
        self._max_copies = cfg.max_copies
        self._include_copy_index = cfg.include_copy_index

        self._schema_config = cfg.schema_config
        self._reject_explicit_copy_index_collision(cfg)

        self.input_schema, self.output_schema = self._create_schemas(
            cfg.schema_config,
            "BatchReplicate",
            adds_fields=True,
        )
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

        # LAST: the created set is READ here, not captured, so this must run
        # after declared_output_fields is populated — an earlier call would see
        # an empty set and pass vacuously.
        #
        # _copies_field_is_not_the_created_field already refuses today's only
        # offending config, so this call cannot currently fire; the pairing is
        # the same one pdf_rasterize and blob_csv_expand carry, and it is what
        # keeps the guard attached to the RUNTIME created set. A created field
        # that is not knowable from the config alone would otherwise be guarded
        # by neither.
        self._reject_input_options_naming_created_fields({"copies_field": cfg.copies_field})

    def _reject_explicit_copy_index_collision(self, cfg: BatchReplicateConfig) -> None:
        """Reject explicit schemas that would always collide with copy_index emission."""
        if not cfg.include_copy_index or cfg.schema_config.fields is None:
            return

        declared_schema_fields = {field.name for field in cfg.schema_config.fields}
        collisions = detect_field_collisions(declared_schema_fields, self.declared_output_fields)
        if collisions is None:
            return

        cause = (
            "BatchReplicate schema declares field(s) "
            f"{collisions!r}, but include_copy_index=True would overwrite them. "
            "Remove the colliding field from the explicit schema or disable include_copy_index."
        )
        raise PluginConfigError(
            cause,
            cause=cause,
            plugin_class=self.config_model.__name__,
            plugin_name=self.name,
            component_type="transform",
        )

    def process(  # type: ignore[override] # Batch signature: list[PipelineRow] instead of PipelineRow
        self, rows: list[PipelineRow], ctx: TransformContext
    ) -> TransformResult:
        """Replicate each row based on its copies field.

        Args:
            rows: List of input rows (batch-aware receives list[PipelineRow])
            ctx: Plugin context

        Returns:
            TransformResult.success_multi() with replicated rows, or a
            batch-level TransformResult.error when a row carries a non-int
            copies value or already carries copy_index
        """
        if not rows:
            # Empty batch is an anomaly — return error, not fabricated data.
            # A synthetic PipelineRow({batch_empty: True}) would flow through
            # the pipeline as real data, corrupting the audit trail.
            return TransformResult.error(
                {"reason": "empty_batch"},
                retryable=False,
            )

        valid_rows: list[dict[str, Any]] = []
        emitted_contracts: list[SchemaContract] = []
        quarantined: list[dict[str, Any]] = []
        quarantined_indices: list[int] = []

        for row_index, row in enumerate(rows):
            # Get copies count - field is optional, type must be correct if present
            if self._copies_field not in row:
                # Field missing - use default, still bounded by max_copies
                copies = min(self._default_copies, self._max_copies)
            else:
                raw_copies = row[self._copies_field]

                # A present copies value must be an int — no coercion: a str
                # that is not a number is not a number. A wrong type (None
                # included: there is no missing-value branch here, the absent
                # FIELD is the missing case) fails the WHOLE batch with a
                # recorded, value-free reason (elspeth-d5034647f0). The caller
                # owns disposition: an aggregation's on_error receives every
                # buffered row, a collector fails the group.
                # `type(x) is int`, not isinstance: bool is an int subclass and
                # True/False must not pass as 1/0 copies.
                if type(raw_copies) is not int:
                    return TransformResult.error(
                        BatchRowTypeError(
                            field=self._copies_field,
                            row_index=row_index,
                            expected="int",
                            found=type(raw_copies).__name__,
                        ).as_reason(),
                        retryable=False,
                    )

                # Value-level validation: copies must be >= 1 and <= max_copies
                # Tier 2 operation safety - type is correct but value is unsafe
                if raw_copies < 1 or raw_copies > self._max_copies:
                    # Record the row INDEX and field NAME for traceability, never
                    # a row value: neither row.to_dict() nor the copies count
                    # itself may leak into the audit success_reason (prior
                    # row-content-leak bug family).
                    quarantined.append(
                        {
                            "reason": "invalid_copies",
                            "field": self._copies_field,
                            "row_index": row_index,
                        }
                    )
                    quarantined_indices.append(row_index)
                    continue

                copies = raw_copies

            if self.declared_output_fields:
                collisions = detect_field_collisions(set(row.keys()), self.declared_output_fields)
                if collisions is not None:
                    # Under an observed upstream only the row data decides
                    # whether copy_index arrives, so this is a row fault: the
                    # whole batch fails with a value-free reason (ruling on
                    # elspeth-d90495084c). The certain case, an explicit schema
                    # declaring copy_index, is refused at construction.
                    return TransformResult.error(
                        BatchRowFieldCollisionError(row_index=row_index, collisions=collisions).as_reason(),
                        retryable=False,
                    )

            # Create copies of this row
            emitted_contracts.append(row.contract)
            for copy_idx in range(copies):
                # Deep copy ensures each replica is fully independent —
                # shallow copy would share nested mutable values across copies
                output = copy.deepcopy(row.to_dict())
                if self._include_copy_index:
                    output[COPY_INDEX_FIELD] = copy_idx
                valid_rows.append(output)

        # If ALL rows were quarantined, return error — no valid output to expand
        if not valid_rows:
            return TransformResult.error(
                {
                    "reason": "all_rows_failed",
                    "error": f"All {len(quarantined)} rows quarantined: invalid copies values",
                    "row_errors": [{"row_index": q["row_index"], "reason": q["reason"]} for q in quarantined],
                },
                retryable=False,
            )

        # Build the emitted contract from rows that actually produced output.
        # Quarantined-only fields must not leak into successful child tokens.
        first_contract = rows[0].contract
        for i, row in enumerate(rows[1:], start=1):
            if row.contract.mode != first_contract.mode:
                raise ValueError(
                    f"Heterogeneous contract modes in batch: row 0 has mode "
                    f"'{first_contract.mode}', row {i} has mode '{row.contract.mode}'. "
                    f"All rows in a batch must share the same contract mode."
                )
        merged_fields: dict[str, FieldContract] = {}
        for contract in emitted_contracts:
            for fc in contract.fields:
                if fc.normalized_name not in merged_fields:
                    merged_fields[fc.normalized_name] = fc

        # Add copy_index as a new inferred field if configured
        if self._include_copy_index:
            merged_fields[COPY_INDEX_FIELD] = FieldContract(
                normalized_name=COPY_INDEX_FIELD,
                original_name=COPY_INDEX_FIELD,
                python_type=int,
                required=False,
                source="inferred",
            )

        output_contract = SchemaContract(
            mode=first_contract.mode,
            fields=tuple(merged_fields.values()),
            locked=True,
        )
        output_contract = self._align_output_contract(output_contract)

        success_reason: TransformSuccessReason = {
            "action": "processed",
            "fields_added": [COPY_INDEX_FIELD] if self._include_copy_index else [],
        }
        if quarantined:
            success_reason["metadata"] = {
                "quarantined_count": len(quarantined),
                "quarantined": quarantined,
                "quarantined_indices": quarantined_indices,
            }

        # Return only valid replicated rows — quarantined rows are in success_reason
        return TransformResult.success_multi(
            [PipelineRow(r, output_contract) for r in valid_rows],
            success_reason=success_reason,
        )

    def close(self) -> None:
        """No resources to release."""
        pass
