"""Tests for RAG query construction."""

import os
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import pytest

from elspeth.plugins.infrastructure.templates import TemplateError
from elspeth.plugins.transforms.rag import query as rag_query
from elspeth.plugins.transforms.rag.query import QueryBuilder


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
        builder = QueryBuilder(query_field="question")
        result = builder.build({"question": "What is RAG?"})
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
        builder = QueryBuilder(query_field="question")
        result = builder.build({"other_field": "value"})
        assert result.error is not None
        assert result.error["reason"] == "missing_field"
        assert result.error["field"] == "question"

    def test_none_value_returns_error(self):
        builder = QueryBuilder(query_field="question")
        result = builder.build({"question": None})
        assert result.error is not None
        assert result.error["reason"] == "invalid_input"
        assert result.error["cause"] == "null_value"

    def test_empty_string_returns_error(self):
        builder = QueryBuilder(query_field="question")
        result = builder.build({"question": ""})
        assert result.error is not None
        assert result.error["reason"] == "invalid_input"
        assert result.error["cause"] == "empty_query"

    def test_whitespace_only_returns_error(self):
        builder = QueryBuilder(query_field="question")
        result = builder.build({"question": "   \t\n  "})
        assert result.error is not None
        assert result.error["reason"] == "invalid_input"
        assert result.error["cause"] == "empty_query"

    @pytest.mark.parametrize(
        "bad_value",
        [
            b"hello world",  # bytes: bytes.strip() silently succeeds, so without
            42,  # the isinstance guard these would produce wrong-typed
            ["a", "b"],  # QueryResult(query=<non-str>) and corrupt the audit
        ],  # trail without a row error. Pin the typed failure.
        ids=["bytes", "int", "list"],
    )
    def test_non_str_value_returns_row_error(self, bad_value):
        """Non-str field values fail their row without corrupting the audit trail.

        Regression guard: bytes.strip() and bool(b"x") both succeed, so a bytes
        value would pass _validate_non_empty and produce QueryResult(query=b"...")
        without the type guard. Pin the failure reason and exclude the value.
        """
        builder = QueryBuilder(query_field="question")
        assert builder.build({"question": bad_value}).error == {
            "reason": "invalid_input",
            "error_type": "wrong_type",
            "field": "question",
            "expected": "str",
            "actual_type": type(bad_value).__name__,
            "error": f"must be str, got {type(bad_value).__name__}",
        }


# =============================================================================
# Template mode
# =============================================================================


class TestTemplateMode:
    def test_renders_with_query_and_row(self):
        builder = QueryBuilder(
            query_field="topic",
            query_template="Find documents about {{ query }} for {{ row.category }}",
        )
        result = builder.build({"topic": "compliance", "category": "finance"})
        assert result.query == "Find documents about compliance for finance"

    def test_structural_error_at_compile_time(self):
        with pytest.raises(TemplateError):
            QueryBuilder(
                query_field="topic",
                query_template="{% if unclosed",
            )

    def test_render_error_returns_error(self):
        builder = QueryBuilder(
            query_field="topic",
            query_template="{{ query }} for {{ row.missing_field }}",
        )
        result = builder.build({"topic": "test"})
        assert result.error is not None
        assert result.error["reason"] == "template_rendering_failed"

    def test_resource_bounded_render_returns_row_error(self):
        builder = QueryBuilder(query_field="topic", query_template="{{ query * 300000000 }}")
        result = builder.build({"topic": "x"})
        assert result.error is not None
        assert result.error["reason"] == "template_rendering_failed"

    def test_render_error_reason_names_no_row_value(self):
        """A lookup key computed from the row never reaches the reason (RAG-F1).

        Before the shared renderer the reason was Jinja's text:
        ``'dict object' has no attribute 'SENTINEL-rag-4e1f'``.
        """
        builder = QueryBuilder(query_field="topic", query_template="{{ query }} {{ row[row.k] }}")
        result = builder.build({"topic": "t", "k": "SENTINEL-rag-4e1f"})
        assert result.error == {
            "reason": "template_rendering_failed",
            "error": "Undefined variable: 'dict object' has no attribute <a key the template does not spell out>",
            "field": "topic",
        }

    def test_a_template_runtime_error_is_a_row_error(self):
        """The catch list is SandboxedTemplate's, which includes a bare TemplateRuntimeError.

        An unknown filter inside a conditional compiles and raises only when
        the branch runs; RAG's own catch list used to omit it, so the run aborted.
        """
        builder = QueryBuilder(query_field="topic", query_template="{% if query %}{{ query | no_such_filter }}{% endif %}")
        result = builder.build({"topic": "t"})
        assert result.error == {
            "reason": "template_rendering_failed",
            "error": "Template rendering failed: TemplateRuntimeError (message withheld: it can quote row data)",
            "field": "topic",
        }


# =============================================================================
# Regex mode
# =============================================================================


class TestRegexMode:
    def test_captures_first_group(self, regex_semantics_pool):
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"issue:\s*(.+?)(?:\n|$)",
        )
        result = builder.build({"text": "issue: payment failed\nother stuff"})
        assert result.query == "payment failed"

    def test_full_match_when_no_groups(self, regex_semantics_pool):
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"\w+@\w+\.\w+",
        )
        result = builder.build({"text": "contact user@example.com for help"})
        assert result.query == "user@example.com"

    def test_no_match_returns_error(self, regex_semantics_pool):
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )
        result = builder.build({"text": "no issue here"})
        assert result.error is not None
        assert result.error["reason"] == "no_regex_match"

    def test_non_participating_group_returns_error(self, regex_semantics_pool):
        """Optional capture group that didn't participate."""
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"(?:issue|problem)(?::\s*(.+?))?$",
        )
        result = builder.build({"text": "issue"})
        assert result.error is not None
        assert result.error["reason"] == "no_regex_match"
        assert result.error["cause"] == "capture_group_empty"

    def test_timeout_on_catastrophic_backtracking(self):
        """ReDoS protection: pathological pattern with adversarial input."""
        pattern = "".join(("(", "a+", ")", "+", "b"))
        builder = QueryBuilder(
            query_field="text",
            query_pattern=pattern,
            regex_timeout=0.1,  # Short timeout for test
        )
        result = builder.build({"text": "a" * 30})
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
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )

        failed_future = Future()
        failed_future.set_exception(ValueError("simulated worker bug"))
        _replace_regex_pool(builder, failed_future)

        with pytest.raises(RuntimeError, match="Regex worker failed"):
            builder.build({"text": "issue: payment failed"})

    def test_worker_error_includes_pattern_and_cause(self):
        """RuntimeError from worker failure includes the pattern for diagnostics."""
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )

        failed_future = Future()
        failed_future.set_exception(ValueError("kaboom"))
        _replace_regex_pool(builder, failed_future)

        with pytest.raises(RuntimeError, match="issue:") as exc_info:
            builder.build({"text": "issue: payment failed"})
        assert "kaboom" in str(exc_info.value)

    def test_non_str_value_is_a_row_error_before_regex_dispatch(self):
        """A non-str query value fails its row before the regex worker runs."""
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )
        try:
            assert builder.build({"text": 12345}).error == {
                "reason": "invalid_input",
                "error_type": "wrong_type",
                "field": "text",
                "expected": "str",
                "actual_type": "int",
                "error": "must be str, got int",
            }
        finally:
            builder.close()


# =============================================================================
# Pool lifecycle
# =============================================================================


class TestPoolLifecycle:
    """ProcessPoolExecutor is created for regex mode and shut down on close()."""

    def test_pool_created_only_for_regex_mode(self):
        builder_field = QueryBuilder(query_field="text")
        assert builder_field._regex_pool is None

        builder_template = QueryBuilder(query_field="text", query_template="{{ query }}")
        assert builder_template._regex_pool is None

        builder_regex = QueryBuilder(query_field="text", query_pattern=r"\w+")
        assert builder_regex._regex_pool is not None
        builder_regex.close()

    def test_close_shuts_down_pool(self):
        builder = QueryBuilder(query_field="text", query_pattern=r"\w+")
        assert builder._regex_pool is not None
        builder.close()
        assert builder._regex_pool is None

    @pytest.mark.skipif(not os.path.exists("/proc"), reason="Linux /proc required")
    def test_no_fd_leak_after_repeated_evaluations(self):
        """FDs don't accumulate over many calls with pool reuse."""
        builder = QueryBuilder(
            query_field="text",
            query_pattern=r"issue:\s*(.+)",
        )
        pid = os.getpid()
        fd_count_before = len(os.listdir(f"/proc/{pid}/fd"))

        for _ in range(20):
            result = builder.build({"text": "issue: payment failed"})
            assert result.query is not None

        fd_count_after = len(os.listdir(f"/proc/{pid}/fd"))

        fd_count_after = len(os.listdir(f"/proc/{pid}/fd"))
        builder.close()

        assert fd_count_after - fd_count_before < 10, (
            f"File descriptor leak: {fd_count_after - fd_count_before} new FDs "
            f"after 20 regex evaluations (before={fd_count_before}, after={fd_count_after})"
        )
