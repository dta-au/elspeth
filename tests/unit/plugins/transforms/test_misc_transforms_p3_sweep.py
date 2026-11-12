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


_SAFE_INT_MAX = 2**53 - 1


class TestTypeCoerceIntSafeRange:
    """#109: coerce_to_int refuses integers canonical JSON cannot carry, as a type_coerce failure."""

    @pytest.mark.parametrize(
        "raw",
        [str(_SAFE_INT_MAX + 1), str(-_SAFE_INT_MAX - 1), "99999999999999999999", 1e20, -(2.0**60), _SAFE_INT_MAX + 1],
    )
    def test_out_of_safe_range_is_rejected(self, raw: object) -> None:
        from elspeth.plugins.transforms.type_coerce import CoercionError, coerce_to_int

        with pytest.raises(CoercionError) as exc_info:
            coerce_to_int(raw)
        assert exc_info.value.failure == "out_of_range"
        # The value is row data: it never reaches the message.
        assert str(raw) not in str(exc_info.value)

    @pytest.mark.parametrize("raw", [str(_SAFE_INT_MAX), str(-_SAFE_INT_MAX), float(2**52), _SAFE_INT_MAX])
    def test_safe_range_boundary_is_accepted(self, raw: object) -> None:
        """Negative control: the boundary itself is still a valid int."""
        from elspeth.plugins.transforms.type_coerce import coerce_to_int

        assert abs(coerce_to_int(raw)) <= _SAFE_INT_MAX

    def test_transform_routes_out_of_range_as_type_mismatch(self) -> None:
        from elspeth.plugins.transforms.type_coerce import TypeCoerce

        transform = TypeCoerce({"schema": {"mode": "observed"}, "conversions": [{"field": "n", "to": "int"}]})

        result = transform.process(make_pipeline_row({"n": "9007199254740993"}), make_context())

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "type_mismatch"
        assert result.reason["error_type"] == "out_of_range"


def _retrieval_output_config(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"output_prefix": "policy", "query_field": "question", "schema_config": {"mode": "observed"}}
    base.update(overrides)
    return base


class TestRetrievalQueryModesUsePresenceNotTruthiness:
    """#96: query_template and query_pattern are exclusive by presence, and neither may be empty."""

    @pytest.mark.parametrize(("template", "pattern"), [("", r"(\w+)"), ("{{ query }}", ""), ("", "")])
    def test_both_options_present_is_rejected_even_when_one_is_empty(self, template: str, pattern: str) -> None:
        from pydantic import ValidationError

        from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig

        with pytest.raises(ValidationError, match=r"query_template|query_pattern"):
            RetrievalOutputConfig(**_retrieval_output_config(query_template=template, query_pattern=pattern))

    @pytest.mark.parametrize("option", ["query_template", "query_pattern"])
    def test_empty_option_alone_is_rejected(self, option: str) -> None:
        from pydantic import ValidationError

        from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig

        with pytest.raises(ValidationError, match=f"{option} cannot be empty"):
            RetrievalOutputConfig(**_retrieval_output_config(**{option: ""}))

    @pytest.mark.parametrize("overrides", [{}, {"query_template": "{{ query }}"}, {"query_pattern": r"issue:\s*(.+)"}])
    def test_single_non_empty_mode_is_accepted(self, overrides: dict[str, str]) -> None:
        """Negative control: field-only, template-only and pattern-only configs still build."""
        from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig

        cfg = RetrievalOutputConfig(**_retrieval_output_config(**overrides))
        assert cfg.query_field == "question"


_CHROMA_PROVIDER_MODULE = "elspeth.plugins.infrastructure.clients.retrieval.chroma"


def _fail_chroma_provider_import(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """Make importing the chroma provider module raise ModuleNotFoundError(name=missing)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == _CHROMA_PROVIDER_MODULE:
            raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


class TestRagProviderRegistryBrokenInstall:
    """#142: only an absent chromadb means 'not installed'; a missing chromadb submodule is a broken install."""

    def test_missing_chromadb_submodule_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.plugins.transforms.rag.config import _get_providers

        _fail_chroma_provider_import(monkeypatch, "chromadb.api")

        with pytest.raises(ModuleNotFoundError) as exc_info:
            _get_providers()
        assert exc_info.value.name == "chromadb.api"

    def test_absent_chromadb_leaves_provider_unregistered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative control: the documented optional-extra state still reads as 'not installed'."""
        from elspeth.plugins.transforms.rag.config import _get_providers

        _fail_chroma_provider_import(monkeypatch, "chromadb")

        assert "chroma" not in _get_providers()

    def test_unregistered_known_provider_names_the_rag_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from elspeth.plugins.transforms.rag import config as rag_config

        monkeypatch.setattr(rag_config, "PROVIDERS", {})

        with pytest.raises(ValidationError, match=r"elspeth\[rag\]"):
            rag_config.RAGRetrievalConfig(
                output_prefix="policy",
                query_field="question",
                provider="chroma",
                provider_config={"collection": "test-index", "mode": "ephemeral"},
                schema_config={"mode": "observed"},
            )
