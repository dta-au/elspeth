"""In-memory duplicate-rationale evidence over allowlist entries.

ONE derivation for every judge surface. The operator ``justify`` CLI, the
key-free ``stage_preview`` MCP tool, and the ``reaudit`` sweep all feed the
judge a ``rationale_duplicate_count`` + ``similar_entries`` pair; deriving it
in three places is how the preview seam drifted from the real signing path
(elspeth-0502deb48c). Callers that already hold loaded entries pass them
straight in; callers that only have a directory load the allowlist first and
then call this module — there is no I/O here.

Exact normalized duplicates are a strong, auditable signal of copy/paste
rationale drift. Fuzzier similarity can be added later without weakening the
hard duplicate count.
"""

from __future__ import annotations

from collections.abc import Sequence

from elspeth_lints.core.allowlist import AllowlistEntry
from elspeth_lints.core.judge import SimilarAllowlistEntry

SIMILAR_ENTRY_LIMIT = 5
REASON_EXCERPT_LIMIT = 240


def normalize_rationale_for_similarity(text: str) -> str:
    """Normalize rationale text for exact duplicate detection."""
    return " ".join(text.casefold().split())


def reason_excerpt(text: str, *, limit: int = REASON_EXCERPT_LIMIT) -> str:
    """Return a compact single-line rationale excerpt for prompt context."""
    single_line = " ".join(text.split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 3] + "..."


def find_similar_allowlist_entries(
    entries: Sequence[AllowlistEntry],
    *,
    rationale: str,
    exclude_key: str,
    limit: int = SIMILAR_ENTRY_LIMIT,
) -> tuple[int, tuple[SimilarAllowlistEntry, ...]]:
    """Return exact duplicate-rationale context for the judge prompt.

    The count is over ALL duplicates; ``similar_entries`` carries at most
    ``limit`` compact excerpts. An empty (or whitespace-only) rationale has
    no meaningful duplicate set and returns ``(0, ())``.
    """
    normalized = normalize_rationale_for_similarity(rationale)
    if not normalized:
        return 0, ()

    duplicates = [entry for entry in entries if entry.key != exclude_key and normalize_rationale_for_similarity(entry.reason) == normalized]
    similar_entries = tuple(
        SimilarAllowlistEntry(
            key=entry.key,
            owner=entry.owner,
            reason_excerpt=reason_excerpt(entry.reason),
        )
        for entry in duplicates[:limit]
    )
    return len(duplicates), similar_entries
