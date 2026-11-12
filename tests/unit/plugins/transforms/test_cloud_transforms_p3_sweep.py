"""Regression tests for the P3 cloud-transform sweep findings (20261111-plugins-review).

Each section names the sweep finding it pins. The tests drive the production
code paths the finding describes and assert on the observable outcome.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from elspeth.contracts import TransformResult
from elspeth.plugins.transforms.aws.textract_inline_analysis import AWSTextractInlineAnalysis
from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    normalize_analyze_document_result,
)
from elspeth.testing import make_pipeline_row

# ---------------------------------------------------------------------------
# Finding #35: FORMS projection must carry checkbox selection state.
# ---------------------------------------------------------------------------


def _geometry() -> dict[str, Any]:
    return {
        "BoundingBox": {"Width": 0.5, "Height": 0.1, "Left": 0.1, "Top": 0.2},
        "Polygon": [
            {"X": 0.1, "Y": 0.2},
            {"X": 0.6, "Y": 0.2},
            {"X": 0.6, "Y": 0.3},
            {"X": 0.1, "Y": 0.3},
        ],
    }


def _checkbox_form_response(*statuses: str) -> dict[str, Any]:
    """One page holding one KEY/VALUE pair per status; each VALUE's CHILD is a checkbox."""
    blocks: list[dict[str, Any]] = [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1, "Geometry": _geometry()},
    ]
    for index, status in enumerate(statuses):
        blocks.extend(
            [
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": f"key-{index}",
                    "Page": 1,
                    "EntityTypes": ["KEY"],
                    "Confidence": 95.0,
                    "Geometry": _geometry(),
                    "Relationships": [
                        {"Type": "CHILD", "Ids": [f"key-word-{index}"]},
                        {"Type": "VALUE", "Ids": [f"value-{index}"]},
                    ],
                },
                {
                    "BlockType": "WORD",
                    "Id": f"key-word-{index}",
                    "Page": 1,
                    "Text": f"Option {index}",
                    "Confidence": 95.0,
                    "Geometry": _geometry(),
                },
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": f"value-{index}",
                    "Page": 1,
                    "EntityTypes": ["VALUE"],
                    "Confidence": 94.0,
                    "Geometry": _geometry(),
                    "Relationships": [{"Type": "CHILD", "Ids": [f"selection-{index}"]}],
                },
                {
                    "BlockType": "SELECTION_ELEMENT",
                    "Id": f"selection-{index}",
                    "Page": 1,
                    "SelectionStatus": status,
                    "Confidence": 93.0,
                    "Geometry": _geometry(),
                },
            ]
        )
    return {"DocumentMetadata": {"Pages": 1}, "AnalyzeDocumentModelVersion": "1.0", "Blocks": blocks}


def _normalize_forms(response: dict[str, Any]) -> Any:
    return normalize_analyze_document_result(
        response=response,
        feature_types=("FORMS",),
        max_blocks=1_000,
        max_result_bytes=1_000_000,
    )


def test_f35_forms_projection_distinguishes_checked_from_unchecked_checkbox() -> None:
    result = _normalize_forms(_checkbox_form_response("SELECTED", "NOT_SELECTED"))

    forms = list(result.forms)
    assert len(forms) == 2
    assert all("value_selection_status" in form for form in forms), [sorted(form) for form in forms]
    statuses = {form["key"]: form["value_selection_status"] for form in forms}
    assert statuses == {"Option 0": "SELECTED", "Option 1": "NOT_SELECTED"}


def test_f35_forms_projection_rejects_unknown_checkbox_status() -> None:
    with pytest.raises(MalformedTextractResponse, match="SELECTED or NOT_SELECTED"):
        _normalize_forms(_checkbox_form_response("MAYBE"))


# ---------------------------------------------------------------------------
# Finding #30: inline Textract must apply max_document_bytes while reading.
# ---------------------------------------------------------------------------

_F30_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 4096
_F30_SHA = hashlib.sha256(_F30_PNG).hexdigest()


class _BoundedReadStore:
    """PayloadStore double that records full and bounded reads separately."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.full_reads: list[str] = []
        self.bounded_reads: list[tuple[str, int]] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("the transform must never store payloads")

    def retrieve(self, content_hash: str) -> bytes:
        self.full_reads.append(content_hash)
        return self.content

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        self.bounded_reads.append((content_hash, max_bytes))
        return None if len(self.content) > max_bytes else self.content

    def exists(self, content_hash: str) -> bool:
        return True

    def delete(self, content_hash: str) -> bool:
        raise AssertionError("the transform must never delete payloads")


def test_f30_oversized_inline_document_is_rejected_by_a_bounded_read() -> None:
    transform = AWSTextractInlineAnalysis(
        {
            "region": "ap-southeast-2",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
            "max_document_bytes": 64,
        }
    )
    store = _BoundedReadStore(_F30_PNG)
    transform._payload_store = store

    result = transform._read_document_bytes(make_pipeline_row({"blob_ref": _F30_SHA}))

    assert isinstance(result, TransformResult)
    assert result.status == "error"
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 64
    assert store.full_reads == [], "the whole blob was read before the size cap applied"
    assert store.bounded_reads == [(_F30_SHA, 64)]
    assert result.reason["actual"] == ">64"
