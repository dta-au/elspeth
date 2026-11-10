"""Write whole files, bound as configuration content, into row fields.

Each ``files`` entry arrives as TEXT in ``content``, never as a path this
module opens. On the CLI, ``files.<name>.content_file`` is materialized into
``content`` + ``content_source`` by the settings loader
(:data:`elspeth.core.template_materialization.FILE_BACKED_TEMPLATE_OPTION_REGISTRY`);
on the web, the same option is filled by ``inline_content`` blob substitution.
Both deliver a ``str``, so the format-aware parsing lives here, at the one
point that sees both.

Because the content is IN THE CONFIG, it flows into node identity and the
topology hash, so editing a bound file between a run and its resume refuses
the resume.

This module currently holds the configuration half: the config models, the
parsing of each entry and the declared types of the written fields. Every
refusal here fires at config validation, before any row is processed.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.identifiers import validate_field_name
from elspeth.contracts.schema import FieldDefinition
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.config_content import (
    CanonicalSizeLimitExceededError,
    NonCanonicalValueTypeError,
    NonStringObjectKeyError,
    bounded_canonical_size,
    reject_duplicate_json_object_members,
    reject_nonfinite_constant,
)

#: Upper bound on the canonical bytes of one entry's parsed value.
MAX_FILE_INJECT_ENTRY_BYTES = 16 * 1024 * 1024

#: Upper bound on the canonical bytes of all of a node's parsed values. Every
#: entry counts in both modes; in ``all`` mode this is what each row gains.
MAX_FILE_INJECT_NODE_BYTES = 64 * 1024 * 1024

#: Deepest container nesting accepted in a ``json`` entry, in each ``jsonl``
#: line, and in ``default_content``. Far above any real document and far below
#: the interpreter's recursion limit, which the size walk would otherwise hit.
MAX_FILE_INJECT_JSON_DEPTH = 100

InjectedFormat = Literal["text", "json", "jsonl", "csv"]


def _refuse_deep_or_nonfinite(value: object) -> None:
    """Refuse a value nested past the depth limit or holding a non-finite float.

    Iterative on purpose: the values it guards against are the ones a
    recursive walk cannot survive.
    """
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if type(item) is float and not math.isfinite(item):
            raise ValueError("holds a non-finite number (NaN or Infinity)")
        # isinstance, not an exact-type test: this must descend into exactly
        # what bounded_canonical_size descends into (any Mapping, list or
        # tuple, e.g. a frozen options bag), or a container it skipped would
        # reach that recursive walk unchecked.
        children: Iterable[object]
        if isinstance(item, Mapping):
            children = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth >= MAX_FILE_INJECT_JSON_DEPTH:
            raise ValueError(f"nested more than {MAX_FILE_INJECT_JSON_DEPTH} levels deep")
        pending.extend((child, depth + 1) for child in children)


def _finite_float(literal: str) -> float:
    """Refuse a JSON number that overflows to infinity (``1e999``).

    ``parse_constant`` only sees the spelled constants; an overflowing literal
    reaches ``float`` and would enter row data as a value canonical JSON cannot
    hash.
    """
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError(f"JSON number {literal} is not finite")
    return value


def _load_json(text: str) -> object:
    """Parse one JSON value under the strict rules shared with reference_join."""
    try:
        loaded: object = json.loads(
            text,
            object_pairs_hook=reject_duplicate_json_object_members,
            parse_constant=reject_nonfinite_constant,
            parse_float=_finite_float,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # The scanner gave up before the explicit check below could count.
        raise ValueError(f"JSON is nested more than {MAX_FILE_INJECT_JSON_DEPTH} levels deep") from exc
    try:
        _refuse_deep_or_nonfinite(loaded)
    except ValueError as exc:
        raise ValueError(f"JSON is {exc}") from exc
    return loaded


def _parse_csv(text: str, *, delimiter: str) -> list[dict[str, str]]:
    """Parse CSV text into one dict per data row, keyed by the header verbatim."""
    if len(delimiter) != 1 or delimiter in '\r\n"':
        raise ValueError(f"delimiter must be exactly one character, and not a quote or a line break; got {delimiter!r}")
    # newline=None reads \n, \r\n and \r alike and hands the reader \n, so no
    # carriage return survives in a header name or a last cell.
    stream = io.StringIO(text, newline=None)
    rows: list[dict[str, str]] = []
    reader = csv.DictReader(stream, delimiter=delimiter, strict=True)
    try:
        fieldnames = reader.fieldnames
        if not fieldnames:
            raise ValueError("no CSV header row: the content is empty or starts with a blank line")
        seen: set[str] = set()
        for name in fieldnames:
            if name == "":
                raise ValueError("empty CSV header name; every column needs a name")
            if name in seen:
                raise ValueError(f"duplicate CSV header {name!r}")
            seen.add(name)
        for position, record in enumerate(reader, start=1):
            # DictReader buckets surplus cells under the key None and pads a
            # short row with the value None. Either would enter row data as an
            # ordinary value, so row arity is refused here instead.
            if None in record:
                raise ValueError(f"data row {position} has more cells than the {len(fieldnames)}-column header")
            if any(cell is None for cell in record.values()):
                raise ValueError(
                    f"data row {position} has fewer cells than the {len(fieldnames)}-column header; "
                    "pad the row (an empty cell is fine) or fix the header"
                )
            rows.append(dict(record))
    except csv.Error as exc:
        # The csv module reports its per-cell cap only through this message;
        # it is a size limit, not malformed CSV, so it is reported as one.
        if str(exc).startswith("field larger than field limit"):
            raise ValueError(
                f"a cell exceeds the CSV per-cell limit of {csv.field_size_limit()} characters "
                f"(near line {reader.line_num}). Shorten the cell, or bind the file as format 'text'."
            ) from exc
        raise ValueError(f"not valid CSV: {exc}") from exc
    return rows


def parse_injected_content(content: str, *, fmt: InjectedFormat, data_key: str | None, delimiter: str) -> object:
    """Parse one entry's text into the value written to a row.

    ``text`` returns ``content`` itself, unmodified. The structured formats
    first drop one leading U+FEFF: an editor-written BOM would otherwise make
    JSON unparseable and hide inside the first CSV header name.

    Raises:
        ValueError: the content does not parse under ``fmt``, or ``data_key`` /
            ``delimiter`` does not apply to it. The message names neither the
            entry nor the node; the caller adds that.
    """
    if data_key is not None and fmt != "json":
        raise ValueError(f"data_key applies only to format 'json', not {fmt!r}")
    if delimiter != "," and fmt != "csv":
        raise ValueError(f"delimiter applies only to format 'csv', not {fmt!r}")
    if fmt == "text":
        return content

    text = content.removeprefix("\ufeff")
    if fmt == "csv":
        return _parse_csv(text, delimiter=delimiter)
    if fmt == "jsonl":
        values: list[object] = []
        # Universal newlines only: str.splitlines would also break on U+2028
        # and friends, which are legal inside a JSON string.
        for number, line in enumerate(io.StringIO(text, newline=None), start=1):
            if not line.strip():
                continue
            try:
                values.append(_load_json(line))
            except ValueError as exc:
                raise ValueError(f"line {number}: {exc}") from exc
        return values

    if not text.strip():
        raise ValueError("content is empty; format 'json' needs one JSON value")
    loaded = _load_json(text)
    if data_key is None:
        return loaded
    # ``json.loads`` with this pairs hook yields an exact dict for an object.
    if type(loaded) is not dict:
        raise ValueError(f"data_key {data_key!r} needs a top-level JSON object, got {type(loaded).__name__}")
    if data_key not in loaded:
        raise ValueError(f"the top-level JSON object has no member {data_key!r}; it has {sorted(loaded)}")
    return loaded[data_key]


class FileInjectEntryConfig(BaseModel):
    """One file bound as configuration content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Content is guarded only BEFORE environment expansion: a materialized file
    # arrives afterwards and its literal ${...} text must stay literal.
    content: Annotated[
        str, EmittedToOutput("file_inject writes this content into row data after environment expansion", reject_at_validation=False)
    ] = Field(
        description=(
            "The file's text. Written by the settings loader from 'content_file' on the CLI, or by "
            "inline_content blob substitution on the web. The node never opens a file at runtime."
        )
    )
    content_source: str | None = Field(
        default=None,
        description=(
            "Do not set this. The settings loader writes it when it expands 'content_file', "
            "and it is used only in diagnostics; nothing resolves or reads the path."
        ),
    )
    format: InjectedFormat = Field(
        description=(
            "How to read the content. Never inferred from a file name. text writes the whole content as one "
            "string, unmodified; json writes the parsed value; jsonl writes a list with one parsed value per "
            "non-blank line; csv writes a list of row objects keyed by the header, whose cells remain strings."
        )
    )
    data_key: str | None = Field(
        default=None, description="json only: write this member of a top-level JSON object instead of the whole value."
    )
    delimiter: str = Field(default=",", description="csv only: the single character that separates cells.")
    output_field: str | None = Field(
        default=None,
        description="mode 'all' only, and required there: the row field this entry's value is written to.",
    )

    @field_validator("output_field")
    @classmethod
    def _require_field_name(cls, value: str | None) -> str | None:
        return None if value is None else validate_field_name(value, "output_field")


#: Exact-type map from a written value to a declarable field type. An exact
#: test keeps bool apart from int; a list or dict has no entry.
_VALUE_FIELD_TYPES: dict[type, Literal["str", "int", "float", "bool"]] = {str: "str", bool: "bool", int: "int", float: "float"}


@dataclass(frozen=True, slots=True)
class _WrittenField:
    """A written field's declared definition plus the evidence behind it."""

    definition: FieldDefinition
    known_non_null_runtime_types: frozenset[type]
    null_reason: str | None


def _derive_written_field(name: str, *, values: list[object], every_entry_is_text: bool, null_reason: str | None) -> _WrittenField:
    """Declare one written field from the closed set of values it can take.

    The declared type follows the entry FORMAT, not the value: ``str`` only
    when every entry that can reach the field is ``text`` and no miss policy
    writes a non-string, otherwise ``any``. The runtime types are kept beside
    it so an author's narrower declaration can still be checked against what
    the parsed content actually is.
    """
    runtime_types = frozenset(type(value) for value in values if value is not None)
    return _WrittenField(
        definition=FieldDefinition(
            name=name,
            field_type="str" if every_entry_is_text and runtime_types == {str} else "any",
            required=True,
            nullable=null_reason is not None,
        ),
        known_non_null_runtime_types=runtime_types,
        null_reason=null_reason,
    )


class FileInjectConfig(TransformDataConfig):
    """Configuration for file_inject: which files, and where each is written."""

    mode: Literal["select", "all"] = Field(
        description=(
            "select writes the one entry whose name equals the row's key_field value to output_field. "
            "all writes every entry onto every row, each to its own output_field. There is no default."
        )
    )
    files: dict[str, FileInjectEntryConfig] = Field(
        description=(
            "Entry name to file. In mode 'select' the names are the key values and are compared exactly as "
            "written, so they must be quoted strings; in mode 'all' they are labels. At least one entry."
        )
    )
    key_field: str | None = Field(
        default=None, description="mode 'select' only, and required there: the row field whose value names the entry."
    )
    output_field: str | None = Field(
        default=None, description="mode 'select' only, and required there: the row field the selected entry's value is written to."
    )
    on_miss: Literal["fail", "null", "default"] | None = Field(
        default=None,
        description=(
            "mode 'select' only. What to do when the row's key value names no entry: fail the row (the default), "
            "write null, or write default_content."
        ),
    )
    default_content: Annotated[
        Any, EmittedToOutput("file_inject writes this value directly into row data whenever the row's key names no entry")
    ] = Field(default=None, description="The non-null value written when on_miss is 'default'. Required then, refused otherwise.")

    _parsed: dict[str, object] = PrivateAttr()
    _written: dict[str, _WrittenField] = PrivateAttr()

    @field_validator("files", mode="before")
    @classmethod
    def _require_matchable_entry_names(cls, value: object) -> object:
        """Refuse entry names that pydantic would coerce or nobody could match.

        YAML reads an unquoted ``1:`` or ``true:`` as an int or a bool. Turning
        that into ``"1"`` or ``"True"`` would bind an entry no row value the
        author had in mind ever equals.
        """
        if isinstance(value, Mapping):
            for name in value:
                if type(name) is not str:
                    raise ValueError(
                        f"entry names must be quoted strings, got {name!r} ({type(name).__name__}). "
                        "Quote the name exactly as rows spell it."
                    )
                if not name:
                    raise ValueError("entry name must not be empty; an empty name can never be matched deliberately")
                if name != name.strip():
                    raise ValueError(f"entry name {name!r} has leading or trailing whitespace. Names are compared exactly, so remove it.")
        return value

    @field_validator("key_field")
    @classmethod
    def _reject_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("output_field")
    @classmethod
    def _require_field_name(cls, value: str | None) -> str | None:
        return None if value is None else validate_field_name(value, "output_field")

    @field_validator("default_content")
    @classmethod
    def _require_canonical_default(cls, value: object) -> object:
        # A field validator, so it runs before the inherited model validators:
        # the env-placeholder scan walks this value recursively and would hit
        # the recursion limit on a deeply nested one before it was refused.
        if value is not None:
            _default_content_size(value)
        return value

    @model_validator(mode="after")
    def _validate_injection(self) -> FileInjectConfig:
        if not self.files:
            raise ValueError("files must name at least one entry; an empty map injects nothing")

        # Which entries can reach each written field. Narrowed locals here are
        # what lets the rest of the method read the mode's options as present.
        writers: dict[str, list[str]] = {}
        null_reason: str | None = None
        miss_values: list[object] = []
        node_bytes = 0
        if self.mode == "all":
            for option, is_set in (
                ("key_field", self.key_field is not None),
                ("top-level output_field", self.output_field is not None),
                ("on_miss", self.on_miss is not None),
                ("default_content", self.default_content is not None),
            ):
                if is_set:
                    raise ValueError(f"{option} applies only to mode 'select'; mode 'all' writes every entry to its own output_field")
            for name, entry in self.files.items():
                if entry.output_field is None:
                    raise ValueError(f"files entry {name!r} needs an output_field: mode 'all' writes each entry to its own field")
                if entry.output_field in writers:
                    raise ValueError(
                        f"files entries {writers[entry.output_field][0]!r} and {name!r} both write output_field "
                        f"{entry.output_field!r}; each entry needs its own field"
                    )
                writers[entry.output_field] = [name]
        else:
            if self.key_field is None:
                raise ValueError("mode 'select' requires key_field, the row field whose value names the entry")
            if self.output_field is None:
                raise ValueError("mode 'select' requires output_field, the row field the selected entry is written to")
            for name, entry in self.files.items():
                if entry.output_field is not None:
                    raise ValueError(
                        f"files entry {name!r}: output_field applies only to mode 'all'; mode 'select' writes the "
                        "selected entry to the top-level output_field"
                    )
            # None means "not set", so a dumped config validates again unchanged.
            if self.on_miss == "default":
                if self.default_content is None:
                    raise ValueError("on_miss is 'default' but default_content is not set; to write a null, use on_miss: null")
                node_bytes = _default_content_size(self.default_content)
                miss_values.append(self.default_content)
            elif self.default_content is not None:
                raise ValueError(f"default_content applies only when on_miss is 'default'; on_miss is {self.effective_on_miss!r}")
            elif self.on_miss == "null":
                null_reason = "on_miss is 'null', so any row that misses takes a None"
            # The live invariant gate mutates key_field to name a created field
            # and requires the config to be refused naming that option
            # (tests/invariants/test_input_options_do_not_name_created_fields.py).
            if self.key_field == self.output_field:
                raise ValueError(
                    f"key_field {self.key_field!r} is also the output_field, so this transform would overwrite the column "
                    "it reads. Repoint key_field at the arriving column, or rename output_field."
                )
            writers[self.output_field] = list(self.files)

        declared_inputs = super().declared_input_fields
        for field_name in writers:
            if field_name in declared_inputs:
                raise ValueError(
                    f"output_field {field_name!r} is also named in required_input_fields, so this transform would "
                    "overwrite a column it declares as arriving. Rename the output_field, or drop it from required_input_fields."
                )

        # Parse here so malformed content fails at config load rather than on
        # the first row. The declared-type check must live here too, not in the
        # transform's __init__, so pre-validation and the engine agree on
        # rejection (test_validation_path_agreement).
        self._parsed = {}
        for name, entry in self.files.items():
            try:
                value = parse_injected_content(entry.content, fmt=entry.format, data_key=entry.data_key, delimiter=entry.delimiter)
            except ValueError as exc:
                raise ValueError(f"files entry {name!r}{_origin(entry)}: {exc}") from exc
            try:
                node_bytes += bounded_canonical_size(value, limit=MAX_FILE_INJECT_ENTRY_BYTES)
            except CanonicalSizeLimitExceededError as exc:
                raise ValueError(
                    f"files entry {name!r}{_origin(entry)} exceeds the {MAX_FILE_INJECT_ENTRY_BYTES}-byte per-entry limit "
                    "on its parsed value. Reduce the file."
                ) from exc
            if node_bytes > MAX_FILE_INJECT_NODE_BYTES:
                raise ValueError(
                    f"files entries{' and default_content' if miss_values else ''} together exceed the "
                    f"{MAX_FILE_INJECT_NODE_BYTES}-byte per-node limit on parsed values (reached at entry {name!r}). "
                    "Reduce the files, or split them across nodes."
                )
            self._parsed[name] = value

        written: dict[str, _WrittenField] = {}
        for field_name in sorted(writers):
            entry_names = writers[field_name]
            field_null_reason = null_reason
            if field_null_reason is None and any(self._parsed[name] is None for name in entry_names):
                field_null_reason = "the bound content is a JSON null this transform will write"
            written[field_name] = _derive_written_field(
                field_name,
                values=[*(self._parsed[name] for name in entry_names), *miss_values],
                every_entry_is_text=all(self.files[name].format == "text" for name in entry_names),
                null_reason=field_null_reason,
            )
        self._written = written
        _validate_declared_output_types(self)
        return self

    @property
    def effective_on_miss(self) -> Literal["fail", "null", "default"]:
        """The miss policy ``select`` applies; ``fail`` when ``on_miss`` is unset.

        Meaningless in mode ``all``, which has no key and so no miss.
        """
        return "fail" if self.on_miss is None else self.on_miss

    @property
    def declared_input_fields(self) -> frozenset[str]:
        declared = super().declared_input_fields
        return declared if self.key_field is None else declared | frozenset({self.key_field})


def _default_content_size(value: object) -> int:
    """Canonical size of ``default_content``, refusing what a row cannot carry.

    The value is written to row data on every miss, so it is held to the same
    rules as bound content: canonical JSON, finite, bounded in depth and size.
    """
    try:
        _refuse_deep_or_nonfinite(value)
        return bounded_canonical_size(value, limit=MAX_FILE_INJECT_ENTRY_BYTES)
    except CanonicalSizeLimitExceededError as exc:
        raise ValueError(f"default_content exceeds the {MAX_FILE_INJECT_ENTRY_BYTES}-byte per-entry limit. Reduce it.") from exc
    except NonStringObjectKeyError as exc:
        raise ValueError("default_content must be canonical JSON: its objects must use string keys") from exc
    except NonCanonicalValueTypeError as exc:
        raise ValueError(
            f"default_content must be canonical JSON: a value of type {exc.type_name} is not a string, number, "
            "boolean, null, list or object. Quote it if text was intended."
        ) from exc
    except ValueError as exc:
        raise ValueError(f"default_content {exc}") from exc


def _origin(entry: FileInjectEntryConfig) -> str:
    """Name the file an entry came from, when the loader recorded one."""
    return f" (from {entry.content_source})" if entry.content_source else ""


def _validate_declared_output_types(cfg: FileInjectConfig) -> None:
    """Refuse an authored written-field declaration the bound content proves wrong.

    A compatible declaration is honored, never overwritten. Authored ``any`` is
    an explicit abstention. A concrete type is checked against the runtime
    types the field can actually take, so ``int`` over a ``json`` entry holding
    ``3`` is true even though the field would otherwise declare ``any``.

    ``required: false`` is refused: every written field is guaranteed on every
    row the transform emits, and a guarantee cannot be optional.
    """
    for declared in cfg.schema_config.fields or ():
        if declared.name not in cfg._written:
            continue
        written = cfg._written[declared.name]
        runtime_types = written.known_non_null_runtime_types
        if declared.field_type != "any" and runtime_types:
            declared_runtime_types = frozenset(
                runtime_type for runtime_type, field_type in _VALUE_FIELD_TYPES.items() if field_type == declared.field_type
            )
            if runtime_types != declared_runtime_types:
                runtime_type_names = sorted(runtime_type.__name__ for runtime_type in runtime_types)
                raise ValueError(
                    f"schema declares {declared.name!r} as {declared.field_type}, but file_inject can write non-null "
                    f"runtime types {runtime_type_names} there. Fix the declaration or the entry formats; "
                    "a declared type is honored, never silently overwritten."
                )
        if written.null_reason is not None and not declared.nullable:
            raise ValueError(
                f"schema declares {declared.name!r} as never null, but {written.null_reason}. Declare the field "
                f"nullable — {{name: {declared.name}, type: {declared.field_type}, required: true, nullable: true}} — "
                "or remove the null."
            )
        if not declared.required:
            raise ValueError(
                f"schema declares {declared.name!r} optional, but file_inject creates it and guarantees it on every "
                "row it emits, and a guarantee cannot be optional. Declare it required (the 'name: type?' form "
                "means optional), or drop it from 'fields' and let file_inject declare it."
            )


def added_output_fields(cfg: FileInjectConfig) -> tuple[FieldDefinition, ...]:
    """One definition per written field, sorted by name and always required.

    The author's own declaration is taken WHOLE when one exists: validation
    has already refused every part of it the bound content refutes. A field
    the author did not declare takes the derived definition.
    """
    declared_by_name = {field.name: field for field in cfg.schema_config.fields or ()}
    return tuple(declared_by_name[name] if name in declared_by_name else written.definition for name, written in cfg._written.items())
