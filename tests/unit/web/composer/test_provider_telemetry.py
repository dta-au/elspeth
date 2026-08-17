"""Bounded operator projection for durably settled Composer provider calls."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass, field
from datetime import UTC, datetime

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.web.composer.audit import llm_call_audit_envelope


def test_provider_telemetry_projector_surface_exists() -> None:
    module_name = "elspeth.web.composer.provider_telemetry"
    assert importlib.util.find_spec(module_name) is not None
    module = importlib.import_module(module_name)

    assert callable(module.record_settled_composer_provider_calls)


@dataclass
class _Instrument:
    points: list[tuple[int | float, dict[str, str]]] = field(default_factory=list)

    def add(self, value: int | float, attributes: dict[str, str]) -> None:
        self.points.append((value, dict(attributes)))

    def record(self, value: int | float, attributes: dict[str, str]) -> None:
        self.points.append((value, dict(attributes)))


def _call(*, status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS, latency_ms: int = 42) -> ComposerLLMCall:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    failed = status is not ComposerLLMCallStatus.SUCCESS
    return ComposerLLMCall(
        model_requested="SECRET-MODEL-CANARY",
        model_returned=None if failed else "SECRET-RETURNED-MODEL",
        status=status,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        latency_ms=latency_ms,
        provider_request_id="SECRET-PROVIDER-REQUEST-ID",
        messages_hash="a" * 64,
        tools_spec_hash="b" * 64,
        declared_tool_names=("secret_selected_tool",),
        started_at=now,
        finished_at=now,
        error_class="SECRET-ERROR-CLASS" if failed else None,
        error_message="SECRET-ERROR-MESSAGE" if failed else None,
        temperature=None,
        seed=None,
    )


def test_settled_provider_call_projects_only_closed_surface_and_status(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    count = _Instrument()
    duration = _Instrument()
    monkeypatch.setattr(module, "_PROVIDER_CALL_COUNTER", count)
    monkeypatch.setattr(module, "_PROVIDER_CALL_DURATION", duration)

    module.record_settled_composer_provider_calls((_call(),), surface="freeform")

    attributes = {"surface": "freeform", "status": "success"}
    assert count.points == [(1, attributes)]
    assert duration.points == [(0.042, attributes)]


def test_metric_recorder_failure_cannot_replace_settled_outcome(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")

    class _FailingInstrument:
        def add(self, _value: int, _attributes: dict[str, str]) -> None:
            raise RuntimeError("SECRET-EXPORTER-FAILURE")

        def record(self, _value: float, _attributes: dict[str, str]) -> None:
            raise RuntimeError("SECRET-EXPORTER-FAILURE")

    monkeypatch.setattr(module, "_PROVIDER_CALL_COUNTER", _FailingInstrument())
    monkeypatch.setattr(module, "_PROVIDER_CALL_DURATION", _FailingInstrument())

    assert module.record_settled_composer_provider_calls((_call(),), surface="guided") is None


def test_settled_freeform_audit_message_projects_only_owned_call_facts(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    count = _Instrument()
    duration = _Instrument()
    monkeypatch.setattr(module, "_PROVIDER_CALL_COUNTER", count)
    monkeypatch.setattr(module, "_PROVIDER_CALL_DURATION", duration)

    module.record_settled_composer_audit_message(
        role="audit",
        writer_principal="compose_loop",
        tool_calls=[llm_call_audit_envelope(_call(latency_ms=73))],
    )

    attributes = {"surface": "freeform", "status": "success"}
    assert count.points == [(1, attributes)]
    assert duration.points == [(0.073, attributes)]


def test_non_composer_or_malformed_audit_message_cannot_fabricate_metrics(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    count = _Instrument()
    duration = _Instrument()
    monkeypatch.setattr(module, "_PROVIDER_CALL_COUNTER", count)
    monkeypatch.setattr(module, "_PROVIDER_CALL_DURATION", duration)
    valid = llm_call_audit_envelope(_call())
    invalid_messages = (
        {"role": "user", "writer_principal": "compose_loop", "tool_calls": [valid]},
        {"role": "audit", "writer_principal": "run_diagnostics", "tool_calls": [valid]},
        {"role": "audit", "writer_principal": "compose_loop", "tool_calls": []},
        {
            "role": "audit",
            "writer_principal": "compose_loop",
            "tool_calls": [{"_kind": "llm_call_audit", "call": {"status": "invented", "latency_ms": 42}}],
        },
        {
            "role": "audit",
            "writer_principal": "compose_loop",
            "tool_calls": [{"_kind": "llm_call_audit", "call": {"status": "success", "latency_ms": True}}],
        },
    )

    for message in invalid_messages:
        module.record_settled_composer_audit_message(**message)

    assert count.points == []
    assert duration.points == []


def test_request_projection_records_duration_and_settled_provider_call_count(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    request_duration = _Instrument()
    request_calls = _Instrument()
    monkeypatch.setattr(module, "_REQUEST_DURATION", request_duration)
    monkeypatch.setattr(module, "_REQUEST_PROVIDER_CALLS", request_calls)
    moments = iter((100.0, 103.5))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(moments))

    token = module.begin_composer_request_metrics(surface="guided")
    module.record_settled_composer_provider_calls(
        (
            _call(status=ComposerLLMCallStatus.TIMEOUT, latency_ms=1200),
            _call(status=ComposerLLMCallStatus.CANCELLED, latency_ms=7),
        ),
        surface="guided",
    )
    module.finish_composer_request_metrics(token, status="timed_out")

    attributes = {"surface": "guided", "status": "timed_out"}
    assert request_duration.points == [(3.5, attributes)]
    assert request_calls.points == [(2, attributes)]


def test_request_projection_uses_final_durable_provider_outcome(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    request_duration = _Instrument()
    request_calls = _Instrument()
    monkeypatch.setattr(module, "_REQUEST_DURATION", request_duration)
    monkeypatch.setattr(module, "_REQUEST_PROVIDER_CALLS", request_calls)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)

    timed_out = module.begin_composer_request_metrics(surface="guided")
    module.record_settled_composer_provider_calls(
        (_call(status=ComposerLLMCallStatus.TIMEOUT),),
        surface="guided",
    )
    module.finish_composer_request_metrics(timed_out, status="completed")

    recovered = module.begin_composer_request_metrics(surface="guided")
    module.record_settled_composer_provider_calls(
        (
            _call(status=ComposerLLMCallStatus.API_ERROR),
            _call(status=ComposerLLMCallStatus.SUCCESS),
        ),
        surface="guided",
    )
    module.finish_composer_request_metrics(recovered, status="completed")

    assert request_duration.points == [
        (0.0, {"surface": "guided", "status": "timed_out"}),
        (0.0, {"surface": "guided", "status": "completed"}),
    ]


def test_explicit_route_terminal_status_overrides_provider_recovery(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    request_duration = _Instrument()
    monkeypatch.setattr(module, "_REQUEST_DURATION", request_duration)
    monkeypatch.setattr(module, "_REQUEST_PROVIDER_CALLS", _Instrument())
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)

    token = module.begin_composer_request_metrics(surface="freeform")
    module.mark_composer_request_terminal("timed_out")
    module.record_settled_composer_provider_calls((_call(),), surface="freeform")
    module.finish_composer_request_metrics(token, status="completed")

    assert request_duration.points == [(0.0, {"surface": "freeform", "status": "timed_out"})]


def test_request_aggregate_ignores_mismatched_surface_calls(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    request_duration = _Instrument()
    request_calls = _Instrument()
    monkeypatch.setattr(module, "_REQUEST_DURATION", request_duration)
    monkeypatch.setattr(module, "_REQUEST_PROVIDER_CALLS", request_calls)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)

    token = module.begin_composer_request_metrics(surface="guided")
    module.record_settled_composer_provider_calls(
        (_call(status=ComposerLLMCallStatus.TIMEOUT),),
        surface="freeform",
    )
    module.finish_composer_request_metrics(token, status="completed")

    attributes = {"surface": "guided", "status": "completed"}
    assert request_duration.points == [(0.0, attributes)]
    assert request_calls.points == [(0, attributes)]


def test_later_settled_success_clears_prior_provider_failure(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    request_duration = _Instrument()
    request_calls = _Instrument()
    monkeypatch.setattr(module, "_REQUEST_DURATION", request_duration)
    monkeypatch.setattr(module, "_REQUEST_PROVIDER_CALLS", request_calls)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)

    token = module.begin_composer_request_metrics(surface="freeform")
    module.record_settled_composer_provider_calls(
        (_call(status=ComposerLLMCallStatus.TIMEOUT),),
        surface="freeform",
    )
    module.record_settled_composer_provider_calls((_call(),), surface="freeform")
    module.finish_composer_request_metrics(token, status="completed")

    attributes = {"surface": "freeform", "status": "completed"}
    assert request_duration.points == [(0.0, attributes)]
    assert request_calls.points == [(2, attributes)]


def test_zero_call_replay_does_not_reuse_prior_request_count(monkeypatch) -> None:
    module = importlib.import_module("elspeth.web.composer.provider_telemetry")
    request_calls = _Instrument()
    monkeypatch.setattr(module, "_REQUEST_DURATION", _Instrument())
    monkeypatch.setattr(module, "_REQUEST_PROVIDER_CALLS", request_calls)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)

    first = module.begin_composer_request_metrics(surface="guided")
    module.record_settled_composer_provider_calls((_call(),), surface="guided")
    module.finish_composer_request_metrics(first, status="completed")

    replay = module.begin_composer_request_metrics(surface="guided")
    module.finish_composer_request_metrics(replay, status="completed")

    attributes = {"surface": "guided", "status": "completed"}
    assert request_calls.points == [(1, attributes), (0, attributes)]
