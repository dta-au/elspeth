"""Pins for the shared duplicate-rationale derivation (allowlist_similarity).

These behaviors were previously pinned only through the justify CLI's private
helper; stage_preview and reaudit now share the derivation, so the pins live
against the core module directly.
"""

from __future__ import annotations

from elspeth_lints.core.allowlist import AllowlistEntry
from elspeth_lints.core.allowlist_similarity import (
    REASON_EXCERPT_LIMIT,
    SIMILAR_ENTRY_LIMIT,
    find_similar_allowlist_entries,
    normalize_rationale_for_similarity,
    reason_excerpt,
)


def _entry(key: str, reason: str, owner: str = "owner@example") -> AllowlistEntry:
    return AllowlistEntry(key=key, owner=owner, reason=reason, safety="reviewed", expires=None)


class TestNormalization:
    def test_casefold_and_whitespace_collapse(self) -> None:
        assert normalize_rationale_for_similarity("  Parse  DON'T\tvalidate \n") == "parse don't validate"

    def test_empty_and_whitespace_only_normalize_to_empty(self) -> None:
        assert normalize_rationale_for_similarity("") == ""
        assert normalize_rationale_for_similarity(" \t\n ") == ""


class TestReasonExcerpt:
    def test_short_reason_passes_through_single_lined(self) -> None:
        assert reason_excerpt("line one\nline two") == "line one line two"

    def test_long_reason_truncates_with_ellipsis_at_limit(self) -> None:
        excerpt = reason_excerpt("x" * (REASON_EXCERPT_LIMIT + 50))
        assert len(excerpt) == REASON_EXCERPT_LIMIT
        assert excerpt.endswith("...")


class TestFindSimilarAllowlistEntries:
    def test_empty_rationale_returns_no_evidence(self) -> None:
        entries = [_entry("k1", "some reason")]
        assert find_similar_allowlist_entries(entries, rationale="", exclude_key="other") == (0, ())
        assert find_similar_allowlist_entries(entries, rationale="  \n", exclude_key="other") == (0, ())

    def test_exact_normalized_duplicates_counted_and_excerpted(self) -> None:
        entries = [
            _entry("k1", "Parse, don't validate"),
            _entry("k2", "parse,   DON'T validate"),
            _entry("k3", "an unrelated rationale"),
        ]
        count, similar = find_similar_allowlist_entries(entries, rationale="parse, don't validate", exclude_key="unrelated-key")
        assert count == 2
        assert [s.key for s in similar] == ["k1", "k2"]
        assert similar[0].owner == "owner@example"
        assert similar[0].reason_excerpt == "Parse, don't validate"

    def test_exclude_key_removes_the_entry_under_judgment(self) -> None:
        entries = [_entry("self", "shared text"), _entry("other", "shared text")]
        count, similar = find_similar_allowlist_entries(entries, rationale="shared text", exclude_key="self")
        assert count == 1
        assert [s.key for s in similar] == ["other"]

    def test_count_exceeds_the_excerpt_limit(self) -> None:
        entries = [_entry(f"k{i}", "same words") for i in range(SIMILAR_ENTRY_LIMIT + 3)]
        count, similar = find_similar_allowlist_entries(entries, rationale="same words", exclude_key="none")
        assert count == SIMILAR_ENTRY_LIMIT + 3
        assert len(similar) == SIMILAR_ENTRY_LIMIT
