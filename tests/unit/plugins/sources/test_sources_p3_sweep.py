"""Regression tests for P3 source/blob findings from the 20261111 plugins review sweep."""

from __future__ import annotations

from pathlib import Path

import pytest

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
