"""Auto-title threads the PRIMARY-role endpoint affordance (Phase 3 Task 2).

Auto-title always uses the primary composer role (see
sessions/routes/messages.py:272) — there is no advisor parameter on
``maybe_auto_title_session`` at all, so the role asymmetry is structural.
Mirrors the structure of test_auto_title_sampling_config.py.
"""

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

_SENTINEL_CREDENTIAL = "sk-auto-title-endpoint-affordance-sentinel"  # secret-scan: allow-this-line
_TEST_SESSION_ID = uuid4()
_TEST_CONTEXT = SessionOperationContext(
    fence=SessionOperationFence(
        session_id=str(_TEST_SESSION_ID),
        operation_id="auto-title-endpoint-operation",
        lease_token="auto-title-endpoint-token",
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
async def test_auto_title_omits_endpoint_kwargs_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _completion("My Title")

    monkeypatch.setattr(at, "_litellm_acompletion", fake_acompletion)

    await at.maybe_auto_title_session(
        service=_TitleService(),
        session_id=_TEST_SESSION_ID,
        user_message="Build a CSV pipeline",
        model="gpt-5",
        temperature=None,
        seed=None,
        session_operation_context=_TEST_CONTEXT,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured


@pytest.mark.asyncio
async def test_auto_title_sends_configured_primary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
        temperature=None,
        seed=None,
        session_operation_context=_TEST_CONTEXT,
        api_base="https://primary-gateway.example.test/v1",
        api_key=_SENTINEL_CREDENTIAL,
    )

    assert captured["api_base"] == "https://primary-gateway.example.test/v1"
    assert captured["api_key"] == _SENTINEL_CREDENTIAL
