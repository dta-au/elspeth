"""Bounded operator metrics projected from durable Composer audit facts."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

import structlog
from opentelemetry import metrics

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus

ComposerTelemetrySurface = Literal["freeform", "guided"]
ComposerRequestTerminalStatus = Literal["completed", "failed", "timed_out", "cancelled"]

_SURFACES = frozenset({"freeform", "guided"})
_REQUEST_STATUSES = frozenset({"completed", "failed", "timed_out", "cancelled"})
_METER = metrics.get_meter(__name__)
_PROVIDER_CALL_COUNTER = _METER.create_counter(
    "composer.llm_call.total",
    unit="1",
    description="Count of durably audited Composer provider calls by surface and closed status.",
)
_PROVIDER_CALL_DURATION = _METER.create_histogram(
    "composer.llm_call.duration",
    unit="s",
    description="Duration of durably audited Composer provider calls by surface and closed status.",
)
_REQUEST_DURATION = _METER.create_histogram(
    "composer.request.duration",
    unit="s",
    description="Composer request duration by surface and closed terminal status.",
)
_REQUEST_PROVIDER_CALLS = _METER.create_histogram(
    "composer.request.provider_calls",
    unit="{call}",
    description="Durably audited provider-call count per Composer request.",
)
_log = structlog.get_logger(__name__)


@dataclass(slots=True)
class _RequestMetricsState:
    surface: ComposerTelemetrySurface
    started_monotonic: float
    provider_call_count: int = 0
    terminal_status: ComposerRequestTerminalStatus | None = None
    provider_terminal_status: ComposerRequestTerminalStatus | None = None


@dataclass(frozen=True, slots=True)
class _ProviderCallFact:
    status: ComposerLLMCallStatus
    latency_ms: int


_REQUEST_METRICS_STATE: ContextVar[_RequestMetricsState | None] = ContextVar(
    "_REQUEST_METRICS_STATE",
    default=None,
)


def _log_projection_failure(*, operation: str, error_type: str) -> None:
    try:
        _log.error(
            "composer_operator_metric_projection_failed",
            operation=operation,
            error_type=error_type,
        )
    except Exception:
        # Telemetry and its fallback logger are both subordinate to the
        # already-committed audit outcome.
        return


def begin_composer_request_metrics(
    *,
    surface: ComposerTelemetrySurface,
) -> Token[_RequestMetricsState | None]:
    """Start one task-local request aggregate before provider work begins."""

    if surface not in _SURFACES:
        raise ValueError("Composer telemetry surface is outside the closed vocabulary")
    return _REQUEST_METRICS_STATE.set(
        _RequestMetricsState(
            surface=surface,
            started_monotonic=time.monotonic(),
        )
    )


def record_settled_composer_provider_calls(
    calls: tuple[ComposerLLMCall, ...],
    *,
    surface: ComposerTelemetrySurface,
) -> None:
    """Project provider calls only after their audit cohort commits."""

    if surface not in _SURFACES or type(calls) is not tuple or any(type(call) is not ComposerLLMCall for call in calls):
        _log_projection_failure(operation="provider_calls", error_type="InvalidProjectionInput")
        return
    _record_provider_call_facts(
        tuple(_ProviderCallFact(status=call.status, latency_ms=call.latency_ms) for call in calls),
        surface=surface,
    )


def _record_provider_call_facts(
    facts: tuple[_ProviderCallFact, ...],
    *,
    surface: ComposerTelemetrySurface,
) -> None:
    state = _REQUEST_METRICS_STATE.get()
    if state is not None and state.surface == surface:
        state.provider_call_count += len(facts)
        if facts:
            final_status = facts[-1].status
            if final_status is ComposerLLMCallStatus.SUCCESS:
                state.provider_terminal_status = None
            elif final_status is ComposerLLMCallStatus.TIMEOUT:
                state.provider_terminal_status = "timed_out"
            elif final_status is ComposerLLMCallStatus.CANCELLED:
                state.provider_terminal_status = "cancelled"
            else:
                state.provider_terminal_status = "failed"
    for fact in facts:
        attributes = {
            "surface": surface,
            "status": fact.status.value,
        }
        try:
            _PROVIDER_CALL_COUNTER.add(1, attributes)
            _PROVIDER_CALL_DURATION.record(fact.latency_ms / 1_000, attributes)
        except Exception as exc:
            _log_projection_failure(operation="provider_calls", error_type=type(exc).__name__)


def record_settled_composer_audit_message(
    *,
    role: str,
    writer_principal: str,
    tool_calls: Sequence[Mapping[str, object]] | None,
) -> None:
    """Project one exact freeform LLM sidecar after its row commits.

    ``SessionServiceImpl.add_message`` is a generic internal writer, so this
    boundary parses only the owned audit envelope's two bounded facts. It does
    not deserialize or forward provider/model strings, hashes, content, tool
    names, errors, IDs, or token fields into the metric path.
    """

    if role != "audit" or writer_principal != "compose_loop" or type(tool_calls) not in {list, tuple}:
        return
    exact_tool_calls = cast("list[Mapping[str, object]] | tuple[Mapping[str, object], ...]", tool_calls)
    if len(exact_tool_calls) != 1:
        return
    envelope = exact_tool_calls[0]
    if type(envelope) not in {dict, MappingProxyType} or set(envelope) != {"_kind", "call"}:
        return
    if envelope["_kind"] != "llm_call_audit":
        return
    raw_call = envelope["call"]
    if type(raw_call) not in {dict, MappingProxyType}:
        return
    exact_call = cast("Mapping[str, object]", raw_call)
    raw_status = exact_call.get("status")
    latency_ms = exact_call.get("latency_ms")
    if type(raw_status) is not str or type(latency_ms) is not int or latency_ms < 0:
        return
    try:
        status = ComposerLLMCallStatus(raw_status)
    except ValueError:
        return
    _record_provider_call_facts(
        (_ProviderCallFact(status=status, latency_ms=latency_ms),),
        surface="freeform",
    )


def mark_composer_request_terminal(status: ComposerRequestTerminalStatus) -> None:
    """Retain an explicit route outcome for the enclosing request aggregate."""

    state = _REQUEST_METRICS_STATE.get()
    if state is not None and status in _REQUEST_STATUSES:
        state.terminal_status = status


def finish_composer_request_metrics(
    token: Token[_RequestMetricsState | None],
    *,
    status: ComposerRequestTerminalStatus,
) -> None:
    """Project one request aggregate and restore the prior task context."""

    state = _REQUEST_METRICS_STATE.get()
    try:
        _REQUEST_METRICS_STATE.reset(token)
    except (RuntimeError, ValueError) as exc:
        _log_projection_failure(operation="request", error_type=type(exc).__name__)
        return
    if state is None or status not in _REQUEST_STATUSES:
        _log_projection_failure(operation="request", error_type="InvalidProjectionInput")
        return
    terminal_status = state.terminal_status or (status if status != "completed" else state.provider_terminal_status) or status
    attributes = {
        "surface": state.surface,
        "status": terminal_status,
    }
    try:
        _REQUEST_DURATION.record(max(0.0, time.monotonic() - state.started_monotonic), attributes)
        _REQUEST_PROVIDER_CALLS.record(state.provider_call_count, attributes)
    except Exception as exc:
        _log_projection_failure(operation="request", error_type=type(exc).__name__)


__all__ = [
    "ComposerRequestTerminalStatus",
    "ComposerTelemetrySurface",
    "begin_composer_request_metrics",
    "finish_composer_request_metrics",
    "mark_composer_request_terminal",
    "record_settled_composer_audit_message",
    "record_settled_composer_provider_calls",
]
