"""Engine span factory with OpenTelemetry and TelemetryManager delivery modes.

Provides structured span creation for pipeline execution.
Falls back to no-op mode when neither delivery owner is configured.

Typical fresh-ingestion hierarchy (names are static; plugin identity is in attributes):
    run                          [run.id=<run_id>]
    ├── source                   [plugin.name=<name>]
    │   └── row                  [row.id=<row_id>, token.id=<token_id>]
    │       ├── transform        [plugin.name=<name>]
    │       └── gate             [plugin.name=<name>]
    └── sink                     [plugin.name=<name>]

Row parenting is path-dependent: ingestion rows use the active source span,
while late leader-drain, resume, and follower rows use durable run correlation.
Aggregation parenting is path-dependent: it uses the active row or source span
when invoked there, and durable run correlation on resume or follower paths.
"""

import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, cast

from elspeth.contracts.events import EngineSpanCompleted, EngineSpanName, EngineSpanStatus, _validate_engine_span_attributes

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer


class NoOpSpan:
    """No-op span for when tracing is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:
        """No-op."""
        pass

    def set_status(self, status: Any) -> None:
        """No-op."""
        pass

    def record_exception(self, exception: BaseException) -> None:
        """No-op."""
        pass

    def is_recording(self) -> bool:
        """Always False for no-op."""
        return False


@dataclass(frozen=True, slots=True)
class _ActiveEngineSpan:
    """Correlation state for one active telemetry-backed engine span."""

    run_id: str
    trace_started_at: datetime
    span_id: str


@dataclass(frozen=True, slots=True)
class _RunTrace:
    """Cross-thread correlation for one active run or resume/follower scope."""

    trace_started_at: datetime
    root_span_id: str | None


@dataclass(slots=True)
class _SpanYieldOutcome:
    """Capture only an exception that crosses this span's yielded body."""

    exception: BaseException | None = None

    def __enter__(self) -> "_SpanYieldOutcome":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.exception = exception
        return False


@dataclass(slots=True)
class _CompletionCallbackFailure:
    """Capture and suppress a failure from one completion callback invocation."""

    exception: BaseException | None = None

    def __enter__(self) -> "_CompletionCallbackFailure":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exception = exception
        return exception is not None


class _TelemetrySpan:
    """Minimal mutable span facade retained for call-site compatibility."""

    def __init__(self, name: EngineSpanName, attributes: dict[str, str | tuple[str, ...]]) -> None:
        self._name = name
        self._attributes = attributes
        self._status = EngineSpanStatus.OK
        self._exception_type: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        candidate = dict(self._attributes)
        candidate.update(_bounded_span_attributes({key: value}))
        _validate_engine_span_attributes(self._name, candidate)
        self._attributes.clear()
        self._attributes.update(candidate)

    def set_status(self, status: Any) -> None:
        if type(status) is not EngineSpanStatus:
            raise TypeError("telemetry-backed engine span status must be EngineSpanStatus")
        self._status = status

    def record_exception(self, exception: BaseException) -> None:
        self._status = EngineSpanStatus.ERROR
        self._exception_type = _safe_exception_type(exception)

    def is_recording(self) -> bool:
        return True


def _safe_exception_type(exception: BaseException) -> str:
    """Return a bounded ASCII class name without rendering exception text."""
    name_descriptor = type.__dict__["__name__"]
    exception_class = type(exception)
    name = name_descriptor.__get__(exception_class, type(exception_class))
    if type(name) is not str:
        return "Exception"
    if not name or len(name) > 128:
        return "Exception"
    if not (name[0].isascii() and (name[0].isalpha() or name[0] == "_")):
        return "Exception"
    if any(not (character.isascii() and (character.isalnum() or character == "_")) for character in name[1:]):
        return "Exception"
    return name


def _add_safe_exception_note(exception: BaseException, note: str) -> None:
    """Attach a note without dispatching through exception instance hooks."""
    state_descriptor = BaseException.__dict__["__dict__"]
    state: object = state_descriptor.__get__(exception, BaseException)
    if type(state) is not dict:
        return
    if dict.__contains__(state, "__notes__"):
        notes: object = dict.__getitem__(state, "__notes__")
        if type(notes) is not list:
            notes = []
            dict.__setitem__(state, "__notes__", notes)
    else:
        notes = []
        dict.__setitem__(state, "__notes__", notes)
    list.append(notes, note)


def _canonical_trace_started_at(value: datetime) -> datetime:
    """Normalize the durable database timestamp to an aware UTC instant."""
    if type(value) is not datetime:
        raise TypeError("trace_started_at must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _new_span_id() -> str:
    """Generate a valid non-zero OpenTelemetry span identifier."""
    span_id = secrets.token_hex(8)
    while span_id == "0000000000000000":
        span_id = secrets.token_hex(8)
    return span_id


def _bounded_span_attributes(attributes: Mapping[str, Any]) -> dict[str, str | tuple[str, ...]]:
    """Project existing span attributes into the bounded telemetry wire shape."""
    bounded: dict[str, str | tuple[str, ...]] = {}
    for key, value in attributes.items():
        if type(value) is str:
            bounded[key] = value[:256]
        elif type(value) in (list, tuple) and all(type(item) is str for item in value):
            bounded[key] = tuple(item[:256] for item in value[:128])
            if key == "token.ids" and len(value) > 128:
                bounded["token.ids.total_count"] = str(len(value))
                bounded["token.ids.truncated_count"] = str(len(value) - 128)
        else:
            raise TypeError(f"engine span attribute {key!r} must be a string or string sequence")
    return bounded


class SpanFactory:
    """Factory for creating OpenTelemetry spans.

    When no tracer is provided, all span methods return no-op contexts.

    Example:
        factory = SpanFactory(tracer=opentelemetry.trace.get_tracer("elspeth"))

        with factory.run_span("run-001") as span:
            with factory.row_span("row-001", "token-001") as row_span:
                with factory.transform_span("my_transform") as transform_span:
                    # Do work
                    pass
    """

    # Singleton no-op span to avoid repeated allocations
    _NOOP_SPAN = NoOpSpan()

    def __init__(
        self,
        tracer: "Tracer | None" = None,
        *,
        telemetry_emit: Callable[[EngineSpanCompleted], None] | None = None,
    ) -> None:
        """Initialize with one optional span delivery owner.

        Args:
            tracer: OpenTelemetry tracer. If None, spans are no-ops.
            telemetry_emit: Existing TelemetryManager event entry point. This
                keeps engine spans inside the configured exporter lifecycle.
        """
        if tracer is not None and telemetry_emit is not None:
            raise ValueError("tracer and telemetry_emit are mutually exclusive span delivery owners")
        self._tracer = tracer
        self._telemetry_emit = telemetry_emit
        self._active_span: ContextVar[_ActiveEngineSpan | None] = ContextVar(
            f"elspeth_engine_span_{id(self)}",
            default=None,
        )
        self._run_traces: dict[str, _RunTrace] = {}
        self._trace_scope_references: dict[str, int] = {}
        self._run_traces_lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        """Whether tracing is enabled."""
        return self._tracer is not None or self._telemetry_emit is not None

    def mark_error(self, span: "Span | NoOpSpan | _TelemetrySpan", exception: BaseException) -> None:
        """Mark a handled engine failure without recording exception content."""
        if self._telemetry_emit is not None:
            cast(_TelemetrySpan, span).record_exception(exception)
            return
        if self._tracer is None:
            return
        from opentelemetry.trace import Status, StatusCode

        cast("Span", span).set_status(Status(StatusCode.ERROR))

    @contextmanager
    def trace_scope(self, run_id: str, trace_started_at: datetime) -> Iterator[None]:
        """Bind durable run correlation without emitting a duplicate run span.

        Resume and follower execution use this scope. Their work belongs to the
        original run trace, but neither lifecycle is a second whole-run span.
        """
        if self._telemetry_emit is None:
            yield
            return
        binding = _RunTrace(trace_started_at=_canonical_trace_started_at(trace_started_at), root_span_id=None)
        with self._run_traces_lock:
            if run_id not in self._run_traces:
                self._run_traces[run_id] = binding
                self._trace_scope_references[run_id] = 1
            else:
                if self._run_traces[run_id] != binding:
                    raise ValueError(f"run_id {run_id!r} already has different active trace correlation")
                self._trace_scope_references[run_id] += 1
        try:
            yield
        finally:
            with self._run_traces_lock:
                remaining = self._trace_scope_references[run_id] - 1
                if remaining > 0:
                    self._trace_scope_references[run_id] = remaining
                else:
                    if self._run_traces[run_id] != binding:
                        raise RuntimeError("active trace scope binding changed before final release")
                    del self._trace_scope_references[run_id]
                    del self._run_traces[run_id]

    @contextmanager
    def _make_span(
        self,
        name: EngineSpanName,
        attributes: dict[str, Any],
        *,
        run_id: str | None = None,
        trace_started_at: datetime | None = None,
        register_run_root: bool = False,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span with attributes, or yield no-op if tracing is disabled.

        Args:
            name: Span name (e.g., "run", "source:csv", "transform:field_mapper")
            attributes: Key-value pairs to set on the span. Values must already
                be in their final form (e.g., token_ids already converted to tuple).
        """
        if self._tracer is not None:
            with self._tracer.start_as_current_span(name.value) as tracer_span:
                for key, value in attributes.items():
                    tracer_span.set_attribute(key, value)
                yield tracer_span
            return

        if self._telemetry_emit is None:
            yield self._NOOP_SPAN
            return

        active_context = copy_context()
        active_parent = active_context[self._active_span] if self._active_span in active_context else None
        if active_parent is not None:
            if register_run_root:
                raise ValueError("run span cannot be nested inside another engine span")
            if run_id is not None and run_id != active_parent.run_id:
                raise ValueError("explicit run_id disagrees with active engine span")
            resolved_run_id = active_parent.run_id
            resolved_trace_started_at = active_parent.trace_started_at
            parent_span_id = active_parent.span_id
        else:
            if run_id is None:
                raise ValueError("telemetry-backed top-level engine span requires explicit run_id")
            resolved_run_id = run_id
            with self._run_traces_lock:
                run_trace = self._run_traces[run_id] if run_id in self._run_traces else None
            if register_run_root:
                if trace_started_at is None:
                    raise ValueError("run span requires canonical trace_started_at")
                resolved_trace_started_at = _canonical_trace_started_at(trace_started_at)
                parent_span_id = None
            elif run_trace is not None:
                normalized_trace_started_at = _canonical_trace_started_at(trace_started_at) if trace_started_at is not None else None
                if normalized_trace_started_at is not None and normalized_trace_started_at != run_trace.trace_started_at:
                    raise ValueError("explicit trace_started_at disagrees with active run correlation")
                resolved_trace_started_at = run_trace.trace_started_at
                parent_span_id = run_trace.root_span_id
            elif trace_started_at is not None:
                resolved_trace_started_at = _canonical_trace_started_at(trace_started_at)
                parent_span_id = None
            else:
                raise ValueError(f"run_id {run_id!r} has no active trace correlation")

        span_id = _new_span_id()
        root_binding: _RunTrace | None = None
        if register_run_root:
            root_binding = _RunTrace(trace_started_at=resolved_trace_started_at, root_span_id=span_id)
            with self._run_traces_lock:
                if resolved_run_id in self._run_traces:
                    raise ValueError(f"run_id {resolved_run_id!r} already has active trace correlation")
                self._run_traces[resolved_run_id] = root_binding

        started_at = datetime.now(UTC)
        started_monotonic = perf_counter()
        safe_attributes = _bounded_span_attributes(attributes)
        telemetry_span = _TelemetrySpan(name, safe_attributes)
        token = self._active_span.set(
            _ActiveEngineSpan(
                run_id=resolved_run_id,
                trace_started_at=resolved_trace_started_at,
                span_id=span_id,
            )
        )
        outcome = _SpanYieldOutcome()
        try:
            with outcome:
                yield telemetry_span
        finally:
            caught = outcome.exception
            self._active_span.reset(token)
            completed_at = started_at + timedelta(seconds=perf_counter() - started_monotonic)
            status = EngineSpanStatus.ERROR if caught is not None else telemetry_span._status
            exception_type = _safe_exception_type(caught) if caught is not None else telemetry_span._exception_type
            try:
                # Construction and correlation cleanup stay outside callback
                # suppression so invariant failures remain loud.
                completion = EngineSpanCompleted(
                    timestamp=completed_at,
                    run_id=resolved_run_id,
                    name=name,
                    started_at=started_at,
                    trace_started_at=resolved_trace_started_at,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    status=status,
                    exception_type=exception_type,
                    attributes=safe_attributes,
                )
                if caught is None:
                    self._telemetry_emit(completion)
                else:
                    # Scope suppression to the callback invocation while the
                    # workload exception is already unwinding. Letting this
                    # finally finish preserves its original exception chain.
                    callback_failure = _CompletionCallbackFailure()
                    with callback_failure:
                        self._telemetry_emit(completion)
                    if callback_failure.exception is not None:
                        callback_type = _safe_exception_type(callback_failure.exception)
                        _add_safe_exception_note(
                            caught,
                            f"Engine span completion callback also failed with {callback_type}",
                        )
            finally:
                if root_binding is not None:
                    with self._run_traces_lock:
                        if self._run_traces[resolved_run_id] != root_binding:
                            raise RuntimeError("active run span binding changed before final release")
                        del self._run_traces[resolved_run_id]

    @contextmanager
    def run_span(
        self,
        run_id: str,
        *,
        trace_started_at: datetime | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for the entire run.

        Args:
            run_id: Run identifier

        Yields:
            Span or NoOpSpan if tracing disabled (never None - uniform interface)
        """
        with self._make_span(
            EngineSpanName.RUN,
            {"run.id": run_id},
            run_id=run_id,
            trace_started_at=trace_started_at,
            register_run_root=True,
        ) as span:
            yield span

    @contextmanager
    def source_span(
        self,
        source_name: str,
        *,
        run_id: str | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for source loading.

        Args:
            source_name: Name of the source plugin

        Yields:
            Span or NoOpSpan
        """
        with self._make_span(
            EngineSpanName.SOURCE,
            {"plugin.name": source_name, "plugin.type": "source"},
            run_id=run_id,
        ) as span:
            yield span

    @contextmanager
    def row_span(
        self,
        row_id: str,
        token_id: str,
        *,
        run_id: str | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for processing a row.

        Args:
            row_id: Row identifier
            token_id: Token identifier

        Yields:
            Span or NoOpSpan
        """
        with self._make_span(
            EngineSpanName.ROW,
            {"row.id": row_id, "token.id": token_id},
            run_id=run_id,
        ) as span:
            yield span

    @contextmanager
    def transform_span(
        self,
        transform_name: str,
        *,
        node_id: str | None = None,
        input_hash: str | None = None,
        token_id: str | None = None,
        token_ids: Sequence[str] | None = None,
        run_id: str | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for a transform operation.

        Args:
            transform_name: Name of the transform plugin
            node_id: Unique node identifier for disambiguation
            input_hash: Optional input data hash
            token_id: Token identifier for single-row transforms
            token_ids: Token identifiers for batch transforms (aggregation flush)

        Note:
            Use token_id for single-row transforms (most common case).
            Use token_ids for batch/aggregation transforms that process multiple tokens.
            These are mutually exclusive - if both provided, token_ids takes precedence.

            node_id enables correlation with Landscape node_states when multiple
            instances of the same plugin type exist in a pipeline.

        Yields:
            Span or NoOpSpan
        """
        attrs: dict[str, Any] = {"plugin.name": transform_name, "plugin.type": "transform"}
        if node_id is not None:
            attrs["node.id"] = node_id
        if input_hash is not None:
            attrs["input.hash"] = input_hash
        # Token tracking for accurate child token attribution
        if token_ids is not None:
            attrs["token.ids"] = tuple(token_ids)
        elif token_id is not None:
            attrs["token.id"] = token_id
        with self._make_span(EngineSpanName.TRANSFORM, attrs, run_id=run_id) as span:
            yield span

    @contextmanager
    def gate_span(
        self,
        gate_name: str,
        *,
        node_id: str | None = None,
        input_hash: str | None = None,
        token_id: str | None = None,
        run_id: str | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for a gate operation.

        Args:
            gate_name: Name of the gate (from GateSettings)
            node_id: Unique node identifier for disambiguation
            input_hash: Optional input data hash
            token_id: Token identifier for the token being evaluated

        Yields:
            Span or NoOpSpan
        """
        attrs: dict[str, Any] = {"plugin.name": gate_name, "plugin.type": "gate"}
        if node_id is not None:
            attrs["node.id"] = node_id
        if input_hash is not None:
            attrs["input.hash"] = input_hash
        if token_id is not None:
            attrs["token.id"] = token_id
        with self._make_span(EngineSpanName.GATE, attrs, run_id=run_id) as span:
            yield span

    @contextmanager
    def aggregation_span(
        self,
        aggregation_name: str,
        *,
        node_id: str | None = None,
        input_hash: str | None = None,
        batch_id: str | None = None,
        token_ids: Sequence[str] | None = None,
        run_id: str | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for an aggregation flush.

        Args:
            aggregation_name: Name of the aggregation plugin
            node_id: Unique node identifier for disambiguation
            input_hash: Input data hash for trace-to-audit correlation
            batch_id: Optional batch identifier
            token_ids: Token identifiers in the batch

        Note:
            Aggregation batches process multiple tokens, so this uses token_ids (plural).
            The token.ids attribute is a tuple for OpenTelemetry compatibility.

        Yields:
            Span or NoOpSpan
        """
        attrs: dict[str, Any] = {"plugin.name": aggregation_name, "plugin.type": "aggregation"}
        if node_id is not None:
            attrs["node.id"] = node_id
        if input_hash is not None:
            attrs["input.hash"] = input_hash
        if batch_id is not None:
            attrs["batch.id"] = batch_id
        if token_ids is not None:
            attrs["token.ids"] = tuple(token_ids)
        with self._make_span(EngineSpanName.AGGREGATION, attrs, run_id=run_id) as span:
            yield span

    @contextmanager
    def sink_span(
        self,
        sink_name: str,
        *,
        node_id: str | None = None,
        token_ids: Sequence[str] | None = None,
        run_id: str | None = None,
    ) -> Iterator["Span | NoOpSpan | _TelemetrySpan"]:
        """Create a span for a sink write.

        Args:
            sink_name: Name of the sink plugin
            node_id: Unique node identifier for disambiguation
            token_ids: Token identifiers being written in this batch

        Note:
            Sinks batch-write multiple tokens, so this uses token_ids (plural).
            The token.ids attribute is a tuple for OpenTelemetry compatibility.

        Yields:
            Span or NoOpSpan
        """
        attrs: dict[str, Any] = {"plugin.name": sink_name, "plugin.type": "sink"}
        if node_id is not None:
            attrs["node.id"] = node_id
        if token_ids is not None:
            attrs["token.ids"] = tuple(token_ids)
        with self._make_span(EngineSpanName.SINK, attrs, run_id=run_id) as span:
            yield span
