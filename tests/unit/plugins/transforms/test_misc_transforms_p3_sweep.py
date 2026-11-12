"""Regression tests for the P3 misc-transforms sweep findings (20261111-plugins-review).

Each class pins one finding; the finding number is in the class docstring.
"""

from __future__ import annotations

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError


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
