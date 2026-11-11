"""Pure Tier-3 parsing helpers for Azure Document Intelligence analyzeResult.

No I/O. Every function fail-closes (MalformedResponseError) on a structurally
invalid result; absent optional facets return an empty container, never
fabricated. These functions are the only place the external analyzeResult shape
is interpreted, so they are unit-tested in isolation. Helpers shared with the
Content Understanding transform live in ``analyze_result``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.plugins.transforms.azure.analyze_result import facet_list, flatten_fields
from elspeth.plugins.transforms.azure.errors import MalformedResponseError


@trust_boundary(
    tier=3,
    source="Azure Document Intelligence analyzeResult (parsed JSON)",
    source_param="analyze_result",
    suppresses=("R1", "R5"),
    invariant="raises MalformedResponseError when analyzeResult.content is present but not a str; an absent content is '' and never fabricated",
    test_ref="tests/unit/plugins/transforms/azure/test_document_intelligence_result.py::test_extract_content_wrong_type_fails_closed",
    test_fingerprint="93ae250a917e3cd19535002cfed3d61894a81a841326bcea486235fbd34be3a1",
)
def extract_content(analyze_result: Mapping[str, Any]) -> str:
    """Return analyzeResult.content, or '' when absent. Fail closed on wrong type."""
    content = analyze_result.get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise MalformedResponseError(f"analyzeResult.content must be str, got {type(content).__name__}")
    return content


def extract_facet_list(analyze_result: Mapping[str, Any], azure_key: str) -> list[Any]:
    """Return analyzeResult[azure_key] (a list), or [] when absent. Fail closed on wrong type."""
    return facet_list(analyze_result, azure_key)


def count_pages(analyze_result: Mapping[str, Any]) -> int:
    """Return the number of analyzed pages, or 0 when absent. Fail closed on wrong type."""
    return len(extract_facet_list(analyze_result, "pages"))


@trust_boundary(
    tier=3,
    source="Azure Document Intelligence analyzeResult (parsed JSON)",
    source_param="analyze_result",
    suppresses=("R1", "R5"),
    invariant="raises MalformedResponseError unless documents is absent or a list whose first entry is an object with well-formed typed fields",
    test_ref="tests/unit/plugins/transforms/azure/test_document_intelligence_result.py::test_first_document_fields_malformed_fails_closed",
    test_fingerprint="d1b5a81edb49773143829a332ad616f4b64ea6c41b7a6b1d5ba8be2001d9a7d0",
)
def first_document_fields(analyze_result: Mapping[str, Any]) -> dict[str, Any]:
    """Flattened ``documents[0].fields`` ({} when the model returned no document).

    Typed models (prebuilt-invoice, custom extraction, ``queryFields``) report
    one document per analyzed file; the plain values are what a pipeline routes
    on. A multi-document result keeps the rest in the ``documents`` facet.
    """
    documents = extract_facet_list(analyze_result, "documents")
    if not documents:
        return {}
    first = documents[0]
    if not isinstance(first, Mapping):
        raise MalformedResponseError(f"analyzeResult.documents entries must be objects, got {type(first).__name__}")
    return flatten_fields(first.get("fields"))
