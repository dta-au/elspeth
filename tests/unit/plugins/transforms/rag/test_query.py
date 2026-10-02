"""Tests for RAG query construction."""

import os
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import pytest

from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.templates import ALL_FIELDS, TemplateError
from elspeth.plugins.transforms.rag import query as rag_query
from elspeth.plugins.transforms.rag.query import QueryBuilder


def _row(data: dict[str, Any]) -> PipelineRow:
    """A real PipelineRow, as ``RetrievalTransformBase.process`` hands ``build``."""
    return PipelineRow(data, SchemaContract(mode="OBSERVED", fields=()))


def _builder(query_field: str, **kwargs: Any) -> QueryBuilder:
    """A QueryBuilder whose template, if any, sees the whole row (``[]``).

    What a declared template sees is pinned in ``TestTemplateRowProjection``.
    """
    return QueryBuilder(query_field, row_projection=ALL_FIELDS, **kwargs)


class _RegexPoolFake:
    def __init__(self, future: Future):
        self._future = future

    def submit(self, *_args: object, **_kwargs: object) -> Future:
        return self._future

    def shutdown(self, *_args: object, **_kwargs: object) -> None:
        pass


def _replace_regex_pool(builder: QueryBuilder, future: Future) -> None:
    assert builder._regex_pool is not None
    builder._regex_pool.shutdown(wait=False)
    builder._regex_pool = _RegexPoolFake(future)


class _InlineRegexPool:
    def submit(self, worker: Callable[..., Any], *args: object) -> Future:
        future = Future()
        try:
            future.set_result(worker(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, *_args: object, **_kwargs: object) -> None:
        pass


@pytest.fixture
def regex_semantics_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests check extraction policy with the real regex worker. Process
    # startup must not consume their regex budget under CI contention. Real
    # isolation, backtracking timeout and pool lifecycle have separate tests.
    def make_pool(**_kwargs: object) -> _InlineRegexPool:
        return _InlineRegexPool()

    monkeypatch.setattr(rag_query, "ProcessPoolExecutor", make_pool)


# =============================================================================
# Field-only mode
# =============================================================================


class TestFieldOnlyMode:
    def test_extracts_value_verbatim(self):
        builder = _builder(query_field="question")
        result = builder.build(_row({"question": "What is RAG?"}))
        assert result.query == "What is RAG?"

    def test_missing_field_returns_error(self):
        """Missing query_field returns a typed missing_field error, never crashes.

        ADR-013 / DeclaredRequiredFieldsContract pre-empts this in the engine
        pipeline (the engine catches the missing field before process() runs).
        The defensive guard here ensures that direct callers of build() -- unit
        tests, batch paths, or non-engine callers -- receive a typed error
        result rather than a bare builtin KeyError with no audit record.
        Mirrors the null_value guard below and the azure/base.py missing_field
        pattern (base.py lines ~304-307).
        """
        builder = _builder(query_field="question")
        result = builder.build(_row({"other_field": "value"}))
        assert result.error is not None
        assert result.error["reason"] == "missing_field"
        assert result.error["field"] == "question"

    def test_none_value_returns_error(self):
        builder = _builder(query_field="question")
        result = builder.build(_row({"question": None}))
        assert result.error is not None
        assert result.error["reason"] == "invalid_input"
        assert result.error["cause"] == "null_value"

    def test_empty_string_returns_error(self):
        builder = _builder(query_field="question")
        result = builder.build(_row({"question": ""}))
        assert result.error is not None
        assert result.error["reason"] == "invalid_input"
        assert result.error["cause"] == "empty_query"

    def test_whitespace_only_returns_error(self):
        builder = _builder(query_field="question")
        result = builder.build(_row({"question": "   \t\n  "}))
        assert result.error is not None
        assert result.error["reason"] == "invalid_input"
        assert result.error["cause"] == "empty_query"

    @pytest.mark.parametrize(
        "bad_value",
        [
            b"hello world",  # bytes: bytes.strip() silently succeeds, so without
            42,  # the type guard these would produce wrong-typed
            ["a", "b"],  # QueryResult(query=<non-str>) and corrupt the audit
        ],  # trail. Pin that the guard fires and returns a routable error.
        ids=["bytes", "int", "list"],
    )
    def test_non_str_value_returns_wrong_type_error(self, bad_value):
        """A non-str field value is a row failure returned for on_error, never a query.

        Regression guard: bytes.strip() and bool(b"x") both succeed, so a bytes
        value would pass _validate_non_empty and produce QueryResult(query=b"...")
        without the type guard. The guard RETURNS the failure (like the missing
        and None cases) so the engine routes the row; a raise here aborted the
        run. The reason names the field and the type, never the value.
        """
        builder = _builder(query_field="question")
        result = builder.build(_row({"question": bad_value}))

        assert result.query is None
        assert result.error == {
            "reason": "invalid_input",
            "error_type": "wrong_type",
            "field": "question",
            "expected": "str",
            "actual_type": type(bad_value).__name__,
            "error": f"must be str, got {type(bad_value).__name__}",
        }
        assert repr(bad_value) not in repr(sorted(result.error.items()))


# =============================================================================
# Template mode
# =============================================================================


class TestTemplateMode:
    def test_renders_with_query_and_row(self):
        builder = _builder(
            query_field="topic",
            query_template="Find documents about {{ query }} for {{ row.category }}",
        )
        result = builder.build(_row({"topic": "compliance", "category": "finance"}))
        assert result.query == "Find documents about compliance for finance"

    @pytest.mark.parametrize(("value", "rendered"), [(42, "42"), (1.5, "1.5"), (True, "True")], ids=["int", "float", "bool"])
    def test_a_non_str_value_is_interpolated_not_refused(self, value, rendered):
        """Template mode binds the value like any row value; only the modes that USE it as the query require a str.

        The one type check in ``build`` runs after the template dispatch, so its
        order is the behaviour: hoisting it above the dispatch would refuse this
        row as ``wrong_type``.
        """
        builder = _builder(query_field="topic", query_template="Find documents about {{ query }}")
        result = builder.build(_row({"topic": value}))
        assert result.error is None
        assert result.query == f"Find documents about {rendered}"

    def test_structural_error_at_compile_time(self):
        with pytest.raises(TemplateError):
            _builder(
                query_field="topic",
                query_template="{% if unclosed",
            )

    def test_render_error_returns_error(self):
        builder = _builder(
            query_field="topic",
            query_template="{{ query }} for {{ row.missing_field }}",
        )
        result = builder.build(_row({"topic": "test"}))
        assert result.error is not None
        assert result.error["reason"] == "template_rendering_failed"

    def test_resource_bounded_render_returns_row_error(self):
        builder = _builder(query_field="topic", query_template="{{ query * 300000000 }}")
        result = builder.build(_row({"topic": "x"}))
        assert result.error is not None
        assert result.error["reason"] == "template_rendering_failed"

    def test_render_error_reason_names_no_row_value(self):
        """A lookup key computed from the row never reaches the reason (RAG-F1).

        Before the shared renderer the reason was Jinja's text:
        ``'dict object' has no attribute 'SENTINEL-rag-4e1f'``. Configuration
        admits a computed key only under the ``[]`` opt-out, the whole row.
        """
        builder = _builder(query_field="topic", query_template="{{ query }} {{ row[row.k] }}")
        result = builder.build(_row({"topic": "t", "k": "SENTINEL-rag-4e1f"}))
        assert result.error == {
            "reason": "template_rendering_failed",
            "error": ("Undefined variable: the row has no field <a key the template does not spell out>"),
            "field": "topic",
        }

    def test_a_template_runtime_error_is_a_row_error(self):
        """The catch list is SandboxedTemplate's, which includes a bare TemplateRuntimeError.

        A filter named by the row is looked up only when the template runs;
        Jinja's message quotes that name (the row's value), so only the class
        is kept. RAG's own catch list used to omit the class, so the run aborted.
        """
        builder = _builder(query_field="topic", query_template="{{ [query] | map(query) | list }}")
        result = builder.build(_row({"topic": "SENTINEL-rag-map"}))
        assert result.error == {
            "reason": "template_rendering_failed",
            "error": "Template rendering failed: TemplateRuntimeError (message withheld: it can quote row data)",
            "field": "topic",
        }

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            pytest.param(
                "{% if query %}{{ query | no_such_filter }}{% endif %}",
                "No filter named 'no_such_filter'.",
                id="unknown-filter-inside-if",
            ),
            pytest.param(
                "{{ query | truncate(2) }}",
                "truncate() arguments can never be satisfied: expected length >= 3, got 2",
                id="literal-truncate",
            ),
        ],
    )
    def test_a_template_that_fails_every_row_is_refused_when_built(self, template: str, message: str) -> None:
        """A configuration error is refused at construction, never routed once per row."""
        with pytest.raises(TemplateError) as excinfo:
            _builder(query_field="topic", query_template=template)
        assert str(excinfo.value) == f"Invalid query template syntax: {message}"


# =============================================================================
# Regex mode
# =============================================================================


class TestRegexMode:
    def test_captures_first_group(self, regex_semantics_pool):
        builder = _builder(
            query_field="text",
            query_pattern=r"issue:\s*(.+?)(?:\n|$)",
        )
        result = builder.build(_row({"text": "issue: payment failed\nother stuff"}))
        assert result.query == "payment failed"

    def test_full_match_when_no_groups(self, regex_semantics_pool):
        builder = _builder(
            query_field="text",
            query_pattern=r"\w+@\w+\.\w+",
        )
        result = builder.build(_row({"text": "contact user@example.com for help"}))
        assert result.query == "user@example.com"

    def test_no_match_returns_error(self, regex_semantics_pool):
        builder = _builder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )
        result = builder.build(_row({"text": "no issue here"}))
        assert result.error is not None
        assert result.error["reason"] == "no_regex_match"

    def test_non_participating_group_returns_error(self, regex_semantics_pool):
        """Optional capture group that didn't participate."""
        builder = _builder(
            query_field="text",
            query_pattern=r"(?:issue|problem)(?::\s*(.+?))?$",
        )
        result = builder.build(_row({"text": "issue"}))
        assert result.error is not None
        assert result.error["reason"] == "no_regex_match"
        assert result.error["cause"] == "capture_group_empty"

    def test_timeout_on_catastrophic_backtracking(self):
        """ReDoS protection: pathological pattern with adversarial input."""
        pattern = "".join(("(", "a+", ")", "+", "b"))
        builder = _builder(
            query_field="text",
            query_pattern=pattern,
            regex_timeout=0.1,  # Short timeout for test
        )
        result = builder.build(_row({"text": "a" * 30}))
        assert result.error is not None
        assert result.error["reason"] == "regex_timeout"
        assert result.error["reason"] != "no_regex_match"
        assert result.error["field"] == "text"
        assert result.error["pattern"] == pattern
        assert result.error["max_seconds"] == 0.1


# =============================================================================
# Subprocess crash detection
# =============================================================================


class TestWorkerFailureDetection:
    """Worker failures in the ProcessPoolExecutor surface as RuntimeError.

    _regex_worker is system-owned code. A crash or exception in the worker
    is a code bug, not a data issue. Per offensive programming rules: plugin
    bugs crash immediately — they don't silently quarantine.
    """

    def test_worker_exception_raises_runtime_error(self):
        """When the pool future raises, build() must raise RuntimeError."""
        builder = _builder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )

        failed_future = Future()
        failed_future.set_exception(ValueError("simulated worker bug"))
        _replace_regex_pool(builder, failed_future)

        with pytest.raises(RuntimeError, match="Regex worker failed"):
            builder.build(_row({"text": "issue: payment failed"}))

    def test_worker_type_error_on_a_str_is_a_worker_bug(self):
        """A TypeError from the worker is not a row fault once the value is a str.

        The type check runs before submit, so the worker only ever sees a str;
        a TypeError it raises is reported like any other worker failure, never
        relabelled as a wrong-typed row.
        """
        builder = _builder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )

        failed_future = Future()
        failed_future.set_exception(TypeError("simulated worker bug"))
        _replace_regex_pool(builder, failed_future)

        with pytest.raises(RuntimeError, match="Regex worker failed"):
            builder.build(_row({"text": "issue: payment failed"}))

    def test_worker_error_includes_pattern_and_cause(self):
        """RuntimeError from worker failure includes the pattern for diagnostics."""
        builder = _builder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )

        failed_future = Future()
        failed_future.set_exception(ValueError("kaboom"))
        _replace_regex_pool(builder, failed_future)

        with pytest.raises(RuntimeError, match="issue:") as exc_info:
            builder.build(_row({"text": "issue: payment failed"}))
        assert "kaboom" in str(exc_info.value)

    def test_non_str_value_is_rejected_before_the_regex_worker(self):
        """A non-str query_field value in regex mode is a returned row failure.

        The type is checked BEFORE the value reaches the worker, so the row is
        routed via on_error with the same reason as field-only mode, and no
        worker exception can ever be the signal for a row fault (the broad
        worker-failure catch reports a code bug, not a data issue). The
        pattern would match the digits if the int were stringified, so a
        coercing implementation fails here too.
        """
        builder = _builder(
            query_field="text",
            query_pattern=r"(\d+)",
        )
        submitted: list[object] = []

        class _RecordingPool(_RegexPoolFake):
            def submit(self, *args: object, **kwargs: object) -> Future:
                submitted.append(args)
                return super().submit(*args, **kwargs)

        # If the value did reach the worker, answer as re.Pattern.search()
        # does for an int, so a missing pre-check fails fast and faithfully.
        worker_answer: Future = Future()
        worker_answer.set_exception(TypeError("expected string or bytes-like object, got 'int'"))
        assert builder._regex_pool is not None
        builder._regex_pool.shutdown(wait=False)
        builder._regex_pool = _RecordingPool(worker_answer)
        try:
            result = builder.build(_row({"text": 12345}))
        finally:
            builder.close()

        assert submitted == [], "a non-str value must never reach the regex worker"
        assert result.query is None
        assert result.error == {
            "reason": "invalid_input",
            "error_type": "wrong_type",
            "field": "text",
            "expected": "str",
            "actual_type": "int",
            "error": "must be str, got int",
        }
        assert "12345" not in repr(sorted(result.error.items()))


# =============================================================================
# Pool lifecycle
# =============================================================================


class TestPoolLifecycle:
    """ProcessPoolExecutor is created for regex mode and shut down on close()."""

    def test_pool_created_only_for_regex_mode(self):
        builder_field = _builder(query_field="text")
        assert builder_field._regex_pool is None

        builder_template = _builder(query_field="text", query_template="{{ query }}")
        assert builder_template._regex_pool is None

        builder_regex = _builder(query_field="text", query_pattern=r"\w+")
        assert builder_regex._regex_pool is not None
        builder_regex.close()

    def test_close_shuts_down_pool(self):
        builder = _builder(query_field="text", query_pattern=r"\w+")
        assert builder._regex_pool is not None
        builder.close()
        assert builder._regex_pool is None

    @pytest.mark.skipif(not os.path.exists("/proc"), reason="Linux /proc required")
    def test_no_fd_leak_after_repeated_evaluations(self):
        """FDs don't accumulate over many calls with pool reuse."""
        builder = _builder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )
        pid = os.getpid()
        fd_count_before = len(os.listdir(f"/proc/{pid}/fd"))

        for _ in range(20):
            result = builder.build(_row({"text": "issue: payment failed"}))
            assert result.query is not None

        fd_count_after = len(os.listdir(f"/proc/{pid}/fd"))

        fd_count_after = len(os.listdir(f"/proc/{pid}/fd"))
        builder.close()

        assert fd_count_after - fd_count_before < 10, (
            f"File descriptor leak: {fd_count_after - fd_count_before} new FDs "
            f"after 20 regex evaluations (before={fd_count_before}, after={fd_count_after})"
        )
