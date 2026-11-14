"""Unit tests for the pure Azure Document Intelligence analyzeResult parser."""

from __future__ import annotations

import pytest

from elspeth.plugins.transforms.azure.document_intelligence_result import (
    count_pages,
    extract_content,
    extract_facet_list,
    first_document_fields,
)
from elspeth.plugins.transforms.azure.errors import MalformedResponseError


def test_extract_content_present_and_absent() -> None:
    assert extract_content({"content": "# Hi"}) == "# Hi"
    assert extract_content({}) == ""


def test_extract_content_wrong_type_fails_closed() -> None:
    with pytest.raises(MalformedResponseError):
        extract_content({"content": 123})


def test_extract_facet_list_present_absent_and_malformed() -> None:
    assert extract_facet_list({"tables": [{"rowCount": 1}]}, "tables") == [{"rowCount": 1}]
    assert extract_facet_list({}, "tables") == []
    with pytest.raises(MalformedResponseError):
        extract_facet_list({"tables": {"not": "a list"}}, "tables")


def test_count_pages() -> None:
    assert count_pages({"pages": [{}, {}, {}]}) == 3
    assert count_pages({}) == 0
    with pytest.raises(MalformedResponseError):
        count_pages({"pages": "x"})


def test_first_document_fields_projects_typed_values() -> None:
    result = {
        "documents": [
            {
                "docType": "invoice",
                "fields": {
                    "VendorName": {"type": "string", "valueString": "Contoso", "content": "CONTOSO LTD.", "confidence": 0.9},
                    "InvoiceTotal": {"type": "currency", "valueCurrency": {"amount": 110.0, "currencyCode": "USD"}},
                    "InvoiceDate": {"type": "date", "valueDate": "2026-11-01"},
                    "Paid": {"type": "selectionMark", "valueSelectionMark": "selected"},
                },
            },
            {"docType": "invoice", "fields": {"VendorName": {"type": "string", "valueString": "Second"}}},
        ]
    }
    assert first_document_fields(result) == {
        "VendorName": "Contoso",
        "InvoiceTotal": {"amount": 110.0, "currencyCode": "USD"},
        "InvoiceDate": "2026-11-01",
        "Paid": "selected",
    }


def test_first_document_fields_absent_documents_or_fields_is_empty() -> None:
    assert first_document_fields({}) == {}
    assert first_document_fields({"documents": []}) == {}
    assert first_document_fields({"documents": [{"docType": "x"}]}) == {}


@pytest.mark.parametrize(
    "result",
    [
        {"documents": "not-a-list"},
        {"documents": ["not-an-object"]},
        {"documents": [{"fields": ["not-a-map"]}]},
        {"documents": [{"fields": {"A": {"valueString": "no type"}}}]},
    ],
)
def test_first_document_fields_malformed_fails_closed(result: dict[str, object]) -> None:
    with pytest.raises(MalformedResponseError):
        first_document_fields(result)
