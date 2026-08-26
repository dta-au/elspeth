"""Expand CSV into pipeline rows, from a payload-store blob or a row field."""

from __future__ import annotations

import codecs
import copy
import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.errors import FrameworkBugError, TransformErrorReason
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sources.field_normalization import ExternalHeaderError, resolve_field_names
from elspeth.plugins.transforms.blob_expand_contract import (
    BLOB_REF_FIELD_DESCRIPTION,
    DEFAULT_BLOB_REF_FIELD,
    DEFAULT_TEXT_FIELD,
    TEXT_FIELD_DESCRIPTION,
)

DEFAULT_MAX_OUTPUT_ROWS = 100_000
DEFAULT_MAX_BLOB_BYTES = 100 * 1024 * 1024
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")
_INVARIANT_PROBE_BLOB_REF = "0" * 64
# One probe document for both arms: the blob arm serves these bytes from a
# hermetic payload seam, the field arm carries the same text on the row.
_INVARIANT_PROBE_CSV_TEXT = "blob_csv_expand_probe_value\nprobe\n"


class BlobCSVExpandConfig(TransformDataConfig):
    """Configuration for blob_csv_expand."""

    # Defaults to None for the same load-bearing reason ``text_field`` does, one
    # arm over. ``_config_named_input_columns`` folds every ``*_field`` option's
    # STRING value into ``consumed_input_fields`` and is completely arm-unaware,
    # so a defaulted spelling leaked ``blob_ref`` on the INLINE arm, which never
    # reads it. That leak was not merely untidy: a CSV whose columns include one
    # called ``blob_ref`` then had that created column un-demoted and required on
    # input, rejecting every row (elspeth-d6eeb3a71d) from a config whose author
    # never wrote ``blob_ref_field`` at all. None names no column, so nothing
    # leaks; ``read_blob_ref_field`` restores the effective default at read time.
    blob_ref_field: str | None = Field(default=None, description=f"{BLOB_REF_FIELD_DESCRIPTION} Defaults to {DEFAULT_BLOB_REF_FIELD!r}.")
    source: Literal["blob", "field"] = Field(
        default="blob",
        description=(
            "Where the CSV text comes from: 'blob' retrieves bytes from the payload store using blob_ref_field; "
            "'field' reads CSV text already present on the row in text_field."
        ),
    )
    # Defaults to None rather than to DEFAULT_TEXT_FIELD, and that is load-bearing.
    # ``BaseTransform._config_named_input_columns`` folds every ``*_field`` option's
    # STRING value into ``consumed_input_fields``, and ``input_schema`` demotes only
    # created fields that are not consumed. A defaulted spelling would therefore
    # un-demote a CSV column called ``content`` on the blob arm — rejecting every row
    # for missing the field the transform exists to create (elspeth-d6eeb3a71d), from
    # a config that never mentions ``source``. None names no column, so nothing leaks.
    text_field: str | None = Field(default=None, description=f"{TEXT_FIELD_DESCRIPTION} Defaults to {DEFAULT_TEXT_FIELD!r}.")
    delimiter: str = Field(default=",", description="Single-character delimiter used to split CSV fields.")
    encoding: str = Field(default="utf-8", description="Encoding used to decode the CSV blob. Applies to source: blob only.")
    skip_rows: int = Field(default=0, ge=0, description="Number of leading CSV records to skip before reading headers or data.")
    columns: list[str] | None = Field(default=None, description="Explicit normalized column names for headerless CSV blobs.")
    field_mapping: dict[str, str] | None = Field(
        default=None, description="Optional mapping from observed CSV headers to normalized names."
    )
    include_row_index: bool = Field(default=True, description="Whether to emit the row index within the parsed CSV document.")
    row_index_field: str = Field(default="csv_row_index", description="Output field receiving the zero-based CSV data row index.")
    max_output_rows: int = Field(default=DEFAULT_MAX_OUTPUT_ROWS, gt=0, description="Maximum rows emitted for one input blob.")
    max_blob_bytes: int = Field(
        default=DEFAULT_MAX_BLOB_BYTES, gt=0, description="Maximum payload size accepted from the blob store. Applies to source: blob only."
    )

    @field_validator("blob_ref_field")
    @classmethod
    def _reject_empty_blob_ref_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("blob_ref_field must not be empty")
        return value.strip()

    @field_validator("text_field")
    @classmethod
    def _reject_empty_text_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("text_field must not be empty")
        return value.strip()

    @field_validator("delimiter")
    @classmethod
    def _validate_delimiter(cls, value: str) -> str:
        if len(value) != 1:
            raise ValueError(f"delimiter must be a single character, got {value!r}")
        return value

    @field_validator("encoding")
    @classmethod
    def _validate_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unknown encoding: {value!r}") from exc
        return value

    @field_validator("row_index_field")
    @classmethod
    def _validate_row_index_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("row_index_field must not be empty")
        if not stripped.isidentifier():
            raise ValueError(f"row_index_field must be a valid Python identifier, got {value!r}")
        return stripped

    @model_validator(mode="after")
    def _validate_normalization_options(self) -> BlobCSVExpandConfig:
        from elspeth.contracts.identifiers import validate_field_names

        if self.columns is not None:
            validate_field_names(self.columns, "columns")
        if self.field_mapping is not None and self.field_mapping:
            validate_field_names(list(self.field_mapping.values()), "field_mapping values")
        if self.include_row_index and self.row_index_field == self.named_blob_ref_field:
            raise ValueError(f"row_index_field {self.row_index_field!r} collides with blob_ref_field")
        # The same read-what-you-are-about-to-overwrite defect on the inline arm's
        # locator. ``named_text_field`` is None exactly when no column is named, and
        # ``row_index_field`` is always a non-empty string, so a config that never
        # mentions either option cannot be refused by this comparison.
        if self.include_row_index and self.row_index_field == self.named_text_field:
            raise ValueError(f"row_index_field {self.row_index_field!r} collides with text_field")
        if self.source == "field" and self.columns is not None and self.read_text_field in self.columns:
            raise ValueError(f"text_field {self.read_text_field!r} collides with a column declared in columns")
        # The created set is knowable HERE for every member that config can name:
        # the declared `columns` plus `row_index_field`. Checking it at config
        # time rather than only in __init__ is what keeps the two validation
        # paths in agreement — pre-validation runs the config model alone, so an
        # __init__-only guard would reject on the engine path while
        # validate_transform_config reported the config clean. pdf_rasterize
        # pairs its guard call with a model validator for exactly this reason;
        # __init__'s call remains, and covers any created field that is not
        # knowable until the schemas are built.
        creatable = set(self.columns or ())
        if self.include_row_index:
            creatable.add(self.row_index_field)
        for option, named in (("blob_ref_field", self.named_blob_ref_field), ("text_field", self.named_text_field)):
            if named is not None and named in creatable:
                raise ValueError(f"{option} {named!r} may not name a field blob_csv_expand creates")
        return self

    @property
    def read_text_field(self) -> str:
        """The column the inline arm reads its CSV text from."""
        return self.text_field or DEFAULT_TEXT_FIELD

    @property
    def named_text_field(self) -> str | None:
        """The text column this config actually NAMES, or None when it names none.

        Left unset on the blob arm the option names nothing: no column is read
        and none is declared, so a config written before ``source`` existed can
        neither acquire an input requirement nor collide with anything.
        """
        if self.source == "field" or self.text_field is not None:
            return self.read_text_field
        return None

    @property
    def read_blob_ref_field(self) -> str:
        """The column the blob arm reads its payload-store hash from."""
        return self.blob_ref_field or DEFAULT_BLOB_REF_FIELD

    @property
    def named_blob_ref_field(self) -> str | None:
        """The blob-reference column this config actually NAMES, or None.

        The mirror of ``named_text_field``. Left unset on the INLINE arm the
        option names nothing: no column is read and none is declared, so a
        ``source: field`` config cannot acquire a phantom ``blob_ref`` input
        requirement or collide with a CSV column that happens to share the name.
        """
        if self.source == "blob" or self.blob_ref_field is not None:
            return self.read_blob_ref_field
        return None

    @property
    def active_input_field(self) -> str:
        """The row column this config actually reads the CSV from."""
        return self.read_text_field if self.source == "field" else self.read_blob_ref_field

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.active_input_field})


@dataclass(frozen=True, slots=True)
class _ParsedCSV:
    rows: tuple[Mapping[str, object], ...]
    headers: tuple[str, ...]

    def __post_init__(self) -> None:
        freeze_fields(self, "rows")


@dataclass(frozen=True, slots=True)
class _CSVSourceText:
    """CSV text plus the audit identity of where it came from.

    Both arms produce one of these and hand ``text`` to the same parser, so the
    arm is visible only in the identity keys that reach error and audit records.
    """

    text: str
    field: str
    blob_ref: str | None


def _source_identity(loaded: _CSVSourceText) -> dict[str, object]:
    """Identity keys naming where a row's CSV came from, for error/audit records."""
    identity: dict[str, object] = {"field": loaded.field}
    if loaded.blob_ref is not None:
        identity["blob_ref"] = loaded.blob_ref
    return identity


class _BlobCSVParseError(Exception):
    def __init__(self, reason: TransformErrorReason) -> None:
        super().__init__(str(reason))
        self.reason = reason


def _csv_error_reason(reason: str, **details: object) -> TransformErrorReason:
    return cast(TransformErrorReason, {"reason": reason, **details})


def _is_payload_hash(value: str) -> bool:
    return len(value) == 64 and all(char in _SHA256_HEX_CHARS for char in value)


def _blob_csv_added_output_fields(cfg: BlobCSVExpandConfig) -> tuple[FieldDefinition, ...]:
    fields: list[FieldDefinition] = []
    if cfg.columns is not None:
        fields.extend(FieldDefinition(name=column, field_type="str", required=True) for column in cfg.columns)
    if cfg.include_row_index:
        fields.append(FieldDefinition(name=cfg.row_index_field, field_type="int", required=True))
    return tuple(fields)


def _build_blob_csv_output_schema_config(schema_config: SchemaConfig, cfg: BlobCSVExpandConfig) -> SchemaConfig:
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _blob_csv_added_output_fields(cfg)
    field_by_name.update((field.name, field) for field in added_fields)

    # The inline arm consumes its text column, so the OUTPUT schema must stop
    # advertising it. Dropping it only from the emitted rows is not enough: this
    # config is what build-time DAG validation compares against the consumer, so
    # leaving the field here makes the graph promise a column no row carries —
    # a downstream `mode: fixed` sink then fails to build with "Extra fields
    # forbidden by consumer", naming a field the author cannot legally declare.
    # `named_text_field` is None on the blob arm, which removes nothing there.
    removed_field = cfg.named_text_field if cfg.source == "field" else None
    if removed_field is not None:
        field_by_name.pop(removed_field, None)

    base_guaranteed = set(schema_config.guaranteed_fields or ()) - ({removed_field} if removed_field is not None else set())
    output_guaranteed = base_guaranteed | {field.name for field in added_fields}

    return SchemaConfig(
        mode=schema_config.mode if schema_config.fields is not None else "flexible",
        fields=tuple(field_by_name.values()),
        guaranteed_fields=tuple(sorted(output_guaranteed)) if output_guaranteed else schema_config.guaranteed_fields,
        audit_fields=schema_config.audit_fields,
        required_fields=schema_config.required_fields,
    )


class BlobCSVExpand(BaseTransform):
    """Parse a CSV blob and emit one output row per CSV data row."""

    # blob_ref_field is the INPUT column ("Input field containing a payload-store
    # content hash"); row_index_field names an emitted field.
    output_naming_config_keys = frozenset({"row_index_field"})
    name = "blob_csv_expand"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:be3347a2aca071a7"
    config_model = BlobCSVExpandConfig
    usage_when_to_use: str = (
        "Use when each input row carries a payload-store reference to a CSV blob and you need to "
        "expand its records into rows while retaining the upstream row fields."
    )
    usage_when_not_to_use: str = (
        "Not a file source or an arbitrary binary parser: use the csv source for a pipeline input "
        "file, and select a format-specific transform for non-CSV payloads."
    )
    example_use: str = """transform:
  plugin: blob_csv_expand
  options:
    blob_ref_field: blob_ref
    skip_rows: 1
    columns: [id, text]
    include_row_index: true
    row_index_field: csv_row_index
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("csv", "blob", "tabular", "fan-out")
    creates_tokens = True
    passes_through_input = True

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "schema": {"mode": "observed"},
            "blob_ref_field": "blob_ref",
        }

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        cfg = BlobCSVExpandConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        self._blob_ref_field = cfg.read_blob_ref_field
        self._source = cfg.source
        self._text_field = cfg.read_text_field
        self._delimiter = cfg.delimiter
        self._encoding = cfg.encoding
        self._skip_rows = cfg.skip_rows
        self._columns = tuple(cfg.columns) if cfg.columns is not None else None
        self._field_mapping = cfg.field_mapping
        self._include_row_index = cfg.include_row_index
        self._row_index_field = cfg.row_index_field
        self._max_output_rows = cfg.max_output_rows
        self._max_blob_bytes = cfg.max_blob_bytes

        self.declared_output_fields = frozenset(field.name for field in _blob_csv_added_output_fields(cfg))

        # The inline arm CONSUMES its text column: the whole CSV document sits
        # in that field, and copying it onto every emitted row multiplies the
        # source bytes by the expansion factor — into the audit payload store,
        # not just memory. So the column is dropped from the output, following
        # line_explode and json_explode, which consume a row field the same way.
        #
        # That makes `passes_through_input` FALSE on this arm, and the swap is
        # mandatory rather than stylistic: it is an all-or-nothing presence
        # contract, enforced per row by the executor
        # (engine/executors/pass_through.py, `divergence = input_fields -
        # runtime_observed`), and it honours NO removed_input_fields exemption.
        # Leaving it True while dropping a column raises a TIER_1
        # PassThroughContractViolation on every inline row. The pair below is
        # the declaration that CAN express "everything except this one column"
        # (elspeth-15c72686f2), which is why neither sibling declares
        # pass-through either.
        #
        # The blob arm is untouched: it reads a hash, not the document, so it
        # keeps the stronger class-level promise. `named_text_field` is the
        # arm-aware spelling and is None on the blob arm, so this cannot claim
        # to remove a column that arm never reads.
        removed_text_field = cfg.named_text_field if cfg.source == "field" else None
        if removed_text_field is not None:
            self.passes_through_input = False
            self.forwards_input_fields = True
            self.removed_input_fields = frozenset({removed_text_field})

        self.input_schema = create_schema_from_config(cfg.schema_config, "BlobCSVExpandInput", allow_coercion=False)
        self._output_schema_config = _build_blob_csv_output_schema_config(cfg.schema_config, cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "BlobCSVExpandOutput", allow_coercion=False)

        # LAST, after declared_output_fields is populated: the helper reads the
        # created set live, so an earlier call passes vacuously. This is what
        # covers the members of ``columns`` — the config validators above can
        # only compare the two options they know by name, while the created set
        # here includes every declared column. Both locators are passed in their
        # NAMED (arm-aware) form, and the helper skips None, so the option that
        # is inert on this arm cannot raise.
        self._reject_input_options_naming_created_fields({"blob_ref_field": cfg.named_blob_ref_field, "text_field": cfg.named_text_field})

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Expand a payload-store CSV blob into one output row per CSV data row while preserving upstream fields.",
                composer_hints=(
                    "Use blob_csv_expand after blob_fetch or another transform that emits a payload-store blob_ref.",
                    "The upstream URL or document id field is preserved on every emitted row for disambiguation.",
                    "CSV headers are normalized like the csv source; use columns for headerless blobs and field_mapping for overrides.",
                    "If a CSV column collides with an existing input field such as url, rename upstream or use field_mapping before expanding.",
                ),
            )
        return None

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject the configured arm's own input for invariant probing.

        The blob arm needs a deterministic payload-store reference; the inline
        arm needs the CSV text itself, since it never reaches a payload store.
        """
        if self._source == "field":
            return [
                self._augment_invariant_probe_row(
                    probe,
                    field_name=self._text_field,
                    value=_INVARIANT_PROBE_CSV_TEXT,
                )
            ]
        return [
            self._augment_invariant_probe_row(
                probe,
                field_name=self._blob_ref_field,
                value=_INVARIANT_PROBE_BLOB_REF,
            )
        ]

    def execute_forward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: TransformContext,
    ) -> TransformResult:
        """Drive the real process path with a hermetic CSV payload seam.

        The seam is installed for both arms: the inline arm simply never reaches
        it, and leaving the install unconditional keeps this hook's lifecycle
        (and its dynamic-attribute sites) identical for every configuration.
        """

        class _InvariantPayloadStore:
            def retrieve(self, ref: str) -> bytes:
                if ref != _INVARIANT_PROBE_BLOB_REF:
                    raise PayloadNotFoundError(ref)
                return _INVARIANT_PROBE_CSV_TEXT.encode("utf-8")

        had_payload_store = "_payload_store" in self.__dict__
        original_payload_store: Any = None
        if had_payload_store:
            original_payload_store = self.__dict__["_payload_store"]
        try:
            self.__dict__["_payload_store"] = _InvariantPayloadStore()
            return super().execute_forward_invariant_probe(probe_rows, ctx)
        finally:
            if had_payload_store:
                self.__dict__["_payload_store"] = original_payload_store
            else:
                delattr(self, "_payload_store")

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        # Only the blob arm has a payload store to require; demanding one for the
        # inline arm would make a node that never touches the store unrunnable.
        if self._source == "blob":
            if ctx.payload_store is None:
                raise FrameworkBugError("BlobCSVExpand requires payload_store — orchestrator must configure it before on_start().")
            self._payload_store = ctx.payload_store

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        del ctx
        base = row.to_dict()
        try:
            loaded = self._load_csv_text(row)
            parsed = self._parse_csv(loaded.text, base_fields=frozenset(base))
        except _BlobCSVParseError as exc:
            return TransformResult.error(exc.reason, retryable=False)

        # Drop the consumed source document AFTER parsing, and after the
        # collision check has seen it: the emitted rows must not carry the whole
        # document, but a CSV header colliding with the text column is still a
        # genuine collision on the row that arrived. Only the inline arm removes
        # anything — `self.removed_input_fields` is empty on the blob arm, so
        # this is a no-op there rather than an arm test repeated in two places.
        for removed in self.removed_input_fields:
            base.pop(removed, None)

        output_rows: list[dict[str, Any]] = []
        for row_index, csv_row in enumerate(parsed.rows):
            output = copy.deepcopy(base)
            output.update(csv_row)
            if self._include_row_index:
                output[self._row_index_field] = row_index
            output_rows.append(output)

        if not output_rows:
            return TransformResult.error(
                _csv_error_reason(
                    "empty_csv",
                    **_source_identity(loaded),
                    error="CSV blob had no data rows" if loaded.blob_ref is not None else "CSV text had no data rows",
                ),
                retryable=False,
            )

        first_keys = set(output_rows[0])
        for index, output_row in enumerate(output_rows[1:], start=1):
            row_keys = set(output_row)
            if row_keys != first_keys:
                raise ValueError(
                    f"Multi-row output has heterogeneous schema: row 0 has fields {sorted(first_keys)}, "
                    f"row {index} has fields {sorted(row_keys)}"
                )

        output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output_rows[0])
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        metadata: dict[str, Any] = {}
        if loaded.blob_ref is not None:
            metadata["blob_ref"] = loaded.blob_ref
        else:
            metadata["text_field"] = loaded.field
        metadata["row_count"] = len(output_rows)

        return TransformResult.success_multi(
            [PipelineRow(output, output_contract) for output in output_rows],
            success_reason={
                "action": "expanded_blob",
                "fields_added": sorted(set(parsed.headers) | ({self._row_index_field} if self._include_row_index else set())),
                "metadata": metadata,
            },
        )

    def _load_csv_text(self, row: PipelineRow) -> _CSVSourceText:
        """Produce the CSV text for the configured arm.

        This is the ONLY place the two arms differ: ``blob`` retrieves bytes
        from the payload store and decodes them, ``field`` reads text already
        sitting on the row. Everything downstream — normalization, collision
        detection, bounds, the error taxonomy — runs on the returned text, so
        the two arms cannot fork into two parsers.
        """
        if self._source == "field":
            return self._load_field_text(row)
        return self._load_blob_text(row)

    def _load_blob_text(self, row: PipelineRow) -> _CSVSourceText:
        blob_ref = row[self._blob_ref_field]
        if type(blob_ref) is not str:
            raise TypeError(
                f"Field '{self._blob_ref_field}' must be a string payload-store hash, got {type(blob_ref).__name__}. "
                "This indicates an upstream validation bug."
            )
        if not _is_payload_hash(blob_ref):
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "invalid_input",
                    field=self._blob_ref_field,
                    blob_ref=blob_ref,
                    error_type="invalid_blob_ref",
                    error="payload-store hash must be 64 lowercase hex characters",
                )
            )

        try:
            body = self._payload_store.retrieve(blob_ref)
        # ORDER IS LOAD-BEARING. A corrupt payload is not a missing payload: it
        # must crash the run rather than be quarantined as a routed row error,
        # because silently routing it would let a pipeline continue past
        # evidence that the store's contents no longer match their hashes.
        #
        # `IntegrityError` and `PayloadNotFoundError` are unrelated siblings
        # today, so this clause is currently inert wherever it sits — but
        # handlers match in ORDER, so if IntegrityError ever became a subclass
        # of PayloadNotFoundError, a `blob_not_found` clause listed first would
        # swallow it and emit exactly the routed error this clause exists to
        # prevent. Keeping it first makes the guard independent of a hierarchy
        # this plugin does not own.
        except IntegrityError:
            raise
        except PayloadNotFoundError as exc:
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "blob_not_found",
                    field=self._blob_ref_field,
                    blob_ref=blob_ref,
                    error=str(exc),
                )
            ) from exc

        body_size = len(body)
        if body_size > self._max_blob_bytes:
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "blob_too_large",
                    field=self._blob_ref_field,
                    blob_ref=blob_ref,
                    body_size=body_size,
                    max_blob_bytes=self._max_blob_bytes,
                )
            )

        try:
            text = body.decode(self._encoding)
        except UnicodeDecodeError as exc:
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "decode_failed",
                    field=self._blob_ref_field,
                    blob_ref=blob_ref,
                    encoding=self._encoding,
                    error=str(exc),
                )
            ) from exc

        return _CSVSourceText(text=text, field=self._blob_ref_field, blob_ref=blob_ref)

    def _load_field_text(self, row: PipelineRow) -> _CSVSourceText:
        # Read through ``row`` rather than the ``to_dict()`` copy: __getitem__
        # resolves an original header name to its normalized key via the
        # contract, which a raw dict lookup would silently miss.
        try:
            value = row[self._text_field]
        except KeyError as exc:
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "invalid_input",
                    field=self._text_field,
                    error_type="missing_text_field",
                    error=f"row carries no field {self._text_field!r} to read CSV text from",
                )
            ) from exc
        # Unlike blob_ref — a hash this pipeline's own blob_fetch wrote — the
        # text field holds whatever an upstream node put there, so a wrong type
        # is a data defect to quarantine, not a framework bug to crash on.
        if type(value) is not str:
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "invalid_input",
                    field=self._text_field,
                    error_type="invalid_text_field",
                    error=f"CSV text field must be a string, got {type(value).__name__}",
                )
            )
        if not value.strip():
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "invalid_input",
                    field=self._text_field,
                    error_type="empty_text_field",
                    error="CSV text field is empty",
                )
            )
        # max_blob_bytes is deliberately NOT applied here: the text is already
        # resident in the row, so refusing it after the fact protects nothing.
        # max_output_rows still bounds the fan-out below.
        return _CSVSourceText(text=value, field=self._text_field, blob_ref=None)

    def _parse_csv(self, text: str, *, base_fields: frozenset[str]) -> _ParsedCSV:
        stream = io.StringIO(text, newline="")
        reader = csv.reader(stream, delimiter=self._delimiter, strict=True)

        for skip_idx in range(self._skip_rows):
            try:
                if next(reader, None) is None:
                    raise _BlobCSVParseError(
                        _csv_error_reason(
                            "csv_exhausted_during_skip_rows",
                            skip_rows=self._skip_rows,
                            rows_skipped=skip_idx,
                        )
                    )
            except csv.Error as exc:
                raise _BlobCSVParseError(
                    _csv_error_reason(
                        "csv_parse_error",
                        phase="skip_rows",
                        row_number=skip_idx + 1,
                        line_number=reader.line_num,
                        error=str(exc),
                    )
                ) from exc

        def next_nonblank_record() -> list[str] | None:
            while True:
                values = next(reader, None)
                if values is None:
                    return None
                if values:
                    return values

        if self._columns is not None:
            raw_headers = None
        else:
            try:
                raw_headers = next_nonblank_record()
            except csv.Error as exc:
                raise _BlobCSVParseError(
                    _csv_error_reason(
                        "csv_parse_error",
                        phase="header",
                        line_number=reader.line_num,
                        error=str(exc),
                    )
                ) from exc
            if raw_headers is None:
                return _ParsedCSV(rows=(), headers=())

        try:
            field_resolution = resolve_field_names(
                raw_headers=raw_headers,
                field_mapping=self._field_mapping,
                columns=list(self._columns) if self._columns is not None else None,
            )
        except ExternalHeaderError as exc:
            raise _BlobCSVParseError(_csv_error_reason("csv_header_error", error=str(exc))) from exc
        except ValueError as exc:
            raise _BlobCSVParseError(_csv_error_reason("csv_config_error", error=str(exc))) from exc

        headers = tuple(field_resolution.final_headers)
        collisions = sorted(base_fields & set(headers))
        if self._include_row_index and self._row_index_field in base_fields:
            collisions.append(self._row_index_field)
        if self._include_row_index and self._row_index_field in headers:
            collisions.append(self._row_index_field)
        if collisions:
            raise _BlobCSVParseError(
                _csv_error_reason(
                    "field_collision",
                    fields=sorted(set(collisions)),
                    error="CSV output fields collide with existing input fields",
                )
            )

        expected_count = len(headers)
        rows: list[dict[str, Any]] = []
        row_number = 0
        while True:
            try:
                values = next_nonblank_record()
            except csv.Error as exc:
                raise _BlobCSVParseError(
                    _csv_error_reason(
                        "csv_parse_error",
                        phase="data",
                        line_number=reader.line_num,
                        row_number=row_number + 1,
                        error=str(exc),
                    )
                ) from exc
            if values is None:
                break
            row_number += 1
            if row_number > self._max_output_rows:
                raise _BlobCSVParseError(
                    _csv_error_reason(
                        "too_many_rows",
                        row_count=row_number,
                        max_output_rows=self._max_output_rows,
                    )
                )
            if len(values) != expected_count:
                raise _BlobCSVParseError(
                    _csv_error_reason(
                        "csv_column_count_mismatch",
                        line_number=reader.line_num,
                        row_number=row_number,
                        expected=expected_count,
                        actual=len(values),
                    )
                )
            rows.append(dict(zip(headers, values, strict=False)))

        return _ParsedCSV(rows=tuple(rows), headers=headers)

    def close(self) -> None:
        pass
