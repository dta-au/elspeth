"""Rasterize a payload-store PDF into one PNG page row per page (one expand group)."""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.binary_documents import BINARY_DOCUMENT_MAX_BYTES, binary_document_signature_matches
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.errors import FrameworkBugError, TransformErrorReason
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeResponse,
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
    source_file_hash: str | None = "sha256:35c153e1b6c48923"
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
        del ctx
        field_name = self._blob_ref_field
        if field_name not in row:
            return TransformResult.error({"reason": "missing_field", "field": field_name}, retryable=False)
        blob_ref = row[field_name]
        if type(blob_ref) is not str:
            raise TypeError(
                f"Field '{field_name}' must be a string payload-store hash, got {type(blob_ref).__name__}. "
                "This indicates an upstream validation bug."
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

        result, output_dir = self._renderer.render(body)
        try:
            return self._map_document_result(result, blob_ref=blob_ref, row=row, output_dir=output_dir)
        finally:
            self._renderer.discard(output_dir)

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
        field_name = self._blob_ref_field
        refused_entries: list[dict[str, Any]] = [
            {"page_number": refused.page_number, "kind": refused.kind.value, "detail": refused.detail} for refused in response.refused
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
        for page in response.rendered:
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
