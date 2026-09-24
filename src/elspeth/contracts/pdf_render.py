"""Audited PDF renderer request and complete worker-result receipt shapes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NotRequired, TypedDict


class PDFRenderLimitsData(TypedDict):
    dpi: int
    max_pages: int
    max_page_pixels: int
    max_page_bytes: int
    render_timeout_seconds: int
    worker_memory_limit_bytes: int
    extract_text: bool
    max_page_text_bytes: int


class PDFRenderRequestData(TypedDict):
    format: str
    input_pdf_hash: str
    render_limits: PDFRenderLimitsData


class PDFRenderedPageData(TypedDict):
    page_number: int
    page_ref: str | None
    width_px: int
    height_px: int
    size_bytes: int
    worker_size_bytes: int
    text: str | None


class PDFRefusedPageData(TypedDict):
    kind: NotRequired[str]
    detail: NotRequired[str]
    page_number: NotRequired[int]
    timeout_seconds: NotRequired[int]


class PDFRenderReceiptData(TypedDict):
    format: str
    renderer_identity: str
    outcome_kind: str
    page_count: int | None
    rendered: list[PDFRenderedPageData]
    refused: list[PDFRefusedPageData]
    result_status: str
    rows: Sequence[object]
    success_reason: object | None
    error_reason: object | None
