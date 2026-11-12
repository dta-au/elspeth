"""Regression tests for the P3 cloud-transform sweep findings (20261111-plugins-review).

Each section names the sweep finding it pins. The tests drive the production
code paths the finding describes and assert on the observable outcome.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    normalize_analyze_document_result,
)

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
