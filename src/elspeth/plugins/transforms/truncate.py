"""Truncate transform plugin.

Truncates specified string fields to maximum lengths.

Every configured field is a string-typed declared input
(``declared_string_input_fields``), exactly as keyword_filter's and the
guardrails' scan fields are: it must be present and string-valued on every
row, its name is the normalized field name rows carry, and a producer schema
that provably types it non-string is refused at build time.

IMPORTANT: Transforms use allow_coercion=False to catch upstream bugs.
If the source outputs wrong types, the transform crashes immediately.
"""

import copy
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult


class TruncateConfig(TransformDataConfig):
    """Configuration for truncate transform.

    Requires 'schema' in config to define input/output expectations.
    Use 'schema: {mode: observed}' for dynamic field handling.
    """

    fields: dict[str, int] = Field(
        default_factory=dict,
        description="Mapping of string field names (as rows carry them, normalized) to maximum lengths",
    )
    suffix: Annotated[
        str,
        EmittedToOutput("truncate appends this to every truncated value, so it lands in row data and in every downstream artifact"),
    ] = Field(
        default="",
        description="Suffix to append when truncating (e.g., '...'). Counts toward max length.",
    )

    @field_validator("fields")
    @classmethod
    def _validate_field_lengths(cls, v: dict[str, int]) -> dict[str, int]:
        for name, max_len in v.items():
            if not name:
                raise ValueError("field name must not be empty")
            if max_len < 1:
                raise ValueError(f"max_len for field '{name}' must be >= 1, got {max_len}")
        return v

    @model_validator(mode="after")
    def _validate_suffix_fits(self) -> "TruncateConfig":
        suffix_len = len(self.suffix)
        for field_name, max_len in self.fields.items():
            if suffix_len >= max_len:
                raise ValueError(f"suffix length ({suffix_len}) must be less than max_len for field '{field_name}' ({max_len})")
        return self


class Truncate(BaseTransform):
    """Truncate string fields to specified maximum lengths.

    Use cases:
    - Enforce maximum field lengths before database insertion
    - Trim verbose text fields for display
    - Ensure data fits within schema constraints

    Config options:
        schema: Required. Schema for input/output (use {mode: observed} for any fields)
        fields: Dict of field_name -> max_length (e.g., {"title": 100, "description": 500}).
            Each field must be present and string-valued on every row; names are
            the normalized field names rows carry (``notes``, not ``Notes``).
        suffix: String to append when truncating (e.g., "..."). Included in max length.
        on_error: Sink to route errors to (default: None, will quarantine)

    Example config:
        - plugin: truncate
          options:
            schema:
              mode: observed
            fields:
              title: 100
              description: 500
              notes: 1000
            suffix: "..."
    """

    name = "truncate"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:4ae16e87917a4322"
    config_model = TruncateConfig
    usage_when_to_use: str = (
        "Use for a deterministic character-length cap on selected text fields, optionally reserving "
        "part of that limit for a visible truncation suffix."
    )
    usage_when_not_to_use: str = (
        "Not for token-aware model context management or semantic summarization: use provider-aware "
        "input controls or a summarizing transform when character count is not the real limit."
    )
    example_use: str = """transform:
  plugin: truncate
  options:
    fields:
      notes: 80
    suffix: "..."
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("text", "truncation", "length-limit")
    passes_through_input = True
    # ``fields`` maps each input column to its max length: the KEYS are read
    # columns. This only describes the option's SHAPE (a mapping, where
    # keyword_filter's ``fields`` is a list) so the fixed-schema read check
    # and demotion protection see the keys; ``consumed_input_fields`` does not
    # read ``declared_string_input_fields``. It grants no lookup semantics.
    column_keyed_config_keys = frozenset({"fields"})

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = TruncateConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._fields = cfg.fields
        self._suffix = cfg.suffix
        # Every configured field must arrive present and string-valued or
        # process() fails the row closed — declared exactly as the
        # text-scanning siblings declare theirs, so the build refuses a
        # provably non-string producer and the spelling rule governs the names.
        self.declared_string_input_fields = frozenset(cfg.fields)

        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)
        self.input_schema, self.output_schema = self._create_schemas(cfg.schema_config, "Truncate")

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "schema": {"mode": "observed"},
            "fields": {"truncate_probe_1": 5},
        }

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        return [
            self._augment_invariant_probe_row(
                probe,
                field_name="truncate_probe_1",
                value="abcdefg",
            )
        ]

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        """Truncate specified fields in row.

        Args:
            row: Input row data
            ctx: Plugin context

        Returns:
            TransformResult with truncated field values
        """
        output = copy.deepcopy(row.to_dict())
        fields_modified: list[str] = []

        for field_name, max_len in self._fields.items():
            # Configured names are the field names rows carry: read and write
            # the payload key itself, never a header alias resolved through
            # the contract (the spelling rule refuses those before process()).
            if field_name not in output:
                return TransformResult.error(
                    {
                        "reason": "missing_field",
                        "field": field_name,
                    },
                    retryable=False,
                )

            value = output[field_name]

            # Type mismatches are upstream contract bugs; always surface explicitly.
            if type(value) is not str:
                return TransformResult.error(
                    {
                        "reason": "type_mismatch",
                        "field": field_name,
                        "expected": "str",
                        "actual": type(value).__name__,
                    },
                    retryable=False,
                )

            # Truncate if needed
            if len(value) > max_len:
                if self._suffix:
                    # Truncate leaving room for suffix
                    truncate_at = max_len - len(self._suffix)
                    output[field_name] = value[:truncate_at] + self._suffix
                else:
                    output[field_name] = value[:max_len]
                fields_modified.append(field_name)

        # The ONE stamp writes the operator's declared fields onto the emitted
        # contract (ADR-050 Decision 2); a truncated value is still a str.
        output_contract = self._align_output_contract(self._apply_declared_output_field_contracts(row.contract))
        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "transformed",
                "fields_modified": fields_modified,
            },
        )

    def close(self) -> None:
        """No resources to release."""
        pass

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name="truncate",
                issue_code=None,
                summary="Truncate string fields to max_length with optional suffix marker. Non-destructive on shorter values.",
                composer_hints=(
                    "Each fields key must be a string field every row carries, named as rows carry it (normalized, "
                    "e.g. notes not Notes); a missing or non-string field fails the row, so route on_error.",
                    "max_length is character count (Python len()), not bytes. Multi-byte content may exceed byte limits even after truncation.",
                    "suffix (e.g. '…') counts toward max_length; the truncated portion is shorter than max_length when suffix is set.",
                    "Use for display preparation, audit-summary fields, or controlling downstream LLM prompt size — not for security boundaries.",
                ),
            )
        return None
