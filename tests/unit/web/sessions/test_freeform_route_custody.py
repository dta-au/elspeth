"""Real route controls for freeform ingress and post-provider custody."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.sql.dml import Insert

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions import composer_turn
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, composition_states_table, session_operation_fences_table
from elspeth.web.sessions.routes._helpers import _join_freeform_owned_task
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.helpers.composer_operations import (
    SettledComposerOperation,
    current_head_state_id,
    install_composer_async_worker,
    install_composer_rate_limiter,
    submit_and_settle,
)
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
    install_composer_rate_limiter(app, limit=100)
    composer = _AuditedComposer()
    app.state.composer_service = composer
    install_composer_async_worker(app)
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
    await _actual_join_control(tmp_path, monkeypatch, site=site, route="messages")


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
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={"title": "return race"})).json()["id"]
            body = {"operation_id": str(uuid4()), "state_id": None}
            if route == "messages":
                body["content"] = "compose"
            else:
                user = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
                body["expected_user_message_id"] = str(user.id)
            settled = await submit_and_settle(client, app, path=f"/api/sessions/{session_id}/{route}", body=body)
            settled.result()
            records = await service.get_messages(UUID(session_id), limit=None)
            roles = [record.role for record in records]
            assert roles.count("user") == 1
            assert roles.count("assistant") == 1
            assert roles.count("audit") == 1
            assert composer.calls == 1
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            assert progress is not None and progress.phase == "complete"
            assert await _fence_released(engine, session_id)
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("messages", "recompose"))
async def test_postprovider_invariant_fault_outweighs_caller_cancel(tmp_path, monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    gate = _ActualCustodyGate(asyncio.get_running_loop())
    gate.install(app, service, engine, monkeypatch, "projection")

    original_invariant = InvariantError("owned continuation invariant fault")
    observed_cancellations = []
    original_shield = asyncio.shield

    async def observe_actual_shield(awaitable):
        try:
            return await original_shield(awaitable)
        except asyncio.CancelledError as original:
            if gate.owners and asyncio.current_task() is gate.owners[0]:
                observed_cancellations.append(original)
            raise

    monkeypatch.setattr(asyncio, "shield", observe_actual_shield)

    def fail_after_cancel(*args, **kwargs):
        gate.park_sql()
        raise original_invariant

    monkeypatch.setattr(composer_turn, "_message_with_state_response", fail_after_cancel)
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={"title": "fault priority"})).json()["id"]
            body = {"operation_id": str(uuid4()), "state_id": None}
            if route == "messages":
                body["content"] = "compose"
            else:
                user = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
                body["expected_user_message_id"] = str(user.id)
            accepted = await client.post(f"/api/sessions/{session_id}/{route}", json=body)
            assert accepted.status_code == 202, accepted.text
            drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
            await asyncio.wait_for(gate.entered.wait(), 10)
            owner = gate.owners[0]
            owner.cancel("caller cancelled before child fault")
            await asyncio.sleep(0)
            assert not owner.done()
            assert not await _fence_released(engine, session_id)
            gate.release.set()
            await drive
            final = await client.get(f"/api/sessions/{session_id}/operations/{body['operation_id']}")
            status, error = SettledComposerOperation(accepted, final).error()
            assert status == 500
            assert error["detail"]["error_type"] == "server_invariant_violated"
            assert error["detail"]["detail"] == "Server invariant violated. See application audit log for diagnostic detail."
            assert len(observed_cancellations) == 1
            assert observed_cancellations[0].args == ("caller cancelled before child fault",)
            assert original_invariant.args[0] not in final.text
            assert observed_cancellations[0].args[0] not in final.text
            assert gate.leases[0].required_work is not None
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            assert progress is not None and progress.phase != "complete"
            assert composer.calls == 1
            assert await _fence_released(engine, session_id)
            roots = []
            pending = [error for ticket in gate.leases[0].required_work.tickets for error in ticket.errors]
            while pending:
                original = pending.pop()
                if any(original is earlier for earlier in roots):
                    continue
                roots.append(original)
                if isinstance(original, BaseExceptionGroup):
                    pending.extend(original.exceptions)
                if original.__cause__ is not None:
                    pending.append(original.__cause__)
            assert any(error is original_invariant for error in roots)
            assert any(error is observed_cancellations[0] for error in roots)
    finally:
        gate.release.set()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("state", "assistant", "cohort", "projection", "progress"))
async def test_recompose_joins_postcommit_writes(tmp_path, monkeypatch: pytest.MonkeyPatch, site: str) -> None:
    await _actual_join_control(tmp_path, monkeypatch, site=site, route="recompose")


@pytest.mark.asyncio
async def test_recompose_rejects_wrong_last_user_identity_before_provider(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "identity"})).json()["id"]
        await service.add_message(UUID(session_id), "user", "retry me", writer_principal="route_user_message")
        settled = await submit_and_settle(
            client,
            app,
            path=f"/api/sessions/{session_id}/recompose",
            body={"expected_user_message_id": str(uuid4()), "operation_id": str(uuid4()), "state_id": None},
        )
        status, body = settled.error()
        assert status == 409, settled.final.text
        assert body["detail"]["error_type"] == "recompose_user_message_mismatch"
        assert composer.calls == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_send_receipt_reuses_original_null_state_after_head_advances(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    key = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        session_id = (await client.post("/api/sessions", json={"title": "receipt"})).json()["id"]
        url = f"/api/sessions/{session_id}/messages"
        original_body = {"content": "same text", "operation_id": key, "state_id": None}
        first = await submit_and_settle(client, app, path=url, body=original_body)
        assert first.final.status_code == 200, first.final.text
        first.result()
        original = app.state.composer_async_operation_authority.get(session_id=UUID(session_id), operation_id=key)
        assert original is not None and original.user_message_id is not None
        assert original.result_sha256 is not None
        canonical_id = str(original.user_message_id)
        assert composer.calls == 1
        assert len(await service.get_state_versions(UUID(session_id))) == 1

        accepted = await client.post(url, json=original_body)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["operation_id"] == key
        assert accepted.json()["kind"] == "compose_message"
        assert accepted.json()["status"] == "completed"
        replay = await client.get(f"/api/sessions/{session_id}/operations/{key}")
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.final.json()
        replay_record = app.state.composer_async_operation_authority.get(session_id=UUID(session_id), operation_id=key)
        assert replay_record is not None
        assert replay_record.user_message_id == original.user_message_id
        assert replay_record.result_sha256 == original.result_sha256
        assert composer.calls == 1

        conflict = await client.post(url, json={"content": "changed", "operation_id": key, "state_id": None})
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["error_type"] == "composer_operation_conflict"
        assert composer.calls == 1

        messages = (await client.get(url)).json()
        matching = [message for message in messages if message["role"] == "user"]
        assert len(matching) == 1
        assert matching[0]["id"] == canonical_id
        assert matching[0]["operation_id"] == key

        intentional_second = await submit_and_settle(
            client,
            app,
            path=url,
            body={"content": "same text", "operation_id": str(uuid4()), "state_id": await current_head_state_id(client, session_id)},
        )
        assert intentional_second.final.status_code == 200, intentional_second.final.text
        intentional_second.result()
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
    app, service, engine, composer = _file_app(tmp_path)

    def fail_projection(*args, **kwargs):
        raise failure("first-party response projection failed")

    monkeypatch.setattr(composer_turn, "_message_with_state_response", fail_projection)
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={"title": "projection fault"})).json()["id"]
            body = {"operation_id": str(uuid4()), "state_id": None}
            if route == "messages":
                body["content"] = "compose"
            else:
                user = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
                body["expected_user_message_id"] = str(user.id)
            settled = await submit_and_settle(client, app, path=f"/api/sessions/{session_id}/{route}", body=body)
            status, error = settled.error()
            assert status == 500
            if failure is InvariantError:
                assert error["detail"]["error_type"] == "server_invariant_violated"
            assert composer.calls == 1
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            assert progress is not None and progress.phase != "complete"
            records = await service.get_messages(UUID(session_id), limit=None)
            # Source34 runs before the atomic terminal commit: no assistant escapes rollback.
            assert sum(message.role == "assistant" for message in records) == 0
            assert sum(message.role == "audit" for message in records) == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_reused_key_conflicts_before_state_validation_without_cross_session_leak(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        own_session = (await client.post("/api/sessions", json={"title": "own"})).json()["id"]
        foreign_session = (await client.post("/api/sessions", json={"title": "other"})).json()["id"]
        key = str(uuid4())
        own_url = f"/api/sessions/{own_session}/messages"
        accepted = await submit_and_settle(client, app, path=own_url, body={"content": "first", "operation_id": key, "state_id": None})
        assert accepted.final.status_code == 200, accepted.final.text
        accepted.result()
        foreign = await submit_and_settle(
            client,
            app,
            path=f"/api/sessions/{foreign_session}/messages",
            body={"content": "foreign", "operation_id": str(uuid4()), "state_id": None},
        )
        assert foreign.final.status_code == 200, foreign.final.text
        foreign.result()
        foreign_state = await service.get_current_state(UUID(foreign_session))
        assert foreign_state is not None
        unknown_state_id = str(uuid4())

        changed_unknown = await client.post(own_url, json={"content": "first", "operation_id": key, "state_id": unknown_state_id})
        changed_foreign = await client.post(own_url, json={"content": "first", "operation_id": key, "state_id": str(foreign_state.id)})
        assert changed_unknown.status_code == changed_foreign.status_code == 409
        assert changed_unknown.json() == changed_foreign.json()
        assert changed_unknown.json()["detail"]["error_type"] == "composer_operation_conflict"

        fresh_unknown = await client.post(own_url, json={"content": "second", "operation_id": str(uuid4()), "state_id": unknown_state_id})
        fresh_foreign = await client.post(
            own_url, json={"content": "second", "operation_id": str(uuid4()), "state_id": str(foreign_state.id)}
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

    app, service, engine, composer = _file_app(tmp_path)
    title_entered = asyncio.Event()
    title_release = asyncio.Event()
    title_finished = asyncio.Event()
    statuses: list[str] = []
    real_finish = helpers_module.finish_composer_request_metrics

    def capture_finish(*args, status: str, **kwargs) -> None:
        statuses.append(status)
        real_finish(*args, status=status, **kwargs)

    async def block_auto_title(**kwargs) -> None:
        title_entered.set()
        await title_release.wait()
        title_finished.set()

    monkeypatch.setattr(helpers_module, "finish_composer_request_metrics", capture_finish)
    monkeypatch.setattr(composer_turn, "maybe_auto_title_session", block_auto_title)
    gate = _ActualCustodyGate(asyncio.get_running_loop())
    gate.install(app, service, engine, monkeypatch, "progress")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={})).json()["id"]
            key = str(uuid4())
            accepted = await client.post(
                f"/api/sessions/{session_id}/messages", json={"content": "compose", "operation_id": key, "state_id": None}
            )
            assert accepted.status_code == 202, accepted.text
            drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
            await asyncio.wait_for(title_entered.wait(), 10)
            before_title = await client.get(f"/api/sessions/{session_id}/operations/{key}")
            assert before_title.json()["status"] == "running"
            assert before_title.json()["result"] is None
            assert not title_finished.is_set()
            assert not await _fence_released(engine, session_id)
            title_release.set()
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert title_finished.is_set()
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            assert progress is not None and progress.phase == "complete"
            owner = gate.owners[0]
            assert not owner.done()
            owner.cancel("cancel after durable completion")
            await asyncio.sleep(0)
            assert not owner.done()
            assert not await _fence_released(engine, session_id)
            gate.release.set()
            await drive
            final = await client.get(f"/api/sessions/{session_id}/operations/{key}")
            SettledComposerOperation(accepted, final).result()
            assert statuses == ["completed"]
            assert composer.calls == 1
            records = await service.get_messages(UUID(session_id), limit=None)
            assert sum(row.role == "assistant" for row in records) == 1
            assert sum(row.role == "audit" for row in records) == 1
    finally:
        title_release.set()
        gate.release.set()
        engine.dispose()


class _ActualCustodyGate:
    def __init__(self, loop):
        self.loop = loop
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.owners: list[asyncio.Task] = []
        self.leases: list[SessionOperationLease] = []
        self.once = False

    def park_sql(self):
        if self.once:
            return
        self.once = True
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            if not self.release.wait(10):
                raise TimeoutError("Actual custody gate was not released")
        finally:
            self.finished.set()

    def install(self, app, service, engine, monkeypatch, site):
        real_started = app.state.composer_async_worker._run_started

        async def capture_owner(services, running, lease):
            owner = asyncio.current_task()
            assert owner is not None
            self.owners.append(owner)
            self.leases.append(lease)
            return await real_started(services, running, lease)

        monkeypatch.setattr(app.state.composer_async_worker, "_run_started", capture_owner)
        if site in ("ingress", "state", "assistant"):

            def after_insert(conn, cursor, statement, parameters, context, executemany):
                if context.compiled is None:
                    return
                compiled = context.compiled.statement
                if not isinstance(compiled, Insert):
                    return
                if site == "state":
                    matches = compiled.table is composition_states_table
                else:
                    role = "user" if site == "ingress" else "assistant"
                    matches = compiled.table is chat_messages_table and context.compiled_parameters[0]["role"] == role
                if matches:
                    self.park_sql()

            event.listen(engine, "after_cursor_execute", after_insert)
        elif site == "cohort":
            real = service._write_audit_cohort_on_connection

            def gated_cohort(*args, **kwargs):
                value = real(*args, **kwargs)
                self.park_sql()
                return value

            monkeypatch.setattr(service, "_write_audit_cohort_on_connection", gated_cohort)
        elif site == "projection":
            real = composer_turn._message_with_state_response

            def gated_projection(*args, **kwargs):
                value = real(*args, **kwargs)
                self.park_sql()
                return value

            monkeypatch.setattr(composer_turn, "_message_with_state_response", gated_projection)
        else:
            real = composer_turn._publish_progress

            async def gated_progress(*args, **kwargs):
                value = await real(*args, **kwargs)
                if kwargs["event"].phase == "complete" and not self.once:
                    self.once = True
                    self.entered.set()
                    try:
                        async with asyncio.timeout(10):
                            while not self.release.is_set():
                                await asyncio.sleep(0.001)
                    finally:
                        self.finished.set()
                return value

            monkeypatch.setattr(composer_turn, "_publish_progress", gated_progress)


async def _actual_join_control(tmp_path, monkeypatch, *, site, route):
    from elspeth.web.sessions.routes import _helpers as helpers_module

    app, service, engine, composer = _file_app(tmp_path)
    statuses: list[str] = []
    real_finish = helpers_module.finish_composer_request_metrics

    def capture_finish(*args, status, **kwargs):
        statuses.append(status)
        real_finish(*args, status=status, **kwargs)

    monkeypatch.setattr(helpers_module, "finish_composer_request_metrics", capture_finish)
    gate = _ActualCustodyGate(asyncio.get_running_loop())
    gate.install(app, service, engine, monkeypatch, site)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={"title": "custody"})).json()["id"]
            key = str(uuid4())
            body = {"operation_id": key, "state_id": None}
            if route == "messages":
                body["content"] = "probe"
            else:
                user = await service.add_message(UUID(session_id), "user", "retry me", writer_principal="route_user_message")
                body["expected_user_message_id"] = str(user.id)
            path = f"/api/sessions/{session_id}/{route}"
            accepted = await client.post(path, json=body)
            assert accepted.status_code == 202, accepted.text
            drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert len(gate.owners) == 1
            owner = gate.owners[0]
            owner.cancel("first cancellation")
            await asyncio.sleep(0)
            owner.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not owner.done()
            assert not gate.finished.is_set()
            assert not await _fence_released(engine, session_id)
            gate.release.set()
            await drive
            assert owner.done() and gate.finished.is_set()
            assert gate.leases[0].closed and gate.leases[0]._renewal_task.done()
            final = await client.get(f"/api/sessions/{session_id}/operations/{key}")
            observed = SettledComposerOperation(accepted, final)
            messages = await service.get_messages(UUID(session_id), limit=None)
            roles = [message.role for message in messages]
            assert roles.count("user") == 1
            if site == "ingress":
                assert composer.calls == 0
                assert roles.count("assistant") == 0
                retry = await client.post(path, json=body)
                assert retry.status_code == 202, retry.text
                replay = await client.get(f"/api/sessions/{session_id}/operations/{key}")
                assert replay.json() == final.json()
                replay_record = app.state.composer_async_operation_authority.get(session_id=UUID(session_id), operation_id=key)
                assert replay_record is not None and replay_record.user_message_id == messages[0].id
                assert composer.calls == 0
                assert [message.role for message in await service.get_messages(UUID(session_id), limit=None)] == ["user"]
                status, error = observed.error()
                assert status == 503, error
                assert final.json()["cancel_requested"] is False
                assert statuses == ["failed"]
            else:
                observed.result()
                assert composer.calls == 1
                assert roles.count("assistant") == 1
                assert roles.count("audit") == 1
                assert len(await service.get_state_versions(UUID(session_id))) == 1
                progress = await app.state.composer_progress_registry.get_latest(session_id)
                assert progress is not None and progress.phase == "complete"
                assert statuses == ["completed"]
            assert await _fence_released(engine, session_id)
    finally:
        gate.release.set()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("messages", "recompose"))
@pytest.mark.parametrize("failure", (RuntimeError, InvariantError))
async def test_after_commit_progress_fault_retains_original_and_cannot_replace_terminal(
    tmp_path, monkeypatch: pytest.MonkeyPatch, route: str, failure: type[Exception]
) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    original = failure("first-party after-commit projection fault")
    owners = []
    real_started = app.state.composer_async_worker._run_started
    real_progress = composer_turn._publish_progress

    async def capture_owner(services, running, lease):
        owner = asyncio.current_task()
        assert owner is not None
        owners.append(owner)
        return await real_started(services, running, lease)

    async def fail_after_commit(*args, **kwargs):
        await real_progress(*args, **kwargs)
        if kwargs["event"].phase == "complete":
            raise original

    monkeypatch.setattr(app.state.composer_async_worker, "_run_started", capture_owner)
    monkeypatch.setattr(composer_turn, "_publish_progress", fail_after_commit)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={"title": "late projection"})).json()["id"]
            body = {"operation_id": str(uuid4()), "state_id": None}
            if route == "messages":
                body["content"] = "compose"
            else:
                user = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
                body["expected_user_message_id"] = str(user.id)
            settled = await submit_and_settle(client, app, path=f"/api/sessions/{session_id}/{route}", body=body)
            settled.result()
            assert len(owners) == 1 and owners[0].done()
            observed_error = owners[0].exception()
            assert observed_error is not None
            original_objects = []
            pending = [observed_error]
            while pending:
                current = pending.pop()
                if any(current is observed for observed in original_objects):
                    continue
                original_objects.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                if current.__cause__ is not None:
                    pending.append(current.__cause__)
            assert any(error is original for error in original_objects)
            assert composer.calls == 1
            records = await service.get_messages(UUID(session_id), limit=None)
            assert sum(row.role == "assistant" for row in records) == 1
            assert sum(row.role == "audit" for row in records) == 1
            assert await _fence_released(engine, session_id)
            unchanged = await client.get(f"/api/sessions/{session_id}/operations/{body['operation_id']}")
            assert unchanged.json() == settled.final.json()
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("messages", "recompose"))
async def test_postprovider_invariant_fault_retains_three_actual_owner_cancellations(
    tmp_path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    gate = _ActualCustodyGate(asyncio.get_running_loop())
    gate.install(app, service, engine, monkeypatch, "projection")

    original_invariant = InvariantError("owned continuation invariant fault")
    observed_cancellations = []
    original_shield = asyncio.shield

    async def observe_actual_shield(awaitable):
        try:
            return await original_shield(awaitable)
        except asyncio.CancelledError as original:
            if gate.owners and asyncio.current_task() is gate.owners[0]:
                observed_cancellations.append(original)
            raise

    monkeypatch.setattr(asyncio, "shield", observe_actual_shield)

    def fail_after_cancel(*args, **kwargs):
        gate.park_sql()
        raise original_invariant

    monkeypatch.setattr(composer_turn, "_message_with_state_response", fail_after_cancel)
    drive = None
    cancellation_markers = (
        "caller cancelled before child fault",
        "caller cancellation second private marker",
        "caller cancellation third private marker",
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            session_id = (await client.post("/api/sessions", json={"title": "fault priority"})).json()["id"]
            body = {"operation_id": str(uuid4()), "state_id": None}
            if route == "messages":
                body["content"] = "compose"
            else:
                user = await service.add_message(UUID(session_id), "user", "compose", writer_principal="route_user_message")
                body["expected_user_message_id"] = str(user.id)
            accepted = await client.post(f"/api/sessions/{session_id}/{route}", json=body)
            assert accepted.status_code == 202, accepted.text
            drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert len(gate.owners) == len(gate.leases) == 1
            owner = gate.owners[0]
            async with asyncio.timeout(5):
                for ordinal, marker in enumerate(cancellation_markers, start=1):
                    assert owner.cancel(marker)
                    while len(observed_cancellations) < ordinal or owner.cancelling():
                        assert not owner.done()
                        await asyncio.sleep(0)
                    assert len(observed_cancellations) == ordinal
                    assert observed_cancellations[ordinal - 1].args == (marker,)
                    assert owner.cancelling() == 0
                    assert not owner.done()
                    assert not gate.finished.is_set()
                    assert not await _fence_released(engine, session_id)
            gate.release.set()
            await asyncio.wait_for(asyncio.shield(drive), 10)
            final = await client.get(f"/api/sessions/{session_id}/operations/{body['operation_id']}")
            status, error = SettledComposerOperation(accepted, final).error()
            assert status == 500
            assert error["detail"]["error_type"] == "server_invariant_violated"
            assert error["detail"]["detail"] == "Server invariant violated. See application audit log for diagnostic detail."
            assert len(observed_cancellations) == 3
            assert [cancelled.args for cancelled in observed_cancellations] == [(marker,) for marker in cancellation_markers]
            assert len({id(cancelled) for cancelled in observed_cancellations}) == 3
            assert observed_cancellations[0].args == ("caller cancelled before child fault",)
            assert original_invariant.args[0] not in final.text
            assert observed_cancellations[0].args[0] not in final.text
            assert all(cancelled.args[0] not in final.text for cancelled in observed_cancellations)
            assert gate.leases[0].required_work is not None
            progress = await app.state.composer_progress_registry.get_latest(session_id)
            assert progress is not None and progress.phase != "complete"
            assert composer.calls == 1
            assert await _fence_released(engine, session_id)
            roots = []
            pending = [error for ticket in gate.leases[0].required_work.tickets for error in ticket.errors]
            while pending:
                original = pending.pop()
                if any(original is earlier for earlier in roots):
                    continue
                roots.append(original)
                if isinstance(original, BaseExceptionGroup):
                    pending.extend(original.exceptions)
                if original.__cause__ is not None:
                    pending.append(original.__cause__)
            assert any(error is original_invariant for error in roots)
            assert any(error is observed_cancellations[0] for error in roots)
            assert all(any(error is cancelled for error in roots) for cancelled in observed_cancellations)
    except BaseException as failure:
        gate.release.set()
        try:
            if drive is not None:
                await asyncio.wait_for(asyncio.shield(drive), 10)
        except BaseException as cleanup_error:
            failure.add_note(f"Owned worker cleanup failed: {cleanup_error!r}")
        raise
    finally:
        gate.release.set()
        engine.dispose()
