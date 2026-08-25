from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeRequest,
    RasterizeResponse,
)
from elspeth.plugins.infrastructure.rasterize.worker import rasterize_document
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, MALFORMED_PDF, NOT_A_PDF, minimal_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _request(pdf: bytes, tmp_path: Path, **overrides: int) -> RasterizeRequest:
    values = {"dpi": 72, "max_pages": 10, "max_page_pixels": 1_000_000, "max_page_bytes": 5 * 1024 * 1024}
    values.update(overrides)
    return RasterizeRequest(pdf_bytes=pdf, output_dir=tmp_path, **values)


def test_renders_every_page_to_png_files_in_order(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(3), tmp_path))
    assert type(result) is RasterizeResponse
    assert result.page_count == 3
    assert result.refused == ()
    assert [page.page_number for page in result.rendered] == [1, 2, 3]
    for page in result.rendered:
        data = page.png_path.read_bytes()
        assert data[:8] == PNG_MAGIC
        assert page.size_bytes == len(data)
        assert (page.width_px, page.height_px) == (200, 100)  # 200x100 pt at 72 dpi
        assert page.png_path.parent == tmp_path


def test_dpi_scales_geometry_by_ceil(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(1, width_pt=100.5, height_pt=50.0), tmp_path, dpi=144))
    assert type(result) is RasterizeResponse
    assert (result.rendered[0].width_px, result.rendered[0].height_px) == (201, 100)


def test_encrypted_document_is_refused_as_encrypted(tmp_path: Path) -> None:
    result = rasterize_document(_request(ENCRYPTED_PDF_PATH.read_bytes(), tmp_path))
    assert result == DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail=result.detail, page_count=None)
    assert "password" in result.detail.lower()


@pytest.mark.parametrize("payload", [MALFORMED_PDF, NOT_A_PDF, b""])
def test_unparseable_document_is_refused_as_malformed(tmp_path: Path, payload: bytes) -> None:
    result = rasterize_document(_request(payload, tmp_path))
    assert type(result) is DocumentRefusal
    assert result.kind is DocumentRefusalKind.MALFORMED


def test_page_cap_refuses_the_whole_document_before_rendering(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(3), tmp_path, max_pages=2))
    assert result == DocumentRefusal(kind=DocumentRefusalKind.TOO_MANY_PAGES, detail=result.detail, page_count=3)
    assert list(tmp_path.iterdir()) == []


def test_oversize_page_is_refused_from_declared_size_without_rendering(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(2), tmp_path, max_page_pixels=100))
    assert type(result) is RasterizeResponse
    assert result.rendered == ()
    assert [(page.page_number, page.kind) for page in result.refused] == [
        (1, PageRefusalKind.OVERSIZE_PIXELS),
        (2, PageRefusalKind.OVERSIZE_PIXELS),
    ]
    assert list(tmp_path.iterdir()) == []


def test_oversize_png_is_refused_and_its_file_removed(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(1), tmp_path, max_page_bytes=64))
    assert type(result) is RasterizeResponse
    assert result.rendered == ()
    assert result.refused[0].kind is PageRefusalKind.OVERSIZE_BYTES
    assert list(tmp_path.iterdir()) == []


def test_worker_module_does_not_import_pypdfium2_at_module_scope() -> None:
    import elspeth.plugins.infrastructure.rasterize.protocol  # noqa: F401  (protocol must be import-clean)

    source = Path(rasterize_document.__code__.co_filename).read_text()
    module_level = [line for line in source.splitlines() if line.startswith("import pypdfium2") or line.startswith("from pypdfium2")]
    assert module_level == []
