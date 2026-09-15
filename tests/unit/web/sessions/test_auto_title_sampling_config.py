"""Auto-title uses caller-threaded composer sampling."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import elspeth.web.sessions._auto_title as at
from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    ChargeableOperation,
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageEntry

_TEST_SESSION_ID = uuid4()
_TEST_CONTEXT = SessionOperationContext(
    fence=SessionOperationFence(
        session_id=str(_TEST_SESSION_ID),
        operation_id="auto-title-sampling-operation",
        lease_token="auto-title-sampling-token",
        operation_epoch=2,
    ),
    operation_kind=SessionOperationKind.COMPOSE,
)


class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []
        self.usage: list[tuple[str, object, tuple[TokenUsageEntry, ...]]] = []

    async def begin_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, source: str, run_id: object = None
    ) -> ProviderAttempt:
        assert source == "auto_title"
        assert run_id is None
        decision = await self.assess_chargeable_operation(
            session_operation_context=session_operation_context, operation=ChargeableOperation.AUTO_TITLE
        )
        if not decision.allowed:
            raise ChargeableAdmissionRefused(decision)
        return ProviderAttempt(attempt_id="title-attempt", started_at=datetime.now(UTC))

    async def settle_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, attempt_id: str, entry: TokenUsageEntry
    ) -> None:
        assert attempt_id == "title-attempt"
        await self.record_token_usage(
            session_operation_context=session_operation_context, source="auto_title", run_id=None, entries=(entry,)
        )

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: str,
        run_id: object,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        del session_operation_context
        self.usage.append((source, run_id, entries))
        return tuple(f"entry-{index}" for index in range(len(entries)))

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision:
        assert session_operation_context == _TEST_CONTEXT
        assert operation is ChargeableOperation.AUTO_TITLE
        return ChargeableAdmissionDecision(
            refusal_reason=None,
            evidence=AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash="a" * 64),
        )

    async def update_session_title(
        self,
        session_id: object,
        title: str,
        *,
        session_operation_context: SessionOperationContext,
    ) -> None:
        del session_operation_context
        self.updates.append((session_id, title))


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.asyncio
async def test_auto_title_omits_sampling_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(at, "_litellm_acompletion", fake_acompletion)
    service = _TitleService()

    await at.maybe_auto_title_session(
        service=service,
        session_id=_TEST_SESSION_ID,
        user_message="Build a CSV pipeline",
        model="gpt-5",
        temperature=None,
        seed=None,
        session_operation_context=_TEST_CONTEXT,
    )

    assert "temperature" not in captured
    assert "seed" not in captured
    assert service.updates[0][1] == "My Title"


@pytest.mark.asyncio
async def test_auto_title_sends_configured_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(at, "_litellm_acompletion", fake_acompletion)

    await at.maybe_auto_title_session(
        service=_TitleService(),
        session_id=_TEST_SESSION_ID,
        user_message="Build a CSV pipeline",
        model="gpt-4o",
        temperature=0.0,
        seed=42,
        session_operation_context=_TEST_CONTEXT,
    )

    assert captured["temperature"] == 0.0
    assert captured["seed"] == 42
