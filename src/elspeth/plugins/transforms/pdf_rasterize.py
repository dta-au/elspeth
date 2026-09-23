"""Rasterize a payload-store PDF into one PNG page row per page (one expand group)."""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
import tempfile
import time
from itertools import chain
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, model_validator

from elspeth.contracts import Call, Determinism
from elspeth.contracts.binary_documents import BINARY_DOCUMENT_MAX_BYTES, binary_document_signature_matches
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError, TransformErrorReason, TransformSuccessReason
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.pdf_render import (
    PDFRefusedPageData,
    PDFRenderedPageData,
    PDFRenderReceiptData,
    PDFRenderRequestData,
)
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.replay_payload_store import SourceBoundPayloadStore
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.rasterize.identity import renderer_identity
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer, RenderLimits, RenderResult, RenderTimedOut
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

DEFAULT_DPI = 150
MIN_DPI = 36
MAX_DPI = 300
DEFAULT_MAX_INPUT_BYTES = 50 * 1024 * 1024
HARD_MAX_INPUT_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_PAGES = 200
HARD_MAX_PAGES = 2_000
DEFAULT_MAX_PAGE_PIXELS = 25_000_000
HARD_MAX_PAGE_PIXELS = 50_000_000
DEFAULT_RENDER_TIMEOUT_SECONDS = 120
HARD_MAX_RENDER_TIMEOUT_SECONDS = 900
DEFAULT_WORKER_MEMORY_LIMIT_BYTES = 2 * 1024**3
HARD_MAX_WORKER_MEMORY_LIMIT_BYTES = 8 * 1024**3
DEFAULT_MAX_PAGE_TEXT_BYTES = 1024 * 1024
HARD_MAX_PAGE_TEXT_BYTES = 5 * 1024 * 1024
PAGE_MIME_TYPE = "image/png"
_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_INVARIANT_PROBE_BLOB_REF = "0" * 64
_INVARIANT_PROBE_PNG = b"\x89PNG\r\n\x1a\n" + b"pdf-rasterize-invariant-probe"

_SIZE_REFUSALS = frozenset({PageRefusalKind.OVERSIZE_PIXELS, PageRefusalKind.OVERSIZE_BYTES, PageRefusalKind.OVERSIZE_TEXT})


def _build_invariant_probe_pdf() -> bytes:
    """Build a valid one-page PDF for the invariant probe seam.

    A small module-level copy of ``tests/fixtures/pdf_documents.py``'s
    ``minimal_pdf(1)`` logic — the invariant harness must stay hermetic and
    may never import from ``tests``.
    """
    width_pt, height_pt = 200.0, 100.0
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:g} {height_pt:g}] "
            f"/Contents 5 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode(),
    ]
    stream = b"BT /F1 24 Tf 20 40 Td (Page 1) Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


_INVARIANT_PROBE_PDF: bytes = _build_invariant_probe_pdf()


class PDFRasterizeConfig(TransformDataConfig):
    """Configuration for pdf_rasterize."""

    blob_ref_field: str = Field(
        default="blob_ref",
        min_length=1,
        max_length=256,
        title="PDF reference field",
        description="Input row field containing the payload-store SHA-256 content hash of the PDF bytes.",
    )
    page_blob_ref_field: str = Field(
        default="page_blob_ref",
        min_length=1,
        max_length=256,
        title="Page image reference field",
        description="Output field receiving the payload-store content hash of each rendered PNG page.",
    )
    page_number_field: str = Field(
        default="page_number",
        min_length=1,
        max_length=256,
        title="Page number field",
        description="Output field receiving the 1-based page number.",
    )
    document_id_field: str = Field(
        default="document_id",
        min_length=1,
        max_length=256,
        title="Document id field",
        description="Output field receiving the source PDF's payload-store content hash, identical on every page row.",
    )
    page_mime_type_field: str = Field(
        default="page_mime_type",
        min_length=1,
        max_length=256,
        title="Page MIME type field",
        description="Output field receiving the page image MIME type (always image/png).",
    )
    page_size_bytes_field: str = Field(
        default="page_size_bytes",
        min_length=1,
        max_length=256,
        title="Page size field",
        description="Output field receiving the encoded PNG byte length.",
    )
    page_width_field: str = Field(
        default="page_width_px",
        min_length=1,
        max_length=256,
        title="Page width field",
        description="Output field receiving the rendered page width in pixels.",
    )
    page_height_field: str = Field(
        default="page_height_px",
        min_length=1,
        max_length=256,
        title="Page height field",
        description="Output field receiving the rendered page height in pixels.",
    )
    extract_text: bool = Field(
        default=True,
        title="Extract page text",
        description=(
            "Extract each page's text via pdfium's text layer (no OCR, fully offline) alongside the rendered PNG. "
            "A page with no text layer yields an empty string, not a refusal. When false, page_text_field is not emitted."
        ),
    )
    page_text_field: Annotated[
        str,
        EmittedToOutput(
            "pdf_rasterize uses this as the emitted FieldDefinition name for the page's text, "
            "so the value becomes a key in row data and a column in the artifact header"
        ),
    ] = Field(
        default="page_text",
        min_length=1,
        max_length=256,
        title="Page text field",
        description="Output field receiving the page's extracted text when extract_text is true; not emitted when false.",
    )
    max_page_text_bytes: int = Field(
        default=DEFAULT_MAX_PAGE_TEXT_BYTES,
        gt=0,
        le=HARD_MAX_PAGE_TEXT_BYTES,
        title="Maximum page text bytes",
        description=(
            "Refuse a page whose extracted text (UTF-8 encoded) exceeds this many bytes; only evaluated when "
            "extract_text is true. Guards against an unbounded page_text row feeding a downstream LLM."
        ),
    )
    dpi: int = Field(
        default=DEFAULT_DPI,
        ge=MIN_DPI,
        le=MAX_DPI,
        title="Render DPI",
        description="Raster resolution; 150 keeps a Letter/A4 page comfortably under the 5 MiB per-page bound.",
    )
    max_input_bytes: int = Field(
        default=DEFAULT_MAX_INPUT_BYTES,
        gt=0,
        le=HARD_MAX_INPUT_BYTES,
        title="Maximum input bytes",
        description="Maximum accepted size of the source PDF retrieved from the payload store.",
    )
    max_pages: int = Field(
        default=DEFAULT_MAX_PAGES,
        gt=0,
        le=HARD_MAX_PAGES,
        title="Maximum pages",
        description="Refuse the whole document (too_many_rows) when its page count exceeds this ceiling.",
    )
    max_page_pixels: int = Field(
        default=DEFAULT_MAX_PAGE_PIXELS,
        gt=0,
        le=HARD_MAX_PAGE_PIXELS,
        title="Maximum page pixels",
        description="Refuse a page whose declared size at the configured dpi exceeds this many pixels, before any bitmap is allocated.",
    )
    max_page_bytes: int = Field(
        default=BINARY_DOCUMENT_MAX_BYTES,
        gt=0,
        le=BINARY_DOCUMENT_MAX_BYTES,
        title="Maximum page bytes",
        description="Maximum encoded PNG bytes per page; may be reduced but never raised above the 5 MiB downstream provider bound.",
    )
    render_timeout_seconds: int = Field(
        default=DEFAULT_RENDER_TIMEOUT_SECONDS,
        gt=0,
        le=HARD_MAX_RENDER_TIMEOUT_SECONDS,
        title="Render timeout seconds",
        description="Wall-clock and CPU budget for rendering one document in the worker subprocess.",
    )
    worker_memory_limit_bytes: int = Field(
        default=DEFAULT_WORKER_MEMORY_LIMIT_BYTES,
        gt=0,
        le=HARD_MAX_WORKER_MEMORY_LIMIT_BYTES,
        title="Worker memory limit bytes",
        description="RLIMIT_AS applied to the render worker subprocess.",
    )
    on_page_failure: Literal["fail_document", "emit_rendered"] = Field(
        default="fail_document",
        title="Page failure policy",
        description=(
            "fail_document: any refused page fails the whole row (typed error routed via on_error). "
            "emit_rendered: emit the pages that rendered and record the refused page numbers in the "
            "success metadata; zero survivors is still a row error."
        ),
    )

    @model_validator(mode="after")
    def _reject_field_name_collisions(self) -> PDFRasterizeConfig:
        emitted = (
            self.page_blob_ref_field,
            self.page_number_field,
            self.document_id_field,
            self.page_mime_type_field,
            self.page_size_bytes_field,
            self.page_width_field,
            self.page_height_field,
            self.page_text_field,
        )
        for name in (self.blob_ref_field, *emitted):
            if not name.strip() or not name.isidentifier():
                raise ValueError(f"pdf_rasterize field names must be non-empty identifiers, got {name!r}")
        if len(set(emitted)) != len(emitted):
            raise ValueError("pdf_rasterize emitted field names must be distinct")
        if self.blob_ref_field in emitted:
            raise ValueError(f"blob_ref_field {self.blob_ref_field!r} may not name a field pdf_rasterize creates")
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.blob_ref_field})


def _pdf_rasterize_added_output_fields(cfg: PDFRasterizeConfig) -> tuple[FieldDefinition, ...]:
    fields = [
        FieldDefinition(name=cfg.page_blob_ref_field, field_type="str", required=True),
        FieldDefinition(name=cfg.page_number_field, field_type="int", required=True),
        FieldDefinition(name=cfg.document_id_field, field_type="str", required=True),
        FieldDefinition(name=cfg.page_mime_type_field, field_type="str", required=True),
        FieldDefinition(name=cfg.page_size_bytes_field, field_type="int", required=True),
        FieldDefinition(name=cfg.page_width_field, field_type="int", required=True),
        FieldDefinition(name=cfg.page_height_field, field_type="int", required=True),
    ]
    if cfg.extract_text:
        fields.append(FieldDefinition(name=cfg.page_text_field, field_type="str", required=True))
    return tuple(fields)


def _build_pdf_rasterize_output_schema_config(schema_config: SchemaConfig, cfg: PDFRasterizeConfig) -> SchemaConfig:
    field_by_name: dict[str, FieldDefinition] = {}
    if schema_config.fields is not None:
        field_by_name.update((field.name, field) for field in schema_config.fields)

    added_fields = _pdf_rasterize_added_output_fields(cfg)
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


class _InvariantPayloadStore:
    """Hermetic in-memory payload store seam for the invariant probe."""

    def __init__(self) -> None:
        self._content_by_hash: dict[str, bytes] = {_INVARIANT_PROBE_BLOB_REF: _INVARIANT_PROBE_PDF}

    def retrieve(self, content_hash: str) -> bytes:
        if content_hash not in self._content_by_hash:
            raise PayloadNotFoundError(content_hash)
        return self._content_by_hash[content_hash]

    def store(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        self._content_by_hash[content_hash] = content
        return content_hash

    def exists(self, content_hash: str) -> bool:
        return content_hash in self._content_by_hash


class _InvariantRenderer:
    """Hermetic renderer seam for the invariant probe: always renders one page."""

    def __init__(self, extract_text: bool) -> None:
        self._extract_text = extract_text

    def render(self, pdf_bytes: bytes) -> tuple[RasterizeResponse, Path]:
        del pdf_bytes
        output_dir = Path(tempfile.mkdtemp(prefix="pdf-rasterize-invariant-probe-"))
        png_path = output_dir / "page-1.png"
        png_path.write_bytes(_INVARIANT_PROBE_PNG)
        # Mirrors _INVARIANT_PROBE_PDF's own content stream (`(Page 1) Tj`), which is
        # what a real pdfium text-layer extraction over that document would return.
        text = "Page 1" if self._extract_text else None
        page = RenderedPage(page_number=1, png_path=png_path, width_px=1, height_px=1, size_bytes=len(_INVARIANT_PROBE_PNG), text=text)
        return RasterizeResponse(page_count=1, rendered=(page,), refused=()), output_dir

    def discard(self, output_dir: Path | None) -> None:
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        pass


class PDFRasterize(BaseTransform):
    """Render each page of a PDF into a PNG payload and emit one row per page."""

    output_naming_config_keys = frozenset(
        {
            "page_blob_ref_field",
            "page_number_field",
            "document_id_field",
            "page_mime_type_field",
            "page_size_bytes_field",
            "page_width_field",
            "page_height_field",
            "page_text_field",
        }
    )
    name = "pdf_rasterize"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:89687a0a81a01a23"
    config_model = PDFRasterizeConfig
    usage_when_to_use: str = (
        "Use when each row carries a payload-store content hash for a PDF (from the blob_rows source or blob_fetch) "
        "and you need one row per page carrying a rendered PNG image — typically feeding aws_textract_inline_analysis "
        "with document_format png and blob_ref_field page_blob_ref so a multipage PDF becomes N synchronous single-page calls."
    )
    usage_when_not_to_use: str = (
        "Not an OCR text extractor: only the PDF text layer is read, empty on scans — OCR needs aws_textract_inline_analysis. "
        "Not for images/non-PDF: pages render as pixels only. S3-staged docs use aws_textract_document_analysis (no rasterizing)."
    )
    example_use: str = """transform:
  plugin: pdf_rasterize
  options:
    blob_ref_field: blob_ref
    dpi: 150
    max_pages: 200
    on_page_failure: fail_document
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("pdf", "rasterize", "image", "blob", "fan-out")
    creates_tokens = True
    passes_through_input = True

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {"schema": {"mode": "observed"}, "blob_ref_field": "blob_ref"}

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        cfg = PDFRasterizeConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)

        self._blob_ref_field = cfg.blob_ref_field
        self._page_blob_ref_field = cfg.page_blob_ref_field
        self._page_number_field = cfg.page_number_field
        self._document_id_field = cfg.document_id_field
        self._page_mime_type_field = cfg.page_mime_type_field
        self._page_size_bytes_field = cfg.page_size_bytes_field
        self._page_width_field = cfg.page_width_field
        self._page_height_field = cfg.page_height_field
        self._extract_text = cfg.extract_text
        self._page_text_field = cfg.page_text_field
        self._max_input_bytes = cfg.max_input_bytes
        self._max_pages = cfg.max_pages
        self._on_page_failure = cfg.on_page_failure

        self._limits = RenderLimits(
            dpi=cfg.dpi,
            max_pages=cfg.max_pages,
            max_page_pixels=cfg.max_page_pixels,
            max_page_bytes=cfg.max_page_bytes,
            render_timeout_seconds=cfg.render_timeout_seconds,
            worker_memory_limit_bytes=cfg.worker_memory_limit_bytes,
            extract_text=cfg.extract_text,
            max_page_text_bytes=cfg.max_page_text_bytes,
        )
        self._renderer: Any = PoolRenderer(self._limits)  # pool is created lazily on first render

        self.declared_output_fields = frozenset(field.name for field in _pdf_rasterize_added_output_fields(cfg))
        self.input_schema = create_schema_from_config(cfg.schema_config, "PDFRasterizeInput", allow_coercion=False)
        self._output_schema_config = _build_pdf_rasterize_output_schema_config(cfg.schema_config, cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "PDFRasterizeOutput", allow_coercion=False)
        self._reject_input_options_naming_created_fields({"blob_ref_field": cfg.blob_ref_field})

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Rasterize a payload-store PDF into one PNG image row per page, ready for image-based extraction.",
                composer_hints=(
                    "Place pdf_rasterize after blob_rows or blob_fetch; the default blob_ref_field matches their blob_ref output.",
                    "Downstream aws_textract_inline_analysis must set blob_ref_field: page_blob_ref and document_format: png "
                    "— the page image is a new field, the PDF's blob_ref is preserved unchanged.",
                    "Keep max_page_bytes at or below the downstream max_document_bytes (5 MiB ceiling); dpi 150 fits "
                    "Letter/A4, raise dpi only with max_page_pixels headroom.",
                    "on_page_failure: fail_document quarantines the whole PDF row on any refused page; emit_rendered "
                    "emits the surviving pages and records the refused page numbers in the run audit.",
                    "Every page row carries document_id (the PDF's payload hash) and a 1-based page_number for "
                    "grouping and ordering downstream.",
                    "extract_text (default true) also emits page_text: each page's text via pdfium's text layer, "
                    "no OCR — empty string for a page with no text layer, not a refusal. Set extract_text: false to skip it.",
                    "max_page_text_bytes (default 1 MiB, ceiling 5 MiB) refuses a page whose extracted text exceeds it "
                    "— a size refusal like max_page_bytes, folded into the same pdf_page_too_large reason.",
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
        """Drive the real process path with hermetic payload-store and renderer seams."""
        had_payload_store = "_payload_store" in self.__dict__
        original_payload_store: Any = None
        if had_payload_store:
            original_payload_store = self.__dict__["_payload_store"]
        had_renderer = "_renderer" in self.__dict__
        original_renderer: Any = None
        if had_renderer:
            original_renderer = self.__dict__["_renderer"]
        try:
            self.__dict__["_payload_store"] = _InvariantPayloadStore()
            self.__dict__["_renderer"] = _InvariantRenderer(self._extract_text)
            return super().execute_forward_invariant_probe(probe_rows, ctx)
        finally:
            if had_payload_store:
                self.__dict__["_payload_store"] = original_payload_store
            else:
                delattr(self, "_payload_store")
            if had_renderer:
                self.__dict__["_renderer"] = original_renderer
            else:
                delattr(self, "_renderer")

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        if ctx.payload_store is None:
            raise FrameworkBugError("PDFRasterize requires payload_store — orchestrator must configure it before on_start().")
        self._payload_store = ctx.payload_store

    def close(self) -> None:
        self._renderer.close()
        super().close()

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        field_name = self._blob_ref_field
        if field_name not in row:
            return TransformResult.error({"reason": "missing_field", "field": field_name}, retryable=False)
        blob_ref = row[field_name]
        if type(blob_ref) is not str:
            # Routed, not raised — see blob_csv_expand for the full note. The
            # missing-field check directly ABOVE and the malformed-hash check
            # directly BELOW both already take this exit; the type check was
            # the lone crash between two routable siblings.
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": field_name,
                    "error_type": "non_string_ref",
                    "error": f"must be a string payload-store hash, got {type(blob_ref).__name__}",
                },
                retryable=False,
            )
        if _PAYLOAD_REF_PATTERN.fullmatch(blob_ref) is None:
            return TransformResult.error(
                {"reason": "invalid_input", "field": field_name, "blob_ref": blob_ref, "error_type": "invalid_blob_ref"},
                retryable=False,
            )

        try:
            body = self._payload_store.retrieve(blob_ref)
        except PayloadNotFoundError:
            return TransformResult.error(
                {"reason": "blob_not_found", "field": field_name, "blob_ref": blob_ref},
                retryable=False,
            )
        except IntegrityError:
            raise

        if not body:
            return TransformResult.error(
                {"reason": "invalid_input", "field": field_name, "blob_ref": blob_ref, "error_type": "empty_document"},
                retryable=False,
            )
        if len(body) > self._max_input_bytes:
            return TransformResult.error(
                {
                    "reason": "blob_too_large",
                    "field": field_name,
                    "blob_ref": blob_ref,
                    "max_blob_bytes": self._max_input_bytes,
                    "actual": str(len(body)),
                },
                retryable=False,
            )
        if not binary_document_signature_matches("pdf", body):
            return TransformResult.error(
                {
                    "reason": "invalid_input",
                    "field": field_name,
                    "blob_ref": blob_ref,
                    "error_type": "document_signature_mismatch",
                },
                retryable=False,
            )

        request_data = self._render_request(blob_ref)
        call_index: int | None = None
        if ctx.landscape is not None and ctx.state_id is not None:
            call_index = ctx.allocate_call_index()
        if ctx.run_mode is RunMode.REPLAY:
            if call_index is None or ctx.call_mode_session is None:
                raise AuditIntegrityError("PDF replay requires a node-state call parent and source-run session")
            evidence = ctx.call_mode_session.replay_call(
                call_type=CallType.FILESYSTEM,
                request_data=request_data,
                current_state_id=ctx.state_id,
                current_operation_id=None,
                current_call_index=call_index,
            )
            if evidence.status is not CallStatus.SUCCESS or evidence.response_data is None:
                raise AuditIntegrityError("PDF replay source call has no successful retained render receipt")
            receipt = deep_thaw(evidence.response_data)
            replayed = self._restore_render_receipt(receipt, row)
            self._record_render_call(ctx, call_index, request_data, receipt, source_call_id=evidence.source_call_id, latency_ms=0.0)
            return replayed

        if ctx.run_mode is RunMode.VERIFY:
            if call_index is None or ctx.call_mode_session is None:
                raise AuditIntegrityError("PDF verify requires a node-state call parent and source-run session")
            ctx.call_mode_session.admit_verify_call(
                call_type=CallType.FILESYSTEM,
                request_data=request_data,
                current_state_id=ctx.state_id,
                current_operation_id=None,
                current_call_index=call_index,
            )

        started = time.perf_counter()
        result, output_dir = self._renderer.render(body)
        try:
            mapped = self._map_document_result(result, blob_ref=blob_ref, row=row, output_dir=output_dir)
            if call_index is None:
                if ctx.run_mode is RunMode.VERIFY:
                    raise AuditIntegrityError("PDF verify requires a node-state call parent")
                return mapped
            receipt = self._render_receipt(result, mapped)
            call = self._record_render_call(
                ctx,
                call_index,
                request_data,
                receipt,
                source_call_id=None,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            if ctx.run_mode is RunMode.VERIFY:
                if ctx.call_mode_session is None:
                    raise AuditIntegrityError("PDF verify requires a source-run session")
                decision = ctx.call_mode_session.verify_call(
                    call_type=CallType.FILESYSTEM,
                    request_data=request_data,
                    current_state_id=ctx.state_id,
                    current_operation_id=None,
                    current_call_index=call_index,
                    current_call_id=call.call_id,
                    live_status=CallStatus.SUCCESS,
                    live_response_data=receipt,
                    live_error_data=None,
                )
                if decision.is_match is not True:
                    raise AuditIntegrityError("PDF verify render receipt differs from source-run evidence")
            return mapped
        finally:
            self._renderer.discard(output_dir)

    def _render_request(self, blob_ref: str) -> PDFRenderRequestData:
        return {
            "format": "pdf_rasterize/v1",
            "input_pdf_hash": blob_ref,
            "render_limits": {
                "dpi": self._limits.dpi,
                "max_pages": self._limits.max_pages,
                "max_page_pixels": self._limits.max_page_pixels,
                "max_page_bytes": self._limits.max_page_bytes,
                "render_timeout_seconds": self._limits.render_timeout_seconds,
                "worker_memory_limit_bytes": self._limits.worker_memory_limit_bytes,
                "extract_text": self._limits.extract_text,
                "max_page_text_bytes": self._limits.max_page_text_bytes,
            },
        }

    def _render_receipt(self, outcome: RenderResult, mapped: TransformResult) -> PDFRenderReceiptData:
        output_rows = [item.to_dict() for item in mapped.rows] if mapped.rows is not None else []
        page_refs = {item[self._page_number_field]: item[self._page_blob_ref_field] for item in output_rows}
        page_sizes = {item[self._page_number_field]: item[self._page_size_bytes_field] for item in output_rows}
        rendered: list[PDFRenderedPageData] = []
        refused: list[PDFRefusedPageData] = []
        page_count: int | None = None
        outcome_kind: str
        if type(outcome) is RasterizeResponse:
            outcome_kind = "rasterized"
            page_count = outcome.page_count
            rendered = [
                {
                    "page_number": page.page_number,
                    "page_ref": page_refs[page.page_number] if page.page_number in page_refs else None,
                    "width_px": page.width_px,
                    "height_px": page.height_px,
                    "size_bytes": page_sizes[page.page_number] if page.page_number in page_sizes else page.size_bytes,
                    "worker_size_bytes": page.size_bytes,
                    "text": page.text,
                }
                for page in outcome.rendered
            ]
            refused = [{"page_number": page.page_number, "kind": page.kind.value, "detail": page.detail} for page in outcome.refused]
        elif type(outcome) is DocumentRefusal:
            outcome_kind = "document_refusal"
            page_count = outcome.page_count
            refused = [{"kind": outcome.kind.value, "detail": outcome.detail}]
        elif type(outcome) is RenderTimedOut:
            outcome_kind = "timeout"
            refused = [{"timeout_seconds": outcome.timeout_seconds}]
        else:
            raise FrameworkBugError(f"Unknown PDF renderer result type: {type(outcome).__name__}")
        return {
            "format": "pdf_rasterize/v1",
            "renderer_identity": renderer_identity(),
            "outcome_kind": outcome_kind,
            "page_count": page_count,
            "rendered": rendered,
            "refused": refused,
            "result_status": mapped.status,
            "rows": list(output_rows),
            "success_reason": deep_thaw(mapped.success_reason),
            "error_reason": deep_thaw(mapped.reason),
        }

    def _restore_render_receipt(self, receipt: object, input_row: PipelineRow) -> TransformResult:
        if type(receipt) is not dict or "format" not in receipt or receipt["format"] != "pdf_rasterize/v1":
            raise AuditIntegrityError("PDF replay source call has no typed render receipt")
        identity = receipt["renderer_identity"] if "renderer_identity" in receipt else None
        if type(identity) is not str or _PAYLOAD_REF_PATTERN.fullmatch(identity) is None:
            raise AuditIntegrityError("PDF replay render receipt has no renderer identity")
        required = {
            "outcome_kind",
            "rendered",
            "refused",
            "page_count",
            "rows",
            "result_status",
            "success_reason",
            "error_reason",
        }
        if not required.issubset(receipt):
            raise AuditIntegrityError("PDF replay render receipt is missing required fields")
        if receipt["outcome_kind"] not in ("rasterized", "document_refusal", "timeout"):
            raise AuditIntegrityError("PDF replay render receipt has no typed worker outcome")
        rendered = receipt["rendered"]
        refused = receipt["refused"]
        page_count = receipt["page_count"]
        rows = receipt["rows"]
        if type(rendered) is not list or type(refused) is not list or type(rows) is not list:
            raise AuditIntegrityError("PDF replay render receipt has malformed output rows")
        if receipt["outcome_kind"] == "rasterized":
            if type(page_count) is not int or not 0 <= page_count <= self._max_pages:
                raise AuditIntegrityError("PDF replay render receipt has invalid page count")
        elif rendered:
            raise AuditIntegrityError("PDF replay refusal receipt cannot contain rendered pages")
        if type(self._payload_store) is not SourceBoundPayloadStore:
            raise AuditIntegrityError("PDF replay requires a source-bound payload store")
        if receipt["result_status"] == "error":
            error_reason = receipt["error_reason"]
            if rows or type(error_reason) is not dict or "reason" not in error_reason or type(error_reason["reason"]) is not str:
                raise AuditIntegrityError("PDF replay error receipt has inconsistent output")
            return TransformResult.error(cast(TransformErrorReason, error_reason), retryable=False)
        if (
            receipt["result_status"] != "success"
            or receipt["outcome_kind"] != "rasterized"
            or not rows
            or len(rows) != len(rendered)
            or type(receipt["success_reason"]) is not dict
            or "action" not in receipt["success_reason"]
            or type(receipt["success_reason"]["action"]) is not str
        ):
            raise AuditIntegrityError("PDF replay success receipt has inconsistent output")
        pages_by_number: dict[int, PDFRenderedPageData] = {}
        for index, page in enumerate(rendered):
            if type(page) is not dict or "page_ref" not in page or "page_number" not in page:
                raise AuditIntegrityError(f"PDF replay rendered page {index} has no archived payload")
            if type(page["page_ref"]) is not str or type(page["page_number"]) is not int:
                raise AuditIntegrityError(f"PDF replay rendered page {index} has malformed payload fields")
            number = page["page_number"]
            if not 1 <= number <= page_count or number in pages_by_number:
                raise AuditIntegrityError(f"PDF replay rendered page {index} has invalid page number")
            page_ref = page["page_ref"]
            if _PAYLOAD_REF_PATTERN.fullmatch(page_ref) is None:
                raise AuditIntegrityError(f"PDF replay rendered page {index} has malformed payload hash")
            archived_bytes = self._payload_store.read_output(page_ref)
            if (
                "size_bytes" not in page
                or "worker_size_bytes" not in page
                or type(page["size_bytes"]) is not int
                or type(page["worker_size_bytes"]) is not int
                or len(archived_bytes) != page["size_bytes"]
            ):
                raise AuditIntegrityError(f"PDF replay rendered page {index} size differs from archived bytes")
            pages_by_number[number] = cast(PDFRenderedPageData, page)
        expected_input = input_row.to_dict()
        output_rows: list[PipelineRow] = []
        for index, output in enumerate(rows):
            if type(output) is not dict or not all(key in output and output[key] == value for key, value in expected_input.items()):
                raise AuditIntegrityError(f"PDF replay output row {index} disagrees with input")
            if self._page_number_field not in output or type(output[self._page_number_field]) is not int:
                raise AuditIntegrityError(f"PDF replay output row {index} has no page number")
            number = output[self._page_number_field]
            if number not in pages_by_number:
                raise AuditIntegrityError(f"PDF replay output row {index} has no matching rendered page")
            page = pages_by_number[number]
            required_output = {
                self._page_blob_ref_field,
                self._page_width_field,
                self._page_height_field,
                self._page_size_bytes_field,
                self._document_id_field,
                self._page_mime_type_field,
            }
            if self._extract_text:
                required_output.add(self._page_text_field)
            if not required_output.issubset(output):
                raise AuditIntegrityError(f"PDF replay output row {index} is missing page fields")
            if (
                output[self._page_blob_ref_field] != page["page_ref"]
                or "width_px" not in page
                or "height_px" not in page
                or output[self._page_width_field] != page["width_px"]
                or output[self._page_height_field] != page["height_px"]
                or output[self._page_size_bytes_field] != page["size_bytes"]
                or output[self._document_id_field] != expected_input[self._blob_ref_field]
                or output[self._page_mime_type_field] != PAGE_MIME_TYPE
            ):
                raise AuditIntegrityError(f"PDF replay output row {index} disagrees with worker receipt")
            if self._extract_text and ("text" not in page or output[self._page_text_field] != page["text"]):
                raise AuditIntegrityError(f"PDF replay output row {index} text differs from worker receipt")
            contract = narrow_contract_to_output(input_contract=input_row.contract, output_row=output)
            contract = self._apply_declared_output_field_contracts(contract)
            contract = self._align_output_contract(contract)
            output_rows.append(PipelineRow(output, contract))
        for page in pages_by_number.values():
            restored_ref = page["page_ref"]
            if type(restored_ref) is not str:
                raise AuditIntegrityError("PDF replay validated page lost its payload reference")
            self._payload_store.restore_output(restored_ref)
        return TransformResult.success_multi(output_rows, success_reason=cast(TransformSuccessReason, receipt["success_reason"]))

    def _record_render_call(
        self,
        ctx: TransformContext,
        call_index: int,
        request_data: PDFRenderRequestData,
        response_data: PDFRenderReceiptData,
        *,
        source_call_id: str | None,
        latency_ms: float,
    ) -> Call:
        return ctx.record_row_call(
            call_index=call_index,
            call_type=CallType.FILESYSTEM,
            status=CallStatus.SUCCESS,
            request_data=RawCallPayload(request_data),
            response_data=RawCallPayload(response_data),
            latency_ms=latency_ms,
            source_call_id=source_call_id,
        )

    def _map_document_result(self, result: RenderResult, *, blob_ref: str, row: PipelineRow, output_dir: Path | None) -> TransformResult:
        if isinstance(result, DocumentRefusal):
            return self._map_document_refusal(result, blob_ref=blob_ref)
        if isinstance(result, RenderTimedOut):
            return TransformResult.error(
                {
                    "reason": "render_timeout",
                    "field": self._blob_ref_field,
                    "blob_ref": blob_ref,
                    "max_seconds": float(result.timeout_seconds),
                },
                retryable=False,
            )
        if output_dir is None:
            raise FrameworkBugError(
                "PDFRasterize renderer returned rendered pages with no output_dir — cannot verify page path containment."
            )
        return self._map_rasterize_response(result, blob_ref=blob_ref, row=row, output_dir=output_dir)

    def _map_document_refusal(self, result: DocumentRefusal, *, blob_ref: str) -> TransformResult:
        field_name = self._blob_ref_field
        if result.kind is DocumentRefusalKind.ENCRYPTED:
            return TransformResult.error(
                {"reason": "pdf_encrypted", "field": field_name, "blob_ref": blob_ref, "detail": result.detail},
                retryable=False,
            )
        if result.kind is DocumentRefusalKind.MALFORMED:
            return TransformResult.error(
                {"reason": "pdf_malformed", "field": field_name, "blob_ref": blob_ref, "detail": result.detail},
                retryable=False,
            )
        # DocumentRefusalKind.TOO_MANY_PAGES
        reason: TransformErrorReason
        if result.page_count is None:
            reason = {
                "reason": "too_many_rows",
                "field": field_name,
                "blob_ref": blob_ref,
                "detail": result.detail,
                "max_pages": self._max_pages,
            }
        else:
            reason = {
                "reason": "too_many_rows",
                "field": field_name,
                "blob_ref": blob_ref,
                "detail": result.detail,
                "max_pages": self._max_pages,
                "page_count": result.page_count,
            }
        return TransformResult.error(reason, retryable=False)

    def _map_rasterize_response(self, response: RasterizeResponse, *, blob_ref: str, row: PipelineRow, output_dir: Path) -> TransformResult:
        # Assert the worker's complete partition before policy handling or page IO.
        # A broken worker protocol is a framework bug, not a document refusal.
        if type(response.page_count) is not int or not 0 <= response.page_count <= self._max_pages:
            raise FrameworkBugError("pdf_rasterize worker returned an invalid page partition: page_count outside configured bounds")
        seen: set[int] = set()
        for page_result in chain[RenderedPage | RefusedPage](response.rendered, response.refused):
            number = page_result.page_number
            if type(number) is not int or not 1 <= number <= response.page_count:
                raise FrameworkBugError("pdf_rasterize worker returned an invalid page partition: page number outside document bounds")
            if number in seen:
                raise FrameworkBugError("pdf_rasterize worker returned an invalid page partition: duplicate page number")
            seen.add(number)
        # Unique in-range members with this cardinality cover exactly 1..page_count.
        if len(seen) != response.page_count:
            raise FrameworkBugError("pdf_rasterize worker returned an invalid page partition: missing pages")

        field_name = self._blob_ref_field
        refused_entries: list[dict[str, Any]] = [
            {"page_number": refused.page_number, "kind": refused.kind.value, "detail": refused.detail}
            for refused in sorted(response.refused, key=lambda item: item.page_number)
        ]

        if not response.rendered:
            if response.refused:
                size_only = all(refused.kind in _SIZE_REFUSALS for refused in response.refused)
                return TransformResult.error(
                    {
                        "reason": "pdf_page_too_large" if size_only else "pdf_page_render_failed",
                        "field": field_name,
                        "blob_ref": blob_ref,
                        "refused_pages": refused_entries,
                        "page_count": response.page_count,
                    },
                    retryable=False,
                )
            # Zero pages rendered AND zero pages refused: the renderer reported an
            # empty document (page_count == 0) with nothing to explain why. Not one
            # of the typed page-refusal kinds — treat the document itself as
            # malformed rather than crash building an output row from nothing.
            return TransformResult.error(
                {
                    "reason": "pdf_malformed",
                    "field": field_name,
                    "blob_ref": blob_ref,
                    "detail": "document has no pages",
                },
                retryable=False,
            )

        if response.refused and self._on_page_failure == "fail_document":
            size_only = all(refused.kind in _SIZE_REFUSALS for refused in response.refused)
            return TransformResult.error(
                {
                    "reason": "pdf_page_too_large" if size_only else "pdf_page_render_failed",
                    "field": field_name,
                    "blob_ref": blob_ref,
                    "refused_pages": refused_entries,
                    "page_count": response.page_count,
                },
                retryable=False,
            )

        base = row.to_dict()
        output_rows: list[dict[str, Any]] = []
        resolved_output_dir = output_dir.resolve()
        for page in sorted(response.rendered, key=lambda item: item.page_number):
            resolved_png_path = page.png_path.resolve()
            if not resolved_png_path.is_relative_to(resolved_output_dir):
                # The spawn worker parses hostile PDF bytes; a compromised worker returning
                # an arbitrary readable path (e.g. a credentials file) must never be trusted
                # to name what gets read and published into the payload store. This is our
                # code's own containment invariant, not a document-shaped row error.
                raise RuntimeError(
                    f"pdf_rasterize worker returned page {page.page_number} at path {page.png_path!r}, "
                    f"outside its own render output directory {output_dir!r} — worker containment breach"
                )
            data = resolved_png_path.read_bytes()
            page_ref = self._payload_store.store(data)
            output = copy.deepcopy(base)
            output[self._page_blob_ref_field] = page_ref
            output[self._page_number_field] = page.page_number
            output[self._document_id_field] = blob_ref
            output[self._page_mime_type_field] = PAGE_MIME_TYPE
            output[self._page_size_bytes_field] = len(data)
            output[self._page_width_field] = page.width_px
            output[self._page_height_field] = page.height_px
            if self._extract_text:
                if type(page.text) is not str:
                    raise FrameworkBugError(
                        f"pdf_rasterize worker returned page {page.page_number} with extract_text enabled but text is "
                        f"{page.text!r} — the worker must always populate text when RasterizeRequest.extract_text is True."
                    )
                output[self._page_text_field] = page.text
            output_rows.append(output)

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

        return TransformResult.success_multi(
            [PipelineRow(output, output_contract) for output in output_rows],
            success_reason={
                "action": "expanded_blob",
                "fields_added": sorted(self.declared_output_fields),
                "metadata": {
                    "blob_ref": blob_ref,
                    "page_count": response.page_count,
                    "rendered_pages": len(output_rows),
                    "refused_pages": refused_entries,
                    "on_page_failure": self._on_page_failure,
                },
            },
        )
