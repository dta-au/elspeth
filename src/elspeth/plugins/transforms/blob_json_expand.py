"""Expand a JSON document into pipeline rows.

The structural twin of ``blob_csv_expand``: same ``TransformDataConfig`` base,
same 1→N fan-out contract, same ``Determinism.IO_READ``. Two things differ.

**Two input arms.** ``source: blob`` (the default) reads bytes from the run
payload store via ``blob_ref_field``, exactly as ``blob_csv_expand`` does.
``source: field`` reads the JSON *text* straight out of a row field named by
``text_field``, with no payload store involved — closing the gap
``json_explode`` names in its own composer hint, that a JSON-looking STRING has
no parser at all. Both arms parse identically; only where the bytes come from
differs.

**Nested values pass through as real Python values.** A record's ``sections``
list arrives in the row as an actual sequence under a field typed ``any``, not
as re-serialized text. That is the only projection under which the target chain
composes, because ``json_explode`` requires a list-shaped value.
``PipelineRow`` deep-freezes on construction, so the emitted value is a
``tuple`` under ``row[name]`` and a ``list`` again under ``row.to_dict()`` —
``json_explode`` accepts both.

Blob bytes and row text are untrusted, so every parse failure is a value-level
error routed by ``on_error``, never a crash. The narrow typed ``except``
clauses below are the whole trust-boundary convention here: like
``blob_csv_expand`` and ``pdf_rasterize`` this plugin carries no
``@trust_boundary`` decorator, and there is no blanket ``except Exception``.
"""

from __future__ import annotations

import codecs
import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

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
from elspeth.plugins.sources.field_normalization import ExternalHeaderError, normalize_field_name
from elspeth.plugins.transforms.blob_expand_contract import (
    BLOB_CONTENT_TYPE_FIELD_DESCRIPTION,
    BLOB_REF_FIELD_DESCRIPTION,
    DEFAULT_BLOB_CONTENT_TYPE_FIELD,
    DEFAULT_BLOB_REF_FIELD,
    DEFAULT_TEXT_FIELD,
    TEXT_FIELD_DESCRIPTION,
)

DEFAULT_MAX_OUTPUT_ROWS = 100_000
DEFAULT_MAX_BLOB_BYTES = 100 * 1024 * 1024
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")
_INVARIANT_PROBE_BLOB_REF = "0" * 64

# Fail-closed content-type → format table. Anything absent from it — including
# text/plain — is refused with a message naming `format` as the remedy. Nothing
# guesses.
_FORMAT_BY_CONTENT_TYPE: Final[dict[str, str]] = {
    "application/json": "json",
    "application/jsonl": "jsonl",
    "application/x-ndjson": "jsonl",
    "text/jsonl": "jsonl",
}


class BlobJSONExpandConfig(TransformDataConfig):
    """Configuration for blob_json_expand."""

    source: Literal["blob", "field"] = Field(
        default="blob",
        description="Where the JSON text comes from: 'blob' reads the payload store via blob_ref_field, 'field' reads text_field.",
    )
    blob_ref_field: str | None = Field(default=None, description=f"{BLOB_REF_FIELD_DESCRIPTION} Defaults to {DEFAULT_BLOB_REF_FIELD!r}.")
    # These two default to None, not to their column names, and that is
    # load-bearing rather than stylistic. ``_config_named_input_columns``
    # (base.py) folds every column-naming option's STRING value into the
    # consumed-input set, reading the VALIDATED config — so an option the author
    # never wrote still contributes its default — and it is entirely arm-blind,
    # bypassing ``declared_input_fields`` below. A consumed column is never
    # demoted, so a str default here would make ``content`` a REQUIRED INPUT on
    # the blob arm the moment ``fields`` declared it: the field this transform
    # exists to CREATE, demanded of every arriving row (elspeth-d6eeb3a71d).
    # None names no column, so nothing leaks; the read-time names come from the
    # ``read_*`` properties below.
    content_type_field: str | None = Field(
        default=None, description=f"{BLOB_CONTENT_TYPE_FIELD_DESCRIPTION} Defaults to {DEFAULT_BLOB_CONTENT_TYPE_FIELD!r}."
    )
    text_field: str | None = Field(default=None, description=f"{TEXT_FIELD_DESCRIPTION} Defaults to {DEFAULT_TEXT_FIELD!r}.")
    format: Literal["json", "jsonl"] | None = Field(
        default=None,
        description=(
            "Parse format. Required when source is 'field'. When source is 'blob' and this is omitted, "
            "the format is inferred fail-closed from the blob's stored content type."
        ),
    )
    data_key: str | None = Field(
        default=None,
        description="Optional top-level object key containing the array of records. Omitted means the document's top level is the array.",
    )
    fields: list[str] = Field(
        ...,
        description=(
            "Record keys this transform emits as row fields, each typed 'any'. Required: JSON records carry no header row, "
            "so nothing else can tell downstream validation which fields the emitted rows will have."
        ),
    )
    field_mapping: dict[str, str] | None = Field(
        default=None, description="Optional mapping from normalized JSON record keys to final pipeline field names."
    )
    encoding: str = Field(default="utf-8", description="Encoding used to decode the JSON blob.")
    include_record_index: bool = Field(default=True, description="Whether to emit the record index within the parsed JSON document.")
    record_index_field: str = Field(default="json_record_index", description="Output field receiving the zero-based JSON record index.")
    max_output_rows: int = Field(default=DEFAULT_MAX_OUTPUT_ROWS, gt=0, description="Maximum rows emitted for one input document.")
    max_blob_bytes: int = Field(
        default=DEFAULT_MAX_BLOB_BYTES,
        gt=0,
        description=(
            "Maximum payload size accepted from the blob store. Applies to source='blob' only — a source='field' document is "
            "already resident in the row, so max_output_rows is its bound."
        ),
    )

    @field_validator("blob_ref_field", "text_field", "content_type_field")
    @classmethod
    def _reject_empty_input_field(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{info.field_name} must not be empty")
        return stripped

    @field_validator("encoding")
    @classmethod
    def _validate_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unknown encoding: {value!r}") from exc
        return value

    @field_validator("fields")
    @classmethod
    def _validate_fields(cls, value: list[str]) -> list[str]:
        from elspeth.contracts.identifiers import validate_field_names

        return list(validate_field_names(value, "fields", allow_empty_sequence=False))

    @field_validator("record_index_field")
    @classmethod
    def _validate_record_index_field(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("record_index_field must not be empty")
        if not stripped.isidentifier():
            raise ValueError(f"record_index_field must be a valid Python identifier, got {value!r}")
        return stripped

    @model_validator(mode="after")
    def _validate_expansion_options(self) -> BlobJSONExpandConfig:
        from elspeth.contracts.identifiers import validate_field_names

        if self.field_mapping is not None and self.field_mapping:
            validate_field_names(list(self.field_mapping.values()), "field_mapping values")

        # The 'field' arm has no content type to infer from, so `format` cannot
        # be derived and is not guessed.
        if self.source == "field" and self.format is None:
            raise ValueError("format is required when source is 'field' — an inline row field carries no content type to infer it from")

        # Same exclusion the json source enforces: JSONL reads line by line, so
        # there is no object root for data_key to select from.
        if self.format == "jsonl" and self.data_key is not None:
            raise ValueError(
                "data_key is not supported with format='jsonl' — JSONL reads line-by-line, data_key extracts from a JSON object root"
            )

        if self.include_record_index:
            if self.record_index_field == self.named_blob_ref_field:
                raise ValueError(f"record_index_field {self.record_index_field!r} collides with blob_ref_field")
            # named_text_field is None exactly when this config names no text
            # column, so a blob-arm config cannot collide with a name it does
            # not use.
            if self.record_index_field == self.named_text_field:
                raise ValueError(f"record_index_field {self.record_index_field!r} collides with text_field")
            if self.record_index_field in self.fields:
                raise ValueError(f"record_index_field {self.record_index_field!r} collides with a declared entry in fields")
        return self

    @model_validator(mode="after")
    def _reject_input_options_naming_created_fields(self) -> BlobJSONExpandConfig:
        """Config-path twin of the BaseTransform guard called from ``__init__``.

        ``validate_transform_config`` never CONSTRUCTS the transform, so a guard
        that lives only in ``__init__`` makes pre-validation report a config
        valid that the engine then rejects — the exact divergence
        ``tests/unit/plugins/test_validation_path_agreement.py`` exists to
        catch. ``pdf_rasterize`` pairs the two the same way (its
        ``_reject_field_name_collisions`` at :246 alongside the ``__init__``
        call at :434).

        The created set is DERIVED from the one function that builds the output
        fields rather than restated here, so a new emitted field cannot be added
        without this guard seeing it.
        """
        created = {field.name for field in _blob_json_added_output_fields(self)}
        offenders = [
            (option, column)
            for option, column in (
                ("blob_ref_field", self.named_blob_ref_field),
                ("content_type_field", self.named_content_type_field),
                ("text_field", self.named_text_field),
            )
            if column is not None and column in created
        ]
        if offenders:
            raise ValueError(
                "; ".join(f"{option} names {column!r}, which blob_json_expand itself creates" for option, column in offenders)
                + ". Point it at a column that ARRIVES on the row, or rename the created field."
            )
        return self

    @property
    def read_blob_ref_field(self) -> str:
        """The column the 'blob' arm reads, whether or not the author named it."""
        return self.blob_ref_field or DEFAULT_BLOB_REF_FIELD

    @property
    def named_blob_ref_field(self) -> str | None:
        """The blob-reference column this config actually NAMES, or None."""
        if self.source == "blob" or self.blob_ref_field is not None:
            return self.read_blob_ref_field
        return None

    @property
    def read_text_field(self) -> str:
        """The column the 'field' arm reads, whether or not the author named it."""
        return self.text_field or DEFAULT_TEXT_FIELD

    @property
    def named_text_field(self) -> str | None:
        """The text column this config actually NAMES, or None when it names none.

        The blob arm names none unless the author wrote one, which is what keeps
        the default out of the consumed-input set — see the field declaration.
        """
        if self.source == "field" or self.text_field is not None:
            return self.read_text_field
        return None

    @property
    def read_content_type_field(self) -> str:
        """The column the format inference reads, whether or not the author named it."""
        return self.content_type_field or DEFAULT_BLOB_CONTENT_TYPE_FIELD

    @property
    def named_content_type_field(self) -> str | None:
        """The content-type column this config actually NAMES, or None.

        Only the inferring blob arm reads one; an explicit ``format`` means the
        column is never consulted, so the config names none unless the author
        wrote one anyway.
        """
        if (self.source == "blob" and self.format is None) or self.content_type_field is not None:
            return self.read_content_type_field
        return None

    @property
    def declared_input_fields(self) -> frozenset[str]:
        inherited = super().declared_input_fields
        if self.source == "field":
            return inherited | frozenset({self.read_text_field})
        # content_type_field is only READ when the format has to be inferred.
        # Declaring it unconditionally would require a column this config never
        # consults; declaring it here is what makes "content type absent" a
        # build-time rejection rather than a per-row surprise.
        if self.format is None:
            return inherited | frozenset({self.read_blob_ref_field, self.read_content_type_field})
        return inherited | frozenset({self.read_blob_ref_field})


@dataclass(frozen=True, slots=True)
class _ParsedJSON:
    records: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        freeze_fields(self, "records")


class _BlobJSONParseError(Exception):
    def __init__(self, reason: TransformErrorReason) -> None:
        super().__init__(str(reason))
        self.reason = reason


def _json_error_reason(reason: str, **details: object) -> TransformErrorReason:
    return cast(TransformErrorReason, {"reason": reason, **details})


def _is_payload_hash(value: str) -> bool:
    return len(value) == 64 and all(char in _SHA256_HEX_CHARS for char in value)


def _blob_json_added_output_fields(cfg: BlobJSONExpandConfig) -> tuple[FieldDefinition, ...]:
    # Every declared field is typed 'any': the real type is unknown until
    # untrusted bytes are parsed, and a nested list or object is not describable
    # in the schema DSL beyond 'any'. Downstream nodes needing a concrete type
    # use type_coerce.
    fields: list[FieldDefinition] = [FieldDefinition(name=name, field_type="any", required=True) for name in cfg.fields]
    if cfg.include_record_index:
        fields.append(FieldDefinition(name=cfg.record_index_field, field_type="int", required=True))
    return tuple(fields)


def _build_blob_json_output_schema_config(schema_config: SchemaConfig, cfg: BlobJSONExpandConfig) -> SchemaConfig:
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _blob_json_added_output_fields(cfg)
    field_by_name.update((field.name, field) for field in added_fields)

    # The inline arm consumes its text column, so the OUTPUT schema must stop
    # advertising it. Dropping it only from the emitted rows is not enough: this
    # config is what the executor checks the emitted row against, and what
    # build-time DAG validation compares against the consumer. Leaving the field
    # here makes the node promise a column no row carries — the run dies with
    # SchemaConfigModeViolation ("missing required fields") on the first row, and
    # a downstream `mode: fixed` sink cannot be declared consistently either way.
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


class BlobJSONExpand(BaseTransform):
    """Parse a JSON document and emit one output row per record."""

    # blob_ref_field / content_type_field / text_field are INPUT columns;
    # record_index_field and every entry of `fields` name EMITTED columns.
    # `fields` must be listed: is_column_naming_config_option (base.py:91)
    # matches the bare name "fields", so without this declaration every entry
    # is classified as a column this transform READS and stays REQUIRED on
    # input — a contract no row can satisfy, since the transform is what
    # creates them. blob_csv_expand needs no equivalent entry for `columns`
    # only because that name is not in the classifier's vocabulary.
    output_naming_config_keys = frozenset({"record_index_field", "fields"})
    name = "blob_json_expand"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:c0289f7e87569f99"
    config_model = BlobJSONExpandConfig
    usage_when_to_use: str = (
        "Use when a row carries a JSON document — either a payload-store reference from blob_fetch or JSON text in a "
        "row field — and you need one row per record, with nested values preserved as real lists and objects."
    )
    usage_when_not_to_use: str = (
        "Not a file source and not an array exploder: use the json source for a pipeline input file, and json_explode "
        "when the array already sits in a row field as a real list."
    )
    example_use: str = """transform:
  plugin: blob_json_expand
  options:
    source: blob
    blob_ref_field: blob_ref
    content_type_field: blob_content_type
    data_key: documents
    fields: [document_id, title, sections]
    include_record_index: true
    record_index_field: json_record_index
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("json", "blob", "structured", "fan-out")
    creates_tokens = True
    passes_through_input = True

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        # The blob arm, with `format` left to inference, so the probe drives the
        # content-type path as well as the payload-store seam. Record keys are
        # deliberately longer than 12 characters: the conftest Hypothesis
        # field-name generator caps at max_size=12 and shorter keys collide into
        # bogus "dropped field" failures.
        return {
            "schema": {"mode": "observed"},
            "source": "blob",
            "blob_ref_field": "blob_ref",
            "content_type_field": "blob_content_type",
            "data_key": "probe_documents",
            "fields": ["probe_document_identifier", "probe_section_titles"],
        }

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        cfg = BlobJSONExpandConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        self._source = cfg.source
        self._blob_ref_field = cfg.read_blob_ref_field
        self._content_type_field = cfg.read_content_type_field
        self._text_field = cfg.read_text_field
        self._format = cfg.format
        self._data_key = cfg.data_key
        self._fields = tuple(cfg.fields)
        self._field_mapping = cfg.field_mapping
        self._encoding = cfg.encoding
        self._include_record_index = cfg.include_record_index
        self._record_index_field = cfg.record_index_field
        self._max_output_rows = cfg.max_output_rows
        self._max_blob_bytes = cfg.max_blob_bytes

        self.declared_output_fields = frozenset(field.name for field in _blob_json_added_output_fields(cfg))

        # The inline arm CONSUMES its text column: the whole JSON document sits
        # in that field, and copying it onto every emitted row multiplies the
        # source bytes by the expansion factor — into the audit payload store,
        # not just memory.
        #
        # That makes `passes_through_input` FALSE on this arm, and the swap is
        # mandatory rather than stylistic. The two declarations are ALTERNATIVES,
        # not layers: `passes_through_input` is an all-or-nothing presence
        # contract enforced per row by the executor
        # (engine/executors/pass_through.py:109, `divergence = input_fields -
        # runtime_observed`), and it honours NO removed_input_fields exemption.
        # Leaving it True while dropping a column raises a TIER_1
        # PassThroughContractViolation — a crash, not a routed row error — on
        # every inline row. The pair below is the weaker declaration that CAN
        # express "everything except this one column" (elspeth-15c72686f2),
        # which is why line_explode and json_explode declare no pass-through
        # either.
        #
        # The blob arm is untouched: it reads a hash, not the document, so it
        # keeps the stronger class-level promise. `named_text_field` is the
        # arm-aware spelling and is None on the blob arm, so this cannot claim
        # to remove a column that arm never reads. Assigning to `self` rebinds
        # per INSTANCE, leaving the class attribute — and every blob-arm
        # instance — alone.
        removed_text_field = cfg.named_text_field if cfg.source == "field" else None
        if removed_text_field is not None:
            self.passes_through_input = False
            self.forwards_input_fields = True
            self.removed_input_fields = frozenset({removed_text_field})

        self.input_schema = create_schema_from_config(cfg.schema_config, "BlobJSONExpandInput", allow_coercion=False)
        self._output_schema_config = _build_blob_json_output_schema_config(cfg.schema_config, cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "BlobJSONExpandOutput", allow_coercion=False)
        # Nothing else rejects an INPUT option aimed at a column this transform
        # CREATES: the executor's collision check needs a row to actually carry
        # the column, and mode:observed declares nothing for DAG validation to
        # carry. The consequence is that the field lands in consumed_input_fields,
        # never demotes, and stays required on input — every row rejected for
        # missing what the transform exists to create. The named_* properties
        # yield None on the arm that does not read them, so an option the author
        # never wrote cannot be blamed for a collision it did not cause.
        self._reject_input_options_naming_created_fields(
            {
                "blob_ref_field": cfg.named_blob_ref_field,
                "content_type_field": cfg.named_content_type_field,
                "text_field": cfg.named_text_field,
            }
        )

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Expand a JSON document into one output row per record while preserving upstream fields and nested values.",
                composer_hints=(
                    "Use blob_json_expand after blob_fetch (source: blob) for a fetched JSON document, or source: field with "
                    "text_field to parse JSON text already sitting in a row field.",
                    "fields is required and lists the record keys to emit; each becomes a required output field typed 'any'.",
                    "A nested array survives as a real list, so json_explode can be chained after this transform to fan out over it.",
                    "With source: blob the format is inferred from the stored content type; set format explicitly for anything else, "
                    "and format is always required with source: field.",
                    "data_key selects a top-level object key holding the record array; omit it when the document's top level is the array.",
                ),
            )
        return None

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject a deterministic blob reference and content type for probing."""
        with_ref = self._augment_invariant_probe_row(
            probe,
            field_name=self._blob_ref_field,
            value=_INVARIANT_PROBE_BLOB_REF,
        )
        return [
            self._augment_invariant_probe_row(
                with_ref,
                field_name=self._content_type_field,
                value="application/json",
            )
        ]

    def execute_forward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: TransformContext,
    ) -> TransformResult:
        """Drive the real process path with a hermetic JSON payload seam."""

        class _InvariantPayloadStore:
            def retrieve(self, ref: str) -> bytes:
                if ref != _INVARIANT_PROBE_BLOB_REF:
                    raise PayloadNotFoundError(ref)
                return (
                    b'{"probe_documents": [{"probe_document_identifier": "blob_json_expand_probe_value", '
                    b'"probe_section_titles": ["probe_section"]}]}'
                )

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
        if self._source != "blob":
            return
        if ctx.payload_store is None:
            raise FrameworkBugError("BlobJSONExpand requires payload_store — orchestrator must configure it before on_start().")
        self._payload_store = ctx.payload_store

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        del ctx
        base = row.to_dict()
        # Locating the document and parsing it share one fault channel, so every
        # untrusted-input failure leaves through a single quarantine. `origin`
        # carries only keys TransformErrorReason declares, so it folds into a
        # parse error's reason as well as into audit metadata; it stays empty
        # until the document is located, because a load failure already names
        # the field it failed on.
        origin: dict[str, str] = {}
        try:
            text, parse_format, origin = self._load_document(row)
            parsed = self._parse_json(text, parse_format=parse_format, base_fields=frozenset(base))
        except _BlobJSONParseError as exc:
            return TransformResult.error(cast(TransformErrorReason, {**exc.reason, **origin}), retryable=False)

        # Drop the consumed source document AFTER parsing, and after the
        # collision check has seen it: the emitted rows must not carry the whole
        # document, but a record key colliding with the text column is still a
        # genuine collision on the row that arrived. Only the inline arm removes
        # anything — `self.removed_input_fields` is empty on the blob arm, so
        # this is a no-op there rather than an arm test repeated in two places.
        for removed in self.removed_input_fields:
            base.pop(removed, None)

        output_rows: list[dict[str, Any]] = []
        for record_index, record in enumerate(parsed.records):
            output = copy.deepcopy(base)
            output.update(record)
            if self._include_record_index:
                output[self._record_index_field] = record_index
            output_rows.append(output)

        if not output_rows:
            return self._empty_document_result(origin=origin)

        fields_added = sorted(set(self._fields) | ({self._record_index_field} if self._include_record_index else set()))

        output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output_rows[0])
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        return TransformResult.success_multi(
            [PipelineRow(output, output_contract) for output in output_rows],
            success_reason={
                "action": "expanded_blob",
                "fields_added": fields_added,
                "metadata": {**origin, "source": self._source, "row_count": len(output_rows)},
            },
        )

    def _empty_document_result(self, *, origin: dict[str, str]) -> TransformResult:
        """Quarantine a document whose record array holds no records.

        Zero rows cannot be used for anything downstream, so an empty CONTAINER
        is a fail state, not an outcome. The empty ROW is the opposite case and
        stays a success: a record carrying every declared field with empty or
        null values is data — the JSON equivalent of a CSV ``,,,`` line — and it
        emits one row like any other. Same call ``blob_csv_expand`` makes for an
        empty CSV (:351-360).
        """
        return TransformResult.error(
            cast(
                TransformErrorReason,
                {
                    **origin,
                    "reason": "invalid_input",
                    "error_type": "empty_document",
                    "error": "JSON document contained no records",
                },
            ),
            retryable=False,
        )

    def _load_document(self, row: PipelineRow) -> tuple[str, str, dict[str, str]]:
        """Locate this row's JSON text and the format to parse it with."""
        if self._source == "field":
            return self._load_field_text(row)
        return self._load_blob_text(row)

    def _load_field_text(self, row: PipelineRow) -> tuple[str, str, dict[str, str]]:
        """Read JSON text straight from a row field. No payload store involved."""
        text = row[self._text_field]
        if type(text) is not str:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "type_mismatch",
                    field=self._text_field,
                    error=f"source='field' requires {self._text_field!r} to hold JSON text",
                    expected="str",
                    actual=type(text).__name__,
                )
            )
        # `format` is mandatory on this arm (config validator), so it is set.
        if self._format is None:
            raise FrameworkBugError(
                "BlobJSONExpand source='field' reached process() without a format — config validation should forbid it."
            )
        return text, self._format, {"field": self._text_field}

    def _load_blob_text(self, row: PipelineRow) -> tuple[str, str, dict[str, str]]:
        """Read bytes from the payload store and decode them."""
        blob_ref = row[self._blob_ref_field]
        if type(blob_ref) is not str:
            raise TypeError(
                f"Field '{self._blob_ref_field}' must be a string payload-store hash, got {type(blob_ref).__name__}. "
                "This indicates an upstream validation bug."
            )
        origin: dict[str, str] = {"blob_ref": blob_ref, "field": self._blob_ref_field}
        if not _is_payload_hash(blob_ref):
            raise _BlobJSONParseError(
                _json_error_reason(
                    "invalid_input",
                    field=self._blob_ref_field,
                    blob_ref=blob_ref,
                    error_type="invalid_blob_ref",
                    error="payload-store hash must be 64 lowercase hex characters",
                )
            )

        resolved = self._resolve_blob_format(row)

        try:
            body = self._payload_store.retrieve(blob_ref)
        except IntegrityError:
            # FIRST, ahead of the absence handler. The payload store returning
            # bytes that fail their own hash is ELSPETH's storage being corrupt,
            # not bad external data — Tier 1, so it crashes the run rather than
            # quarantining one row. The two are unrelated Exception siblings
            # today (contracts/payload_store.py), which makes the order inert
            # right now; ordering it first is what keeps this clause able to do
            # its job if the hierarchy ever changes, instead of the absence
            # handler swallowing corruption and reporting `blob_not_found`.
            raise
        except PayloadNotFoundError as exc:
            raise _BlobJSONParseError(
                _json_error_reason("blob_not_found", field=self._blob_ref_field, blob_ref=blob_ref, error=str(exc))
            ) from exc

        body_size = len(body)
        if body_size > self._max_blob_bytes:
            raise _BlobJSONParseError(
                _json_error_reason(
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
            raise _BlobJSONParseError(
                _json_error_reason("decode_failed", field=self._blob_ref_field, blob_ref=blob_ref, encoding=self._encoding, error=str(exc))
            ) from exc

        return text, resolved, origin

    def _resolve_blob_format(self, row: PipelineRow) -> str:
        """Infer the parse format from the stored content type, fail-closed."""
        if self._format is not None:
            return self._format

        # content_type_field is a declared input field whenever format is
        # omitted, so a pipeline that cannot guarantee the column is rejected at
        # build time. A row that still arrives without it is refused here rather
        # than guessed at.
        if self._content_type_field not in row:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "unsupported_content_type",
                    field=self._content_type_field,
                    error=f"no content type on the row to infer the parse format from — set the 'format' option on {self.name}",
                )
            )
        content_type = row[self._content_type_field]
        if type(content_type) is not str:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "unsupported_content_type",
                    field=self._content_type_field,
                    error=f"content type must be a string to infer the parse format — set the 'format' option on {self.name}",
                    expected="str",
                    actual=type(content_type).__name__,
                )
            )
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in _FORMAT_BY_CONTENT_TYPE:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "unsupported_content_type",
                    field=self._content_type_field,
                    content_type=content_type,
                    error=(
                        f"content type {normalized!r} does not identify a JSON format "
                        f"(recognised: {sorted(_FORMAT_BY_CONTENT_TYPE)}) — set the 'format' option on {self.name}"
                    ),
                )
            )
        return _FORMAT_BY_CONTENT_TYPE[normalized]

    def _parse_json(self, text: str, *, parse_format: str, base_fields: frozenset[str]) -> _ParsedJSON:
        elements = self._load_elements(text, parse_format=parse_format)

        if len(elements) > self._max_output_rows:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "too_many_rows",
                    row_count=len(elements),
                    max_output_rows=self._max_output_rows,
                )
            )

        collisions = sorted(base_fields & set(self._fields))
        if self._include_record_index and self._record_index_field in base_fields:
            collisions.append(self._record_index_field)
        if collisions:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "field_collision",
                    fields=sorted(set(collisions)),
                    error="JSON output fields collide with existing input fields",
                )
            )

        records = tuple(self._project_record(element, record_number=index + 1) for index, element in enumerate(elements))
        return _ParsedJSON(records=records)

    def _load_elements(self, text: str, *, parse_format: str) -> tuple[Any, ...]:
        """Decode the document down to the record array."""
        if parse_format == "jsonl":
            if self._data_key is not None:
                raise _BlobJSONParseError(
                    _json_error_reason(
                        "invalid_input",
                        error_type="data_key_with_jsonl",
                        error=(
                            "data_key selects a key from a JSON object root, and JSONL has none — the content type "
                            f"resolved the format to 'jsonl'. Set the 'format' option on {self.name} explicitly, or drop data_key."
                        ),
                    )
                )
            elements: list[Any] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    elements.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise _BlobJSONParseError(
                        _json_error_reason("invalid_json", phase="jsonl_line", line_number=line_number, error=str(exc))
                    ) from exc
            return tuple(elements)

        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _BlobJSONParseError(_json_error_reason("invalid_json", phase="document", line_number=exc.lineno, error=str(exc))) from exc

        if self._data_key is not None:
            if type(document) is not dict:
                raise _BlobJSONParseError(
                    _json_error_reason(
                        "invalid_input",
                        error_type="data_key_root_not_object",
                        error=f"cannot extract data_key {self._data_key!r}: expected a JSON object at the top level",
                        expected="object",
                        actual=type(document).__name__,
                    )
                )
            if self._data_key not in document:
                raise _BlobJSONParseError(
                    _json_error_reason(
                        "invalid_input",
                        error_type="data_key_not_found",
                        error=f"data_key {self._data_key!r} not found in the JSON object",
                        available_fields=sorted(str(key) for key in document),
                    )
                )
            document = document[self._data_key]

        if type(document) is not list:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "invalid_input",
                    error_type="records_not_array",
                    error=(
                        "data_key did not select a JSON array"
                        if self._data_key is not None
                        else "the document's top level is not a JSON array"
                    ),
                    expected="array",
                    actual=type(document).__name__,
                )
            )
        return tuple(document)

    def _project_record(self, element: Any, *, record_number: int) -> Mapping[str, Any]:
        """Normalize one record's keys and project it onto the declared fields.

        Projecting to exactly ``fields`` is what makes the emitted rows
        homogeneous by construction. Records legitimately carry per-record extra
        keys, and a heterogeneous emitted key set is a hard ValueError in the
        multi-row path, not a quarantine — so the extras are dropped here rather
        than crashing the run several lines later.
        """
        if type(element) is not dict:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "invalid_input",
                    error_type="record_not_object",
                    error="every element of the record array must be a JSON object",
                    row_number=record_number,
                    expected="object",
                    actual=type(element).__name__,
                )
            )

        normalized: dict[str, Any] = {}
        for key, value in element.items():
            if type(key) is not str:
                raise _BlobJSONParseError(
                    _json_error_reason(
                        "invalid_input",
                        error_type="record_key_not_string",
                        error="record keys must be strings",
                        row_number=record_number,
                        actual=type(key).__name__,
                    )
                )
            try:
                candidate = normalize_field_name(key)
            except ExternalHeaderError as exc:
                raise _BlobJSONParseError(
                    _json_error_reason("invalid_input", error_type="record_key_unnormalizable", error=str(exc), row_number=record_number)
                ) from exc
            final_name = self._field_mapping[candidate] if self._field_mapping and candidate in self._field_mapping else candidate
            if final_name in normalized:
                raise _BlobJSONParseError(
                    _json_error_reason(
                        "field_collision",
                        fields=[final_name],
                        error=f"two record keys normalize to {final_name!r}",
                        row_number=record_number,
                    )
                )
            normalized[final_name] = value

        missing = [name for name in self._fields if name not in normalized]
        if missing:
            raise _BlobJSONParseError(
                _json_error_reason(
                    "missing_field",
                    fields=missing,
                    error="record does not carry every field declared in fields",
                    row_number=record_number,
                    available_fields=sorted(normalized),
                )
            )
        return {name: normalized[name] for name in self._fields}

    def close(self) -> None:
        pass
