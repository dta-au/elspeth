"""Actual mounted Composer HTTP actors for spawned PostgreSQL tests."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import litellm
import pytest
from httpx import ASGITransport, AsyncClient
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from sqlalchemy import select
from starlette.types import Message as ASGIMessage

from elspeth.web.app import create_app
from elspeth.web.async_workers import run_stream_read_in_worker
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.config import WebSettings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import session_operation_fences_table
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_operations import message_body, settle_composer_operation


def pg_http_settings(session_url: str, landscape_url: str, data_dir: Path) -> WebSettings:
    return WebSettings(
        data_dir=data_dir,
        session_db_url=session_url,
        landscape_url=landscape_url,
        deployment_target="docker-compose",
        deployment_state_mode="external-postgresql",
        host="0.0.0.0",
        secret_key="external-deployment-secret-key-with-more-than-32-bytes",
        payload_store_path=data_dir / "payloads",
        composer_model="gpt-5.5",
        composer_advisor_model="openai/gpt-4.1",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_boot_probe_enabled=False,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        composer_async_claim_lease_seconds=5,
        shareable_link_signing_key=bytes(range(32)),
        plugin_allowlist=("transform:passthrough",),
    )


@dataclass
class HTTPStreamObservation:
    task: asyncio.Task[None]
    incoming: asyncio.Queue[ASGIMessage]
    first_body: asyncio.Event
    frames: list[dict[str, object]]
    statuses: list[int]


async def observe_mounted_stream(app, *, token: str, session_id: str, operation_id: str) -> HTTPStreamObservation:
    incoming: asyncio.Queue[ASGIMessage] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    first_body = asyncio.Event()
    frames: list[dict[str, object]] = []
    statuses: list[int] = []

    async def send(message: ASGIMessage) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
        elif message["type"] == "http.response.body" and message.get("body"):
            for line in message["body"].decode("utf-8").splitlines():
                if line.startswith("data:"):
                    frame = json.loads(line.removeprefix("data:").strip())
                    assert frame["session_id"] == session_id
                    assert frame["operation_id"] == operation_id
                    frames.append(frame)
                    assert len(frames) <= 200, "unexpected unbounded test stream"
                    first_body.set()

    path = f"/api/sessions/{session_id}/operations/{operation_id}/stream"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {token}".encode()), (b"host", b"composer.test")],
        "client": ("127.0.0.1", 12345),
        "server": ("composer.test", 80),
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    observation = HTTPStreamObservation(task, incoming, first_body, frames, statuses)
    try:
        await asyncio.wait_for(first_body.wait(), timeout=10)
        assert statuses == [200], statuses
    except BaseException:
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=10)
        raise
    return observation


async def detach_stream(app, observation: HTTPStreamObservation) -> None:
    await observation.incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(observation.task, timeout=10)
    deadline = asyncio.get_running_loop().time() + 5
    while app.state.composer_stream_permits.occupied != 0:
        assert asyncio.get_running_loop().time() < deadline, "unfinished stream I/O retained capacity"
        await asyncio.sleep(0.01)
    assert app.state.composer_stream_permits.occupied == 0


def mounted_pg_http_process(
    session_url: str, landscape_url: str, data_dir: str, session_id: str, user_id: str, username: str, pipe: Connection
) -> None:
    try:
        asyncio.run(_serve(session_url, landscape_url, Path(data_dir), session_id, user_id, username, pipe))
    finally:
        pipe.close()


async def _serve(
    session_url: str, landscape_url: str, data_dir: Path, session_id: str, user_id: str, username: str, pipe: Connection
) -> None:
    with pytest.MonkeyPatch.context() as credential_patch:
        credential_patch.setenv("OPENAI_API_KEY", "offline-pg-http-test-only-not-a-provider-credential")
        app = create_app(settings=pg_http_settings(session_url, landscape_url, data_dir), process_watchdog_factory=OwnedTestProcessWatchdog)
        provider = app.state.auth_provider
        assert isinstance(provider, LocalAuthProvider)
        # Token never crosses the process pipe or enters persisted evidence.
        token = provider._issuer.mint(identity_id=user_id, username=username)
        authenticated = await provider.authenticate(token)
        assert authenticated.user_id == user_id
        sdk_entered = asyncio.Event()
        sdk_release = asyncio.Event()
        sdk_calls = 0
        sdk_cancelled = 0
        observations: dict[str, HTTPStreamObservation] = {}

        async def offline_sdk(**kwargs):
            nonlocal sdk_calls, sdk_cancelled
            assert kwargs["model"] == "gpt-5.5"
            assert os.environ["OPENAI_API_KEY"] == "offline-pg-http-test-only-not-a-provider-credential"
            assert kwargs["messages"]
            sdk_calls += 1
            sdk_entered.set()
            try:
                await sdk_release.wait()
            except asyncio.CancelledError:
                sdk_cancelled += 1
                raise
            return ModelResponse(
                choices=[Choices(message=Message(content="Offline provider completed.", role="assistant"), finish_reason="stop")],
                model=kwargs["model"],
                id="offline-pg-http-response",
                usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        with pytest.MonkeyPatch.context() as monkeypatch:
            # Only the external SDK is scripted. Gateway, quota, call ownership,
            # parsing, audit, composer loop, worker and SQL stay actual producers.
            monkeypatch.setattr(litellm, "acompletion", AsyncMock(spec=litellm.acompletion, side_effect=offline_sdk))
            async with (
                app.router.lifespan_context(app),
                AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://composer.test", headers={"Authorization": f"Bearer {token}"}
                ) as client,
            ):
                pipe.send("ready")
                try:
                    while True:
                        command, argument = await asyncio.to_thread(pipe.recv)
                        if command == "stop":
                            break
                        if command == "start":
                            body = message_body("Explain this request without changing the pipeline.", operation_id=argument)
                            response = await client.post(f"/api/sessions/{session_id}/messages", json=body)
                            assert response.status_code == 202, response.text
                            assert response.json()["operation_id"] == argument
                            await asyncio.wait_for(sdk_entered.wait(), timeout=20)
                            assert sdk_calls == 1
                            pipe.send({"accepted": response.json(), "sdk_calls": sdk_calls})
                        elif command == "observe":
                            observations[argument] = await observe_mounted_stream(
                                app, token=token, session_id=session_id, operation_id=argument
                            )
                            assert app.state.composer_stream_permits.occupied == 1
                            pipe.send({"observed": argument, "permits": 1, "sdk_calls": sdk_calls})
                        elif command == "disconnect":
                            await detach_stream(app, observations.pop(argument))
                            pipe.send({"detached": argument, "permits": 0, "sdk_calls": sdk_calls, "sdk_cancelled": sdk_cancelled})
                        elif command == "poll":
                            response = await client.get(f"/api/sessions/{session_id}/operations/{argument}")
                            assert response.status_code == 200, response.text
                            row, job_now = await run_stream_read_in_worker(
                                app.state.composer_async_operation_authority.get_with_database_now,
                                session_id=UUID(session_id),
                                operation_id=argument,
                            )
                            assert row is not None
                            assert row.actor_user_id == user_id

                            def read_fence():
                                with app.state.session_engine.connect() as connection:
                                    fence = (
                                        connection.execute(
                                            select(
                                                session_operation_fences_table.c.operation_id,
                                                session_operation_fences_table.c.operation_epoch,
                                                session_operation_fences_table.c.owner_instance_id,
                                                session_operation_fences_table.c.operation_kind,
                                                session_operation_fences_table.c.lease_expires_at,
                                                session_operation_fences_table.c.released_at,
                                            ).where(session_operation_fences_table.c.session_id == session_id)
                                        )
                                        .mappings()
                                        .one()
                                    )
                                    return dict(fence), database_now(connection)

                            fence, fence_now = await run_stream_read_in_worker(read_fence)
                            assert fence["operation_id"] == row.session_operation_id
                            assert fence["operation_epoch"] == row.session_operation_epoch
                            pipe.send(
                                {
                                    "snapshot": response.json(),
                                    "claim_owner": row.claim_owner_instance_id,
                                    "claim_expires_at": row.claim_expires_at,
                                    "database_now": job_now,
                                    "fence": fence,
                                    "fence_database_now": fence_now,
                                    "attempt": row.attempt,
                                    "cancel_requested_at": row.cancel_requested_at,
                                    "sdk_calls": sdk_calls,
                                    "sdk_cancelled": sdk_cancelled,
                                    "permits": app.state.composer_stream_permits.occupied,
                                }
                            )
                        elif command == "cancel":
                            response = await client.post(f"/api/sessions/{session_id}/operations/{argument}/cancel")
                            assert response.status_code in (200, 202), response.text
                            final = await settle_composer_operation(
                                client, session_id=session_id, operation_id=argument, timeout_seconds=20
                            )
                            assert final.json()["status"] == "failed", final.text
                            assert final.json()["error"]["http_status"] == 499, final.text
                            pipe.send({"terminal": final.json(), "sdk_calls": sdk_calls, "sdk_cancelled": sdk_cancelled})
                        elif command == "finish-observer":
                            observation = observations.pop(argument)
                            await asyncio.wait_for(observation.task, timeout=10)
                            assert any(frame["event"] == "terminal" for frame in observation.frames), observation.frames
                            assert app.state.composer_stream_permits.occupied == 0
                            pipe.send({"terminal_observed": argument, "permits": 0, "sdk_calls": sdk_calls})
                        else:
                            raise AssertionError(f"Unknown lifecycle command: {command}")
                finally:
                    for observation in observations.values():
                        await detach_stream(app, observation)
                    sdk_release.set()
