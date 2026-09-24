"""Query construction for RAG retrieval transform.

Three modes, all anchored on query_field:
1. Field only: use field value verbatim
2. Field + template: render Jinja2 template with {{ query }} and {{ row }}
3. Field + regex: extract search text via capture group
"""

from __future__ import annotations

import multiprocessing as mp
import re
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any

from jinja2 import TemplateSyntaxError

from elspeth.contracts.errors import TransformErrorReason
from elspeth.core.regex_worker import run_regex_worker
from elspeth.plugins.infrastructure.templates import SandboxedTemplate, TemplateError


@dataclass(frozen=True)
class QueryResult:
    """Result of query construction."""

    query: str | None = None
    error: TransformErrorReason | None = None


class QueryBuilder:
    """Constructs search queries from row data.

    Supports three modes:
    - Field only: query_field set, no template or pattern
    - Template: query_field + query_template (Jinja2)
    - Regex: query_field + query_pattern (re capture group)

    Regex mode uses a ProcessPoolExecutor (max 1 worker) for timeout enforcement.
    Python threads cannot be interrupted while running C extension code (re module),
    so process-level isolation is the only reliable timeout mechanism for ReDoS.
    The pool amortizes process creation cost across all rows in the run.
    """

    def __init__(
        self,
        query_field: str,
        *,
        query_template: str | None = None,
        query_pattern: str | None = None,
        regex_timeout: float = 5.0,
    ) -> None:
        self._query_field = query_field
        self._regex_timeout = regex_timeout
        self._compiled_template: SandboxedTemplate | None = None
        self._compiled_pattern: re.Pattern[str] | None = None
        self._regex_pool: ProcessPoolExecutor | None = None

        if query_template is not None:
            try:
                self._compiled_template = SandboxedTemplate(query_template)
            except TemplateSyntaxError as e:
                raise TemplateError(f"Invalid query template syntax: {e}") from e

        if query_pattern is not None:
            self._compiled_pattern = re.compile(query_pattern)
            # Lazily create the process pool on first use. Single worker
            # bounds concurrent process count while amortizing spawn cost.
            self._regex_pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))

    def build(self, row_data: dict[str, Any]) -> QueryResult:
        """Construct a search query from row data."""
        if self._query_field not in row_data:
            return QueryResult(
                error=TransformErrorReason(
                    reason="missing_field",
                    field=self._query_field,
                )
            )
        extracted = row_data[self._query_field]

        if extracted is None:
            return QueryResult(
                error=TransformErrorReason(
                    reason="invalid_input",
                    field=self._query_field,
                    cause="null_value",
                )
            )

        if self._compiled_template is None and not isinstance(extracted, str):
            actual_type = type(extracted).__name__
            return QueryResult(
                error=TransformErrorReason(
                    reason="invalid_input",
                    error_type="wrong_type",
                    field=self._query_field,
                    expected="str",
                    actual_type=actual_type,
                    error=f"must be str, got {actual_type}",
                )
            )

        if self._compiled_template is not None:
            return self._build_template(extracted, row_data)
        elif self._compiled_pattern is not None:
            return self._build_regex(extracted)
        else:
            return self._build_field_only(extracted)

    def _build_field_only(self, extracted: Any) -> QueryResult:
        # build() routes observed wrong types before dispatch. Keep this guard
        # for direct private calls so bytes cannot become a successful query.
        if not isinstance(extracted, str):
            raise TypeError(
                f"query_field '{self._query_field}' expected str, got {type(extracted).__name__} "
                f"— upstream plugin bug (Tier 2 data must not be coerced)"
            )
        return self._validate_non_empty(extracted)

    def _build_template(self, extracted: Any, row_data: dict[str, Any]) -> QueryResult:
        assert self._compiled_template is not None  # guaranteed by build() guard
        try:
            query = self._compiled_template.render(query=extracted, row=row_data)
        except TemplateError as e:
            # SandboxedTemplate's message is value-free by construction: a
            # template may compute a lookup key from the row, and Jinja's own
            # message would quote it.
            return QueryResult(
                error=TransformErrorReason(
                    reason="template_rendering_failed",
                    error=str(e),
                    field=self._query_field,
                )
            )
        return self._validate_non_empty(query)

    def _build_regex(self, extracted: Any) -> QueryResult:
        assert self._compiled_pattern is not None  # guaranteed by build() guard
        assert self._regex_pool is not None  # created when pattern is compiled

        future = self._regex_pool.submit(run_regex_worker, self._compiled_pattern, extracted)
        try:
            match_result = future.result(timeout=self._regex_timeout)
        except FuturesTimeoutError:
            future.cancel()
            # future.cancel() only prevents queued tasks from starting — it
            # cannot kill a worker already executing C extension code (re module).
            # shutdown(wait=False) does NOT terminate running workers; the stuck
            # process stays alive as an orphan. We must explicitly kill it.
            #
            # _processes is a private dict[int, Process] — there is no public
            # API to force-kill pool workers in CPython's ProcessPoolExecutor.
            stuck_processes = list(self._regex_pool._processes.values())
            self._regex_pool.shutdown(wait=False, cancel_futures=True)
            for proc in stuck_processes:
                if proc.is_alive():
                    proc.kill()
            self._regex_pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
            return QueryResult(
                error=TransformErrorReason(
                    reason="regex_timeout",
                    field=self._query_field,
                    pattern=self._compiled_pattern.pattern,
                    max_seconds=self._regex_timeout,
                )
            )
        except TypeError as exc:
            # re.Pattern.search() raises TypeError when handed a non-str value
            # (int, bytes, list, ...). The field reached us as Tier 2 pipeline
            # data, so a non-str here is an upstream plugin contract violation,
            # not a regex-engine bug — crash with a message that names the
            # actual fault (Tier 2 data must not be coerced).
            raise TypeError(
                f"query_field '{self._query_field}' expected str, got "
                f"{type(extracted).__name__} — upstream plugin bug "
                f"(Tier 2 data must not be coerced): {exc}"
            ) from exc
        except Exception as exc:
            # _regex_worker is system-owned code — a crash is a code bug, not a data issue.
            raise RuntimeError(
                f"Regex worker failed while evaluating pattern "
                f"{self._compiled_pattern.pattern!r} against field "
                f"'{self._query_field}': {exc}. This is a bug in the regex "
                f"worker or the Python regex engine — not a data issue."
            ) from exc

        if not match_result.matched:
            return QueryResult(
                error=TransformErrorReason(
                    reason="no_regex_match",
                    field=self._query_field,
                    pattern=self._compiled_pattern.pattern,
                )
            )

        # RAG policy: capture-group patterns use the first group; plain patterns use the full match.
        captured = match_result.groups[0] if self._compiled_pattern.groups else match_result.full_match
        if captured is None:
            return QueryResult(
                error=TransformErrorReason(
                    reason="no_regex_match",
                    field=self._query_field,
                    cause="capture_group_empty",
                )
            )

        return self._validate_non_empty(captured)

    def _validate_non_empty(self, query: str) -> QueryResult:
        if not query.strip():
            return QueryResult(
                error=TransformErrorReason(
                    reason="invalid_input",
                    field=self._query_field,
                    cause="empty_query",
                )
            )
        return QueryResult(query=query)

    def close(self) -> None:
        """Shut down the regex process pool if one was created."""
        if self._regex_pool is not None:
            self._regex_pool.shutdown(wait=False)
            self._regex_pool = None
