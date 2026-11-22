"""Exact immutable analysis may be reused; source admission still runs per call."""

import gc
import random
import sys
import threading
import tracemalloc
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest
from jinja2 import TemplateSyntaxError

from elspeth.core import templates
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP
from elspeth.core.config import MAX_IN_MEMORY_PIPELINE_YAML_BYTES


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
        templates.validate_jinja_source("é" * 32769)
    assert templates._validate_bounded_jinja_source.cache_info().currsize == 1


def test_source_guard_cache_retention_is_bounded():
    entries = templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES
    for index in range(entries + 1):
        templates.validate_jinja_source(f"bounded text {index}")
    assert templates._validate_bounded_jinja_source.cache_info().currsize == entries


# --- Byte budget -------------------------------------------------------------------------------------------------

ASTRAL = "\U0001f600"  # one character outside the BMP: CPython then stores the whole text at 4 bytes per character


def _prose(size: int, index: int, extra: str = "") -> str:
    """A distinct template of at most ``size`` UTF-8 bytes that reads two row fields."""
    head = f"Query {index}{extra}: classify {{{{ row.customer_{index} }}}} by {{{{ row.source_row.region }}}}. "
    filler = "The analyst reads the record and writes one line of plain prose. "
    text = (head + filler * (size // len(filler) + 1))[:size]
    while len(text.encode("utf-8")) > size:
        text = text[:-1]
    return text


def _wide(size: int, index: int) -> str:
    """One expression naming as many distinct fields as fit in ``size`` bytes: its field set outweighs its text."""
    names: list[str] = []
    length = len("{{  }}")
    while length < size - 32:
        names.append(f"row.w{index}_{len(names)}")
        length += len(names[-1]) + len(" ~ ")
    return "{{ " + " ~ ".join(names) + " }}"


def _walk(texts: list[str]) -> None:
    """One validation walk: each text analysed as columns with and without a nested row, and as context."""
    for text in texts:
        templates.extract_jinja2_field_usage(text)
        templates.extract_jinja2_field_usage(text, row_attribute="source_row")
        templates.extract_jinja2_context_fields(text, row_attribute="source_row")


def _ledger_total(budget: templates._TemplateAnalysisBudget) -> int:
    return sum(entry.cost for entry in budget.order) + sum(record.size for record in budget.held.values())


def _isolated(budget_bytes: int, *, max_entries: int = templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES):
    """Fresh caches over the real analyses, sharing a budget of their own."""
    budget = templates._TemplateAnalysisBudget(budget_bytes)
    validate = templates._template_analysis_cache(templates._no_value_bytes, budget=budget, max_entries=max_entries)(
        templates._validate_bounded_jinja_source.__wrapped__
    )
    field = templates._template_analysis_cache(templates._field_usage_bytes, budget=budget, max_entries=max_entries)(
        templates._cached_jinja2_field_usage.__wrapped__
    )
    context = templates._template_analysis_cache(templates._name_set_bytes, budget=budget, max_entries=max_entries)(
        templates._cached_jinja2_context_fields.__wrapped__
    )
    return budget, validate, field, context


_MODULE_CACHES = (templates._validate_bounded_jinja_source, templates._cached_jinja2_field_usage, templates._cached_jinja2_context_fields)


def test_all_three_caches_report_one_shared_byte_budget():
    assert {cache.cache_info().maxbytes for cache in _MODULE_CACHES} == {templates._TEMPLATE_ANALYSIS_BUDGET_BYTES}
    templates.extract_jinja2_field_usage("{{ row.name }}")
    shared = _ledger_total(templates._TEMPLATE_ANALYSIS_BUDGET)
    assert shared > 0
    assert [cache.cache_info().currbytes for cache in _MODULE_CACHES] == [shared, shared, shared]


def test_a_walk_of_400_templates_of_8_kib_hits_on_every_second_pass_call():
    assert templates.MAX_JINJA_TEMPLATE_BYTES >= 8192
    walk = [_prose(8192, index) for index in range(400)]
    _walk(walk)
    _walk(walk)
    field = templates._cached_jinja2_field_usage.cache_info()
    context = templates._cached_jinja2_context_fields.cache_info()
    source = templates._validate_bounded_jinja_source.cache_info()
    assert (field.misses, field.hits) == (800, 800)
    assert (context.misses, context.hits) == (400, 400)
    assert (source.misses, source.hits) == (400, 2000)
    assert field.currbytes <= field.maxbytes


@pytest.mark.parametrize("extra", [pytest.param("", id="ascii"), pytest.param(ASTRAL, id="four-bytes-per-character")])
def test_a_walk_of_the_largest_file_text_at_the_size_limit_hits_on_its_second_pass(extra):
    """The aggregate file text, cut into templates at the size limit: 64 x 16 KiB today, 64 x 64 KiB at 4 MiB / 64 KiB."""
    size = templates.MAX_JINJA_TEMPLATE_BYTES
    count = BLOB_INLINE_AGGREGATE_BYTE_CAP // size
    walk = [_prose(size, index, extra) for index in range(count)]
    _walk(walk)
    _walk(walk)
    field = templates._cached_jinja2_field_usage.cache_info()
    assert (field.misses, field.hits) == (2 * count, 2 * count)
    assert templates._cached_jinja2_context_fields.cache_info().hits == count
    # The budget counts process bytes: with one astral character every text is stored, and counted, at 4 bytes per character.
    characters = sum(len(text) for text in walk)
    assert field.currbytes >= (4 if extra else 1) * characters
    assert field.currbytes <= field.maxbytes


def test_the_shared_budget_holds_two_worst_case_walks_of_the_largest_pipeline():
    """Sizing evidence for the constant, read from the live caps so it follows them when they move.

    A pipeline's template text is at most its file text plus its inline YAML, stored at up to 4 bytes per
    character and counted once; each of the three caches may hold its full entry bound of ordinary entries
    (bookkeeping plus a field set of a few names, ~0.75 KiB measured).
    """
    worst_text = 4 * (BLOB_INLINE_AGGREGATE_BYTE_CAP + MAX_IN_MEMORY_PIPELINE_YAML_BYTES)
    ordinary_entry = templates._TEMPLATE_ANALYSIS_ENTRY_OVERHEAD_BYTES + 256
    worst_walk = worst_text + 3 * templates._TEMPLATE_ANALYSIS_CACHE_ENTRIES * ordinary_entry
    assert 2 * worst_walk <= templates._TEMPLATE_ANALYSIS_BUDGET_BYTES
    assert templates._TEMPLATE_ANALYSIS_BUDGET.max_bytes == templates._TEMPLATE_ANALYSIS_BUDGET_BYTES


def test_the_byte_budget_is_never_exceeded_and_evicts_the_least_recently_used():
    budget_bytes = 400_000
    budget, validate, field, context = _isolated(budget_bytes)
    hot = _prose(8192, 10_000)
    inserted: list[str] = []
    total_text = 0
    index = 0
    while total_text < 3 * budget_bytes:
        text = _prose(8192, index)
        validate(text)
        field(text, "row", None)
        context(text, "row", "source_row")
        # A text in steady use is the most recently used after every insert, whatever its age.
        field(hot, "row", None)
        inserted.append(text)
        total_text += sys.getsizeof(text)
        index += 1
        info = field.cache_info()
        assert info.currbytes <= budget_bytes
        assert info.currbytes == _ledger_total(budget)
    assert len(budget.order) == sum(cache.cache_info().currsize for cache in (validate, field, context))
    before = field.cache_info()
    # Under LRU the text in steady use missed once, on first sight; FIFO evicts and re-admits it as it ages.
    assert before.misses == len(inserted) + 1
    field(hot, "row", None)
    field(inserted[-1], "row", None)
    assert field.cache_info().hits == before.hits + 2
    field(inserted[0], "row", None)
    assert field.cache_info().misses == before.misses + 1


def test_the_entry_bound_still_holds_for_tiny_templates_and_evicts_the_least_recently_used():
    budget, _validate, field, _context = _isolated(templates._TEMPLATE_ANALYSIS_BUDGET_BYTES, max_entries=8)
    for index in range(20):
        field("{{ row.field_" + str(index) + " }}", "row", None)
        field("{{ row.hot }}", "row", None)
    info = field.cache_info()
    assert info.currsize == 8
    assert len(budget.order) == 8
    # Under LRU the text in steady use missed once, on first sight; FIFO evicts and re-admits it as it ages.
    assert info.misses == 20 + 1
    field("{{ row.hot }}", "row", None)
    field("{{ row.field_19 }}", "row", None)
    assert field.cache_info().hits == info.hits + 2
    field("{{ row.field_0 }}", "row", None)
    assert field.cache_info().misses == info.misses + 1


def test_an_entry_larger_than_the_whole_budget_is_computed_but_never_retained():
    budget, _validate, field, _context = _isolated(20_000)
    small = "{{ row.small }}"
    field(small, "row", None)
    retained = field.cache_info()
    assert retained.currsize == 1
    big = _prose(8192, 1, ASTRAL)  # ~32 KiB in memory: larger than the budget on its own
    assert field(big, "row", None) == templates._cached_jinja2_field_usage.__wrapped__(big, "row", None)
    assert field(big, "row", None).fields == frozenset({"customer_1", "source_row"})
    info = field.cache_info()
    assert (info.currsize, info.currbytes, info.misses) == (retained.currsize, retained.currbytes, retained.misses + 2)
    assert id(big) not in budget.held
    # The resident entry was not evicted to make room for an entry that could never fit.
    field(small, "row", None)
    assert field.cache_info().hits == retained.hits + 1


def test_values_are_counted_so_a_wide_template_costs_several_times_its_text():
    text = _wide(templates.MAX_JINJA_TEMPLATE_BYTES, 0)
    before = templates._cached_jinja2_field_usage.cache_info().currbytes
    usage = templates.extract_jinja2_field_usage(text)
    added = templates._cached_jinja2_field_usage.cache_info().currbytes - before
    assert len(usage.fields) > 500
    assert added >= sys.getsizeof(text) + templates._field_usage_bytes(usage)
    assert added > 5 * sys.getsizeof(text)


def test_one_text_is_counted_once_however_many_caches_and_keys_hold_it():
    text = _prose(8192, 0)
    _walk([text])
    budget = templates._TEMPLATE_ANALYSIS_BUDGET
    record = budget.held[id(text)]
    assert record.users == 4  # source check, columns with and without a nested row, context
    assert record.size == sys.getsizeof(text) + templates._TEMPLATE_ANALYSIS_HELD_OBJECT_OVERHEAD_BYTES
    currbytes = templates._cached_jinja2_field_usage.cache_info().currbytes
    assert currbytes == _ledger_total(budget)
    assert currbytes < 2 * sys.getsizeof(text)
    # An equal text in a distinct object hits and adds nothing; as a new key it is separate memory, counted again.
    copy = "".join(list(text))
    assert copy == text and copy is not text
    templates.extract_jinja2_field_usage(copy)
    assert templates._cached_jinja2_field_usage.cache_info().currbytes == currbytes
    templates.extract_jinja2_field_usage(copy, "other")
    assert budget.held[id(copy)].users == 1
    # Clearing two caches leaves the text held by the third; clearing all three releases everything.
    templates._cached_jinja2_field_usage.cache_clear()
    templates._cached_jinja2_context_fields.cache_clear()
    assert budget.held[id(text)].users == 1
    assert id(copy) not in budget.held
    assert templates._validate_bounded_jinja_source.cache_info().currbytes == _ledger_total(budget)
    templates._validate_bounded_jinja_source.cache_clear()
    assert (templates._validate_bounded_jinja_source.cache_info().currbytes, len(budget.held), len(budget.order)) == (0, 0, 0)


def test_the_byte_estimate_is_not_below_what_the_caches_really_retain():
    """tracemalloc control: the caches are the texts' only holders once the walk drops them."""
    warm = _prose(8192, 10**6)
    _walk([warm])
    for cache in _MODULE_CACHES:
        cache.cache_clear()
    del warm
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    try:
        gc.collect()
        start = tracemalloc.get_traced_memory()[0]
        corpus = [_prose(8192, index) for index in range(200)] + [_prose(8192, index, ASTRAL) for index in range(200, 300)]
        corpus += [_wide(4096, index) for index in range(20)]
        _walk(corpus)
        del corpus
        gc.collect()
        measured = tracemalloc.get_traced_memory()[0] - start
    finally:
        if not already_tracing:
            tracemalloc.stop()
    estimate = templates._cached_jinja2_field_usage.cache_info().currbytes
    # ~40 KiB of interpreter-held attribute names (Jinja's visitor builds ``visit_<Node>`` per node) is not the caches'.
    assert estimate + 64 * 1024 >= measured
    assert estimate <= 1.5 * measured


def test_concurrent_callers_get_the_serial_results_and_consistent_counters():
    budget_bytes = 300_000  # small enough that threads evict each other's entries throughout
    budget, validate, field, context = _isolated(budget_bytes)
    corpus = [_prose(2048, index) for index in range(40)] + [_prose(2048, index, ASTRAL) for index in range(40, 60)]
    corpus += ["{{ row.a }} {{ row[key] }}", "{{ row.source_row.get('x') }} {{ row.keys() }}"]
    wrapped = (validate.__wrapped__, field.__wrapped__, context.__wrapped__)
    expected = {
        text: (
            wrapped[0](text),
            wrapped[1](text, "row", None),
            wrapped[1](text, "row", "source_row"),
            wrapped[2](text, "row", "source_row"),
        )
        for text in corpus
    }
    threads_count, rounds = 8, 4
    barrier = threading.Barrier(threads_count)
    failures: list[BaseException] = []
    mismatches: list[str] = []

    def worker(seed: int) -> None:
        order = list(corpus)
        random.Random(seed).shuffle(order)
        try:
            barrier.wait()
            for _ in range(rounds):
                for text in order:
                    got = (validate(text), field(text, "row", None), field(text, "row", "source_row"), context(text, "row", "source_row"))
                    if got != expected[text]:
                        mismatches.append(text)
                    if field.cache_info().currbytes > budget_bytes:
                        mismatches.append("over budget")
        except Exception as error:  # reported by the ``failures == []`` assertion on the main thread
            failures.append(error)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert failures == []
    assert mismatches == []
    calls = threads_count * rounds * len(corpus)
    for cache, per_text in ((validate, 1), (field, 2), (context, 1)):
        info = cache.cache_info()
        assert info.hits + info.misses == per_text * calls
        assert info.misses > 0 and info.hits > 0
    assert budget.retained_bytes <= budget_bytes
    assert budget.retained_bytes == _ledger_total(budget)
    assert len(budget.order) == sum(cache.cache_info().currsize for cache in (validate, field, context))
    users: dict[int, int] = {}
    for entry in budget.order:
        for key_object in entry.key:
            users[id(key_object)] = users.get(id(key_object), 0) + 1
    assert users == {key: record.users for key, record in budget.held.items()}


_DIFFERENTIAL_CORPUS = [
    "{{ row.name }}",
    "{{ row['Original Name'] }} {{ row.get('x', 1) }}",
    "{{ row[key] }} {{ row.get(name) }}",
    "{{ row|attr(name) }} {{ items|map(attribute=name)|list }}",
    "{% for r in [row] %}{{ r.alias }}{% endfor %}",
    "{% macro m(x) %}{{ x.inner }}{% endmacro %}{{ m(row) }}",
    "{% set a = {'k': row} %}{{ a.k.carried }}",
    "{{ row.keys() }} {{ row.contract }} {{ row.get }}",
    "{{ row.source_row.nested }} {{ row['source_row']['other'] }} {{ row.direct }}",
    "{{ other.foreign }} {{ row.local }}",
    "plain text, no expressions",
    "Query " + ASTRAL + " {{ row.astral }}",
]
_RAISING_CORPUS = [
    pytest.param("{{ row.name ", TemplateSyntaxError, id="syntax"),
    pytest.param("{{ x }}" * 257, ValueError, id="too-many-expressions"),
    pytest.param("(" * 65 + "x" + ")" * 65, ValueError, id="nesting"),
]


@pytest.mark.parametrize("text", _DIFFERENTIAL_CORPUS)
@pytest.mark.parametrize(("namespace", "row_attribute"), [("row", None), ("row", "source_row"), ("other", None)])
def test_cached_analysis_matches_the_uncached_analysis(text, namespace, row_attribute):
    uncached = templates._cached_jinja2_field_usage.__wrapped__(text, namespace, row_attribute)
    assert templates.extract_jinja2_field_usage(text, namespace, row_attribute=row_attribute) == uncached
    assert templates.extract_jinja2_field_usage(text, namespace, row_attribute=row_attribute) == uncached  # the hit
    if row_attribute is not None:
        context = templates._cached_jinja2_context_fields.__wrapped__(text, namespace, row_attribute)
        assert templates.extract_jinja2_context_fields(text, namespace, row_attribute=row_attribute) == context
        assert templates.extract_jinja2_context_fields(text, namespace, row_attribute=row_attribute) == context
    assert templates._cached_jinja2_field_usage.cache_info().hits == 1


@pytest.mark.parametrize(("text", "error"), _RAISING_CORPUS)
def test_a_raising_analysis_raises_the_same_error_on_every_call_and_is_never_cached(text, error):
    with pytest.raises(error) as uncached:
        if error is TemplateSyntaxError:
            templates._cached_jinja2_field_usage.__wrapped__(text, "row", None)
        else:
            templates._validate_bounded_jinja_source.__wrapped__(text)
    for attempt in (1, 2):
        with pytest.raises(error) as cached:
            templates.extract_jinja2_field_usage(text)
        assert str(cached.value) == str(uncached.value)
        raised_in = templates._cached_jinja2_field_usage if error is TemplateSyntaxError else templates._validate_bounded_jinja_source
        info = raised_in.cache_info()
        assert (info.misses, info.hits, info.currsize) == (attempt, 0, 0)
