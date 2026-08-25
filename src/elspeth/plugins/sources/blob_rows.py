"""blob_rows — emit one row per configured managed-blob reference.

A generic source for binary-document (and other payload-store) workflows:
each configured entry becomes exactly one row carrying the blob's custody
identity and bounded metadata. The source never parses, decodes, or copies
blob content — ``blob_ref`` is the content-addressed ``PayloadStore``
reference a consuming transform (e.g. ``aws_textract_inline_analysis`` or
``pdf_rasterize``) uses for integrity-checked retrieval immediately before
its external call (elspeth-0c6a343921).

Authority boundaries: for web-authored pipelines every entry is populated
by the trusted plural blob resolver (``set_source_from_blobs``), never
copied from LLM assertions, and web execution re-resolves each entry
against the session's authoritative blob record at run admission. Outside
the web service the trusted CLI operator supplies the entries; ``blob_id``
is then retained as provenance, not proof of session ownership, and the
source validates that every payload ref exists in the configured
``PayloadStore`` before emitting any row.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elspeth.contracts import Determinism, PluginSchema, SourceRow
from elspeth.contracts.blobs import STORAGE_MIME_TYPES
from elspeth.contracts.contexts import LifecycleContext, SourceContext
from elspeth.contracts.contract_builder import ContractBuilder
from elspeth.contracts.payload_store import PayloadStore
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema_contract_factory import create_contract_from_config
from elspeth.plugins.infrastructure.base import BaseSource
from elspeth.plugins.infrastructure.config_base import DataPluginConfig
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BLOB_ID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

_MAX_BLOBS = 1_000

_ROW_FIELDS = ("blob_id", "blob_ref", "blob_filename", "blob_mime_type", "blob_size_bytes")


class BlobRowsEntry(BaseModel):
    """One authoritative blob reference persisted in source options."""

    model_config = ConfigDict(extra="forbid")

    blob_id: str = Field(description="Web custody identity (UUID) of the managed blob.")
    payload_ref: str = Field(description="64-lowercase-hex SHA-256 content hash in the payload store.")
    filename: str = Field(min_length=1, max_length=512, description="Blob filename recorded at admission.")
    mime_type: str = Field(description="Declared storage MIME type recorded at admission.")
    size_bytes: int = Field(ge=0, description="Blob byte length recorded at admission.")

    @field_validator("blob_id")
    @classmethod
    def _validate_blob_id(cls, v: str) -> str:
        if _BLOB_ID_PATTERN.fullmatch(v) is None:
            raise ValueError(f"blob_id must be a UUID string, got {v!r}")
        return v.lower()

    @field_validator("payload_ref")
    @classmethod
    def _validate_payload_ref(cls, v: str) -> str:
        if _PAYLOAD_REF_PATTERN.fullmatch(v) is None:
            raise ValueError("payload_ref must be 64 lowercase hex characters")
        return v

    @field_validator("mime_type")
    @classmethod
    def _validate_mime_type(cls, v: str) -> str:
        if v not in STORAGE_MIME_TYPES:
            raise ValueError(f"mime_type must be one of {sorted(STORAGE_MIME_TYPES)}, got {v!r}")
        return v


class BlobRowsSourceConfig(DataPluginConfig):
    """Configuration for the blob_rows source."""

    _plugin_component_type: ClassVar[str | None] = "source"

    on_validation_failure: str = Field(
        ...,
        description="Sink name for non-conformant rows, or 'discard' for explicit drop",
    )
    blobs: list[BlobRowsEntry] = Field(
        min_length=1,
        max_length=_MAX_BLOBS,
        description="Authoritative blob references, in authoring order (becomes source-row order).",
    )

    @field_validator("blobs")
    @classmethod
    def _validate_unique(cls, v: list[BlobRowsEntry]) -> list[BlobRowsEntry]:
        blob_ids = [entry.blob_id for entry in v]
        if len(set(blob_ids)) != len(blob_ids):
            raise ValueError("blobs entries must have unique blob_id values")
        payload_refs = [entry.payload_ref for entry in v]
        if len(set(payload_refs)) != len(payload_refs):
            raise ValueError("blobs entries must have unique payload_ref values")
        return v


class BlobRowsSource(BaseSource):
    """Emit one five-field custody row per configured managed blob.

    Config options:
        blobs: Non-empty bounded list of authoritative blob references
        schema: Schema configuration (required, via DataPluginConfig)
        on_validation_failure: Quarantine routing for non-conformant rows

    Every row has exactly the five fixed fields ``blob_id``, ``blob_ref``,
    ``blob_filename``, ``blob_mime_type``, and ``blob_size_bytes`` — field
    names are fixed; there are no renaming options.
    """

    name = "blob_rows"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:30fac7a29eeb8e48"
    config_model = BlobRowsSourceConfig
    # DESIGN DEVIATION (recorded for adjudication): the approved design lists
    # ``creates_tokens = True``, but that attribute exists only on the
    # TRANSFORM contract (BaseTransform/TransformProtocol) — no consumer
    # reads it off a source, and sources tokenize implicitly (one token per
    # emitted SourceRow). Declaring it here would be a dead attribute.
    _on_validation_failure: str

    usage_when_to_use: str = (
        "Use to turn managed session blobs (for example pasted images held under blob custody) into rows: one blob becomes "
        "one row carrying its payload hash and bounded metadata, in authoring order. Downstream transforms retrieve the "
        "bytes by content hash; rows never contain document content or base64."
    )
    usage_when_not_to_use: str = (
        "Do not use to parse blob content into records — bind a csv/json/text source for data blobs. Do not hand-author "
        "entries in the web composer; bind blobs through set_source_from_blobs so every field is resolved from the "
        "session's authoritative records."
    )
    example_use: str = """sources:
  documents:
    plugin: blob_rows
    on_success: documents
    options:
      blobs:
        - blob_id: 11111111-1111-1111-1111-111111111111
          payload_ref: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
          filename: page-1.png
          mime_type: image/png
          size_bytes: 183421
      schema: {mode: observed}
      on_validation_failure: discard
"""
    capability_tags: tuple[str, ...] = ("blob", "payload", "binary", "source", "batch")

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Emits one custody row (payload hash + bounded metadata) per managed blob, without copying content.",
                composer_hints=(
                    "Pasted binary documents become managed blobs, not inline base64; one blob produces one source row.",
                    "Bind blobs with set_source_from_blobs — never author or edit the blobs list directly.",
                    "Multiple blobs preserve their binding order as the source-row order.",
                    "Rows carry blob_ref (the payload hash) for downstream retrieval; they never contain document bytes.",
                    "Mixed document formats need homogeneous sources or branches — one explicitly configured "
                    "consumer per format; a multipage PDF can be split into PNG pages by pdf_rasterize.",
                ),
            )
        return None

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = BlobRowsSourceConfig.from_dict(config, plugin_name=self.name)

        self._blobs = tuple(cfg.blobs)
        self._schema_config = cfg.schema_config
        self._initialize_declared_guaranteed_fields(self._schema_config)
        self._on_validation_failure = cfg.on_validation_failure

        self._schema_class: type[PluginSchema] = create_schema_from_config(
            self._schema_config,
            "BlobRowsSchema",
            allow_coercion=False,
        )
        self.output_schema = self._schema_class

        initial_contract = create_contract_from_config(self._schema_config)
        if initial_contract.locked:
            self.set_schema_contract(initial_contract)
            self._contract_builder: ContractBuilder | None = None
        else:
            self._contract_builder = ContractBuilder(initial_contract)
        self._first_valid_row_processed = False

        self._payload_store: PayloadStore | None = None

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        self._payload_store = ctx.payload_store

    def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
        """Validate every payload ref exists, then emit one row per entry.

        Existence is validated for the COMPLETE list before the first row is
        emitted: a missing payload is a configuration/infrastructure defect
        (the web path re-resolved records at run admission; the CLI path is
        the trusted operator's assertion), never a quarantinable data row.
        The source deliberately does not retrieve content — the consuming
        transform performs integrity-checked retrieval itself.
        """
        store = self._payload_store
        if store is None:
            raise RuntimeError("blob_rows requires a payload store; on_start() did not receive one")
        missing = [entry.payload_ref for entry in self._blobs if not store.exists(entry.payload_ref)]
        if missing:
            raise FileNotFoundError(f"blob_rows payload ref(s) not found in payload store: {', '.join(sorted(missing))}")

        self._first_valid_row_processed = False
        for index, entry in enumerate(self._blobs):
            row = {
                "blob_id": entry.blob_id,
                "blob_ref": entry.payload_ref,
                "blob_filename": entry.filename,
                "blob_mime_type": entry.mime_type,
                "blob_size_bytes": entry.size_bytes,
            }
            yield from self._validate_and_yield(row, ctx, source_row_index=index)

        if not self._first_valid_row_processed and self._contract_builder is not None:
            self.set_schema_contract(self._contract_builder.contract.with_locked())

    def _validate_and_yield(self, row: dict[str, Any], ctx: SourceContext, *, source_row_index: int) -> Iterator[SourceRow]:
        """Validate one custody row against the configured schema."""
        try:
            validated = self._schema_class.model_validate(row)
            validated_row = validated.to_row()

            if self._contract_builder is not None and not self._first_valid_row_processed:
                field_resolution = {name: name for name in _ROW_FIELDS}
                self._contract_builder.process_first_row(validated_row, field_resolution)
                self.set_schema_contract(self._contract_builder.contract)
                self._first_valid_row_processed = True

            contract = self.require_schema_contract()
            yield SourceRow.valid(validated_row, contract=contract, source_row_index=source_row_index)
        except ValueError as exc:
            error_msg = f"blob_rows row failed schema validation: {exc}"
            ctx.record_validation_error(
                row=row,
                error=error_msg,
                schema_mode=self._schema_config.mode,
                destination=self._on_validation_failure,
            )
            if self._on_validation_failure != "discard":
                yield SourceRow.quarantined(
                    row=row,
                    error=error_msg,
                    destination=self._on_validation_failure,
                    source_row_index=source_row_index,
                )

    def close(self) -> None:
        """No resources to close."""
