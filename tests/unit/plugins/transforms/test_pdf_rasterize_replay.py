"""Future PDF render receipts replay without a worker and verify completely."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts.call_mode import ReplayCallEvidence, VerificationDecision
from elspeth.contracts.enums import CallStatus, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.replay_payload_store import SourceBoundPayloadStore
from elspeth.plugins.infrastructure.rasterize.protocol import RasterizeResponse, RenderedPage
from elspeth.plugins.transforms.pdf_rasterize import PDFRasterize
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.pdf_documents import minimal_pdf

_PNG = b"\x89PNG\r\n\x1a\n" + b"audited-pdf-page"


class _OnePageRenderer:
    def __init__(self, *, fail_if_called: bool = False, png: bytes = _PNG, worker_claimed_size: int | None = None) -> None:
        self.calls = 0
        self.fail_if_called = fail_if_called
        self.png = png
        self.worker_claimed_size = worker_claimed_size

    def render(self, pdf_bytes: bytes) -> tuple[RasterizeResponse, Path]:
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("replay launched the PDF renderer")
        assert pdf_bytes.startswith(b"%PDF")
        directory = Path(tempfile.mkdtemp(prefix="pdf-receipt-test-"))
        path = directory / "page-1.png"
        path.write_bytes(self.png)
        page = RenderedPage(
            page_number=1,
            png_path=path,
            width_px=20,
            height_px=10,
            size_bytes=self.worker_claimed_size if self.worker_claimed_size is not None else len(self.png),
            text="page",
        )
        return RasterizeResponse(page_count=1, rendered=(page,), refused=()), directory

    def discard(self, output_dir: Path | None) -> None:
        if output_dir is not None:
            shutil.rmtree(output_dir)

    def close(self) -> None:
        pass


def _transform(store: Any, renderer: _OnePageRenderer) -> PDFRasterize:
    transform = PDFRasterize({"schema": {"mode": "observed"}})
    transform.on_start(SimpleNamespace(payload_store=store))
    transform._renderer = renderer
    return transform


def _context(mode: RunMode) -> Any:
    ctx = make_context()
    ctx.run_mode = mode
    ctx.landscape.allocate_call_index.return_value = 0
    ctx.landscape.record_call.return_value = SimpleNamespace(call_id="new-render-call")
    return ctx


def test_live_receipt_replays_without_worker_and_restores_exact_page(tmp_path: Path) -> None:
    source_store = FilesystemPayloadStore(tmp_path / "source")
    pdf_ref = source_store.store(minimal_pdf(1))
    row = make_pipeline_row({"blob_ref": pdf_ref, "name": "original.pdf"})
    live_ctx = _context(RunMode.LIVE)
    live_renderer = _OnePageRenderer(worker_claimed_size=1)
    live_result = _transform(source_store, live_renderer).process(row, live_ctx)
    assert live_result.status == "success"
    receipt = live_ctx.landscape.record_call.call_args.kwargs["response_data"].to_dict()
    assert receipt["format"] == "pdf_rasterize/v1"
    assert len(receipt["renderer_identity"]) == 64
    page_ref = hashlib.sha256(_PNG).hexdigest()
    assert receipt["rendered"][0]["page_ref"] == page_ref
    assert receipt["rendered"][0]["size_bytes"] == len(_PNG)
    assert receipt["rendered"][0]["worker_size_bytes"] == 1
    assert receipt["rows"][0]["page_blob_ref"] == page_ref

    current_store = FilesystemPayloadStore(tmp_path / "new-audit")
    bounded_store = SourceBoundPayloadStore(
        mode=RunMode.REPLAY,
        source_store=source_store,
        current_store=current_store,
        source_refs={pdf_ref},
        output_refs={page_ref},
    )
    replay_ctx = _context(RunMode.REPLAY)
    replay_ctx.call_mode_session = SimpleNamespace(
        replay_call=lambda **kwargs: ReplayCallEvidence(
            source_call_id="source-render-call",
            status=CallStatus.SUCCESS,
            response_data=receipt,
            error_data=None,
            latency_ms=1.0,
        )
    )
    replay_renderer = _OnePageRenderer(fail_if_called=True)
    replayed = _transform(bounded_store, replay_renderer).process(row, replay_ctx)

    assert replay_renderer.calls == 0
    assert [item.to_dict() for item in replayed.rows] == [item.to_dict() for item in live_result.rows]
    assert current_store.retrieve(page_ref) == _PNG
    assert replay_ctx.landscape.record_call.call_args.kwargs["source_call_id"] == "source-render-call"

    legacy_receipt = dict(receipt)
    del legacy_receipt["renderer_identity"]
    legacy_ctx = _context(RunMode.REPLAY)
    legacy_ctx.call_mode_session = SimpleNamespace(
        replay_call=lambda **kwargs: ReplayCallEvidence(
            source_call_id="legacy-render-call",
            status=CallStatus.SUCCESS,
            response_data=legacy_receipt,
            error_data=None,
            latency_ms=1.0,
        )
    )
    legacy_store = FilesystemPayloadStore(tmp_path / "legacy-audit")
    legacy_bounded = SourceBoundPayloadStore(
        mode=RunMode.REPLAY,
        source_store=source_store,
        current_store=legacy_store,
        source_refs={pdf_ref},
        output_refs={page_ref},
    )
    legacy_renderer = _OnePageRenderer(fail_if_called=True)
    with pytest.raises(AuditIntegrityError, match="renderer identity"):
        _transform(legacy_bounded, legacy_renderer).process(row, legacy_ctx)
    assert legacy_renderer.calls == 0
    assert not legacy_store.exists(page_ref)


def test_verify_persists_full_comparison_and_fails_on_changed_page(tmp_path: Path) -> None:
    source_store = FilesystemPayloadStore(tmp_path / "payloads")
    pdf_ref = source_store.store(minimal_pdf(1))
    row = make_pipeline_row({"blob_ref": pdf_ref})
    context = _context(RunMode.VERIFY)
    seen: list[dict[str, Any]] = []

    def compare(**kwargs: Any) -> VerificationDecision:
        seen.append(kwargs["live_response_data"])
        return VerificationDecision(
            current_call_id=kwargs["current_call_id"],
            source_call_id="source-render-call",
            is_match=False,
            differences={"page_ref": "changed"},
        )

    context.call_mode_session = SimpleNamespace(admit_verify_call=lambda **kwargs: "source-render-call", verify_call=compare)
    changed_renderer = _OnePageRenderer(png=_PNG + b"changed")
    with pytest.raises(AuditIntegrityError, match="differs"):
        _transform(source_store, changed_renderer).process(row, context)
    assert changed_renderer.calls == 1
    assert seen and seen[0]["rendered"][0]["page_ref"] == hashlib.sha256(_PNG + b"changed").hexdigest()
    assert context.landscape.record_call.called


def test_verify_missing_source_request_does_not_launch_renderer(tmp_path: Path) -> None:
    store = FilesystemPayloadStore(tmp_path / "payloads")
    pdf_ref = store.store(minimal_pdf(1))
    context = _context(RunMode.VERIFY)

    def refuse(**kwargs: Any) -> str:
        raise AuditIntegrityError("source request missing")

    context.call_mode_session = SimpleNamespace(admit_verify_call=refuse)
    renderer = _OnePageRenderer()
    with pytest.raises(AuditIntegrityError, match="source request missing"):
        _transform(store, renderer).process(make_pipeline_row({"blob_ref": pdf_ref}), context)
    assert renderer.calls == 0
    assert not context.landscape.record_call.called
