"""Context formatting for RAG retrieval output.

Joins multiple retrieved chunks into a single text field with
configurable formatting and optional length capping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk


@dataclass(frozen=True)
class FormattedContext:
    """Result of context formatting."""

    text: str
    truncated: bool


def format_context(
    chunks: list[RetrievalChunk],
    *,
    format_mode: Literal["numbered", "separated", "raw"],
    separator: str = "\n---\n",
    max_length: int | None = None,
) -> FormattedContext:
    """Format retrieved chunks into a single context string."""
    if not chunks:
        return FormattedContext(text="", truncated=False)

    formatted_parts = _format_parts(chunks, format_mode)
    return _apply_length_cap(formatted_parts, format_mode, separator, max_length)


def _format_parts(
    chunks: list[RetrievalChunk],
    format_mode: Literal["numbered", "separated", "raw"],
) -> list[str]:
    """Format each chunk according to the mode."""
    if format_mode == "numbered":
        return [f"{i + 1}. {chunk.content}" for i, chunk in enumerate(chunks)]
    else:
        return [chunk.content for chunk in chunks]


def _joiner_for(format_mode: Literal["numbered", "separated", "raw"], separator: str) -> str:
    if format_mode == "numbered":
        return "\n"
    elif format_mode == "separated":
        return separator
    else:  # raw
        return ""


def _apply_length_cap(
    parts: list[str],
    format_mode: Literal["numbered", "separated", "raw"],
    separator: str,
    max_length: int | None,
) -> FormattedContext:
    """Apply max_length truncation at chunk boundaries where possible."""
    joiner = _joiner_for(format_mode, separator)
    full_text = joiner.join(parts)

    if max_length is None or len(full_text) <= max_length:
        return FormattedContext(text=full_text, truncated=False)

    # Try to truncate at chunk boundaries
    included: list[str] = []
    current_length = 0

    for i, part in enumerate(parts):
        part_length = len(part)
        joiner_length = len(joiner) if i > 0 else 0

        if current_length + joiner_length + part_length <= max_length:
            included.append(part)
            current_length += joiner_length + part_length
        else:
            break

    if included:
        return FormattedContext(text=joiner.join(included), truncated=True)

    # First chunk exceeds limit — hard truncate with indicator.
    # The indicator must fit WITHIN max_length, not be appended after.
    indicator = "[truncated]"
    content_budget = max_length - len(indicator)
    if content_budget > 0:
        truncated_text = parts[0][:content_budget] + indicator
    else:
        # max_length too small for indicator — hard truncate without it.
        truncated_text = parts[0][:max_length]
    return FormattedContext(text=truncated_text, truncated=True)
