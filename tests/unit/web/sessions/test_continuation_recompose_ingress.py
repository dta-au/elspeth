"""Recompose adopts one authenticated original row before ordinary provider work."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.sessions.models import chat_messages_table, composer_async_operations_table
from tests.helpers.composer_operations import recompose_body
from tests.helpers.continuation_app import (
    ACTOR,
    ScriptedSDK,
    continuation_app,
    end_admit,
    install_sdk,
    submit_and_settle,
)
from tests.helpers.session_fences import acquire_compose_context


@pytest.mark.asyncio
async def test_recompose_adopts_original_human_and_calls_ordinary_primary_and_advisor(tmp_path, monkeypatch) -> None:
    sdk = ScriptedSDK([("primary", ("Hello. I can explain pipelines.", None)), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        worker = harness.app.state.composer_async_worker
        unsettled: list[BaseException] = []
        original_finished = worker._job_finished

        def observe_finished(key, task) -> None:
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    unsettled.append(failure)
            original_finished(key, task)

        monkeypatch.setattr(worker, "_job_finished", observe_finished)
        async with acquire_compose_context(service, harness.session_id) as context:
            original = await service.add_message(
                harness.session_id, "user", "Hello", writer_principal="route_user_message", session_operation_context=context
            )
        operation_id = str(uuid4())
        try:
            result = await submit_and_settle(
                harness,
                recompose_body(original.id, operation_id=operation_id),
                route="recompose",
            )
        except AssertionError as error:
            if unsettled:
                raise unsettled[0] from error
            raise
        assert result.status == "completed", result
        assert sdk.roles == ["primary", "end"]  # Recompose never buys TITLE.
        assert {tool["function"]["name"] for tool in sdk.requests[0]["tools"]} >= {"set_pipeline"}
        with harness.app.state.session_engine.connect() as conn:
            humans = conn.execute(
                select(chat_messages_table.c.id, chat_messages_table.c.writer_principal).where(
                    chat_messages_table.c.session_id == str(harness.session_id), chat_messages_table.c.role == "user"
                )
            ).all()
            parent = conn.execute(
                select(composer_async_operations_table.c.user_message_id, composer_async_operations_table.c.ingress_phase).where(
                    composer_async_operations_table.c.operation_id == operation_id
                )
            ).one()
        assert humans == [(str(original.id), "route_user_message")]
        assert parent.user_message_id == str(original.id) and parent.ingress_phase == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected_status", [("absent", 400), ("foreign", 400), ("changed", 409)])
async def test_recompose_refuses_missing_foreign_or_changed_original_before_provider(
    tmp_path, monkeypatch, case: str, expected_status: int
) -> None:
    sdk = ScriptedSDK([])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        original_id = uuid4()
        if case == "foreign":
            other = await service.create_session(user_id=ACTOR, title="Other session", auth_provider_type="local")
            async with acquire_compose_context(service, other.id) as context:
                original = await service.add_message(
                    other.id, "user", "Other request", writer_principal="route_user_message", session_operation_context=context
                )
            original_id = original.id
        if case == "changed":
            async with acquire_compose_context(service, harness.session_id) as context:
                original = await service.add_message(
                    harness.session_id, "user", "Original request", writer_principal="route_user_message", session_operation_context=context
                )
                original_id = original.id
                await service.add_message(
                    harness.session_id, "user", "New request", writer_principal="route_user_message", session_operation_context=context
                )
        operation_id = str(uuid4())
        async with harness.client() as client:
            refused = await client.post(
                f"/api/sessions/{harness.session_id}/recompose",
                json=recompose_body(original_id, operation_id=operation_id),
            )
        assert refused.status_code == expected_status, refused.text
        assert sdk.requests == []
        with harness.app.state.session_engine.connect() as conn:
            parent = conn.execute(
                select(composer_async_operations_table.c.operation_id).where(composer_async_operations_table.c.operation_id == operation_id)
            ).one_or_none()
        assert parent is None


@pytest.mark.asyncio
async def test_recompose_durable_stop_before_source_read_never_dispatches_sdk(tmp_path, monkeypatch) -> None:
    sdk = ScriptedSDK([])
    install_sdk(monkeypatch, sdk)
    # Keep the background scanner asleep so Stop is observed before a claim.
    async with continuation_app(tmp_path, monkeypatch, composer_async_scan_interval_seconds=60.0) as harness:
        service = harness.app.state.session_service
        async with acquire_compose_context(service, harness.session_id) as context:
            original = await service.add_message(
                harness.session_id, "user", "Stop this request", writer_principal="route_user_message", session_operation_context=context
            )
        operation_id = str(uuid4())
        worker = harness.app.state.composer_async_worker
        monkeypatch.setattr(worker, "notify_work", lambda: None)
        worker._wake.clear()
        async with harness.client() as client:
            accepted = await client.post(
                f"/api/sessions/{harness.session_id}/recompose",
                json=recompose_body(original.id, operation_id=operation_id),
            )
            assert accepted.status_code == 202, accepted.text
            stopped = await client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel")
            assert stopped.status_code in (200, 202), stopped.text
        await worker.run_until_idle()
        assert sdk.requests == []
        with harness.app.state.session_engine.connect() as conn:
            humans = conn.execute(
                select(chat_messages_table.c.id).where(
                    chat_messages_table.c.session_id == str(harness.session_id), chat_messages_table.c.role == "user"
                )
            ).all()
        assert humans == [(str(original.id),)]
