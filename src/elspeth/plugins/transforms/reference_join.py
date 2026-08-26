"""Enrich a row from a keyed reference table bound as configuration content.

The reference table arrives as TEXT in ``reference_content``, never as a path
this transform opens. On the CLI, ``reference_file`` is materialized into
``reference_content`` by the settings loader
(:data:`elspeth.core.template_materialization.FILE_BACKED_TEMPLATE_OPTION_REGISTRY`);
on the web, the same option is filled by ``inline_content`` blob substitution.
Both deliver a ``str``, which is why the option is registered ``content_kind
="text"`` and the parsing lives here rather than in the loader.

Because the table is IN THE CONFIG, it flows into node identity and the
topology hash, so editing the reference file between a run and its resume
refuses the resume. That is the whole reason this transform does not read a
file at runtime, and it is why ``determinism`` is DETERMINISTIC rather than
IO_READ.
"""

from __future__ import annotations

import copy
import csv
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig, declare_missing_guaranteed_fields
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.expression_parser import (
    ExpressionEvaluationError,
    ExpressionParser,
    ExpressionSecurityError,
    ExpressionSyntaxError,
)
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

#: The single name an ``output`` expression may address: the matched entry.
REFERENCE_ENTRY_NAME = "ref"

#: Sentinel for an output path that did not resolve against a matched entry.
#: Distinct from ``None``, which is a legitimate value a reference table may
#: hold (a JSON ``null``), and which ``on_miss: null`` also produces.
_UNRESOLVED = object()


class ReferenceJoinConfig(TransformDataConfig):
    """Configuration for reference_join."""

    reference_content: str = Field(
        ...,
        description=(
            "The reference table as raw text. Written by the settings loader from "
            "'reference_file' on the CLI, or by inline_content blob substitution on the web. "
            "Authoring it directly inline is legal but usually means a file was intended."
        ),
    )
    # DELIBERATELY NOT ``EmittedToOutput``, even though reference VALUES do reach
    # row data. ``_expand_config_templates`` runs AFTER ``_expand_env_vars``
    # (core/config.py:3296-3300), so a ``${VAR}`` inside the table file is never
    # expanded and cannot carry a host secret; on the web path
    # ``load_settings_from_config_dict`` defaults ``expand_env_vars=False`` for the
    # same reason. Marking it would reject a table whose data legitimately
    # contains ``${...}`` with a message about an expansion that cannot happen.
    # ``default_values`` below is the option that IS authored in YAML, IS
    # expanded, and DOES land in row data.
    reference_source: str | None = Field(
        default=None,
        description=(
            "Do not set this. The settings loader writes it when it expands 'reference_file', "
            "and it is used only in diagnostics; nothing resolves or reads the path."
        ),
    )
    reference_format: Literal["csv", "json"] = Field(
        ...,
        description="How to parse reference_content. Never inferred — a blob carries no reliable type.",
    )
    key_field: str = Field(..., description="Input field whose value is matched against the reference table key.")
    reference_key_name: str = Field(
        ...,
        description=(
            "Name of the key WITHIN each reference entry (a CSV header, or a JSON object member). "
            "Deliberately not named '*_field': it names a column of the reference table, not of the row."
        ),
    )
    output: dict[str, str] = Field(
        ...,
        description=(
            "Map of output field name to an expression over the matched entry, bound as 'ref' "
            "(for example \"ref['description']\" or \"ref['tax']['rate']\")."
        ),
    )
    on_miss: Literal["fail", "null", "default"] = Field(
        default="fail",
        description=(
            "What to do when the row key is absent from the table, or an output expression does not "
            "resolve against the matched entry: fail the row, write null, or write default_values."
        ),
    )
    default_values: Annotated[
        dict[str, Any],
        EmittedToOutput("reference_join writes these values directly into row data whenever a lookup misses"),
    ] = Field(default_factory=dict, description="Per-output-field fallback values used when on_miss is 'default'.")

    @field_validator("key_field", "reference_key_name")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_join(self) -> ReferenceJoinConfig:
        from elspeth.contracts.identifiers import validate_field_names

        if not self.output:
            raise ValueError("output must name at least one field to add; an empty map joins nothing")
        validate_field_names(list(self.output), "output")

        # The live invariant gate mutates key_field to name a created field and
        # requires the config to be REFUSED naming that option
        # (tests/invariants/test_input_options_do_not_name_created_fields.py).
        if self.key_field in self.output:
            raise ValueError(
                f"key_field {self.key_field!r} also appears in output, so this transform would overwrite the column "
                "it reads. Repoint key_field at the arriving column, or rename the output field."
            )

        unknown_defaults = sorted(set(self.default_values) - set(self.output))
        if unknown_defaults:
            raise ValueError(
                f"default_values names {unknown_defaults} which are not output fields {sorted(self.output)}. "
                "An unmatched entry would silently do nothing."
            )
        if self.on_miss == "default":
            missing_defaults = sorted(set(self.output) - set(self.default_values))
            if missing_defaults:
                raise ValueError(f"on_miss is 'default' but default_values has no entry for {missing_defaults}")

        # Parse here so a malformed table fails at config load rather than on the
        # first row, and so duplicate keys are refused before anything runs.
        build_reference_index(self)
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.key_field})


class ReferenceTableError(ValueError):
    """The reference table could not be parsed, or is not usable as a table."""


@dataclass(frozen=True, slots=True)
class ReferenceIndex:
    """A reference table resolved down to exactly what row processing needs.

    ``entries`` maps a stringified key to ``{output_field: value}``, where a
    value may be :data:`_UNRESOLVED`. Every expression has already been
    evaluated, so ``process`` is a dict lookup and evaluates nothing.
    """

    entries: Mapping[str, Mapping[str, Any]]


def _parse_reference_entries(cfg: ReferenceJoinConfig) -> list[Mapping[str, object]]:
    """Parse ``reference_content`` into a list of entries, one per table row."""
    if cfg.reference_format == "csv":
        stream = io.StringIO(cfg.reference_content, newline="")
        try:
            reader = csv.DictReader(stream, strict=True)
            fieldnames = reader.fieldnames
            # CSV headers are used VERBATIM, not normalized like the csv source:
            # output expressions address them by name, so silently rewriting
            # "Product SKU" to "product_sku" would break an authored path.
            if not fieldnames:
                raise ReferenceTableError("reference table is empty: no CSV header row")
            csv_entries: list[Mapping[str, object]] = [dict(record) for record in reader]
        except csv.Error as exc:
            raise ReferenceTableError(f"reference table is not valid CSV: {exc}") from exc
        return csv_entries

    try:
        loaded = json.loads(cfg.reference_content)
    except json.JSONDecodeError as exc:
        raise ReferenceTableError(f"reference table is not valid JSON: {exc}") from exc
    if not isinstance(loaded, list):
        raise ReferenceTableError(
            f"reference table must be a JSON array of objects, got {type(loaded).__name__}. "
            "A key-to-entry object is not accepted: reference_key_name would be meaningless in that shape."
        )
    entries: list[Mapping[str, object]] = []
    for index, entry in enumerate(loaded):
        if not isinstance(entry, dict):
            raise ReferenceTableError(f"reference table entry {index} is a {type(entry).__name__}, expected an object")
        entries.append(entry)
    return entries


def _compile_output_expressions(cfg: ReferenceJoinConfig) -> dict[str, ExpressionParser]:
    """Compile every ``output`` expression, naming the field that failed."""
    compiled: dict[str, ExpressionParser] = {}
    for field_name, expression in cfg.output.items():
        try:
            compiled[field_name] = ExpressionParser(expression, allowed_names=[REFERENCE_ENTRY_NAME])
        except (ExpressionSyntaxError, ExpressionSecurityError) as exc:
            raise ReferenceTableError(
                f"output field {field_name!r} has an invalid expression {expression!r}: {exc}. "
                f"Address the matched entry as {REFERENCE_ENTRY_NAME!r}, e.g. \"{REFERENCE_ENTRY_NAME}['description']\"."
            ) from exc
    return compiled


def build_reference_index(cfg: ReferenceJoinConfig) -> ReferenceIndex:
    """Resolve the whole table once: parse, key, and evaluate every output path.

    Called from config validation so that every failure this can detect —
    unparseable content, a missing key column, duplicate keys, an expression
    that resolves for no entry at all — is reported at load rather than on some
    row deep into a run.
    """
    entries = _parse_reference_entries(cfg)
    compiled = _compile_output_expressions(cfg)

    resolved: dict[str, dict[str, Any]] = {}
    first_position: dict[str, int] = {}
    resolved_count = dict.fromkeys(cfg.output, 0)

    for position, entry in enumerate(entries):
        if cfg.reference_key_name not in entry:
            available = sorted(entry)
            raise ReferenceTableError(f"reference entry {position} has no {cfg.reference_key_name!r} key. Available keys: {available}")
        key = _coerce_key(entry[cfg.reference_key_name])
        if key in resolved:
            raise ReferenceTableError(
                f"duplicate reference key {key!r} at entries {first_position[key]} and {position}. "
                "Which entry won would depend on file ordering, so the run would not be reproducible; "
                "remove or merge the duplicate."
            )
        first_position[key] = position

        values: dict[str, Any] = {}
        for field_name, parser in compiled.items():
            # KeyError/IndexError/TypeError here mean the PATH does not fit THIS
            # entry — a sparse table is legitimate — so they become _UNRESOLVED
            # and are governed by on_miss, not by crashing the load.
            try:
                values[field_name] = parser.evaluate({REFERENCE_ENTRY_NAME: entry})
            except (KeyError, IndexError, TypeError, ExpressionEvaluationError):
                values[field_name] = _UNRESOLVED
            else:
                resolved_count[field_name] += 1
        resolved[key] = values

    if entries:
        never_resolved = sorted(name for name, count in resolved_count.items() if count == 0)
        if never_resolved:
            raise ReferenceTableError(
                f"output fields {never_resolved} resolved against none of the {len(entries)} reference entries. "
                "A path that fits no entry is a configuration error, not sparse data; check the expression "
                "against the table's actual keys."
            )

    return ReferenceIndex(entries=resolved)


def _coerce_key(value: object) -> str:
    """Reference keys are compared as strings, because CSV has no other type.

    A JSON table may hold an int key while the row carries "42" from a CSV
    source; matching on the string spelling is the only rule that behaves the
    same for both formats.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # Guarded before int: bool is an int subclass and "True" is the honest
        # spelling, not "1".
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    raise ReferenceTableError(f"reference key must be a string or number, got {type(value).__name__}")


def _reference_join_added_output_fields(cfg: ReferenceJoinConfig) -> tuple[FieldDefinition, ...]:
    # Declared ``any`` following value_transform, which guarantees.py:610-616
    # names as the precedent for a transform that writes fields whose type it
    # cannot know statically. Nullable because a table may hold a JSON null and
    # because on_miss: null writes one.
    return tuple(FieldDefinition(name=name, field_type="any", required=True, nullable=True) for name in sorted(cfg.output))


def _build_reference_join_output_schema_config(cfg: ReferenceJoinConfig) -> SchemaConfig:
    schema_config = cfg.schema_config
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _reference_join_added_output_fields(cfg)
    field_by_name.update((field.name, field) for field in added_fields)

    base_guaranteed = set(schema_config.guaranteed_fields or ())
    output_guaranteed = tuple(sorted(base_guaranteed | {field.name for field in added_fields}))

    return SchemaConfig(
        mode=schema_config.mode if schema_config.fields is not None else "flexible",
        fields=declare_missing_guaranteed_fields(tuple(field_by_name.values()), output_guaranteed),
        guaranteed_fields=output_guaranteed,
        audit_fields=schema_config.audit_fields,
        required_fields=schema_config.required_fields,
    )


class ReferenceJoin(BaseTransform):
    """Match a row field against a reference table and add named fields to the row."""

    # key_field is the INPUT column this transform READS; the ``output`` map
    # names created fields but is not a column-naming option by
    # ``is_column_naming_config_option``, so it needs no classification here.
    output_naming_config_keys: frozenset[str] = frozenset()
    name = "reference_join"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:6e05e4eb6cee767c"
    config_model = ReferenceJoinConfig
    passes_through_input = True
    usage_when_to_use: str = (
        "Use when a row carries a business key and the descriptive values for that key live in a small "
        "reference table you can ship alongside the pipeline, such as a product or code list."
    )
    usage_when_not_to_use: str = (
        "Not a way to read a data file at runtime and not a second source: the table is fixed at config "
        "time. Use a source for pipeline input, and blob_csv_expand when the payload should become rows."
    )
    example_use: str = """transform:
  plugin: reference_join
  options:
    reference_content: "sku,description\\nhats,A fine hat\\n"
    reference_format: csv
    key_field: product
    reference_key_name: sku
    output:
      product_description: "ref['description']"
    on_miss: fail
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("join", "lookup", "enrichment", "reference-data")

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "schema": {"mode": "observed"},
            "reference_content": "sku,description\nprobe,probe value\n",
            "reference_format": "csv",
            "key_field": "reference_join_probe_key",
            "reference_key_name": "sku",
            "output": {"reference_join_probe_added_1": "ref['description']"},
        }

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        cfg = ReferenceJoinConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        self._key_field = cfg.key_field
        self._reference_key = cfg.reference_key_name
        self._reference_source = cfg.reference_source
        self._on_miss = cfg.on_miss
        self._default_values = dict(cfg.default_values)
        self._output_field_names = tuple(sorted(cfg.output))
        self._index = build_reference_index(cfg)

        self.declared_output_fields = frozenset(cfg.output)

        self.input_schema = create_schema_from_config(cfg.schema_config, "ReferenceJoinInput", allow_coercion=False)
        self._output_schema_config = _build_reference_join_output_schema_config(cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "ReferenceJoinOutput", allow_coercion=False)

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Add fields to a row by matching one of its values against a key in a fixed reference table.",
                composer_hints=(
                    "The reference table is configuration, not a source: it is fixed when the run starts and is not fetched.",
                    "On the CLI use reference_file: <name>.csv beside settings.yaml; the loader reads it into reference_content.",
                    "In the composer there is no filesystem: create_blob with the table bytes, then wire_blob_inline_ref "
                    "with field_path 'node:<node_id>.options.reference_content'. Pasting a whole table as a literal option "
                    "value works for a handful of rows but bloats the composition and hits the inline byte cap.",
                    "Output expressions see ONLY the matched entry as 'ref'. row[...] is not in scope here and is rejected "
                    "at config load, and a bare column name is not an expression — write ref['description'].",
                    "Address the matched entry as 'ref' in every output expression, e.g. ref['description'].",
                    "reference_key_name names a column of the reference table; key_field names the column on the arriving row.",
                    "on_miss defaults to fail, because an unenriched row reaching a sink looks identical to an enriched one.",
                ),
            )
        return None

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Give the probe row a key that the probe table actually contains."""
        return [self._augment_invariant_probe_row(probe, field_name=self._key_field, value="probe")]

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        del ctx
        if self._key_field not in row:
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": self._key_field,
                    "error": f"row has no {self._key_field!r} field to join on",
                },
                retryable=False,
            )

        raw_key = row[self._key_field]
        try:
            key = _coerce_key(raw_key)
        except ReferenceTableError:
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": self._key_field,
                    "error": f"join key must be a string or number, got {type(raw_key).__name__}",
                },
                retryable=False,
            )

        entry = self._index.entries.get(key)
        values: dict[str, Any] = {}
        missed: list[str] = []
        for field_name in self._output_field_names:
            resolved = _UNRESOLVED if entry is None else entry[field_name]
            if resolved is _UNRESOLVED:
                missed.append(field_name)
                continue
            values[field_name] = resolved

        if missed:
            if self._on_miss == "fail":
                return TransformResult.error(
                    {
                        "reason": "reference_miss",
                        "field": self._key_field,
                        "reference_key_value": key,
                        "unresolved_fields": missed,
                        "error": (
                            f"no reference entry for {key!r}" if entry is None else f"reference entry {key!r} did not resolve {missed}"
                        ),
                    },
                    retryable=False,
                )
            for field_name in missed:
                values[field_name] = self._default_values[field_name] if self._on_miss == "default" else None

        output = copy.deepcopy(row.to_dict())
        output.update(values)

        output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output)
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "enriched",
                "fields_added": sorted(values),
                "metadata": {
                    "matched": entry is not None,
                    "unresolved_fields": missed,
                },
            },
        )

    def close(self) -> None:
        """No resources to release: the reference table is configuration."""
