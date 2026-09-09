"""Picklable messages crossing the rasterize worker process boundary.

No third-party imports: this module is loaded by BOTH the parent process
(which never imports pypdfium2) and the spawned worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DocumentRefusalKind(StrEnum):
    """Why a whole document could not be rasterized (a Tier-2 row outcome)."""

    ENCRYPTED = "encrypted"  # PdfiumError err_code 4 (password/security)
    MALFORMED = "malformed"  # PdfiumError err_code 3 / any other open failure
    TOO_MANY_PAGES = "too_many_pages"


class PageRefusalKind(StrEnum):
    """Why one page could not be rendered within the configured limits."""

    INVALID_GEOMETRY = "invalid_geometry"  # declared page size is non-positive at the configured dpi
    OVERSIZE_PIXELS = "oversize_pixels"  # declared size x dpi exceeds max_page_pixels (checked BEFORE render)
    OVERSIZE_BYTES = "oversize_bytes"  # encoded PNG exceeds max_page_bytes
    OVERSIZE_TEXT = "oversize_text"  # extracted text (UTF-8 encoded) exceeds max_page_text_bytes
    MEMORY_EXHAUSTED = "memory_exhausted"  # MemoryError under RLIMIT_AS
    RENDER_ERROR = "render_error"  # PdfiumError during page load/render


@dataclass(frozen=True, slots=True)
class RasterizeRequest:
    pdf_bytes: bytes
    dpi: int
    max_pages: int
    max_page_pixels: int
    max_page_bytes: int
    output_dir: Path  # parent-owned temp dir; worker writes page-<n>.png files here
    extract_text: bool
    max_page_text_bytes: int  # only evaluated when extract_text is True


@dataclass(frozen=True, slots=True)
class RenderedPage:
    page_number: int  # 1-based
    png_path: Path
    width_px: int
    height_px: int
    size_bytes: int
    text: str | None  # None when extraction disabled; "" is a real page with no text layer


@dataclass(frozen=True, slots=True)
class RefusedPage:
    page_number: int
    kind: PageRefusalKind
    detail: str


@dataclass(frozen=True, slots=True)
class DocumentRefusal:
    kind: DocumentRefusalKind
    detail: str
    page_count: int | None = None


@dataclass(frozen=True, slots=True)
class RasterizeResponse:
    page_count: int
    rendered: tuple[RenderedPage, ...]
    refused: tuple[RefusedPage, ...]


RasterizeOutcome = RasterizeResponse | DocumentRefusal
