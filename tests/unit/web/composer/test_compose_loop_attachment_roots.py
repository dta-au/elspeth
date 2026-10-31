"""Authenticated attachment transitions through the genuine SDK boundary.

Provider replies and external guard HTTP are scripted; the owned test process
watchdog leaves process-death supervision outside this test's scope.
Graphs below are provider tool-call payloads; no graph/proposal/state producer
is replaced. Tutorial positives are explicit uploaded-attachment follow-ups,
not the complete fixed scrape/LLM/cleanup tutorial walkthrough.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import litellm
import pytest
import pytest_asyncio
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from litellm.types.utils import ChatCompletionMessageToolCall, Choices, Function, Message, ModelResponse, Usage
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.composer import advisor_context, prompt_guard_clients
from elspeth.web.composer import service as composer_service_module
from elspeth.web.composer.availability import compute_availability
from elspeth.web.composer.pipeline_proposal import PipelineProposal, composition_content_hash
from elspeth.web.composer.redaction import _ToolResultResponseModel
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.composer.tutorial_sample import resolve_tutorial_sample_urls, tutorial_sample_base_url
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import RequiredWorkCoordinator
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composer_async_operations_table,
    composition_proposals_table,
    composition_states_table,
    proposal_events_table,
    quota_provider_attempts_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog

_ACTOR = "u13-sdk-user"
_CSV = b"record_id,value\nalpha,7\n"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 16
_DECLINE = "I cannot author the requested pipeline. No pipeline has been created or changed."
_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class _Transition:
    operation_id: str
    request: str
    pipeline: dict[str, Any] | None
    state_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    primary_calls: int = 0
    authoring_call_id: str | None = None
    approval_feedback: dict[str, Any] | None = None


@dataclass(frozen=True)
class _SDKObservation:
    operation_id: str
    attempt_id: str
    fence_id: str
    epoch: int
    model: str
    strict_tool_count: int
    has_tools: bool
    response: ModelResponse
    semantic_calls: tuple[tuple[str, str, dict[str, Any]], ...]


def _structure_ids(app: FastAPI, session_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    with app.state.session_engine.connect() as conn:
        states = (
            conn.execute(select(composition_states_table.c.id).where(composition_states_table.c.session_id == session_id)).scalars().all()
        )
        proposals = (
            conn.execute(select(composition_proposals_table.c.id).where(composition_proposals_table.c.session_id == session_id))
            .scalars()
            .all()
        )
    return tuple(sorted(states)), tuple(sorted(proposals))


def _selector_ids(pipeline: dict[str, Any]) -> set[str]:
    roots = [pipeline["source"]] if "source" in pipeline else list(pipeline["sources"].values())
    return {blob_id for root in roots for blob_id in ([root["blob_id"]] if "blob_id" in root else root["blob_ids"])}


def _user_request_present(messages: list[dict[str, Any]], request: str) -> bool:
    for message in messages:
        if message["role"] != "user":
            continue
        content = message["content"]
        if type(content) is str:
            text = content
        else:
            assert type(content) is list
            text = "".join(part["text"] for part in content if part["type"] == "text")
        if request in text:
            return True
    return False


@dataclass
class _SDKScript:
    app: FastAPI
    dialect: ToolContractDialect
    guard_requests: list[tuple[str, dict[str, Any]]]
    observations: list[_SDKObservation] = field(default_factory=list)
    active: _Transition | None = None
    session_id: str | None = None

    async def complete(self, **kwargs: Any) -> ModelResponse:
        transition = self.active
        assert transition is not None and self.session_id is not None, "Unexpected SDK dispatch outside a user transition"
        model = kwargs["model"]
        settings = self.app.state.settings
        assert model in (settings.composer_model, settings.composer_advisor_model)
        primary = model == settings.composer_model
        assert kwargs["messages"]
        expected_request = transition.request
        if not primary:
            # The canonical brief contains this public contact address. The
            # actual advisor renders a dedicated complete-request section with
            # PII redacted; its JSON transcript is a separate evidence channel.
            expected_request = "\n".join(
                (
                    advisor_context._ADVISOR_UNTRUSTED_USER_MESSAGE_HEADER,
                    advisor_context._ADVISOR_UNTRUSTED_SUMMARY_BEGIN,
                    expected_request.replace("noreply@dta.gov.au", "<redacted-sensitive:email>"),
                    advisor_context._ADVISOR_UNTRUSTED_SUMMARY_END,
                )
            )
        assert _user_request_present(kwargs["messages"], expected_request), "The actual provider lost its genuine user-request evidence"
        expected_guard_host = "u13-primary-guard.example.test" if primary else "u13-advisor-guard.example.test"
        assert self.guard_requests and self.guard_requests[-1][0] == expected_guard_host
        assert self.guard_requests[-1][1]["userPrompt"] == transition.request

        with self.app.state.session_engine.connect() as conn:
            pending = (
                conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.settled_at.is_(None)))
                .mappings()
                .one()
            )
            job = (
                conn.execute(select(composer_async_operations_table).where(composer_async_operations_table.c.status == "running"))
                .mappings()
                .one()
            )
            fence = (
                conn.execute(select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == self.session_id))
                .mappings()
                .one()
            )
        assert pending["identity_id"] == job["actor_user_id"] == _ACTOR
        assert pending["session_id"] == job["session_id"] == self.session_id
        assert job["operation_id"] == transition.operation_id
        assert pending["operation_id"] == job["session_operation_id"] == fence["operation_id"]
        assert pending["operation_epoch"] == job["session_operation_epoch"] == fence["operation_epoch"]
        assert pending["lease_token"] == job["session_operation_lease_token"] == fence["lease_token"]
        assert fence["owner_instance_id"] == job["claim_owner_instance_id"]
        assert fence["operation_kind"] == "compose" and fence["released_at"] is None
        if transition.authoring_call_id is None:
            assert _structure_ids(self.app, self.session_id) == (transition.state_ids, transition.proposal_ids), (
                "Structural effects preceded the provider's authoring tool call"
            )

        tools = kwargs["tools"] if "tools" in kwargs else []
        functions = {tool["function"]["name"]: tool["function"] for tool in tools}
        strict_count = sum(function.get("strict") is True for function in functions.values())
        if primary and tools:
            assert "set_pipeline" in functions and "emit_pipeline_proposal" not in functions
            if self.dialect is ToolContractDialect.NONE:
                assert all("strict" not in function for function in functions.values())
            else:
                assert strict_count > 0
                assert functions["set_pipeline"]["strict"] is False

        semantic_calls: list[tuple[str, str, dict[str, Any]]] = []
        sdk_tools: list[ChatCompletionMessageToolCall] = []
        if primary:
            transition.primary_calls += 1
            if transition.pipeline is None:
                content = _DECLINE
            elif transition.primary_calls == 1:
                pipeline = transition.pipeline
                roots = [pipeline["source"]] if "source" in pipeline else list(pipeline["sources"].values())
                selections = list(dict.fromkeys(("source", root["plugin"]) for root in roots))
                selections.extend(("sink", output["plugin"]) for output in pipeline["outputs"])
                calls = [("list_blobs", {})] + [
                    ("get_plugin_schema", {"plugin_type": kind, "name": name}) for kind, name in dict.fromkeys(selections)
                ]
                for index, (name, arguments) in enumerate(calls):
                    self._append_tool(transition, functions, sdk_tools, semantic_calls, index, name, arguments)
                content = None
            elif transition.primary_calls == 2:
                assert _selector_ids(transition.pipeline) <= {
                    blob_id for blob_id in _selector_ids(transition.pipeline) if blob_id in json.dumps(kwargs["messages"])
                }, "Provider attachment identities were never supplied by actual discovery/context"
                self._append_tool(transition, functions, sdk_tools, semantic_calls, 0, "set_pipeline", transition.pipeline)
                transition.authoring_call_id = semantic_calls[-1][0]
                content = None
            else:
                feedback = [
                    message
                    for message in kwargs["messages"]
                    if message["role"] == "tool" and message["tool_call_id"] == transition.authoring_call_id
                ]
                assert len(feedback) == 1
                envelope = json.loads(feedback[0]["content"])
                assert type(envelope) is dict and envelope["success"] is True
                assert type(envelope["data"]) is dict and envelope["data"]["status"] == "APPROVAL_REQUIRED"
                if transition.approval_feedback is not None:
                    assert transition.approval_feedback == envelope
                transition.approval_feedback = deepcopy(envelope)
                content = "The pipeline proposal is ready for your review. Approve it before running it."
        else:
            response_format = kwargs["response_format"]["json_schema"]
            assert response_format["name"] in {"advisor_checkpoint_response", "advisor_end_response"}
            verdict = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "CLEAN", "note": None}
            if response_format["name"] == "advisor_end_response":
                verdict["publication_verdict"] = "ADMIT"
            Draft202012Validator(response_format["schema"]).validate(verdict)
            content = json.dumps(verdict)

        response = ModelResponse(
            id=f"u13-sdk-response-{len(self.observations) + 1}",
            model=model,
            choices=[
                Choices(
                    index=0,
                    message=Message(role="assistant", content=content, tool_calls=sdk_tools or None),
                    finish_reason="tool_calls" if sdk_tools else "stop",
                )
            ],
            usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
        assert type(response) is ModelResponse and type(response.choices[0]) is Choices
        assert type(response.choices[0].message) is Message and type(response.usage) is Usage
        self.observations.append(
            _SDKObservation(
                transition.operation_id,
                pending["attempt_id"],
                pending["operation_id"],
                pending["operation_epoch"],
                model,
                strict_count,
                bool(tools),
                response,
                tuple(semantic_calls),
            )
        )
        return response

    def _append_tool(
        self,
        transition: _Transition,
        functions: dict[str, Any],
        sdk_tools: list[ChatCompletionMessageToolCall],
        calls: list[tuple[str, str, dict[str, Any]]],
        index: int,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        assert name in functions
        wire = encode_semantic_arguments(name, self.dialect, arguments)
        Draft202012Validator(functions[name]["parameters"]).validate(wire)
        call_id = f"u13-{transition.operation_id}-{transition.primary_calls}-{index}"
        sdk_tools.append(
            ChatCompletionMessageToolCall(id=call_id, type="function", function=Function(name=name, arguments=json.dumps(wire)))
        )
        calls.append((call_id, name, deepcopy(arguments)))


@dataclass
class _Harness:
    app: FastAPI
    client: httpx.AsyncClient
    sdk: _SDKScript


def _coordinator_custody_facts(coordinator: RequiredWorkCoordinator) -> dict[str, Any]:
    with coordinator._lock:
        tickets = coordinator.tickets
        children = tuple(coordinator._proposal_children.values())
        release_prepared = coordinator._release_prepared
    incomplete = []
    for ticket in tickets:
        with ticket._lock:
            if ticket.complete:
                continue
            key = ticket.key
            future = ticket._future
            incomplete.append(
                {
                    "source": key.source.name,
                    "source_ordinal": int(key.source),
                    "stage": key.stage.name,
                    "subphase": key.subphase.name,
                    "transition_ordinal": key.transition_ordinal,
                    "semantic_ordinal": key.semantic_ordinal,
                    "invocation_ordinal": key.invocation_ordinal,
                    "sql_attempt_ordinal": key.sql_attempt_ordinal,
                    "complete": ticket.complete,
                    "future_bound": future is not None,
                    "future_done": future.done() if future is not None else None,
                    "future_cancelled": future.cancelled() if future is not None else None,
                    "projection_started": ticket._projection_started,
                    "no_submission": ticket._no_submission,
                    "submission_unknown": ticket._submission_unknown,
                    "generation_joined": ticket._generation_joined,
                    "error_count": len(ticket.errors),
                    "custody_error_count": len(ticket._custody_errors),
                }
            )
    return {
        "authority_kind": coordinator.authority.authority_kind.name,
        "all_completed": coordinator.all_completed,
        "release_prepared": release_prepared,
        "ticket_count": len(tickets),
        "incomplete_ticket_count": len(incomplete),
        "incomplete_tickets": incomplete,
        "child_count": len(children),
        "children": [_coordinator_custody_facts(child) for child in children],
    }


def _emit_worker_custody_snapshot(app: FastAPI, *, phase: str) -> None:
    worker = app.state.composer_async_worker
    # Values only: never retain/print authority keys, payloads, results,
    # exception objects, task locals or object representations.
    snapshot = {
        "phase": phase,
        "worker_type": f"{type(worker).__module__}.{type(worker).__qualname__}",
        "stopping": worker._stopping,
        "job_count": len(worker._jobs),
        "jobs": [{"done": task.done(), "cancelled": task.cancelled()} for task in worker._jobs.values()],
        "loop_done": worker._loop_task.done() if worker._loop_task is not None else None,
        "loop_cancelled": worker._loop_task.cancelled() if worker._loop_task is not None else None,
        "coordinators": [_coordinator_custody_facts(owner) for owner in worker._required_coordinators.values()],
        "recovery_coordinators": [
            _coordinator_custody_facts(owner) for owners in worker._recovery_coordinators.values() for owner in owners
        ],
    }
    print("U13_REQUIRED_WORK_SNAPSHOT " + json.dumps(snapshot, sort_keys=True), flush=True)


@asynccontextmanager
async def _observed_sdk_lifespan(app: FastAPI) -> AsyncIterator[None]:
    diagnostic_failures: list[BaseException] = []
    try:
        async with app.router.lifespan_context(app):
            try:
                yield
            finally:
                try:
                    _emit_worker_custody_snapshot(app, phase="before_lifespan_exit")
                except BaseException as diagnostic_failure:
                    # A diagnostic must not become an application shutdown
                    # trigger. Defer its failure until the real lifespan exits.
                    diagnostic_failures.append(diagnostic_failure)
    except BaseException as lifecycle_failure:
        try:
            _emit_worker_custody_snapshot(app, phase="after_lifespan_exit")
        except BaseException as diagnostic_failure:
            diagnostic_failures.append(diagnostic_failure)
        if diagnostic_failures:
            raise BaseExceptionGroup("SDK lifecycle and custody diagnostic failures", [lifecycle_failure, *diagnostic_failures]) from None
        raise
    else:
        try:
            _emit_worker_custody_snapshot(app, phase="after_lifespan_exit")
        except BaseException as diagnostic_failure:
            diagnostic_failures.append(diagnostic_failure)
        if len(diagnostic_failures) == 1:
            raise diagnostic_failures[0]
        if diagnostic_failures:
            raise BaseExceptionGroup("SDK custody diagnostic failures", diagnostic_failures)


@pytest.fixture(autouse=True)
def _composer_available_for_phase3() -> None:
    """Shadow the enclosing fake-availability fixture with the real resolver.

    This harness supplies an offline credential and scripts the SDK seam; its
    configured provider/model provenance must remain production-computed.
    """
    assert composer_service_module.compute_availability is compute_availability


@pytest_asyncio.fixture
async def attachment_sdk_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialect: ToolContractDialect) -> AsyncIterator[_Harness]:
    guard_requests: list[tuple[str, dict[str, Any]]] = []

    def guard_http(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host in {"u13-primary-guard.example.test", "u13-advisor-guard.example.test"}
        assert request.url.path == "/contentsafety/text:shieldPrompt"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "offline-u13-guard-subscription"
        payload = json.loads(request.content)
        assert set(payload) == {"userPrompt", "documents"}
        assert type(payload["userPrompt"]) is str and type(payload["documents"]) is list
        assert all(type(document) is str for document in payload["documents"])
        guard_requests.append((request.url.host, payload))
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def guard_client(settings: PromptGuardSettings) -> prompt_guard_clients.AzurePromptGuardClient:
        # Supply only the external HTTP transport; admission, parsing, decision
        # and durable guard audit remain actual Azure/client/gateway producers.
        return prompt_guard_clients.AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(guard_http))

    monkeypatch.setenv("OPENAI_API_KEY", "offline-u13-sdk-not-a-provider-credential")
    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", guard_client)
    data_dir = tmp_path / "app"
    settings = WebSettings(
        data_dir=data_dir,
        session_db_url=f"sqlite:///{data_dir}/sessions.db",
        landscape_url=f"sqlite:///{data_dir}/runs/audit.db",
        payload_store_path=data_dir / "payloads",
        auth_provider="local",
        registration_mode="open",
        composer_model="gpt-5.5",
        composer_advisor_model="openai/gpt-4.1",
        composer_endpoint_base_url=None,
        composer_endpoint_api_key=None,
        composer_advisor_endpoint_base_url=None,
        composer_advisor_endpoint_api_key=None,
        composer_prompt_guard=PromptGuardSettings(
            endpoint="https://u13-primary-guard.example.test", subscription_key=SecretStr("offline-u13-guard-subscription")
        ),
        composer_advisor_prompt_guard=PromptGuardSettings(
            endpoint="https://u13-advisor-guard.example.test", subscription_key=SecretStr("offline-u13-guard-subscription")
        ),
        composer_strict_tools="off" if dialect is ToolContractDialect.NONE else "forward_to_endpoint",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=bytes(range(32)),
        plugin_allowlist=("source:csv", "source:text", "source:blob_rows", "sink:json"),
    )
    assert settings.composer_boot_probe_enabled is True
    app = create_app(settings=settings, process_watchdog_factory=OwnedTestProcessWatchdog)
    availability = app.state.composer_service.get_availability()
    assert availability is app.state.composer_availability
    assert availability.available and availability.model == settings.composer_model
    assert availability.provider == "openai" and availability.missing_keys == ()
    assert app.dependency_overrides == {}
    provider = app.state.auth_provider
    assert isinstance(provider, LocalAuthProvider)
    provider.create_user(_ACTOR, "offline attachment password", "Attachment SDK User")
    with app.state.session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id=_ACTOR)
        grant_test_pipeline_user(conn, identity_id=_ACTOR)
    sdk = _SDKScript(app, dialect, guard_requests)
    monkeypatch.setattr(litellm, "acompletion", sdk.complete)
    async with (
        _observed_sdk_lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://u13.test") as client,
    ):
        assert sdk.observations == []
        unauthenticated = await client.post("/api/sessions", json={"title": "Unauthorized must refuse"})
        assert unauthenticated.status_code == 401, unauthenticated.text
        login = await client.post("/api/auth/login", json={"username": _ACTOR, "password": "offline attachment password"})
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        me = await client.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["user_id"] == _ACTOR, me.text
        yield _Harness(app, client, sdk)


async def _create_session_and_uploads(harness: _Harness, request_kind: str) -> tuple[str, dict[str, dict[str, Any]]]:
    title = "First-run tutorial (in progress)" if request_kind.startswith("tutorial") else "Authenticated attachment SDK"
    response = await harness.client.post("/api/sessions", json={"title": title})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert response.json()["user_id"] == _ACTOR
    preferences = await harness.client.patch(
        f"/api/sessions/{session_id}/composer/preferences", json={"trust_mode": "explicit_approve", "density_default": "high"}
    )
    assert preferences.status_code == 200 and preferences.json()["trust_mode"] == "explicit_approve", preferences.text
    blobs: dict[str, dict[str, Any]] = {}
    for name, content, mime in (("records.csv", _CSV, "text/csv"), ("first.png", _PNG, "image/png"), ("second.jpg", _JPEG, "image/jpeg")):
        uploaded = await harness.client.post(f"/api/sessions/{session_id}/blobs", files={"file": (name, content, mime)})
        assert uploaded.status_code == 201, uploaded.text
        blob = uploaded.json()
        assert (blob["session_id"], blob["filename"], blob["status"], blob["creation_modality"]) == (
            session_id,
            name,
            "ready",
            CreationModality.VERBATIM.value,
        )
        assert blob["content_hash"] == hashlib.sha256(content).hexdigest() and blob["size_bytes"] == len(content)
        blobs[name] = blob
    assert _structure_ids(harness.app, session_id) == ((), ())
    harness.sdk.session_id = session_id
    return session_id, blobs


def _provider_pipeline(kind: str, blobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def root(plugin: str, **selector: Any) -> dict[str, Any]:
        return {
            "plugin": plugin,
            "on_success": "documents",
            "on_validation_failure": "discard",
            "description": "Provider-authored attachment root",
            "options": {"schema": {"mode": "observed"}, **({"column": "provider_text"} if plugin == "text" else {})},
            **selector,
        }

    pipeline = {
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "documents",
                "plugin": "json",
                "on_write_failure": "discard",
                "description": None,
                "options": {
                    "path": "outputs/attachment-records.jsonl",
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            }
        ],
        "metadata": {"name": f"Provider {kind} attachment pipeline", "description": "Explicit uploaded-attachment exercise"},
    }
    if kind == "existing_scalar":
        pipeline["source"] = root("csv", blob_id=blobs["records.csv"]["id"])
    elif kind == "named_roots":
        pipeline["sources"] = {
            "table": root("text", blob_id=blobs["records.csv"]["id"]),
            "scans": root("blob_rows", blob_ids=[blobs["second.jpg"]["id"], blobs["first.png"]["id"]]),
        }
    else:
        assert kind == "default_plural"
        pipeline["source"] = root("blob_rows", blob_ids=[blobs["first.png"]["id"], blobs["second.jpg"]["id"]])
    return pipeline


def _authoring_request(kind: str, request_kind: str) -> str:
    prefix = (
        "This is an explicit tutorial follow-up using uploaded attachments, replacing the scraping exercise. "
        if request_kind == "tutorial_followup"
        else ""
    )
    action = {
        "existing_scalar": "Create a pipeline using records.csv as a CSV source.",
        "named_roots": "Fully replace the pipeline with named roots table and scans. Read records.csv as text lines in provider_text; scans must be document rows ordered second.jpg then first.png.",
        "default_plural": "Fully replace the pipeline with one default document-row source ordered first.png then second.jpg.",
    }[kind]
    return (
        prefix
        + action
        + " Route all roots directly to the documents JSON-lines sink at outputs/attachment-records.jsonl. Explicitly discard source-validation and sink-write failures. Show a proposal for review; do not run it."
    )


async def _submit(harness: _Harness, session_id: str, request: str, pipeline: dict[str, Any] | None) -> _Transition:
    current = await harness.app.state.session_service.get_current_state(UUID(session_id))
    states, proposals = _structure_ids(harness.app, session_id)
    transition = _Transition(str(uuid4()), request, deepcopy(pipeline), states, proposals)
    harness.sdk.active = transition
    response = await harness.client.post(
        f"/api/sessions/{session_id}/messages",
        json={"operation_id": transition.operation_id, "content": request, "state_id": str(current.id) if current is not None else None},
    )
    assert response.status_code == 202 and response.json()["operation_id"] == transition.operation_id, response.text
    deadline = asyncio.get_running_loop().time() + harness.app.state.settings.composer_timeout_seconds + 10
    while True:
        terminal = await harness.client.get(f"/api/sessions/{session_id}/operations/{transition.operation_id}")
        assert terminal.status_code == 200, terminal.text
        snapshot = terminal.json()
        if snapshot["status"] in ("completed", "failed"):
            break
        assert asyncio.get_running_loop().time() < deadline, snapshot
        await asyncio.sleep(0.02)
    # Preserve the actual terminal payload, including any P4/resource failure.
    # Such a failure is a receiving finding, never permission to trim a stage.
    assert snapshot["status"] == "completed", ("U13_GENUINE_SDK_TERMINAL_FAILURE", snapshot)
    observations = [item for item in harness.sdk.observations if item.operation_id == transition.operation_id]
    assert observations and any(item.model == harness.app.state.settings.composer_model for item in observations)
    assert any(item.model == harness.app.state.settings.composer_advisor_model for item in observations)
    harness.sdk.active = None
    return transition


async def _assert_provider_receipts(harness: _Harness, session_id: str) -> None:
    with harness.app.state.session_engine.connect() as conn:
        attempts = conn.execute(select(quota_provider_attempts_table)).mappings().all()
        ledger = conn.execute(select(token_usage_ledger_table)).mappings().all()
    observed = {item.attempt_id: item for item in harness.sdk.observations}
    assert len(observed) == len(harness.sdk.observations) == len(attempts) == len(ledger)
    assert {row["attempt_id"] for row in attempts} == set(observed)
    assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in ledger}
    assert all(row["settled_at"] is not None and row["identity_id"] == _ACTOR and row["session_id"] == session_id for row in attempts)
    ledger_by_id = {row["entry_id"]: row for row in ledger}
    for attempt in attempts:
        item = observed[attempt["attempt_id"]]
        usage = ledger_by_id[attempt["ledger_entry_id"]]
        assert (attempt["operation_id"], attempt["operation_epoch"]) == (item.fence_id, item.epoch)
        assert (usage["identity_id"], usage["session_id"], usage["source"], usage["model"]) == (_ACTOR, session_id, "composer", item.model)
        assert (usage["prompt_tokens"], usage["completion_tokens"]) == (100, 20)
    messages = await harness.app.state.session_service.get_messages(UUID(session_id), limit=None)
    calls = [
        entry["call"]
        for message in messages
        if message.role == "audit"
        for entry in (message.tool_calls or ())
        if entry["_kind"] == "llm_call_audit"
    ]
    assert len(calls) == len(observed) and {call["call_id"] for call in calls} == set(observed)
    for call in calls:
        item = observed[call["call_id"]]
        assert call["status"] == "success"
        assert call["model_requested"] == call["model_returned"] == item.model
        assert call["provider_request_id"] == item.response.id
        assert (call["prompt_tokens"], call["completion_tokens"], call["total_tokens"]) == (100, 20, 120)
        assert call["provider_cost_source"] == "litellm.cost_per_token"
        assert type(call["provider_cost"]) in (float, int) and math.isfinite(call["provider_cost"]) and call["provider_cost"] > 0
        if item.model == harness.app.state.settings.composer_model and item.has_tools:
            assert call["tool_contract_dialect"] == harness.sdk.dialect.value
            assert call["strict_tool_count"] == item.strict_tool_count
    guards = [
        entry["check"]
        for message in messages
        if message.role == "audit"
        for entry in (message.tool_calls or ())
        if entry["_kind"] == "prompt_guard_audit"
    ]
    assert len(guards) == len(harness.sdk.observations)
    for check, item in zip(guards, harness.sdk.observations, strict=True):
        assert check["operation_id"] == item.fence_id and check["operation_epoch"] == item.epoch
        assert check["disposition"] == "allow"
        assert check["role"] == ("composer" if item.model == harness.app.state.settings.composer_model else "advisor")


async def _accept_and_verify(harness: _Harness, session_id: str, transition: _Transition) -> None:
    assert transition.pipeline is not None and transition.authoring_call_id is not None
    response = await harness.client.get(f"/api/sessions/{session_id}/proposals", params={"status": "pending"})
    assert response.status_code == 200, response.text
    (proposal,) = response.json()
    assert proposal["status"] == "pending" and proposal["committed_state_id"] is None
    assert proposal["tool_name"] == "set_pipeline" and proposal["tool_call_id"] == transition.authoring_call_id
    states, proposals = _structure_ids(harness.app, session_id)
    assert states == transition.state_ids
    assert set(proposals) == {*transition.proposal_ids, proposal["id"]}
    service = harness.app.state.session_service
    before = await service.get_current_state(UUID(session_id))
    with harness.app.state.session_engine.connect() as conn:
        native = (
            conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal["id"])).mappings().one()
        )
        job = (
            conn.execute(
                select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == transition.operation_id)
            )
            .mappings()
            .one()
        )
        created = conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == native["audit_event_id"])).mappings().one()
        user = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == job["user_message_id"])).mappings().one()
    authority = await service.get_authoritative_pipeline_proposal(session_id=UUID(session_id), proposal_id=UUID(proposal["id"]))
    restored = authority.proposal
    assert type(restored) is PipelineProposal
    assert str(authority.row.id) == proposal["id"]
    assert native["arguments_json"] == transition.pipeline
    assert deep_thaw(restored.pipeline) == transition.pipeline
    assert restored.draft_hash == proposal["pipeline_metadata"]["draft_hash"]
    assert native["user_message_id"] == job["user_message_id"]
    assert user["role"] == "user" and user["writer_principal"] == "route_user_message" and user["content"] == transition.request
    assert native["composer_model_identifier"] == native["composer_model_version"] == harness.app.state.settings.composer_model
    assert native["composer_provider"] == harness.app.state.composer_availability.provider
    assert created["actor"] == f"composer-web:user:{_ACTOR}"
    creation = created["payload"]
    assert creation["schema"] == "pipeline_proposal_created.v3" and creation["tool_call_id"] == transition.authoring_call_id
    binding = creation["composer_operation"]
    assert binding == {
        "operation_id": transition.operation_id,
        "session_operation_id": job["session_operation_id"],
        "session_operation_epoch": job["session_operation_epoch"],
        "attempt": job["attempt"],
    }
    if before is None:
        assert proposal["base_state_id"] is None and proposal["pipeline_metadata"]["base"] == {"kind": "absent"}
    else:
        assert proposal["base_state_id"] == str(before.id)
        assert proposal["pipeline_metadata"]["base"] == {
            "kind": "present",
            "state_id": str(before.id),
            "composition_content_hash": composition_content_hash(state_from_record(before)),
        }
    observations = [item for item in harness.sdk.observations if item.operation_id == transition.operation_id]
    authorizations = [call for item in observations for call in item.semantic_calls if call[1] == "set_pipeline"]
    assert authorizations == [(transition.authoring_call_id, "set_pipeline", transition.pipeline)]
    messages = await service.get_messages(UUID(session_id), limit=None)
    assistant_calls = [
        call
        for message in messages
        if message.role == "assistant"
        for call in (message.tool_calls or ())
        if call["id"] == transition.authoring_call_id
    ]
    assert len(assistant_calls) == 1 and assistant_calls[0]["function"]["name"] == "set_pipeline"
    assert assistant_calls[0]["wire_conformant"] is True
    tool_rows = [message for message in messages if message.role == "tool" and message.tool_call_id == transition.authoring_call_id]
    assert len(tool_rows) == 1
    # Persistence deliberately reduces open ToolResult data to a value-free
    # string; the provider receives the complete serialized payload instead.
    stored = _ToolResultResponseModel.model_validate_json(tool_rows[0].content, strict=True)
    assert stored.model_dump(mode="json", exclude_unset=True) == json.loads(tool_rows[0].content)
    assert stored.success is True and stored.affected_nodes == [] and stored.data == "<redacted-response-mapping>"
    feedback = transition.approval_feedback
    assert feedback is not None and feedback["success"] is True and feedback["affected_nodes"] == []
    assert feedback["version"] == stored.version == (before.version if before is not None else 1)
    assert feedback["data"] == {
        "status": "APPROVAL_REQUIRED",
        "proposal_id": proposal["id"],
        "tool_name": "set_pipeline",
        "summary": proposal["summary"],
        "message": "The requested pipeline change is pending human approval and has not been applied.",
    }
    await _assert_provider_receipts(harness, session_id)

    sdk_count = len(harness.sdk.observations)
    accepted = await harness.client.post(
        f"/api/sessions/{session_id}/proposals/{proposal['id']}/accept", json={"draft_hash": restored.draft_hash}
    )
    assert accepted.status_code == 200 and accepted.json()["status"] == "committed", accepted.text
    assert len(harness.sdk.observations) == sdk_count, "Manual acceptance made an unrequested provider call"
    with harness.app.state.session_engine.connect() as conn:
        event = (
            conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == accepted.json()["audit_event_id"]))
            .mappings()
            .one()
        )
    payload = event["payload"]
    assert event["actor"] == f"user:{_ACTOR}" and payload["schema"] == "pipeline_proposal_accepted.v2"
    assert payload["creation_composer_operation"] == binding
    assert payload["tool_call_id"] == payload["dispatch"]["tool_call_id"] == transition.authoring_call_id
    assert payload["dispatch"]["tool_name"] == "set_pipeline" and payload["dispatch"]["status"] == "success"
    assert accepted.json()["committed_state_id"] == payload["committed_state_id"]
    current = await service.get_current_state(UUID(session_id))
    assert current is not None and str(current.id) == payload["final_state_id"]
    assert composition_content_hash(state_from_record(current)) == payload["final_state_content_hash"]
    committed = await service.get_state_in_session(state_id=UUID(payload["committed_state_id"]), session_id=UUID(session_id))
    assert composition_content_hash(state_from_record(committed)) == payload["committed_state_content_hash"]
    _assert_resolved_sources(harness.app, session_id, transition.pipeline, current.sources)
    assert current.metadata_["name"] == transition.pipeline["metadata"]["name"]
    assert current.nodes == () and current.edges == ()
    await _assert_provider_receipts(harness, session_id)


def _assert_resolved_sources(app: FastAPI, session_id: str, pipeline: dict[str, Any], sources: Any) -> None:
    assert sources is not None
    sources = deep_thaw(sources)
    roots = {"source": pipeline["source"]} if "source" in pipeline else pipeline["sources"]
    assert set(sources) == set(roots)
    for name, root in roots.items():
        source = sources[name]
        assert (source["plugin"], source["on_success"], source["on_validation_failure"], source["description"]) == (
            root["plugin"],
            root["on_success"],
            root["on_validation_failure"],
            root["description"],
        )
        ids = [root["blob_id"]] if "blob_id" in root else root["blob_ids"]
        expected = []
        with app.state.session_engine.connect() as conn:
            for blob_id in ids:
                row = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).mappings().one()
                assert (
                    row["session_id"] == session_id
                    and row["status"] == "ready"
                    and row["creation_modality"] == CreationModality.VERBATIM.value
                )
                expected.append(
                    {
                        "blob_id": row["id"],
                        "payload_ref": row["content_hash"],
                        "filename": row["filename"],
                        "mime_type": row["mime_type"],
                        "size_bytes": row["size_bytes"],
                    }
                )
                if "blob_id" in root:
                    assert source["options"]["path"] == row["storage_path"]
        if "blob_id" in root:
            assert source["options"]["blob_ref"] == ids[0]
            if root["plugin"] == "text":
                assert source["options"]["column"] == "provider_text"
        else:
            assert source["plugin"] == "blob_rows" and source["options"]["blobs"] == expected
            assert "blob_ref" not in source["options"] and "path" not in source["options"]


def _literal_ts_constant(source: str, name: str) -> str:
    marker = f"export const {name} =\n"
    assert source.count(marker) == 1, f"Canonical tutorial literal {name} is missing or repeated"
    declaration, terminator, _rest = source.split(marker, 1)[1].partition(";\n")
    assert terminator
    pieces = [json.loads(piece.strip()) for piece in declaration.strip().split(" +\n")]
    assert pieces and all(type(piece) is str for piece in pieces)
    return "".join(pieces)


def _canonical_tutorial_request(settings: WebSettings) -> str:
    assert _literal_ts_constant('export const control =\n  "left " +\n  "right";\n', "control") == "left right"
    with pytest.raises(AssertionError):
        _literal_ts_constant('export const control =\n  "value";\n', "missing")
    with pytest.raises(json.JSONDecodeError):
        _literal_ts_constant("export const control =\n  compute();\n", "control")
    frontend = _REPO_ROOT / "src/elspeth/web/frontend/src/components/tutorial"
    machine = (frontend / "tutorialMachine.ts").read_text()
    shell = (frontend / "TutorialFreeformShell.tsx").read_text()
    header = "Build a pipeline to scrape and summarize these three synthetic project briefs."
    closing = "Please show me the proposed pipeline for review before I run it."
    assert json.dumps(header) in shell and json.dumps(closing) in shell
    urls = resolve_tutorial_sample_urls(base_url=tutorial_sample_base_url(settings=settings))
    return "\n\n".join(
        (
            header,
            _literal_ts_constant(machine, "TUTORIAL_SOURCE_PROMPT") + "\n" + "\n".join(urls),
            _literal_ts_constant(machine, "TUTORIAL_SINK_PROMPT"),
            _literal_ts_constant(machine, "TUTORIAL_TRANSFORMS_PROMPT"),
            closing,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", (ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT), ids=("none", "openai-strict"))
@pytest.mark.parametrize("request_kind", ("ordinary", "tutorial_followup"), ids=("ordinary", "tutorial-followup"))
async def test_authenticated_existing_scalar_has_sdk_owned_proposal(attachment_sdk_app: _Harness, request_kind: str) -> None:
    harness = attachment_sdk_app
    session_id, blobs = await _create_session_and_uploads(harness, request_kind)
    pipeline = _provider_pipeline("existing_scalar", blobs)
    transition = await _submit(harness, session_id, _authoring_request("existing_scalar", request_kind), pipeline)
    await _accept_and_verify(harness, session_id, transition)


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", (ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT), ids=("none", "openai-strict"))
@pytest.mark.parametrize("request_kind", ("ordinary", "tutorial_followup"), ids=("ordinary", "tutorial-followup"))
async def test_authenticated_provider_authors_attachment_roots_per_transition(attachment_sdk_app: _Harness, request_kind: str) -> None:
    harness = attachment_sdk_app
    session_id, blobs = await _create_session_and_uploads(harness, request_kind)
    for kind in ("existing_scalar", "named_roots", "default_plural"):
        pipeline = _provider_pipeline(kind, blobs)
        transition = await _submit(harness, session_id, _authoring_request(kind, request_kind), pipeline)
        await _accept_and_verify(harness, session_id, transition)


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", (ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT), ids=("none", "openai-strict"))
@pytest.mark.parametrize("request_kind", ("ordinary", "canonical_tutorial"), ids=("ordinary", "canonical-tutorial"))
async def test_authenticated_provider_decline_never_authors_a_graph(attachment_sdk_app: _Harness, request_kind: str) -> None:
    harness = attachment_sdk_app
    session_id, _blobs = await _create_session_and_uploads(harness, "tutorial" if request_kind == "canonical_tutorial" else "ordinary")
    request = (
        _canonical_tutorial_request(harness.app.state.settings)
        if request_kind == "canonical_tutorial"
        else _authoring_request("named_roots", "ordinary")
    )
    transition = await _submit(harness, session_id, request, None)
    assert transition.authoring_call_id is None
    assert _structure_ids(harness.app, session_id) == ((), ())
    assert await harness.app.state.session_service.get_current_state(UUID(session_id)) is None
    response = await harness.client.get(f"/api/sessions/{session_id}/proposals")
    assert response.status_code == 200 and response.json() == [], response.text
    observations = [item for item in harness.sdk.observations if item.operation_id == transition.operation_id]
    assert all(not item.semantic_calls for item in observations)
    await _assert_provider_receipts(harness, session_id)
