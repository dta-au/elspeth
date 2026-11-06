"""Finite physical SDK responses for common Composer integration tests."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select

from elspeth.contracts.composer_content import CompleteValueManifest
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.recovery_controls import recovery_context_message
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from tests.helpers.advisor_review import parse_advisor_publication_review


@dataclass
class Function:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: Function


@dataclass
class Message:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Choice:
    message: Message


@dataclass
class Response:
    choices: list[Choice]
    usage: Mapping[str, object]
    model: str
    id: str
    provider: str | None = None


@dataclass(frozen=True)
class Step:
    role: Literal["primary", "early", "end", "hint"]
    content: str | None = None
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None
    failure: BaseException | None = None
    publication: Literal["ADMIT", "WITHHOLD"] = "ADMIT"
    verdict: Literal["CLEAN", "FLAGGED"] = "CLEAN"
    malformed: bool = False
    before_return: Callable[[], Awaitable[None]] | None = None


class CommonSDK:
    def __init__(self, *, request: str, candidate: str, steps: tuple[Step, ...], saved: int = 0):
        self.request = request
        self.candidate = candidate
        self.steps = steps
        self.saved = saved
        self.requests: list[dict[str, Any]] = []
        self.phases: list[str] = []
        self.phase: str | None = None
        self.composer = None

    def bind(self, composer, monkeypatch):
        self.composer = composer
        actual = composer._advisor_checkpoint._run_advisor_checkpoint

        async def observe(**kwargs):
            assert self.phase is None
            self.phase = kwargs["phase"]
            self.phases.append(self.phase)
            try:
                return await actual(**kwargs)
            finally:
                self.phase = None

        monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe)

    async def __call__(self, **kwargs):
        ordinal = len(self.requests)
        assert ordinal < len(self.steps), "Unscripted physical SDK transition"
        step = self.steps[ordinal]
        self.requests.append(deepcopy(kwargs))
        assert self.composer is not None
        if step.role == "primary":
            assert kwargs["model"] == self.composer._settings.composer_model
            assert self.phase is None
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            assert any(row["role"] == "user" and row["content"] == self.request for row in kwargs["messages"])
        else:
            assert kwargs["model"] == self.composer._settings.composer_advisor_model
            assert kwargs.get("tools") is None
            if step.role in {"early", "end"}:
                assert self.phase == step.role and "response_format" in kwargs
            else:
                assert self.phase is None
        if step.before_return is not None:
            await step.before_return()
        if step.failure is not None:
            raise step.failure
        content = step.content
        if step.role in {"early", "end"}:
            payload = {
                "verdict": step.verdict,
                "category": "other",
                "steps": [],
                "findings": "Review uses the actual request and saved effects.",
                "note": None,
            }
            if step.role == "end":
                review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
                assert review["current_request"] == self.request
                assert review["candidate_reply"] == self.candidate
                assert review["saved_effects"]["saved_revision_count"] == self.saved
                assert review["saved_effects"]["net_changed"] is bool(self.saved)
                payload["publication_verdict"] = step.publication
            content = json.dumps(payload)
        calls = []
        if step.tool_name is not None:
            assert step.arguments is not None
            calls.append(ToolCall(f"common-tool-{ordinal + 1}", Function(step.tool_name, json.dumps(step.arguments))))
        return Response(
            choices=[] if step.malformed else [Choice(Message(content, calls))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model="provider/common-v1",
            id=f"common-sdk-{ordinal + 1}",
            provider="DeepInfra",
        )

    def assert_done(self):
        assert len(self.requests) == len(self.steps)
        assert self.phases == [step.role for step in self.steps if step.role in {"early", "end"}]


def assert_initial_wire(request, *, text: str, state: CompositionState, session_id: str):
    messages = request["messages"]
    assert any(row["role"] == "user" and row["content"] == text for row in messages)
    state_rows = [row["content"] for row in messages if row["role"] == "user" and row["content"].startswith(STATE_CONTEXT_PREFIX + "\n")]
    (rendered,) = state_rows
    view = json.loads(rendered.split("\n", 1)[1])["current_state"]
    assert set(view) == {"schema_version", "data", "content_manifest"}
    assert view["schema_version"] == "composer.bounded-view.v1"
    manifest = CompleteValueManifest.from_payload(view["content_manifest"])
    assert manifest.scope.session_id == UUID(session_id)
    assert manifest.scope.composition_content_hash == composition_content_hash(state)
    if not state.sources and not state.nodes and not state.outputs:
        assert manifest.values == ()
        assert view["data"]["sources"] == {} and view["data"]["nodes"] == []
        assert view["data"]["edges"] == [] and view["data"]["outputs"] == []
    names = {entry["function"]["name"] for entry in request["tools"]}
    assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
    assert all(not any(key.startswith("_elspeth_") for key in row) for row in messages)


def assert_route_context(request, call, *, session_id: str, user_id: str):
    recovery = call.kwargs["recovery_context"]
    retained = call.kwargs["retained_operation_context"]
    assert type(recovery) is GraphRecoveryContext
    assert type(retained) is RetainedOperationContext
    retained.validate_for(session_id=session_id, actor_user_id=user_id)
    assert request["messages"][-2]["content"] == recovery_context_message(recovery)
    assert request["messages"][-1]["content"] == retained.provider_message()


def assert_actual_audits(engine, requests, statuses, *, session_id: str):
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == session_id)
                .order_by(chat_messages_table.c.sequence_no)
            )
            .mappings()
            .all()
        )
        attempts = (
            conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == session_id))
            .mappings()
            .all()
        )
    calls = [
        row["tool_calls"][0]["call"]
        for row in rows
        if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0]["_kind"] == "llm_call_audit"
    ]
    assert len(calls) == len(requests) == len(statuses)
    assert len({call["call_id"] for call in calls}) == len(calls)
    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in attempts}
    assert set(attempts_by_id) == {call["call_id"] for call in calls}
    private_requests = {}
    evidence_ids = set()
    for row in rows:
        envelopes = row["tool_calls"] or ()
        if not envelopes or envelopes[0].get("_kind") != "composer_diagnostic_evidence":
            continue
        assert len(envelopes) == 1 and row["role"] == "audit" and row["writer_principal"] == "compose_loop"
        envelope = envelopes[0]
        if envelope["evidence"]["status"] != "provider_request":
            continue
        call_id = envelope["evidence"]["tool_call_id"]
        attempt = attempts_by_id[call_id]
        evidence = ComposerDiagnosticEvidence.from_envelope(
            envelope,
            expected_session_id=session_id,
            expected_operation_id=attempt["operation_id"],
            expected_tool_call_id=call_id,
        )
        assert evidence.operation_epoch == attempt["operation_epoch"]
        assert evidence.evidence_id not in evidence_ids
        evidence_ids.add(evidence.evidence_id)
        assert call_id not in private_requests and evidence.arguments.content is not None
        private_requests[call_id] = json.loads(evidence.arguments.content)
    assert set(private_requests) == set(attempts_by_id)
    for call, request, status in zip(calls, requests, statuses, strict=True):
        assert call["status"] == status
        assert call["model_requested"] == request["model"]
        assert call["messages_hash"] == stable_hash(request["messages"])
        tools = request.get("tools")
        assert call["tools_spec_hash"] == (None if tools is None else stable_hash(tools))
        private = private_requests[call["call_id"]]
        assert private["messages"] == request["messages"] and private["tools"] == tools
        assert call["planner_call_ordinal"] is None
        if status in {"timeout", "api_error", "cancelled"}:
            assert call["model_returned"] is None
            assert call["prompt_tokens"] is None and call["completion_tokens"] is None
            assert call["provider_cost"] is None and call["provider_cost_source"] == "not_available"
    return calls
