"""Provider-neutral retrieval core shared by the RAG retrieval transforms.

Lifecycle:
    __init__: Build QueryBuilder, output field names, schemas, accumulators.
    on_start: Reset accumulators and build the searcher via the subclass hook.
    process: Build query -> search -> format -> attach to row.
    on_complete: Emit telemetry with run statistics.
    close: Release searcher and query builder resources.

A subclass supplies the searcher (``_build_searcher``) and its own readiness
path; this module knows nothing about which backend answers the query.
"""

from __future__ import annotations

import json
import keyword
import math
import re
from abc import abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

import structlog
from jinja2 import TemplateSyntaxError
from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism, TransformResult, propagate_contract
from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.emitted_option import EmittedToOutput
from elspeth.contracts.errors import FrameworkBugError, TransformErrorReason
from elspeth.contracts.events import RAGRetrievalStatistics, TelemetryEvent
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.telemetry import make_warn_telemetry_before_start
from elspeth.plugins.infrastructure.templates import (
    AllFields,
    DeclaredFields,
    RowProjection,
    SandboxedTemplate,
    TemplateError,
    create_sandboxed_environment,
    declared_row_projection,
    find_runtime_unbound_variables,
)
from elspeth.plugins.transforms.rag.formatter import format_context
from elspeth.plugins.transforms.rag.query import QueryBuilder

if TYPE_CHECKING:
    from elspeth.contracts.contexts import LifecycleContext, TransformContext
    from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalSearcher

logger = structlog.get_logger(__name__)


_warn_telemetry_before_start = make_warn_telemetry_before_start(logger)

# The two names QueryBuilder renders a query template with: the query_field
# value and the projected row (rag/query.py).
_QUERY_CONTEXT_NAMES: frozenset[str] = frozenset({"query", "row"})


class RetrievalOutputConfig(TransformDataConfig):
    """Options every retrieval transform shares: the query and the emitted context."""

    @field_validator("schema_config", mode="before")
    @classmethod
    def coerce_schema_config(cls, v: object) -> object:
        """Accept raw dict for schema_config — convert via SchemaConfig.from_dict."""
        if isinstance(v, dict):
            return SchemaConfig.from_dict(v)
        return v

    output_prefix: Annotated[
        str,
        EmittedToOutput(
            "the retrieval transform builds its emitted field names from this prefix, so the value becomes "
            "part of row-data keys and downstream artifact columns"
        ),
    ] = Field(description="Prefix used for fields emitted by retrieval, such as contexts and scores.")
    query_field: str = Field(description="Input row field containing the retrieval query text.")
    query_template: str | None = Field(
        default=None,
        description="Optional template used to build the retrieval query from row fields.",
    )
    query_pattern: str | None = Field(
        default=None,
        description="Optional regular expression used to extract the retrieval query from query_field.",
    )
    top_k: int = Field(default=5, ge=1, le=100, description="Maximum number of matching documents to return for each query.")
    min_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum relevance score required for a retrieved document.")
    on_no_results: Literal["quarantine", "continue"] = Field(
        default="quarantine",
        description="Behavior when no retrieval results satisfy the score threshold.",
    )
    context_format: Literal["numbered", "separated", "raw"] = Field(
        default="numbered",
        description="Formatting style used when combining retrieved contexts into output text.",
    )
    context_separator: Annotated[
        str,
        EmittedToOutput("the retrieval transform inserts this separator into the combined context value written to row data"),
    ] = Field(default="\n---\n", description="Separator inserted between retrieved contexts when applicable.")
    max_context_length: int | None = Field(
        default=None,
        ge=1,
        description="Optional maximum character length for the combined context output.",
    )

    @field_validator("output_prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        if not v.isidentifier():
            raise ValueError(f"output_prefix must be a valid Python identifier, got {v!r}")
        if keyword.iskeyword(v):
            raise ValueError(
                f"output_prefix must not be a Python keyword, got {v!r}. "
                f"Keywords like 'class', 'return' produce field names that break Jinja2 templates."
            )
        return v

    @field_validator("query_field")
    @classmethod
    def validate_query_field(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("query_field cannot be empty")
        if not stripped.isidentifier():
            raise ValueError(f"query_field must be a valid Python identifier, got {v!r}")
        if keyword.iskeyword(stripped):
            raise ValueError(f"query_field must not be a Python keyword, got {stripped!r}")
        return stripped

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.query_field})

    @model_validator(mode="after")
    def validate_query_modes(self) -> Self:
        if self.query_template and self.query_pattern:
            raise ValueError("query_template and query_pattern are mutually exclusive")
        return self

    @field_validator("query_template")
    @classmethod
    def validate_query_template(cls, v: str | None) -> str | None:
        """Compile the query template at config time, as the LLM config does its prompt.

        A template the runtime would refuse to build (malformed, an unknown
        filter or test, a literal argument that fails every row) is refused
        here too, so ``elspeth validate``, the composer and the run agree.
        """
        if v is None:
            return None
        try:
            SandboxedTemplate(v)
        except (TemplateSyntaxError, TemplateError) as exc:
            raise ValueError(f"Invalid query template: {exc}") from exc
        return v

    def query_template_row_projection(self) -> RowProjection:
        """What the query template's ``row`` holds (ADR-051): the node's declaration.

        ``required_input_fields`` read as every template node reads it, with
        ``query_field`` always declared (the node reads it by its own option):
        a list holds those fields plus ``query_field``, omitted holds
        ``query_field`` alone, and ``[]`` is the opt-out, the whole row. The
        configuration checks below and the render read this one value.
        """
        return declared_row_projection(self.required_input_fields, always_declared=frozenset({self.query_field}))

    @model_validator(mode="after")
    def _validate_query_template_row_reads(self) -> Self:
        """Refuse a query template whose reads configuration can prove fail or go unused.

        The query renders with ``query`` (the ``query_field`` value) and
        ``row``, projected to ``query_template_row_projection()``. Each refusal
        is a template that would otherwise fail every row at render, or a
        declaration the template cannot use; the LLM prompt template carries
        the same checks (``LLMConfig``), and the composer's mutation gate runs
        this model, so ``elspeth validate``, the composer and the run agree.

        - a top-level name other than ``query``, ``row`` or a sandbox global
          is undefined on every row;
        - under every declaration, ``[]`` included, the row used as an object
          (a retired name such as ``row.to_dict``, a call on a row field such
          as ``row.keys()``, an uncalled ``row.get``): a template row holds
          fields and one method, ``get``, so these fail or garble every row;
        - under a declaration (a list, or omitted), a computed row key
          (``row[k]``, ``row.get(k)``, ``row|attr(k)``) or ``carrier-limit``
          names no field the declaration could cover; ``[]`` opts out of this
          check;
        - a literal ``row.<field>`` read outside the declaration fails every
          row as an undeclared read; omitted, only ``query_field`` is declared;
        - the dual: fields declared beyond ``query_field`` while the template
          never reads ``row`` at all can never reach the query.
        """
        if self.query_template is None:
            return self
        from elspeth.core.templates import (
            describe_dynamic_row_access,
            describe_row_api_misuse,
            extract_jinja2_field_usage,
            template_loads_name,
        )
        from elspeth.plugins.sources.field_normalization import describe_undeclared_row_fields, undeclared_row_fields

        template = self.query_template
        env = create_sandboxed_environment()
        # The field validator compiled the template, so it parses here.
        unbound = sorted(find_runtime_unbound_variables(env.parse(template)) - _QUERY_CONTEXT_NAMES - frozenset(env.globals))
        if unbound:
            names = ", ".join(f"'{name}'" for name in unbound)
            raise ValueError(
                f"query_template references {names}, which the query render context does not define: a query "
                "template sees 'query' (the query_field value) and 'row', so rendering fails with 'Undefined "
                "variable' on every row. Rewrite each name as '{{ query }}' or '{{ row.<field> }}', or remove it."
            )

        usage = extract_jinja2_field_usage(template)
        if usage.row_api_misuses:
            raise ValueError(f"query_template {describe_row_api_misuse(usage.row_api_misuses)}")

        match self.query_template_row_projection():
            case AllFields():
                return self
            case DeclaredFields(names=declared):
                pass

        if usage.computed_key_accesses:
            raise ValueError(
                f"query_template uses dynamic row field access ({describe_dynamic_row_access(usage.computed_key_accesses)}). "
                "The template's row holds only the fields this node declares (options.required_input_fields and "
                "query_field), and a computed key names no field a declaration could cover. Use static row.field or "
                "row['field'] references, or set options.required_input_fields: [] to opt out: the template then "
                "sees the whole row."
            )

        undeclared = undeclared_row_fields(usage.fields, declared)
        if undeclared:
            fields = describe_undeclared_row_fields(undeclared)
            if self.required_input_fields is None:
                raise ValueError(
                    f"query_template reads {fields} under 'row', but options.required_input_fields is not declared, "
                    f"so the template's row holds only the query field '{self.query_field}' and every row fails "
                    "at render with 'Undeclared field'. Declare the fields the query reads in "
                    "options.required_input_fields (the upstream producer must guarantee them), or set "
                    "options.required_input_fields: [] to opt out: the template then sees the whole row."
                )
            declared_names = ", ".join(f"'{name}'" for name in sorted(declared))
            raise ValueError(
                f"query_template reads {fields} under 'row', which this node does not declare: its template sees "
                f"{declared_names} (options.required_input_fields and query_field), so every row fails at render "
                "with 'Undeclared field'. Add each field to options.required_input_fields only if the upstream "
                "producer guarantees it, or rewrite the reference to a declared field."
            )

        beyond_query = sorted(set(self.required_input_fields or ()) - {self.query_field})
        if beyond_query and not template_loads_name(template, "row"):
            listed = ", ".join(f"'{name}'" for name in beyond_query)
            raise ValueError(
                f"options.required_input_fields declares {listed}, but query_template never reads 'row', so no "
                "declared field can reach the query. Reference them in the template (e.g. "
                f"'{{{{ row.{beyond_query[0]} }}}}'), or drop them from options.required_input_fields: the query "
                f"field '{self.query_field}' reaches the template as '{{{{ query }}}}' without a declaration."
            )
        return self

    @field_validator("query_pattern")
    @classmethod
    def validate_regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e
        return v


class RetrievalTransformBase(BaseTransform):
    """Enriches rows with retrieval-augmented context from a searcher.

    Abstract: plugin discovery skips it. Uses synchronous process() since
    retrieval calls are I/O-bound but single-query-per-row.

    Output fields (prefixed with output_prefix), with the types the plugin
    declares (``created_output_fields``) and the engine enforces:
        {prefix}__rag_context: Formatted text from retrieved chunks (str;
            None when nothing was found under on_no_results: continue).
        {prefix}__rag_score: Best relevance score (float, 0.0-1.0; None
            when nothing was found under on_no_results: continue).
        {prefix}__rag_count: Number of chunks retrieved (int).
        {prefix}__rag_sources: JSON envelope with source provenance (str).
    """

    # Every retrieval is a call to a search backend; subclasses redeclare it.
    determinism: Determinism = Determinism.EXTERNAL_CALL
    # The provider value written into error reasons and run statistics.
    _provider_label: str
    _searcher: RetrievalSearcher | None

    def __init__(self, config: dict[str, Any], retrieval_config: RetrievalOutputConfig) -> None:
        super().__init__(config)

        self._retrieval_config = retrieval_config
        self._initialize_declared_input_fields(self._retrieval_config)
        prefix = self._retrieval_config.output_prefix

        # Output field names
        self._field_context = f"{prefix}__rag_context"
        self._field_score = f"{prefix}__rag_score"
        self._field_count = f"{prefix}__rag_count"
        self._field_sources = f"{prefix}__rag_sources"

        self.declared_output_fields = frozenset(
            [
                self._field_context,
                self._field_score,
                self._field_count,
                self._field_sources,
            ]
        )
        self._reject_input_options_naming_created_fields({"query_field": self._retrieval_config.query_field})
        # The plugin's declaration of the four fields it creates (ADR-050).
        # Every provider parses its hits into ``RetrievalChunk``, whose ``score``
        # is a float normalized to [0.0, 1.0] at the Tier-3 boundary; the context
        # is the formatter's text and the sources envelope is ``json.dumps``
        # output. Under ``on_no_results: continue`` the context and score are
        # None ("no retrieval happened", not zero relevance), so both are
        # nullable; the count is then 0.
        self._created_output_fields: tuple[FieldDefinition, ...] = (
            FieldDefinition(self._field_context, "str", nullable=True),
            FieldDefinition(self._field_score, "float", nullable=True),
            FieldDefinition(self._field_count, "int"),
            FieldDefinition(self._field_sources, "str"),
        )

        # Schemas — RAG adds fields, so output uses observed mode
        self.input_schema, self.output_schema = self._create_schemas(
            self._retrieval_config.schema_config,
            self.name,
            adds_fields=True,
        )

        # Output schema config for DAG contract propagation.
        self._output_schema_config = self._build_output_schema_config(self._retrieval_config.schema_config)

        # Query builder
        self._query_builder = QueryBuilder(
            self._retrieval_config.query_field,
            row_projection=self._retrieval_config.query_template_row_projection(),
            query_template=self._retrieval_config.query_template,
            query_pattern=self._retrieval_config.query_pattern,
        )

        # Welford online accumulators for telemetry
        self._total_queries = 0
        self._quarantine_count = 0
        self._total_chunks = 0
        self._score_count = 0
        self._score_mean = 0.0
        self._score_m2 = 0.0

        # Searcher — deferred to on_start()
        self._searcher: RetrievalSearcher | None = None

        # Lifecycle dependencies — set in on_start()
        self._run_id: str = ""
        self._telemetry_emit: Callable[[TelemetryEvent], None] = _warn_telemetry_before_start

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Inject a deterministic retrieval query for invariant probing."""
        return [
            self._augment_invariant_probe_row(
                probe,
                field_name="rag_probe_query",
                value="What is the policy?",
            )
        ]

    def execute_forward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: Any,
    ) -> TransformResult:
        """Exercise the real retrieval path with a provider-agnostic local double."""
        if len(probe_rows) != 1:
            raise FrameworkBugError(
                f"{self.__class__.__name__}.execute_forward_invariant_probe() "
                f"received {len(probe_rows)} rows; retrieval invariant probes require exactly 1 row."
            )

        class _InvariantProvider:
            def __init__(self) -> None:
                self.last_skipped_count = 0
                self.last_skipped_reasons: list[dict[str, Any]] = []

            def search(
                self,
                query: str,
                top_k: int,
                min_score: float,
                *,
                state_id: str,
                token_id: str | None,
                member_token: WorkerMembershipToken,
                work_item: TokenWorkItem,
            ) -> list[RetrievalChunk]:
                del query, top_k, min_score, state_id, token_id
                return [
                    RetrievalChunk(
                        content="Probe context",
                        score=0.95,
                        source_id="probe-doc",
                        metadata={"kind": "invariant"},
                    )
                ]

            def close(self) -> None:
                return None

        original_provider = self._searcher
        original_on_start_called = self._on_start_called
        probe_provider = _InvariantProvider()
        try:
            self.__dict__["_searcher"] = probe_provider
            self._on_start_called = True
            return super().execute_forward_invariant_probe(probe_rows, ctx)
        finally:
            self._on_start_called = original_on_start_called
            self.__dict__["_searcher"] = original_provider
            probe_provider.close()

    @abstractmethod
    def _build_searcher(self, ctx: LifecycleContext) -> RetrievalSearcher:
        """Construct the search backend for this run."""

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        """The four retrieval fields, typed as the plugin's code fixes them (ADR-050)."""
        return self._created_output_fields

    def on_start(self, ctx: LifecycleContext) -> None:
        """Capture lifecycle context and build the searcher."""
        super().on_start(ctx)
        self._run_id = ctx.run_id
        self._telemetry_emit = ctx.telemetry_emit
        self._total_queries = 0
        self._quarantine_count = 0
        self._total_chunks = 0
        self._score_count = 0
        self._score_mean = 0.0
        self._score_m2 = 0.0
        self._searcher = self._build_searcher(ctx)

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        """Process a single row: build query, search, format, attach."""
        if not self._on_start_called:
            raise RuntimeError(
                f"{self.__class__.__name__}.process() called before on_start(). "
                f"The orchestrator must call on_start() before processing rows."
            )
        if self._searcher is None:
            raise RuntimeError(
                f"{self.__class__.__name__} provider not initialized. on_start() must construct the provider before process() is called."
            )
        if ctx.state_id is None:
            raise RuntimeError(f"{self.__class__.__name__} requires state_id on TransformContext.")

        # Orchestrator always provides token — None is a calling-code bug.
        # Consistent with the state_id guard above.
        if ctx.token is None:
            raise RuntimeError(f"{self.__class__.__name__} requires token on TransformContext.")
        token_id = ctx.token.token_id

        # 1. Build query from row data
        query_result = self._query_builder.build(row)
        if query_result.error is not None:
            self._quarantine_count += 1
            return TransformResult.error(
                query_result.error,
                retryable=False,
            )

        query = query_result.query
        assert query is not None  # guaranteed when error is None

        # 2. Search via provider — Tier 3 boundary (external call)
        self._total_queries += 1
        try:
            chunks = self._searcher.search(
                query,
                self._retrieval_config.top_k,
                self._retrieval_config.min_score,
                state_id=ctx.state_id,
                token_id=token_id,
                member_token=ctx.require_member_token(),
                work_item=ctx.require_work_item(),
            )
        except RetrievalError as e:
            if e.retryable:
                raise  # Engine retry handles transient failures
            self._quarantine_count += 1
            retrieval_error_reason: TransformErrorReason = {
                "reason": "retrieval_failed",
                "error": str(e),
                "cause": f"{type(e).__name__}: {e.__cause__}" if e.__cause__ else str(e),
                "provider": self._provider_label,
            }
            if e.status_code is not None:
                retrieval_error_reason["status_code"] = e.status_code
            return TransformResult.error(retrieval_error_reason, retryable=False)

        # Providers surface skip evidence on the instance so the transform can
        # persist why candidate hits were rejected even when no usable chunks remain.
        skipped_count = self._searcher.last_skipped_count
        skipped_reasons = self._searcher.last_skipped_reasons

        # 3. Handle zero results
        if not chunks:
            if self._retrieval_config.on_no_results == "quarantine":
                self._quarantine_count += 1
                no_results_error_reason: TransformErrorReason = {
                    "reason": "no_results",
                    "query": query,
                    "provider": self._provider_label,
                }
                if skipped_count > 0:
                    no_results_error_reason["skipped_count"] = skipped_count
                if skipped_reasons:
                    no_results_error_reason["skipped_reasons"] = skipped_reasons
                return TransformResult.error(no_results_error_reason, retryable=False)
            # on_no_results == "continue" — None sentinels preserve semantic
            # distinction: None means "no retrieval happened", 0.0/"" would
            # fabricate a result indistinguishable from "zero relevance".
            output = row.to_dict()
            output[self._field_context] = None
            output[self._field_score] = None
            output[self._field_count] = 0
            output[self._field_sources] = json.dumps({"v": 1, "sources": []})

            output_contract = propagate_contract(
                input_contract=row.contract,
                output_row=output,
                transform_adds_fields=True,
            )
            output_contract = self._apply_declared_output_field_contracts(output_contract)
            output_contract = self._align_output_contract(output_contract)
            no_results_success_metadata: dict[str, Any] = {"chunk_count": 0, "no_results": True}
            if skipped_count > 0:
                no_results_success_metadata["skipped_count"] = skipped_count
            if skipped_reasons:
                no_results_success_metadata["skipped_reasons"] = skipped_reasons
            return TransformResult.success(
                PipelineRow(output, output_contract),
                success_reason={
                    "action": "rag_retrieval",
                    "metadata": no_results_success_metadata,
                },
            )

        # 4. Format context
        self._total_chunks += len(chunks)
        best_score = chunks[0].score  # chunks are ordered by descending score
        self._update_score_stats(best_score)

        formatted = format_context(
            chunks,
            format_mode=self._retrieval_config.context_format,
            separator=self._retrieval_config.context_separator,
            max_length=self._retrieval_config.max_context_length,
        )

        # 5. Build sources envelope
        sources_envelope = {
            "v": 1,
            "sources": [
                {
                    "source_id": chunk.source_id,
                    "score": chunk.score,
                    "metadata": deep_thaw(chunk.metadata),
                }
                for chunk in chunks
            ],
        }

        # 6. Build output row
        output = row.to_dict()
        output[self._field_context] = formatted.text
        output[self._field_score] = best_score
        output[self._field_count] = len(chunks)
        output[self._field_sources] = json.dumps(sources_envelope)

        output_contract = propagate_contract(
            input_contract=row.contract,
            output_row=output,
            transform_adds_fields=True,
        )
        output_contract = self._apply_declared_output_field_contracts(output_contract)
        output_contract = self._align_output_contract(output_contract)

        success_metadata: dict[str, Any] = {
            "chunk_count": len(chunks),
            "best_score": best_score,
            "truncated": formatted.truncated,
        }
        if skipped_count > 0:
            success_metadata["skipped_count"] = skipped_count
        if skipped_reasons:
            success_metadata["skipped_reasons"] = skipped_reasons

        return TransformResult.success(
            PipelineRow(output, output_contract),
            success_reason={
                "action": "rag_retrieval",
                "metadata": success_metadata,
            },
        )

    def on_complete(self, ctx: LifecycleContext) -> None:
        """Emit telemetry with run statistics."""
        super().on_complete(ctx)
        if ctx.node_id is None:
            raise FrameworkBugError("RAG completion requires a node_id")
        score_std = 0.0
        if self._score_count >= 2:
            score_std = math.sqrt(self._score_m2 / (self._score_count - 1))

        event = RAGRetrievalStatistics(
            timestamp=datetime.now(UTC),
            run_id=self._run_id,
            node_id=ctx.node_id,
            plugin_name=self.name,
            provider=self._provider_label,
            total_queries=self._total_queries,
            total_chunks=self._total_chunks,
            quarantine_count=self._quarantine_count,
            score_count=self._score_count,
            score_mean=self._score_mean if self._score_count > 0 else None,
            score_std=score_std if self._score_count >= 2 else None,
        )
        self._telemetry_emit(event)

    def close(self) -> None:
        """Release searcher and query builder resources."""
        # The searcher may be None if close() is called before on_start() —
        # this is a valid lifecycle path (e.g., config validation failure
        # before the pipeline starts). But if on_start() was called, the
        # searcher must exist — that's guaranteed by on_start's construction.
        if self._searcher is not None:
            self._searcher.close()
        self._query_builder.close()

    def _update_score_stats(self, score: float) -> None:
        """Welford online algorithm for running mean and variance."""
        self._score_count += 1
        delta = score - self._score_mean
        self._score_mean += delta / self._score_count
        delta2 = score - self._score_mean
        self._score_m2 += delta * delta2
