"""Regression tests for the P3 blob / rasterize sweep findings (20261111-plugins-review).

One section per finding; each test fails at an assertion on the pre-fix tree.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.payload_store import PayloadNotFoundError
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer, RenderLimits
from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand
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
