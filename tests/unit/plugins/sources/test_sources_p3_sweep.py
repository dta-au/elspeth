"""Regression tests for P3 source/blob findings from the 20261111 plugins review sweep."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.plugins.infrastructure.results import TransformResult
from tests.fixtures.factories import make_source_context
from tests.unit.plugins.sources.test_aws_s3_source import _source_for


class TestS3CSVBlankLinesBeforeHeader:
    """Sweep finding #11: a blank first line must not become an empty header."""

    def test_leading_blank_line_is_skipped_before_header(self) -> None:
        source, _, ctx = _source_for(b"\nid\n1\n", format="csv")
        rows = list(source.load(ctx))

        assert [(row.is_quarantined, row.row) for row in rows] == [(False, {"id": "1"})]
        assert ctx.validation_errors == []

    @pytest.mark.parametrize("payload", [b"\n\n", b"\r\n"], ids=["lf-only", "crlf-only"])
    def test_blank_only_object_is_recorded_as_missing_header(self, payload: bytes) -> None:
        source, _, ctx = _source_for(payload, format="csv")
        rows = list(source.load(ctx))

        assert [row.is_quarantined for row in rows] == [True]
        assert rows[0].quarantine_error == "CSV parse error: empty file contains no header row"
        assert [error["error"] for error in ctx.validation_errors] == ["CSV parse error: empty file contains no header row"]


def _utf16le(text: str) -> bytes:
    return text.encode("utf-16-le")


class TestJSONLUndecodableCodeUnitIsolatedToItsLine:
    """Sweep finding #42: one undecodable UTF-16 code unit must not drop its neighbours."""

    @pytest.mark.parametrize("bad_unit", [b"\x00\xdc", b"\x00\xd8"], ids=["lone-low-surrogate", "lone-high-surrogate"])
    def test_lines_before_and_after_bad_code_unit_are_read(self, tmp_path: Path, bad_unit: bytes) -> None:
        from elspeth.plugins.sources.json_source import JSONSource

        jsonl_file = tmp_path / "data.jsonl"
        jsonl_file.write_bytes(_utf16le('{"id": 1}\n') + bad_unit + _utf16le('\n{"id": 3}\n{"id": 4}\n'))
        source = JSONSource(
            {
                "path": str(jsonl_file),
                "format": "jsonl",
                "encoding": "utf-16-le",
                "on_validation_failure": "quarantine",
                "schema": {"mode": "observed"},
            }
        )

        rows = list(source.load(make_source_context(plugin_name="json")))

        assert [(row.is_quarantined, row.source_row_index) for row in rows] == [
            (False, 0),
            (True, 1),
            (False, 2),
            (False, 3),
        ]
        assert [rows[0].row, rows[2].row, rows[3].row] == [{"id": 1}, {"id": 3}, {"id": 4}]
        assert rows[1].row["__line_number__"] == 2
        assert rows[1].row["__raw_bytes_hex__"] == (bad_unit + _utf16le("\n")).hex()


def _json_source(path: Path, **options: object) -> object:
    from elspeth.plugins.sources.json_source import JSONSource

    return JSONSource(
        {
            "path": str(path),
            "on_validation_failure": "quarantine",
            "schema": {"mode": "observed"},
            **options,
        }
    )


class TestJSONSourceToleratesUTF8BOM:
    """Sweep finding #43: a leading UTF-8 BOM must not quarantine a valid JSON/JSONL file."""

    def test_bom_prefixed_json_array_yields_its_rows(self, tmp_path: Path) -> None:
        from elspeth.plugins.sources.json_source import JSONSource

        json_file = tmp_path / "data.json"
        json_file.write_bytes(b"\xef\xbb\xbf" + b'[{"id": 1}, {"id": 2}]')
        source = _json_source(json_file)
        assert isinstance(source, JSONSource)

        rows = list(source.load(make_source_context(plugin_name="json")))

        assert [(row.is_quarantined, row.row) for row in rows] == [(False, {"id": 1}), (False, {"id": 2})]

    def test_bom_prefixed_jsonl_keeps_line_one(self, tmp_path: Path) -> None:
        """Only the leading BOM is stripped: a U+FEFF later in the file is still data."""
        from elspeth.plugins.sources.json_source import JSONSource

        jsonl_file = tmp_path / "data.jsonl"
        jsonl_file.write_bytes(b"\xef\xbb\xbf" + b'{"id": 1}\n{"id": 2}\n\xef\xbb\xbf{"id": 3}\n')
        source = _json_source(jsonl_file, format="jsonl")
        assert isinstance(source, JSONSource)

        rows = list(source.load(make_source_context(plugin_name="json")))

        assert [(row.is_quarantined, row.source_row_index) for row in rows] == [(False, 0), (False, 1), (True, 2)]
        assert [rows[0].row, rows[1].row] == [{"id": 1}, {"id": 2}]


def _blob_csv_config_error(**options: object) -> str | None:
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpandConfig

    try:
        BlobCSVExpandConfig.from_dict(
            {"schema": {"mode": "observed"}, "source": "field", "text_field": "content", **options},
            plugin_name="blob_csv_expand",
        )
    except PluginConfigError as exc:
        return str(exc)
    return None


class TestBlobCSVExpandDelimiterValidatedAtConfigTime:
    """Sweep finding #51: a delimiter csv.reader rejects must be refused when config loads."""

    def test_delimiter_csv_reader_rejects_is_refused(self) -> None:
        # Ordinary delimiters are the control: only csv.reader's rejects are refused.
        refused = {
            delimiter: _blob_csv_config_error(delimiter=delimiter) is not None for delimiter in ['"', "\n", "\r", ",", "\t", ";", "|"]
        }

        assert refused == {'"': True, "\n": True, "\r": True, ",": False, "\t": False, ";": False, "|": False}

    def test_refusal_names_the_delimiter_option(self) -> None:
        error = _blob_csv_config_error(delimiter='"')

        assert error is not None
        assert "delimiter" in error


class _BoundedReadRecordingStore:
    """Payload store double modelling retrieve_bounded's contract and recording every read."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.full_reads: list[str] = []
        self.bounded_reads: list[tuple[str, int]] = []

    def retrieve(self, content_hash: str) -> bytes:
        self.full_reads.append(content_hash)
        return self.content

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        self.bounded_reads.append((content_hash, max_bytes))
        if len(self.content) > max_bytes:
            return None
        return self.content


class TestBlobCSVExpandBoundsTheBlobRead:
    """Sweep finding #52: max_blob_bytes must bound the read, not be checked after it."""

    def _run(self, body: bytes, *, max_blob_bytes: int) -> tuple[TransformResult, _BoundedReadRecordingStore, str]:
        import hashlib

        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
        from elspeth.testing import make_pipeline_row
        from tests.fixtures.factories import make_context

        blob_ref = hashlib.sha256(body).hexdigest()
        transform = BlobCSVExpand(
            {
                "schema": {"mode": "observed", "guaranteed_fields": ["blob_ref"]},
                "blob_ref_field": "blob_ref",
                "max_blob_bytes": max_blob_bytes,
            }
        )
        store = _BoundedReadRecordingStore(body)
        transform._payload_store = store
        result = transform.process(make_pipeline_row({"blob_ref": blob_ref}), make_context())
        return result, store, blob_ref

    def test_oversized_blob_is_refused_without_a_full_read(self) -> None:
        body = b"id,name\n" + b"1,alice\n" * 100
        result, store, blob_ref = self._run(body, max_blob_bytes=16)

        assert store.full_reads == []
        assert store.bounded_reads == [(blob_ref, 16)]
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "blob_too_large"
        assert result.reason["max_blob_bytes"] == 16
        # The bounded read stops past the cap, so no exact size is known to report.
        assert "body_size" not in result.reason

    def test_blob_within_limit_is_read_through_the_bounded_path(self) -> None:
        body = b"id,name\n1,alice\n"
        result, store, blob_ref = self._run(body, max_blob_bytes=1024)

        assert store.full_reads == []
        assert store.bounded_reads == [(blob_ref, 1024)]
        assert result.status == "success"
