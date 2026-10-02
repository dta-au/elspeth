"""Real route controls for freeform ingress and post-provider custody."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.routes._helpers import _join_freeform_owned_task
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from tests.unit.web.sessions.test_routes import _llm_call, _make_app, _ProgressAwareComposer


class _AuditedComposer(_ProgressAwareComposer):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def compose(self, *args, **kwargs) -> ComposerResult:
        self.calls += 1
        result = await super().compose(*args, **kwargs)
        return replace(result, llm_calls=(_llm_call(),))


def _file_app(tmp_path):
    app, old_service = _make_app(tmp_path)
    old_service._engine.dispose()
    engine = create_session_engine(f"sqlite:///{tmp_path / 'freeform-custody.db'}")
    initialize_session_schema(engine)
    wire_test_pipeline_user_authority(app, identity_id="alice", engine=engine)
    service = FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    app.state.session_service = service
    app.state.session_engine = engine
    composer = _AuditedComposer()
    app.state.composer_service = composer
    return app, service, engine, composer


async def _fence_released(engine, session_id: str) -> bool:
    with engine.connect() as conn:
        row = (
            conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == session_id))
            .mappings()
            .one()
        )
    return row["released_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("ingress", "state", "assistant", "cohort", "projection", "progress"))
async def test_send_joins_postcommit_writes_and_exact_ingress_retry(tmp_path, monkeypatch: pytest.MonkeyPatch, site: str) -> None:
    from elspeth.web.sessions.routes import _helpers as helpers_module
    from elspeth.web.sessions.routes import messages as messages_module

    app, service, engine, composer = _file_app(tmp_path)
    registered: dict[str, asyncio.Task] = {}
    real_create_task = SessionOperationLease.create_task

    def capture_task(self, coroutine, *, name=None):
        task = real_create_task(self, coroutine, name=name)
        if name is not None:
            registered[name] = task
        return task

    monkeypatch.setattr(SessionOperationLease, "create_task", capture_task)
    terminal_statuses: list[str] = []
    real_finish = helpers_module.finish_composer_request_metrics

    def capture_finish(*args, status: str, **kwargs) -> None:
        terminal_statuses.append(status)
        real_finish(*args, status=status, **kwargs)

    monkeypatch.setattr(helpers_module, "finish_composer_request_metrics", capture_finish)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    key = str(uuid4())

    if site == "ingress":
        real = service.add_message_with_transcript

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(service, "add_message_with_transcript", pause)
    elif site == "state":
        real = service.save_composition_state

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(service, "save_composition_state", pause)
    elif site == "assistant":
        real = service.add_message

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            if args[1] == "assistant":
                entered.set()
                try:
                    await release.wait()
                finally:
                    finished.set()
            return result

        monkeypatch.setattr(service, "add_message", pause)
    elif site == "cohort":
        real = messages_module._persist_turn_audit_cohort

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(messages_module, "_persist_turn_audit_cohort", pause)
    elif site == "projection":
        real = messages_module._pending_proposal_responses

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(messages_module, "_pending_proposal_responses", pause)
    else:
        real = messages_module._publish_progress

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            if kwargs["event"].phase == "complete":
                entered.set()
                try:
                    await release.wait()
                finally:
                    finished.set()
            return result

        monkeypatch.setattr(messages_module, "_publish_progress", pause)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "custody"})).json()["id"]
        pending = asyncio.create_task(
            client.post(f"/api/sessions/{session_id}/messages", json={"content": "probe", "client_request_id": key})
        )
        await asyncio.wait_for(entered.wait(), 10)
        pending.cancel("first cancellation")
        await asyncio.sleep(0)
        pending.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not pending.done()
        assert not finished.is_set()
        assert not await _fence_released(engine, session_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        messages = await service.get_messages(UUID(session_id), limit=None)
        roles = [message.role for message in messages]
        assert roles.count("user") == 1
        if site == "ingress":
            assert composer.calls == 0
            assert roles.count("assistant") == 0
            monkeypatch.setattr(service, "add_message_with_transcript", real)
            retry = await client.post(f"/api/sessions/{session_id}/messages", json={"content": "probe", "client_request_id": key})
            assert retry.status_code == 409, retry.text
            assert retry.json()["detail"]["error_type"] == "message_already_accepted"
            assert retry.json()["detail"]["user_message_id"] == str(messages[0].id)
            assert composer.calls == 0
            assert [message.role for message in await service.get_messages(UUID(session_id), limit=None)] == ["user"]
        else:
            assert composer.calls == 1
            assert roles.count("assistant") == 1
            assert roles.count("audit") == 1
            assert len(await service.get_state_versions(UUID(session_id))) == 1
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            assert progress is not None and progress.phase == "complete"
        assert await _fence_released(engine, session_id)
        assert registered["send-message-ingress"].done()
        if site != "ingress":
            assert registered["send-message-post-provider-settlement"].done()
        assert terminal_statuses[0] == ("cancelled" if site == "ingress" else "completed")
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("messages", "recompose"))
async def test_successful_provider_return_cancellation_still_settles_full_turn(tmp_path, route: str) -> None:
    app, service, engine, _composer = _file_app(tmp_path)

    class _CancelAtReturnComposer(_AuditedComposer):
        async def compose(self, *args, **kwargs) -> ComposerResult:
            result = await super().compose(*args, **kwargs)
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel("cancel after successful provider return")
            return result

    composer = _CancelAtReturnComposer()
    app.state.composer_service = composer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "return race"})).json()["id"]
        if route == "messages":
            url = f"/api/sessions/{session_id}/messages"
            body = {"content": "compose", "client_request_id": str(uuid4())}
        else:
            row = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
            url = f"/api/sessions/{session_id}/recompose"
            body = {"expected_user_message_id": str(row.id)}
        with pytest.raises(asyncio.CancelledError, match="cancel after successful provider return"):
            await client.post(url, json=body)
        records = await service.get_messages(UUID(session_id), limit=None)
        roles = [record.role for record in records]
        assert roles.count("user") == 1
        assert roles.count("assistant") == 1
        assert roles.count("audit") == 1
        assert composer.calls == 1
        progress = await app.state.composer_progress_registry.get_latest(session_id)
        assert progress is not None and progress.phase == "complete"
        assert await _fence_released(engine, session_id)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("messages", "recompose"))
async def test_postprovider_invariant_fault_outweighs_caller_cancel(tmp_path, monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    from elspeth.web.sessions.routes import messages as messages_module
    from elspeth.web.sessions.routes.composer import compose as compose_module

    app, service, engine, composer = _file_app(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_after_cancel(*args, **kwargs):
        entered.set()
        await release.wait()
        raise InvariantError("owned continuation invariant fault")

    module = messages_module if route == "messages" else compose_module
    monkeypatch.setattr(module, "_pending_proposal_responses", fail_after_cancel)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "fault priority"})).json()["id"]
        if route == "messages":
            url = f"/api/sessions/{session_id}/messages"
            body = {"content": "compose", "client_request_id": str(uuid4())}
        else:
            row = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
            url = f"/api/sessions/{session_id}/recompose"
            body = {"expected_user_message_id": str(row.id)}
        pending = asyncio.create_task(client.post(url, json=body))
        await asyncio.wait_for(entered.wait(), 10)
        pending.cancel("caller cancelled before child fault")
        await asyncio.sleep(0)
        assert not pending.done()
        assert not await _fence_released(engine, session_id)
        release.set()
        response = await pending
        assert response.status_code == 500
        assert response.json()["detail"]["error_type"] == "server_invariant_violated"
        progress = await app.state.composer_progress_registry.get_latest(session_id)
        assert progress is not None and progress.phase != "complete"
        assert composer.calls == 1
        assert await _fence_released(engine, session_id)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("state", "assistant", "cohort", "projection", "progress"))
async def test_recompose_joins_postcommit_writes(tmp_path, monkeypatch: pytest.MonkeyPatch, site: str) -> None:
    from elspeth.web.sessions.routes import _helpers as helpers_module
    from elspeth.web.sessions.routes.composer import compose as compose_module

    app, service, engine, composer = _file_app(tmp_path)
    registered: dict[str, asyncio.Task] = {}
    real_create_task = SessionOperationLease.create_task

    def capture_task(self, coroutine, *, name=None):
        task = real_create_task(self, coroutine, name=name)
        if name is not None:
            registered[name] = task
        return task

    monkeypatch.setattr(SessionOperationLease, "create_task", capture_task)
    terminal_statuses: list[str] = []
    real_finish = helpers_module.finish_composer_request_metrics

    def capture_finish(*args, status: str, **kwargs) -> None:
        terminal_statuses.append(status)
        real_finish(*args, status=status, **kwargs)

    monkeypatch.setattr(helpers_module, "finish_composer_request_metrics", capture_finish)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    if site == "state":
        real = service.save_composition_state

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(service, "save_composition_state", pause)
    elif site == "assistant":
        real = service.add_message

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            if args[1] == "assistant":
                entered.set()
                try:
                    await release.wait()
                finally:
                    finished.set()
            return result

        monkeypatch.setattr(service, "add_message", pause)
    elif site == "cohort":
        real = compose_module._persist_turn_audit_cohort

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(compose_module, "_persist_turn_audit_cohort", pause)
    elif site == "projection":
        real = compose_module._pending_proposal_responses

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            return result

        monkeypatch.setattr(compose_module, "_pending_proposal_responses", pause)
    else:
        real = compose_module._publish_progress

        async def pause(*args, **kwargs):
            result = await real(*args, **kwargs)
            if kwargs["event"].phase == "complete":
                entered.set()
                try:
                    await release.wait()
                finally:
                    finished.set()
            return result

        monkeypatch.setattr(compose_module, "_publish_progress", pause)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "recompose custody"})).json()["id"]
        user_row = await service.add_message(UUID(session_id), "user", "retry me", writer_principal="route_user_message")
        pending = asyncio.create_task(
            client.post(
                f"/api/sessions/{session_id}/recompose",
                json={"expected_user_message_id": str(user_row.id)},
            )
        )
        await asyncio.wait_for(entered.wait(), 10)
        pending.cancel("first cancellation")
        await asyncio.sleep(0)
        pending.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not pending.done()
        assert not finished.is_set()
        assert not await _fence_released(engine, session_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        messages = await service.get_messages(UUID(session_id), limit=None)
        roles = [message.role for message in messages]
        assert composer.calls == 1
        assert roles.count("user") == 1
        assert roles.count("assistant") == 1
        assert roles.count("audit") == 1
        assert len(await service.get_state_versions(UUID(session_id))) == 1
        progress = await app.state.composer_progress_registry.get_latest(session_id)
        assert progress is not None and progress.phase == "complete"
        assert await _fence_released(engine, session_id)
        assert registered["recompose-post-provider-settlement"].done()
        assert terminal_statuses == ["completed"]
    engine.dispose()


@pytest.mark.asyncio
async def test_recompose_rejects_wrong_last_user_identity_before_provider(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "identity"})).json()["id"]
        await service.add_message(UUID(session_id), "user", "retry me", writer_principal="route_user_message")
        response = await client.post(f"/api/sessions/{session_id}/recompose", json={"expected_user_message_id": str(uuid4())})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error_type"] == "recompose_user_message_mismatch"
        assert composer.calls == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_send_receipt_reuses_original_null_state_after_head_advances(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    key = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "receipt"})).json()["id"]
        url = f"/api/sessions/{session_id}/messages"
        first = await client.post(url, json={"content": "same text", "client_request_id": key})
        assert first.status_code == 200, first.text
        assert composer.calls == 1
        assert len(await service.get_state_versions(UUID(session_id))) == 1

        accepted = await client.post(url, json={"content": "same text", "client_request_id": key, "state_id": None})
        assert accepted.status_code == 409, accepted.text
        assert accepted.json()["detail"]["error_type"] == "message_already_accepted"
        canonical_id = accepted.json()["detail"]["user_message_id"]
        assert accepted.json()["detail"]["client_request_id"] == key
        assert composer.calls == 1

        conflict = await client.post(url, json={"content": "changed", "client_request_id": key})
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["error_type"] == "message_idempotency_conflict"
        assert composer.calls == 1

        messages = (await client.get(url)).json()
        matching = [message for message in messages if message["role"] == "user"]
        assert len(matching) == 1
        assert matching[0]["id"] == canonical_id
        assert matching[0]["client_request_id"] == key

        intentional_second = await client.post(url, json={"content": "same text", "client_request_id": str(uuid4())})
        assert intentional_second.status_code == 200, intentional_second.text
        assert composer.calls == 2
        assert len([message for message in await service.get_messages(UUID(session_id), limit=None) if message.role == "user"]) == 2
    engine.dispose()


@pytest.mark.asyncio
async def test_freeform_join_rejects_child_self_cancellation() -> None:
    async def cancel_self() -> None:
        raise asyncio.CancelledError("child stopped")

    task = asyncio.create_task(cancel_self())
    with pytest.raises(AuditIntegrityError, match="continuation cancelled"):
        await _join_freeform_owned_task(task)


@pytest.mark.asyncio
async def test_freeform_join_preserves_first_party_child_error_over_caller_cancel() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_later() -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("first-party settlement fault")

    async def owner() -> None:
        await _join_freeform_owned_task(asyncio.create_task(fail_later()))

    request = asyncio.create_task(owner())
    await entered.wait()
    request.cancel("caller stopped")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError, match="first-party settlement fault"):
        await request


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("messages", "recompose"))
@pytest.mark.parametrize("failure", (RuntimeError, InvariantError))
async def test_response_projection_fault_cannot_publish_false_complete(
    tmp_path, monkeypatch: pytest.MonkeyPatch, route: str, failure: type[Exception]
) -> None:
    from elspeth.web.sessions.routes import messages as messages_module
    from elspeth.web.sessions.routes.composer import compose as compose_module

    app, service, engine, composer = _file_app(tmp_path)

    async def fail_projection(*args, **kwargs):
        raise failure("first-party response projection failed")

    module = messages_module if route == "messages" else compose_module
    monkeypatch.setattr(module, "_pending_proposal_responses", fail_projection)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "projection fault"})).json()["id"]
        if route == "messages":
            url = f"/api/sessions/{session_id}/messages"
            body = {"content": "compose", "client_request_id": str(uuid4())}
        else:
            row = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
            url = f"/api/sessions/{session_id}/recompose"
            body = {"expected_user_message_id": str(row.id)}
        response = await client.post(url, json=body)
        assert response.status_code == 500
        if failure is InvariantError:
            assert response.json()["detail"]["error_type"] == "server_invariant_violated"
        assert composer.calls == 1
        progress = await app.state.composer_progress_registry.get_latest(session_id)
        assert progress is not None and progress.phase != "complete"
        records = await service.get_messages(UUID(session_id), limit=None)
        assert sum(message.role == "assistant" for message in records) == 1
        assert sum(message.role == "audit" for message in records) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_reused_key_conflicts_before_state_validation_without_cross_session_leak(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        own_session = (await client.post("/api/sessions", json={"title": "own"})).json()["id"]
        foreign_session = (await client.post("/api/sessions", json={"title": "other"})).json()["id"]
        key = str(uuid4())
        own_url = f"/api/sessions/{own_session}/messages"
        accepted = await client.post(own_url, json={"content": "first", "client_request_id": key})
        assert accepted.status_code == 200, accepted.text
        foreign = await client.post(
            f"/api/sessions/{foreign_session}/messages",
            json={"content": "foreign", "client_request_id": str(uuid4())},
        )
        assert foreign.status_code == 200, foreign.text
        foreign_state = await service.get_current_state(UUID(foreign_session))
        assert foreign_state is not None
        unknown_state_id = str(uuid4())

        changed_unknown = await client.post(own_url, json={"content": "first", "client_request_id": key, "state_id": unknown_state_id})
        changed_foreign = await client.post(own_url, json={"content": "first", "client_request_id": key, "state_id": str(foreign_state.id)})
        assert changed_unknown.status_code == changed_foreign.status_code == 409
        assert changed_unknown.json() == changed_foreign.json()
        assert changed_unknown.json()["detail"]["error_type"] == "message_idempotency_conflict"

        fresh_unknown = await client.post(
            own_url, json={"content": "second", "client_request_id": str(uuid4()), "state_id": unknown_state_id}
        )
        fresh_foreign = await client.post(
            own_url, json={"content": "second", "client_request_id": str(uuid4()), "state_id": str(foreign_state.id)}
        )
        assert fresh_unknown.status_code == fresh_foreign.status_code == 404
        assert fresh_unknown.json() == fresh_foreign.json()
        assert composer.calls == 2
        own_rows = await service.get_messages(UUID(own_session), limit=None)
        assert sum(row.role == "user" for row in own_rows) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_completed_turn_stays_completed_when_caller_cancels_during_auto_title_join(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.sessions.routes import _helpers as helpers_module
    from elspeth.web.sessions.routes import messages as messages_module

    app, service, engine, composer = _file_app(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    statuses: list[str] = []
    real_finish = helpers_module.finish_composer_request_metrics

    def capture_finish(*args, status: str, **kwargs) -> None:
        statuses.append(status)
        real_finish(*args, status=status, **kwargs)

    async def block_auto_title(**kwargs) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(helpers_module, "finish_composer_request_metrics", capture_finish)
    monkeypatch.setattr(messages_module, "maybe_auto_title_session", block_auto_title)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={})).json()["id"]
        pending = asyncio.create_task(
            client.post(
                f"/api/sessions/{session_id}/messages",
                json={"content": "compose", "client_request_id": str(uuid4())},
            )
        )
        await asyncio.wait_for(entered.wait(), 10)
        for _ in range(200):
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            if progress is not None and progress.phase == "complete":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("composer did not publish complete before auto-title join")
        assert not pending.done()
        pending.cancel("cancel after durable completion")
        await asyncio.sleep(0)
        assert not pending.done()
        assert not await _fence_released(engine, session_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert statuses == ["completed"]
        assert composer.calls == 1
        records = await service.get_messages(UUID(session_id), limit=None)
        assert sum(row.role == "assistant" for row in records) == 1
        assert sum(row.role == "audit" for row in records) == 1
    engine.dispose()
