"""Regression tests for the P3 blob / rasterize sweep findings (20261111-plugins-review).

One section per finding; each test fails at an assertion on the pre-fix tree.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.rasterize import identity as rasterize_identity
from elspeth.plugins.infrastructure.rasterize.protocol import (
    PageRefusalKind,
    RasterizeRequest,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer, RenderLimits
from elspeth.plugins.infrastructure.rasterize.worker import rasterize_document
from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand
from elspeth.plugins.transforms.pdf_rasterize import PDFRasterize
from elspeth.testing import make_pipeline_row
from elspeth.testing.rasterize_fakes import crashing_worker, raising_worker
from tests.fixtures.factories import make_context

# ---------------------------------------------------------------------------
# Finding #69 — blob_json_expand JSONL split must break on "\n" only.
# ---------------------------------------------------------------------------


def _field_jsonl_transform(*, fields: list[str]) -> BlobJSONExpand:
    return BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "jsonl",
            "fields": fields,
        }
    )


def _run_field_jsonl(text: str, *, fields: list[str]) -> Any:
    transform = _field_jsonl_transform(fields=fields)
    return transform.process(make_pipeline_row({"content": text}), make_context())


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
        pytest.param("\u0085", id="next-line"),
    ],
)
def test_jsonl_record_with_unicode_line_break_inside_a_string_parses(separator: str) -> None:
    """JSON allows U+2028/U+2029/U+0085 unescaped inside strings; JSONL splits on "\\n" only."""
    records = [{"title": f"first{separator}half"}, {"title": "second"}]
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    # Control: the encoder really wrote the separator raw (not as an escape).
    assert separator in text

    result = _run_field_jsonl(text, fields=["title"])

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert [row.to_dict()["title"] for row in result.rows] == [f"first{separator}half", "second"]


def test_jsonl_crlf_line_endings_still_parse() -> None:
    """Negative control for the split change: CRLF-terminated JSONL keeps parsing."""
    text = '{"title": "a"}\r\n{"title": "b"}\r\n'

    result = _run_field_jsonl(text, fields=["title"])

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert [row.to_dict()["title"] for row in result.rows] == ["a", "b"]


def test_jsonl_invalid_line_reports_its_newline_line_number() -> None:
    """Line numbers count "\\n"-terminated lines, not Unicode line breaks."""
    text = '{"title": "a\u2028b"}\n{"title": \n'

    result = _run_field_jsonl(text, fields=["title"])

    assert result.status == "error"
    assert result.reason["reason"] == "invalid_json"
    assert result.reason["phase"] == "jsonl_line"
    assert result.reason["line_number"] == 2


# ---------------------------------------------------------------------------
# Finding #70 — blob_json_expand rejects NaN / Infinity at the parse boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("parse_format", ["json", "jsonl"])
def test_non_finite_constant_is_quarantined_as_invalid_json(constant: str, parse_format: str) -> None:
    record = '{"title": "t", "score": ' + constant + "}"
    text = f"[{record}]" if parse_format == "json" else record + "\n"
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": parse_format,
            "fields": ["title", "score"],
        }
    )

    # On the pre-fix tree the parsed float('nan') escapes the parser and
    # process() raises while inferring the output contract; record that as an
    # assertion failure rather than letting it surface as a crash.
    raised: ValueError | None = None
    try:
        result = transform.process(make_pipeline_row({"content": text}), make_context())
    except ValueError as exc:
        raised = exc
    assert raised is None, f"non-finite constant escaped the JSON boundary: {raised!r}"

    assert result.status == "error"
    assert result.reason["reason"] == "invalid_json"
    assert result.reason["error_type"] == "non_finite_constant"
    assert result.reason["phase"] == ("document" if parse_format == "json" else "jsonl_line")


def test_finite_numbers_still_parse() -> None:
    """Negative control: ordinary floats are not refused by the constant hook."""
    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": "json",
            "fields": ["title", "score"],
        }
    )

    result = transform.process(make_pipeline_row({"content": '[{"title": "t", "score": 1.5e3}]'}), make_context())

    assert result.status == "success", result.reason
    assert result.rows is not None
    assert result.rows[0].to_dict()["score"] == 1500.0


# ---------------------------------------------------------------------------
# Finding #95 — max_blob_bytes must bound the READ, not just the decoded size.
# ---------------------------------------------------------------------------


class _ReadTrackingStore:
    """Payload store fake modelling both read contracts and recording which ran.

    ``retrieve_bounded`` follows contracts/payload_store.py: it reads at most
    ``max_bytes + 1`` bytes and returns None when the blob is larger.
    """

    def __init__(self, content: bytes) -> None:
        self.content_hash = hashlib.sha256(content).hexdigest()
        self._content = content
        self.unbounded_reads = 0
        self.bounded_reads: list[int] = []

    def _get(self, content_hash: str) -> bytes:
        if content_hash != self.content_hash:
            raise PayloadNotFoundError(content_hash)
        return self._content

    def retrieve(self, content_hash: str) -> bytes:
        self.unbounded_reads += 1
        return self._get(content_hash)

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        self.bounded_reads.append(max_bytes)
        content = self._get(content_hash)
        return None if len(content) > max_bytes else content

    def exists(self, content_hash: str) -> bool:
        return content_hash == self.content_hash

    def delete(self, content_hash: str) -> bool:
        return False


def _blob_transforms() -> list[Any]:
    return [
        pytest.param(
            lambda: BlobTextExpand(
                {"schema": {"mode": "observed", "guaranteed_fields": ["blob_ref"]}, "blob_ref_field": "blob_ref", "max_blob_bytes": 8}
            ),
            id="blob_text_expand",
        ),
        pytest.param(
            lambda: BlobCSVExpand(
                {"schema": {"mode": "observed", "guaranteed_fields": ["blob_ref"]}, "blob_ref_field": "blob_ref", "max_blob_bytes": 8}
            ),
            id="blob_csv_expand",
        ),
        pytest.param(
            lambda: BlobJSONExpand(
                {
                    "schema": {"mode": "observed", "guaranteed_fields": ["blob_ref"]},
                    "blob_ref_field": "blob_ref",
                    "format": "jsonl",
                    "fields": ["a"],
                    "max_blob_bytes": 8,
                }
            ),
            id="blob_json_expand",
        ),
    ]


@pytest.mark.parametrize("factory", _blob_transforms())
def test_oversize_blob_is_refused_through_a_bounded_read(factory: Any) -> None:
    body = b'{"a": "0123456789abcdef"}\n' * 4
    store = _ReadTrackingStore(body)
    transform = factory()
    transform._payload_store = store

    result = transform.process(make_pipeline_row({"blob_ref": store.content_hash}), make_context())

    assert store.unbounded_reads == 0, "the whole blob was read before max_blob_bytes was checked"
    assert store.bounded_reads == [8]
    assert result.status == "error"
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 8


@pytest.mark.parametrize("factory", _blob_transforms())
def test_blob_at_the_cap_is_still_read_in_full(factory: Any) -> None:
    """Negative control: a blob of exactly max_blob_bytes is not refused for size.

    The 8-byte body is not a valid document for every format (the CSV one has
    no data row), so the control asserts only that the size cap let it through.
    """
    body = b'{"a":1}\n'
    assert len(body) == 8
    store = _ReadTrackingStore(body)
    transform = factory()
    transform._payload_store = store

    result = transform.process(make_pipeline_row({"blob_ref": store.content_hash}), make_context())

    assert store.unbounded_reads == 0
    assert store.bounded_reads == [8]
    assert result.status == "success" or result.reason["reason"] != "blob_too_large", result.reason


# ---------------------------------------------------------------------------
# Finding #132 — PoolRenderer.render must not leak its temp output dir when it raises.
# ---------------------------------------------------------------------------


def _render_limits() -> RenderLimits:
    return RenderLimits(
        dpi=72,
        max_pages=10,
        max_page_pixels=1_000_000,
        max_page_bytes=5 * 1024 * 1024,
        max_total_bytes=50 * 1024 * 1024,
        render_timeout_seconds=20,
        worker_memory_limit_bytes=2 * 1024**3,
        extract_text=True,
        max_page_text_bytes=1024 * 1024,
    )


def _rasterize_dirs(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.name.startswith("elspeth-rasterize-"))


@pytest.mark.parametrize(
    "worker",
    [pytest.param(crashing_worker, id="worker-death"), pytest.param(raising_worker, id="worker-raises")],
)
def test_render_discards_its_output_dir_when_the_worker_fails_outside_the_protocol(
    worker: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    renderer = PoolRenderer(_render_limits(), worker=worker)
    try:
        with pytest.raises(RuntimeError, match="code bug"):
            renderer.render(b"%PDF-")
    finally:
        renderer.close()

    assert _rasterize_dirs(tmp_path) == [], "render() raised but left its caller-less output dir behind"


def test_render_discards_its_output_dir_when_on_submitted_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def _boom(_pids: tuple[int, ...]) -> None:
        raise LookupError("seam failure")

    renderer = PoolRenderer(_render_limits(), worker=raising_worker)
    try:
        with pytest.raises(LookupError, match="seam failure"):
            renderer.render(b"%PDF-", on_submitted=_boom)
    finally:
        renderer.close()

    assert _rasterize_dirs(tmp_path) == [], "render() raised but left its caller-less output dir behind"


def test_successful_render_still_hands_the_output_dir_to_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the success path must keep the dir for the caller to discard."""
    from tests.fixtures.pdf_documents import minimal_pdf

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    renderer = PoolRenderer(_render_limits())
    try:
        _result, output_dir = renderer.render(minimal_pdf(1))
        assert output_dir is not None
        assert output_dir.is_dir()
        assert _rasterize_dirs(tmp_path) == [output_dir.name]
        renderer.discard(output_dir)
    finally:
        renderer.close()
    assert _rasterize_dirs(tmp_path) == []


# ---------------------------------------------------------------------------
# Finding #156 — a MemoryError in page load / geometry / text extraction is a
# per-page MEMORY_EXHAUSTED refusal, not a whole-document failure.
# ---------------------------------------------------------------------------


def _out_of_memory_on_second_call(original: Callable[..., Any]) -> Callable[..., Any]:
    calls = {"n": 0}

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise MemoryError("simulated RLIMIT_AS")
        return original(*args, **kwargs)

    return _wrapped


@pytest.mark.parametrize(
    ("owner_name", "method"),
    [
        pytest.param("PdfDocument", "__getitem__", id="page-load"),
        pytest.param("PdfPage", "get_size", id="page-geometry"),
        pytest.param("PdfPage", "get_textpage", id="text-page"),
        pytest.param("PdfTextPage", "get_text_range", id="text-range"),
    ],
)
def test_memory_error_outside_page_render_refuses_only_that_page(
    owner_name: str, method: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pypdfium2

    from tests.fixtures.pdf_documents import minimal_pdf

    owner = {"PdfDocument": pypdfium2.PdfDocument, "PdfPage": pypdfium2.PdfPage, "PdfTextPage": pypdfium2.PdfTextPage}[owner_name]
    monkeypatch.setattr(owner, method, _out_of_memory_on_second_call(owner.__dict__[method]))
    request = RasterizeRequest(
        pdf_bytes=minimal_pdf(3),
        dpi=72,
        max_pages=10,
        max_page_pixels=1_000_000,
        max_page_bytes=5 * 1024 * 1024,
        max_total_bytes=50 * 1024 * 1024,
        output_dir=tmp_path,
        extract_text=True,
        max_page_text_bytes=1024 * 1024,
    )

    escaped: MemoryError | None = None
    try:
        outcome = rasterize_document(request)
    except MemoryError as exc:
        escaped = exc
    assert escaped is None, "a single page's MemoryError escaped and would refuse the whole document as a malformed parse"

    assert type(outcome) is RasterizeResponse
    assert [page.page_number for page in outcome.rendered] == [1, 3]
    assert len(outcome.refused) == 1
    refused = outcome.refused[0]
    assert type(refused) is RefusedPage
    assert refused.page_number == 2
    assert refused.kind is PageRefusalKind.MEMORY_EXHAUSTED


# ---------------------------------------------------------------------------
# Finding #162 — renderer_identity() must find the native PDFium binary on
# every platform pypdfium2 ships wheels for, not only libpdfium.so.
# ---------------------------------------------------------------------------


class _FakePdfiumDistribution:
    """Models the importlib.metadata.Distribution surface renderer_identity() reads."""

    def __init__(self, root: Path, files: list[str], version: str = "5.0.0") -> None:
        self._root = root
        self.files = [metadata.PackagePath(name) for name in files]
        self.version = version

    def locate_file(self, path: metadata.PackagePath) -> Path:
        return self._root / str(path)


def _identity_with(files: list[str], root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"native-binary:" + name.encode())
    distribution = _FakePdfiumDistribution(root, files)

    def _distribution(name: str) -> _FakePdfiumDistribution:
        assert name == "pypdfium2"
        return distribution

    monkeypatch.setattr(rasterize_identity.metadata, "distribution", _distribution)
    rasterize_identity.renderer_identity.cache_clear()
    try:
        return rasterize_identity.renderer_identity()
    finally:
        rasterize_identity.renderer_identity.cache_clear()


@pytest.mark.parametrize(
    "native_binary",
    [
        pytest.param("pypdfium2_raw/libpdfium.so", id="linux"),
        pytest.param("pypdfium2_raw/libpdfium.dylib", id="macos"),
        pytest.param("pypdfium2_raw/pdfium.dll", id="windows"),
    ],
)
def test_renderer_identity_hashes_the_platform_native_pdfium_binary(
    native_binary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = ["pypdfium2_raw/__init__.py", "pypdfium2_raw/bindings.py", native_binary]

    raised: RuntimeError | None = None
    try:
        identity = _identity_with(files, tmp_path, monkeypatch)
    except RuntimeError as exc:
        raised = exc
    assert raised is None, f"renderer_identity() refused {native_binary}: {raised}"

    assert len(identity) == 64
    assert all(char in "0123456789abcdef" for char in identity)


def test_renderer_identity_depends_on_the_native_binary_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The binary's bytes are part of the identity on a non-.so platform too."""
    files = ["pypdfium2_raw/pdfium.dll"]
    first = tmp_path / "a"
    second = tmp_path / "b"
    raised: RuntimeError | None = None
    try:
        identity_a = _identity_with(files, first, monkeypatch)
        (second / "pypdfium2_raw").mkdir(parents=True)
        identity_b = _identity_with(files, second, monkeypatch)
        (second / "pypdfium2_raw" / "pdfium.dll").write_bytes(b"a different build")
        identity_c = _identity_with_existing(files, second, monkeypatch)
    except RuntimeError as exc:
        raised = exc
    assert raised is None, f"renderer_identity() refused pdfium.dll: {raised}"

    assert identity_a == identity_b
    assert identity_c != identity_b


def _identity_with_existing(files: list[str], root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    distribution = _FakePdfiumDistribution(root, files)
    monkeypatch.setattr(rasterize_identity.metadata, "distribution", lambda _name: distribution)
    rasterize_identity.renderer_identity.cache_clear()
    try:
        return rasterize_identity.renderer_identity()
    finally:
        rasterize_identity.renderer_identity.cache_clear()


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(["pypdfium2_raw/__init__.py"], id="no-binary"),
        pytest.param(["pypdfium2_raw/libpdfium.so", "pypdfium2_raw/extra/libpdfium.so"], id="two-binaries"),
    ],
)
def test_renderer_identity_still_refuses_an_ambiguous_install(files: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: exactly one native PDFium binary is still required."""
    with pytest.raises(RuntimeError):
        _identity_with(files, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Finding #55 — the parent re-checks the worker's page text and geometry claims.
# ---------------------------------------------------------------------------

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


class _ClaimingRenderer:
    """Stands in for a compromised worker: writes real PNG files, makes arbitrary claims."""

    def __init__(self, pages: tuple[RenderedPage, ...]) -> None:
        self._pages = pages

    def render(self, pdf_bytes: bytes) -> tuple[RasterizeResponse, Path]:
        del pdf_bytes
        output_dir = Path(tempfile.mkdtemp(prefix="claiming-rasterize-"))
        written = []
        for page in self._pages:
            png_path = output_dir / f"page-{page.page_number}.png"
            png_path.write_bytes(_PNG_BYTES)
            written.append(
                RenderedPage(
                    page_number=page.page_number,
                    png_path=png_path,
                    width_px=page.width_px,
                    height_px=page.height_px,
                    size_bytes=len(_PNG_BYTES),
                    text=page.text,
                )
            )
        return RasterizeResponse(page_count=len(written), rendered=tuple(written), refused=()), output_dir

    def discard(self, output_dir: Path | None) -> None:
        if output_dir is not None:
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        pass


class _LifecycleContext:
    def __init__(self, payload_store: Any) -> None:
        self.payload_store = payload_store


def _claimed_page(number: int, *, text: str | None = "", width: Any = 200, height: Any = 100) -> RenderedPage:
    return RenderedPage(page_number=number, png_path=Path("unset"), width_px=width, height_px=height, size_bytes=0, text=text)


def _rasterize_with_claims(tmp_path: Path, pages: tuple[RenderedPage, ...], **options: Any) -> Any:
    store = FilesystemPayloadStore(tmp_path / "payloads")
    from tests.fixtures.pdf_documents import minimal_pdf

    pdf_ref = store.store(minimal_pdf(len(pages)))
    transform = PDFRasterize({"schema": {"mode": "observed"}, "on_page_failure": "emit_rendered", **options})
    transform.on_start(_LifecycleContext(store))
    transform.__dict__["_renderer"] = _ClaimingRenderer(pages)
    raised: Exception | None = None
    result = None
    try:
        result = transform.process(make_pipeline_row({"blob_ref": pdf_ref}), make_context())
    except Exception as exc:
        raised = exc
    assert raised is None, f"a worker claim the parent should refuse crashed the transform instead: {raised!r}"
    return result


def _refused_kinds(result: Any) -> dict[int, str]:
    return {entry["page_number"]: entry["kind"] for entry in result.success_reason["metadata"]["refused_pages"]}


@pytest.mark.parametrize(
    "oversize_text",
    [pytest.param("x" * 17, id="ascii"), pytest.param("\u00e9" * 9, id="multibyte-18-bytes-9-chars")],
)
def test_page_text_over_max_page_text_bytes_is_refused_by_the_parent(oversize_text: str, tmp_path: Path) -> None:
    pages = (_claimed_page(1, text=oversize_text), _claimed_page(2, text="ok"))

    result = _rasterize_with_claims(tmp_path, pages, max_page_text_bytes=16)

    assert result.status == "success", result.reason
    assert [row.to_dict()["page_number"] for row in result.rows] == [2], "the oversize page text was emitted"
    assert _refused_kinds(result) == {1: PageRefusalKind.OVERSIZE_TEXT.value}


def test_page_text_at_max_page_text_bytes_is_admitted(tmp_path: Path) -> None:
    """Negative control: exactly max_page_text_bytes is within the bound."""
    pages = (_claimed_page(1, text="x" * 16),)

    result = _rasterize_with_claims(tmp_path, pages, max_page_text_bytes=16)

    assert result.status == "success", result.reason
    assert [row.to_dict()["page_text"] for row in result.rows] == ["x" * 16]


def test_page_text_returned_when_extraction_is_disabled_is_refused(tmp_path: Path) -> None:
    pages = (_claimed_page(1, text="not requested"), _claimed_page(2, text=None))

    result = _rasterize_with_claims(tmp_path, pages, extract_text=False)

    assert result.status == "success", result.reason
    assert [row.to_dict()["page_number"] for row in result.rows] == [2]
    assert set(_refused_kinds(result)) == {1}


@pytest.mark.parametrize(
    ("width", "height", "kind"),
    [
        pytest.param(0, 100, PageRefusalKind.INVALID_GEOMETRY, id="zero-width"),
        pytest.param(200, -1, PageRefusalKind.INVALID_GEOMETRY, id="negative-height"),
        pytest.param(True, 100, PageRefusalKind.INVALID_GEOMETRY, id="bool-width"),
        pytest.param(200.0, 100, PageRefusalKind.INVALID_GEOMETRY, id="float-width"),
        pytest.param(2000, 2000, PageRefusalKind.OVERSIZE_PIXELS, id="over-max-page-pixels"),
    ],
)
def test_page_geometry_claims_are_checked_by_the_parent(width: Any, height: Any, kind: PageRefusalKind, tmp_path: Path) -> None:
    pages = (_claimed_page(1, width=width, height=height), _claimed_page(2))

    result = _rasterize_with_claims(tmp_path, pages, max_page_pixels=1_000_000)

    assert result.status == "success", result.reason
    assert [row.to_dict()["page_number"] for row in result.rows] == [2], "a page with an impossible geometry claim was emitted"
    assert _refused_kinds(result) == {1: kind.value}
