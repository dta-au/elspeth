"""Drive a Composer request through the production app, worker and Continuation driver.

Only the external SDK (``litellm.acompletion``) and the Azure prompt-guard HTTP
boundary are scripted. Everything between admission and the sealed terminal
is production code. This mirrors the U-40 completion-owner harness
(``tests/unit/web/sessions/test_continuation_completion_owners.py``) so that
tests ported off the deleted V1 lifecycle share one shape.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import litellm
import pytest
from fastapi import FastAPI
from litellm import ModelResponse
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.hashing import canonical_json
from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.composer.continuation_advisor import ContinuationAdvisorInvocation
from elspeth.web.composer.continuation_progress import decode_continuation_frontier
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import chat_messages_table, composer_continuation_effects_table
from elspeth.web.sessions.schemas import ComposerOperationSnapshot
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user

PRIMARY_MODEL = "test/model"
ADVISOR_MODEL = "test/advisor"
ACTOR = "continuation-user"


def install_clean_guard(monkeypatch: pytest.MonkeyPatch) -> PromptGuardSettings:
    """A prompt guard whose HTTP boundary reports no attack for every document."""

    def serve(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    monkeypatch.setattr(
        prompt_guard_clients,
        "build_prompt_guard_client",
        lambda configuration: AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(serve)),
    )
    return PromptGuardSettings(endpoint="https://guard.example.test", subscription_key=SecretStr("fixture-guard-credential"))


@dataclass(frozen=True, slots=True)
class ContinuationApp:
    app: FastAPI
    token: str
    session_id: UUID

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers=self.headers)


def continuation_settings(tmp_path: Path, guard: PromptGuardSettings, **overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "landscape_url": f"sqlite:///{tmp_path}/runs/audit.db",
        "payload_store_path": tmp_path / "payloads",
        "composer_model": PRIMARY_MODEL,
        "composer_advisor_model": ADVISOR_MODEL,
        "composer_prompt_guard": guard,
        "composer_advisor_prompt_guard": guard,
        "composer_prompt_guard_mode": "required",
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_boot_probe_enabled": False,
        "composer_rate_limit_per_minute": 100,
        "shareable_link_signing_key": b"\x00" * 32,
        "plugin_allowlist": ("transform:passthrough",),
        "composer_async_scan_interval_seconds": 0.05,
    }
    values.update(overrides)
    return WebSettings(**values)


@asynccontextmanager
async def continuation_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings_overrides: Any) -> AsyncIterator[ContinuationApp]:
    """The production app, lifespan and worker; only external I/O is scripted."""
    from tests.helpers.fenced_session import seed_token_policies

    settings = continuation_settings(tmp_path, install_clean_guard(monkeypatch), **settings_overrides)
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        provider = app.state.auth_provider
        assert isinstance(provider, LocalAuthProvider)
        provider.create_user(ACTOR, "test continuation password", "Continuation User")
        with app.state.session_engine.begin() as conn:
            ensure_test_identity(conn, identity_id=ACTOR)
            grant_test_pipeline_user(conn, identity_id=ACTOR)
            seed_token_policies(conn, identity_id=ACTOR)
        token = provider._issuer.mint(identity_id=ACTOR, username=ACTOR)
        session = await app.state.session_service.create_session(user_id=ACTOR, title="Continuation evidence", auth_provider_type="local")
        yield ContinuationApp(app=app, token=token, session_id=session.id)


def sdk_reply(
    content: str | None, tool_calls: Sequence[dict[str, Any]] | None = None, *, model: str = PRIMARY_MODEL, cost: float | None = 0.01
) -> ModelResponse:
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    if cost is not None:
        usage["cost"] = cost
    return ModelResponse(
        model=model,
        choices=[
            {
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": list(tool_calls) if tool_calls else None},
            }
        ],
        usage=usage,
    )


def end_admit(findings: str = "The reply answers the request and changes nothing.") -> dict[str, Any]:
    """A clean END Advisor verdict that admits publication."""
    return {
        "verdict": "CLEAN",
        "category": "other",
        "steps": [],
        "findings": findings,
        "note": None,
        "publication_verdict": "ADMIT",
    }


def semantic_primary_response(
    request: dict[str, Any], *, instruction: str = "Use the retained inspection to explain the current pipeline to the user."
) -> dict[str, Any]:
    """One SDK response bound to the actual semantic request's own premise.

    This runs only at the external scripted SDK boundary. It does not produce
    a graph, tell a custody it dispatched, or replace the production parser.
    """
    assert request["model"] == ADVISOR_MODEL and "tools" not in request
    schema = request["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["schema_version"]["const"] == "composer.continuation-decision.v1"
    retained = json.loads(request["messages"][-1]["content"])
    source = ContinuationAdvisorInvocation.model_validate_json(canonical_json(retained["source"]), strict=True)
    frontier = decode_continuation_frontier(retained["frontier"])
    assert source.kind == "semantic" and source.premise is not None
    obligations = [item.obligation_id for item in frontier.obligations if item.status == "open"]
    assert obligations
    return {
        "schema_version": "composer.continuation-decision.v1",
        "decision": "CONTINUE",
        "scope": "whole_request",
        "obligation_ids": obligations,
        "obligations": [],
        "evidence": [],
        "reason": "The actual inspected state supports one concrete explanatory PRIMARY turn.",
        "remaining_actionable_ids": obligations,
        "prescribed_action": {
            "kind": "primary_turn",
            "obligation_ids": obligations,
            "evidence_ids": [],
            "input_hash": source.premise.input_hash,
            "graph_hash": frontier.graph_hash,
            "policy_hash": source.premise.policy_hash,
            "tool_manifest_hash": source.premise.tool_manifest_hash,
            "instruction": instruction,
        },
    }


def semantic_read_response(request: dict[str, Any], *, tool_name: str = "get_pipeline_state", arguments_json: str = "{}") -> dict[str, Any]:
    answer = semantic_primary_response(request)
    action = answer["prescribed_action"]
    action.pop("instruction")
    action.update(kind="read", tool_name=tool_name, arguments_json=arguments_json)
    answer["reason"] = "Use the real read owner to inspect an unresolved source fact before review."
    return answer


def semantic_review_response(request: dict[str, Any]) -> dict[str, Any]:
    from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, continuation_candidate_id

    answer = semantic_primary_response(request)
    retained = json.loads(request["messages"][-1]["content"])
    candidate = EndContinuationCandidate.model_validate_json(canonical_json(retained["end_candidate"]), strict=True)
    source = ContinuationAdvisorInvocation.model_validate_json(canonical_json(retained["source"]), strict=True)
    action = answer["prescribed_action"]
    action.pop("instruction")
    action.update(kind="review", candidate_ref=continuation_candidate_id(candidate), evidence_binding=source.coverage_binding)
    answer["reason"] = "The actual retained candidate must pass the original shared END review."
    return answer


def semantic_stop_response(request: dict[str, Any]) -> dict[str, Any]:
    from elspeth.web.sessions.continuation_semantic_review_result import decode_semantic_review_result

    answer = semantic_primary_response(request)
    retained = json.loads(request["messages"][-1]["content"])
    actual_review = decode_semantic_review_result(canonical_json(retained["review_result"]))
    assert actual_review.publication_binding is not None
    frontier = decode_continuation_frontier(retained["frontier"])
    answer.update(
        decision="STOP_CONFIRMED",
        remaining_actionable_ids=[],
        prescribed_action=None,
        obligation_ids=[item.obligation_id for item in frontier.obligations],
        obligations=[
            {
                "obligation_id": item.obligation_id,
                "parent_id": item.parent_id,
                "description": item.description,
                "original_message_id": item.original_message_id,
                "start": item.start,
                "end": item.end,
                "status": "satisfied",
            }
            for item in frontier.obligations
        ],
        reason="The complete real reviewed result addresses the originating request; no actionable work remains.",
    )
    return answer


def tool_call(app: FastAPI, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A provider tool call in the app's own planner dialect."""
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments

    dialect = app.state.composer_service._planner_dialect
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": canonical_json(encode_semantic_arguments(name, dialect, arguments))},
    }


type ScriptStep = tuple[str, Any]


class ScriptedSDK:
    """Physical SDK replies in order; each step names the role it must serve.

    ``("primary", (content, tool_calls))`` serves the composer model with the
    full palette; the payload may also be a callable taking the app, for tool
    calls that need the app's planner dialect. ``("end", verdict)`` serves the
    END Advisor review. ``("semantic", response_factory)`` serves the actual
    semantic Advisor request; its callable receives the real SDK kwargs.
    ``("raise", exception)`` raises the original exception
    object at the SDK boundary, whatever the request.
    """

    def __init__(self, steps: Sequence[ScriptStep], *, app_ref: list[FastAPI] | None = None) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.app_ref = app_ref if app_ref is not None else []

    async def __call__(self, **kwargs: Any) -> object:
        assert self.steps, "unscripted physical SDK call"
        role, payload = self.steps.pop(0)
        self.requests.append({"role": role, **kwargs})
        if role == "raise":
            raise payload
        if role == "primary":
            assert kwargs["model"] == PRIMARY_MODEL
            assert kwargs["tools"], "every PRIMARY call carries the provider palette"
            if callable(payload):
                payload = payload(self.app_ref[0])
            content, calls = payload
            return sdk_reply(content, calls)
        if role == "semantic":
            assert kwargs["model"] == ADVISOR_MODEL and "tools" not in kwargs
            response = payload(kwargs) if callable(payload) else payload
            return sdk_reply(json.dumps(response), model=ADVISOR_MODEL)
        assert role == "end" and kwargs["model"] == ADVISOR_MODEL and "response_format" in kwargs, role
        return ModelResponse(
            model=ADVISOR_MODEL,
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(payload), "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    @property
    def roles(self) -> list[str]:
        return [request["role"] for request in self.requests]


def install_sdk(monkeypatch: pytest.MonkeyPatch, sdk: Callable[..., Any]) -> None:
    monkeypatch.setattr(litellm, "acompletion", sdk)


async def settle(
    harness: ContinuationApp, *, operation_id: str, client: httpx.AsyncClient | None = None, rounds: int = 240
) -> ComposerOperationSnapshot:
    """Poll the public operation until it is sealed, then join the owner's release."""

    async def poll(active: httpx.AsyncClient) -> ComposerOperationSnapshot:
        for _ in range(rounds):
            response = await active.get(f"/api/sessions/{harness.session_id}/operations/{operation_id}")
            if response.status_code == 503 and response.json()["error_type"] == "database_unavailable":
                # SQLite serialises writers; a poll can lose its busy wait to
                # the owner's locked reads. The browser observer also retries.
                await asyncio.sleep(0.25)
                continue
            assert response.status_code == 200, response.text
            snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
            if snapshot.status in ("completed", "failed"):
                await harness.app.state.composer_async_worker.run_until_idle()
                return snapshot
            await asyncio.sleep(0.25)
        raise AssertionError("Continuation request did not settle")

    if client is not None:
        return await poll(client)
    async with harness.client() as owned:
        return await poll(owned)


async def submit_and_settle(
    harness: ContinuationApp, body: dict[str, Any], *, route: str = "messages", rounds: int = 240
) -> ComposerOperationSnapshot:
    """Admit one request through the production route and wait for its terminal."""
    async with harness.client() as client:
        accepted = await client.post(f"/api/sessions/{harness.session_id}/{route}", json=body)
        assert accepted.status_code == 202, accepted.text
        return await settle(harness, operation_id=body["operation_id"], client=client, rounds=rounds)


def effects(app: FastAPI, operation_id: str) -> tuple[Any, ...]:
    with app.state.session_engine.connect() as conn:
        return tuple(
            conn.execute(
                select(composer_continuation_effects_table)
                .where(composer_continuation_effects_table.c.operation_id == operation_id)
                .order_by(composer_continuation_effects_table.c.effect_key)
            )
        )


def assistant_contents(app: FastAPI, session_id: UUID) -> list[str]:
    with app.state.session_engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.content)
            .where(chat_messages_table.c.session_id == str(session_id), chat_messages_table.c.role == "assistant")
            .order_by(chat_messages_table.c.sequence_no)
        )
        return [row.content for row in rows]
