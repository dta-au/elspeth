"""Scripted HttpClient for driver tests: routes (method, path-prefix) → responder; records every call in order."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from drive_battery import HttpResponse  # evals/composer-battery is appended to sys.path by conftest.py

from tests.unit.evals.composer_battery import threadgen as tg


@dataclass
class Call:
    method: str
    path: str
    json: Any
    params: dict[str, Any] | None
    timeout: float | None


@dataclass
class FakeClient:
    """``responders`` maps ``"METHOD path-prefix"`` → callable(call) → HttpResponse (or raises)."""

    responders: dict[str, Callable[[Call], Any]]
    calls: list[Call] = field(default_factory=list)
    token: str | None = None

    def set_token(self, token: str) -> None:
        self.token = token

    def request(
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        call = Call(method, path, json, dict(params) if params else None, timeout)
        self.calls.append(call)
        for key, fn in self.responders.items():
            m, prefix = key.split(" ", 1)
            if m == method and path.startswith(prefix):
                return fn(call)
        raise AssertionError(f"unscripted request: {method} {path}")

    def steps(self) -> list[str]:
        return [f"{c.method} {c.path.split('?')[0]}" for c in self.calls]


def ok(body: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, body, json.dumps(body))


def happy_responders(
    messages: list[dict[str, Any]], *, state: dict[str, Any] | None, session_id: str = "s1"
) -> dict[str, Callable[[Call], Any]]:
    """A session that composes cleanly and returns ``messages`` on the paginated read."""

    def paged(call: Call):
        off = int((call.params or {}).get("offset", 0))
        lim = int((call.params or {}).get("limit", 500))
        return ok(messages[off : off + lim])

    def state_resp(call: Call):
        if state is None:
            return ok(None)  # the real route answers "no composition state yet" with 200 + a null body, never a 404
        return ok(
            {"id": "state-2", "session_id": session_id, "version": 2, "is_valid": True, "created_at": "2026-08-17T00:00:00Z", **state}
        )

    return {
        "POST /api/auth/login": lambda c: ok({"access_token": "tok", "token_type": "bearer"}),
        "GET /api/system/status": lambda c: ok(
            {
                "composer_available": True,
                "composer_model": tg.COMPOSER,
                "composer_provider": "openrouter",
                "composer_reason": None,
                "composer_missing_keys": [],
                "composer_timeout_seconds": 600.0,
                "frontend_build": "index-abc.js",
                "tutorial_ready": True,
                "tutorial_reason": None,
                "plugin_policy_readiness": {},
            }
        ),
        "POST /api/sessions/": lambda c: _session_child(c, messages, session_id),
        "POST /api/sessions": lambda c: ok(
            {"id": session_id, "user_id": "u", "title": "Session — 17 Aug 2026", "created_at": "t", "updated_at": "t", "archived": False},
            201,
        ),
        "PATCH /api/sessions/": lambda c: ok(
            {
                "id": session_id,
                "user_id": "u",
                "title": (c.json or {}).get("title"),
                "created_at": "t",
                "updated_at": "t",
                "archived": False,
            }
        ),
        "GET /api/sessions/" + session_id + "/composer/preferences": lambda c: ok(
            {
                "session_id": session_id,
                "trust_mode": "auto_commit",
                "density_default": "high",
                "interpretation_review_disabled": False,
                "updated_at": "t",
            }
        ),
        "GET /api/sessions/" + session_id + "/interpretations": lambda c: ok({"events": []}),
        "GET /api/sessions/" + session_id + "/state": state_resp,
        "GET /api/sessions/" + session_id + "/messages": paged,
        "GET /api/sessions/" + session_id + "/proposals": lambda c: ok([]),
        "GET /api/sessions/" + session_id + "/proposal-events": lambda c: ok([]),
        "GET /api/sessions/" + session_id + "/composer-progress": lambda c: ok(
            {
                "phase": "failed",
                "headline": "x",
                "evidence": [],
                "likely_next": None,
                "reason": "convergence_wall_clock_timeout",
                "session_id": session_id,
                "request_id": None,
                "updated_at": "t",
                "inflight_requests": 0,
            }
        ),
        "GET /api/sessions": lambda c: ok([]),
        "DELETE /api/sessions/": lambda c: ok(None, 204),
    }


def _session_child(call: Call, messages: list[dict[str, Any]], session_id: str):
    if call.path.endswith("/messages"):
        return ok({"message": {}, "state": None, "proposals": []})  # MessageWithStateResponse shape
    if call.path.endswith("/validate"):
        return ok({"is_valid": True, "checks": [], "errors": [], "warnings": [], "readiness": "ready"})
    if "/interpretations/" in call.path and call.path.endswith("/resolve"):
        return ok({"event": {"id": call.path.split("/")[-2], "status": "accepted_as_drafted"}, "new_state": None})
    raise AssertionError(f"unscripted POST {call.path}")
