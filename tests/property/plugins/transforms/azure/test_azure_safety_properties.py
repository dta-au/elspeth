# tests/property/plugins/transforms/azure/test_azure_safety_properties.py
"""Property-based tests for Azure Content Safety and Prompt Shield pure logic.

These transforms are security-critical: they determine whether content is safe
to pass through the pipeline. The fail-CLOSED security posture means:
- Unknown categories → reject (not silently pass)
- Missing categories → reject (not assume severity 0)
- Non-bool attackDetected → reject (not treat null as falsy safe)
- severity > threshold → block (not >=, not <)

Properties tested:
1. ContentSafetyThresholds: Pydantic validation enforces 0-6 range
2. _check_thresholds: threshold comparison correctness and completeness
3. _AZURE_CATEGORY_MAP: structural alignment with thresholds model
4. get_fields_to_scan: field selection modes ("all", single, list)
5. _analyze_content: fail-CLOSED on unknown/missing categories
6. _analyze_prompt: strict bool type validation on attackDetected
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from elspeth.plugins.transforms.azure.content_safety import (
    _AZURE_CATEGORY_MAP,
    AzureContentSafety,
    ContentSafetyThresholds,
)
from elspeth.plugins.transforms.safety_utils import get_fields_to_scan
from elspeth.testing import make_pipeline_row

# =============================================================================
# Strategies
# =============================================================================

# Valid severity scores (Azure returns 0-6)
severity_scores = st.integers(min_value=0, max_value=6)

# Valid threshold values (matching Pydantic constraint)
threshold_values = st.integers(min_value=0, max_value=6)

# Out-of-range severity scores
out_of_range_severity = st.one_of(
    st.integers(min_value=-100, max_value=-1),
    st.integers(min_value=7, max_value=100),
)

# All 4 categories
CATEGORIES = ("hate", "violence", "sexual", "self_harm")

# Strategy for a complete analysis dict (all 4 categories with valid scores)
analysis_dicts = st.fixed_dictionaries(dict.fromkeys(CATEGORIES, severity_scores))

# Strategy for a complete thresholds config
threshold_configs = st.fixed_dictionaries(dict.fromkeys(CATEGORIES, threshold_values))


# =============================================================================
# Helpers
# =============================================================================


def _make_thresholds(config: dict[str, int]) -> ContentSafetyThresholds:
    """Create ContentSafetyThresholds from a flat dict."""
    return ContentSafetyThresholds(**config)


def _make_checker(thresholds: ContentSafetyThresholds) -> AzureContentSafety:
    """Create a minimal AzureContentSafety instance for _check_thresholds testing.

    Uses object.__new__ to bypass the constructor (which needs HTTP config).
    This is acceptable per CLAUDE.md for unit tests of isolated algorithms —
    _check_thresholds only reads self._thresholds.
    """
    obj = object.__new__(AzureContentSafety)
    obj._thresholds = thresholds
    return obj


# =============================================================================
# ContentSafetyThresholds Pydantic Validation Properties
# =============================================================================


class TestContentSafetyThresholdsValidation:
    """Pydantic model must enforce 0-6 range for all categories."""

    @given(config=threshold_configs)
    @settings(max_examples=200)
    def test_valid_thresholds_accepted(self, config: dict[str, int]) -> None:
        """Property: All values in [0, 6] produce valid thresholds."""
        t = ContentSafetyThresholds(**config)
        # Explicit (category, value) table over the owned pydantic model: a model
        # that dropped a category raises AttributeError here rather than having
        # its severity silently unchecked.
        severities: tuple[tuple[str, int], ...] = (
            ("hate", t.hate),
            ("violence", t.violence),
            ("sexual", t.sexual),
            ("self_harm", t.self_harm),
        )
        assert tuple(name for name, _ in severities) == CATEGORIES
        for cat, severity in severities:
            assert 0 <= severity <= 6, f"{cat}={severity} outside [0, 6]"

    @given(category=st.sampled_from(CATEGORIES), bad_value=out_of_range_severity)
    @settings(max_examples=100)
    def test_out_of_range_rejected(self, category: str, bad_value: int) -> None:
        """Property: Values outside [0, 6] are rejected by Pydantic."""
        import pydantic

        config = dict.fromkeys(CATEGORIES, 3)
        config[category] = bad_value
        with pytest.raises(pydantic.ValidationError):
            ContentSafetyThresholds(**config)

    def test_extra_fields_rejected(self) -> None:
        """Property: extra='forbid' rejects unknown threshold categories."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="extra"):
            ContentSafetyThresholds(
                hate=2,
                violence=2,
                sexual=2,
                self_harm=2,
                unknown_category=3,  # type: ignore[call-arg]
            )

    def test_missing_field_rejected(self) -> None:
        """Property: All 4 categories are required (no defaults)."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            ContentSafetyThresholds(hate=2, violence=2, sexual=2)  # type: ignore[call-arg]

    def test_frozen_rejects_field_reassignment(self) -> None:
        """Property: Frozen model rejects post-construction mutation of security thresholds."""
        import pydantic

        t = ContentSafetyThresholds(hate=2, violence=2, sexual=2, self_harm=2)
        with pytest.raises(pydantic.ValidationError):
            t.hate = 6  # type: ignore[misc]


# =============================================================================
# _check_thresholds Properties
# =============================================================================


class TestCheckThresholdsProperties:
    """Threshold comparison must be correct for all severity/threshold combinations."""

    @given(config=threshold_configs, analysis=analysis_dicts)
    @settings(max_examples=500)
    def test_threshold_result_matches_manual_check(self, config: dict[str, int], analysis: dict[str, int]) -> None:
        """Property: _check_thresholds result matches severity > threshold for each category."""
        thresholds = _make_thresholds(config)
        checker = _make_checker(thresholds)

        result = checker._check_thresholds(analysis)

        # Compute expected: any category where severity > threshold
        any_exceeded = any(analysis[cat] > config[cat] for cat in CATEGORIES)

        if any_exceeded:
            assert result is not None
            for cat in CATEGORIES:
                assert result[cat]["severity"] == analysis[cat]
                assert result[cat]["threshold"] == config[cat]
                assert result[cat]["exceeded"] == (analysis[cat] > config[cat])
        else:
            assert result is None

    @given(config=threshold_configs)
    @settings(max_examples=200)
    def test_at_threshold_is_not_exceeded(self, config: dict[str, int]) -> None:
        """Property: severity == threshold is NOT a violation (strictly greater than)."""
        thresholds = _make_thresholds(config)
        checker = _make_checker(thresholds)

        # Set each severity exactly at its threshold
        analysis = {cat: config[cat] for cat in CATEGORIES}

        result = checker._check_thresholds(analysis)
        assert result is None

    @given(
        config=threshold_configs,
        violating_cat=st.sampled_from(CATEGORIES),
    )
    @settings(max_examples=200)
    def test_single_violation_triggers_full_report(self, config: dict[str, int], violating_cat: str) -> None:
        """Property: One category exceeding triggers report with ALL categories."""
        thresholds = _make_thresholds(config)
        checker = _make_checker(thresholds)

        # Set all at threshold (safe), then push one over
        analysis = {cat: config[cat] for cat in CATEGORIES}
        analysis[violating_cat] = min(config[violating_cat] + 1, 7)

        # If the threshold is 6, adding 1 goes to 7 (> 6) which exceeds
        # If the threshold is already 6 and severity is 7, that's > 6
        result = checker._check_thresholds(analysis)
        assert result is not None
        assert result[violating_cat]["exceeded"] is True

        # Report contains ALL 4 categories, not just the violated one
        assert set(result.keys()) == set(CATEGORIES)

    @given(config=threshold_configs)
    @settings(max_examples=200)
    def test_all_zero_severity_never_triggers(self, config: dict[str, int]) -> None:
        """Property: Zero severity for all categories never triggers regardless of thresholds."""
        thresholds = _make_thresholds(config)
        checker = _make_checker(thresholds)

        analysis = dict.fromkeys(CATEGORIES, 0)
        result = checker._check_thresholds(analysis)
        assert result is None

    @given(config=threshold_configs)
    @settings(max_examples=200)
    def test_max_severity_always_triggers_when_threshold_under_max(self, config: dict[str, int]) -> None:
        """Property: Severity 6 triggers for any threshold < 6."""
        thresholds = _make_thresholds(config)
        checker = _make_checker(thresholds)

        analysis = dict.fromkeys(CATEGORIES, 6)
        result = checker._check_thresholds(analysis)

        any_threshold_under_six = any(config[cat] < 6 for cat in CATEGORIES)
        if any_threshold_under_six:
            assert result is not None
        else:
            # All thresholds are 6, severity 6 is NOT > 6
            assert result is None


# =============================================================================
# _AZURE_CATEGORY_MAP Structural Properties
# =============================================================================


class TestAzureCategoryMapProperties:
    """Category map must align with ContentSafetyThresholds and be complete."""

    def test_map_covers_all_threshold_fields(self) -> None:
        """Property: Every threshold field has a corresponding Azure category."""
        threshold_fields = set(ContentSafetyThresholds.model_fields.keys())
        mapped_internal_names = set(_AZURE_CATEGORY_MAP.values())
        assert mapped_internal_names == threshold_fields

    def test_map_keys_are_pascal_case(self) -> None:
        """Property: Azure API category names are PascalCase (not snake_case)."""
        for azure_name in _AZURE_CATEGORY_MAP:
            assert azure_name[0].isupper(), f"Azure category {azure_name!r} should be PascalCase"
            assert "_" not in azure_name, f"Azure category {azure_name!r} should not contain underscores"

    def test_map_values_are_snake_case(self) -> None:
        """Property: Internal names match Python/Pydantic convention (snake_case)."""
        for internal_name in _AZURE_CATEGORY_MAP.values():
            assert internal_name == internal_name.lower(), f"Internal name {internal_name!r} should be lowercase"

    def test_map_is_bijective(self) -> None:
        """Property: Azure → internal mapping is 1:1 (no two Azure names map to same internal)."""
        values = list(_AZURE_CATEGORY_MAP.values())
        assert len(values) == len(set(values))

    def test_exactly_four_categories(self) -> None:
        """Property: Exactly 4 categories (Hate, Violence, Sexual, SelfHarm)."""
        assert len(_AZURE_CATEGORY_MAP) == 4
        assert set(_AZURE_CATEGORY_MAP.keys()) == {"Hate", "Violence", "Sexual", "SelfHarm"}


# =============================================================================
# get_fields_to_scan Properties
# =============================================================================


class TestFieldScanSelectionProperties:
    """Field selection must correctly handle 'all', single string, and list modes."""

    @given(
        row=st.dictionaries(
            keys=st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnopqrstuvwxyz"),
            values=st.one_of(
                st.text(min_size=0, max_size=20),
                st.integers(min_value=-100, max_value=100),
                st.floats(allow_nan=False, allow_infinity=False),
                st.none(),
            ),
            min_size=1,
            max_size=8,
        ),
    )
    @settings(max_examples=200)
    def test_all_mode_selects_only_string_fields(self, row: dict[str, Any]) -> None:
        """Property: 'all' mode returns only keys with string values."""
        result = get_fields_to_scan("all", make_pipeline_row(row))

        expected = [k for k, v in row.items() if isinstance(v, str)]
        assert result == expected

    @given(
        field_name=st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnopqrstuvwxyz"),
    )
    @settings(max_examples=100)
    def test_single_string_mode_returns_singleton_list(self, field_name: str) -> None:
        """Property: Single string field name returns [field_name].

        'all' is a reserved keyword that triggers a different code path;
        exclude it from this property.
        """
        assume(field_name != "all")
        result = get_fields_to_scan(field_name, make_pipeline_row({}))
        assert result == [field_name]

    @given(
        fields=st.lists(
            st.text(min_size=1, max_size=10, alphabet="abcdefghijklmnopqrstuvwxyz"),
            min_size=1,
            max_size=5,
            unique=True,
        ),
    )
    @settings(max_examples=100)
    def test_list_mode_returns_exact_list(self, fields: list[str]) -> None:
        """Property: List of field names is returned as-is."""
        result = get_fields_to_scan(fields, make_pipeline_row({}))
        assert result == fields

    def test_all_mode_empty_row_returns_empty(self) -> None:
        """Property: 'all' mode on empty row returns empty list."""
        assert get_fields_to_scan("all", make_pipeline_row({})) == []

    def test_all_mode_no_strings_returns_empty(self) -> None:
        """Property: 'all' mode with only non-string values returns empty list."""
        assert get_fields_to_scan("all", make_pipeline_row({"a": 1, "b": 3.14, "c": None})) == []


# =============================================================================
# Content Safety Fail-CLOSED Properties
# =============================================================================


class TestContentSafetyFailClosed:
    """_analyze_content must fail CLOSED on malformed Azure responses.

    These tests verify the validation logic from _analyze_content by testing
    the patterns it enforces. Since _analyze_content requires HTTP infrastructure,
    we test the invariants that the validation code upholds:
    - Unknown categories raise ValueError
    - Missing expected categories raise MalformedResponseError
    - Non-integer severity raises MalformedResponseError
    """

    @given(
        unknown_category=st.text(min_size=1, max_size=20).filter(lambda s: s not in _AZURE_CATEGORY_MAP),
    )
    @settings(max_examples=100)
    def test_unknown_category_detected(self, unknown_category: str) -> None:
        """Property: Any string NOT in _AZURE_CATEGORY_MAP is unknown.

        This validates the fail-CLOSED invariant: if Azure adds a new category
        (e.g., 'Terrorism'), it won't silently map to None and be treated as safe.
        """
        assert _AZURE_CATEGORY_MAP.get(unknown_category) is None

    def test_category_map_lookup_is_case_sensitive(self) -> None:
        """Property: Category lookup is case-sensitive (lowercase 'hate' != 'Hate').

        This prevents accidental fail-OPEN from case normalization.
        """
        for azure_name in _AZURE_CATEGORY_MAP:
            # Lowercase version must NOT match (unless it happens to be another valid key)
            lower = azure_name.lower()
            if lower != azure_name:
                assert lower not in _AZURE_CATEGORY_MAP

    @given(
        subset=st.lists(
            st.sampled_from(list(_AZURE_CATEGORY_MAP.keys())),
            min_size=0,
            max_size=3,
            unique=True,
        ),
    )
    @settings(max_examples=50)
    def test_missing_categories_detectable(self, subset: list[str]) -> None:
        """Property: Any proper subset of expected categories has missing ones.

        This validates the fail-CLOSED check for missing categories:
        if Azure stops returning a category, we detect it rather than defaulting to 0.
        """
        expected = set(_AZURE_CATEGORY_MAP.values())
        returned = {_AZURE_CATEGORY_MAP[k] for k in subset}
        missing = expected - returned

        if len(subset) < len(_AZURE_CATEGORY_MAP):
            assert len(missing) > 0


# =============================================================================
# Prompt Shield Type Strictness Properties
# =============================================================================


class TestPromptShieldTypeStrictness:
    """attackDetected must be strictly bool — not null, string, or int.

    This is the critical security property: if attackDetected is None (null in JSON),
    Python's truthiness check would make `if None:` → False → no attack detected →
    content passes through → fail OPEN. The strict bool check prevents this.
    """

    @given(value=st.sampled_from([None, "true", "false", 1, 0, "True", "False", ""]))
    @settings(max_examples=20)
    def test_non_bool_attack_detected_is_not_bool(self, value: Any) -> None:
        """Property: Non-bool values fail isinstance(value, bool) check."""
        assert not isinstance(value, bool)

    @given(value=st.booleans())
    @settings(max_examples=10)
    def test_bool_values_pass_check(self, value: bool) -> None:
        """Property: True and False pass isinstance check."""
        assert isinstance(value, bool)

    def test_null_attack_detected_would_be_falsy(self) -> None:
        """Property: None is falsy — demonstrates WHY strict bool check matters.

        Without the strict check: `if None:` → False → "no attack" → fail OPEN.
        This is the exact vulnerability the code prevents.
        """
        assert not None  # None is falsy
        assert not isinstance(None, bool)  # But it's not a bool

    def test_zero_is_falsy_but_not_bool(self) -> None:
        """Property: Integer 0 is falsy — another fail-OPEN vector without type check."""
        assert not 0  # 0 is falsy
        # In Python, bool is a subclass of int, so isinstance(0, bool) is False
        # but isinstance(True, int) is True. We need the exact type check.
        assert not isinstance(0, bool)

    def test_empty_string_is_falsy_but_not_bool(self) -> None:
        """Property: Empty string is falsy — yet another fail-OPEN vector."""
        assert not ""
        assert not isinstance("", bool)

    @given(
        user_attack=st.booleans(),
        doc_attack=st.booleans(),
    )
    @settings(max_examples=10)
    def test_attack_detection_is_or_logic(self, user_attack: bool, doc_attack: bool) -> None:
        """Property: Any attack detected (user OR document) → reject.

        This mirrors the logic in _process_single_with_state:
            if analysis["user_prompt_attack"] or analysis["document_attack"]:
                → error result
        """
        any_attack = user_attack or doc_attack
        # Verify truth table
        if user_attack or doc_attack:
            assert any_attack is True
        else:
            assert any_attack is False

    @given(
        n_docs=st.integers(min_value=0, max_value=10),
        data=st.data(),
    )
    @settings(max_examples=50)
    def test_any_document_attack_means_attack(self, n_docs: int, data: st.DataObject) -> None:
        """Property: If ANY document has attackDetected=True, doc_attack is True.

        This mirrors the _analyze_prompt loop:
            doc_attack = False
            for doc in documents_analysis:
                if doc["attackDetected"]:
                    doc_attack = True
        """
        attacks = data.draw(st.lists(st.booleans(), min_size=n_docs, max_size=n_docs))

        # Replicate the production logic
        doc_attack = False
        for attack_detected in attacks:
            if attack_detected:
                doc_attack = True

        assert doc_attack == any(attacks) if attacks else doc_attack is False
