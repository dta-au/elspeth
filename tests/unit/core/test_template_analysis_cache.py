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


def test_context_analysis_cache_keys_on_namespace_and_row_attribute():
    text = "{{ row.direct }} {{ row.source_row.nested }} {{ row.alternate.other }} {{ other.foreign }}"
    assert templates.extract_jinja2_context_fields(text, row_attribute="source_row") == frozenset({"direct", "alternate"})
    # Same text and row attribute, different namespace: a key without the namespace returns the row answer.
    assert templates.extract_jinja2_context_fields(text, "other", row_attribute="source_row") == frozenset({"foreign"})
    # Same text and namespace, different row attribute: a key without it returns the source_row answer.
    assert templates.extract_jinja2_context_fields(text, row_attribute="alternate") == frozenset({"direct", "source_row"})
    assert templates.extract_jinja2_context_fields(text + " {{ row.new }}", row_attribute="source_row") == frozenset(
        {"direct", "alternate", "new"}
    )
    assert templates.extract_jinja2_context_fields(text, row_attribute="source_row") == frozenset({"direct", "alternate"})
    assert templates._cached_jinja2_context_fields.cache_info().misses == 4
    assert templates._cached_jinja2_context_fields.cache_info().hits == 1


@pytest.mark.parametrize("wrapper", ["field", "context"])
@pytest.mark.parametrize(
    ("text", "limit"),
    [
        pytest.param("abcdef", 4, id="ascii-over-chars-and-bytes"),
        pytest.param("ééé", 4, id="multibyte-under-chars-over-bytes"),
    ],
)
def test_source_admission_runs_before_any_analysis_cache_lookup(monkeypatch, wrapper, text, limit):
    if wrapper == "field":
        cache = templates._cached_jinja2_field_usage

        def analyse() -> object:
            return templates.extract_jinja2_field_usage(text)
    else:
        cache = templates._cached_jinja2_context_fields

        def analyse() -> object:
            return templates.extract_jinja2_context_fields(text, row_attribute="source_row")

    analyse()
    warm = cache.cache_info()
    assert warm.currsize == 1
    monkeypatch.setattr(templates, "MAX_JINJA_TEMPLATE_BYTES", limit)
    with pytest.raises(ValueError, match="Template exceeds"):
        analyse()
    # A warm entry exists for this exact key, so any lookup before admission would count a hit.
    assert cache.cache_info() == warm


def test_all_three_caches_share_the_sized_entry_bound():
    assert templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES == 8192
    for cache in (templates._validate_bounded_jinja_source, templates._cached_jinja2_field_usage, templates._cached_jinja2_context_fields):
        assert cache.cache_info().maxsize == templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES


def test_a_repeated_walk_larger_than_the_old_bound_hits_on_its_second_pass():
    """Validation walks a graph's templates in the same order each time; an LRU smaller than the walk never hits."""
    walk = [f"Query {index}: classify {{{{ row.customer_{index} }}}} by {{{{ row.source_row.region }}}}." for index in range(400)]
    for _ in range(2):
        for text in walk:
            templates.extract_jinja2_field_usage(text)
            templates.extract_jinja2_field_usage(text, row_attribute="source_row")
            templates.extract_jinja2_context_fields(text, row_attribute="source_row")
    field = templates._cached_jinja2_field_usage.cache_info()
    context = templates._cached_jinja2_context_fields.cache_info()
    source = templates._validate_bounded_jinja_source.cache_info()
    assert (field.misses, field.hits) == (800, 800)
    assert (context.misses, context.hits) == (400, 400)
    # validate_jinja_source runs on all three calls per text; only the first per text misses.
    assert (source.misses, source.hits) == (400, 2000)


def test_both_caches_evict_old_entries():
    entries = templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES
    for index in range(entries + 1):
        text = "{{ row.field_" + str(index) + " }}"
        templates.extract_jinja2_field_usage(text)
        templates.extract_jinja2_context_fields(text, row_attribute="source_row")
    assert templates._cached_jinja2_field_usage.cache_info().currsize == entries
    assert templates._cached_jinja2_context_fields.cache_info().currsize == entries


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
    entries = templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES
    for index in range(entries + 1):
        templates.validate_jinja_source(f"bounded text {index}")
    assert templates._validate_bounded_jinja_source.cache_info().currsize == entries
