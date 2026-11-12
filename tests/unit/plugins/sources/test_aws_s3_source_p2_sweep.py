"""Regression tests for aws_s3 source findings from the 20261111 plugins review sweep."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.plugins.sources.test_aws_s3_source import _source_for

_MID_DOCUMENT_FAILURES = [
    pytest.param(b'[{"id":1},{"id":2}] garbage', {}, id="trailing-garbage"),
    pytest.param(b'[{"id":1},{"id":2},{"id"', {}, id="truncated-tail"),
    pytest.param(
        b'{"r":[{"id":1},{"id":2}],"r":[{"id":3}]}',
        {"json_options": {"data_key": "r"}},
        id="duplicate-data-key",
    ),
]


class TestJSONMidDocumentFailure:
    """Sweep finding #9: a JSON document failing after rows were emitted."""

    @pytest.mark.parametrize(("payload", "options"), _MID_DOCUMENT_FAILURES)
    def test_file_error_uses_next_unused_source_row_index(self, payload: bytes, options: dict[str, Any]) -> None:
        source, _, ctx = _source_for(payload, format="json", **options)
        rows = list(source.load(ctx))

        assert [(row.is_quarantined, row.source_row_index) for row in rows] == [
            (False, 0),
            (False, 1),
            (True, 2),
        ]
        indexes = [row.source_row_index for row in rows]
        assert len(indexes) == len(set(indexes))

    @pytest.mark.parametrize(("payload", "options"), _MID_DOCUMENT_FAILURES)
    def test_file_error_states_rows_were_already_emitted(self, payload: bytes, options: dict[str, Any]) -> None:
        source, _, ctx = _source_for(payload, format="json", **options)
        rows = list(source.load(ctx))

        quarantine_error = rows[-1].quarantine_error
        assert quarantine_error is not None
        assert "2 rows were already emitted" in quarantine_error
        assert ctx.validation_errors[-1]["error"] == quarantine_error

    def test_discard_mode_records_that_rows_were_already_emitted(self) -> None:
        source, _, ctx = _source_for(b'[{"id":1},{"id":2}] garbage', format="json", on_validation_failure="discard")
        rows = list(source.load(ctx))

        assert [row.is_quarantined for row in rows] == [False, False]
        assert len(ctx.validation_errors) == 1
        assert "2 rows were already emitted" in ctx.validation_errors[0]["error"]

    def test_failure_before_first_row_keeps_index_zero_and_plain_message(self) -> None:
        source, _, ctx = _source_for(b"[{", format="json")
        rows = list(source.load(ctx))

        assert len(rows) == 1
        assert rows[0].is_quarantined
        assert rows[0].source_row_index == 0
        assert rows[0].quarantine_error == "JSON object could not be parsed within configured bounds"


class TestRecordsAfterUnreadableRecord:
    """Sweep finding #10: records after an oversized/unparseable record must not vanish silently."""

    def test_jsonl_resumes_after_oversized_line(self) -> None:
        payload = b'{"id":1}\n{"id":"' + b"x" * 50 + b'"}\n{"id":3}\n{"id":4}\n'
        source, _, ctx = _source_for(payload, format="jsonl", max_record_chars=20)
        rows = list(source.load(ctx))

        assert [(row.is_quarantined, row.source_row_index) for row in rows] == [
            (False, 0),
            (True, 1),
            (False, 2),
            (False, 3),
        ]
        assert [rows[0].row, rows[2].row, rows[3].row] == [{"id": 1}, {"id": 3}, {"id": 4}]

    def test_jsonl_resumes_when_oversized_line_newline_lands_on_the_bound(self) -> None:
        # The oversized line is exactly max_record_chars + 1 characters
        # including its newline, so the bounded read consumes the whole line.
        # Resync must not swallow the following line.
        oversized = b'{"id":"' + b"x" * 11 + b'"}\n'
        assert len(oversized) == 21
        payload = oversized + b'{"id":2}\n{"id":3}\n'
        source, _, ctx = _source_for(payload, format="jsonl", max_record_chars=20)
        rows = list(source.load(ctx))

        assert [(row.is_quarantined, row.source_row_index) for row in rows] == [
            (True, 0),
            (False, 1),
            (False, 2),
        ]
        assert [rows[1].row, rows[2].row] == [{"id": 2}, {"id": 3}]

    def test_jsonl_resumes_after_oversized_final_line_without_newline(self) -> None:
        payload = b'{"id":1}\n{"id":"' + b"x" * 50 + b'"}'
        source, _, ctx = _source_for(payload, format="jsonl", max_record_chars=20)
        rows = list(source.load(ctx))

        assert [(row.is_quarantined, row.source_row_index) for row in rows] == [(False, 0), (True, 1)]

    def test_csv_oversized_record_states_processing_stopped(self) -> None:
        payload = b"id,name\n1,Ada\n2," + b"x" * 50 + b"\n3,Grace\n4,Linus\n"
        source, _, ctx = _source_for(payload, format="csv", max_record_chars=20)
        rows = list(source.load(ctx))

        assert [row.is_quarantined for row in rows] == [False, True]
        message = rows[1].quarantine_error
        assert message is not None
        assert "stopped at row 2" in message
        assert "later records were not read" in message
        assert ctx.validation_errors[-1]["error"] == message

    def test_csv_parse_error_states_processing_stopped(self) -> None:
        payload = b'id,name\n1,Ada\n2,"Gr"ace\n3,Grace\n'
        source, _, ctx = _source_for(payload, format="csv")
        rows = list(source.load(ctx))

        assert [row.is_quarantined for row in rows] == [False, True]
        message = rows[1].quarantine_error
        assert message is not None
        assert "stopped at row 2" in message
        assert "later records were not read" in message
