from __future__ import annotations

import pickle
from pathlib import Path

from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeRequest,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)


def test_messages_round_trip_through_pickle() -> None:
    request = RasterizeRequest(
        pdf_bytes=b"%PDF-",
        dpi=72,
        max_pages=1,
        max_page_pixels=10,
        max_page_bytes=10,
        output_dir=Path("/tmp/x"),
        extract_text=True,
        max_page_text_bytes=1024 * 1024,
    )
    response = RasterizeResponse(
        page_count=2,
        rendered=(RenderedPage(page_number=1, png_path=Path("/tmp/x/page-1.png"), width_px=1, height_px=1, size_bytes=70, text="hello"),),
        refused=(RefusedPage(page_number=2, kind=PageRefusalKind.RENDER_ERROR, detail="boom"),),
    )
    refusal = DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail="password", page_count=None)
    for message in (request, response, refusal):
        assert pickle.loads(pickle.dumps(message)) == message


def test_discriminator_values_stay_outside_the_terminal_vocabulary() -> None:
    forbidden = {"success", "failure", "transient", "completed", "failed", "quarantined", "buffered", "coalesced"}
    values = {member.value for member in DocumentRefusalKind} | {member.value for member in PageRefusalKind}
    assert values.isdisjoint(forbidden)
