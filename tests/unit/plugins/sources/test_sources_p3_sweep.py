"""Regression tests for P3 source/blob findings from the 20261111 plugins review sweep."""

from __future__ import annotations

import pytest

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
