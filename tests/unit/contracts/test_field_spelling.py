"""The field-name spelling rule's one predicate and its build-time gate.

Operator ruling 2026-09-25 (elspeth-5887fb7928): a declaration names the
canonical field; a header spelling is refused. The 2026-09-26 amendment fixes
the predicate every surface shares: ``T not in names and C = resolve(T) != T and
C in names -> C``, where ``resolve`` is the upstream's own resolution (the row
contract's ``find_name``, the sources' ``field_mapping`` renames, normalization)
— Codex final review finding 1: comparing only ``normalize(T)`` let a header the
source renames bypass the rule.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.field_spelling import (
    NO_SOURCE_RENAMES,
    NORMALIZATION_ONLY,
    DeclaredSpellings,
    FieldNameResolution,
    HeaderSpelling,
    SourceFieldRenames,
    describe_header_spelling,
    header_normalization_remedy,
    header_spelled_declarations,
    header_spelled_names,
    header_spelling_canonical,
    normalized_field_name_or_empty,
)
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.plugins.sources.field_normalization import normalize_field_name

# The Codex shape: CSV header 'Name', source field_mapping {name: b}.
RENAMED = FieldNameResolution.of_source_renames([SourceFieldRenames(mapping={"name": "b"}, keys="normalized")])
# The same rename on a headerless source: columns [Name], field_mapping {Name: b}.
RENAMED_COLUMN = FieldNameResolution.of_source_renames([SourceFieldRenames(mapping={"Name": "b"}, keys="as_written")])


def _contract(*fields: tuple[str, str]) -> SchemaContract:
    """A row contract of (normalized_name, original_name) str fields."""
    return SchemaContract(
        mode="OBSERVED",
        fields=tuple(FieldContract(normalized, original, str, False, "inferred") for normalized, original in fields),
        locked=True,
    )


def _plain(*keys: str) -> SchemaContract:
    """A row contract whose every field is keyed by its own original name (no header differs from its key)."""
    return _contract(*((key, key) for key in keys))


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
        assert header_spelled_names(["Zed", "Name", "id", "Name"], {"id", "name", "zed"}, NORMALIZATION_ONLY, kind="read") == (
            HeaderSpelling(literal="Name", canonical="name", kind="read", leg="normalized"),
            HeaderSpelling(literal="Zed", canonical="zed", kind="read", leg="normalized"),
        )


class TestUpstreamResolution:
    """The predicate resolves a declared name the way the upstream resolves it (Codex final finding 1)."""

    @pytest.mark.parametrize("literal", ["Name", "NAME", "name"])
    def test_a_source_rename_makes_every_spelling_of_the_mapped_header_a_spelling_of_its_target(self, literal: str) -> None:
        # Rows are keyed field_mapping.get(normalize(h), normalize(h)): any header
        # normalizing to 'name' becomes 'b', so 'Name', 'NAME' and the fixed
        # point 'name' itself all name 'b' — the fixed point is no longer exempt.
        assert header_spelled_names([literal], {"b"}, RENAMED, kind="read") == (
            HeaderSpelling(literal=literal, canonical="b", kind="read", leg="renamed"),
        )

    def test_without_the_rename_the_mapped_header_names_nothing(self) -> None:
        # The control: normalization alone never reaches 'b' (the bypass).
        assert header_spelled_names(["Name", "name"], {"b"}, NORMALIZATION_ONLY, kind="read") == ()

    def test_the_rename_target_itself_is_the_canonical_declaration(self) -> None:
        assert header_spelled_names(["b"], {"b"}, RENAMED, kind="read") == ()

    def test_a_rename_whose_target_is_absent_settles_nothing(self) -> None:
        assert header_spelled_names(["Name"], {"id"}, RENAMED, kind="read") == ()

    def test_every_source_reaching_the_node_contributes_its_renames(self) -> None:
        both = FieldNameResolution.of_source_renames(
            [
                SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
                SourceFieldRenames(mapping={"name": "c"}, keys="normalized"),
                SourceFieldRenames(mapping={"Name": "d"}, keys="as_written"),
                NO_SOURCE_RENAMES,
            ]
        )
        assert both.renames == {"name": (("b", "renamed"), ("c", "renamed"))}
        assert both.renames_as_written == {"Name": (("d", "renamed_as_written"),)}
        assert header_spelled_names(["Name"], {"c"}, both, kind="read") == (
            HeaderSpelling(literal="Name", canonical="c", kind="read", leg="renamed"),
        )

    def test_a_headerless_rename_is_keyed_by_the_column_as_written(self) -> None:
        # A headerless source keys field_mapping by the column as written
        # (resolve_field_names): 'Name' names 'b'; 'NAME' and 'name' are not
        # keys it renames, so they name nothing the rows carry.
        assert header_spelled_names(["Name"], {"b"}, RENAMED_COLUMN, kind="read") == (
            HeaderSpelling(literal="Name", canonical="b", kind="read", leg="renamed_as_written"),
        )
        assert header_spelled_names(["NAME", "name"], {"b"}, RENAMED_COLUMN, kind="read") == ()

    def test_a_headered_rename_is_not_keyed_as_written(self) -> None:
        # The converse: under a header row the key 'Name' is never matched (keys
        # are normalized headers), so only 'name'-normalizing literals name 'b'.
        headered = FieldNameResolution.of_source_renames([SourceFieldRenames(mapping={"name": "b"}, keys="normalized")])
        assert headered.renames_as_written == {}
        assert header_spelled_names(["Name"], {"b"}, headered, kind="read")[0].leg == "renamed"

    def test_the_row_contract_resolves_the_recorded_original_name(self) -> None:
        # At run time the source's contract records b's original name 'Name';
        # find_name('Name') is what row['Name'] reads, so the declaration names it.
        contract = _contract(("b", "Name"), ("id", "id"))
        surface = DeclaredSpellings.of(reads=["Name", "id"], creates=[])
        assert surface.in_row(row_keys={"b", "id"}, forwarded_keys=(), contract=contract) == (
            HeaderSpelling(literal="Name", canonical="b", kind="read", leg="recorded"),
        )
        # The control: a row whose 'b' was never the header 'Name' (no rename
        # recorded) leaves 'Name' naming nothing it carries.
        assert surface.in_row(row_keys={"b", "id"}, forwarded_keys=(), contract=_contract(("b", "b"), ("id", "id"))) == ()

    def test_a_created_name_that_a_lookup_would_resolve_elsewhere_is_a_shadow(self) -> None:
        contract = _contract(("b", "Name"))
        surface = DeclaredSpellings.of(reads=[], creates=["Name"])
        assert surface.in_row(row_keys={"b"}, forwarded_keys={"b"}, contract=contract) == (
            HeaderSpelling(literal="Name", canonical="b", kind="create", leg="recorded"),
        )

    def test_the_remedy_names_the_leg_that_fired(self) -> None:
        renamed = describe_header_spelling(HeaderSpelling(literal="Name", canonical="b", kind="read", leg="renamed"))
        key = describe_header_spelling(HeaderSpelling(literal="name", canonical="b", kind="read", leg="renamed"))
        recorded = describe_header_spelling(HeaderSpelling(literal="Name", canonical="b", kind="read", leg="recorded"))
        plain = describe_header_spelling(HeaderSpelling(literal="Name", canonical="name", kind="read", leg="recorded"))
        assert renamed == (
            "'Name' is a header spelling of 'b': headers are normalized to lowercase identifiers ('Name' -> 'name') "
            "and the source's field_mapping renames 'name' to 'b'. Declare 'b'"
        )
        assert key == "'name' is a header spelling of 'b': the source's field_mapping renames 'name' to 'b'. Declare 'b'"
        assert (
            recorded
            == "'Name' is a header spelling of 'b': rows carry the field it names as 'b' (a lookup of 'Name' reads that field). Declare 'b'"
        )
        column = describe_header_spelling(HeaderSpelling(literal="Name", canonical="b", kind="read", leg="renamed_as_written"))
        assert column == "'Name' is a header spelling of 'b': the source's field_mapping renames its column 'Name' to 'b'. Declare 'b'"
        # A recorded original that is only the normalized header keeps the source's own sentence.
        assert plain == f"'Name' is a header spelling of 'name': {header_normalization_remedy('Name', 'name')}"


def _gate(
    *,
    reads: list[str],
    creates: list[str],
    present: set[str] | frozenset[str],
    forwarded: set[str] | frozenset[str],
    participated: bool,
    closed: bool,
    resolution: FieldNameResolution = NORMALIZATION_ONLY,
) -> tuple[HeaderSpelling, ...]:
    return header_spelled_declarations(
        spellings=DeclaredSpellings.of(reads=reads, creates=creates),
        present=present,
        forwarded=forwarded,
        participated=participated,
        closed=closed,
        resolution=resolution,
    )


class TestTransformRenames:
    """The build follows a transform's identity-carrying renames (review-C1-alias-bypass-r2).

    field_mapper ``{b: c}`` carries b's recorded original name onto ``c``
    (``narrow_contract_to_output(renamed_fields=...)``), so at run time a
    lookup of any spelling of the old field reads ``c``. The build resolution
    must name ``c`` for the same spellings, and nothing a lookup does not.
    """

    def test_a_source_renamed_header_follows_the_transform_rename(self) -> None:
        carried = RENAMED.then_renamed({"b": "c"})
        assert header_spelled_names(["Name", "name"], {"id", "c"}, carried, kind="read") == (
            HeaderSpelling(literal="Name", canonical="c", kind="read", leg="carried"),
            HeaderSpelling(literal="name", canonical="c", kind="read", leg="carried"),
        )

    def test_the_name_a_source_rename_gave_the_field_does_not_follow_it(self) -> None:
        # b's original name is the header 'Name', not 'b': after the rename a
        # lookup of 'b' reads nothing, so creating 'b' afresh shadows nothing.
        carried = RENAMED.then_renamed({"b": "c"})
        assert header_spelled_names(["b"], {"id", "c"}, carried, kind="create") == ()

    def test_an_unmapped_header_follows_the_transform_rename_by_its_own_name(self) -> None:
        # No source rename: the field's original is the header it normalizes
        # from, so 'name' and 'Name' both read 'c' once name -> c.
        carried = NORMALIZATION_ONLY.then_renamed({"name": "c"})
        assert header_spelled_names(["Name", "name"], {"id", "c"}, carried, kind="create") == (
            HeaderSpelling(literal="Name", canonical="c", kind="create", leg="carried"),
            HeaderSpelling(literal="name", canonical="c", kind="create", leg="carried"),
        )

    def test_a_rename_source_is_resolved_as_the_lookup_it_is(self) -> None:
        # field_mapper {Name: c} behind source {name: b} renames the field 'Name' names: b.
        carried = RENAMED.then_renamed({"Name": "c"})
        assert [spelling.canonical for spelling in header_spelled_names(["Name"], {"c"}, carried, kind="read")] == ["c"]

    def test_chained_renames_compose_and_intermediate_names_do_not_follow(self) -> None:
        for base in (RENAMED, NORMALIZATION_ONLY):
            chained = base.then_renamed({"b" if base is RENAMED else "name": "c"}).then_renamed({"c": "d"})
            assert [spelling.canonical for spelling in header_spelled_names(["Name"], {"d"}, chained, kind="read")] == ["d"]
            # 'c' was only the name a rename gave the field; its identity stayed 'Name'.
            assert header_spelled_names(["c"], {"d"}, chained, kind="create") == ()

    def test_a_branch_that_did_not_rename_keeps_the_field_under_its_own_name(self) -> None:
        renamed_arm = NORMALIZATION_ONLY.then_renamed({"name": "c"})
        merged = FieldNameResolution.union([renamed_arm, NORMALIZATION_ONLY])
        assert [spelling.canonical for spelling in header_spelled_names(["Name"], {"name"}, merged, kind="read")] == ["name"]
        assert [spelling.canonical for spelling in header_spelled_names(["Name"], {"c"}, merged, kind="read")] == ["c"]

    def test_union_order_does_not_change_the_resolution(self) -> None:
        renamed_arm = RENAMED.then_renamed({"b": "c"})
        assert FieldNameResolution.union([renamed_arm, RENAMED]) == FieldNameResolution.union([RENAMED, renamed_arm])

    def test_an_identity_or_empty_rename_changes_nothing(self) -> None:
        assert RENAMED.then_renamed({}) is RENAMED
        assert RENAMED.then_renamed({"b": "b"}) is RENAMED

    def test_the_remedy_names_the_carried_field(self) -> None:
        [spelling] = header_spelled_names(["Name"], {"c"}, RENAMED.then_renamed({"b": "c"}), kind="read")
        assert describe_header_spelling(spelling) == (
            "'Name' is a header spelling of 'c': a transform upstream renames the field it names to 'c', so rows carry it "
            "as 'c' (a lookup of 'Name' reads that field). Declare 'c'"
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
        assert closed_vote == (HeaderSpelling(literal="Name", canonical="name", kind="read", leg="normalized"),)

    def test_a_renamed_header_is_refused_against_a_closed_vote(self) -> None:
        # The Codex shape at build: fixed source b: str under field_mapping {name: b}.
        assert _gate(reads=["Name"], creates=[], present={"b"}, forwarded={"b"}, participated=True, closed=True, resolution=RENAMED) == (
            HeaderSpelling(literal="Name", canonical="b", kind="read", leg="renamed"),
        )
        assert _gate(reads=["Name"], creates=[], present={"b"}, forwarded={"b"}, participated=True, closed=True) == ()

    def test_a_created_name_needs_only_participation(self) -> None:
        assert _gate(reads=[], creates=["Name"], present={"id", "name"}, forwarded={"id", "name"}, participated=True, closed=False) == (
            HeaderSpelling(literal="Name", canonical="name", kind="create", leg="normalized"),
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
    """The run-time residual: every declaration reduced once per node, the membership tests per row."""

    def test_every_declaration_is_kept_because_a_rename_can_make_a_fixed_point_a_spelling(self) -> None:
        surface = DeclaredSpellings.of(reads=["id", "name", "sci__rag_context"], creates=["total"])

        assert [name.literal for name in surface.reads] == ["id", "name", "sci__rag_context"]
        assert [(name.literal, name.normalized) for name in surface.creates] == [("total", "total")]
        assert (
            surface.in_row(
                row_keys={"id", "name", "sci__rag_context"}, forwarded_keys={"id"}, contract=_plain("id", "name", "sci__rag_context")
            )
            == ()
        )

    def test_the_row_verdict_equals_the_predicate(self) -> None:
        surface = DeclaredSpellings.of(reads=["Name", "Total"], creates=["Label", "Name"])
        row_keys = frozenset({"name", "label", "id"})

        spelled = surface.in_row(row_keys=row_keys, forwarded_keys=row_keys, contract=_plain(*row_keys))

        # One entry per literal; a name both read and created reports as created.
        assert spelled == (
            HeaderSpelling(literal="Label", canonical="label", kind="create", leg="normalized"),
            HeaderSpelling(literal="Name", canonical="name", kind="create", leg="normalized"),
        )
        for spelling in spelled:
            assert header_spelling_canonical(spelling.literal, row_keys) == spelling.canonical
        assert header_spelling_canonical("Total", row_keys) is None

    def test_only_a_node_that_declares_nothing_is_empty(self) -> None:
        # The build gate and the composer skip the upstream walk for an empty surface.
        assert DeclaredSpellings.of(reads=[], creates=[]).is_empty
        assert not DeclaredSpellings.of(reads=["name"], creates=[]).is_empty
        assert not DeclaredSpellings.of(reads=[], creates=["Name"]).is_empty

    def test_a_created_name_is_checked_against_the_forwarded_keys_only(self) -> None:
        surface = DeclaredSpellings.of(reads=[], creates=["Name"])

        assert surface.in_row(row_keys={"id", "name"}, forwarded_keys={"id"}, contract=_plain("id", "name")) == ()
