"""Tests for field normalization algorithm."""

import concurrent.futures
import keyword
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from elspeth.plugins.sources.field_normalization import ExternalHeaderError, FieldResolution


class TestNormalizeFieldName:
    """Unit tests for normalize_field_name function."""

    def test_basic_normalization_spaces_to_underscore(self) -> None:
        """Spaces are replaced with underscores."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("User ID") == "user_id"

    def test_basic_normalization_lowercase(self) -> None:
        """Mixed case is lowercased."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("CaSE Study1 !!!! xx!") == "case_study1_xx"

    def test_special_chars_replaced(self) -> None:
        """Special characters become underscores, collapsed."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("data.field") == "data_field"
        assert normalize_field_name("amount$$$") == "amount"

    def test_unicode_numbers_that_are_not_identifier_continue_are_stripped(self) -> None:
        """Unicode number symbols like superscript two are external header text."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("Area (m\u00b2)") == "area_m"
        assert normalize_field_name("persons/km\u00b2") == "persons_km"
        assert normalize_field_name("ratio \u00bd") == "ratio"

    def test_leading_digit_prefixed(self) -> None:
        """Leading digits get underscore prefix."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("123_field") == "_123_field"

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace stripped."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("  Amount  ") == "amount"

    def test_empty_result_raises_error(self) -> None:
        """Headers that normalize to empty raise ValueError."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        with pytest.raises(ValueError, match="normalizes to empty"):
            normalize_field_name("!!!")

    def test_algorithm_version_available(self) -> None:
        """Algorithm version is accessible for audit trail."""
        from elspeth.plugins.sources.field_normalization import (
            NORMALIZATION_ALGORITHM_VERSION,
        )

        assert NORMALIZATION_ALGORITHM_VERSION == "1.0.1"

    def test_unicode_bom_stripped(self) -> None:
        """BOM character at start is stripped."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("\ufeffid") == "id"

    def test_zero_width_chars_stripped(self) -> None:
        """Zero-width characters are stripped."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("id\u200b") == "id"

    def test_emoji_stripped(self) -> None:
        """Emoji characters are stripped."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("Status 🔥") == "status"

    def test_python_keyword_gets_suffix(self) -> None:
        """Python keywords get underscore suffix."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("class") == "class_"
        assert normalize_field_name("for") == "for_"
        assert normalize_field_name("import") == "import_"

    def test_header_normalizing_to_keyword_gets_suffix(self) -> None:
        """Headers that normalize to keywords also get suffix."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("CLASS") == "class_"
        assert normalize_field_name("For ") == "for_"

    def test_dunder_names_neutralized(self) -> None:
        """Dunder names from external headers are stripped of leading/trailing underscores.

        Security regression test: __class__, __init__, __import__ must not
        survive normalization as dunder patterns. Step 6 (strip underscores)
        is the critical defense.
        """
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("__class__") == "class_"  # stripped + keyword suffix
        assert normalize_field_name("__init__") == "init"
        assert normalize_field_name("__import__") == "import_"  # stripped + keyword suffix
        assert normalize_field_name("___private") == "private"
        assert normalize_field_name("__double__leading__trailing__") == "double_leading_trailing"

    def test_accented_chars_preserved(self) -> None:
        """Accented characters are valid identifiers (PEP 3131)."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        assert normalize_field_name("café") == "café"
        assert normalize_field_name("naïve") == "naïve"

    def test_unicode_nfc_normalization_consistent(self) -> None:
        """Unicode characters in different forms normalize to same result."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        # "é" can be: precomposed U+00E9, or decomposed U+0065 U+0301
        precomposed = "café"
        decomposed = "caf\u0065\u0301"

        assert precomposed != decomposed  # Different byte representations
        assert normalize_field_name(precomposed) == normalize_field_name(decomposed)


class TestNormalizationProperties:
    """Property-based tests for normalization invariants."""

    @given(raw=st.text(min_size=1, max_size=100))
    def test_property_normalized_result_is_identifier(self, raw: str) -> None:
        """Property: All normalized results are valid Python identifiers."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        try:
            result = normalize_field_name(raw)
            # If it didn't raise, result must be valid identifier
            assert result.isidentifier(), f"'{result}' is not a valid identifier"
            # And not a keyword (keywords get suffix)
            assert not keyword.iskeyword(result), f"'{result}' is a keyword without suffix"
        except ExternalHeaderError as e:
            # External headers may normalize away to nothing; any other
            # non-identifier result should be treated as a test failure.
            assert "normalizes to empty" in str(e), f"Unexpected error: {e}"

    @given(raw=st.text(min_size=1, max_size=100))
    def test_property_normalization_is_idempotent(self, raw: str) -> None:
        """Property: Normalizing twice gives same result as normalizing once."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        try:
            once = normalize_field_name(raw)
            twice = normalize_field_name(once)
            assert once == twice, f"Not idempotent: '{once}' != '{twice}'"
        except ExternalHeaderError:
            pass  # Empty result expected for some inputs


class TestNormalizationThreadSafety:
    """Thread safety tests - module-level regex patterns are immutable but verify."""

    def test_concurrent_normalization_no_interference(self) -> None:
        """Multiple threads normalizing fields doesn't cause interference."""
        from elspeth.plugins.sources.field_normalization import normalize_field_name

        headers = ["User ID", "Amount $", "CaSE Study1", "data.field"]
        expected = ["user_id", "amount", "case_study1", "data_field"]

        def normalize_batch(batch: list[str]) -> list[str]:
            return [normalize_field_name(h) for h in batch]

        # Run 100 iterations in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(normalize_batch, headers) for _ in range(100)]
            results = [f.result() for f in futures]

        # All results should be identical
        for result in results:
            assert result == expected


class TestCollisionDetection:
    """Tests for collision detection functions."""

    def test_no_collision_passes(self) -> None:
        """No collision when all normalized names are unique."""
        from elspeth.plugins.sources.field_normalization import check_normalization_collisions

        raw = ["User ID", "Amount", "Date"]
        normalized = ["user_id", "amount", "date"]
        # Should not raise
        check_normalization_collisions(raw, normalized)

    def test_two_way_collision_raises(self) -> None:
        """Two headers normalizing to same value raises error."""
        from elspeth.plugins.sources.field_normalization import check_normalization_collisions

        raw = ["Case Study 1", "case-study-1"]
        normalized = ["case_study_1", "case_study_1"]

        with pytest.raises(ValueError, match="collision") as exc_info:
            check_normalization_collisions(raw, normalized)

        # Error should mention both original headers
        assert "Case Study 1" in str(exc_info.value)
        assert "case-study-1" in str(exc_info.value)

    def test_three_way_collision_lists_all(self) -> None:
        """Three+ headers colliding lists all of them."""
        from elspeth.plugins.sources.field_normalization import check_normalization_collisions

        raw = ["A B", "a-b", "A  B"]
        normalized = ["a_b", "a_b", "a_b"]

        with pytest.raises(ValueError, match="collision") as exc_info:
            check_normalization_collisions(raw, normalized)

        error = str(exc_info.value)
        assert "A B" in error
        assert "a-b" in error
        assert "A  B" in error


class TestMappingCollisionDetection:
    """Tests for field_mapping collision detection."""

    def test_no_collision_passes(self) -> None:
        """No collision when mapping targets are unique."""
        from elspeth.plugins.sources.field_normalization import check_mapping_collisions

        mapping = {"user_id": "uid", "amount": "amt"}
        headers = ["user_id", "amount", "date"]
        final = ["uid", "amt", "date"]
        # Should not raise
        check_mapping_collisions(headers, final, mapping)

    def test_mapping_collision_raises(self) -> None:
        """Mapping two fields to same target raises error."""
        from elspeth.plugins.sources.field_normalization import check_mapping_collisions

        mapping = {"a": "x", "b": "x"}
        headers = ["a", "b", "c"]
        final = ["x", "x", "c"]

        with pytest.raises(ValueError, match="collision") as exc_info:
            check_mapping_collisions(headers, final, mapping)

        error = str(exc_info.value)
        assert "'a'" in error
        assert "'b'" in error
        assert "'x'" in error

    def test_mapping_to_existing_field_raises(self) -> None:
        """Mapping to name that already exists as unmapped field raises error."""
        from elspeth.plugins.sources.field_normalization import check_mapping_collisions

        # "uid" exists naturally, and we try to map "user_id" to "uid"
        mapping = {"user_id": "uid"}
        headers = ["user_id", "uid", "date"]
        final = ["uid", "uid", "date"]

        with pytest.raises(ValueError, match="collision"):
            check_mapping_collisions(headers, final, mapping)


class TestResolveFieldNames:
    """Tests for the complete field resolution flow."""

    def test_normalize_only(self) -> None:
        """Resolution with raw headers always normalizes."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        raw_headers = ["User ID", "Amount $"]
        result = resolve_field_names(
            raw_headers=raw_headers,
            field_mapping=None,
            columns=None,
        )

        assert result.final_headers == ("user_id", "amount")
        assert result.resolution_mapping == {
            "User ID": "user_id",
            "Amount $": "amount",
        }
        assert result.normalization_version == "1.0.1"

    def test_resolution_handles_common_unit_superscript_headers(self) -> None:
        """Real-world unit headers normalize instead of escaping as plain ValueError."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        result = resolve_field_names(
            raw_headers=["Area (m\u00b2)", "persons/km\u00b2"],
            field_mapping=None,
            columns=None,
        )

        assert result.final_headers == ("area_m", "persons_km")
        assert result.resolution_mapping == {
            "Area (m\u00b2)": "area_m",
            "persons/km\u00b2": "persons_km",
        }

    def test_normalize_with_mapping(self) -> None:
        """Resolution with normalize + mapping override."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        raw_headers = ["User ID", "Amount $"]
        result = resolve_field_names(
            raw_headers=raw_headers,
            field_mapping={"user_id": "uid"},
            columns=None,
        )

        assert result.final_headers == ("uid", "amount")
        assert result.resolution_mapping == {
            "User ID": "uid",
            "Amount $": "amount",
        }

    def test_columns_mode(self) -> None:
        """Resolution with explicit columns (headerless mode)."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        result = resolve_field_names(
            raw_headers=None,
            field_mapping=None,
            columns=["id", "name", "amount"],
        )

        assert result.final_headers == ("id", "name", "amount")
        assert result.resolution_mapping == {
            "id": "id",
            "name": "name",
            "amount": "amount",
        }
        assert result.normalization_version is None  # No normalization

    def test_columns_with_mapping(self) -> None:
        """Resolution with columns + mapping override."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        result = resolve_field_names(
            raw_headers=None,
            field_mapping={"id": "customer_id"},
            columns=["id", "name"],
        )

        assert result.final_headers == ("customer_id", "name")
        assert result.resolution_mapping == {
            "id": "customer_id",
            "name": "name",
        }

    def test_collision_on_duplicate_raw_headers_raises(self) -> None:
        """Duplicate raw headers that normalize to same value raise collision error."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        with pytest.raises(ValueError, match="collision"):
            resolve_field_names(
                raw_headers=["id", "ID"],
                field_mapping=None,
                columns=None,
            )

    def test_mapping_key_not_found_raises(self) -> None:
        """Mapping key not in headers raises helpful error."""
        from elspeth.plugins.sources.field_normalization import resolve_field_names

        with pytest.raises(ValueError, match="not found") as exc_info:
            resolve_field_names(
                raw_headers=["user_id", "amount"],
                field_mapping={"nonexistent": "x"},
                columns=None,
            )

        error = str(exc_info.value)
        assert "nonexistent" in error
        assert "user_id" in error  # Shows available headers


class TestFieldResolutionImmutability:
    """FieldResolution containers must be deeply immutable.

    FieldResolution crosses source→audit→sink boundaries, so mutation
    after construction would cause audit inconsistency.
    """

    def test_final_headers_is_tuple(self) -> None:
        """final_headers must be stored as tuple, not list."""
        resolution = FieldResolution(
            final_headers=("name", "amount"),
            resolution_mapping=MappingProxyType({"Name": "name", "Amount": "amount"}),
            normalization_version="v1",
        )
        assert isinstance(resolution.final_headers, tuple)

    def test_resolution_mapping_is_immutable(self) -> None:
        """resolution_mapping must be wrapped in MappingProxyType."""
        resolution = FieldResolution(
            final_headers=("name", "amount"),
            resolution_mapping=MappingProxyType({"Name": "name", "Amount": "amount"}),
            normalization_version="v1",
        )
        assert isinstance(resolution.resolution_mapping, MappingProxyType)
        with pytest.raises(TypeError):
            resolution.resolution_mapping["injected"] = "evil"  # type: ignore[index]

    def test_caller_list_mutation_does_not_affect_instance(self) -> None:
        """Defensive copy: mutating the original list must not affect the frozen instance."""
        original_headers = ["name", "amount"]
        resolution = FieldResolution(
            final_headers=tuple(original_headers),
            resolution_mapping=MappingProxyType({"Name": "name", "Amount": "amount"}),
            normalization_version="v1",
        )
        original_headers.append("injected")
        assert len(resolution.final_headers) == 2

    def test_caller_dict_mutation_does_not_affect_instance(self) -> None:
        """Defensive copy: mutating the original dict must not affect the frozen instance."""
        original_mapping = {"Name": "name", "Amount": "amount"}
        resolution = FieldResolution(
            final_headers=("name", "amount"),
            resolution_mapping=MappingProxyType(original_mapping),
            normalization_version="v1",
        )
        original_mapping["injected"] = "evil"
        assert "injected" not in resolution.resolution_mapping


class TestUndeclaredRowFields:
    """``undeclared_row_fields`` is the shared config-time coverage predicate.

    Both authoring surfaces call it (``LLMConfig`` and the composer's
    ``_validate_prompt_template_variable_bindings``), so a defect here is a
    defect in both at once (elspeth-a9ba80cb0b).
    """

    def _u(self, fields: set[str], declared: list[str]) -> tuple[str, ...]:
        from elspeth.plugins.sources.field_normalization import undeclared_row_fields

        return undeclared_row_fields(fields, declared)

    def test_reports_a_field_the_declaration_does_not_cover(self) -> None:
        assert self._u({"case_study"}, ["case_study_1"]) == ("case_study",)

    def test_literal_match_covers(self) -> None:
        assert self._u({"case_study"}, ["case_study"]) == ()

    def test_wider_declaration_covers(self) -> None:
        assert self._u({"case_study"}, ["case_study", "audit_id"]) == ()

    def test_reports_only_the_shortfall_sorted(self) -> None:
        assert self._u({"zeta", "alpha", "a"}, ["a"]) == ("alpha", "zeta")

    def test_a_declarable_variant_is_reported_not_bridged(self) -> None:
        """Coverage is EXACT, because render-time resolution is.

        ``SchemaContract.find_name`` matches a field's ``normalized_name`` OR
        its ``original_name`` — two exact spellings, neither knowable at config
        time. So ``{{ row.Name }}`` against a declared ``name`` resolves only if
        the producer's original header happened to be ``Name``; measured
        against a row whose one column is ``a_b``, twelve declarable spellings
        (``A_B``, ``a__b``, ``A_B_``, ...) raise on every row and one renders.
        An earlier version bridged these through ``normalize_field_name`` and
        silenced all twelve, including plain typos of the declared name.
        """
        assert self._u({"Name"}, ["name"]) == ("Name",)
        assert self._u({"a__b"}, ["a_b"]) == ("a__b",)
        assert self._u({"A_B_"}, ["a_b"]) == ("A_B_",)

    def test_undeclarable_literal_is_bridged_to_its_canonical_key(self) -> None:
        """The one sound inference, and it runs the OTHER way.

        A literal that is not a legal declaration entry can never be a
        ``normalized_name``, so it can only be an ``original_name`` and the row
        key it resolves to IS its canonical form.
        """
        assert self._u({"Original Header"}, ["original_header"]) == ()
        assert self._u({"class"}, ["class_"]) == ()
        assert self._u({"meta.prompt"}, ["meta_prompt"]) == ()

    def test_undeclarable_literal_is_reported_when_its_canonical_key_is_not_declared(self) -> None:
        """Dropping these unconditionally was a false negative, not caution.

        The declaration omitting the field entirely was accepted and then
        raised at render for every row — while the sibling declared-fields
        validator was already telling authors to declare exactly this name.
        """
        assert self._u({"Original Header"}, ["something_else"]) == ("Original Header",)
        assert self._u({"class"}, ["unrelated"]) == ("class",)

    def test_a_literal_with_no_declarable_form_is_dropped(self) -> None:
        """There is nothing to ask for: ``'!!!'`` normalizes to nothing."""
        assert self._u({"!!!"}, ["x"]) == ()

    def test_declarable_underscore_is_reported(self) -> None:
        """``_`` IS a legal declaration entry, so its shortfall is repairable."""
        assert self._u({"_"}, ["x"]) == ("_",)
        assert self._u({"_"}, ["_"]) == ()

    def test_undeclarable_declaration_entries_cover_nothing(self) -> None:
        assert self._u({"a"}, ["Original Header"]) == ("a",)

    def test_empty_inputs(self) -> None:
        assert self._u(set(), ["a"]) == ()
        assert self._u({"a"}, []) == ("a",)

    def test_the_repair_the_renderer_names_is_the_one_the_check_accepts(self) -> None:
        """``describe_undeclared_row_fields`` and ``undeclared_row_fields`` are
        the repair and the check for one contract, and they disagreed once."""
        from elspeth.plugins.sources.field_normalization import (
            declarable_field_name,
            describe_undeclared_row_fields,
        )

        for literal in ("Original Header", "class", "meta.prompt", "case_study", "Name"):
            covering = declarable_field_name(literal)
            assert covering is not None
            assert self._u({literal}, [covering]) == (), f"{literal!r} not cleared by its own named repair"
            rendered = describe_undeclared_row_fields((literal,))
            assert covering in rendered

        assert declarable_field_name("!!!") is None
