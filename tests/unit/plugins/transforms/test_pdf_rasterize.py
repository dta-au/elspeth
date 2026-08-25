"""Tests for the pdf_rasterize transform plugin."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.payload_store import IntegrityError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)
from elspeth.plugins.infrastructure.rasterize.renderer import RenderTimedOut
from elspeth.plugins.transforms.pdf_rasterize import PDFRasterize
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, minimal_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


class _FakeLifecycleContext:
    """Minimal LifecycleContext double — mirrors test_blob_rows.py's _FakeLifecycleContext."""

    def __init__(self, payload_store: Any) -> None:
        self.payload_store = payload_store


class _StubRenderer:
    """Scripted renderer: returns the queued result and writes the PNG files it promises."""

    def __init__(self, result: Any, png_bytes: tuple[bytes, ...] = ()) -> None:
        self._result = result
        self._png_bytes = png_bytes
        self.calls: list[bytes] = []
        self.discarded: list[Path | None] = []
        self.closed = False

    def render(self, pdf_bytes: bytes) -> tuple[Any, Path | None]:
        self.calls.append(pdf_bytes)
        output_dir = Path(tempfile.mkdtemp(prefix="stub-rasterize-"))
        if type(self._result) is RasterizeResponse:
            rendered = tuple(
                RenderedPage(
                    page_number=page.page_number,
                    png_path=output_dir / f"page-{page.page_number}.png",
                    width_px=page.width_px,
                    height_px=page.height_px,
                    size_bytes=len(data),
                    text=page.text,
                )
                for page, data in zip(self._result.rendered, self._png_bytes, strict=True)
            )
            for page, data in zip(rendered, self._png_bytes, strict=True):
                page.png_path.write_bytes(data)
            return (
                RasterizeResponse(page_count=self._result.page_count, rendered=rendered, refused=self._result.refused),
                output_dir,
            )
        return self._result, output_dir

    def discard(self, output_dir: Path | None) -> None:
        self.discarded.append(output_dir)
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        self.closed = True


def _page(number: int, text: str | None = "") -> RenderedPage:
    return RenderedPage(page_number=number, png_path=Path("unset"), width_px=200, height_px=100, size_bytes=0, text=text)


class _EscapingPathRenderer:
    """Returns a rendered page whose png_path escapes its own output_dir.

    Stands in for a compromised worker that names an arbitrary readable path
    instead of a file inside the render output directory it was given.
    """

    def __init__(self, escape_path: Path) -> None:
        self._escape_path = escape_path

    def render(self, pdf_bytes: bytes) -> tuple[RasterizeResponse, Path]:
        del pdf_bytes
        output_dir = Path(tempfile.mkdtemp(prefix="escaping-rasterize-"))
        page = RenderedPage(page_number=1, png_path=self._escape_path, width_px=10, height_px=10, size_bytes=4, text="")
        return RasterizeResponse(page_count=1, rendered=(page,), refused=()), output_dir

    def discard(self, output_dir: Path | None) -> None:
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        pass


class _LyingSizeRenderer:
    """Returns a rendered page whose claimed size_bytes does not match the real file."""

    def __init__(self, real_bytes: bytes, claimed_size: int) -> None:
        self._real_bytes = real_bytes
        self._claimed_size = claimed_size

    def render(self, pdf_bytes: bytes) -> tuple[RasterizeResponse, Path]:
        del pdf_bytes
        output_dir = Path(tempfile.mkdtemp(prefix="lying-size-rasterize-"))
        png_path = output_dir / "page-1.png"
        png_path.write_bytes(self._real_bytes)
        page = RenderedPage(page_number=1, png_path=png_path, width_px=10, height_px=10, size_bytes=self._claimed_size, text="")
        return RasterizeResponse(page_count=1, rendered=(page,), refused=()), output_dir

    def discard(self, output_dir: Path | None) -> None:
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        pass


def _transform(store: FilesystemPayloadStore, renderer: Any, **options: Any) -> PDFRasterize:
    transform = PDFRasterize({"schema": {"mode": "observed"}, **options})
    transform.on_start(_FakeLifecycleContext(store))
    transform.__dict__["_renderer"] = renderer
    return transform


@pytest.fixture
def store(tmp_path: Path) -> FilesystemPayloadStore:
    return FilesystemPayloadStore(tmp_path / "payloads")


def test_emits_one_row_per_page_with_page_metadata_and_parent_fields(store: FilesystemPayloadStore) -> None:
    pdf_ref = store.store(minimal_pdf(2))
    renderer = _StubRenderer(RasterizeResponse(page_count=2, rendered=(_page(1), _page(2)), refused=()), (PNG, PNG + b"x"))
    transform = _transform(store, renderer)
    result = transform.process(make_pipeline_row({"blob_ref": pdf_ref, "blob_filename": "a.pdf"}), make_context())
    assert result.status == "success" and result.is_multi_row
    rows = [row.to_dict() for row in result.rows]
    assert [row["page_number"] for row in rows] == [1, 2]
    assert {row["document_id"] for row in rows} == {pdf_ref}
    assert all(row["blob_ref"] == pdf_ref and row["blob_filename"] == "a.pdf" for row in rows)
    assert all(row["page_mime_type"] == "image/png" for row in rows)
    assert [store.retrieve(row["page_blob_ref"]) for row in rows] == [PNG, PNG + b"x"]
    assert [row["page_size_bytes"] for row in rows] == [len(PNG), len(PNG) + 1]
    assert rows[0]["page_width_px"] == 200 and rows[0]["page_height_px"] == 100
    assert result.rows[0].contract is result.rows[1].contract
    assert result.success_reason["action"] == "expanded_blob"
    assert result.success_reason["metadata"]["page_count"] == 2
    assert result.success_reason["metadata"]["refused_pages"] == []
    assert renderer.discarded and renderer.discarded[0] is not None and not renderer.discarded[0].exists()


def test_page_png_path_outside_output_dir_raises_containment_error(store: FilesystemPayloadStore, tmp_path: Path) -> None:
    """A worker-returned png_path outside its own render output_dir is a containment

    breach, not a document problem: the parent must refuse to read and publish it
    rather than trust an arbitrary readable path (e.g. a credentials file) named by a
    compromised worker.
    """
    ref = store.store(minimal_pdf(1))
    escape_target = tmp_path / "not-the-output-dir" / "page-1.png"
    escape_target.parent.mkdir()
    escape_target.write_bytes(b"attacker-controlled-bytes")
    transform = _transform(store, _EscapingPathRenderer(escape_target))
    with pytest.raises(RuntimeError, match="containment breach"):
        transform.process(make_pipeline_row({"blob_ref": ref}), make_context())


def test_emitted_page_size_is_the_real_byte_length_not_the_workers_claim(store: FilesystemPayloadStore) -> None:
    """page_size_bytes must reflect the bytes actually read from disk, never a

    worker-claimed size_bytes value — the worker's claim about its own output is not
    trustworthy any more than the path it names.
    """
    ref = store.store(minimal_pdf(1))
    real_bytes = PNG + b"extra-bytes-the-worker-did-not-count-in-its-claim"
    transform = _transform(store, _LyingSizeRenderer(real_bytes, claimed_size=1))
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "success"
    assert result.rows[0]["page_size_bytes"] == len(real_bytes)
    assert store.retrieve(result.rows[0]["page_blob_ref"]) == real_bytes


def test_encrypted_document_is_a_typed_row_error_not_a_crash(store: FilesystemPayloadStore) -> None:
    ref = store.store(ENCRYPTED_PDF_PATH.read_bytes())
    transform = _transform(store, _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail="Incorrect password")))
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.retryable is False
    assert result.reason["reason"] == "pdf_encrypted"
    assert result.reason["blob_ref"] == ref


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        (DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="Data format error"), "pdf_malformed"),
        (DocumentRefusal(kind=DocumentRefusalKind.TOO_MANY_PAGES, detail="900 pages", page_count=900), "too_many_rows"),
        (RenderTimedOut(timeout_seconds=120), "render_timeout"),
    ],
)
def test_document_level_refusals_map_to_typed_reasons(store: FilesystemPayloadStore, refusal: Any, reason: str) -> None:
    ref = store.store(minimal_pdf(1))
    result = _transform(store, _StubRenderer(refusal)).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.reason["reason"] == reason


def test_fail_document_refuses_the_whole_row_when_any_page_is_refused(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(2))
    response = RasterizeResponse(page_count=2, rendered=(_page(1),), refused=(RefusedPage(2, PageRefusalKind.RENDER_ERROR, "boom"),))
    result = _transform(store, _StubRenderer(response, (PNG,))).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.reason["reason"] == "pdf_page_render_failed"
    assert result.reason["refused_pages"] == [{"page_number": 2, "kind": "render_error", "detail": "boom"}]
    # nothing was persisted for a refused document
    assert not store.exists(hashlib.sha256(PNG).hexdigest())


def test_fail_document_uses_too_large_reason_for_size_refusals(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    response = RasterizeResponse(page_count=1, rendered=(), refused=(RefusedPage(1, PageRefusalKind.OVERSIZE_BYTES, "big"),))
    result = _transform(store, _StubRenderer(response)).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.reason["reason"] == "pdf_page_too_large"


def test_fully_empty_response_is_pdf_malformed_not_a_crash(store: FilesystemPayloadStore) -> None:
    """A renderer reporting zero pages AND zero refusals (page_count=0, nothing to explain

    why) must not crash building an output row from an empty rendered tuple — it is a
    typed pdf_malformed row error instead. Regression test for fix round 1 finding #1.
    """
    ref = store.store(minimal_pdf(1))
    response = RasterizeResponse(page_count=0, rendered=(), refused=())
    result = _transform(store, _StubRenderer(response)).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error"
    assert result.reason["reason"] == "pdf_malformed"
    assert result.reason["blob_ref"] == ref
    assert result.reason["detail"] == "document has no pages"


def test_zero_survivors_with_refusals_maps_per_kind_regardless_of_policy(store: FilesystemPayloadStore) -> None:
    """The zero-rendered-with-refusals path is unconditional on on_page_failure — confirms

    the fix-round-1 restructure (moving this check ahead of the fail_document/emit_rendered
    branch) preserves the existing per-kind mapping under emit_rendered too.
    """
    ref = store.store(minimal_pdf(1))
    response = RasterizeResponse(page_count=1, rendered=(), refused=(RefusedPage(1, PageRefusalKind.OVERSIZE_PIXELS, "huge"),))
    result = _transform(store, _StubRenderer(response), on_page_failure="emit_rendered").process(
        make_pipeline_row({"blob_ref": ref}), make_context()
    )
    assert result.status == "error" and result.reason["reason"] == "pdf_page_too_large"


def test_emit_rendered_emits_surviving_pages_and_records_the_gaps(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(3))
    response = RasterizeResponse(
        page_count=3, rendered=(_page(1), _page(3)), refused=(RefusedPage(2, PageRefusalKind.MEMORY_EXHAUSTED, "oom"),)
    )
    transform = _transform(store, _StubRenderer(response, (PNG, PNG)), on_page_failure="emit_rendered")
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "success"
    assert [row["page_number"] for row in result.rows] == [1, 3]
    assert result.success_reason["metadata"]["refused_pages"] == [{"page_number": 2, "kind": "memory_exhausted", "detail": "oom"}]


def test_emit_rendered_with_zero_survivors_is_still_a_row_error(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    response = RasterizeResponse(page_count=1, rendered=(), refused=(RefusedPage(1, PageRefusalKind.RENDER_ERROR, "boom"),))
    result = _transform(store, _StubRenderer(response), on_page_failure="emit_rendered").process(
        make_pipeline_row({"blob_ref": ref}), make_context()
    )
    assert result.status == "error" and result.reason["reason"] == "pdf_page_render_failed"


def test_input_validation_precedes_rendering(store: FilesystemPayloadStore) -> None:
    renderer = _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="never called"))
    transform = _transform(store, renderer)
    missing = transform.process(make_pipeline_row({"other": 1}), make_context())
    assert missing.reason["reason"] == "missing_field"
    bad_ref = transform.process(make_pipeline_row({"blob_ref": "nope"}), make_context())
    assert bad_ref.reason["reason"] == "invalid_input" and bad_ref.reason["error_type"] == "invalid_blob_ref"
    absent = transform.process(make_pipeline_row({"blob_ref": "0" * 64}), make_context())
    assert absent.reason["reason"] == "blob_not_found"
    not_pdf = transform.process(make_pipeline_row({"blob_ref": store.store(PNG)}), make_context())
    assert not_pdf.reason["reason"] == "invalid_input" and not_pdf.reason["error_type"] == "document_signature_mismatch"
    with pytest.raises(TypeError):
        transform.process(make_pipeline_row({"blob_ref": 12}), make_context())  # Tier-2 type violation: crash (blob_csv_expand precedent)
    assert renderer.calls == []


def test_input_over_max_input_bytes_is_blob_too_large(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    transform = _transform(store, _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="never")), max_input_bytes=64)
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.reason["reason"] == "blob_too_large"


def test_payload_store_integrity_error_propagates(store: FilesystemPayloadStore, tmp_path: Path) -> None:
    ref = store.store(minimal_pdf(1))
    (tmp_path / "payloads" / ref[:2] / ref).write_bytes(b"%PDF-tampered")
    transform = _transform(store, _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="never")))
    with pytest.raises(IntegrityError):
        transform.process(make_pipeline_row({"blob_ref": ref}), make_context())


def test_real_renderer_end_to_end(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(2))
    transform = PDFRasterize({"schema": {"mode": "observed"}, "dpi": 72})
    transform.on_start(_FakeLifecycleContext(store))
    try:
        result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    finally:
        transform.close()
    assert result.status == "success"
    assert [row["page_number"] for row in result.rows] == [1, 2]
    assert store.retrieve(result.rows[0]["page_blob_ref"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert [row["page_text"] for row in result.rows] == ["Page 1", "Page 2"]


def test_real_renderer_extract_text_false_omits_page_text(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    transform = PDFRasterize({"schema": {"mode": "observed"}, "dpi": 72, "extract_text": False})
    transform.on_start(_FakeLifecycleContext(store))
    try:
        result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    finally:
        transform.close()
    assert result.status == "success"
    assert "page_text" not in result.rows[0].to_dict()


def test_importing_pdf_rasterize_does_not_pull_in_pypdfium2() -> None:
    """``pypdfium2`` must stay WORKER-ONLY (spawned render subprocess), never imported

    at module scope by ``pdf_rasterize.py`` or the modules it imports at import time
    (``renderer.py``, ``protocol.py``) — otherwise every process that merely imports
    the plugin (discovery, the main engine process, test collection) pays the native
    library's import cost and inherits its failure modes. Run in a fresh subprocess:
    the current test session may have already imported ``pypdfium2`` via an earlier,
    unrelated real-renderer test, which would hide the leak if checked in-process.
    """
    import os
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[4]
    code = (
        "import sys\n"
        "import elspeth.plugins.transforms.pdf_rasterize\n"
        "assert 'pypdfium2' not in sys.modules, sorted(m for m in sys.modules if 'pypdfium2' in m)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo_root / "src"))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr


class TestConfig:
    def test_input_field_may_not_name_an_emitted_field(self) -> None:
        # The @model_validator's field-collision check fires before
        # _reject_input_options_naming_created_fields runs in __init__, and
        # TransformDataConfig.from_dict() wraps the pydantic ValidationError
        # as PluginConfigError (not ValueError) — see controller ruling #1.
        with pytest.raises(PluginConfigError, match="page_blob_ref"):
            PDFRasterize({"schema": {"mode": "observed"}, "blob_ref_field": "page_blob_ref"})

    def test_emitted_field_names_must_be_distinct(self) -> None:
        with pytest.raises(PluginConfigError, match="distinct"):
            PDFRasterize({"schema": {"mode": "observed"}, "page_number_field": "document_id"})

    @pytest.mark.parametrize(
        ("option", "value"),
        [("dpi", 301), ("dpi", 35), ("max_page_bytes", 5 * 1024 * 1024 + 1), ("max_pages", 2001), ("on_page_failure", "ignore")],
    )
    def test_ceilings_are_hard(self, option: str, value: Any) -> None:
        with pytest.raises(PluginConfigError):
            PDFRasterize({"schema": {"mode": "observed"}, option: value})

    def test_declares_created_fields_and_probe(self) -> None:
        transform = PDFRasterize(PDFRasterize.probe_config())
        assert transform.declared_output_fields == frozenset(
            {
                "page_blob_ref",
                "page_number",
                "document_id",
                "page_mime_type",
                "page_size_bytes",
                "page_width_px",
                "page_height_px",
                "page_text",
            }
        )
        assert PDFRasterize.creates_tokens is True and PDFRasterize.passes_through_input is True

    def test_page_text_field_may_not_collide_with_another_emitted_field(self) -> None:
        with pytest.raises(PluginConfigError, match="distinct"):
            PDFRasterize({"schema": {"mode": "observed"}, "page_text_field": "document_id"})

    def test_blob_ref_field_may_not_name_page_text_field(self) -> None:
        with pytest.raises(PluginConfigError, match="page_text"):
            PDFRasterize({"schema": {"mode": "observed"}, "blob_ref_field": "page_text"})

    def test_extract_text_false_omits_declared_field(self) -> None:
        transform = PDFRasterize({"schema": {"mode": "observed"}, "extract_text": False})
        assert "page_text" not in transform.declared_output_fields


def test_registers_via_builtin_discovery() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    assert manager.get_transform_by_name("pdf_rasterize").name == "pdf_rasterize"
