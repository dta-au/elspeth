"""Drive the live acceptance instance over external HTTPS for a graph battery.

Built for acceptance battery round 2 (2026-08-04) and kept because every
round re-derives the same choreography. Ordinary operational tooling: no
secrets live here, and every verdict is read from the run API / Landscape
audit trail rather than from output-file mtimes.

Per-graph choreography (see ADR-037 and the round-2 report for why each gate
exists):

    POST /api/sessions                      -> session id
    POST /api/sessions/{id}/messages        -> freeform compose (minutes; see
                                               --timeout, and note a client
                                               timeout does NOT abort the
                                               server-side compose)
    resolve-reviews                         -> gate 1: pending interpretation
                                               reviews must be resolved
    POST /api/sessions/{id}/validate        -> real execution contracts
    POST /api/sessions/{id}/execute         -> gate 2: >=2 LLM nodes answers
                                               428 with a fanout ack token
    GET  /api/runs/{run_id}                 -> poll to a terminal status
    GET  /api/runs/{run_id}/diagnostics     -> per-token node states, error
                                               reasons, sink attribution

State (bearer token, per-graph response archive) is written under
``--state-dir``, which defaults to a path OUTSIDE the repository so a
captured token is never committed.

Usage::

    python scripts/acceptance_battery.py ensure-account
    python scripts/acceptance_battery.py api POST /api/sessions body.json g01/session.json
    python scripts/acceptance_battery.py resolve-reviews <session-id> g01
    python scripts/acceptance_battery.py run-graph <session-id> g01
    python scripts/acceptance_battery.py guided-rider 10
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE = "https://elspeth.aws.foundryside.dev"
DEFAULT_STATE_DIR = Path(os.environ.get("ELSPETH_BATTERY_STATE_DIR", "/tmp/elspeth-battery"))
# Terminal run statuses. ``completed_with_failures`` is healthy: rows were
# quarantined by an on_error route, which is frequently the property under test.
RUNNING_STATUSES = frozenset({"running", "pending", "created", "in_progress", "queued"})


class Battery:
    """One authenticated driver bound to a base URL and a state directory."""

    def __init__(self, base: str, state_dir: Path) -> None:
        self.base = base.rstrip("/")
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.creds_path = self.state_dir / "credentials.json"

    # ── plumbing ────────────────────────────────────────────────────────

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = "elspeth-acceptance-battery"
        if self.creds_path.exists():
            token = json.loads(self.creds_path.read_text())["access_token"]
            session.headers["Authorization"] = f"Bearer {token}"
        return session

    def api(
        self,
        method: str,
        path: str,
        *,
        save: str | None = None,
        timeout: int = 240,
        **kwargs: Any,
    ) -> tuple[int, Any]:
        """Call the instance, archive the response, return (status, body)."""
        response = self._session().request(method, f"{self.base}{path}", timeout=timeout, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"_raw": response.text[:2000]}
        if save:
            out = self.state_dir / save
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"status": response.status_code, "body": body}, indent=2, default=str))
        return response.status_code, body

    # ── commands ────────────────────────────────────────────────────────

    def ensure_account(self) -> int:
        """Reuse a valid token, else self-register (registration_mode=open)."""
        if self.creds_path.exists():
            status, body = self.api("GET", "/api/auth/me", timeout=30)
            if status == 200:
                print("token valid:", body.get("username", "?"))
                return 0
        username = "battery-" + secrets.token_hex(4)
        password = secrets.token_urlsafe(24)
        session = requests.Session()
        registered = session.post(
            f"{self.base}/api/auth/register",
            json={"username": username, "password": password, "display_name": "Acceptance battery"},
            timeout=30,
        )
        payload = registered.json()
        if "access_token" not in payload:
            payload = session.post(
                f"{self.base}/api/auth/login",
                json={"username": username, "password": password},
                timeout=30,
            ).json()
        self.creds_path.write_text(json.dumps({"username": username, "password": password, **payload}))
        self.creds_path.chmod(0o600)
        print("account ready:", username)
        return 0

    def resolve_reviews(self, session_id: str, graph: str) -> int:
        """Gate 1: resolve every pending interpretation review.

        A compose CORRECTION can stage further reviews, so callers should
        re-run this after any repair turn until it reports zero.
        """
        _, body = self.api("GET", f"/api/sessions/{session_id}/interpretations?status=pending")
        events = body.get("events", []) if isinstance(body, dict) else []
        print("pending reviews:", len(events))
        for event in events:
            event_id = event.get("id") or event.get("event_id")
            status, _ = self.api(
                "POST",
                f"/api/sessions/{session_id}/interpretations/{event_id}/resolve",
                save=f"{graph}/resolve-{event_id}.json",
                json={"choice": "accepted_as_drafted"},
            )
            print("resolved", event_id, "->", status)
        return 0

    def run_graph(self, session_id: str, graph: str, budget: int) -> int:
        """Validate, execute (acking LLM fanout if demanded), poll, diagnose."""
        status, validation = self.api("POST", f"/api/sessions/{session_id}/validate", save=f"{graph}/validate.json")
        valid = bool(validation.get("is_valid") or validation.get("valid"))
        print("validate:", status, "is_valid=", valid)
        if status != 200 or not valid:
            for check in validation.get("checks", []):
                if not check.get("passed") and "Skipped" not in str(check.get("detail")):
                    print("  FAILED:", check.get("name"), "|", str(check.get("detail"))[:300])
            return 1

        status, launched = self.api("POST", f"/api/sessions/{session_id}/execute", save=f"{graph}/execute.json")
        if status == 428:
            # Gate 2: the LLM-fanout guard wants explicit acknowledgement that
            # high-cardinality provider calls are intended.
            detail = launched.get("detail", launched) if isinstance(launched, dict) else {}
            token = (detail.get("fanout_guard") or {}).get("ack_token")
            if token:
                status, launched = self.api(
                    "POST",
                    f"/api/sessions/{session_id}/execute",
                    save=f"{graph}/execute-acked.json",
                    json={"fanout_ack_token": token},
                )
                print("fanout acknowledged")
        print("execute:", status)
        if status != 202:
            print("  ", str(launched)[:400])
            return 1

        run_id = launched["run_id"]
        deadline = time.time() + budget
        run: dict[str, Any] = {}
        while time.time() < deadline:
            _, run = self.api("GET", f"/api/runs/{run_id}", save=f"{graph}/run.json")
            if (run.get("status") or run.get("state")) not in RUNNING_STATUSES:
                break
            time.sleep(12)
        print("RUN", run_id, "->", run.get("status") or run.get("state"))
        self.api("GET", f"/api/runs/{run_id}/diagnostics", save=f"{graph}/diagnostics.json")
        self.api("GET", f"/api/runs/{run_id}/outputs", save=f"{graph}/outputs.json")
        return 0

    def guided_rider(self, attempts: int, intent: str) -> int:
        """Sample plain-guided cold starts; report completions over attempts.

        Each attempt is a fresh session and operation id. A client timeout is
        reconciled against the server's authoritative outcome rather than
        counted as a failure.
        """
        results: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            _, session = self.api("POST", "/api/sessions", json={"title": f"battery guided rider {attempt}"})
            session_id = session["id"]
            operation_id = str(uuid.uuid4())
            outcome: dict[str, Any] = {"attempt": attempt, "session": session_id, "operation": operation_id}
            try:
                status, body = self.api(
                    "POST",
                    f"/api/sessions/{session_id}/guided/start",
                    save=f"rider/attempt-{attempt:02d}.json",
                    timeout=280,
                    json={"operation_id": operation_id, "profile": "live", "intent": intent},
                )
                outcome["http"] = status
                if status == 200:
                    outcome["result"] = "completed"
                    outcome["has_state"] = body.get("composition_state") is not None
                else:
                    detail = body.get("detail", body) if isinstance(body, dict) else {}
                    outcome["result"] = "failed"
                    outcome["failure_code"] = detail.get("failure_code") if isinstance(detail, dict) else None
            except requests.RequestException:
                outcome["result"] = "client-timeout"
                for _ in range(20):
                    time.sleep(15)
                    status, reconciled = self.api(
                        "POST",
                        f"/api/sessions/{session_id}/guided/start/{operation_id}/reconcile",
                        timeout=60,
                    )
                    if status == 200 and reconciled.get("status") in {"completed", "failed"}:
                        outcome["result"] = f"reconciled-{reconciled['status']}"
                        outcome["failure_code"] = reconciled.get("failure_code")
                        break
            results.append(outcome)
            print(json.dumps(outcome, default=str))
            with (self.state_dir / "rider-results.jsonl").open("a") as handle:
                handle.write(json.dumps(outcome, default=str) + "\n")
            time.sleep(20)
        completed = sum(1 for result in results if "completed" in str(result.get("result")))
        print(f"GUIDED RIDER: {completed}/{attempts} completions")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE, help="instance base URL")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="token + response archive (kept out of the repo)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("ensure-account")

    call = commands.add_parser("api")
    call.add_argument("method")
    call.add_argument("path")
    call.add_argument("body", nargs="?", default=None, help="path to a JSON request body, or '-' for none")
    call.add_argument("save", nargs="?", default=None, help="archive name under the state dir")
    call.add_argument("--timeout", type=int, default=240)

    reviews = commands.add_parser("resolve-reviews")
    reviews.add_argument("session_id")
    reviews.add_argument("graph", nargs="?", default="misc")

    run = commands.add_parser("run-graph")
    run.add_argument("session_id")
    run.add_argument("graph")
    run.add_argument("--budget", type=int, default=900, help="seconds to poll before giving up")

    rider = commands.add_parser("guided-rider")
    rider.add_argument("attempts", type=int, nargs="?", default=10)
    rider.add_argument("--intent", required=True, help="the canonical intent to replay each attempt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    battery = Battery(args.base, args.state_dir)
    if args.command == "ensure-account":
        return battery.ensure_account()
    if args.command == "api":
        payload = None
        if args.body and args.body != "-":
            payload = json.loads(Path(args.body).read_text())
        status, body = battery.api(args.method, args.path, save=args.save, timeout=args.timeout, json=payload)
        print(status)
        print(json.dumps(body, indent=2, default=str)[:4000])
        return 0
    if args.command == "resolve-reviews":
        return battery.resolve_reviews(args.session_id, args.graph)
    if args.command == "run-graph":
        return battery.run_graph(args.session_id, args.graph, args.budget)
    if args.command == "guided-rider":
        return battery.guided_rider(args.attempts, args.intent)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
