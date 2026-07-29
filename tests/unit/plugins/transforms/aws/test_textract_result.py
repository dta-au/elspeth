"""Tests for strict Amazon Textract result aggregation and normalization."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_freeze
from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    normalize_textract_result,
)


def _geometry(*, left: float = 0.1) -> dict[str, Any]:
    return {
        "BoundingBox": {"Width": 0.8, "Height": 0.1, "Left": left, "Top": 0.2},
        "Polygon": [
            {"X": left, "Y": 0.2},
            {"X": 0.9, "Y": 0.2},
            {"X": 0.9, "Y": 0.3},
            {"X": left, "Y": 0.3},
        ],
    }


def _first_page() -> dict[str, Any]:
    return {
        "JobStatus": "SUCCEEDED",
        "DocumentMetadata": {"Pages": 2},
        "AnalyzeDocumentModelVersion": "1.0",
        "Warnings": [{"ErrorCode": "PAGE_CHARACTERS_EXCEEDED", "Pages": [1]}],
        "Blocks": [
            {
                "BlockType": "PAGE",
                "Id": "page-1",
                "Page": 1,
                "Geometry": _geometry(left=0.0),
                "Relationships": [{"Type": "CHILD", "Ids": ["line-1", "line-2"]}],
            },
            {
                "BlockType": "LINE",
                "Id": "line-1",
                "Page": 1,
                "Text": "Invoice",
                "Confidence": 99.1,
                "Geometry": _geometry(),
                "Relationships": [{"Type": "CHILD", "Ids": ["word-1"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "word-1",
                "Page": 1,
                "Text": "Invoice",
                "Confidence": 99.0,
                "Geometry": _geometry(),
                "UnknownMember": {"preserved": True},
            },
            {
                "BlockType": "LINE",
                "Id": "line-2",
                "Page": 1,
                "Text": "Total $42",
                "Confidence": 98.2,
                "Geometry": _geometry(left=0.2),
                "Relationships": [{"Type": "CHILD", "Ids": ["word-2", "word-3"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "word-2",
                "Page": 1,
                "Text": "Total",
                "Confidence": 98.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "WORD",
                "Id": "word-3",
                "Page": 1,
                "Text": "$42",
                "Confidence": 98.0,
                "Geometry": _geometry(),
            },
        ],
        "NextToken": "page-two",
        "ResponseMetadata": {"RequestId": "request-1"},
        "UnknownTopLevel": "omit-me",
    }


def _second_page() -> dict[str, Any]:
    return {
        "JobStatus": "SUCCEEDED",
        "DocumentMetadata": {"Pages": 2},
        "AnalyzeDocumentModelVersion": "1.0",
        "Blocks": [
            {
                "BlockType": "PAGE",
                "Id": "page-2",
                "Page": 2,
                "Geometry": _geometry(left=0.0),
                "Relationships": [{"Type": "CHILD", "Ids": ["line-3"]}],
            },
            {
                "BlockType": "LINE",
                "Id": "line-3",
                "Page": 2,
                "Text": "Thank you",
                "Confidence": 97.5,
                "Geometry": _geometry(),
                "Relationships": [{"Type": "CHILD", "Ids": ["word-4", "word-5"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "word-4",
                "Page": 2,
                "Text": "Thank",
                "Confidence": 97.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "WORD",
                "Id": "word-5",
                "Page": 2,
                "Text": "you",
                "Confidence": 97.0,
                "Geometry": _geometry(),
            },
        ],
        "ResponseMetadata": {"RequestId": "request-2"},
    }


def _normalize(*pages: dict[str, Any], max_blocks: int = 200_000, max_result_bytes: int = 50_000_000):
    return normalize_textract_result(
        job_id="job-1",
        result_pages=list(pages) or [_first_page(), _second_page()],
        feature_types=("FORMS", "TABLES"),
        s3_version="version-1",
        max_blocks=max_blocks,
        max_result_bytes=max_result_bytes,
    )


def test_normalize_text_pages_metadata_and_native_result() -> None:
    result = _normalize()

    assert result.text == "Invoice\nTotal $42\n\f\nThank you"
    assert result.page_count == 2
    assert result.block_count == len(result.native_result["Blocks"])
    assert result.pages == (
        {
            "page": 1,
            "text": "Invoice\nTotal $42",
            "geometry": _geometry(left=0.0),
            "lines": [
                {"id": "line-1", "text": "Invoice", "confidence": 99.1, "geometry": _geometry()},
                {"id": "line-2", "text": "Total $42", "confidence": 98.2, "geometry": _geometry(left=0.2)},
            ],
        },
        {
            "page": 2,
            "text": "Thank you",
            "geometry": _geometry(left=0.0),
            "lines": [
                {"id": "line-3", "text": "Thank you", "confidence": 97.5, "geometry": _geometry()},
            ],
        },
    )
    assert result.metadata == {
        "job_id": "job-1",
        "job_status": "SUCCEEDED",
        "page_count": 2,
        "block_count": 10,
        "model_version": "1.0",
        "warnings": [{"error_code": "PAGE_CHARACTERS_EXCEEDED", "pages": [1]}],
        "feature_types": ["FORMS", "TABLES"],
        "s3_version": "version-1",
    }
    assert result.native_result["Warnings"] == [{"ErrorCode": "PAGE_CHARACTERS_EXCEEDED", "Pages": [1]}]
    assert result.native_result["Blocks"][2]["UnknownMember"] == {"preserved": True}
    assert "ResponseMetadata" not in result.native_result
    assert "NextToken" not in result.native_result
    assert "UnknownTopLevel" not in result.native_result


def test_normalize_accepts_deeply_frozen_audited_client_pages() -> None:
    frozen_pages = deep_freeze([_first_page(), _second_page()])

    result = normalize_textract_result(
        job_id="job-1",
        result_pages=frozen_pages,
        feature_types=("FORMS", "TABLES"),
        s3_version="version-1",
        max_blocks=200_000,
        max_result_bytes=50_000_000,
    )

    assert result.text == "Invoice\nTotal $42\n\f\nThank you"
    assert result.native_result["Blocks"][0]["Relationships"] == [{"Type": "CHILD", "Ids": ["line-1", "line-2"]}]


def test_duplicate_block_id_fails_closed() -> None:
    first = _first_page()
    first["Blocks"].append(deepcopy(first["Blocks"][0]))

    with pytest.raises(MalformedTextractResponse, match="duplicate block id"):
        _normalize(first, _second_page())


def test_page_count_disagreement_fails_closed() -> None:
    second = _second_page()
    second["DocumentMetadata"]["Pages"] = 3

    with pytest.raises(MalformedTextractResponse, match="page count"):
        _normalize(_first_page(), second)


def test_page_block_numbering_disagreement_fails_closed() -> None:
    second = _second_page()
    second["Blocks"][0]["Page"] = 3

    with pytest.raises(MalformedTextractResponse, match="PAGE block"):
        _normalize(_first_page(), second)


@pytest.mark.parametrize("missing", ["JobStatus", "DocumentMetadata", "AnalyzeDocumentModelVersion", "Blocks"])
def test_missing_required_result_key_fails_closed(missing: str) -> None:
    first = _first_page()
    del first[missing]

    with pytest.raises(MalformedTextractResponse, match=missing):
        _normalize(first, _second_page())


def test_non_list_blocks_fail_closed() -> None:
    first = _first_page()
    first["Blocks"] = {"not": "a list"}

    with pytest.raises(MalformedTextractResponse, match="Blocks"):
        _normalize(first, _second_page())


def test_dangling_relationship_fails_closed() -> None:
    first = _first_page()
    first["Blocks"][0]["Relationships"][0]["Ids"].append("missing-block")

    with pytest.raises(MalformedTextractResponse, match="dangling relationship"):
        _normalize(first, _second_page())


@pytest.mark.parametrize(
    ("member", "value", "message"),
    [
        ("Page", 0, "Page"),
        ("Confidence", float("nan"), "Confidence"),
        ("Confidence", 101.0, "Confidence"),
        ("Geometry", {"BoundingBox": {"Width": 1.2, "Height": 0.1, "Left": 0.0, "Top": 0.0}}, "Geometry"),
    ],
)
def test_invalid_known_block_member_fails_closed(member: str, value: object, message: str) -> None:
    first = _first_page()
    first["Blocks"][1][member] = value

    with pytest.raises(MalformedTextractResponse, match=message):
        _normalize(first, _second_page())


def test_too_many_blocks_fail_closed() -> None:
    with pytest.raises(MalformedTextractResponse, match="max_blocks"):
        _normalize(max_blocks=9)


def test_oversized_native_result_fails_closed() -> None:
    with pytest.raises(MalformedTextractResponse, match="max_result_bytes"):
        _normalize(max_result_bytes=100)


def test_result_pages_must_not_be_empty() -> None:
    with pytest.raises(MalformedTextractResponse, match="result_pages"):
        normalize_textract_result(
            job_id="job-1",
            result_pages=[],
            feature_types=("FORMS",),
            s3_version=None,
            max_blocks=100,
            max_result_bytes=100_000,
        )


def _facet_first_page() -> dict[str, Any]:
    first = _first_page()
    first["Blocks"].extend(
        [
            {
                "BlockType": "TABLE",
                "Id": "table-1",
                "Page": 1,
                "Confidence": 98.5,
                "Geometry": _geometry(),
                "EntityTypes": ["STRUCTURED_TABLE"],
                "Relationships": [{"Type": "CHILD", "Ids": ["cell-1", "cell-2"]}],
            },
            {
                "BlockType": "CELL",
                "Id": "cell-1",
                "Page": 1,
                "RowIndex": 1,
                "ColumnIndex": 1,
                "RowSpan": 1,
                "ColumnSpan": 1,
                "Confidence": 98.0,
                "Geometry": _geometry(),
                "Relationships": [{"Type": "CHILD", "Ids": ["cell-word-1"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "cell-word-1",
                "Page": 1,
                "Text": "Item",
                "Confidence": 98.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "CELL",
                "Id": "cell-2",
                "Page": 1,
                "RowIndex": 1,
                "ColumnIndex": 3,
                "RowSpan": 1,
                "ColumnSpan": 1,
                "Confidence": 97.0,
                "Geometry": _geometry(left=0.3),
                "Relationships": [{"Type": "CHILD", "Ids": ["cell-word-2", "selection-1"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "cell-word-2",
                "Page": 1,
                "Text": "Approved",
                "Confidence": 97.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "SELECTION_ELEMENT",
                "Id": "selection-1",
                "Page": 1,
                "SelectionStatus": "SELECTED",
                "Confidence": 96.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "KEY_VALUE_SET",
                "Id": "key-1",
                "Page": 1,
                "EntityTypes": ["KEY"],
                "Confidence": 97.0,
                "Geometry": _geometry(),
                "Relationships": [
                    {"Type": "CHILD", "Ids": ["key-word-1"]},
                    {"Type": "VALUE", "Ids": ["value-1"]},
                ],
            },
            {
                "BlockType": "WORD",
                "Id": "key-word-1",
                "Page": 1,
                "Text": "Invoice number",
                "Confidence": 97.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "KEY_VALUE_SET",
                "Id": "value-1",
                "Page": 1,
                "EntityTypes": ["VALUE"],
                "Confidence": 96.0,
                "Geometry": _geometry(left=0.3),
                "Relationships": [{"Type": "CHILD", "Ids": ["value-word-1"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "value-word-1",
                "Page": 1,
                "Text": "INV-123",
                "Confidence": 96.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "KEY_VALUE_SET",
                "Id": "key-2",
                "Page": 1,
                "EntityTypes": ["KEY"],
                "Confidence": 95.0,
                "Geometry": _geometry(),
                "Relationships": [{"Type": "CHILD", "Ids": ["key-word-2"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "key-word-2",
                "Page": 1,
                "Text": "Optional note",
                "Confidence": 95.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "QUERY",
                "Id": "query-1",
                "Page": 1,
                "Query": {"Text": "What is the invoice total?", "Alias": "invoice_total"},
                "Relationships": [{"Type": "ANSWER", "Ids": ["answer-1"]}],
            },
            {
                "BlockType": "QUERY_RESULT",
                "Id": "answer-1",
                "Page": 1,
                "Text": "$42.00",
                "Confidence": 95.0,
                "Geometry": _geometry(),
            },
            {
                "BlockType": "QUERY",
                "Id": "query-2",
                "Page": 1,
                "Query": {"Text": "What is the purchase order?"},
            },
            {
                "BlockType": "SIGNATURE",
                "Id": "signature-1",
                "Page": 1,
                "Confidence": 94.0,
                "Geometry": _geometry(left=0.4),
            },
            {
                "BlockType": "LAYOUT_TITLE",
                "Id": "layout-1",
                "Page": 1,
                "Confidence": 93.0,
                "Geometry": _geometry(),
                "Relationships": [{"Type": "CHILD", "Ids": ["layout-word-1"]}],
            },
            {
                "BlockType": "WORD",
                "Id": "layout-word-1",
                "Page": 1,
                "Text": "Invoice title",
                "Confidence": 93.0,
                "Geometry": _geometry(),
            },
        ]
    )
    return first


def test_normalize_tables_forms_queries_signatures_and_layout() -> None:
    result = _normalize(_facet_first_page(), _second_page())

    assert result.tables == (
        {
            "id": "table-1",
            "page": 1,
            "confidence": 98.5,
            "geometry": _geometry(),
            "entity_types": ["STRUCTURED_TABLE"],
            "rows": [
                [
                    {
                        "id": "cell-1",
                        "row": 1,
                        "column": 1,
                        "row_span": 1,
                        "column_span": 1,
                        "text": "Item",
                        "confidence": 98.0,
                        "geometry": _geometry(),
                        "selection_status": None,
                    },
                    {
                        "id": "cell-2",
                        "row": 1,
                        "column": 3,
                        "row_span": 1,
                        "column_span": 1,
                        "text": "Approved",
                        "confidence": 97.0,
                        "geometry": _geometry(left=0.3),
                        "selection_status": "SELECTED",
                    },
                ]
            ],
        },
    )
    assert result.forms == (
        {
            "page": 1,
            "key": "Invoice number",
            "value": "INV-123",
            "key_block_id": "key-1",
            "value_block_id": "value-1",
            "key_confidence": 97.0,
            "value_confidence": 96.0,
            "key_geometry": _geometry(),
            "value_geometry": _geometry(left=0.3),
        },
        {
            "page": 1,
            "key": "Optional note",
            "value": None,
            "key_block_id": "key-2",
            "value_block_id": None,
            "key_confidence": 95.0,
            "value_confidence": None,
            "key_geometry": _geometry(),
            "value_geometry": None,
        },
    )
    assert result.queries == (
        {
            "page": 1,
            "query": "What is the invoice total?",
            "alias": "invoice_total",
            "answer": "$42.00",
            "confidence": 95.0,
            "query_block_id": "query-1",
            "answer_block_id": "answer-1",
        },
        {
            "page": 1,
            "query": "What is the purchase order?",
            "alias": None,
            "answer": None,
            "confidence": None,
            "query_block_id": "query-2",
            "answer_block_id": None,
        },
    )
    assert result.signatures == ({"id": "signature-1", "page": 1, "confidence": 94.0, "geometry": _geometry(left=0.4)},)
    assert result.layout == (
        {
            "id": "layout-1",
            "block_type": "LAYOUT_TITLE",
            "page": 1,
            "text": "Invoice title",
            "confidence": 93.0,
            "geometry": _geometry(),
        },
    )


def test_duplicate_table_cell_coordinates_fail_closed() -> None:
    first = _facet_first_page()
    next(block for block in first["Blocks"] if block["Id"] == "cell-2")["ColumnIndex"] = 1

    with pytest.raises(MalformedTextractResponse, match="duplicate cell coordinate"):
        _normalize(first, _second_page())


@pytest.mark.parametrize("member", ["RowIndex", "ColumnIndex", "RowSpan", "ColumnSpan"])
def test_non_positive_cell_coordinates_and_spans_fail_closed(member: str) -> None:
    first = _facet_first_page()
    next(block for block in first["Blocks"] if block["Id"] == "cell-1")[member] = 0

    with pytest.raises(MalformedTextractResponse, match=member):
        _normalize(first, _second_page())


def test_query_answer_relationship_must_target_query_result() -> None:
    first = _facet_first_page()
    query = next(block for block in first["Blocks"] if block["Id"] == "query-1")
    query["Relationships"][0]["Ids"] = ["word-1"]

    with pytest.raises(MalformedTextractResponse, match="QUERY_RESULT"):
        _normalize(first, _second_page())


def test_form_value_relationship_must_target_value_block() -> None:
    first = _facet_first_page()
    key = next(block for block in first["Blocks"] if block["Id"] == "key-1")
    key["Relationships"][1]["Ids"] = ["word-1"]

    with pytest.raises(MalformedTextractResponse, match="VALUE"):
        _normalize(first, _second_page())


def test_cyclic_child_relationship_fails_closed() -> None:
    first = _facet_first_page()
    layout_word = next(block for block in first["Blocks"] if block["Id"] == "layout-word-1")
    layout_word["Relationships"] = [{"Type": "CHILD", "Ids": ["layout-1"]}]

    with pytest.raises(MalformedTextractResponse, match="cyclic CHILD"):
        _normalize(first, _second_page())


def test_cycle_in_non_facet_page_graph_fails_closed() -> None:
    first = _first_page()
    word = next(block for block in first["Blocks"] if block["Id"] == "word-1")
    word["Relationships"] = [{"Type": "CHILD", "Ids": ["page-1"]}]

    with pytest.raises(MalformedTextractResponse, match="cyclic CHILD"):
        _normalize(first, _second_page())
