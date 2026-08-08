"""Forensic granularity of the unwind-path persist-failure records.

Before the atomic-cohort refactor (793c96a6e / 7e1f1f86e) the persist
helpers looped per row: each lost audit row incremented
``_COMPOSER_PERSIST_FAILED_DURING_UNWIND_COUNTER`` once and emitted its
own slog line naming its model/tool. The cohort refactor collapsed that
to ``.add(1)`` per cohort and a single log line carrying only
``llm_calls[0].model_requested`` — a 12-row evidence loss became
indistinguishable from a 1-row loss on dashboards calibrated to the old
per-row unit, and the log lost every model but the first.

These tests pin the restored contract: the counter records the number of
rows that failed to become durable, and the log lists the full cohort
(``models_requested`` / ``statuses`` mirroring the tool path's
``tool_names``).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.protocol import (
    RunDiagnosticsAuditAuthority,
    RunDiagnosticsAuditDraft,
    RunDiagnosticsAuthorityLostError,
    SessionServiceProtocol,
)
from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes._helpers import (
    _persist_llm_calls,
    _persist_run_diagnostics_llm_calls,
    _persist_tool_invocations,
    _persist_turn_audit_cohort,
)


class _RecordingCounter:
    """Stand-in for the module-level OTel counter; records every add."""

    def __init__(self) -> None:
        self.adds: list[tuple[int | float, dict[str, Any] | None]] = []

    def add(self, amount: int | float, attributes: dict[str, Any] | None = None, context: Any = None) -> None:
        del context
        self.adds.append((amount, dict(attributes) if attributes is not None else None))


@pytest.fixture()
def unwind_counter(monkeypatch: pytest.MonkeyPatch) -> _RecordingCounter:
    counter = _RecordingCounter()
    monkeypatch.setattr(_helpers, "_COMPOSER_PERSIST_FAILED_DURING_UNWIND_COUNTER", counter)
    return counter


@dataclass
class _FailingService:
    """Raises the configured exception from every atomic-persist entrypoint."""

    raise_on_call: Exception = field(default_factory=lambda: OperationalError("INSERT", {}, Exception("db down")))

    async def add_messages_atomic(
        self,
        session_id: UUID,
        drafts: Sequence[AuditMessageDraft],
        *,
        writer_principal: str,
        composition_state_id: UUID | None = None,
    ) -> None:
        raise self.raise_on_call

    async def add_run_diagnostics_audit_messages_atomic(
        self,
        authority: RunDiagnosticsAuditAuthority,
        drafts: Sequence[RunDiagnosticsAuditDraft],
    ) -> None:
        raise self.raise_on_call


def _tool_invocation(tool_name: str) -> ComposerToolInvocation:
    arguments_canonical = canonical_json({"detail": "args"})
    result_canonical = canonical_json({"status": "SUCCESS"})
    return ComposerToolInvocation(
        tool_call_id=f"call_{tool_name}_1",
        tool_name=tool_name,
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode("utf-8")).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode("utf-8")).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=3,
        version_after=3,
        started_at=datetime(2026, 8, 8, tzinfo=UTC),
        finished_at=datetime(2026, 8, 8, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )


def _llm_call(model: str, status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS) -> ComposerLLMCall:
    failed = status is not ComposerLLMCallStatus.SUCCESS
    return build_llm_call_record(
        model_requested=model,
        messages=[{"role": "user", "content": "prompt"}],
        tools=None,
        status=status,
        started_at=datetime(2026, 8, 8, tzinfo=UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        error_class="ProviderAPIError" if failed else None,
        error_message="provider rejected the call" if failed else None,
    )


def _event(captured: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [entry for entry in captured if entry["event"] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} event, got {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_llm_calls_unwind_counts_every_lost_row_and_names_every_model(unwind_counter: _RecordingCounter) -> None:
    calls = (
        _llm_call("provider/model-a"),
        _llm_call("provider/model-b", status=ComposerLLMCallStatus.API_ERROR),
        _llm_call("provider/model-c"),
    )

    with structlog.testing.capture_logs() as captured:
        await _persist_llm_calls(
            cast(SessionServiceProtocol, _FailingService()),
            uuid4(),
            calls,
            None,
            plugin_crash_pending=True,
        )

    assert unwind_counter.adds == [(3, {"helper": "llm_calls"})]
    event = _event(captured, "composer_llm_call_persist_failed_during_unwind")
    assert event["calls"] == 3
    assert event["models_requested"] == ["provider/model-a", "provider/model-b", "provider/model-c"]
    assert event["statuses"] == ["success", "api_error", "success"]


@pytest.mark.asyncio
async def test_tool_invocations_unwind_counts_every_lost_row(unwind_counter: _RecordingCounter) -> None:
    invocations = (
        _tool_invocation("preview_pipeline"),
        _tool_invocation("upsert_node"),
    )

    with structlog.testing.capture_logs() as captured:
        result = await _persist_tool_invocations(
            cast(SessionServiceProtocol, _FailingService()),
            uuid4(),
            invocations,
            None,
            plugin_crash_pending=True,
        )

    assert result == ()
    assert unwind_counter.adds == [(2, {"helper": "tool_invocations"})]
    event = _event(captured, "composer_tool_invocation_persist_failed_during_unwind")
    assert event["tool_names"] == ["preview_pipeline", "upsert_node"]


@pytest.mark.asyncio
async def test_turn_cohort_unwind_counts_both_groups_and_names_every_model(unwind_counter: _RecordingCounter) -> None:
    invocations = (_tool_invocation("preview_pipeline"),)
    calls = (
        _llm_call("provider/model-a"),
        _llm_call("provider/model-b"),
    )

    with structlog.testing.capture_logs() as captured:
        result = await _persist_turn_audit_cohort(
            cast(SessionServiceProtocol, _FailingService()),
            uuid4(),
            invocations,
            calls,
            tool_composition_state_id=None,
            llm_composition_state_id=None,
            plugin_crash_pending=True,
        )

    assert result == ()
    # The whole turn failed to become durable: 1 tool row + 2 LLM sidecars.
    assert unwind_counter.adds == [(3, {"helper": "turn_audit_cohort"})]
    event = _event(captured, "composer_turn_audit_cohort_persist_failed_during_unwind")
    assert event["tool_names"] == ["preview_pipeline"]
    assert event["models_requested"] == ["provider/model-a", "provider/model-b"]


@pytest.mark.asyncio
async def test_run_diagnostics_unwind_counts_every_lost_row_and_names_every_model(unwind_counter: _RecordingCounter) -> None:
    authority = RunDiagnosticsAuditAuthority(run_id=uuid4(), session_id=uuid4(), state_id=uuid4())
    calls = (
        _llm_call("provider/model-a"),
        _llm_call("provider/model-b", status=ComposerLLMCallStatus.API_ERROR),
    )

    with structlog.testing.capture_logs() as captured:
        await _persist_run_diagnostics_llm_calls(
            cast(SessionServiceProtocol, _FailingService()),
            authority,
            calls,
            plugin_crash_pending=True,
        )

    assert unwind_counter.adds == [(2, {"helper": "run_diagnostics_llm_calls"})]
    event = _event(captured, "run_diagnostics_llm_call_persist_failed_during_unwind")
    assert event["calls"] == 2
    assert event["models_requested"] == ["provider/model-a", "provider/model-b"]
    assert event["statuses"] == ["success", "api_error"]


@pytest.mark.asyncio
async def test_run_diagnostics_authority_loss_unwind_counts_every_lost_row(unwind_counter: _RecordingCounter) -> None:
    authority = RunDiagnosticsAuditAuthority(run_id=uuid4(), session_id=uuid4(), state_id=uuid4())
    calls = (_llm_call("provider/model-a"), _llm_call("provider/model-b"))
    service = _FailingService(raise_on_call=RunDiagnosticsAuthorityLostError(authority, reason="run_rebound"))

    with structlog.testing.capture_logs() as captured:
        await _persist_run_diagnostics_llm_calls(
            cast(SessionServiceProtocol, service),
            authority,
            calls,
            plugin_crash_pending=True,
        )

    assert unwind_counter.adds == [(2, {"helper": "run_diagnostics_llm_calls"})]
    event = _event(captured, "run_diagnostics_audit_authority_lost_during_unwind")
    assert event["calls"] == 2
    assert event["reason"] == "run_rebound"
