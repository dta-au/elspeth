"""Rasterize worker: the ONLY module that imports pypdfium2, and only inside a function.

Runs in a spawn-context subprocess owned by ``renderer.PoolRenderer``. Everything
crossing the boundary is a ``protocol`` message. Document/page problems become
refusals (Tier-2 data outcomes); anything else raises and is treated as a code bug
by the parent.
"""

from __future__ import annotations

import math
import signal
import sys
from pathlib import Path

from elspeth.plugins.infrastructure.rasterize.png import encode_rgb_png
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeOutcome,
    RasterizeRequest,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)

_PDFIUM_ERR_PASSWORD = 4
_PDFIUM_ERR_SECURITY = 5
_POINTS_PER_INCH = 72.0


class CpuBudgetExceeded(Exception):
    """Raised inside the worker when RLIMIT_CPU's soft limit delivers SIGXCPU."""


def _raise_cpu_budget(signum: int, frame: object) -> None:
    raise CpuBudgetExceeded(f"worker exceeded its CPU budget (signal {signum})")


def worker_initializer(memory_limit_bytes: int, cpu_seconds: int) -> None:
    """Apply per-worker resource limits. Linux-only; a no-op elsewhere."""
    if not sys.platform.startswith("linux"):
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
    signal.signal(signal.SIGXCPU, _raise_cpu_budget)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))


def _pdfium_error_code(exc: BaseException) -> int | None:
    # PdfiumError carries ``err_code`` (pypdfium2/_helpers/misc.py:7-21); parse it, never isinstance.
    code = exc.__dict__["err_code"] if "err_code" in exc.__dict__ else None
    return code if type(code) is int else None


def rasterize_document(request: RasterizeRequest) -> RasterizeOutcome:
    """Parse once, render every page, write PNGs into ``request.output_dir``."""
    import pypdfium2  # deferred: initialises libpdfium + registers atexit in THIS process only
    from pypdfium2 import PdfiumError

    try:
        document = pypdfium2.PdfDocument(request.pdf_bytes)
    except PdfiumError as exc:
        code = _pdfium_error_code(exc)
        if code in (_PDFIUM_ERR_PASSWORD, _PDFIUM_ERR_SECURITY):
            return DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail=str(exc))
        return DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail=str(exc))
    except (ValueError, TypeError) as exc:  # pypdfium2 rejects empty/odd inputs before pdfium sees them
        return DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail=str(exc))

    try:
        page_count = len(document)
        if page_count > request.max_pages:
            return DocumentRefusal(
                kind=DocumentRefusalKind.TOO_MANY_PAGES,
                detail=f"document declares {page_count} pages; max_pages is {request.max_pages}",
                page_count=page_count,
            )
        scale = request.dpi / _POINTS_PER_INCH
        rendered: list[RenderedPage] = []
        refused: list[RefusedPage] = []
        for index in range(page_count):
            page_number = index + 1
            outcome = _render_page(document, index, page_number, scale, request)
            if isinstance(outcome, RenderedPage):  # ADR-032: nominal isinstance against an ELSPETH-owned dataclass
                rendered.append(outcome)
            else:
                refused.append(outcome)
        return RasterizeResponse(page_count=page_count, rendered=tuple(rendered), refused=tuple(refused))
    finally:
        document.close()


def _render_page(document: object, index: int, page_number: int, scale: float, request: RasterizeRequest) -> RenderedPage | RefusedPage:
    from pypdfium2 import PdfiumError

    try:
        page = document[index]  # type: ignore[index]  # PdfDocument.__getitem__; parsed, not typed
    except PdfiumError as exc:
        return RefusedPage(page_number=page_number, kind=PageRefusalKind.RENDER_ERROR, detail=str(exc))
    try:
        width_pt, height_pt = page.get_size()
        width_px = math.ceil(width_pt * scale)
        height_px = math.ceil(height_pt * scale)
        if width_px <= 0 or height_px <= 0 or width_px * height_px > request.max_page_pixels:
            return RefusedPage(
                page_number=page_number,
                kind=PageRefusalKind.OVERSIZE_PIXELS,
                detail=f"page would render to {width_px}x{height_px} px; max_page_pixels is {request.max_page_pixels}",
            )
        try:
            bitmap = page.render(scale=scale, rev_byteorder=True, draw_annots=False)
            png = encode_rgb_png(bitmap.buffer, width=bitmap.width, height=bitmap.height, stride=bitmap.stride)
        except MemoryError:
            return RefusedPage(
                page_number=page_number, kind=PageRefusalKind.MEMORY_EXHAUSTED, detail="render exceeded the worker memory limit"
            )
        except PdfiumError as exc:
            return RefusedPage(page_number=page_number, kind=PageRefusalKind.RENDER_ERROR, detail=str(exc))
        if len(png) > request.max_page_bytes:
            return RefusedPage(
                page_number=page_number,
                kind=PageRefusalKind.OVERSIZE_BYTES,
                detail=f"encoded page is {len(png)} bytes; max_page_bytes is {request.max_page_bytes}",
            )
        png_path = Path(request.output_dir) / f"page-{page_number}.png"
        png_path.write_bytes(png)
        return RenderedPage(page_number=page_number, png_path=png_path, width_px=bitmap.width, height_px=bitmap.height, size_bytes=len(png))
    finally:
        page.close()
