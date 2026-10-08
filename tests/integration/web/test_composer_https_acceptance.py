"""Real TLS harness controls; durable-operation acceptance is added at integration.

These controls prove the instrument distinguishes an early body frame from a
buffered response. They do not claim production composer acceptance by themselves.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from tests.helpers.composer_https import local_composer_tls


@pytest.mark.parametrize("buffered", [False, True], ids=["early-frame-positive", "buffered-frame-negative"])
def test_tls_instrument_requires_body_before_completion(tmp_path: Path, buffered: bool) -> None:
    completed = threading.Event()
    release = threading.Event()

    async def stream(_request):
        async def chunks():
            if not buffered:
                yield b"event: heartbeat\ndata: {}\n\n"
            while not release.is_set():
                await asyncio.sleep(0.01)
            completed.set()
            yield b"event: terminal\ndata: {}\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/control", stream)])
    with (
        local_composer_tls(app, tmp_path / "tls") as tls,
        httpx.Client(base_url=tls.base_url, verify=tls.verify, timeout=0.3) as client,
    ):
        try:
            with client.stream("GET", "/control") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                iterator = response.iter_bytes()
                if buffered:
                    with pytest.raises(httpx.ReadTimeout):
                        next(iterator)
                    assert not completed.is_set()
                else:
                    first = next(iterator)
                    assert b"event: heartbeat\n" in first
                    assert not completed.is_set()
                    release.set()
                    assert b"event: terminal\n" in b"".join(iterator)
                    assert completed.is_set()
        finally:
            release.set()


@pytest.mark.parametrize("idle_cutoff", [False, True], ids=["client-detach", "proxy-idle-cutoff"])
def test_production_operation_survives_tls_disconnect_without_provider_replay(tmp_path: Path, idle_cutoff: bool) -> None:
    """Socket observation detaches while the real app-owned job stays running."""
    import time
    from uuid import uuid4

    from tests.helpers.composer_operations import build_composer_operation_app

    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))
    operation_id = str(uuid4())
    path = f"/api/sessions/{harness.session_id}/operations/{operation_id}"
    body = {"operation_id": operation_id, "content": "Explain the current pipeline.", "state_id": None}
    with (
        local_composer_tls(harness.app, tmp_path / "proxy", upstream_read_timeout="200ms" if idle_cutoff else None) as tls,
        httpx.Client(base_url=tls.base_url, verify=tls.verify, timeout=5, headers={"Authorization": f"Bearer {harness.token}"}) as client,
    ):
        try:
            admitted = client.post(f"/api/sessions/{harness.session_id}/messages", json=body)
            assert admitted.status_code == 202, admitted.text
            assert harness.composer.entered.wait(timeout=5), "real worker did not enter delayed fake composer"
            assert harness.composer.calls == 1
            with client.stream("GET", f"{path}/stream") as response:
                assert response.status_code == 200, response.text
                frames = response.iter_lines()
                if idle_cutoff:
                    with pytest.raises(httpx.RemoteProtocolError):
                        list(frames)
                    assert "i/o timeout" in tls.proxy_log.read_text()
                else:
                    saw_progress = False
                    for line in frames:
                        if line.startswith("data:") and '"event":"progress"' in line:
                            saw_progress = True
                            break
                    assert saw_progress
                pending = client.get(path)
                assert pending.status_code == 200
                assert pending.json()["status"] == "running"
                assert pending.json()["result"] is None
            # Closing the TLS stream must neither cancel nor replay the operation.
            replay = client.post(f"/api/sessions/{harness.session_id}/messages", json=body)
            assert replay.status_code == 202, replay.text
            assert replay.json()["operation_id"] == operation_id
            assert harness.composer.calls == 1
            harness.composer.release.set()
            deadline = time.monotonic() + 10
            while True:
                terminal = client.get(path)
                assert terminal.status_code == 200, terminal.text
                if terminal.json()["status"] in ("completed", "failed"):
                    break
                assert time.monotonic() < deadline, "operation failed to settle"
                time.sleep(0.02)
            assert terminal.json()["status"] == "completed", terminal.text
            assert terminal.json()["result"]["message"]["content"] == "Completed delayed response."
            assert harness.composer.calls == 1
            if not idle_cutoff:
                with client.stream("GET", f"{path}/stream") as response:
                    assert response.status_code == 200
                    assert '"event":"terminal"' in response.read().decode()
            assert client.get(path).json() == terminal.json()
        finally:
            harness.composer.release.set()


def test_browser_observes_real_tls_progress_before_durable_completion(tmp_path: Path) -> None:
    """Chromium consumes actual SSE bytes, then detaches without owning the job."""
    import json
    import subprocess
    import time
    from uuid import uuid4

    from tests.helpers.composer_operations import build_composer_operation_app

    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))
    operation_id = str(uuid4())
    operation_path = f"/api/sessions/{harness.session_id}/operations/{operation_id}"
    browser_program = r"""
import { chromium } from '@playwright/test';
import { readFileSync } from 'node:fs';
const { baseUrl, token, operationPath } = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ headless: true });
try {
  // Trust bypass is limited to this ephemeral self-signed loopback fixture.
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  await page.goto(baseUrl + '/docs');
  const result = await page.evaluate(async ({ token, operationPath }) => {
    const controller = new AbortController();
    const response = await fetch(operationPath + '/stream', {
      headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' },
      signal: controller.signal,
    });
    if (!response.ok || response.body === null) throw new Error('No stream body');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let bytes = '';
    const deadline = setTimeout(() => controller.abort(), 10000);
    try {
      while (!bytes.includes('"event":"progress"')) {
        const chunk = await reader.read();
        if (chunk.done) throw new Error('Stream ended before progress');
        bytes += decoder.decode(chunk.value, { stream: true });
      }
      const pending = await fetch(operationPath, { headers: { Authorization: `Bearer ${token}` } });
      const snapshot = await pending.json();
      return { sawProgress: true, status: snapshot.status, result: snapshot.result };
    } finally {
      clearTimeout(deadline); controller.abort(); await reader.cancel().catch(() => {});
    }
  }, { token, operationPath });
  console.log(JSON.stringify(result));
  await context.close();
} finally { await browser.close(); }
"""
    with (
        local_composer_tls(harness.app, tmp_path / "proxy") as tls,
        httpx.Client(base_url=tls.base_url, verify=tls.verify, timeout=5, headers={"Authorization": f"Bearer {harness.token}"}) as client,
    ):
        try:
            admitted = client.post(
                f"/api/sessions/{harness.session_id}/messages",
                json={
                    "operation_id": operation_id,
                    "content": "Explain this pipeline.",
                    "state_id": None,
                },
            )
            assert admitted.status_code == 202, admitted.text
            assert harness.composer.entered.wait(timeout=5)
            browser = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    browser_program,
                    tls.base_url,
                    harness.token,
                    operation_path,
                ],
                cwd=Path(__file__).parents[3] / "src/elspeth/web/frontend",
                input=json.dumps({"baseUrl": tls.base_url, "token": harness.token, "operationPath": operation_path}),
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            assert browser.returncode == 0, browser.stderr
            observed = json.loads(browser.stdout)
            assert observed == {"sawProgress": True, "status": "running", "result": None}
            assert harness.composer.calls == 1
            harness.composer.release.set()
            deadline = time.monotonic() + 10
            while True:
                snapshot = client.get(operation_path)
                assert snapshot.status_code == 200, snapshot.text
                if snapshot.json()["status"] in ("completed", "failed"):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert snapshot.json()["status"] == "completed", snapshot.text
            assert harness.composer.calls == 1
        finally:
            harness.composer.release.set()


def test_deliberate_caddy_buffering_falls_back_to_same_job_get(tmp_path: Path) -> None:
    """A bounded proxy buffer withholds SSE until the consumer abandons it."""
    import time
    from uuid import uuid4

    from tests.helpers.composer_operations import build_composer_operation_app

    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))
    operation_id = str(uuid4())
    path = f"/api/sessions/{harness.session_id}/operations/{operation_id}"
    with (
        local_composer_tls(harness.app, tmp_path / "buffering-proxy", response_buffer_bytes=65536) as tls,
        httpx.Client(base_url=tls.base_url, verify=tls.verify, timeout=5, headers={"Authorization": f"Bearer {harness.token}"}) as client,
    ):
        try:
            admitted = client.post(
                f"/api/sessions/{harness.session_id}/messages",
                json={
                    "operation_id": operation_id,
                    "content": "Explain this pipeline.",
                    "state_id": None,
                },
            )
            assert admitted.status_code == 202, admitted.text
            assert harness.composer.entered.wait(timeout=5)
            # This must time out on observable delivery, despite the producer's
            # available progress. Merely receiving headers is insufficient.
            with pytest.raises(httpx.ReadTimeout), client.stream("GET", f"{path}/stream", timeout=0.25) as response:
                next(response.iter_bytes())
            pending = client.get(path)
            assert pending.status_code == 200
            assert pending.json()["status"] == "running"
            assert pending.json()["result"] is None
            assert harness.composer.calls == 1
            harness.composer.release.set()
            deadline = time.monotonic() + 10
            while True:
                terminal = client.get(path)
                assert terminal.status_code == 200, terminal.text
                if terminal.json()["status"] in ("completed", "failed"):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert terminal.json()["status"] == "completed", terminal.text
            assert terminal.json()["operation_id"] == operation_id
            assert harness.composer.calls == 1
        finally:
            harness.composer.release.set()


@pytest.mark.parametrize("proxy_mode", ["healthy", "buffered", "idle-cutoff"])
def test_production_browser_observer_completes_same_job_over_tls(tmp_path: Path, proxy_mode: str) -> None:
    """Bundle and execute the shipped observer/custody/decoder inside Chromium."""
    import json
    import subprocess
    from uuid import uuid4

    from starlette.responses import Response

    from tests.helpers.composer_operations import build_composer_operation_app

    frontend = Path(__file__).parents[3] / "src/elspeth/web/frontend"
    bundle_entry = tmp_path / "acceptance-entry.ts"
    bundle_path = tmp_path / "acceptance.js"
    bundle_entry.write_text(
        f'import {{ authenticateComposerCustody }} from "{frontend}/src/stores/composerOperationCustody.ts";\n'
        f'import {{ submitAndObserveComposerOperation }} from "{frontend}/src/api/composerOperationObserver.ts";\n'
        r"""
window.runComposerAcceptance = async ({ token, sessionId, operationId, releaseDelay }) => {
  const originalFetch = window.fetch.bind(window);
  const requests = [];
  let sawProgress = false;
  let sawHeartbeat = false;
  let terminalSeen = false;
  const frameEvents = [];
  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), location.href);
    if (url.origin !== location.origin) throw new Error('Acceptance egress refused');
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${token}`);
    requests.push({ method: init.method ?? 'GET', path: url.pathname });
    const response = await originalFetch(input, { ...init, headers });
    if (!url.pathname.endsWith('/stream') || !response.ok || response.body === null) return response;
    const decoder = new TextDecoder();
    let retained = '';
    const body = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        retained += decoder.decode(chunk, { stream: true });
        if (retained.includes('"event":"heartbeat"') && !terminalSeen) sawHeartbeat = true;
        if (retained.includes('"event":"terminal"')) terminalSeen = true;
        const completed = retained.split('\n\n');
        retained = completed.pop();
        for (const frame of completed) {
          const data = frame.split('\n').find(line => line.startsWith('data:'));
          if (data !== undefined) {
            const value = JSON.parse(data.slice(5));
            frameEvents.push({ event: value.event, phase: value.payload?.phase ?? null, keys: Object.keys(value.payload ?? {}) });
          }
        }
        retained = retained.slice(-65536);
        controller.enqueue(chunk);
      },
    }));
    return new Response(body, { status: response.status, headers: response.headers });
  };
  authenticateComposerCustody({ principalId: 'transport-user', authProvider: 'local' });
  const release = setTimeout(() => void fetch('/__acceptance/release', { method: 'POST' }), releaseDelay);
  try {
    const result = await submitAndObserveComposerOperation({
      mode: 'submitted', scope: { principalId: 'transport-user', authProvider: 'local' },
      sessionId, operationId, kind: 'compose_message', createdAt: Date.now(),
      body: { operation_id: operationId, content: 'Explain the current pipeline.', state_id: null },
    }, { progress: () => { sawProgress = true; } });
    return { message: result.message.content, sawProgress, sawHeartbeat, requests, frameEvents };
  } finally {
    clearTimeout(release); window.fetch = originalFetch;
  }
};
""",
        encoding="utf-8",
    )
    bundled = subprocess.run(
        [
            "node",
            str(frontend / "node_modules/esbuild/bin/esbuild"),
            str(bundle_entry),
            "--bundle",
            "--format=iife",
            "--platform=browser",
            f"--alias:@={frontend}/src",
            f"--outfile={bundle_path}",
        ],
        cwd=frontend,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert bundled.returncode == 0, bundled.stderr
    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))
    operation_id = str(uuid4())

    async def script(_request):
        return Response(bundle_path.read_bytes(), media_type="application/javascript")

    async def release(request):
        if request.headers.get("Authorization") != f"Bearer {harness.token}":
            return Response(status_code=403)
        harness.composer.release.set()
        return Response(status_code=204)

    harness.app.router.routes.insert(0, Route("/__acceptance/observer.js", script))
    harness.app.router.routes.insert(0, Route("/__acceptance/release", release, methods=["POST"]))
    program = r"""
import { chromium } from '@playwright/test';
import { readFileSync } from 'node:fs';
const { baseUrl, token, sessionId, operationId, releaseDelay } = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  await page.goto(baseUrl + '/docs');
  await page.addScriptTag({ url: baseUrl + '/__acceptance/observer.js' });
  const result = await page.evaluate(args => window.runComposerAcceptance(args), {
    token, sessionId, operationId, releaseDelay: Number(releaseDelay),
  });
  console.log(JSON.stringify(result));
  await context.close();
} finally { await browser.close(); }
"""
    with local_composer_tls(
        harness.app,
        tmp_path / "proxy",
        upstream_read_timeout="200ms" if proxy_mode == "idle-cutoff" else None,
        response_buffer_bytes=65536 if proxy_mode == "buffered" else None,
    ) as tls:
        try:
            observed = subprocess.run(
                ["node", "--input-type=module", "-e", program],
                cwd=frontend,
                input=json.dumps(
                    {
                        "baseUrl": tls.base_url,
                        "token": harness.token,
                        "sessionId": str(harness.session_id),
                        "operationId": operation_id,
                        "releaseDelay": 17000 if proxy_mode == "buffered" else 16000 if proxy_mode == "healthy" else 11500,
                    }
                ),
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
            assert observed.returncode == 0, observed.stderr
            evidence = json.loads(observed.stdout)
            evidence_path = tmp_path / f"acceptance-browser-{proxy_mode}-{uuid4()}-evidence.json"
            evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
            proxy_evidence = evidence_path.with_suffix(".proxy.log")
            proxy_evidence.write_text(tls.proxy_log.read_text(), encoding="utf-8")
            assert evidence["message"] == "Completed delayed response."
            admission_path = f"/api/sessions/{harness.session_id}/messages"
            durable_path = f"/api/sessions/{harness.session_id}/operations/{operation_id}"
            assert sum(item == {"method": "POST", "path": admission_path} for item in evidence["requests"]) == 1
            assert {"method": "GET", "path": durable_path} in evidence["requests"]
            assert harness.composer.calls == 1
            if proxy_mode == "healthy":
                assert evidence["sawProgress"], evidence
                assert evidence["sawHeartbeat"], evidence
            else:
                assert not evidence["sawHeartbeat"]
        finally:
            harness.composer.release.set()


def test_four_live_tls_readers_leave_auth_and_worker_terminal_usable(tmp_path: Path) -> None:
    """Measure one principal's supported reader cap while real workers settle.

    Readers deliberately stop consuming after their first frame. This is a live
    socket/permit/auth/terminal contention proof at four readers, not proof that
    32 readers or cancellation-resistant SQL is supportable.
    """
    import time
    from contextlib import ExitStack
    from uuid import uuid4

    from elspeth.web.composer_stream import ComposerStreamPermits
    from tests.helpers.composer_operations import build_composer_operation_app

    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))
    permits = harness.app.state.composer_stream_permits
    assert isinstance(permits, ComposerStreamPermits)
    paths: list[str] = []
    with (
        local_composer_tls(harness.app, tmp_path / "proxy") as tls,
        httpx.Client(base_url=tls.base_url, verify=tls.verify, timeout=5, headers={"Authorization": f"Bearer {harness.token}"}) as client,
    ):
        try:
            for index in range(4):
                session = client.post("/api/sessions", json={"title": f"Reader contention {index}"})
                assert session.status_code in (200, 201), session.text
                session_id = session.json()["id"]
                operation_id = str(uuid4())
                admitted = client.post(
                    f"/api/sessions/{session_id}/messages",
                    json={
                        "operation_id": operation_id,
                        "content": "Explain this pipeline.",
                        "state_id": None,
                    },
                )
                assert admitted.status_code == 202, admitted.text
                paths.append(f"/api/sessions/{session_id}/operations/{operation_id}")
            deadline = time.monotonic() + 10
            while harness.composer.calls != 4:
                assert time.monotonic() < deadline
                time.sleep(0.02)
            with ExitStack() as readers:
                body_readers = []
                for path in paths:
                    stream = readers.enter_context(client.stream("GET", f"{path}/stream"))
                    assert stream.status_code == 200
                    body_reader = stream.iter_bytes()
                    body_readers.append(body_reader)
                    assert next(body_reader)
                assert permits.occupied == 4
                # Negative control: a fifth reader on this principal cannot
                # consume an additional slot, even on an already-owned job.
                refused = client.get(f"{paths[0]}/stream")
                assert refused.status_code == 503, refused.text
                assert permits.occupied == 4
                ordinary = client.get("/api/auth/me")
                assert ordinary.status_code == 200, ordinary.text
                assert ordinary.json()["user_id"] == "transport-user"
                harness.composer.release.set()
                deadline = time.monotonic() + 10
                for path in paths:
                    while True:
                        snapshot = client.get(path)
                        assert snapshot.status_code == 200, snapshot.text
                        if snapshot.json()["status"] in ("completed", "failed"):
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.02)
                    assert snapshot.json()["status"] == "completed", snapshot.text
                assert harness.composer.calls == 4
            deadline = time.monotonic() + 3
            while permits.occupied:
                assert time.monotonic() < deadline, "real TLS readers retained subscription permits after close"
                time.sleep(0.02)
            assert permits.occupied == 0
        finally:
            harness.composer.release.set()


def test_eight_tls_readers_bound_real_pool_submissions_and_settle_jobs(
    tmp_path: Path,
) -> None:
    """Combine real sockets and auth with actual bounded executor custody.

    The Composer response is the existing local delayed double. This proves
    reader/pool/terminal contention; provider authoring has its separate proof.
    """
    import time
    from contextlib import ExitStack
    from uuid import uuid4

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from elspeth.web.async_workers import (
        ADMISSION_CAPACITY,
        MAX_WORKERS,
        AsyncWorkerAdmissionTimeoutError,
        outstanding_admissions,
        run_sync_in_worker,
    )
    from elspeth.web.auth.local import LocalAuthProvider
    from elspeth.web.composer_stream import ComposerStreamPermits
    from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
    from tests.helpers.composer_operations import build_composer_operation_app

    assert MAX_WORKERS == 16 and ADMISSION_CAPACITY == 32
    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))
    provider = harness.app.state.auth_provider
    assert isinstance(provider, LocalAuthProvider)
    provider.create_user("second-reader", "local reader fixture password", "Second Reader")
    with harness.app.state.session_engine.begin() as connection:
        ensure_test_identity(connection, identity_id="second-reader")
        grant_test_pipeline_user(connection, identity_id="second-reader")
    provider.create_user("third-reader", "local reader fixture password", "Third Reader")
    with harness.app.state.session_engine.begin() as connection:
        ensure_test_identity(connection, identity_id="third-reader")
        grant_test_pipeline_user(connection, identity_id="third-reader")
    third_token = provider._issuer.mint(identity_id="third-reader", username="third-reader")
    second_token = provider._issuer.mint(identity_id="second-reader", username="second-reader")
    permits = harness.app.state.composer_stream_permits
    assert isinstance(permits, ComposerStreamPermits)
    release_pool = threading.Event()
    count_lock = threading.Lock()
    started = 0
    completed = 0
    unexpected_execution = False
    tasks: list[asyncio.Task[object]] = []

    def held_actual_read() -> object:
        nonlocal started, completed
        with count_lock:
            started += 1
        try:
            assert release_pool.wait(timeout=15), "test-owned pool gate was not released"
            return harness.app.state.session_service.get_session_for_stream(harness.session_id)
        finally:
            with count_lock:
                completed += 1

    def overflow_callable() -> None:
        nonlocal unexpected_execution
        unexpected_execution = True

    async def pool_control(request: Request) -> JSONResponse:
        if request.headers.get("Authorization") != f"Bearer {harness.token}":
            return JSONResponse({}, status_code=403)
        action = request.path_params["action"]
        if action == "start":
            assert not tasks
            tasks.extend(asyncio.create_task(run_sync_in_worker(held_actual_read)) for _ in range(ADMISSION_CAPACITY))
            async with asyncio.timeout(5):
                while started != MAX_WORKERS or outstanding_admissions() != ADMISSION_CAPACITY:
                    await asyncio.sleep(0.01)
            return JSONResponse(
                {
                    "started": started,
                    "completed": completed,
                    "outstanding": outstanding_admissions(),
                }
            )
        if action == "overflow":
            try:
                async with asyncio.timeout(2.5):
                    await run_sync_in_worker(overflow_callable)
            except AsyncWorkerAdmissionTimeoutError:
                return JSONResponse({"refused": True, "executed": unexpected_execution})
            raise AssertionError("Thirty-third pool submission escaped the admission bound")
        if action == "cancel":
            for task in tasks:
                task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
            return JSONResponse(
                {
                    "cancelled": len(outcomes),
                    "started": started,
                    "completed": completed,
                    "outstanding": outstanding_admissions(),
                }
            )
        raise AssertionError("Unknown test-owned pool control")

    harness.app.router.routes.insert(0, Route("/__acceptance/pool/{action}", pool_control, methods=["POST"]))
    paths: list[tuple[httpx.Client, str]] = []
    with (
        local_composer_tls(harness.app, tmp_path / "proxy") as tls,
        ExitStack() as clients,
    ):
        first = clients.enter_context(
            httpx.Client(
                base_url=tls.base_url,
                verify=tls.verify,
                timeout=5,
                headers={"Authorization": f"Bearer {harness.token}"},
            )
        )
        second = clients.enter_context(
            httpx.Client(
                base_url=tls.base_url,
                verify=tls.verify,
                timeout=5,
                headers={"Authorization": f"Bearer {second_token}"},
            )
        )
        third = clients.enter_context(
            httpx.Client(base_url=tls.base_url, verify=tls.verify, timeout=5, headers={"Authorization": f"Bearer {third_token}"})
        )
        try:
            for client, reader_count in ((first, 3), (second, 4), (third, 1)):
                for index in range(reader_count):
                    session = client.post("/api/sessions", json={"title": f"Pool reader {index}"})
                    assert session.status_code == 201, session.text
                    session_id = session.json()["id"]
                    operation_id = str(uuid4())
                    admitted = client.post(
                        f"/api/sessions/{session_id}/messages",
                        json={
                            "operation_id": operation_id,
                            "content": "Explain this pipeline.",
                            "state_id": None,
                        },
                    )
                    assert admitted.status_code == 202, admitted.text
                    paths.append(
                        (
                            client,
                            f"/api/sessions/{session_id}/operations/{operation_id}",
                        )
                    )
            with ExitStack() as readers:
                body_readers = []
                for client, path in paths:
                    stream = readers.enter_context(client.stream("GET", path + "/stream"))
                    assert stream.status_code == 200
                    body_reader = stream.iter_bytes()
                    body_readers.append(body_reader)
                    assert next(body_reader), "TLS headers alone are not a delivered body frame"
                assert permits.occupied == 8
                # This principal and operation each hold only one reader,
                # so the ninth refusal discriminates the global bound.
                with third.stream("GET", paths[-1][1] + "/stream") as ninth:
                    assert ninth.status_code == 503, "Global reader cap admitted a ninth stream"
                assert permits.occupied == 8
                saturated = first.post("/__acceptance/pool/start")
                assert saturated.status_code == 200, saturated.text
                assert saturated.json() == {
                    "started": 16,
                    "completed": 0,
                    "outstanding": 32,
                }
                overflow = first.post("/__acceptance/pool/overflow")
                assert overflow.status_code == 200, overflow.text
                assert overflow.json() == {"refused": True, "executed": False}
                abandoned = first.post("/__acceptance/pool/cancel")
                assert abandoned.status_code == 200, abandoned.text
                state = abandoned.json()
                assert state["cancelled"] == 32 and state["started"] == 16 and state["completed"] == 0
                assert 16 <= state["outstanding"] <= 32, "cancellation released a live physical invocation"
                release_pool.set()
                for client, identity in (
                    (first, "transport-user"),
                    (second, "second-reader"),
                    (third, "third-reader"),
                ):
                    ordinary = client.get("/api/auth/me")
                    assert ordinary.status_code == 200, ordinary.text
                    assert ordinary.json()["user_id"] == identity
                harness.composer.release.set()
                deadline = time.monotonic() + 10
                for client, path in paths:
                    while True:
                        snapshot = client.get(path)
                        assert snapshot.status_code == 200, snapshot.text
                        if snapshot.json()["status"] in ("completed", "failed"):
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.02)
                    assert snapshot.json()["status"] == "completed", snapshot.text
                assert harness.composer.calls == 8
            deadline = time.monotonic() + 5
            while permits.occupied or outstanding_admissions():
                assert time.monotonic() < deadline, "TLS or physical worker custody leaked after completion"
                time.sleep(0.02)
            assert completed == started == 16
            assert permits.occupied == outstanding_admissions() == 0
        finally:
            release_pool.set()
            harness.composer.release.set()


def test_tls_global_cap_control_detects_removed_global_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ninth admitted reader must make the complete real-TLS control fail."""
    from elspeth.web.composer_stream import ComposerStreamCapacityError, ComposerStreamPermit, ComposerStreamPermits

    def acquire_without_global_bound(self: ComposerStreamPermits, operation_id: str, principal_id: str) -> ComposerStreamPermit:
        permit = ComposerStreamPermit(operation_id, principal_id)
        operation_count = sum(count for key, count in self._counts.items() if key.operation_id == operation_id)
        principal_count = sum(count for key, count in self._counts.items() if key.principal_id == principal_id)
        if operation_count >= 2 or principal_count >= 4:
            raise ComposerStreamCapacityError
        self._counts[permit] = self._counts.get(permit, 0) + 1
        return permit

    monkeypatch.setattr(ComposerStreamPermits, "acquire", acquire_without_global_bound)
    with pytest.raises(AssertionError, match="Global reader cap admitted a ninth stream"):
        test_eight_tls_readers_bound_real_pool_submissions_and_settle_jobs(tmp_path)


@pytest.mark.parametrize("scenario", ["late-sql-stop", "two-tabs-reload-stop", "auth-refresh-stale-terminal", "malformed-quarantine"])
def test_browser_custody_races_with_real_tls_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    """Shipped browser custody/reducer against real auth, SQL admission and worker.

    The deterministic composer is offline; the generated page is a module harness,
    not a claim about React rendering or planner/provider authoring.
    """
    import json
    import subprocess
    from uuid import uuid4

    from starlette.responses import JSONResponse, Response

    from elspeth.web.async_workers import run_sync_in_worker
    from elspeth.web.auth.middleware import get_current_user
    from elspeth.web.composer import provider_gateway
    from tests.helpers.composer_operations import build_composer_operation_app

    provider_attempts = []

    async def refuse_provider(*args, **kwargs):
        provider_attempts.append(True)
        raise AssertionError("Offline browser acceptance cannot invoke a provider")

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", refuse_provider)
    with pytest.raises(AssertionError, match="cannot invoke a provider"):
        asyncio.run(provider_gateway._litellm_acompletion())
    assert provider_attempts == [True]
    provider_attempts.clear()
    frontend = Path(__file__).parents[3] / "src/elspeth/web/frontend"
    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=45))
    assert harness.app.state.settings.composer_boot_probe_enabled is False
    authority = harness.app.state.composer_async_operation_authority
    operation_id, other_id = str(uuid4()), str(uuid4())
    admission_entered, admission_release, admission_finished = threading.Event(), threading.Event(), threading.Event()
    admission_calls = []
    original_admit = authority.admit

    def delayed_admit(**kwargs):
        admission_calls.append(kwargs["operation_id"])
        try:
            if scenario == "late-sql-stop" and kwargs["operation_id"] == operation_id:
                # Hold the actual admitted worker BEFORE it opens the SQL transaction.
                # Neither a connection nor a lock is held while browser transport dies.
                admission_entered.set()
                if not admission_release.wait(20):
                    raise TimeoutError("Browser failed to release its owned admission worker")
            return original_admit(**kwargs)
        finally:
            admission_finished.set()

    monkeypatch.setattr(authority, "admit", delayed_admit)
    bundle_entry, bundle_path = tmp_path / "custody-entry.ts", tmp_path / "custody.js"
    bundle_entry.write_text(
        f'import * as custody from "{frontend}/src/stores/composerOperationCustody.ts";\n'
        f'import * as observer from "{frontend}/src/api/composerOperationObserver.ts";\n'
        f'import * as api from "{frontend}/src/api/client.ts";\n'
        f'import {{ useAuthStore }} from "{frontend}/src/stores/authStore.ts";\n'
        r"""
const requests = [];
let admissionAbort = null;
let heldTerminal = null;
let releaseTerminal = null;
let holdNextTerminal = false;
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = new URL(String(input), location.href);
  if (url.origin !== location.origin) throw new Error('Browser acceptance egress refused');
  const headers = new Headers(init.headers);
  const token = localStorage.getItem('auth_token');
  const item = { method: init.method ?? 'GET', path: url.pathname,
    publicConfiguration: url.pathname === '/api/auth/config',
    authorized: headers.get('Authorization') === (url.pathname === '/api/auth/config' || token === null ? null : `Bearer ${token}`) };
  if (item.method === 'POST' && url.pathname.endsWith('/messages')) item.operationId = JSON.parse(init.body).operation_id;
  requests.push(item);
  const response = await originalFetch(input, { ...init, signal: item.method === 'POST' && url.pathname.endsWith('/messages') && admissionAbort !== null ? admissionAbort.signal : init.signal });
  item.status = response.status;
  if (item.method === 'GET' && /\/operations\/[^/]+$/.test(url.pathname) && response.ok) {
    const snapshot = await response.clone().json();
    item.jobStatus = snapshot.status;
    if (holdNextTerminal && snapshot.status === 'completed') {
      holdNextTerminal = false;
      heldTerminal = { operationId: snapshot.operation_id, status: snapshot.status };
      await new Promise(resolve => { releaseTerminal = resolve; });
    }
  }
  return response;
};
const scope = { principalId: 'transport-user', authProvider: 'local' };
let sessionId;
let stateId = null;
let pending = null;
let outcome = null;
window.custodyAcceptance = {
  async boot(token, sid) {
    sessionId = sid;
    localStorage.setItem('auth_token', token);
    const user = await api.fetchCurrentUser({ logoutOnUnauthorized: false });
    const config = await api.fetchAuthConfig();
    if (user.user_id !== scope.principalId || config.provider !== scope.authProvider) throw new Error('Authenticated custody scope mismatch');
    custody.authenticateComposerCustody({ principalId: user.user_id, authProvider: config.provider });
    stateId = (await api.fetchCompositionState(sid))?.id ?? null;
  },
  descriptor(id) { return { mode: 'submitted', scope, sessionId, operationId: id, kind: 'compose_message', createdAt: Date.now(), body: { operation_id: id, content: 'Explain the current pipeline.', state_id: stateId } }; },
  start(id, restore = false) {
    outcome = null;
    const descriptor = restore ? custody.findComposerOperationCustody(scope, sessionId).foreground : this.descriptor(id);
    if (descriptor === null) throw new Error('Missing exact restored custody');
    pending = (restore ? observer.observeComposerOperation(descriptor) : observer.submitAndObserveComposerOperation(descriptor)).then(
      result => { outcome = { status: 'completed', content: result.message.content }; },
      error => { outcome = { status: 'error', name: error.name ?? null, httpStatus: error.status ?? null }; },
    );
  },
  state() {
    const record = custody.findComposerOperationCustody(scope, sessionId);
    return { requests, outcome, foreground: record.foreground, unresolved: [...record.unresolvedSubmissions.values()], raw: sessionStorage.getItem(custody.COMPOSER_CUSTODY_KEY), notice: custody.composerCustodyRecoveryNotice(), heldTerminal };
  },
  async control(action = 'state') {
    const response = await fetch('/__acceptance/custody-control/' + action, { method: action === 'state' ? 'GET' : 'POST', headers: { Authorization: `Bearer ${localStorage.getItem('auth_token')}` } });
    if (!response.ok) throw new Error('Control denied');
    return response.status === 204 ? null : response.json();
  },
  abortAdmission() { admissionAbort.abort(); observer.detachComposerObservers(); },
  holdAdmission() { admissionAbort = new AbortController(); },
  stop() { return observer.cancelObservedComposerOperation(sessionId); },
  holdTerminal() { holdNextTerminal = true; },
  releaseTerminal() { releaseTerminal(); },
  async refresh(token) { await useAuthStore.getState().loginWithToken(token); },
  async refreshCredentials() {
    const previous = localStorage.getItem('auth_token');
    const { access_token } = await api.refreshToken();
    if (access_token === previous) throw new Error('Real refresh did not replace bearer');
    await useAuthStore.getState().loginWithToken(access_token);
    return localStorage.getItem('auth_token') === access_token;
  },
  async logout() { await useAuthStore.getState().logout(); },
  async loggedOutRefusal() { return (await fetch('/api/auth/me')).status; },
  async preStopped(id) {
    const controller = new AbortController(); controller.abort('user_cancel');
    try { await observer.submitAndObserveComposerOperation(this.descriptor(id), { signal: controller.signal }); }
    catch (error) { return error.name; }
    throw new Error('Stopped action unexpectedly published');
  },
  async trySubmit(id) {
    try { await observer.submitAndObserveComposerOperation(this.descriptor(id)); }
    catch (error) { return error.message; }
    throw new Error('Quarantined storage unexpectedly submitted');
  },
};
""",
        encoding="utf-8",
    )
    bundled = subprocess.run(
        [
            "node",
            str(frontend / "node_modules/esbuild/bin/esbuild"),
            str(bundle_entry),
            "--bundle",
            "--format=iife",
            "--platform=browser",
            f"--alias:@={frontend}/src",
            f"--outfile={bundle_path}",
        ],
        cwd=frontend,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert bundled.returncode == 0, bundled.stderr

    async def script(_request):
        return Response(bundle_path.read_bytes(), media_type="application/javascript")

    async def control(request):
        user = await get_current_user(request)
        if user.user_id != "transport-user":
            return Response(status_code=403)
        action = request.path_params["action"]
        if action == "release-admission":
            admission_release.set()
            return Response(status_code=204)
        if action == "release-composer":
            harness.composer.release.set()
            return Response(status_code=204)
        if action != "state":
            return Response(status_code=404)
        record = await run_sync_in_worker(authority.get, session_id=harness.session_id, operation_id=operation_id)
        return JSONResponse(
            {
                "admissionHeld": admission_entered.is_set() and not admission_finished.is_set(),
                "admissionFinished": admission_finished.is_set(),
                "jobMissing": record is None,
                "jobStatus": record.status if record is not None else None,
                "composerEntered": harness.composer.entered.is_set(),
                "composerCalls": harness.composer.calls,
            }
        )

    harness.app.router.routes.insert(0, Route("/__acceptance/custody.js", script))
    harness.app.router.routes.insert(0, Route("/__acceptance/custody-control/{action}", control, methods=["GET", "POST"]))
    program = r"""
import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
const { baseUrl, token, sessionId, operationId, otherId, scenario } = JSON.parse(await new Promise(resolve => { let text=''; process.stdin.on('data', chunk => text += chunk); process.stdin.on('end', () => resolve(text)); }));
const browser = await chromium.launch({ headless: true });
const browserDeadline = setTimeout(() => void browser.close(), 40000);
const evidence = { scenario, witnesses: {} };
async function waitFor(read, predicate, label) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) { const value = await read(); if (predicate(value)) return value; await new Promise(resolve => setTimeout(resolve, 30)); }
  throw new Error('Timed out: ' + label);
}
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  await context.route('**/*', route => new URL(route.request().url()).origin === baseUrl ? route.continue() : route.abort('blockedbyclient'));
  async function load(page) { await page.goto(baseUrl + '/docs'); await page.addScriptTag({ url: baseUrl + '/__acceptance/custody.js' }); await page.evaluate(({token, sessionId}) => window.custodyAcceptance.boot(token, sessionId), { token, sessionId }); }
  const a = await context.newPage(); await load(a);
  const read = page => page.evaluate(() => window.custodyAcceptance.state());
  const backend = () => a.evaluate(() => window.custodyAcceptance.control());
  if (scenario === 'late-sql-stop') {
    await a.evaluate(id => { window.custodyAcceptance.holdAdmission(); window.custodyAcceptance.start(id); }, operationId);
    evidence.witnesses.beforeAbort = await waitFor(backend, x => x.admissionHeld && x.jobMissing, 'real admission worker held before SQL');
    await a.evaluate(() => window.custodyAcceptance.abortAdmission());
    await waitFor(() => read(a), x => x.outcome?.name === 'ComposerObservationDetached', 'transport detach');
    const before = await read(a); assert.equal(before.foreground.operationId, operationId); assert.equal(before.foreground.body.operation_id, operationId);
    evidence.beforeReloadRequests = before.requests;
    await load(a);
    await a.evaluate(id => window.custodyAcceptance.start(id, true), operationId);
    await waitFor(() => read(a), x => x.requests.some(r => r.path.endsWith('/operations/' + operationId) && r.status === 404), 'real missing operation snapshot after reload');
    await a.evaluate(() => window.custodyAcceptance.stop());
    const missingStop = await waitFor(() => read(a), x => x.requests.some(r => r.path.endsWith('/cancel') && r.status === 404), 'missing cancellation before late SQL');
    assert.equal(missingStop.foreground.body.operation_id, operationId);
    assert.deepEqual(missingStop.foreground.body, before.foreground.body);
    assert.deepEqual(missingStop.foreground.scope, before.foreground.scope);
    evidence.witnesses.missingStopRetainedExactBody = true;
    evidence.witnesses.whileMissing = await backend(); assert.equal(evidence.witnesses.whileMissing.admissionHeld, true);
    await a.evaluate(() => window.custodyAcceptance.control('release-admission'));
    const terminal = await waitFor(() => read(a), x => x.outcome !== null, 'late SQL committed and Stop settled');
    assert.equal(terminal.outcome.httpStatus, 499); assert.equal(terminal.foreground, null);
    assert.equal(terminal.requests.filter(r => r.method === 'POST' && r.path.endsWith('/messages')).length, 0);
    evidence.afterReload = terminal; evidence.witnesses.terminal = await backend(); assert.equal(evidence.witnesses.terminal.jobStatus, 'failed');
  } else if (scenario === 'two-tabs-reload-stop') {
    await a.evaluate(id => window.custodyAcceptance.start(id), operationId);
    await waitFor(backend, x => x.composerEntered, 'accepted composer running');
    const b = await context.newPage(); await load(b);
    await b.evaluate(id => window.custodyAcceptance.start(id), otherId);
    const attached = await waitFor(() => read(b), x => x.foreground?.mode === 'observer', 'distinct losing action attaches observation-only');
    assert.equal(attached.foreground.operationId, operationId); assert.equal('body' in attached.foreground, false); assert.deepEqual(attached.unresolved, []);
    assert.equal(attached.requests.find(r => r.operationId === otherId).status, 409);
    evidence.beforeReloadRequests = attached.requests; evidence.witnesses.attachment = attached.foreground;
    await load(b); await b.evaluate(id => window.custodyAcceptance.start(id, true), operationId);
    assert.equal((await read(b)).foreground.mode, 'observer');
    await b.evaluate(() => window.custodyAcceptance.stop());
    const stoppedB = await waitFor(() => read(b), x => x.outcome !== null, 'restored observer Stop terminal');
    const stoppedA = await waitFor(() => read(a), x => x.outcome !== null, 'first tab observes same Stop terminal');
    assert.equal(stoppedA.outcome.httpStatus, 499); assert.equal(stoppedB.outcome.httpStatus, 499);
    assert.equal(stoppedB.requests.filter(r => r.method === 'POST' && r.path.endsWith('/messages')).length, 0);
    const postCount = stoppedA.requests.filter(r => r.method === 'POST' && r.path.endsWith('/messages')).length;
    assert.equal(await a.evaluate(id => window.custodyAcceptance.preStopped(id), crypto.randomUUID()), 'ComposerObservationDetached');
    assert.equal((await read(a)).requests.filter(r => r.method === 'POST' && r.path.endsWith('/messages')).length, postCount);
    evidence.firstTab = await read(a); evidence.afterReload = stoppedB; evidence.witnesses.stopBeforePostDispatched = false;
  } else if (scenario === 'auth-refresh-stale-terminal') {
    await a.evaluate(id => { window.custodyAcceptance.holdTerminal(); window.custodyAcceptance.start(id); }, operationId);
    await waitFor(backend, x => x.composerEntered, 'auth case composer running');
    await a.evaluate(() => window.custodyAcceptance.control('release-composer'));
    const held = await waitFor(() => read(a), x => x.heldTerminal !== null, 'real committed terminal GET held before browser publication');
    assert.equal(held.outcome, null); assert.equal(held.foreground.operationId, operationId);
    evidence.witnesses.heldRealTerminal = held.heldTerminal;
    evidence.witnesses.actualRefreshChangedBearer = await a.evaluate(() => window.custodyAcceptance.refreshCredentials());
    assert.equal(evidence.witnesses.actualRefreshChangedBearer, true);
    await a.evaluate(() => window.custodyAcceptance.releaseTerminal());
    const stale = await waitFor(() => read(a), x => x.outcome !== null, 'stale terminal rejected after real same-principal refresh');
    assert.equal(stale.outcome.name, 'ComposerObservationDetached');
    await waitFor(() => read(a), x => x.foreground === null, 'new authenticated generation reconciles exact terminal');
    evidence.backend = await backend();
    await a.evaluate(() => window.custodyAcceptance.logout());
    evidence.witnesses.loggedOutRefusal = await a.evaluate(() => window.custodyAcceptance.loggedOutRefusal());
    assert.equal(evidence.witnesses.loggedOutRefusal, 401);
    evidence.afterLogout = await read(a); assert.equal(evidence.afterLogout.raw, null); assert.equal(evidence.afterLogout.foreground, null);
    assert.ok(evidence.afterLogout.requests.filter(r => r.path === '/api/auth/me').length >= 3);
    assert.ok(evidence.afterLogout.requests.filter(r => r.path === '/api/auth/config').length >= 3);
  } else {
    await a.evaluate(id => window.custodyAcceptance.start(id), operationId);
    await waitFor(backend, x => x.composerEntered, 'quarantine case composer running');
    const valid = (await read(a)).foreground;
    const raw = JSON.stringify({ schema: 'composer-operations.v1', entries: [
      { foreground: valid, unresolvedSubmissions: [] },
      { foreground: { ...valid, mode: 'observer', operationId: otherId }, unresolvedSubmissions: [] },
    ] });
    await a.evaluate(raw => sessionStorage.setItem('elspeth_composer_operations_v1', raw), raw);
    await load(a);
    const hydrated = await read(a); assert.equal(hydrated.foreground.operationId, operationId); assert.ok(hydrated.notice.includes('read-only recovery')); assert.equal(hydrated.raw, raw);
    evidence.witnesses.quarantineNotice = hydrated.notice;
    await a.evaluate(id => window.custodyAcceptance.start(id, true), operationId);
    await a.evaluate(() => window.custodyAcceptance.control('release-composer'));
    const recovered = await waitFor(() => read(a), x => x.outcome !== null, 'valid sibling reconciles through real durable GET');
    assert.equal(recovered.outcome.content, 'Completed delayed response.'); assert.equal(recovered.raw, raw); assert.equal(recovered.foreground, null);
    const refused = await a.evaluate(id => window.custodyAcceptance.trySubmit(id), crypto.randomUUID());
    assert.ok(refused.includes('reconcile composer custody')); assert.equal((await read(a)).requests.filter(r => r.method === 'POST' && r.path.endsWith('/messages')).length, 0);
    evidence.witnesses.refusedNewSubmission = refused;
    await load(a); assert.equal((await read(a)).raw, raw); assert.ok((await read(a)).notice.includes('read-only recovery'));
    await a.evaluate(() => window.custodyAcceptance.logout()); const cleared = await read(a); assert.equal(cleared.raw, null); assert.equal(cleared.notice, null);
    await a.evaluate(token => window.custodyAcceptance.refresh(token), token);
    await load(a);
    await a.evaluate(id => window.custodyAcceptance.start(id), otherId);
    const fresh = await waitFor(() => read(a), x => x.outcome !== null, 'logout clears quarantine and real fresh admission succeeds');
    assert.equal(fresh.outcome.status, 'completed'); evidence.afterLogoutFreshAction = fresh;
    evidence.witnesses.validRecovery = recovered;
  }
  if (scenario !== 'auth-refresh-stale-terminal') evidence.backend = await backend();
  for (const value of Object.values(evidence)) if (value && Array.isArray(value.requests)) assert.ok(value.requests.every(r => r.authorized && !r.path.includes(token)));
  console.log(JSON.stringify(evidence)); await context.close();
} catch (error) { console.error(JSON.stringify(evidence)); throw error; }
finally { clearTimeout(browserDeadline); await browser.close(); }
"""
    lane = tmp_path
    evidence_path = lane / f"h102-browser-{scenario}-{uuid4()}"
    with local_composer_tls(harness.app, tmp_path / "proxy") as tls:
        try:
            observed = subprocess.run(
                ["node", "--input-type=module", "-e", program],
                cwd=frontend,
                input=json.dumps(
                    {
                        "baseUrl": tls.base_url,
                        "token": harness.token,
                        "sessionId": str(harness.session_id),
                        "operationId": operation_id,
                        "otherId": other_id,
                        "scenario": scenario,
                    }
                ),
                capture_output=True,
                text=True,
                timeout=50,
                check=False,
            )
            evidence_path.with_suffix(".stdout.json").write_text(observed.stdout, encoding="utf-8")
            evidence_path.with_suffix(".stderr.log").write_text(observed.stderr, encoding="utf-8")
            evidence_path.with_suffix(".exit").write_text(f"{observed.returncode}\n", encoding="utf-8")
            evidence_path.with_suffix(".proxy.log").write_text(tls.proxy_log.read_text(), encoding="utf-8")
            assert observed.returncode == 0, observed.stderr
            evidence = json.loads(observed.stdout)
            assert provider_attempts == []
            if scenario == "late-sql-stop":
                assert admission_calls == [operation_id]
                assert admission_finished.is_set()
                assert harness.composer.calls <= 1
            elif scenario == "two-tabs-reload-stop":
                assert admission_calls == [operation_id, other_id]
                assert harness.composer.calls == 1
            elif scenario == "auth-refresh-stale-terminal":
                assert admission_calls == [operation_id]
                assert harness.composer.calls == 1
                assert evidence["afterLogout"]["outcome"]["name"] == "ComposerObservationDetached"
            else:
                assert admission_calls == [operation_id, other_id]
                assert harness.composer.calls == 2
        finally:
            admission_release.set()
            harness.composer.release.set()
            # Never leave a test-owned physical admission thread behind on failure.
            if admission_entered.is_set():
                assert admission_finished.wait(10), "Owned admission SQL thread did not finish"
