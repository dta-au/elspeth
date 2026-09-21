"""Probe authenticated public ingress, one-use tickets and terminal replay.

Run against an operator-created canary run; this never creates or cancels a run.
Only closed statuses and event sequences are printed, never URLs or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from elspeth.plugins.infrastructure.clients.json_utils import parse_json_strict

_TERMINAL = {"completed", "failed", "cancelled"}


def _event_sequence(message: str | bytes, run_id: str, after: int) -> tuple[int, bool]:
    if not isinstance(message, str):
        raise ValueError("expected_text_event")
    event, error = parse_json_strict(message)
    if error is not None:
        raise ValueError("invalid_event_json")
    if not isinstance(event, dict) or event.get("run_id") != run_id:
        raise ValueError("unexpected_run_event")
    sequence = event.get("event_sequence")
    if type(sequence) is not int or sequence <= after:
        raise ValueError("invalid_event_sequence")
    kind = event.get("event_type")
    if not isinstance(kind, str) or kind not in {"progress", "error", *_TERMINAL}:
        raise ValueError("invalid_event_type")
    return sequence, kind in _TERMINAL


async def probe(origin: str, run_id: str, bearer: str, timeout: float) -> dict[str, int | str]:
    """Authenticate over HTTP, receive an event, then replay through public WS."""
    ws_origin = origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

    def ws_url(ticket: str, after: int) -> str:
        return f"{ws_origin}/ws/runs/{run_id}?{urlencode({'ticket': ticket, 'after_sequence': after})}"

    async with httpx.AsyncClient(base_url=origin, headers={"Authorization": f"Bearer {bearer}"}, timeout=15) as client:

        async def ticket() -> str:
            response = await client.post(f"/api/runs/{run_id}/ws-ticket")
            response.raise_for_status()
            payload, error = parse_json_strict(response.text)
            if error is not None:
                raise ValueError("invalid_ticket_json")
            if not isinstance(payload, dict):
                raise ValueError("invalid_ticket_response")
            issued = payload.get("ticket")
            if not isinstance(issued, str) or not issued:
                raise ValueError("invalid_ticket_response")
            return issued

        async with asyncio.timeout(timeout):
            first_ticket = await ticket()
            async with connect(ws_url(first_ticket, 0), open_timeout=15) as socket:
                first_sequence, _ = _event_sequence(await socket.recv(), run_id, 0)

            # A failed upgrade (403) or policy close is an admissible rejection.
            # A timeout or successful data delivery is never evidence of one-use.
            try:
                async with connect(ws_url(first_ticket, 0), open_timeout=15) as socket:
                    try:
                        await asyncio.wait_for(socket.recv(), timeout=5)
                    except ConnectionClosed:
                        if socket.close_code not in (4001, 1008):
                            raise ValueError("unexpected_reused_ticket_close") from None
                    else:
                        raise ValueError("ticket_reuse_accepted")
            except InvalidStatus as exc:
                if exc.response.status_code != 403:
                    raise ValueError("unexpected_reused_ticket_status") from None

            # Replay from 0 also works if the run finished before this probe began.
            # Requiring the same first sequence proves replay, not just live tailing.
            replay_ticket = await ticket()
            async with connect(ws_url(replay_ticket, 0), open_timeout=15) as socket:
                replay_sequence, terminal = _event_sequence(await socket.recv(), run_id, 0)
                if replay_sequence != first_sequence:
                    raise ValueError("first_event_not_replayed")
                while not terminal:
                    replay_sequence, terminal = _event_sequence(await socket.recv(), run_id, replay_sequence)
                await asyncio.wait_for(socket.wait_closed(), timeout=5)
                if socket.close_code != 1000:
                    raise ValueError("terminal_close_not_clean")
    return {"status": "passed", "first_event_sequence": first_sequence, "terminal_event_sequence": replay_sequence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--bearer-file", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path
    ):
        parser.error("origin must be an HTTPS origin without path, credentials or query")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    try:
        if args.bearer_file.stat().st_mode & 0o077:
            raise ValueError("bearer_file_must_be_private")
        bearer = args.bearer_file.read_text().strip()
        if not bearer or any(character.isspace() for character in bearer):
            raise ValueError("invalid_bearer_file")
        result = asyncio.run(probe(args.origin, str(args.run_id), bearer, args.timeout))
    except Exception as exc:
        # HTTP and websocket exceptions may embed the credential-bearing URL.
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
