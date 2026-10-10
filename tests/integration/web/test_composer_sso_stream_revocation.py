"""Mounted SSO revocation while an actual ASGI body send owns a permit."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from starlette.types import Message

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import app as app_module
from elspeth.web.app import create_app
from elspeth.web.async_workers import run_stream_read_in_worker
from elspeth.web.config import WebSettings
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_operations import message_body
from tests.helpers.fake_idp import FakeIdP


async def _login(client: httpx.AsyncClient, idp: FakeIdP, subject: str) -> str:
    start = await client.get("/api/auth/sso/start")
    assert start.status_code == 302
    query = parse_qs(urlsplit(start.headers["location"]).query)
    code = idp.authorize(nonce=query["nonce"][0], subject=subject)
    callback = await client.get("/api/auth/sso/callback", params={"code": code, "state": query["state"][0]})
    assert callback.status_code == 302
    fragment = urlsplit(callback.headers["location"]).fragment
    handoff = parse_qs(urlsplit("x://x/" + fragment).query)["code"][0]
    complete = await client.post("/api/auth/sso/complete", json={"code": handoff})
    assert complete.status_code == 200, complete.text
    return complete.json()["access_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["identity", "role"])
@pytest.mark.parametrize("release_phase", ["before-cutoff", "after-cutoff"])
async def test_mounted_sso_revocation_during_send_preserves_actual_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str, release_phase: str
) -> None:
    idp = FakeIdP()
    resolve = app_module.resolve_sso_runtime

    async def external_idp_transport(wiring, settings, *, transport=None):
        assert transport is None
        return await resolve(wiring, settings, transport=idp.transport())

    monkeypatch.setattr(app_module, "resolve_sso_runtime", external_idp_transport)
    settings = WebSettings(
        data_dir=tmp_path,
        auth_provider="oidc",
        sso_issuer=idp.issuer,
        sso_client_id=idp.client_id,
        sso_client_secret=idp.client_secret,
        sso_transaction_secret="mounted-sso-transaction-test-only-" * 2,  # secret-scan: allow-this-line
        public_base_url="https://composer.test",
        compartment_id="mounted-sso-test",
        sso_admin_subjects=("administrator",),
        secret_key="mounted-sso-token-signing-test-only-key",
        shareable_link_signing_key=bytes(range(32)),
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=60,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=100,
        quota_default_tokens_per_day=100_000,
        quota_default_storage_bytes=1_000_000,
    )
    app = create_app(settings=settings, process_watchdog_factory=OwnedTestProcessWatchdog)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://composer.test") as admin,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://composer.test") as actor,
    ):
        admin_token = await _login(admin, idp, "administrator")
        admin.headers["Authorization"] = f"Bearer {admin_token}"
        provisioned = await admin.post(
            "/api/auth/admin/identities",
            json={"provider": "oidc", "subject": "composer-actor", "role": "user", "note": "Owned stream revocation test"},
        )
        assert provisioned.status_code == 201, provisioned.text
        identity_id = provisioned.json()["identity"]["identity_id"]
        role_id = provisioned.json()["role"]["role_id"]
        actor_token = await _login(actor, idp, "composer-actor")
        actor.headers["Authorization"] = f"Bearer {actor_token}"
        created = await actor.post("/api/sessions", json={"title": "Mounted SSO stream proof"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        operation_id = str(uuid4())
        service = app.state.session_service
        # An actual competing lease keeps this operation queued, without any
        # composer/provider fake, provider request or admission bypass.
        lease = await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=UUID(session_id),
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=30,
        )
        async with lease:
            accepted = await actor.post(
                f"/api/sessions/{session_id}/messages", json=message_body("Explain the current state.", operation_id=operation_id)
            )
            assert accepted.status_code == 202, accepted.text
            path = f"/api/sessions/{session_id}/operations/{operation_id}"
            queued = await actor.get(path)
            assert queued.status_code == 200 and queued.json()["status"] == "queued"
            entered = asyncio.Event()
            release = asyncio.Event()
            completed = asyncio.Event()
            incoming: asyncio.Queue[Message] = asyncio.Queue()
            await incoming.put({"type": "http.request", "body": b"", "more_body": False})
            data_sends: list[bytes] = []
            headers: list[int] = []
            cancellation_received = asyncio.Event()
            physical_send_tasks: list[asyncio.Task] = []

            async def send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers.append(message["status"])
                elif message["type"] == "http.response.body" and message["body"]:
                    physical = asyncio.current_task()
                    assert physical is not None
                    physical_send_tasks.append(physical)
                    data_sends.append(message["body"])
                    assert len(data_sends) == 1, "Revoked SSO observer initiated another data send"
                    entered.set()
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            cancellation_received.set()
                    completed.set()

            stream_path = path + "/stream"
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": stream_path,
                "raw_path": stream_path.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"authorization", f"Bearer {actor_token}".encode()), (b"host", b"composer.test")],
                "client": ("127.0.0.1", 12345),
                "server": ("composer.test", 443),
            }
            subscriber = asyncio.create_task(app(scope, incoming.get, send))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert headers == [200] and app.state.composer_stream_permits.occupied == 1
                if revocation == "identity":
                    refused = await admin.post(
                        f"/api/auth/admin/identities/{identity_id}/disable", json={"reason": "Owned blocked-send revocation"}
                    )
                else:
                    refused = await admin.post(f"/api/auth/admin/roles/{role_id}/revoke", json={"note": "Owned blocked-send revocation"})
                assert refused.status_code == 200, refused.text
                denied = await actor.get(path)
                assert denied.status_code in (401, 403)
                if release_phase == "after-cutoff":
                    await asyncio.wait({subscriber}, timeout=8)
                    snapshot = {
                        "subscriber_done": subscriber.done(),
                        "physical_send_completed": completed.is_set(),
                        "physical_send_cancel_received": cancellation_received.is_set(),
                        "permits_occupied": app.state.composer_stream_permits.occupied,
                        "physical_tasks": [
                            {
                                "same_as_subscriber": task is subscriber,
                                "done": task.done(),
                                "cancelling": task.cancelling(),
                                "stack": [
                                    {"file": frame.f_code.co_filename, "function": frame.f_code.co_name, "line": frame.f_lineno}
                                    for frame in task.get_stack()
                                ],
                            }
                            for task in physical_send_tasks
                        ],
                    }
                    (tmp_path / "mounted-send-custody.json").write_text(json.dumps(snapshot, indent=2))
                    assert subscriber.done(), "Mounted subscriber exceeded cutoff; physical custody snapshot retained"
                    assert cancellation_received.is_set()
                    assert app.state.composer_stream_permits.occupied == 1
                    assert not completed.is_set()
                release.set()
                await asyncio.wait({subscriber}, timeout=5)
                assert subscriber.done(), "Subscriber did not finish after actual physical release"
                await subscriber
                await asyncio.wait_for(completed.wait(), timeout=2)
                deadline = asyncio.get_running_loop().time() + 2
                while app.state.composer_stream_permits.occupied:
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.01)
                assert len(data_sends) == 1

                def actual_side_effect_counts():
                    with app.state.session_engine.connect() as connection:
                        return (
                            connection.execute(
                                select(func.count())
                                .select_from(quota_provider_attempts_table)
                                .where(quota_provider_attempts_table.c.session_id == session_id)
                            ).scalar_one(),
                            connection.execute(
                                select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == session_id)
                            ).scalar_one(),
                        )

                assert await run_stream_read_in_worker(actual_side_effect_counts) == (0, 0)
                if revocation == "identity":
                    restored = await admin.post(
                        f"/api/auth/admin/identities/{identity_id}/enable", json={"note": "Restore solely for owned test cleanup"}
                    )
                else:
                    restored = await admin.post(
                        "/api/auth/admin/roles",
                        json={"identity_id": identity_id, "role": "user", "note": "Restore solely for owned test cleanup"},
                    )
                assert restored.status_code in (200, 201), restored.text
                # Explicit Stop is cleanup after the revocation witness, not
                # the actor that caused stream detachment.
                stopped = await actor.post(path + "/cancel")
                assert stopped.status_code == 200, stopped.text
                assert stopped.json()["status"] == "failed"
                assert stopped.json()["error"]["http_status"] == 499
            finally:
                release.set()
                await incoming.put({"type": "http.disconnect"})
                await asyncio.wait({subscriber}, timeout=8)
                assert subscriber.done(), "Subscriber cleanup incomplete after actual physical release"
                await subscriber
