"""Pure Tier-3 parsing for an Azure AI Content Understanding ``result``.

No I/O. A Content Understanding result carries one or more ``contents`` —
documents (``kind: document``, page-ranged) or audio/video
(``kind: audioVisual``, time-ranged) — each with its own markdown, fields and
structural facets. ``parse_result_contents`` is the one boundary that reads the
external ``result``: it validates every member the transform interprets and
constructs ``ResultContents``, an owned value. Every structural violation
raises ``MalformedResponseError``; an absent optional facet is an empty
container, never fabricated. Helpers shared with Document Intelligence live in
``analyze_result``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.plugins.transforms.azure.analyze_result import facet_list, flatten_fields
from elspeth.plugins.transforms.azure.errors import MalformedResponseError

_MAX_PAGE_NUMBER = 1_000_000


@dataclass(frozen=True, slots=True)
class ResultContents:
    """The validated ``result.contents``.

    ``items`` keeps the provider's own content objects (facets and the
    ``contents`` output pass them through as Azure sent them), deep-frozen at
    construction and thawed back to plain JSON containers by each accessor;
    ``markdown_parts`` and ``page_count`` were validated at the parse boundary.
    The freeze is one walk of the contents, beside the HTTP round trip and JSON
    parse that produced them.
    """

    items: tuple[Mapping[str, Any], ...]
    markdown_parts: tuple[str, ...]
    page_count: int

    def __post_init__(self) -> None:
        freeze_fields(self, "items", "markdown_parts")

    def __len__(self) -> int:
        return len(self.items)

    def markdown(self) -> str:
        """Every content's markdown joined by a blank line ('' when none has any)."""
        return "\n\n".join(self.markdown_parts)

    def first_fields(self) -> dict[str, Any]:
        """Flattened ``fields`` of the first content ({} when there is no content).

        A single analyzed file produces one content, whose ``fields`` hold the
        analyzer's extraction schema. A result split into several contents keeps
        the rest reachable through the ``contents`` facet or ``result_field``.
        """
        if not self.items:
            return {}
        return flatten_fields(deep_thaw(self.items[0])["fields"] if "fields" in self.items[0] else None)

    def facet(self, azure_key: str) -> list[Any]:
        """``contents[*][azure_key]`` lists concatenated in content order ([] when absent)."""
        combined: list[Any] = []
        for content in self.items:
            combined.extend(facet_list(deep_thaw(content), azure_key))
        return combined

    def as_list(self) -> list[Mapping[str, Any]]:
        """The content objects themselves (thawed to plain JSON containers), for the ``contents`` facet."""
        return [deep_thaw(content) for content in self.items]


@trust_boundary(
    tier=3,
    source="Azure Content Understanding analysis result (parsed JSON)",
    source_param="result",
    suppresses=("R1", "R5"),
    invariant=(
        "raises MalformedResponseError unless result.contents is a list of objects whose markdown, when present, "
        "is a str and whose document contents carry integer 1 <= startPageNumber <= endPageNumber; never coerces"
    ),
    test_ref="tests/unit/plugins/transforms/azure/test_content_understanding.py::test_parse_result_contents_rejects_malformed_results",
    test_fingerprint="2824d62aeb7783c0629e3607f9ce795aebeadcff6a1f4b00f99277711de2057f",
)
def parse_result_contents(result: Mapping[str, Any]) -> ResultContents:
    """Validate ``result.contents`` and construct the owned ``ResultContents``."""
    if "contents" not in result:
        raise MalformedResponseError("result.contents is required")
    contents = facet_list(result, "contents")
    items: list[Mapping[str, Any]] = []
    markdown_parts: list[str] = []
    page_count = 0
    for content in contents:
        if not isinstance(content, Mapping):
            raise MalformedResponseError(f"result.contents entries must be objects, got {type(content).__name__}")
        items.append(content)
        markdown = content.get("markdown")
        if markdown is not None:
            if not isinstance(markdown, str):
                raise MalformedResponseError(f"contents[].markdown must be str, got {type(markdown).__name__}")
            markdown_parts.append(markdown)
        if content.get("kind") == "document":
            start = content.get("startPageNumber")
            end = content.get("endPageNumber")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= _MAX_PAGE_NUMBER:
                raise MalformedResponseError("document contents require integer 1 <= startPageNumber <= endPageNumber")
            page_count += end - start + 1
    return ResultContents(items=tuple(items), markdown_parts=tuple(markdown_parts), page_count=page_count)
