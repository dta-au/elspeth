"""Local guard input refusals retain their safe cause through real custody."""

from __future__ import annotations

import json

import httpx
import litellm
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_DOCUMENTS, PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, enforce_prompt_guard
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.models import quota_provider_attempts_table, session_operation_fences_table, token_usage_ledger_table
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.composer import test_prompt_guard_admission as guard_admission
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _TestSnapshotFactory

_LOCAL_DETAIL = "The authoring input is outside the supported prompt guard input envelope. The protected dispatch was not sent."
_REMOTE_BODY = "unsupported_input REMOTE-RESPONSE-CANARY"


authority = guard_admission.authority


def _settings() -> PromptGuardSettings:
    return PromptGuardSettings(endpoint="https://shield.example.test", subscription_key="offline-guard-key")


def _remote_response(request: httpx.Request, *, outcome: str, requests: list[httpx.Request]) -> httpx.Response:
    requests.append(request)
    assert request.url.host == "shield.example.test"
    if outcome == "unavailable":
        return httpx.Response(503, text=_REMOTE_BODY)
    assert outcome == "blocked", "A local refusal must not contact Azure"
    payload = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "userPromptAnalysis": {"attackDetected": True},
            "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "mounted", "mode", "expected_status", "expected_code", "expected_reason", "expected_http_calls"),
    [
        ("user_unit", False, "debug-when-unconfigured", 422, "prompt_guard_unsupported_input", "unsupported_input", 0),
        ("document_count", False, "debug-when-unconfigured", 422, "prompt_guard_unsupported_input", "unsupported_input", 0),
        ("user_unit", True, "required", 422, "prompt_guard_unsupported_input", "unsupported_input", 0),
        ("document_count", True, "required", 422, "prompt_guard_unsupported_input", "unsupported_input", 0),
        ("unavailable", True, "required", 503, "prompt_guard_unavailable", "service_error", 1),
        ("blocked", True, "debug-when-unconfigured", 422, "prompt_guard_blocked", "policy_blocked", 1),
        ("unconfigured", False, "required", 503, "prompt_guard_unavailable", "guard_not_configured", 0),
    ],
    ids=[
        "local-user-debug",
        "local-aggregate-debug",
        "local-user-required",
        "local-aggregate-required",
        "azure-unavailable",
        "azure-blocked",
        "required-unconfigured",
    ],
)
async def test_actual_guard_refusal_preserves_local_cause_and_remote_fail_closed(
    authority, case, mounted, mode, expected_status, expected_code, expected_reason, expected_http_calls
):
    engine, _service, owner, coordinator = authority
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        return _remote_response(request, outcome=case, requests=requests)

    mount = PromptGuardMount(AzurePromptGuardClient(_settings(), http_transport=httpx.MockTransport(transport))) if mounted else None
    user = "Ordinary safe input."
    messages = []
    if case == "user_unit":
        user = "u" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1)
    elif case == "document_count":
        # Every complete unit fits; their count exceeds the existing envelope.
        messages = [
            {"role": "assistant", "content": "d" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS - 100)}
            for _index in range(PROMPT_GUARD_MAX_DOCUMENTS + 1)
        ]
        assert all(len(json.dumps(message, separators=(",", ":"))) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS for message in messages)
    messages.append({"role": "user", "content": user})

    with pytest.raises(PromptGuardRefused) as caught:
        await enforce_prompt_guard(
            mount=mount,
            mode=mode,
            role="composer",
            provider_custody=custody,
            messages=messages,
            user_prompt=user,
        )

    check = _checks(engine)
    assert len(check) == 1
    assert check[0]["reason"] == expected_reason
    assert check[0]["disposition"] == ("blocked" if case == "blocked" else "unavailable")
    assert check[0]["adapter"] == ("azure_prompt_shields" if mounted else "unconfigured")
    assert check[0]["mode"] == mode
    assert check[0]["operation_id"] == custody.context.fence.operation_id
    assert check[0]["operation_epoch"] == custody.context.fence.operation_epoch
    assert check[0]["audit_id"] == caught.value.audit_id
    assert len(requests) == expected_http_calls
    assert custody.attempt is None and custody.call is None
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []
        assert connection.execute(select(token_usage_ledger_table)).all() == []
    coordinator.assert_completed()

    # The service attaches evidence using this owned copy on terminal unwind.
    carried = caught.value.with_evidence(
        state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        initial_version=1,
    )
    assert carried.to_payload() == caught.value.to_payload()
    projected = project_composer_operation_error(carried, request_id="safe-request-id")
    assert projected.http_status == expected_status
    assert projected.error_type == expected_code
    assert projected.failure_code == "http_error"
    assert projected.body["error_type"] == expected_code
    assert projected.body["audit_id"] == check[0]["audit_id"]
    assert projected.body["request_id"] == "safe-request-id"
    if expected_reason == "unsupported_input":
        assert projected.body["detail"] == _LOCAL_DETAIL
    serialized = json.dumps(projected.model_dump(mode="json")) + json.dumps(check)
    assert user not in serialized and _REMOTE_BODY not in serialized
    assert "offline-guard-key" not in serialized

    # A genuine integrity failure must still outrank an ordinary input refusal.
    grouped = project_composer_operation_error(
        ExceptionGroup("safe grouped failure", [carried, AuditIntegrityError("safe integrity failure")]),
        request_id="safe-request-id",
    )
    assert grouped.http_status == 500 and grouped.error_type == "audit_integrity_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "mounted", "mode", "expected_status", "expected_code", "expected_reason", "expected_http_calls"),
    [
        ("user_unit", False, "debug-when-unconfigured", 422, "prompt_guard_unsupported_input", "unsupported_input", 0),
        ("user_unit", True, "required", 422, "prompt_guard_unsupported_input", "unsupported_input", 0),
        ("unavailable", True, "required", 503, "prompt_guard_unavailable", "service_error", 1),
        ("blocked", True, "debug-when-unconfigured", 422, "prompt_guard_blocked", "policy_blocked", 1),
    ],
    ids=["local-debug", "local-required", "azure-unavailable", "azure-blocked"],
)
async def test_actual_async_authoring_refusal_persists_and_replays_safe_cause(
    tmp_path, monkeypatch, case, mounted, mode, expected_status, expected_code, expected_reason, expected_http_calls
):
    app, service = _make_app(tmp_path, quota_enabled=True)
    requests: list[httpx.Request] = []
    sdk_calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        return _remote_response(request, outcome=case, requests=requests)

    def client_factory(settings: PromptGuardSettings) -> AzurePromptGuardClient:
        return AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(transport))

    async def unexpected_model_call(**kwargs):
        sdk_calls.append(kwargs)
        raise AssertionError("A refused protected dispatch reached the model SDK")

    # Only the external transport is replaced; the guard adapter/parser is real.
    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", client_factory)
    monkeypatch.setattr(litellm, "acompletion", unexpected_model_call)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    guard_settings = _settings() if mounted else None
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_advisor_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
            "composer_advisor_endpoint_base_url": "https://model.example.test/v1",
            "composer_advisor_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
            "composer_prompt_guard_mode": mode,
            "composer_prompt_guard": guard_settings,
            "composer_advisor_prompt_guard": guard_settings,
            "composer_boot_probe_enabled": False,
        }
    )
    app.state.composer_service = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(), app.state.settings, sessions_service=service, session_engine=app.state.session_engine
    )
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    user = "u" * (PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1) if case == "user_unit" else "What can you help me build?"
    session = await service.create_session("alice", "Existing capacity session", "local")
    path = f"/api/sessions/{session.id}/messages"
    body = message_body(user)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            settled = await submit_and_settle(client, app, path=path, body=body)
            status, public = settled.error()
            assert status == expected_status, public
            assert public["detail"]["error_type"] == expected_code
            if expected_reason == "unsupported_input":
                assert public["detail"]["detail"] == _LOCAL_DETAIL
            before = await service.get_messages(session.id, limit=None)
            replay = await client.post(path, json=body)
            assert replay.status_code == 202
            assert replay.json()["operation_id"] == body["operation_id"]
            await app.state.composer_async_worker.run_until_idle()
            observed = await client.get(f"/api/sessions/{session.id}/operations/{body['operation_id']}")
            assert observed.status_code == 200
            assert observed.json() == settled.final.json()
            history = await client.get(f"/api/sessions/{session.id}/operations", params={"operation_id": body["operation_id"]})
            assert history.status_code == 200
            outcomes = history.json()["items"]
            assert len(outcomes) == 1 and outcomes[0]["operation_id"] == body["operation_id"]
            assert outcomes[0]["failure_code"] == "http_error"
            # The closed failure feed omits all detailed guard categories.
            assert outcomes[0]["error_type"] is None

        record = await run_sync_in_worker(
            app.state.composer_async_operation_authority.get, session_id=session.id, operation_id=body["operation_id"]
        )
        assert record is not None and record.status == "failed" and record.result_json is not None
        stored = ComposerOperationError.model_validate_json(record.result_json, strict=True)
        assert stored.http_status == expected_status and stored.error_type == expected_code
        assert stored.failure_code == "http_error" and stored.body == public
        rows = await service.get_messages(session.id, limit=None)
        assert [row.id for row in rows] == [row.id for row in before]
        assert [row.role for row in rows if row.role in ("user", "assistant")] == ["user"]
        checks = [envelope["check"] for row in rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "prompt_guard_audit"]
        assert len(checks) == 1
        check = checks[0]
        assert check["reason"] == expected_reason
        assert check["disposition"] == ("blocked" if case == "blocked" else "unavailable")
        assert check["adapter"] == ("azure_prompt_shields" if mounted else "unconfigured")
        assert check["mode"] == mode
        assert check["audit_id"] == public["detail"]["audit_id"]
        assert check["operation_id"] == record.session_operation_id
        assert check["operation_epoch"] == record.session_operation_epoch
        assert await service.get_current_state(session.id) is None
        assert await service.list_composition_proposals(session.id) == []
        with service._engine.connect() as connection:
            assert connection.execute(select(quota_provider_attempts_table)).all() == []
            assert connection.execute(select(token_usage_ledger_table)).all() == []
            fence = connection.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
            ).one()
        assert fence.released_at is not None
        assert len(requests) == expected_http_calls and sdk_calls == []
        serialized = json.dumps(public) + json.dumps(deep_thaw(checks))
        assert user not in serialized and _REMOTE_BODY not in serialized
        assert "offline-guard-key" not in serialized and "offline-model-key" not in serialized
    finally:
        service._engine.dispose()
