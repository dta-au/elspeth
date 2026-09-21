"""Exercise the acceptance probe with a real websocket and controlled ticket API."""

import functools
import json
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from scripts import probe_websocket_ingress as probe_module
from websockets.asyncio.server import ServerConnection, serve


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_allowed", [False, True])
async def test_probe_requires_one_use_ticket_and_replays_terminal(monkeypatch: pytest.MonkeyPatch, reuse_allowed: bool) -> None:
    run_id = str(uuid4())
    issued: list[str] = []
    consumed: set[str] = set()

    def issue(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/runs/{run_id}/ws-ticket"
        assert request.headers["authorization"] == "Bearer test-credential"
        issued.append(f"opaque-{len(issued)}")
        return httpx.Response(200, json={"ticket": issued[-1], "expires_at": "2099-01-01T00:00:00Z"})

    async def stream(socket: ServerConnection) -> None:
        assert socket.request is not None
        query = parse_qs(urlsplit(socket.request.path).query)
        ticket = query["ticket"][0]
        assert query["after_sequence"] == ["0"]
        assert ticket in issued
        if ticket in consumed and not reuse_allowed:
            await socket.close(4001)
            return
        consumed.add(ticket)
        await socket.send(json.dumps({"run_id": run_id, "event_sequence": 1, "event_type": "progress"}))
        await socket.send(json.dumps({"run_id": run_id, "event_sequence": 2, "event_type": "completed"}))
        await socket.close(1000)

    monkeypatch.setattr(probe_module.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(issue)))
    async with serve(stream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        if reuse_allowed:
            with pytest.raises(ValueError, match="ticket_reuse_accepted"):
                await probe_module.probe(f"http://127.0.0.1:{port}", run_id, "test-credential", 5)
        else:
            assert await probe_module.probe(f"http://127.0.0.1:{port}", run_id, "test-credential", 5) == {
                "status": "passed",
                "first_event_sequence": 1,
                "terminal_event_sequence": 2,
            }
            assert len(issued) == 2


@pytest.mark.parametrize(
    "event",
    [
        {"run_id": "wrong", "event_sequence": 1, "event_type": "progress"},
        {"run_id": "run", "event_sequence": True, "event_type": "completed"},
        {"run_id": "run", "event_sequence": 1, "event_type": "completed"},
        {"run_id": "run", "event_sequence": 2, "event_type": "unknown"},
    ],
)
def test_event_admission_rejects_wrong_identity_sequence_and_kind(event: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        probe_module._event_sequence(json.dumps(event), "run", 1)


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1"])
def test_cli_rejects_nonfinite_or_nonpositive_deadline(monkeypatch: pytest.MonkeyPatch, timeout: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--origin",
            "https://example.invalid",
            "--run-id",
            str(uuid4()),
            "--bearer-file",
            "/tmp/unused-probe-bearer",
            f"--timeout={timeout}",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        probe_module.main()
    assert caught.value.code == 2
