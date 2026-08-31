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
import math
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
        # first row, and so duplicate keys are refused before anything runs. The
        # declared-type check must live here, not in __init__, so pre-validation
        # and the engine agree on rejection (test_validation_path_agreement).
        index = build_reference_index(self)
        _validate_declared_output_types(self, _derive_joined_field_types(self, index))
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
        # Strip a leading BOM here rather than in the loader: the table arrives as
        # text from a file on the CLI and from blob substitution on the web, and
        # only this point sees both. An Excel-exported CSV keeps the BOM inside the
        # first header name, so reference_key_name would "not exist" against a
        # column whose difference from it is invisible in a terminal.
        stream = io.StringIO(cfg.reference_content.lstrip("\ufeff"), newline="")
        try:
            reader = csv.DictReader(stream, strict=True)
            fieldnames = reader.fieldnames
            # CSV headers are used VERBATIM, not normalized like the csv source:
            # output expressions address them by name, so silently rewriting
            # "Product SKU" to "product_sku" would break an authored path.
            if not fieldnames:
                raise ReferenceTableError("reference table is empty: no CSV header row")
            csv_entries: list[Mapping[str, object]] = []
            for position, record in enumerate(reader):
                # DictReader pads a short row with restval and buckets surplus
                # cells under restkey. Both defaults are None, which would enter
                # the index as an ordinary value and read back as a resolved
                # null — indistinguishable from a JSON null and invisible to
                # on_miss. Row arity is a table defect; refuse it at load.
                if None in record:
                    raise ReferenceTableError(
                        f"reference table data row {position + 1} has more cells than the "
                        f"{len(fieldnames)}-column header declares; {record[None]!r} belongs to no column."
                    )
                # A cell that is present but empty reads as "", so None here can
                # only be restval — i.e. the row ran out of cells.
                short = sorted(name for name, value in record.items() if value is None)
                if short:
                    raise ReferenceTableError(
                        f"reference table data row {position + 1} has no cell for {short}. "
                        "A short row would join as a null value rather than a miss, so on_miss "
                        "could not see it; pad the row (an empty cell is fine) or fix the header."
                    )
                csv_entries.append(dict(record))
        except csv.Error as exc:
            raise ReferenceTableError(f"reference table is not valid CSV: {exc}") from exc
        return csv_entries

    try:
        loaded = json.loads(cfg.reference_content)
    except json.JSONDecodeError as exc:
        raise ReferenceTableError(f"reference table is not valid JSON: {exc}") from exc
    # ``json.loads`` yields exact builtins (never a frozen proxy or tuple), so
    # the exact-type test is the honest discriminator here.
    if type(loaded) is not list:
        raise ReferenceTableError(
            f"reference table must be a JSON array of objects, got {type(loaded).__name__}. "
            "A key-to-entry object is not accepted: reference_key_name would be meaningless in that shape."
        )
    entries: list[Mapping[str, object]] = []
    for index, entry in enumerate(loaded):
        if type(entry) is not dict:
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
    if not entries:
        raise ReferenceTableError(
            "reference table has no entries: every row would miss the join. "
            "A CSV header with no data rows, or a JSON [], is an empty container rather than "
            "sparse data — supply the table, or remove the reference_join node."
        )
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
            # ExpressionEvaluationError carries two different facts. Chained from
            # KeyError/IndexError it means the PATH does not fit THIS entry — a
            # sparse table is legitimate — so that becomes _UNRESOLVED and is
            # governed by on_miss. Chained from anything else (ZeroDivisionError,
            # ValueError, a TypeError out of a call or comparison) the expression is
            # BROKEN for this entry, and swallowing it would hide an author error
            # behind a miss that on_miss cannot tell apart from sparseness.
            # KeyError/TypeError are deliberately NOT caught: expression_parser
            # re-raises those as evaluator bugs that must crash through.
            try:
                values[field_name] = parser.evaluate({REFERENCE_ENTRY_NAME: entry})
            except ExpressionEvaluationError as exc:
                if not isinstance(exc.__cause__, KeyError | IndexError):
                    raise ReferenceTableError(
                        f"output field {field_name!r} failed to evaluate against reference entry "
                        f"{key!r} (position {position}): {exc}. That is a broken expression rather "
                        "than a sparse entry, so it is refused at load instead of becoming a miss."
                    ) from exc
                values[field_name] = _UNRESOLVED
            else:
                resolved_count[field_name] += 1
        resolved[key] = values

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
    if type(value) is str:
        return value
    if type(value) is bool:
        # Exact-type tests keep bool and int apart without relying on arm
        # order: "true" is the honest spelling, not "1".
        return "true" if value else "false"
    if type(value) is float:
        if not math.isfinite(value):
            raise ReferenceTableError(f"reference key must be a finite number, got {value!r}")
        # An upstream numeric transform or a JSON source can carry 42.0 where the
        # table holds 42. Spelling those differently would silently miss the join,
        # so an integral float takes the integer spelling.
        return str(int(value)) if value.is_integer() else repr(value)
    if type(value) is int:
        return str(value)
    raise ReferenceTableError(f"reference key must be a string or number, got {type(value).__name__}")


#: Exact-type map from a resolved reference value to a declarable field type.
#: An exact test keeps bool apart from int; anything unmapped (a dict or list a
#: JSON table may hold) declares ``any``.
_VALUE_FIELD_TYPES: dict[type, Literal["str", "int", "float", "bool"]] = {str: "str", bool: "bool", int: "int", float: "float"}


def _derive_joined_field_types(cfg: ReferenceJoinConfig, index: ReferenceIndex) -> dict[str, FieldDefinition]:
    """Derive each joined field's declarable type from the resolved index.

    Under ``on_miss: fail`` the emitted set is CLOSED — a row either takes a
    value already resolved in the index or fails — so the type derives from
    exactly those values (a csv table therefore derives str unless an
    expression converts: csv.DictReader yields str for every cell). Closing it
    means reading it at ENTRY granularity, not field granularity: ``process``
    fails the whole row when any one output field is unresolved, so an entry
    that leaves one field unresolved emits nothing for the others either, and
    a None it happens to hold is not a value this join can write.
    ``default`` adds the configured default to the set, because a key miss is
    always possible, and every entry can emit there — an unresolved field takes
    the default rather than failing the row. ``null`` abstains outright: any
    row may miss and take a None. Unification is conservative — mixed concrete
    types declare ``any`` (an honest abstention), never a guess; a None in the
    set makes the field nullable rather than widening its type.
    """
    emitting = list(index.entries.values())
    if cfg.on_miss == "fail":
        emitting = [entry_values for entry_values in emitting if all(entry_values[name] is not _UNRESOLVED for name in cfg.output)]
    derived: dict[str, FieldDefinition] = {}
    for name in sorted(cfg.output):
        if cfg.on_miss == "null":
            derived[name] = FieldDefinition(name=name, field_type="any", required=True, nullable=True)
            continue
        values = [entry_values[name] for entry_values in emitting if entry_values[name] is not _UNRESOLVED]
        if cfg.on_miss == "default":
            values.append(cfg.default_values[name])
        types: set[Literal["str", "int", "float", "bool", "any"]] = {
            _VALUE_FIELD_TYPES[type(value)] if type(value) in _VALUE_FIELD_TYPES else "any" for value in values if value is not None
        }
        field_type: Literal["str", "int", "float", "bool", "any"] = next(iter(types)) if len(types) == 1 else "any"
        derived[name] = FieldDefinition(
            name=name,
            field_type=field_type,
            required=True,
            nullable=any(value is None for value in values),
        )
    return derived


def _validate_declared_output_types(cfg: ReferenceJoinConfig, derived: Mapping[str, FieldDefinition]) -> None:
    """Refuse an authored joined-field declaration the resolved table proves wrong.

    A compatible declaration is honored, never clobbered; either side saying
    ``any`` is an abstention and cannot refute the other. What IS refutable is
    refused here at config load — the alternative was silently overwriting the
    author, which forced a TRUE declaration wrong and steered the correction
    downstream as type erasure (elspeth-cd5cb844bc).

    ``required`` is refused rather than honored for a different reason: every
    joined field is named in the output ``guaranteed_fields``, and
    ``SchemaConfig.from_dict`` refuses a guarantee that is optional. Honoring
    ``required: false`` would mint a config the authoring seam rejects, so the
    contradiction is reported here instead of being rewritten in silence.
    """
    for declared in cfg.schema_config.fields or ():
        if declared.name not in derived:
            continue
        derived_field = derived[declared.name]
        if "any" not in (declared.field_type, derived_field.field_type) and declared.field_type != derived_field.field_type:
            raise ValueError(
                f"schema declares {declared.name!r} as {declared.field_type}, but the resolved reference table "
                f"can only emit {derived_field.field_type} for it. Fix the declaration or the table; a declared "
                "type is honored, never silently overwritten."
            )
        if derived_field.nullable and not declared.nullable:
            reason = (
                "on_miss is 'null', so any row that misses takes a None"
                if cfg.on_miss == "null"
                else "the resolved reference table or default_values holds a None this join will emit"
            )
            raise ValueError(
                f"schema declares {declared.name!r} as never null, but {reason}. "
                f"Declare the field nullable — '{declared.name}: {declared.field_type}?' — or remove the null."
            )
        if not declared.required:
            raise ValueError(
                f"schema declares {declared.name!r} optional, but reference_join creates it and guarantees it on "
                "every row it emits, and a guarantee cannot be optional. Declare it required, or drop it from "
                "'fields' and let the join declare it."
            )


def _reference_join_added_output_fields(cfg: ReferenceJoinConfig, index: ReferenceIndex) -> tuple[FieldDefinition, ...]:
    """One definition per joined field, always required — the join writes every
    output field on every row it emits.

    The author's own declaration is taken WHOLE when one exists, never merged
    with the derivation: ``_validate_declared_output_types`` has already refused
    every part of it the resolved table refutes, so what is left is true and
    rewriting any of it here would be the silent overwrite this change removes.
    A field the author did not declare takes the derived definition.
    """
    derived = _derive_joined_field_types(cfg, index)
    declared_by_name = {field.name: field for field in cfg.schema_config.fields or ()}
    return tuple(declared_by_name[name] if name in declared_by_name else derived[name] for name in sorted(cfg.output))


def _build_reference_join_output_schema_config(cfg: ReferenceJoinConfig, index: ReferenceIndex) -> SchemaConfig:
    schema_config = cfg.schema_config
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _reference_join_added_output_fields(cfg, index)
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
    source_file_hash: str | None = "sha256:89af64c51dc15b48"
    config_model = ReferenceJoinConfig
    passes_through_input = True
    usage_when_to_use: str = (
        "Use when a row carries a business key and the descriptive values for that key live in a small "
        "reference table you can ship alongside the pipeline, such as a product or code list."
    )
    usage_when_not_to_use: str = (
        "Not a way to read a data file at runtime and not a second source: the table is fixed at config "
        "time. Use a source for pipeline input, and blob_csv_expand when the payload should become rows. "
        "Not for a ragged or empty table either: the whole table is resolved at config load, so a short "
        "row, a stray extra cell, or a header with no data rows is refused there rather than at runtime."
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
        # Rendered once: the loader writes reference_source only on the file path,
        # and a miss is the one diagnostic where knowing WHICH table was searched
        # is what tells an author whether the key or the file is wrong.
        self._reference_origin = f" in {cfg.reference_source}" if cfg.reference_source else ""
        self._on_miss = cfg.on_miss
        self._default_values = dict(cfg.default_values)
        self._output_field_names = tuple(sorted(cfg.output))
        self._index = build_reference_index(cfg)

        self.declared_output_fields = frozenset(cfg.output)

        self.input_schema = create_schema_from_config(cfg.schema_config, "ReferenceJoinInput", allow_coercion=False)
        self._output_schema_config = _build_reference_join_output_schema_config(cfg, self._index)
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
                    "In the composer there is no filesystem: create_blob with the table bytes, then "
                    "wire_blob_inline_ref at field_path 'node:<node_id>.options.reference_content'. "
                    "Pasting a table as a literal option value hits the inline byte cap.",
                    "Output expressions see ONLY the matched entry as 'ref'. row[...] is not in scope here and is rejected "
                    "at config load, and a bare column name is not an expression — write ref['description'].",
                    "reference_key_name names a column of the reference table; key_field names the column on the arriving row.",
                    "The table must be rectangular and non-empty: every row needs a cell per header column "
                    "(empty counts, missing does not), and a header with no data rows is refused. These fire "
                    "at config load, so a blob you cannot inspect fails the run.",
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

        entry = self._index.entries[key] if key in self._index.entries else None
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
                            f"no reference entry for {key!r}{self._reference_origin}"
                            if entry is None
                            else f"reference entry {key!r}{self._reference_origin} did not resolve {missed}"
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
