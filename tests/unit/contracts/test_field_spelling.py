"""The field-name spelling rule's one predicate and its build-time gate.

Operator ruling 2026-09-25 (elspeth-5887fb7928): a declaration names the
canonical field; a header spelling is refused. The 2026-09-26 amendment fixes
the predicate every surface shares:
``T not in names and normalize(T) != T and normalize(T) in names -> normalize(T)``.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.field_spelling import (
    DeclaredSpellings,
    HeaderSpelling,
    header_normalization_remedy,
    header_spelled_declarations,
    header_spelled_names,
    header_spelling_canonical,
    normalized_field_name_or_empty,
)
from elspeth.plugins.sources.field_normalization import normalize_field_name


class TestHeaderSpellingCanonical:
    @pytest.mark.parametrize(
        ("name", "present", "expected"),
        [
            # The header spelling of an arriving field (probe A: header Name).
            ("Name", {"id", "name"}, "name"),
            # Probe B: the header was NAME, the declaration spells it Name. The
            # predicate normalizes the declaration, so any case variant is caught.
            ("NAME", {"id", "name"}, "name"),
            ("First Name", {"first_name"}, "first_name"),
            ("class", {"class_"}, "class_"),
            ("1st", {"_1st"}, "_1st"),
        ],
    )
    def test_a_non_canonical_spelling_of_a_present_field_is_named(self, name: str, present: set[str], expected: str) -> None:
        assert header_spelling_canonical(name, present) == expected

    @pytest.mark.parametrize(
        ("name", "present"),
        [
            # The canonical name itself (probe E) — an ordinary declaration.
            ("name", {"id", "name"}),
            # A name the row carries as written (probe F: a source field_mapping
            # value keeps 'Name'), even when its normalized form is also present.
            ("Name", {"Name", "name"}),
            # A non-canonical name whose canonical form is absent (probe C:
            # a created 'Total') is not a header spelling of anything present.
            ("Total", {"id", "name"}),
            # A name that normalizes to nothing names no field at all.
            ("_", {"id"}),
            ("!!!", {"id"}),
        ],
    )
    def test_everything_else_is_not_a_header_spelling(self, name: str, present: set[str]) -> None:
        assert header_spelling_canonical(name, present) is None

    @pytest.mark.parametrize("raw", ["Name", "  First  Name ", "CaSE Study1 !!!! xx!", "class", "1st", "﻿id", "café"])
    def test_the_contracts_algorithm_is_the_one_sources_use(self, raw: str) -> None:
        # One algorithm: the sources' Tier-3 entry point is a wrapper over it.
        assert normalize_field_name(raw) == normalized_field_name_or_empty(raw)

    def test_the_tier_3_wrapper_refuses_what_the_algorithm_empties(self) -> None:
        assert normalized_field_name_or_empty("!!!") == ""
        with pytest.raises(ValueError, match="normalizes to empty"):
            normalize_field_name("!!!")


class TestHeaderSpelledNames:
    def test_sorted_literals_with_their_canonical_names(self) -> None:
        assert header_spelled_names(["Zed", "Name", "id", "Name"], {"id", "name", "zed"}, kind="read") == (
            HeaderSpelling(literal="Name", canonical="name", kind="read"),
            HeaderSpelling(literal="Zed", canonical="zed", kind="read"),
        )


def _gate(
    *,
    reads: list[str],
    creates: list[str],
    present: set[str] | frozenset[str],
    forwarded: set[str] | frozenset[str],
    participated: bool,
    closed: bool,
) -> tuple[HeaderSpelling, ...]:
    return header_spelled_declarations(
        spellings=DeclaredSpellings.of(reads=reads, creates=creates),
        present=present,
        forwarded=forwarded,
        participated=participated,
        closed=closed,
    )


class TestBuildTimeGate:
    """``header_spelled_declarations`` is the one build-time verdict (DAG and composer)."""

    def test_an_abstaining_upstream_settles_nothing(self) -> None:
        assert _gate(reads=["Name"], creates=["Name"], present=frozenset(), forwarded=frozenset(), participated=False, closed=False) == ()

    def test_a_read_needs_a_closed_upstream(self) -> None:
        # Absence of 'Name' is proven only by an upper bound on the arriving fields.
        open_vote = _gate(reads=["Name"], creates=[], present={"name"}, forwarded={"name"}, participated=True, closed=False)
        closed_vote = _gate(reads=["Name"], creates=[], present={"name"}, forwarded={"name"}, participated=True, closed=True)
        assert open_vote == ()
        assert closed_vote == (HeaderSpelling(literal="Name", canonical="name", kind="read"),)

    def test_a_created_name_needs_only_participation(self) -> None:
        assert _gate(reads=[], creates=["Name"], present={"id", "name"}, forwarded={"id", "name"}, participated=True, closed=False) == (
            HeaderSpelling(literal="Name", canonical="name", kind="create"),
        )

    def test_the_canonical_spelling_and_an_unrelated_created_name_pass(self) -> None:
        assert (
            _gate(
                reads=["name"], creates=["Total", "name"], present={"id", "name"}, forwarded={"id", "name"}, participated=True, closed=True
            )
            == ()
        )


def test_the_remedy_is_the_sources_sentence() -> None:
    assert header_normalization_remedy("Name", "name", header_kind="CSV headers") == (
        "CSV headers are normalized to lowercase identifiers ('Name' -> 'name'). Declare 'name'"
    )


def test_a_created_name_is_checked_against_what_the_node_forwards() -> None:
    # A rename that removes 'name' and writes 'Name' restores the header as the key:
    # nothing arriving is shadowed, so it is not refused.
    assert _gate(reads=[], creates=["Name"], present={"id", "name"}, forwarded={"id"}, participated=True, closed=True) == ()


class TestDeclaredSpellings:
    """The run-time residual: the configuration leg once per node, the membership legs per row."""

    def test_canonical_declarations_leave_no_candidate(self) -> None:
        # The usual node: every declaration canonical, so a row pays nothing.
        surface = DeclaredSpellings.of(reads=["id", "name", "sci__rag_context"], creates=["total"])

        assert surface.reads == (HeaderSpelling(literal="sci__rag_context", canonical="sci_rag_context", kind="read"),)
        assert surface.creates == ()
        assert surface.in_row(row_keys={"id", "name", "sci__rag_context"}, forwarded_keys={"id"}) == ()

    def test_the_row_verdict_equals_the_predicate(self) -> None:
        surface = DeclaredSpellings.of(reads=["Name", "Total"], creates=["Label", "Name"])
        row_keys = frozenset({"name", "label", "id"})

        spelled = surface.in_row(row_keys=row_keys, forwarded_keys=row_keys)

        # One entry per literal; a name both read and created reports as created.
        assert spelled == (
            HeaderSpelling(literal="Label", canonical="label", kind="create"),
            HeaderSpelling(literal="Name", canonical="name", kind="create"),
        )
        for spelling in spelled:
            assert header_spelling_canonical(spelling.literal, row_keys) == spelling.canonical
        assert header_spelling_canonical("Total", row_keys) is None

    def test_only_a_node_without_candidates_is_empty(self) -> None:
        # The build gate and the composer skip the upstream walk for an empty surface.
        assert DeclaredSpellings.of(reads=["id", "name"], creates=["total"]).is_empty
        assert not DeclaredSpellings.of(reads=["Name"], creates=[]).is_empty
        assert not DeclaredSpellings.of(reads=[], creates=["Name"]).is_empty

    def test_a_created_name_is_checked_against_the_forwarded_keys_only(self) -> None:
        surface = DeclaredSpellings.of(reads=[], creates=["Name"])

        assert surface.in_row(row_keys={"id", "name"}, forwarded_keys={"id"}) == ()
