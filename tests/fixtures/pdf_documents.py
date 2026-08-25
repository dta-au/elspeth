"""Hand-built PDF documents for rasterizer tests (valid xref, no external tools)."""

from __future__ import annotations

from pathlib import Path

ENCRYPTED_PDF_PATH = Path(__file__).parent / "pdf" / "encrypted_aes128_user_secret.pdf"
MALFORMED_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
NOT_A_PDF = b"\x89PNG\r\n\x1a\nnot-a-pdf"


def minimal_pdf(page_count: int = 1, *, width_pt: float = 200.0, height_pt: float = 100.0) -> bytes:
    """Return a valid single-font PDF with ``page_count`` pages of the given MediaBox."""
    if page_count < 1:
        raise ValueError("page_count must be >= 1")
    objs: list[bytes] = []
    # 1 catalog, 2 pages root, 3 font, then per page: page + content stream
    first_page_obj = 4
    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(page_count))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(page_count):
        content_obj = first_page_obj + 2 * i + 1
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:g} {height_pt:g}] "
                f"/Contents {content_obj} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 24 Tf 20 40 Td (Page {i + 1}) Tj ET".encode()
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
