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
from typing import Annotated, Any, Literal, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.errors import TransformErrorReason
from elspeth.contracts.freeze import freeze_fields
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

#: Upper bound on the materialized row-by-output matrix. The complete matrix
#: is retained for constant-time joins, so bounding each input independently
#: would still permit an unsafe Cartesian product.
MAX_REFERENCE_INDEX_CELLS = 1_000_000

#: Upper bound on canonical bytes retained by the materialized index. Cell
#: count alone does not bound values copied from a wide reference field.
MAX_REFERENCE_INDEX_BYTES = 64 * 1024 * 1024

#: Sentinel for an output path that did not resolve against a matched entry.
#: Distinct from ``None``, which is a legitimate value a reference table may
#: hold (a JSON ``null``), and which ``on_miss: null`` also produces.
_UNRESOLVED = object()


def _bounded_canonical_size(value: object, *, limit: int) -> int:
    """Return canonical-JSON byte size while stopping at ``limit``."""

    def add(total: int, increment: int) -> int:
        total += increment
        if total > limit:
            raise ReferenceTableError(
                f"reference table outputs exceed the {MAX_REFERENCE_INDEX_BYTES}-byte materialized index limit. "
                "Reduce the table, output map, or retained value sizes."
            )
        return total

    def string_size(text: str) -> int:
        size = add(0, 2)
        for character in text:
            codepoint = ord(character)
            if character in ('"', "\\"):
                width = 2
            elif codepoint <= 0x1F:
                width = 6
            elif codepoint <= 0x7F:
                width = 1
            elif codepoint <= 0x7FF:
                width = 2
            elif codepoint <= 0xFFFF:
                width = 3
            else:
                width = 4
            size = add(size, width)
        return size

    if value is None:
        return add(0, 4)
    if type(value) is bool:
        return add(0, 4 if value else 5)
    if type(value) is str:
        return string_size(value)
    if type(value) is int:
        return add(0, len(str(value)))
    if type(value) is float:
        return add(0, 32)
    if isinstance(value, Mapping):
        total = add(0, 2)
        for index, (key, item) in enumerate(value.items()):
            if type(key) is not str:
                raise TypeError("reference output objects must use string keys")
            if index:
                total = add(total, 1)
            total = add(total, string_size(key))
            total = add(total, 1)
            total = add(total, _bounded_canonical_size(item, limit=limit - total))
        return total
    if isinstance(value, (list, tuple)):
        total = add(0, 2)
        for index, item in enumerate(value):
            if index:
                total = add(total, 1)
            total = add(total, _bounded_canonical_size(item, limit=limit - total))
        return total
    raise TypeError(f"reference output value of type {type(value).__name__} is not canonical JSON")


class _SingleReferenceJoinConfig(TransformDataConfig):
    """Configuration for reference_join."""

    reference_content: Annotated[
        str, EmittedToOutput("inline reference text can reach row data after environment expansion", reject_at_validation=False)
    ] = Field(
        ...,
        description=(
            "The reference table as raw text. Written by the settings loader from "
            "'reference_file' on the CLI, or by inline_content blob substitution on the web. "
            "Authoring it directly inline is legal but usually means a file was intended."
        ),
    )
    # Table text is guarded only BEFORE environment expansion. Materialized
    # files arrive afterwards and retain literal ${...} values; direct plugin
    # validation also treats content as already materialized configuration.
    reference_source: str | None = Field(
        default=None,
        description=(
            "Do not set this. The settings loader writes it when it expands 'reference_file', "
            "and it is used only in diagnostics; nothing resolves or reads the path."
        ),
    )
    reference_format: Literal["csv", "json"] = Field(
        ...,
        description=(
            "How to parse reference_content. Never inferred — a blob carries no reliable type. "
            "CSV cells remain strings, including numeric-looking cells; JSON numbers retain numeric types."
        ),
    )
    key_field: str = Field(..., description="Input field whose value is matched against the reference table key.")
    reference_key_name: str = Field(
        ...,
        description=(
            "Name of the key WITHIN each reference entry (a CSV header, or a JSON object member). "
            "Deliberately not named '*_field': it names a column of the reference table, not of the row."
        ),
    )
    output: Annotated[dict[str, str], EmittedToOutput("output names and expression literals reach row data")] = Field(
        ...,
        description=(
            "Map of output field name to an expression over the matched entry, bound as 'ref' "
            "(for example \"ref['description']\" or \"ref['tax']['rate']\"). "
            "A stored result cannot be a set because it has no canonical order; build a list instead."
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

    _validated_index: ReferenceIndex = PrivateAttr()

    @field_validator("key_field", "reference_key_name")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_join(self, info: ValidationInfo) -> _SingleReferenceJoinConfig:
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
        entries = info.context["entries"] if info.context is not None else None
        index = build_reference_index(
            self,
            entries=entries,
            cell_limit=info.context["cell_limit"] if info.context is not None else None,
            byte_limit=info.context["byte_limit"] if info.context is not None else None,
        )
        self._validated_index = index
        _validate_declared_output_types(self, _derive_joined_field_types(self, index))
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.key_field})


class ReferenceTableError(ValueError):
    """The reference table could not be parsed, or is not usable as a table."""


def _reject_duplicate_json_object_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Preserve each JSON object's member names only when they are unambiguous."""
    members: dict[str, object] = {}
    for name, value in pairs:
        if name in members:
            raise ReferenceTableError(f"reference table has a duplicate JSON object member {name!r}")
        members[name] = value
    return members


@dataclass(frozen=True, slots=True)
class ReferenceIndex:
    """A reference table resolved down to exactly what row processing needs.

    ``entries`` maps a stringified key to ``{output_field: value}``, where a
    value may be :data:`_UNRESOLVED`. Every expression has already been
    evaluated, so ``process`` is a dict lookup and evaluates nothing.
    """

    entries: Mapping[str, Mapping[str, Any]]
    cell_count: int
    retained_bytes: int

    def __post_init__(self) -> None:
        # The table is resolved once at config load and read on every row;
        # freezing it is what turns that into a guarantee. A nested reference
        # value (a JSON table's object or array) freezes with it and is emitted
        # as-is: PipelineRow deep-freezes row data itself and thaws in to_dict,
        # so the frozen value is already the row's native representation.
        freeze_fields(self, "entries")


def _parse_reference_entries(cfg: _SingleReferenceJoinConfig | ReferenceTableConfig) -> list[Mapping[str, object]]:
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
            seen_headers: set[str] = set()
            for name in fieldnames:
                if name in seen_headers:
                    raise ReferenceTableError(f"reference table has a duplicate CSV header {name!r}")
                seen_headers.add(name)
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
        loaded = json.loads(cfg.reference_content, object_pairs_hook=_reject_duplicate_json_object_members)
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


def _compile_output_expressions(cfg: _SingleReferenceJoinConfig) -> dict[str, ExpressionParser]:
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
        if (
            compiled[field_name].has_string_amplification_risk()
            or compiled[field_name].has_string_concatenation_risk()
            or compiled[field_name].has_eager_container_string_allocation_risk()
            or compiled[field_name].has_repeated_string_copying_calls()
        ):
            raise ReferenceTableError(
                f"output field {field_name!r} has an expression {expression!r} that can amplify text through "
                "multiplication, formatting, concatenation, eager container construction, or repeated string-copying calls. "
                "Reference outputs must use bounded field access and conversion only."
            )
        if compiled[field_name].result_can_be_set():
            raise ReferenceTableError(
                f"output field {field_name!r} has an expression {expression!r} that can produce a set, which has no "
                "canonical order: the joined value, and the row's output hash, would differ between runs of identical "
                "input. Use a list [...] or tuple (...) literal; a set literal belongs only in a membership test or len()."
            )
    return compiled


def build_reference_index(
    cfg: _SingleReferenceJoinConfig,
    *,
    entries: list[Mapping[str, object]] | None = None,
    cell_limit: int | None = None,
    byte_limit: int | None = None,
) -> ReferenceIndex:
    """Resolve the whole table once: parse, key, and evaluate every output path.

    Called from config validation so that every failure this can detect —
    unparseable content, a missing key column, duplicate keys, an expression
    that resolves for no entry at all — is reported at load rather than on some
    row deep into a run.
    """
    if entries is None:
        entries = _parse_reference_entries(cfg)
    if not entries:
        raise ReferenceTableError(
            "reference table has no entries: every row would miss the join. "
            "A CSV header with no data rows, or a JSON [], is an empty container rather than "
            "sparse data — supply the table, or remove the reference_join node."
        )
    cell_count = len(entries) * len(cfg.output)
    available_cells = MAX_REFERENCE_INDEX_CELLS if cell_limit is None else cell_limit
    available_bytes = MAX_REFERENCE_INDEX_BYTES if byte_limit is None else byte_limit
    if cell_count > available_cells:
        raise ReferenceTableError(
            f"reference table with {len(entries)} entries and {len(cfg.output)} output fields would materialize "
            f"{cell_count} values; the limit is {MAX_REFERENCE_INDEX_CELLS}. Reduce the table or output map."
        )
    compiled = _compile_output_expressions(cfg)

    resolved: dict[str, dict[str, Any]] = {}
    first_position: dict[str, int] = {}
    resolved_count = dict.fromkeys(cfg.output, 0)
    retained_bytes = 0

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
            # ExpressionEvaluationError carries two different facts, told apart by
            # its value-free ``kind``. ``missing_key`` / ``index_out_of_range``
            # mean the PATH does not fit THIS entry — a sparse table is
            # legitimate — so that becomes _UNRESOLVED and is governed by
            # on_miss. Any other kind (division by zero, an invalid value, a type
            # error out of a call or comparison) means the expression is BROKEN
            # for this entry, and swallowing it would hide an author error behind
            # a miss that on_miss cannot tell apart from sparseness.
            # KeyError/TypeError are deliberately NOT caught: expression_parser
            # re-raises those as evaluator bugs that must crash through.
            try:
                value = parser.evaluate({REFERENCE_ENTRY_NAME: entry})
            except ExpressionEvaluationError as exc:
                if exc.kind not in ("missing_key", "index_out_of_range"):
                    raise ReferenceTableError(
                        f"output field {field_name!r} failed to evaluate against reference entry "
                        f"{key!r} (position {position}): {exc}. That is a broken expression rather "
                        "than a sparse entry, so it is refused at load instead of becoming a miss."
                    ) from exc
                values[field_name] = _UNRESOLVED
            else:
                retained_bytes += _bounded_canonical_size(value, limit=available_bytes - retained_bytes)
                values[field_name] = value
                resolved_count[field_name] += 1
        resolved[key] = values

    never_resolved = sorted(name for name, count in resolved_count.items() if count == 0)
    if never_resolved:
        raise ReferenceTableError(
            f"output fields {never_resolved} resolved against none of the {len(entries)} reference entries. "
            "A path that fits no entry is a configuration error, not sparse data; check the expression "
            "against the table's actual keys."
        )

    return ReferenceIndex(entries=resolved, cell_count=cell_count, retained_bytes=retained_bytes)


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


@dataclass(frozen=True, slots=True)
class _JoinedFieldDerivation:
    """Public field definition plus the runtime evidence that produced it."""

    definition: FieldDefinition
    known_non_null_runtime_types: frozenset[type]


def _derive_joined_field_types(cfg: _SingleReferenceJoinConfig, index: ReferenceIndex) -> dict[str, _JoinedFieldDerivation]:
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
    the default rather than failing the row. ``null`` keeps the known hit values
    and makes the field nullable, because any row may miss and take a None.
    Unification is conservative — mixed concrete types declare ``any`` (an
    honest abstention), never a guess; a None in the set makes the field
    nullable rather than widening its type.
    """
    emitting = list(index.entries.values())
    if cfg.on_miss == "fail":
        emitting = [entry_values for entry_values in emitting if all(entry_values[name] is not _UNRESOLVED for name in cfg.output)]
    derived: dict[str, _JoinedFieldDerivation] = {}
    for name in sorted(cfg.output):
        values = [entry_values[name] for entry_values in emitting if entry_values[name] is not _UNRESOLVED]
        if cfg.on_miss == "default":
            values.append(cfg.default_values[name])
        runtime_types = frozenset(type(value) for value in values if value is not None)
        field_type: Literal["str", "int", "float", "bool", "any"] = "any"
        if len(runtime_types) == 1:
            runtime_type = next(iter(runtime_types))
            field_type = _VALUE_FIELD_TYPES[runtime_type] if runtime_type in _VALUE_FIELD_TYPES else "any"
        derived[name] = _JoinedFieldDerivation(
            definition=FieldDefinition(
                name=name,
                field_type=field_type,
                required=True,
                nullable=cfg.on_miss == "null" or any(value is None for value in values),
            ),
            known_non_null_runtime_types=runtime_types,
        )
    return derived


def _validate_declared_output_types(cfg: _SingleReferenceJoinConfig, derived: Mapping[str, _JoinedFieldDerivation]) -> None:
    """Refuse an authored joined-field declaration the resolved table proves wrong.

    A compatible declaration is honored, never clobbered. Authored ``any`` is
    an explicit abstention. Derived public ``any`` retains private evidence so
    a vacuous emitted set cannot refute the author, while known heterogeneous
    or unrepresentable runtime types can. What IS refutable is refused here at
    config load — the alternative was silently overwriting the author, which
    forced a TRUE declaration wrong and steered the correction downstream as
    type erasure (elspeth-cd5cb844bc).

    ``required`` is refused rather than honored for a different reason: every
    joined field is named in the output ``guaranteed_fields``, and honoring
    ``required: false`` would build an output SchemaConfig that fails its OWN
    ``to_dict``/``from_dict`` round-trip — ``from_dict`` refuses a guarantee
    that is optional, and the output config is constructed directly, so nothing
    else catches it (pinned by
    ``test_an_optional_joined_field_would_not_survive_its_own_round_trip``).
    The contradiction is reported here instead of being rewritten in silence.

    Deriving ``required=False`` for a joined field remains open to the plugin
    (elspeth-cd5cb844bc names it as an option under ``on_miss: null``), but it
    would have to drop the field from ``guaranteed_fields`` in the same change.
    This check constrains what an AUTHOR may assert against a guarantee the
    plugin currently makes; it does not decide what that guarantee should be.
    """
    for declared in cfg.schema_config.fields or ():
        if declared.name not in derived:
            continue
        derivation = derived[declared.name]
        derived_field = derivation.definition
        runtime_types = derivation.known_non_null_runtime_types
        if declared.field_type != "any" and runtime_types:
            matching_runtime_type = False
            if len(runtime_types) == 1:
                runtime_type = next(iter(runtime_types))
                if runtime_type in _VALUE_FIELD_TYPES:
                    matching_runtime_type = _VALUE_FIELD_TYPES[runtime_type] == declared.field_type
            if not matching_runtime_type:
                runtime_type_names = sorted(runtime_type.__name__ for runtime_type in runtime_types)
                evidence = f"the resolved reference table can emit non-null runtime types {runtime_type_names}"
                raise ValueError(
                    f"schema declares {declared.name!r} as {declared.field_type}, but {evidence}. "
                    "Fix the declaration or the table; a declared type is honored, never silently overwritten."
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


def _reference_join_added_output_fields(cfg: _SingleReferenceJoinConfig, index: ReferenceIndex) -> tuple[FieldDefinition, ...]:
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
    return tuple(declared_by_name[name] if name in declared_by_name else derived[name].definition for name in sorted(cfg.output))


class ReferenceTableConfig(BaseModel):
    """One configuration-bound table, shared by any number of lookup specs."""

    model_config = ConfigDict(extra="forbid", strict=True)
    reference_content: Annotated[
        str, EmittedToOutput("inline reference text can reach row data after environment expansion", reject_at_validation=False)
    ] = Field(
        description=(
            "Reference table text. On the CLI reference_file is materialized here; in Composer wire an uploaded "
            "blob at node:<node_id>.options.tables.<table_name>.reference_content. The node never opens files at runtime."
        )
    )
    reference_source: str | None = Field(default=None, description="Loader-written source name for diagnostics only; never opened.")
    reference_format: Literal["csv", "json"] = Field(
        description="Explicit table format: CSV cells remain strings; JSON retains numeric types."
    )


class ReferenceLookupConfig(BaseModel):
    """An ordered lookup against a named table."""

    model_config = ConfigDict(extra="forbid", strict=True)
    table: str = Field(min_length=1, description="Name of an entry in tables; multiple lookups may share one table.")
    key_field: str = Field(
        description="Key field from the original input row or an earlier lookup's output. No self or forward references."
    )
    reference_key_name: str = Field(description="Match column within the reference table, not a pipeline row field.")
    output: Annotated[dict[str, str], EmittedToOutput("output names and expression literals reach row data")] = Field(
        description="Distinct output fields mapped to expressions over ref, the matched reference entry. No row access."
    )
    on_miss: Literal["fail", "null", "default"] = Field(
        default="fail",
        description=(
            "On missing match or sparse output path: fail the whole original row via the node's on_error route, "
            "write null, or write default_values. No partial enrichment is emitted. Invalid input/configuration is never a miss."
        ),
    )
    default_values: Annotated[dict[str, Any], EmittedToOutput("lookup fallback values are written directly into row data")] = Field(
        default_factory=dict, description="Per-output fallback values; all output fields must be covered for on_miss: default."
    )


@dataclass(frozen=True, slots=True)
class _CompiledLookup:
    table: str | None
    config: _SingleReferenceJoinConfig
    index: ReferenceIndex

    def error(self, reason: TransformErrorReason, lookup_index: int) -> TransformResult:
        """Attribute an error without adding any row or reference values."""
        if self.table is not None:
            reason["lookup_index"] = lookup_index
            reason["table"] = self.table
        return TransformResult.error(reason, retryable=False)


class _ReferenceFieldWire(TypedDict, total=False):
    """Serialized field definition; include/exclude may select any subset."""

    name: str
    field_type: Literal["str", "int", "float", "bool", "any"]
    required: bool
    nullable: bool


class _ReferenceSchemaWire(TypedDict, total=False):
    """Pydantic's schema dataclass wire shape in Python and JSON modes."""

    mode: Literal["fixed", "flexible", "observed"]
    fields: tuple[_ReferenceFieldWire, ...] | list[_ReferenceFieldWire] | None
    guaranteed_fields: tuple[str, ...] | list[str] | None
    required_fields: tuple[str, ...] | list[str] | None
    audit_fields: tuple[str, ...] | list[str] | None


class _ReferenceTableWire(TypedDict, total=False):
    """Serialized named table, including optional diagnostic provenance."""

    reference_content: str
    reference_source: str | None
    reference_format: Literal["csv", "json"]


class _ReferenceLookupWire(TypedDict, total=False):
    """Serialized lookup semantics and arbitrary configured fallback values."""

    table: str
    key_field: str
    reference_key_name: str
    output: dict[str, str]
    on_miss: Literal["fail", "null", "default"]
    default_values: dict[str, Any]


class _ReferenceJoinConfigWire(TypedDict, total=False):
    """Serialized active join form, honoring aliases and field selections.

    Every key is optional because Pydantic include/exclude/default filtering
    may remove it. Schema is emitted under exactly one of its two names.
    The serializer removes the inactive single or multi option set.
    """

    schema: _ReferenceSchemaWire
    schema_config: _ReferenceSchemaWire
    required_input_fields: list[str] | None
    reference_content: str | None
    reference_source: str | None
    reference_format: Literal["csv", "json"] | None
    key_field: str | None
    reference_key_name: str | None
    output: dict[str, str] | None
    on_miss: Literal["fail", "null", "default"]
    default_values: dict[str, Any]
    tables: dict[str, _ReferenceTableWire] | None
    lookups: list[_ReferenceLookupWire] | None


class ReferenceJoinConfig(TransformDataConfig):
    """One legacy lookup, or named tables and ordered lookup specifications."""

    reference_content: Annotated[
        str | None, EmittedToOutput("inline reference text can reach row data after environment expansion", reject_at_validation=False)
    ] = Field(default=None, description=_SingleReferenceJoinConfig.model_fields["reference_content"].description)
    reference_source: str | None = Field(default=None, description=_SingleReferenceJoinConfig.model_fields["reference_source"].description)
    reference_format: Literal["csv", "json"] | None = Field(
        default=None, description=_SingleReferenceJoinConfig.model_fields["reference_format"].description
    )
    key_field: str | None = Field(default=None, description=_SingleReferenceJoinConfig.model_fields["key_field"].description)
    reference_key_name: str | None = Field(
        default=None, description=_SingleReferenceJoinConfig.model_fields["reference_key_name"].description
    )
    output: Annotated[dict[str, str] | None, EmittedToOutput("output names and expression literals reach row data")] = Field(
        default=None, description=_SingleReferenceJoinConfig.model_fields["output"].description
    )
    on_miss: Literal["fail", "null", "default"] = Field(
        default="fail", description=_SingleReferenceJoinConfig.model_fields["on_miss"].description
    )
    default_values: Annotated[dict[str, Any], EmittedToOutput("reference_join fallback values are written directly into row data")] = Field(
        default_factory=dict, description=_SingleReferenceJoinConfig.model_fields["default_values"].description
    )
    tables: dict[str, ReferenceTableConfig] | None = Field(default=None, description="Named configuration-bound reference tables.")
    lookups: list[ReferenceLookupConfig] | None = Field(
        default=None, description="Lookups evaluated in configuration order; later keys may consume earlier outputs."
    )
    _compiled_lookups: tuple[_CompiledLookup, ...] = PrivateAttr()

    @model_serializer(mode="wrap")
    def _serialize_active_form(self, handler: SerializerFunctionWrapHandler) -> _ReferenceJoinConfigWire:
        """Preserve the selected configuration form through normal round trips."""
        serialized: _ReferenceJoinConfigWire = handler(self)
        if self.tables is None:
            serialized.pop("tables", None)
            serialized.pop("lookups", None)
        else:
            serialized.pop("reference_content", None)
            serialized.pop("reference_source", None)
            serialized.pop("reference_format", None)
            serialized.pop("key_field", None)
            serialized.pop("reference_key_name", None)
            serialized.pop("output", None)
            serialized.pop("on_miss", None)
            serialized.pop("default_values", None)
        return serialized

    @model_validator(mode="after")
    def _validate_lookup_forms(self) -> ReferenceJoinConfig:
        legacy_fields = {
            "reference_content",
            "reference_source",
            "reference_format",
            "key_field",
            "reference_key_name",
            "output",
            "on_miss",
            "default_values",
        }
        compiled: list[_CompiledLookup] = []
        if self.tables is None and self.lookups is None:
            values = {name: value for name, value in self.model_dump().items() if name not in {"tables", "lookups"}}
            cfg = _SingleReferenceJoinConfig.model_validate(values)
            compiled.append(_CompiledLookup(None, cfg, cfg._validated_index))
        else:
            if self.model_fields_set & legacy_fields:
                raise ValueError("tables/lookups and single-lookup options are mutually exclusive")
            if not self.tables or not self.lookups:
                raise ValueError("tables and at least one lookup are required")
            used = {spec.table for spec in self.lookups}
            unknown = used - self.tables.keys()
            if unknown:
                raise ValueError(f"lookups name unknown table(s): {sorted(unknown)}")
            unused = self.tables.keys() - used
            if unused:
                raise ValueError(f"unused reference tables: {sorted(unused)}")
            output_names: set[str] = set()
            for spec in self.lookups:
                duplicate = output_names & spec.output.keys()
                if duplicate:
                    raise ValueError(f"duplicate output fields across lookups: {sorted(duplicate)}")
                output_names.update(spec.output)
            available_outputs: set[str] = set()
            for lookup_index, spec in enumerate(self.lookups):
                key_field = spec.key_field.strip()
                if key_field in output_names and key_field not in available_outputs:
                    raise ValueError(f"lookup {lookup_index} key_field {spec.key_field!r} references an output not yet available")
                available_outputs.update(spec.output)
            # Configuration-scoped cache: never a filename or a global cache.
            # The named table binds both content and format; indexes are built
            # separately for each match-column/output-expression combination.
            parsed = {name: _parse_reference_entries(table) for name, table in self.tables.items()}
            cells = 0
            retained_bytes = 0
            for spec in self.lookups:
                values = {
                    "schema": self.schema_config.to_dict(),
                    **self.tables[spec.table].model_dump(),
                    **spec.model_dump(exclude={"table"}),
                }
                cfg = _SingleReferenceJoinConfig.model_validate(
                    values,
                    context={
                        "entries": parsed[spec.table],
                        "cell_limit": MAX_REFERENCE_INDEX_CELLS - cells,
                        "byte_limit": MAX_REFERENCE_INDEX_BYTES - retained_bytes,
                    },
                )
                index = cfg._validated_index
                cells += index.cell_count
                retained_bytes += index.retained_bytes
                if cells > MAX_REFERENCE_INDEX_CELLS:
                    raise ReferenceTableError(f"reference lookups materialize {cells} values; the limit is {MAX_REFERENCE_INDEX_CELLS}")
                if retained_bytes > MAX_REFERENCE_INDEX_BYTES:
                    raise ReferenceTableError(
                        f"reference lookup outputs exceed the {MAX_REFERENCE_INDEX_BYTES}-byte materialized index limit"
                    )
                compiled.append(_CompiledLookup(spec.table, cfg, index))
        self._compiled_lookups = tuple(compiled)
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        required = set(super().declared_input_fields)
        available_outputs: set[str] = set()
        for lookup in self._compiled_lookups:
            if lookup.config.key_field not in available_outputs:
                required.add(lookup.config.key_field)
            available_outputs.update(lookup.config.output)
        return frozenset(required)


class ReferenceJoin(BaseTransform):
    """Match a row field against a reference table and add named fields to the row."""

    # key_field is the INPUT column this transform READS; the ``output`` map
    # names created fields but is not a column-naming option by
    # ``is_column_naming_config_option``, so it needs no classification here.
    output_naming_config_keys: frozenset[str] = frozenset()
    name = "reference_join"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:4ae18cd4950c7221"
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
        self._lookups = cfg._compiled_lookups
        first = self._lookups[0]
        self._key_field = first.config.key_field
        self._index = first.index
        self.declared_output_fields = frozenset(name for lookup in self._lookups for name in lookup.config.output)
        self.input_schema = create_schema_from_config(cfg.schema_config, "ReferenceJoinInput", allow_coercion=False)
        field_by_name = {field.name: field for field in cfg.schema_config.fields or ()}
        for lookup in self._lookups:
            field_by_name.update((field.name, field) for field in _reference_join_added_output_fields(lookup.config, lookup.index))
        guaranteed = tuple(sorted(set(cfg.schema_config.guaranteed_fields or ()) | self.declared_output_fields))
        self._output_schema_config = SchemaConfig(
            mode=cfg.schema_config.mode if cfg.schema_config.fields is not None else "flexible",
            fields=declare_missing_guaranteed_fields(tuple(field_by_name.values()), guaranteed),
            guaranteed_fields=guaranteed,
            audit_fields=cfg.schema_config.audit_fields,
            required_fields=cfg.schema_config.required_fields,
        )
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
                    "In the composer, discover an uploaded ready table with list_blobs and get_blob_metadata. "
                    "Wire it with wire_blob_inline_ref at 'node:<node_id>.options.reference_content'.",
                    "Use create_blob only for table bytes you create. Set reference_format explicitly to csv or json; "
                    "the blob filename does not infer it.",
                    "CSV cells remain strings, including numeric-looking cells such as 24. JSON numbers retain numeric types. "
                    "A field_mapper or sink schema does not convert these values.",
                    "Keep downstream fields as str when acceptable, or insert type_coerce after the join before a numeric consumer. "
                    "For example, conversions: [{field: amount, to: int}] converts a joined amount field; "
                    "its schema declares the arriving str, not the converted int.",
                    "Never replace a supplied CSV with JSON to repair types. Use a typed JSON table only when the user's data "
                    "requirements permit that format; otherwise preserve the table and explicitly convert the joined fields.",
                    "Pasting a table as a literal option value hits the inline byte cap.",
                    "Output expressions see ONLY the matched entry as 'ref'. row[...] is not in scope here and is rejected "
                    "at config load, and a bare column name is not an expression — write ref['description'].",
                    "reference_key_name names a column of the reference table; key_field names the column on the arriving row.",
                    "The table must be rectangular and non-empty: every row needs a cell per header column "
                    "(empty counts, missing does not), and a header with no data rows is refused. These fire "
                    "at config load, so a blob you cannot inspect fails the run.",
                    "on_miss defaults to fail, because an unenriched row reaching a sink looks identical to an enriched one.",
                    "For multiple lookups use tables: {name: {reference_content, reference_format}} and lookups: "
                    "[{table, key_field, reference_key_name, output, on_miss, default_values}]. These are mutually exclusive "
                    "with the top-level single-lookup options. Shared tables are parsed once.",
                    "Wire each uploaded table with wire_blob_inline_ref at "
                    "'node:<node_id>.options.tables.<table_name>.reference_content'; on the CLI put reference_file under each table.",
                    "Lookups run in configuration order. A key_field can read the original row or an earlier lookup's output, "
                    "so first resolve a shared key and use it in multiple later tables. Self and forward references are rejected. "
                    "Output names must be distinct and cannot overwrite arriving fields.",
                    "A generated null is an invalid downstream key, even with on_miss: null or default. A default string is "
                    "looked up normally; if absent, that later lookup applies its own on_miss policy. Input schema admission "
                    "precedes the ordered lookups, and the first lookup failure stops processing.",
                    "Set a lookup's on_miss to default and supply default_values, or use fail with the node's on_error "
                    "quarantine sink. A failure routes the whole original row; no partial enrichment goes downstream.",
                ),
            )
        return None

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Give the probe row a key that the probe table actually contains."""
        return [self._augment_invariant_probe_row(probe, field_name=self._key_field, value="probe")]

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        del ctx
        values: dict[str, Any] = {}
        audit: list[dict[str, Any]] = []
        for lookup_index, lookup in enumerate(self._lookups):
            cfg = lookup.config
            attribution = {} if lookup.table is None else {"lookup_index": lookup_index, "table": lookup.table}
            if cfg.key_field not in values and cfg.key_field not in row:
                return lookup.error(
                    {
                        "reason": "invalid_input",
                        "field": cfg.key_field,
                        "error": f"row has no {cfg.key_field!r} field to join on",
                    },
                    lookup_index,
                )
            raw_key = values[cfg.key_field] if cfg.key_field in values else row[cfg.key_field]
            try:
                key = _coerce_key(raw_key)
            except ReferenceTableError:
                return lookup.error(
                    {
                        "reason": "invalid_input",
                        "field": cfg.key_field,
                        "error": f"join key must be a string or number, got {type(raw_key).__name__}",
                    },
                    lookup_index,
                )
            entry = lookup.index.entries[key] if key in lookup.index.entries else None
            missed: list[str] = []
            for field_name in sorted(cfg.output):
                resolved = _UNRESOLVED if entry is None else entry[field_name]
                if resolved is _UNRESOLVED:
                    missed.append(field_name)
                else:
                    values[field_name] = resolved
            if missed:
                if cfg.on_miss == "fail":
                    origin = f" in {cfg.reference_source}" if cfg.reference_source else ""
                    return lookup.error(
                        {
                            "reason": "reference_miss",
                            "field": cfg.key_field,
                            "unresolved_fields": missed,
                            "error": (
                                f"no reference entry for the row's {cfg.key_field!r}{origin}"
                                if entry is None
                                else f"the reference entry for the row's {cfg.key_field!r}{origin} did not resolve {missed}"
                            ),
                        },
                        lookup_index,
                    )
                for field_name in missed:
                    values[field_name] = cfg.default_values[field_name] if cfg.on_miss == "default" else None
            audit.append(
                {
                    **attribution,
                    "fields": sorted(cfg.output),
                    "matched": entry is not None,
                    "unresolved_fields": missed,
                    "on_miss": cfg.on_miss,
                }
            )

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
                "metadata": (
                    {"matched": audit[0]["matched"], "unresolved_fields": audit[0]["unresolved_fields"]}
                    if self._lookups[0].table is None
                    else {"lookups": audit}
                ),
            },
        )

    def close(self) -> None:
        """No resources to release: the reference table is configuration."""
