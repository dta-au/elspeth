"""Optional title SDK latency must leave the real Composer operation usable."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import litellm
import pytest
from litellm.exceptions import Timeout as LiteLLMTimeout
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.models import quota_provider_attempts_table, session_operation_fences_table, token_usage_ledger_table
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _TestSnapshotFactory


async def _title_timeout_app(tmp_path, monkeypatch, *, quota_enabled=False, title_decision="allow", guard_delay=0.0, operation_budget=8.0):
    """Keep the worker, guard enforcer and all admission/settlement SQL real."""
    app, service = _make_app(tmp_path, quota_enabled=quota_enabled)
    guard_settings = PromptGuardSettings(endpoint="https://shield.example.test", subscription_key="offline-test-key")
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard_settings,
            "composer_advisor_prompt_guard": guard_settings,
            "composer_timeout_seconds": operation_budget,
        }
    )
    guards = []

    async def inspect(_self, request):
        title = any("Conversation excerpt (UNTRUSTED USER DATA" in document for document in request.documents)
        guards.append((title, request))
        if title:
            await asyncio.sleep(guard_delay)
        disposition = title_decision if title else "allow"
        reason = {"allow": "clean", "blocked": "policy_blocked", "unavailable": "service_error"}[disposition]
        return PromptGuardDecision(disposition, reason)

    monkeypatch.setattr("elspeth.web.composer.prompt_guard_clients.AzurePromptGuardClient.inspect", inspect)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    app.state.composer_service = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(), app.state.settings, sessions_service=service, session_engine=app.state.session_engine
    )
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    return app, service, guards


def _fast_completion(kwargs):
    content = (
        json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"})
        if "response_format" in kwargs
        else "I can help build a pipeline."
    )
    return litellm.ModelResponse(
        model=kwargs["model"],
        choices=[{"message": {"role": "assistant", "content": content, "tool_calls": None}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7, "cost": 0.001},
    )


def _durable_provider_rows(service, session_id):
    with service._engine.connect() as connection:
        attempts = connection.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session_id))
        ).all()
        ledger = connection.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session_id))).all()
        fence = connection.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id))
        ).one()
    return attempts, ledger, fence


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_enabled", [False, True], ids=["no-token-cap", "configured-token-cap"])
async def test_slow_title_sdk_times_out_before_original_operation_deadline(tmp_path, monkeypatch, quota_enabled):
    # Leave real guard, SQL and END review headroom after the five-second SDK
    # timeout. The unbounded twenty-second title still cannot fit this deadline.
    app, service, guards = await _title_timeout_app(tmp_path, monkeypatch, quota_enabled=quota_enabled, operation_budget=12.0)
    session = await service.create_session("alice", "New session", "local")
    title_stopped = asyncio.Event()
    title_kwargs = []
    title_times = []
    primary_times = []

    async def completion(**kwargs):
        if kwargs.get("max_tokens") == 40 and "tools" not in kwargs:
            title_kwargs.append(kwargs)
            title_times.append(time.monotonic())
            try:
                # An offline SDK obeying LiteLLM's real timeout argument. An
                # absent timeout leaves the worker's admitted deadline in charge.
                async with asyncio.timeout(kwargs.get("timeout")):
                    await asyncio.sleep(20)
            except TimeoutError as exc:
                raise LiteLLMTimeout(message="offline title model timeout", model=kwargs["model"], llm_provider="openai") from exc
            finally:
                title_times.append(time.monotonic())
                title_stopped.set()
            raise AssertionError("The deliberately slow title SDK must time out")
        assert title_stopped.is_set(), "Primary dispatch must follow title SDK settlement"
        attempts, ledger, _fence = _durable_provider_rows(service, session.id)
        title_attempts = [attempt for attempt in attempts if attempt.source == "auto_title"]
        assert len(title_attempts) == 1 and title_attempts[0].settled_at is not None
        title_usage = [entry for entry in ledger if entry.source == "auto_title"]
        assert len(title_usage) == 1 and title_usage[0].prompt_tokens is None and title_usage[0].completion_tokens is None
        primary_times.append(time.monotonic())
        return _fast_completion(kwargs)

    monkeypatch.setattr(litellm, "acompletion", completion)
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(
            client, app, path=f"/api/sessions/{session.id}/messages", body=message_body("What can you help me build?")
        )
    elapsed = time.monotonic() - started
    if quota_enabled:
        # An unknown billable title outcome must not become invented zero usage.
        status, body = settled.error()
        assert status == 403 and body["detail"]["failure_code"] == "admission_refused", body
        assert body["detail"]["detail"] == "Composer admission refused: token_accounting_unavailable."
        assert primary_times == []
    else:
        assert settled.final.json()["status"] == "completed", settled.final.json()
        # The ordinary empty-graph footer still accompanies the model's reply.
        assert settled.result()["message"]["content"].startswith("I can help build a pipeline.\n\n---\n\n[ELSPETH-SYSTEM]")
        assert primary_times and primary_times[0] >= title_times[-1]
    assert len(title_kwargs) == 1 and title_kwargs[0]["timeout"] == 5.0
    assert title_kwargs[0]["num_retries"] == title_kwargs[0]["max_retries"] == 0
    assert 5.0 <= title_times[1] - title_times[0] < 8.0
    assert elapsed < 12.0
    assert title_stopped.is_set() and guards[0][0] is True
    attempts, ledger, fence = _durable_provider_rows(service, session.id)
    assert len(attempts) == len(ledger) == 1 + len(primary_times)
    assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
    assert fence.released_at is not None
    rows = await service.get_messages(session.id, limit=None, include_private_evidence=True)
    calls = [envelope["call"] for row in rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "llm_call_audit"]
    title_attempt = next(attempt for attempt in attempts if attempt.source == "auto_title")
    title_call = next(call for call in calls if call["call_id"] == title_attempt.attempt_id)
    assert title_call["status"] == "timeout" and title_call["error_class"] == "Timeout"
    assert title_call["prompt_tokens"] is None and title_call["completion_tokens"] is None
    assert len(calls) == len(attempts), "Every real provider attempt must retain exactly one durable terminal audit"


@pytest.mark.asyncio
async def test_title_sdk_timeout_does_not_bound_guard_or_required_settlement(tmp_path, monkeypatch):
    # A short SDK budget makes both guard and SQL settlement visibly longer,
    # without replacing the worker's clock or its admitted outer deadline.
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_PROVIDER_TIMEOUT_SECONDS", 0.02, raising=False)
    app, service, guards = await _title_timeout_app(tmp_path, monkeypatch, guard_delay=0.06)
    session = await service.create_session("alice", "New session", "local")
    title_settled = asyncio.Event()
    real_finish = service.finish_provider_attempt
    title_kwargs = []
    primary_times = []

    async def finish_provider_attempt(**kwargs):
        if kwargs["required_work"].key.source is RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL:
            await asyncio.sleep(0.06)
            await real_finish(**kwargs)
            title_settled.set()
        else:
            await real_finish(**kwargs)

    async def completion(**kwargs):
        if kwargs.get("max_tokens") == 40 and "tools" not in kwargs:
            title_kwargs.append(kwargs)
            return litellm.ModelResponse(
                model=kwargs["model"],
                choices=[{"message": {"role": "assistant", "content": "Useful Pipeline"}, "finish_reason": "stop"}],
                usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7, "cost": 0.001},
            )
        assert title_settled.is_set()
        primary_times.append(time.monotonic())
        return _fast_completion(kwargs)

    monkeypatch.setattr(service, "finish_provider_attempt", finish_provider_attempt)
    monkeypatch.setattr(litellm, "acompletion", completion)
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(
            client, app, path=f"/api/sessions/{session.id}/messages", body=message_body("What can you help me build?")
        )
    assert settled.final.json()["status"] == "completed", settled.final.json()
    assert len(title_kwargs) == 1 and title_kwargs[0]["timeout"] == 0.02
    assert primary_times[0] - started >= 0.12
    assert title_settled.is_set() and guards[0][0] is True
    attempts, ledger, fence = _durable_provider_rows(service, session.id)
    assert len(attempts) == len(ledger) == 1 + len(primary_times)
    assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
    assert fence.released_at is not None
    updated = await service.get_session(session.id)
    assert updated is not None and updated.title == "Useful Pipeline"


@pytest.mark.asyncio
@pytest.mark.parametrize(("title_decision", "expected_status"), [("blocked", 422), ("unavailable", 503)])
async def test_title_sdk_timeout_never_turns_guard_refusal_into_primary_dispatch(tmp_path, monkeypatch, title_decision, expected_status):
    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_PROVIDER_TIMEOUT_SECONDS", 0.02, raising=False)
    app, service, guards = await _title_timeout_app(tmp_path, monkeypatch, title_decision=title_decision, guard_delay=0.06)
    sdk_calls = []

    async def must_not_dispatch(**kwargs):
        sdk_calls.append(kwargs)
        raise AssertionError("A typed title guard refusal must precede all SDK dispatch")

    monkeypatch.setattr(litellm, "acompletion", must_not_dispatch)
    session = await service.create_session("alice", "New session", "local")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(client, app, path=f"/api/sessions/{session.id}/messages", body=message_body("Build it."))
    status, body = settled.error()
    assert status == expected_status and body["detail"]["error_type"] == f"prompt_guard_{title_decision}"
    assert len(guards) == 1 and guards[0][0] is True and sdk_calls == []
    attempts, ledger, fence = _durable_provider_rows(service, session.id)
    assert attempts == ledger == [] and fence.released_at is not None
    rows = await service.get_messages(session.id, limit=None)
    checks = [envelope["check"] for row in rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "prompt_guard_audit"]
    assert len(checks) == 1 and checks[0]["audit_id"] == body["detail"]["audit_id"]
    assert checks[0]["disposition"] == title_decision
