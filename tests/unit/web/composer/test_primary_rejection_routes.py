"""F6 evidence through both real route owners, settlement and durable readback."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import litellm
import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.yaml_serializer import ComposerYamlSerializer
from elspeth.web.execution import routes as execution_routes
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.middleware.request_id import REQUEST_ID_HEADER
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import RuntimeWebPluginConfig
from elspeth.web.sessions import composer_turn
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.protocol import RunDiagnosticsAuditAuthority, RunDiagnosticsAuthorityLostError
from elspeth.web.sessions.routes._helpers import (
    _MAX_PROVIDER_DETAIL_CHARS,
    _PROVIDER_DETAIL_REDACTED,
    composer_session_lock_registry,
)
from tests.helpers.composer_operations import current_head_state_id, message_body, submit_and_settle
from tests.helpers.composition_state import composition_state_data
from tests.unit.web.composer.test_explicit_provider_custody import _required_guard_settings
from tests.unit.web.composer.test_primary_rejection_gateway import _PRIVATE, _sdk_rejection, _settled_failure
from tests.unit.web.sessions.test_routes import (
    _execute_session_operation_context,
    _make_app,
    _save_test_composition_state,
)
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft

pytest_plugins = ("tests.helpers.execution_custody",)


@pytest.fixture
def primary_route_case(tmp_path, monkeypatch):
    app, service = _make_app(tmp_path, quota_enabled=True)
    guard_settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4o-mini",
            "composer_boot_probe_enabled": False,
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard_settings.composer_prompt_guard,
            "composer_advisor_prompt_guard": guard_settings.composer_advisor_prompt_guard,
        }
    )
    try:
        yield app, service, requests
    finally:
        # _make_app's defining module owns an autouse cleanup fixture; importing
        # the function alone does not install that fixture in this module.
        app.state.session_engine.dispose()


def _install_real_composer(app, service, *, debug):
    app.state.settings = app.state.settings.model_copy(update={"composer_expose_provider_errors": debug})
    app.state.composer_service = ComposerServiceImpl(
        app.state.catalog_service,
        app.state.settings,
        sessions_service=service,
        session_engine=app.state.session_engine,
        secret_service=app.state.scoped_secret_resolver,
        plugin_snapshot_factory=app.state.plugin_snapshot_factory,
        operator_profile_registry=app.state.operator_profile_registry,
    )


def _install_real_execution(app, service, execution_fixture):
    loop = asyncio.get_running_loop()
    registry = execution_fixture.registry(loop)
    policy = compile_web_plugin_policy(
        registry=get_shared_plugin_manager(), settings=RuntimeWebPluginConfig.from_settings(app.state.settings)
    )
    execution = ExecutionServiceImpl(
        loop=loop,
        broadcaster=ProgressBroadcaster(loop, telemetry=app.state.sessions_telemetry),
        settings=app.state.settings,
        session_service=service,
        yaml_generator=ComposerYamlSerializer(),
        telemetry=app.state.sessions_telemetry,
        execution_lease_release_registry=registry,
        secret_service=app.state.scoped_secret_resolver,
        plugin_snapshot_factory=app.state.plugin_snapshot_factory.for_user_id,
        operator_profile_registry=app.state.operator_profile_registry,
        web_plugin_policy=policy,
        catalog=app.state.catalog_service,
    )
    app.state.execution_service = execution_fixture.bind(execution)
    app.state.execution_lease_release_registry = registry
    app.include_router(execution_routes.create_execution_router())


def _original(*, credential):
    message = _PRIVATE + " " + ("Bearer " + "f6-fixture-value-" * 3 if credential else "retained-provider-detail " * 100)
    return _sdk_rejection(message=message)


def _assert_public_projection(detail, *, category, evidence, debug, credential):
    assert detail["error_type"] == category
    assert detail["detail"] == "_BadRequestLLMError"
    if debug:
        assert detail["provider_status_code"] == 400
        assert len(detail["provider_detail"]) <= _MAX_PROVIDER_DETAIL_CHARS
        if credential:
            assert detail["provider_detail"] == _PROVIDER_DETAIL_REDACTED
        else:
            assert len(detail["provider_detail"]) == _MAX_PROVIDER_DETAIL_CHARS
            assert _PRIVATE in detail["provider_detail"]
    else:
        assert "provider_detail" not in detail and "provider_status_code" not in detail
        assert _PRIVATE not in str(detail)
    assert "request_rejection" in detail, "The actual route lost the primary request rejection descriptor"
    descriptor = SafeRequestRejection.from_wire(detail["request_rejection"])
    assert descriptor.reason_code == "context_length_exceeded"
    assert descriptor.reason_source == "http_response"
    assert descriptor.reason_disposition == "retained"
    assert descriptor.retryable is False
    assert descriptor.diagnostic_reference == evidence.evidence_id
    assert descriptor.diagnostic_reference is not None
    assert _PRIVATE not in descriptor.model_dump_json()


def _observe_settlement(monkeypatch, app, service, events, settled):
    actual_finish = service.finish_provider_attempt

    async def finish(**kwargs):
        assert events[-1] == "physical"
        await actual_finish(**kwargs)
        call = kwargs["call"]
        context = kwargs["session_operation_context"]
        assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
        evidence = await _settled_failure(app.state.session_engine, service, context, call)
        settled.append((call, context, evidence))
        events.append("settled")

    monkeypatch.setattr(service, "finish_provider_attempt", finish)


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True], ids=["debug-off", "debug-on"])
@pytest.mark.parametrize("credential", [False, True], ids=["bounded-prose", "credential-redacted"])
async def test_tool_primary_durable_route_retains_rejection_after_required_audit(primary_route_case, monkeypatch, debug, credential):
    app, service, guard_requests = primary_route_case
    _install_real_composer(app, service, debug=debug)
    original = _original(credential=credential)
    events, settled, physical, projected = [], [], [], []
    _observe_settlement(monkeypatch, app, service, events, settled)
    actual_persist = composer_turn._persist_llm_calls
    actual_project = composer_turn._litellm_error_detail

    async def completion(**kwargs):
        assert guard_requests
        physical.append(kwargs)
        events.append("physical")
        raise original

    async def persist(*args, **kwargs):
        assert kwargs["required_audit"] is True and kwargs["required_work"] is not None
        assert events == ["physical", "settled"]
        result = await actual_persist(*args, **kwargs)
        events.append("required-audit")
        return result

    def project(category, wrapper, **kwargs):
        assert wrapper.__cause__ is original
        assert events == ["physical", "settled", "required-audit"]
        events.append("projection")
        detail = actual_project(category, wrapper, **kwargs)
        projected.append(detail)
        return detail

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(composer_turn, "_persist_llm_calls", persist)
    monkeypatch.setattr(composer_turn, "_litellm_error_detail", project)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/sessions", json={"title": "Primary rejection"})
        assert created.status_code == 201, created.text
        sid = UUID(created.json()["id"])
        await _seed_output_only_draft(app, service, sid)
        body = message_body("Inspect this ordinary primary draft.", state_id=await current_head_state_id(client, sid))
        result = await submit_and_settle(client, app, path=f"/api/sessions/{sid}/messages", body=body)
        assert result.accepted.status_code == 202
        request_id = result.accepted.headers[REQUEST_ID_HEADER]
        assert request_id
        assert result.accepted.request.method == "POST"
        assert result.accepted.request.url.path == f"/api/sessions/{sid}/messages"
        assert json.loads(result.accepted.request.content) == body
        snapshot = result.final.json()
        assert snapshot["status"] == "failed" and snapshot["result"] is None
        status, error_body = result.error()
        assert status == 502
        visible = await client.get(f"/api/sessions/{sid}/messages")
        assert visible.status_code == 200
    assert len(physical) == len(settled) == len(projected) == 1
    assert events == ["physical", "settled", "required-audit", "projection"]
    call, context, evidence = settled[0]
    assert context.fence.session_id == str(sid)
    assert call.call_id == evidence.tool_call_id
    stored = app.state.composer_async_operation_authority.get(session_id=sid, operation_id=body["operation_id"])
    assert stored is not None and stored.result_json is not None
    reloaded = ComposerOperationError.model_validate_json(stored.result_json, strict=True)
    assert reloaded.http_status == 502 and reloaded.failure_code == "http_error"
    assert reloaded.body == error_body
    assert snapshot["error"]["body"] == error_body
    assert stored.request_id == request_id
    assert error_body["detail"] == {**projected[0], "request_id": request_id}
    assert evidence.evidence_id not in visible.text
    _assert_public_projection(error_body["detail"], category="llm_unavailable", evidence=evidence, debug=debug, credential=credential)


@pytest.mark.asyncio
@pytest.mark.parametrize("debug", [False, True], ids=["debug-off", "debug-on"])
@pytest.mark.parametrize("credential", [False, True], ids=["bounded-prose", "credential-redacted"])
async def test_text_primary_diagnostics_route_retains_rejection_and_audit_authority(
    primary_route_case, execution_fixture, monkeypatch, debug, credential
):
    app, service, guard_requests = primary_route_case
    _install_real_composer(app, service, debug=debug)
    _install_real_execution(app, service, execution_fixture)
    session = await service.create_session("alice", "Diagnostics rejection", "local")
    state = await _save_test_composition_state(service, session.id, composition_state_data(is_valid=True), provenance="session_seed")
    async with _execute_session_operation_context(service, session.id) as execute_context:
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
    assert run.landscape_run_id is None and run.status == "pending"
    lock = await composer_session_lock_registry(app).get_lock(str(session.id))
    original = _original(credential=credential)
    events, settled, physical, authorities, audit_drafts = [], [], [], [], []
    _observe_settlement(monkeypatch, app, service, events, settled)
    actual_audit = service.add_run_diagnostics_audit_messages_atomic
    actual_project = execution_routes._litellm_error_detail

    async def completion(**kwargs):
        assert guard_requests
        assert all(json.loads(request.content)["userPrompt"] == "" for request in guard_requests)
        physical.append(kwargs)
        events.append("physical")
        raise original

    async def audit(authority, drafts):
        assert type(authority) is RunDiagnosticsAuditAuthority
        assert authority == RunDiagnosticsAuditAuthority(run_id=run.id, session_id=session.id, state_id=state.id)
        assert lock.locked()
        assert events == ["physical", "settled"]
        assert len(drafts) == 1 and drafts[0].tool_calls[0]["call"]["call_id"] == settled[0][0].call_id
        authorities.append(authority)
        audit_drafts.append(drafts)
        before = await service.get_messages(session.id, limit=None)
        result = await actual_audit(authority, drafts)
        after = await service.get_messages(session.id, limit=None)
        assert [row.id for row in after] == [row.id for row in before], (
            "A physical checkpoint must be replayed without duplicate charge/audit"
        )
        events.append("run-authority")
        return result

    def project(category, wrapper, **kwargs):
        assert wrapper.__cause__ is original
        assert events == ["physical", "settled", "run-authority"]
        events.append("projection")
        return actual_project(category, wrapper, **kwargs)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(service, "add_run_diagnostics_audit_messages_atomic", audit)
    monkeypatch.setattr(execution_routes, "_litellm_error_detail", project)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.id}/diagnostics/evaluate")
    assert response.status_code == 502, response.text
    assert len(physical) == len(settled) == len(authorities) == 1
    assert events == ["physical", "settled", "run-authority", "projection"]
    call, context, evidence = settled[0]
    assert context.fence.session_id == str(session.id) and evidence.tool_call_id == call.call_id
    _assert_public_projection(
        response.json()["detail"], category="run_diagnostics_explanation_failed", evidence=evidence, debug=debug, credential=credential
    )

    # Retain the real repository's stale-state and archive refusals against the
    # same actual terminal call. Neither an invented audit nor a second SDK
    # attempt is used to establish the negative authority controls.
    other = await _save_test_composition_state(service, session.id, composition_state_data(is_valid=True), provenance="session_seed")
    wrong = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=session.id, state_id=other.id)
    drafts = audit_drafts[0]
    with pytest.raises(RunDiagnosticsAuthorityLostError) as rebound:
        await actual_audit(wrong, drafts)
    assert rebound.value.reason == "run_rebound"
    await service.archive_session(session.id)
    with pytest.raises(RunDiagnosticsAuthorityLostError) as archived:
        await actual_audit(authorities[0], drafts)
    assert archived.value.reason == "session_archived"
    assert len(physical) == 1
