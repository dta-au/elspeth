"""Regression tests for the P3 blob / rasterize sweep findings (20261111-plugins-review).

One section per finding; each test fails at an assertion on the pre-fix tree.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
from elspeth.testing import make_pipeline_row
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
