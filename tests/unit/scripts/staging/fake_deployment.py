"""An in-memory stand-in for the ELSPETH web API routes the staging tools call.

It is an :class:`~scripts.staging.opslib.HttpTransport`: tools under test
talk to it through a real :class:`~scripts.staging.opslib.WebClient`, so the
client's authentication, paths and error handling are exercised too. Every
request is recorded in ``requests`` for assertions about what a tool did.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from scripts.staging.opslib import HttpResponse

BASE_URL = "https://deployment.invalid"

type JsonObject = dict[str, Any]


@dataclass
class FakeTurn:
    """What one fake Composer turn did: the new state and the rows the server would store for it."""

    state: JsonObject
    audit: list[JsonObject] = field(default_factory=list)
    assistant_tool_calls: list[JsonObject] = field(default_factory=list)
    reviews: dict[str, JsonObject] = field(default_factory=dict)
    status: str = "completed"
    error_code: str | None = None
    created_at: tuple[str, str] = ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:05+00:00")
    # The provider audit row is stored only after this many later reads of the messages view,
    # as on a server whose evidence rows land after the operation reports completed.
    audit_after_reads: int = 0
    # Session files the turn created (filename -> content) or deleted (by filename), as create_blob and delete_blob do.
    created_files: dict[str, bytes] = field(default_factory=dict)
    deleted_files: tuple[str, ...] = ()
    # The published reply on the completed operation's result message, and its rejection.
    reply_segments: list[JsonObject] = field(default_factory=lambda: [{"kind": "text", "content": "Done."}])
    rejection: JsonObject | None = None
    # role="tool" rows the turn stores (each a list of tool_calls envelopes).
    tool_rows: list[list[JsonObject]] = field(default_factory=list)


type Provider = Callable[[JsonObject, str], tuple[JsonObject, list[JsonObject]] | FakeTurn]


def fake_jwt(username: str, exp: float) -> str:
    def part(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'username': username, 'exp': exp, 'jti': uuid4().hex})}.sig"


def success_call() -> JsonObject:
    return {"_kind": "llm_call_audit", "call": {"status": "success", "latency_ms": 1200}}


@dataclass
class Blob:
    id: str
    filename: str
    data: bytes

    def meta(self, session_id: str) -> JsonObject:
        return {
            "id": self.id,
            "session_id": session_id,
            "filename": self.filename,
            "mime_type": "text/plain",
            "size_bytes": len(self.data),
            "content_hash": hashlib.sha256(self.data).hexdigest(),
            "status": "ready",
        }


@dataclass
class Session:
    owner: str
    state: JsonObject
    blobs: dict[str, Blob] = field(default_factory=dict)
    messages: list[JsonObject] = field(default_factory=list)
    reviews: dict[str, JsonObject] = field(default_factory=dict)
    operations: dict[str, JsonObject] = field(default_factory=dict)
    trust_mode: str = "auto_commit"
    late_rows: list[tuple[int, JsonObject]] = field(default_factory=list)


def _without_checkpoint_passes(row: JsonObject) -> JsonObject:
    """The row as an older server returns it: its audit view carries no advisor checkpoint pass records."""
    calls = row["tool_calls"] if "tool_calls" in row else None
    if row["role"] != "audit" or type(calls) is not list:
        return row
    return {**row, "tool_calls": [call for call in calls if call["_kind"] != "advisor_checkpoint_pass_audit"]}


@dataclass
class FakeDeployment:
    passwords: dict[str, str]
    sessions: dict[str, Session] = field(default_factory=dict)
    provider: Provider | None = None
    token_lifetime: float = 3600.0
    clock: Callable[[], float] = lambda: 1_000_000.0
    on_request: Callable[[str, str], None] | None = None
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)
    # A server with the END review view returns END pass rows under include_llm_audit and
    # sets X-Elspeth-End-Review-Rows-Omitted; an older server returns neither.
    end_review_view: bool = True
    end_review_rows_omitted: int = 0

    # -- transport -------------------------------------------------------

    def __call__(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == BASE_URL
        query = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None
        self.requests.append((method, parsed.path, payload))
        if parsed.path == "/api/auth/login":
            if self.passwords.get(payload["username"]) != payload["password"]:
                return self._json(401, {"detail": "bad credentials"})
            return self._json(200, {"access_token": self._issue(payload["username"])})
        user = self.tokens.get(headers.get("Authorization", "").removeprefix("Bearer "))
        if user is None:
            return self._json(401, {"detail": "unauthenticated"})
        response = self._route(method, parsed.path, query, payload, user, headers)
        if self.on_request is not None:
            self.on_request(method, parsed.path)
        return response

    def _issue(self, username: str) -> str:
        token = fake_jwt(username, self.clock() + self.token_lifetime)
        self.tokens[token] = username
        return token

    @staticmethod
    def _json(status: int, value: object) -> HttpResponse:
        return HttpResponse(status=status, body=json.dumps(value).encode())

    def _page(self, items: list[Any], query: Mapping[str, str]) -> HttpResponse:
        offset = int(query["offset"])
        return self._json(200, items[offset : offset + int(query["limit"])])

    # -- routes ----------------------------------------------------------

    def _route(self, method: str, path: str, query: dict[str, str], payload: Any, user: str, headers: Mapping[str, str]) -> HttpResponse:
        if path == "/api/auth/me":
            return self._json(200, {"username": user, "user_id": "id-" + user})
        if path == "/api/auth/logout":
            self.tokens.pop(headers["Authorization"].removeprefix("Bearer "))
            return HttpResponse(status=204, body=b"")
        if path == "/api/auth/token":
            return self._json(200, {"access_token": self._issue(user)})
        match = re.fullmatch(r"/api/sessions/([0-9a-f-]{36})(/.*)", path)
        if match is None:
            return self._json(404, {"detail": "no route"})
        session = self.sessions.get(match.group(1))
        if session is None or session.owner != user:
            return self._json(404, {"detail": "session not found"})
        rest = match.group(2)
        key = (method, rest)
        if key == ("GET", "/state"):
            return self._json(200, session.state)
        if key == ("GET", "/blobs"):
            return self._page([blob.meta(match.group(1)) for blob in session.blobs.values()], query)
        blob_match = re.fullmatch(r"/blobs/([^/]+)/content", rest)
        if method == "GET" and blob_match is not None:
            return HttpResponse(status=200, body=session.blobs[blob_match.group(1)].data)
        if key == ("GET", "/composer/preferences"):
            return self._json(200, {"session_id": match.group(1), "trust_mode": session.trust_mode, "density_default": "standard"})
        if key == ("GET", "/messages"):
            still_late: list[tuple[int, JsonObject]] = []
            for reads_left, row in session.late_rows:
                if reads_left <= 0:
                    session.messages.append(row)
                else:
                    still_late.append((reads_left - 1, row))
            session.late_rows = still_late
            audit = query.get("include_llm_audit") == "true"
            rows = [
                row if self.end_review_view else _without_checkpoint_passes(row)
                for row in session.messages
                if audit or row["role"] != "audit"
            ]
            page = self._page(rows, query)
            if audit and self.end_review_view:
                return HttpResponse(
                    status=page.status, body=page.body, headers={"x-elspeth-end-review-rows-omitted": str(self.end_review_rows_omitted)}
                )
            return page
        if key == ("GET", "/interpretations"):
            assert query["status"] == "pending"
            return self._json(200, {"events": [review["event"] for review in session.reviews.values() if review["pending"]]})
        review_match = re.fullmatch(r"/interpretations/([^/]+)/(prompt|resolve)", rest)
        if review_match is not None:
            review = session.reviews[review_match.group(1)]
            if review_match.group(2) == "prompt":
                return self._json(200, {**review["snapshot"], "is_live_review": review["pending"]})
            assert payload == {"choice": "accepted_as_drafted", "reviewed_anchor": review["snapshot"]["anchor"]}
            review["pending"] = False
            self._resolve_prompt_review(session, review)
            return self._json(200, {"event": review["event"]})
        if key == ("POST", "/validate"):
            return self._json(200, {"is_valid": True, "readiness": "ready", "errors": []})
        if key == ("POST", "/messages"):
            return self._admit(session, payload)
        operation_match = re.fullmatch(r"/operations/([0-9a-f-]{36})", rest)
        if method == "GET" and operation_match is not None:
            return self._json(200, session.operations[operation_match.group(1)])
        return self._json(404, {"detail": f"no route {method} {rest}"})

    @staticmethod
    def _resolve_prompt_review(session: Session, review: JsonObject) -> None:
        """What the server's resolve writes (``sessions/pending_interpretation.py``, prompt-template branch).

        On the reviewed node only: the matching pending requirement becomes
        resolved with the event id, the accepted value and the resolved
        template hash, and ``options.approved_prompt_artifact_hash`` is set to
        the effective artifact hash. Nothing else in the graph changes.
        """
        snapshot = review["snapshot"]
        for node in session.state["nodes"]:
            if node["id"] != snapshot["node_id"]:
                continue
            options = node["options"]
            artifact = hashlib.sha256(json.dumps(snapshot["authored_surface"], sort_keys=True).encode()).hexdigest()
            if "interpretation_requirements" in options:
                for requirement in options["interpretation_requirements"]:
                    if requirement["kind"] == review["event"]["kind"] and requirement["status"] == "pending":
                        requirement.update(
                            status="resolved",
                            event_id=review["event"]["id"],
                            accepted_value="accepted",
                            resolved_prompt_template_hash=snapshot["anchor"],
                        )
            options["approved_prompt_artifact_hash"] = artifact

    def _admit(self, session: Session, payload: JsonObject) -> HttpResponse:
        operation_id = payload["operation_id"]
        if operation_id not in session.operations:
            assert self.provider is not None
            produced = self.provider(copy.deepcopy(session.state), payload["content"])
            turn = produced if isinstance(produced, FakeTurn) else FakeTurn(state=produced[0], audit=produced[1])
            session.state = turn.state
            session.reviews.update(turn.reviews)
            for filename, data in turn.created_files.items():
                blob_id = str(uuid4())
                session.blobs[blob_id] = Blob(id=blob_id, filename=filename, data=data)
            for blob_id in [blob.id for blob in session.blobs.values() if blob.filename in turn.deleted_files]:
                del session.blobs[blob_id]
            first, last = turn.created_at
            session.messages.append(
                {"role": "user", "operation_id": operation_id, "tool_calls": None, "content": payload["content"], "created_at": first}
            )
            if turn.assistant_tool_calls:
                session.messages.append(
                    {
                        "role": "assistant",
                        "operation_id": operation_id,
                        "tool_calls": turn.assistant_tool_calls,
                        "content": "",
                        "created_at": first,
                    }
                )
            for envelopes in turn.tool_rows:
                session.messages.append(
                    {"role": "tool", "operation_id": operation_id, "tool_calls": envelopes, "content": "", "created_at": first}
                )
            audit_row = {"role": "audit", "operation_id": operation_id, "tool_calls": turn.audit, "content": "", "created_at": last}
            if turn.audit_after_reads:
                session.late_rows.append((turn.audit_after_reads, audit_row))
            else:
                session.messages.append(audit_row)
            completed = turn.status == "completed"
            session.operations[operation_id] = {
                "operation_id": operation_id,
                "status": turn.status,
                "poll_after_ms": 0,
                "result": {"message": {"segments": turn.reply_segments, "rejection": turn.rejection}} if completed else None,
                "error": None if completed else {"code": turn.error_code},
            }
        return self._json(202, {"operation_id": operation_id, "status": "queued", "poll_after_ms": 1000})

    # -- assertions ------------------------------------------------------

    def calls(self, method: str, suffix: str) -> list[object]:
        return [payload for verb, path, payload in self.requests if verb == method and path.endswith(suffix)]
