"""Expand a payload-store text blob into pipeline rows, one per line or chunk."""

from __future__ import annotations

import codecs
import copy
import re
from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.transforms.blob_expand_contract import (
    BLOB_REF_FIELD_DESCRIPTION,
    DEFAULT_BLOB_REF_FIELD,
)

DEFAULT_MAX_OUTPUT_ROWS = 100_000
DEFAULT_MAX_BLOB_BYTES = 100 * 1024 * 1024
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")
_INVARIANT_PROBE_BLOB_REF = "0" * 64

# The record separators every line-oriented file tool recognises, and exactly
# the set ``csv.reader`` treats as record boundaries — so the two expanders in
# this family agree on where a text blob's lines end. Deliberately NARROWER
# than ``str.splitlines()``, which also cuts on vertical tab, form feed, the
# ASCII file/group/record separators, and three Unicode line breaks: those are
# Python-isms, and silently splitting a data file on a form feed is a surprise
# no file format asked for.
_NEWLINE_PATTERN = re.compile(r"\r\n|\r|\n")


class BlobTextExpandConfig(TransformDataConfig):
    """Configuration for blob_text_expand."""

    blob_ref_field: str = Field(default=DEFAULT_BLOB_REF_FIELD, description=BLOB_REF_FIELD_DESCRIPTION)
    output_field: str = Field(default="line", description="Output field receiving each emitted line or chunk.")
    include_index: bool = Field(default=True, description="Whether to emit the chunk's zero-based position within the blob.")
    # The index is the chunk's position IN THE BLOB, not the ordinal of the row
    # emitted. The two coincide unless skip_blank_lines drops something, and
    # when it does, the blob position is the one that is still useful: it stays
    # a true line number you can point at in the source file, so skipped lines
    # show up as a visible gap rather than being papered over. Do not "fix"
    # this into enumerate() over the emitted rows — that silently renumbers
    # every line after the first blank one.
    index_field: str = Field(default="line_index", description="Output field receiving the chunk's zero-based position within the blob.")
    delimiter: str | None = Field(
        default=None,
        description="Literal separator to split on. Omitted (the default) splits on CRLF, CR, or LF line endings.",
    )
    encoding: str = Field(default="utf-8", description="Encoding used to decode the text blob. Decoding is strict.")
    skip_blank_lines: bool = Field(default=False, description="Whether to drop empty chunks instead of emitting a row for each.")
    max_output_rows: int = Field(default=DEFAULT_MAX_OUTPUT_ROWS, gt=0, description="Maximum chunks read from one input blob.")
    max_blob_bytes: int = Field(default=DEFAULT_MAX_BLOB_BYTES, gt=0, description="Maximum payload size accepted from the blob store.")

    @field_validator("blob_ref_field")
    @classmethod
    def _reject_empty_blob_ref_field(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blob_ref_field must not be empty")
        return value.strip()

    @field_validator("output_field", "index_field")
    @classmethod
    def _validate_output_identifiers(cls, value: str, info: Any) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{info.field_name} must not be empty")
        if not stripped.isidentifier():
            raise ValueError(f"{info.field_name} must be a valid Python identifier, got {value!r}")
        return stripped

    @field_validator("delimiter")
    @classmethod
    def _reject_empty_delimiter(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("delimiter must not be the empty string; omit it to split on line endings")
        return value

    @field_validator("encoding")
    @classmethod
    def _validate_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unknown encoding: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _reject_field_collisions(self) -> BlobTextExpandConfig:
        if self.output_field == self.blob_ref_field:
            raise ValueError(f"output_field {self.output_field!r} collides with blob_ref_field")
        if self.include_index:
            if self.index_field == self.blob_ref_field:
                raise ValueError(f"index_field {self.index_field!r} collides with blob_ref_field")
            if self.index_field == self.output_field:
                raise ValueError(f"index_field and output_field must differ, both are {self.index_field!r}")
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.blob_ref_field})


def _is_payload_hash(value: str) -> bool:
    return len(value) == 64 and all(char in _SHA256_HEX_CHARS for char in value)


def _split_bounded(text: str, *, delimiter: str | None, max_chunks: int) -> list[str]:
    """Split ``text`` into AT MOST ``max_chunks + 1`` chunks.

    The bound is applied BY the split, not after it: a 100 MB blob of nothing
    but separators must not materialise 100 million chunks before the ceiling
    is consulted. Both arms therefore pass ``maxsplit=max_chunks``, so the
    result carries one chunk more than the ceiling allows — enough for the
    caller to know the ceiling was exceeded — with the tail left unsplit.
    A returned list longer than ``max_chunks`` therefore MEANS "too many", and
    its length is not the blob's true chunk count.

    A SINGLE trailing separator terminates the final chunk rather than opening
    an empty one, which is the universal convention for line-oriented files
    (and what ``str.splitlines()`` does). Empty text yields zero chunks, again
    matching ``"".splitlines()``.
    """
    if not text:
        return []

    if delimiter is None:
        if text.endswith("\r\n"):
            body = text[:-2]
        elif text.endswith("\n") or text.endswith("\r"):
            body = text[:-1]
        else:
            body = text
        return _NEWLINE_PATTERN.split(body, maxsplit=max_chunks)

    body = text[: -len(delimiter)] if text.endswith(delimiter) else text
    return body.split(delimiter, max_chunks)


def _blob_text_added_output_fields(cfg: BlobTextExpandConfig) -> tuple[FieldDefinition, ...]:
    fields = [FieldDefinition(name=cfg.output_field, field_type="str", required=True)]
    if cfg.include_index:
        fields.append(FieldDefinition(name=cfg.index_field, field_type="int", required=True))
    return tuple(fields)


def _build_blob_text_output_schema_config(schema_config: SchemaConfig, cfg: BlobTextExpandConfig) -> SchemaConfig:
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _blob_text_added_output_fields(cfg)
    field_by_name.update((field.name, field) for field in added_fields)

    base_guaranteed = set(schema_config.guaranteed_fields or ())
    output_guaranteed = base_guaranteed | {field.name for field in added_fields}

    return SchemaConfig(
        mode=schema_config.mode if schema_config.fields is not None else "flexible",
        fields=tuple(field_by_name.values()),
        guaranteed_fields=tuple(sorted(output_guaranteed)) if output_guaranteed else schema_config.guaranteed_fields,
        audit_fields=schema_config.audit_fields,
        required_fields=schema_config.required_fields,
    )


class BlobTextExpand(BaseTransform):
    """Decode a text blob from the payload store and emit one row per line or chunk."""

    # blob_ref_field is the INPUT column (a payload-store content hash); the
    # other two name emitted columns.
    output_naming_config_keys = frozenset({"output_field", "index_field"})
    name = "blob_text_expand"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:ddc21af6b54c67eb"
    config_model = BlobTextExpandConfig
    usage_when_to_use: str = (
        "Use when each input row carries a payload-store reference to a plain-text blob and you need "
        "one row per line, or per occurrence of a literal separator, with the upstream row fields kept."
    )
    usage_when_not_to_use: str = (
        "Not a file source and not a row-field splitter: use the text source for a pipeline input file, "
        "line_explode for text already sitting in a row field, and blob_csv_expand for CSV payloads."
    )
    example_use: str = """transform:
  plugin: blob_text_expand
  options:
    blob_ref_field: blob_ref
    output_field: line
    include_index: true
    index_field: line_index
    skip_blank_lines: false
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("text", "lines", "blob", "fan-out")
    creates_tokens = True
    passes_through_input = True

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        # The emitted column names must exceed 12 characters: the conftest
        # Hypothesis field-name generator caps at max_size=12, so a short name
        # can be generated onto the probe row and collide with a column this
        # transform creates. The collision would surface as a value-level
        # error, which ADR-009's forward invariant treats as a legitimate
        # processing error — a green that asserted nothing.
        return {
            "schema": {"mode": "observed"},
            "blob_ref_field": "blob_ref",
            "output_field": "blob_text_expand_line",
            "index_field": "blob_text_expand_line_index",
        }

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        cfg = BlobTextExpandConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        self._blob_ref_field = cfg.blob_ref_field
        self._output_field = cfg.output_field
        self._include_index = cfg.include_index
        self._index_field = cfg.index_field
        self._delimiter = cfg.delimiter
        self._encoding = cfg.encoding
        self._skip_blank_lines = cfg.skip_blank_lines
        self._max_output_rows = cfg.max_output_rows
        self._max_blob_bytes = cfg.max_blob_bytes

        self.declared_output_fields = frozenset(field.name for field in _blob_text_added_output_fields(cfg))

        self.input_schema = create_schema_from_config(cfg.schema_config, "BlobTextExpandInput", allow_coercion=False)
        self._output_schema_config = _build_blob_text_output_schema_config(cfg.schema_config, cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "BlobTextExpandOutput", allow_coercion=False)

        # Defence in depth, called LAST so the created set is populated. The
        # config validator above already refuses both collisions by name, and
        # today that validator always wins — but it can only compare the names
        # it knows, and this plugin's created set is only fully enumerable
        # because it has no list-valued column option. Add one (a `columns`, a
        # `fields`) and the validator silently stops covering the created set
        # while this call keeps doing so, because it reads the set live.
        self._reject_input_options_naming_created_fields({"blob_ref_field": cfg.blob_ref_field})

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Decode a payload-store text blob and emit one output row per line, preserving upstream fields.",
                composer_hints=(
                    "Use blob_text_expand after blob_fetch or another transform that emits a payload-store blob_ref.",
                    "For text already sitting in a row field there is nothing to fetch: use line_explode instead.",
                    "Decoding is strict — a blob that is not valid text under the configured encoding is quarantined, never repaired.",
                    "index_field is the chunk's position in the blob, so it stays a true line number when skip_blank_lines drops rows.",
                ),
            )
        return None

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject a deterministic payload-store reference for invariant probing."""
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
        """Drive the real process path with a hermetic text payload seam."""

        class _InvariantPayloadStore:
            def retrieve(self, ref: str) -> bytes:
                if ref != _INVARIANT_PROBE_BLOB_REF:
                    raise PayloadNotFoundError(ref)
                return b"blob_text_expand_probe_value\nprobe\n"

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
        if ctx.payload_store is None:
            raise FrameworkBugError("BlobTextExpand requires payload_store — orchestrator must configure it before on_start().")
        self._payload_store = ctx.payload_store

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        del ctx
        blob_ref = row[self._blob_ref_field]
        if type(blob_ref) is not str:
            raise TypeError(
                f"Field '{self._blob_ref_field}' must be a string payload-store hash, got {type(blob_ref).__name__}. "
                "This indicates an upstream validation bug."
            )
        if not _is_payload_hash(blob_ref):
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "error_type": "invalid_blob_ref",
                    "error": "payload-store hash must be 64 lowercase hex characters",
                },
                retryable=False,
            )

        try:
            body = self._payload_store.retrieve(blob_ref)
        except IntegrityError:
            # FIRST, and the order is load-bearing rather than stylistic.
            # `except` clauses match in source order, so a store error that is
            # BOTH kinds — a corrupt payload discovered while resolving a
            # reference — is caught by whichever clause comes first. With
            # blob_not_found first, tampering would be downgraded to a routed
            # value-level error and the row would quietly continue down the
            # on_error path, which is exactly what an integrity failure must
            # never do. Corruption always propagates.
            raise
        except PayloadNotFoundError as exc:
            return TransformResult.error(
                {
                    "reason": "blob_not_found",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "error": str(exc),
                },
                retryable=False,
            )

        body_size = len(body)
        if body_size > self._max_blob_bytes:
            return TransformResult.error(
                {
                    "reason": "blob_too_large",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "body_size": body_size,
                    "max_blob_bytes": self._max_blob_bytes,
                },
                retryable=False,
            )

        # Strict decoding, deliberately. errors="replace" would write U+FFFD
        # onto the row and hand a corrupted value downstream under a success
        # status; an undecodable blob is a quarantine case, not a repair case.
        try:
            text = body.decode(self._encoding)
        except UnicodeDecodeError as exc:
            return TransformResult.error(
                {
                    "reason": "decode_failed",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "encoding": self._encoding,
                    "error": str(exc),
                },
                retryable=False,
            )

        chunks = _split_bounded(text, delimiter=self._delimiter, max_chunks=self._max_output_rows)
        if len(chunks) > self._max_output_rows:
            return TransformResult.error(
                {
                    "reason": "too_many_rows",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "row_count": len(chunks),
                    "max_output_rows": self._max_output_rows,
                },
                retryable=False,
            )

        base = row.to_dict()
        # Checked only when this blob actually emits something: an expansion
        # that produces no rows overwrites nothing, so quarantining it for a
        # collision would report a conflict that never happened.
        collisions = sorted(self._colliding_fields(base)) if chunks else []
        if collisions:
            return TransformResult.error(
                {
                    "reason": "field_collision",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "fields": collisions,
                    "error": "emitted fields collide with existing input fields",
                },
                retryable=False,
            )

        output_rows: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            if self._skip_blank_lines and not chunk:
                continue
            output = copy.deepcopy(base)
            output[self._output_field] = chunk
            if self._include_index:
                output[self._index_field] = index
            output_rows.append(output)

        fields_added = [self._output_field] + ([self._index_field] if self._include_index else [])
        if not output_rows:
            # Zero rows is a FAIL STATE, not an answer: nothing downstream can
            # consume it. Note the distinction this guards — an empty ROW is
            # data (a line between two newlines is a row whose value is the
            # empty string, and it is emitted like any other), while an empty
            # CONTAINER is a failure to produce data. Only the latter reaches
            # here, so the row leaves through on_error rather than the
            # transform reporting success with nothing to show for it. No
            # blank row is synthesised to rescue it.
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "error_type": "empty_expansion",
                    "error": "text blob yielded no rows to expand",
                },
                retryable=False,
            )

        output_contract = narrow_contract_to_output(input_contract=row.contract, output_row=output_rows[0])
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        return TransformResult.success_multi(
            [PipelineRow(output, output_contract) for output in output_rows],
            success_reason={
                "action": "expanded_blob",
                "fields_added": fields_added,
                "metadata": {
                    "blob_ref": blob_ref,
                    "row_count": len(output_rows),
                },
            },
        )

    def _colliding_fields(self, base: Mapping[str, object]) -> set[str]:
        """Return the emitted column names already present on the input row."""
        emitted = {self._output_field}
        if self._include_index:
            emitted.add(self._index_field)
        return emitted & set(base)

    def close(self) -> None:
        pass
