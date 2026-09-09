"""Property checks for pagination and mapping-order invariance."""

from __future__ import annotations

from collections.abc import Mapping
from itertools import pairwise
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.plugins.transforms.aws.textract_result import normalize_textract_result


def _geometry() -> dict[str, Any]:
    return {
        "BoundingBox": {"Width": 0.8, "Height": 0.1, "Left": 0.1, "Top": 0.2},
        "Polygon": [{"X": 0.1, "Y": 0.2}, {"X": 0.9, "Y": 0.2}],
    }


def _blocks() -> list[dict[str, Any]]:
    return [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1, "Geometry": _geometry()},
        {
            "BlockType": "LINE",
            "Id": "line-1",
            "Page": 1,
            "Text": "First page",
            "Confidence": 99.0,
            "Geometry": _geometry(),
        },
        {"BlockType": "PAGE", "Id": "page-2", "Page": 2, "Geometry": _geometry()},
        {
            "BlockType": "LINE",
            "Id": "line-2",
            "Page": 2,
            "Text": "Second page",
            "Confidence": 98.0,
            "Geometry": _geometry(),
        },
    ]


def _reverse_mapping_order(value: Any, *, reverse: bool) -> Any:
    if isinstance(value, Mapping):
        items = list(value.items())
        if reverse:
            items.reverse()
        return {key: _reverse_mapping_order(item, reverse=not reverse) for key, item in items}
    if isinstance(value, list):
        return [_reverse_mapping_order(item, reverse=reverse) for item in value]
    return value


def _responses(cuts: list[int], reverse_flags: list[bool]) -> list[dict[str, Any]]:
    blocks = _blocks()
    boundaries = [0, *sorted(cuts), len(blocks)]
    responses: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(pairwise(boundaries)):
        response = {
            "JobStatus": "SUCCEEDED",
            "DocumentMetadata": {"Pages": 2},
            "AnalyzeDocumentModelVersion": "1.0",
            "Warnings": [],
            "Blocks": blocks[start:end],
        }
        responses.append(_reverse_mapping_order(response, reverse=reverse_flags[index % len(reverse_flags)]))
    return responses


def _normalize(responses: list[dict[str, Any]]):
    return normalize_textract_result(
        job_id="job-property",
        result_pages=responses,
        feature_types=("FORMS",),
        s3_version=None,
        max_blocks=100,
        max_result_bytes=100_000,
    )


@settings(max_examples=50)
@given(
    cuts=st.lists(st.integers(min_value=1, max_value=3), unique=True),
    reverse_flags=st.lists(st.booleans(), min_size=1, max_size=4),
)
def test_pagination_cuts_and_mapping_order_do_not_change_semantics(cuts: list[int], reverse_flags: list[bool]) -> None:
    expected = _normalize(_responses([], [False]))
    actual = _normalize(_responses(cuts, reverse_flags))

    assert actual.text == expected.text
    assert actual.pages == expected.pages
    assert actual.tables == expected.tables
    assert actual.forms == expected.forms
    assert actual.queries == expected.queries
    assert actual.signatures == expected.signatures
    assert actual.layout == expected.layout
    assert actual.metadata == expected.metadata
    assert actual.native_result == expected.native_result
