"""Production app/auth/session setup for durable Composer tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI

from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.config import WebSettings
from elspeth.web.coordination.rate_limit_authority import RepositoryRateLimitAuthority
from elspeth.web.middleware.rate_limit import ComposerRateLimiter, SharedRateLimiter
from tests.fixtures.composer_fakes import DelayedComposerFake
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user


@dataclass(frozen=True, slots=True)
class ComposerOperationApp:
    app: FastAPI
    token: str
    session_id: UUID
    composer: DelayedComposerFake


async def build_composer_operation_app(tmp_path: Path, *, timeout_seconds: float = 300.0) -> ComposerOperationApp:
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=timeout_seconds,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough",),
    )
    app = create_app(settings=settings)
    provider = app.state.auth_provider
    if not isinstance(provider, LocalAuthProvider):
        raise TypeError("Composer test app requires the real local auth provider")
    provider.create_user("transport-user", "test transport password", "Transport User")
    with app.state.session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="transport-user")
        grant_test_pipeline_user(conn, identity_id="transport-user")
    token = provider._issuer.mint(identity_id="transport-user", username="transport-user")
    session = await app.state.session_service.create_session(
        user_id="transport-user", title="Transport evidence", auth_provider_type="local"
    )
    fake = DelayedComposerFake()
    app.state.composer_service.compose = fake.compose
    return ComposerOperationApp(app=app, token=token, session_id=session.id, composer=fake)


async def settle_composer_operation(
    client: httpx.AsyncClient, *, session_id: UUID | str, operation_id: str, timeout_seconds: float = 10.0
) -> httpx.Response:
    """Keep admission/worker/polling on the caller's one running event loop."""
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        response = await client.get(f"/api/sessions/{session_id}/operations/{operation_id}")
        response.raise_for_status()
        if response.json()["status"] in ("completed", "failed"):
            return response
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Composer operation did not settle within the test deadline")
        await asyncio.sleep(0.02)


@dataclass(frozen=True, slots=True)
class SettledComposerOperation:
    accepted: httpx.Response
    final: httpx.Response

    def result(self):
        from elspeth.web.sessions.schemas import ComposerOperationStatusResponse

        snapshot = ComposerOperationStatusResponse.model_validate_json(self.final.content, strict=True)
        if snapshot.result is None:
            raise AssertionError("Composer operation did not complete successfully")
        return snapshot.result.model_dump(mode="json")

    def error(self):
        from elspeth.web.sessions.schemas import ComposerOperationStatusResponse

        snapshot = ComposerOperationStatusResponse.model_validate_json(self.final.content, strict=True)
        if snapshot.error is None:
            raise AssertionError("Composer operation did not fail")
        return snapshot.error.http_status, snapshot.error.body


def install_composer_async_worker(
    app: FastAPI, *, concurrency: int = 4, scan_interval_seconds: float = 1.0, claim_lease_seconds: int = 30, drain_seconds: float = 10.0
):
    from threading import Event

    from elspeth.web.composer.progress import ComposerProgressRegistry
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.middleware.request_id import RequestIdMiddleware
    from elspeth.web.process_recovery import ProcessRecovery
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
    from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog

    service = app.state.session_service
    # Hand-built route fixtures must model the production SQL composer budget.
    # Other local scopes and their adapters stay independent.
    limiter = app.state.rate_limiter
    if isinstance(limiter, ComposerRateLimiter):
        install_composer_rate_limiter(app, limit=limiter._limit)
    authority = ComposerAsyncOperationAuthority(
        app.state.session_engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=claim_lease_seconds
    )
    if "instance_draining" not in app.state._state:
        app.state.instance_draining = Event()
    if "process_recovery" not in app.state._state:
        app.state.process_recovery = ProcessRecovery(
            watchdog=OwnedTestProcessWatchdog(app.state.instance_draining), instance_draining=app.state.instance_draining
        )
    if "composer_progress_registry" not in app.state._state or app.state.composer_progress_registry is None:
        app.state.composer_progress_registry = ComposerProgressRegistry()
    if not any(middleware.cls is RequestIdMiddleware for middleware in app.user_middleware):
        app.add_middleware(RequestIdMiddleware)
    worker = ComposerAsyncWorker(
        app=app,
        authority=authority,
        concurrency=concurrency,
        scan_interval_seconds=scan_interval_seconds,
        claim_lease_seconds=claim_lease_seconds,
        drain_seconds=drain_seconds,
        owner_instance_id=service.session_operation_owner_instance_id,
        process_recovery=app.state.process_recovery,
        instance_draining=app.state.instance_draining,
    )
    app.state.composer_async_operation_authority = authority
    app.state.composer_async_worker = worker
    return worker


def install_composer_rate_limiter(app: FastAPI, *, limit: int) -> SharedRateLimiter:
    """Install one SQL composer bucket for all consumers in a test app."""
    limiter = SharedRateLimiter(
        limit,
        authority=RepositoryRateLimitAuthority(app.state.session_engine, signing_key=b"fixture-composer-quota-key-32!!!!"),
        scope="composer",
    )
    app.state.rate_limiter = limiter
    return limiter


async def submit_and_settle(client: httpx.AsyncClient, app: FastAPI, *, path: str, body, max_rounds: int = 50) -> SettledComposerOperation:
    import asyncio

    accepted = await client.post(path, json=body)
    if accepted.status_code != 202:
        raise AssertionError(f"Composer admission returned {accepted.status_code}: {accepted.text}")
    from elspeth.web.sessions.schemas import ComposerOperationAcceptedResponse, ComposerOperationStatusResponse

    acknowledgement = ComposerOperationAcceptedResponse.model_validate_json(accepted.content, strict=True)
    operation_id = body["operation_id"]
    assert acknowledgement.operation_id == operation_id
    endpoint = path.rsplit("/", 1)[1]
    assert endpoint in ("messages", "recompose")
    expected_kind = "compose_message" if endpoint == "messages" else "compose_recompose"
    assert acknowledgement.kind == expected_kind
    session_id = path.split("/")[-2]
    worker = app.state.composer_async_worker
    await worker.run_until_idle()
    for _ in range(max_rounds):
        terminal = await client.get(f"/api/sessions/{session_id}/operations/{operation_id}")
        if terminal.status_code != 200:
            raise AssertionError(f"Composer observation returned {terminal.status_code}: {terminal.text}")
        snapshot = ComposerOperationStatusResponse.model_validate_json(terminal.content, strict=True)
        assert snapshot.operation_id == operation_id
        assert snapshot.kind == expected_kind
        if snapshot.status in ("completed", "failed"):
            return SettledComposerOperation(accepted=accepted, final=terminal)
        await asyncio.sleep(0.02)
        await worker.run_until_idle()
    raise AssertionError("Composer operation did not settle")


def settle_sync(test_client, app: FastAPI, *, path: str, body) -> SettledComposerOperation:
    assert test_client.app is app

    async def run(client: httpx.AsyncClient) -> SettledComposerOperation:
        return await submit_and_settle(client, app, path=path, body=body)

    return test_client.run_in_one_loop(run)


def message_body(content: str, *, state_id: UUID | str | None = None, operation_id: str | None = None):
    from uuid import uuid4

    return {"content": content, "operation_id": operation_id or str(uuid4()), "state_id": str(state_id) if state_id is not None else None}


def recompose_body(expected_user_message_id: UUID | str, *, state_id: UUID | str | None = None, operation_id: str | None = None):
    from uuid import uuid4

    return {
        "expected_user_message_id": str(expected_user_message_id),
        "operation_id": operation_id or str(uuid4()),
        "state_id": str(state_id) if state_id is not None else None,
    }


def strip_request_id(detail):
    return {key: value for key, value in detail.items() if key != "request_id"}


async def current_head_state_id(client: httpx.AsyncClient, session_id: UUID | str) -> str | None:
    response = await client.get(f"/api/sessions/{session_id}/state")
    response.raise_for_status()
    body = response.json()
    return body["id"] if body is not None else None


def current_head_state_id_sync(test_client, session_id: UUID | str) -> str | None:
    response = test_client.get(f"/api/sessions/{session_id}/state")
    response.raise_for_status()
    body = response.json()
    return body["id"] if body is not None else None


@dataclass(frozen=True)
class RunningComposerOperation:
    accepted: httpx.Response
    drive: asyncio.Task[None]
    operation_id: str
    poll_path: str


@asynccontextmanager
async def running_operation_async(client: httpx.AsyncClient, app: FastAPI, *, path: str, body):
    """Keep admission, worker and the observer's work within the caller's loop."""
    accepted = await client.post(path, json=body)
    assert accepted.status_code == 202, accepted.text
    operation_id = accepted.json()["operation_id"]
    assert operation_id == body["operation_id"]
    session_prefix, _, _route = path.rpartition("/")
    poll_path = f"{session_prefix}/operations/{operation_id}"
    drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
    try:
        yield RunningComposerOperation(accepted, drive, operation_id, poll_path)
    finally:
        if not drive.done():
            cancellation = await client.post(f"{poll_path}/cancel")
            assert cancellation.status_code in (200, 202), cancellation.text
        await drive


def running_operation_sync(test_client, app: FastAPI, *, path: str, body, observe):
    """Run an async live-operation observation through the sync client's one loop."""
    assert test_client.app is app

    async def run(client: httpx.AsyncClient):
        async with running_operation_async(client, app, path=path, body=body) as operation:
            return await observe(client, operation)

    return test_client.run_in_one_loop(run)
