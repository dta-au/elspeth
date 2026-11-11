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
