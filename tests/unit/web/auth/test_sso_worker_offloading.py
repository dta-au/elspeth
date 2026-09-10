"""SSO persistence must let unrelated requests progress during database waits."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace

import pytest

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.sso import AdmittedIdentity, ConsumedHandoff, SsoIdentityRebound
from tests.helpers.fake_idp import FakeIdP
from tests.unit.web.auth.conftest import saturated_worker_pool
from tests.unit.web.auth.test_sso_routes import (
    _app,
    _client,
    _engine,
    _fragment_params,
    _RecordingRecorder,
    _runtime,
    _start,
    _Substrate,
)


def _block_operation[**P, T](
    original: Callable[P, T], *, on_enter: Callable[[], None], release: threading.Event, loop_thread: int
) -> Callable[P, T]:
    def blocked(*args: P.args, **kwargs: P.kwargs) -> T:
        assert threading.get_ident() != loop_thread, "SSO database work ran on the event loop"
        on_enter()
        assert release.wait(timeout=10), "unrelated request failed to progress while SSO database work waited"
        return original(*args, **kwargs)

    return blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["callback", "complete"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_overload_audit_does_not_block_or_disappear_on_cancellation(
    endpoint: str, cancelled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine()
    substrate = _Substrate(engine)
    recorder = _RecordingRecorder()
    idp = FakeIdP()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    audited = asyncio.Event()
    release = threading.Event()
    original = recorder.record_auth_failure

    def record_failure(*args: object, **kwargs: object) -> None:
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10)
        original(*args, **kwargs)
        loop.call_soon_threadsafe(audited.set)

    monkeypatch.setattr(recorder, "record_auth_failure", record_failure)
    try:
        async with _client(_app(sso=_runtime(idp, substrate), recorder=recorder)) as client:
            started = await _start(client)
            code = idp.authorize(nonce=started.nonce, subject="ada", preferred_username="ada.l")
            async with saturated_worker_pool():
                operation = (
                    client.get("/api/auth/sso/callback", params={"code": code, "state": started.state})
                    if endpoint == "callback"
                    else client.post("/api/auth/sso/complete", json={"code": "unused"})
                )
                task = asyncio.create_task(operation)
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    assert not task.done()
                    response = await asyncio.wait_for(client.get("/api/auth/config"), timeout=2)
                    assert response.status_code == 200
                    if cancelled:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                finally:
                    release.set()
                if not cancelled:
                    result = await task
                    assert result.status_code == (302 if endpoint == "callback" else 503)
                async with asyncio.timeout(5):
                    while not audited.is_set():
                        await asyncio.sleep(0.01)
        (failure,) = recorder.of("auth_failure")
        assert failure["failure_stage"] == f"sso_{endpoint}"
        assert failure["failure_category"] == "provider_unavailable"
        assert recorder.of("login_success") == []
        assert recorder.of("token_issued") == []
    finally:
        release.set()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["upsert", "login", "issue", "consume", "read", "token", "callback_failure", "complete_failure"])
async def test_sso_database_wait_does_not_block_other_requests(stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    idp = FakeIdP()
    engine = _engine()
    substrate = _Substrate(engine)
    recorder = _RecordingRecorder()
    runtime = _runtime(idp, substrate)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    calls = 0

    # Keep the real SQLite store and the real IdP/token flow underneath the
    # blocked operation; only the database delay is injected.
    targets = {
        "upsert": runtime.upsert_identity,
        "login": recorder.record_login_success,
        "issue": substrate.handoffs.issue,
        "consume": substrate.handoffs.consume,
        "read": runtime.read_identity,
        "token": recorder.record_token_issued,
        "callback_failure": recorder.record_auth_failure,
        "complete_failure": recorder.record_auth_failure,
    }
    original = targets[stage]

    def on_enter() -> None:
        nonlocal calls
        calls += 1
        loop.call_soon_threadsafe(entered.set)

    blocked = _block_operation(original, on_enter=on_enter, release=release, loop_thread=loop_thread)

    if stage == "upsert":
        runtime = replace(runtime, upsert_identity=blocked)
    elif stage == "read":
        runtime = replace(runtime, read_identity=blocked)
    elif stage in {"issue", "consume"}:
        monkeypatch.setattr(substrate.handoffs, stage, blocked)
    else:
        method = {"login": "record_login_success", "token": "record_token_issued"}
        recorder_method = method[stage] if stage in method else "record_auth_failure"
        monkeypatch.setattr(recorder, recorder_method, blocked)

    try:
        async with _client(_app(sso=runtime, recorder=recorder)) as client:
            started = await _start(client)
            code = idp.authorize(nonce=started.nonce, subject="ada", preferred_username="ada.l")
            if stage in {"consume", "read", "token"}:
                callback = await client.get("/api/auth/sso/callback", params={"code": code, "state": started.state})
                (handoff,) = _fragment_params(callback.headers["location"])["code"]
                operation = client.post("/api/auth/sso/complete", json={"code": handoff})
            elif stage == "complete_failure":
                operation = client.post("/api/auth/sso/complete", json={"code": "unknown"})
            else:
                state = "wrong" if stage == "callback_failure" else started.state
                operation = client.get("/api/auth/sso/callback", params={"code": code, "state": state})

            task = asyncio.create_task(operation)
            observed = asyncio.create_task(entered.wait())
            try:
                done, _pending = await asyncio.wait({task, observed}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    task.result()  # Surface a worker-thread assertion immediately.
                assert entered.is_set(), "SSO operation never reached the database probe"
                assert not task.done()
                response = await asyncio.wait_for(client.get("/api/auth/config"), timeout=2)
                assert response.status_code == 200
            finally:
                release.set()
                observed.cancel()
                await asyncio.gather(observed, return_exceptions=True)
                result = await task

            assert calls == 1
            if stage == "complete_failure":
                assert result.status_code == 401
            elif stage in {"consume", "read", "token"}:
                assert result.status_code == 200
                assert runtime.issuer.decode(result.json()["access_token"]).identity_id == "id-ada"
            else:
                assert result.status_code == 302
            if stage.endswith("failure"):
                (failure,) = recorder.of("auth_failure")
                assert failure["failure_stage"] == ("sso_callback" if stage == "callback_failure" else "sso_complete")
                assert recorder.of("token_issued") == []
    finally:
        release.set()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_callback_audits_worker_refusal_once_even_after_cancellation(cancelled: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine()
    substrate = _Substrate(engine)
    recorder = _RecordingRecorder()
    idp = FakeIdP()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    audited = asyncio.Event()
    release = threading.Event()
    original_audit = recorder.record_auth_failure

    def reject_identity(_claims: IdentityClaims) -> AdmittedIdentity:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10)
        raise SsoIdentityRebound

    def record_failure(*args: object, **kwargs: object) -> None:
        original_audit(*args, **kwargs)
        loop.call_soon_threadsafe(audited.set)

    runtime = replace(_runtime(idp, substrate), upsert_identity=reject_identity)
    monkeypatch.setattr(recorder, "record_auth_failure", record_failure)
    try:
        async with _client(_app(sso=runtime, recorder=recorder)) as client:
            started = await _start(client)
            code = idp.authorize(nonce=started.nonce, subject="ada", preferred_username="ada.l")
            task = asyncio.create_task(client.get("/api/auth/sso/callback", params={"code": code, "state": started.state}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                if cancelled:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                release.set()
            if not cancelled:
                response = await task
                assert response.status_code == 302
                assert _fragment_params(response.headers["location"])["error"] == ["sso_identity_rebound"]
            async with asyncio.timeout(5):
                while not audited.is_set():
                    await asyncio.sleep(0.01)
        (failure,) = recorder.of("auth_failure")
        assert failure["failure_stage"] == "sso_callback"
        assert failure["failure_category"] == "sso_identity_rebound"
        assert recorder.of("login_success") == []
        assert recorder.of("token_issued") == []
    finally:
        release.set()
        engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_complete_still_audits_a_started_handoff_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine()
    substrate = _Substrate(engine)
    recorder = _RecordingRecorder()
    runtime = _runtime(FakeIdP(), substrate)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    audited = asyncio.Event()
    release = threading.Event()
    original_consume = substrate.handoffs.consume
    original_audit = recorder.record_auth_failure

    def delayed_consume(*, code_hash: str) -> ConsumedHandoff | None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10)
        return original_consume(code_hash=code_hash)

    def record_failure(*args: object, **kwargs: object) -> None:
        original_audit(*args, **kwargs)
        loop.call_soon_threadsafe(audited.set)

    monkeypatch.setattr(substrate.handoffs, "consume", delayed_consume)
    monkeypatch.setattr(recorder, "record_auth_failure", record_failure)
    try:
        async with _client(_app(sso=runtime, recorder=recorder)) as client:
            task = asyncio.create_task(client.post("/api/auth/sso/complete", json={"code": "unknown"}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
            # Keep a loop tick active after cancellation, just as the worker
            # helper does while awaited (selector wakeups can be delayed).
            async with asyncio.timeout(5):
                while not audited.is_set():
                    await asyncio.sleep(0.01)
        (failure,) = recorder.of("auth_failure")
        assert failure["failure_stage"] == "sso_complete"
        assert failure["failure_category"] == "sso_handoff_invalid"
        assert recorder.of("token_issued") == []
    finally:
        release.set()
        engine.dispose()
