"""Regression tests for the P3 misc-transforms sweep findings (20261111-plugins-review).

Each class pins one finding; the finding number is in the class docstring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.keyword_filter import KeywordFilter
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context


class TestKeywordFilterEmptyLowerBoundBraceQuantifier:
    """#77: the ReDoS guard must treat ``{,n}`` as a quantifier, as ``re`` does."""

    @pytest.mark.parametrize("pattern", ["(a+){,100}", "^(a+){,40}$", "(?:a*){,5}", "(a+){,}"])
    def test_nested_repetition_with_empty_lower_bound_is_detected(self, pattern: str) -> None:
        from elspeth.plugins.transforms.keyword_filter import _nested_repetition_detected

        assert _nested_repetition_detected(pattern) is True

    def test_config_rejects_empty_lower_bound_nested_quantifier(self) -> None:
        from elspeth.plugins.transforms.keyword_filter import KeywordFilterConfig

        with pytest.raises(PluginConfigError, match="nested quantifiers"):
            KeywordFilterConfig.from_dict(
                {
                    "fields": ["x"],
                    "blocked_patterns": ["^(a+){,40}$"],
                    "schema": {"mode": "observed"},
                }
            )

    @pytest.mark.parametrize("pattern", ["a{,3}", "(ab){,3}", "x{,}", "(a+){}", "(a+){ ,3}"])
    def test_single_level_empty_lower_bound_is_not_nested(self, pattern: str) -> None:
        """Negative control: one level of ``{,n}`` is not nested, and ``{}`` / ``{ ,3}`` are literals in ``re``."""
        from elspeth.plugins.transforms.keyword_filter import _nested_repetition_detected

        assert _nested_repetition_detected(pattern) is False


class TestKeywordFilterSingleStringFieldDeclaration:
    """#78: a single named scan field gets the same static string declaration as the list form."""

    @staticmethod
    def _make(fields: str | list[str]) -> KeywordFilter:
        return KeywordFilter({"fields": fields, "blocked_patterns": ["secret"], "schema": {"mode": "observed"}})

    def test_single_string_field_is_declared_string_input(self) -> None:
        assert self._make("message").declared_string_input_fields == frozenset({"message"})

    def test_single_string_matches_list_form(self) -> None:
        assert self._make("message").declared_string_input_fields == self._make(["message"]).declared_string_input_fields

    def test_all_mode_makes_no_static_string_claim(self) -> None:
        """Negative control: 'all' skips non-strings at runtime, so it declares nothing."""
        assert self._make("all").declared_string_input_fields == frozenset()


class TestAllModeRecordsUnscannedFields:
    """#58: fields='all' must say in success_reason which non-string fields it did not scan."""

    def test_keyword_filter_all_mode_warns_about_nested_value(self) -> None:
        transform = KeywordFilter({"fields": "all", "blocked_patterns": ["secret"], "schema": {"mode": "observed"}})
        row = make_pipeline_row({"name": "ok", "payload": {"text": "secret"}, "tags": ["secret"]})

        result = transform.process(row, make_context())

        assert result.status == "success"
        assert result.success_reason is not None
        warnings = result.success_reason.get("validation_warnings", [])
        assert any("'payload'" in w and "dict" in w for w in warnings), warnings
        assert any("'tags'" in w and "list" in w for w in warnings), warnings
        # The warning names the field and its type, never the row content.
        assert not any("secret" in w for w in warnings), warnings

    def test_keyword_filter_all_strings_row_has_no_warning(self) -> None:
        """Negative control: a fully scanned row keeps the plain success_reason."""
        transform = KeywordFilter({"fields": "all", "blocked_patterns": ["secret"], "schema": {"mode": "observed"}})

        result = transform.process(make_pipeline_row({"name": "ok", "note": "fine"}), make_context())

        assert result.status == "success"
        assert result.success_reason == {"action": "filtered"}

    @pytest.mark.parametrize("plugin_name", ["content_safety", "prompt_shield"])
    def test_azure_safety_all_mode_warns_about_nested_value(self, plugin_name: str) -> None:
        from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety
        from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield

        options: dict[str, Any] = {
            "endpoint": "https://test.cognitiveservices.azure.com",
            "api_key": "test-key",
            "fields": "all",
            "schema": {"mode": "observed"},
        }
        transform: AzureContentSafety | AzurePromptShield
        if plugin_name == "content_safety":
            options["thresholds"] = {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2}
            transform = AzureContentSafety(options)
        else:
            transform = AzurePromptShield(options)
        row = make_pipeline_row({"name": "ok", "payload": {"text": "attack"}})

        try:
            with patch.object(transform, "_analyze_field_with_capacity_retry", return_value=None) as analyze:
                result = transform._process_single_with_state(row, "state-1", ctx=make_context())
            assert [c.args[1] for c in analyze.call_args_list] == ["name"]
        finally:
            transform.close()

        assert result.status == "success"
        assert result.success_reason is not None
        warnings = result.success_reason.get("validation_warnings", [])
        assert any("'payload'" in w and "dict" in w for w in warnings), warnings


class TestTypeCoerceStrictNumericGrammar:
    """#106: strict int/float parsing accepts only ASCII digits, no PEP 515 underscores."""

    @pytest.mark.parametrize("raw", ["1_000", " 1_0 ", "12_34", "\uff11\uff12\uff13", "\u0664\u0662", "-1_0"])
    def test_int_rejects_underscores_and_non_ascii_digits(self, raw: str) -> None:
        from elspeth.plugins.transforms.type_coerce import CoercionError, coerce_to_int

        with pytest.raises(CoercionError) as exc_info:
            coerce_to_int(raw)
        assert exc_info.value.failure == "not_integer_string"

    @pytest.mark.parametrize("raw", ["1_000.5", "1_0e3", "\uff11.\uff15", "\u0664.\u0662"])
    def test_float_rejects_underscores_and_non_ascii_digits(self, raw: str) -> None:
        from elspeth.plugins.transforms.type_coerce import CoercionError, coerce_to_float

        with pytest.raises(CoercionError) as exc_info:
            coerce_to_float(raw)
        assert exc_info.value.failure == "not_numeric_string"

    @pytest.mark.parametrize(("raw", "expected"), [("42", 42), (" -7 ", -7), ("+5", 5), ("007", 7)])
    def test_int_still_accepts_plain_ascii_integers(self, raw: str, expected: int) -> None:
        """Negative control: the ordinary grammar is unchanged."""
        from elspeth.plugins.transforms.type_coerce import coerce_to_int

        assert coerce_to_int(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), [("12.5", 12.5), ("1e3", 1000.0), (" -0.5 ", -0.5), (".5", 0.5), ("1E-3", 0.001)])
    def test_float_still_accepts_plain_ascii_numbers(self, raw: str, expected: float) -> None:
        """Negative control: the ordinary grammar is unchanged."""
        from elspeth.plugins.transforms.type_coerce import coerce_to_float

        assert coerce_to_float(raw) == expected
