"""Exact immutable analysis may be reused; source admission still runs per call."""

from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from elspeth.core import templates


@pytest.fixture(autouse=True)
def clear_analysis_caches():
    templates._validate_bounded_jinja_source.cache_clear()
    templates._cached_jinja2_field_usage.cache_clear()
    templates._cached_jinja2_context_fields.cache_clear()
    yield
    templates._validate_bounded_jinja_source.cache_clear()
    templates._cached_jinja2_field_usage.cache_clear()
    templates._cached_jinja2_context_fields.cache_clear()


def test_repeated_analysis_reuses_immutable_results_without_reparsing(monkeypatch):
    factory = Mock(wraps=templates._create_field_extraction_environment)
    monkeypatch.setattr(templates, "_create_field_extraction_environment", factory)
    text = "{{ row.name }} {{ row[key] }}"
    first = templates.extract_jinja2_field_usage(text)
    second = templates.extract_jinja2_field_usage(text)
    assert second is first
    assert first.fields == frozenset({"name"})
    assert first.has_dynamic_access
    assert factory.call_count == 1
    with pytest.raises(FrozenInstanceError):
        first.fields = frozenset({"corrupted"})


def test_text_namespace_and_nested_row_analysis_have_distinct_cache_keys():
    text = "{{ row.direct }} {{ row.source_row.nested }} {{ other.foreign }}"
    assert templates.extract_jinja2_field_usage(text).fields == frozenset({"direct", "source_row"})
    assert templates.extract_jinja2_field_usage(text, "other").fields == frozenset({"foreign"})
    assert templates.extract_jinja2_field_usage(text, row_attribute="source_row").fields == frozenset({"nested"})
    assert templates.extract_jinja2_context_fields(text, row_attribute="source_row") == frozenset({"direct"})
    assert templates.extract_jinja2_field_usage("{{ row.changed }}").fields == frozenset({"changed"})
    assert templates.extract_jinja2_field_usage(text).fields == frozenset({"direct", "source_row"})
    assert templates._cached_jinja2_field_usage.cache_info().misses == 4
    assert templates._cached_jinja2_field_usage.cache_info().hits == 1


def test_cached_results_never_skip_current_source_size_admission(monkeypatch):
    text = "{{ row.name }}"
    templates.extract_jinja2_field_usage(text)
    templates.extract_jinja2_context_fields(text, row_attribute="source_row")
    monkeypatch.setattr(templates, "MAX_JINJA_TEMPLATE_BYTES", 4)
    with pytest.raises(ValueError, match="Template exceeds"):
        templates.extract_jinja2_field_usage(text)
    with pytest.raises(ValueError, match="Template exceeds"):
        templates.extract_jinja2_context_fields(text, row_attribute="source_row")


def test_both_caches_evict_old_entries():
    for index in range(129):
        text = "{{ row.field_" + str(index) + " }}"
        templates.extract_jinja2_field_usage(text)
        templates.extract_jinja2_context_fields(text, row_attribute="source_row")
    assert templates._cached_jinja2_field_usage.cache_info().currsize == 128
    assert templates._cached_jinja2_context_fields.cache_info().currsize == 128


def test_source_guard_reuses_exact_text_but_rejects_new_resource_violations():
    text = "{{ row.name }}"
    templates.validate_jinja_source(text)
    templates.validate_jinja_source(text)
    assert templates._validate_bounded_jinja_source.cache_info().hits == 1

    for changed in (text * 257, "{% if true %}{% endif %}" * 33):
        with pytest.raises(ValueError, match="too many Jinja"):
            templates.validate_jinja_source(changed)
    with pytest.raises(ValueError, match="nesting exceeds"):
        templates.validate_jinja_source("(" * 65 + "x" + ")" * 65)
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        templates.validate_jinja_source("é" * 8193)
    assert templates._validate_bounded_jinja_source.cache_info().currsize == 1


def test_source_guard_cache_retention_is_bounded():
    for index in range(129):
        templates.validate_jinja_source(f"bounded text {index}")
    assert templates._validate_bounded_jinja_source.cache_info().currsize == 128
