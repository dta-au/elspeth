"""Freeform planner failures are translated into safe HTTP outcomes.

These tests exercise ``PipelinePlannerError`` on the empty-pipeline path
through the real FastAPI stack and assert a deliberate safe status, a
``failed`` progress snapshot, a durable redacted disposition audit row, and no
raw provider content leak.

The planner's own LLM-call audit evidence (``attach_llm_calls`` +
``_plan_and_stage_empty_pipeline``) is already durable and is deliberately NOT
re-persisted here — these tests assert it survives alongside the new
disposition record, but the disposition record is a separate row.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import litellm
import pytest
import structlog
from litellm.exceptions import APIError as LiteLLMAPIError
from sqlalchemy import func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, diagnostic_evidence_identity
from elspeth.web.composer.pipeline_planner import _PROSE_NUDGE_BUDGET
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    chat_messages_table,
    composer_async_operations_table,
    composition_proposals_table,
    composition_states_table,
)
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.composer_fakes import StaticPluginSnapshotFactory
from tests.fixtures.identities import ensure_test_identity, wire_test_pipeline_user_authority
from tests.helpers.composer_operations import install_composer_async_worker, settle_sync
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

# Distinctive token planted in every scripted provider payload / error so the
# leak assertions distinguish deliberately withheld prose from public output
# and value-free audit metadata.
_PROVIDER_LEAK_SENTINEL = "PROVIDER-LEAK-SENTINEL-9f13c7"
_WITHHELD_PROSE = f"I cannot help. {_PROVIDER_LEAK_SENTINEL}"

_EMPTY_INTENT = "Build a CSV to JSONL pipeline."
_PARITY_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "evals" / "composer-parity" / "fixtures"
_COMPLETE_MULTI_CLAUSE_REQUESTS = (
    pytest.param(
        "Build a pipeline that reads customers.csv and writes results.jsonl.",
        id="ordinary-source-and-sink-clauses",
    ),
    pytest.param(
        "Build a pipeline that reads customers.csv and splits rows by country. Do not do that with a generic transform.",
        id="scoped-anaphoric-constraint",
    ),
    pytest.param(
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not add a generic transform.",
        id="scoped-negative-action-constraint",
    ),
    *(
        pytest.param(json.loads(path.read_text(encoding="utf-8"))["intent"], id=f"parity-{path.stem}")
        for path in sorted(_PARITY_FIXTURE_DIR.glob("*.json"))
    ),
)


@dataclass
class _Function:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Function


@dataclass
class _Message:
    content: str | None
    tool_calls: list[_ToolCall] = field(default_factory=list)


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]
    usage: Mapping[str, object]
    model: str = "provider/planner-v1"
    id: str = "planner-request-1"


def _malformed_completion() -> Any:
    """A prose overrun whose escape-hatch turn is also unusable.

    Three no-tool-call replies exhaust the nudge budget and engage the escape
    hatch; the advisor then answers with malformed tool-call arguments, so the
    hatch is spent and the overrun's terminal MALFORMED_RESPONSE disposition
    stands. Every reply carries the leak sentinel.
    """

    def _prose() -> _Response:
        return _Response(
            choices=[_Choice(message=_Message(content=_WITHHELD_PROSE, tool_calls=[]))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    def _malformed_arguments() -> _Response:
        return _Response(
            choices=[
                _Choice(
                    message=_Message(
                        content=None,
                        tool_calls=[
                            _ToolCall(
                                id="call-1",
                                function=_Function(
                                    name="emit_pipeline_proposal",
                                    arguments=f"not strict json {_PROVIDER_LEAK_SENTINEL}",
                                ),
                            )
                        ],
                    )
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    responses = [_prose(), _prose(), _prose(), _malformed_arguments()]

    async def completion(**_kwargs: Any) -> _Response:
        return responses.pop(0)

    return completion


def _conversational_completion() -> Any:
    """Offline SDK reply; real ComposerServiceImpl still owns the turn and audit."""

    async def completion(**_kwargs: Any) -> _Response:
        return _Response(
            choices=[_Choice(message=_Message(content="No pipeline changes were requested."))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    return completion


def _cost_unavailable_completion() -> Any:
    async def completion(**_kwargs: Any) -> _Response:
        return _Response(
            choices=[_Choice(message=_Message(content=_PROVIDER_LEAK_SENTINEL))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    return completion


def _timeout_completion() -> Any:
    """A provider timeout — trips the planner wall-clock TIMEOUT code."""

    async def completion(**_kwargs: Any) -> _Response:
        raise TimeoutError(_PROVIDER_LEAK_SENTINEL)

    return completion


def _provider_error_completion() -> Any:
    """A declared LiteLLM provider failure — trips the PROVIDER_ERROR code."""

    async def completion(**_kwargs: Any) -> _Response:
        raise LiteLLMAPIError(
            status_code=503,
            message=f"provider unavailable {_PROVIDER_LEAK_SENTINEL}",
            llm_provider="test-provider",
            model="test/planner",
        )

    return completion


def _build_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: Any,
    *,
    pending_llm_review: bool = False,
) -> tuple[SyncASGITestClient, Any, SessionServiceImpl]:
    """Wire a minimal real FastAPI app whose composer is a real ComposerServiceImpl.

    The provider double drives the real freeform routes through
    ``ComposerServiceImpl.compose`` →
    ``_plan_and_stage_empty_pipeline`` → ``plan_pipeline`` path so the scripted
    completion drives a genuine ``PipelinePlannerError``.
    """
    from fastapi import FastAPI

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.sqlite3'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    sessions = FencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.freeform.planner.failure"),
    )

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_boot_probe_enabled=False,
        llm_profiles={
            "offline-review": {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                "region_name": "us-east-1",
            }
        }
        if pending_llm_review
        else {},
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )

    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **_kwargs: ComposerAvailability(available=True, provider="test", model="test/planner", reason=None),
    )
    monkeypatch.setattr("litellm.acompletion", completion)

    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(),
        settings,
        sessions_service=sessions,
        session_engine=engine,
    )

    app = FastAPI()
    wire_test_pipeline_user_authority(app, identity_id="alice", engine=engine)

    async def mock_user() -> UserIdentity:
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = mock_user
    app.state.session_service = sessions
    app.state.sessions_telemetry = build_sessions_telemetry()
    app.state.session_engine = engine
    app.state.scoped_secret_resolver = None
    app.state.settings = settings
    app.state.composer_service = composer
    app.state.interpretation_surfacing = composer._interpretation_surfacing
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.state.catalog_service = create_catalog_service()
    runtime_policy = RuntimeWebPluginConfig.from_settings(settings)
    app.state.web_plugin_policy = compile_web_plugin_policy(
        registry=get_shared_plugin_manager(),
        settings=runtime_policy,
    )
    app.state.operator_profile_registry = OperatorProfileRegistry(
        policy=app.state.web_plugin_policy,
        settings=runtime_policy,
    )

    class _EmptyInventory:
        def has_server_ref(self, name: str) -> bool:
            return False

        def has_user_ref(self, principal: str, name: str) -> bool:
            return False

        def has_ref(self, principal: str, name: str) -> bool:
            return False

        def server_generation(self, name: str) -> str | None:
            return None

        def user_generation(self, principal: str, name: str) -> str | None:
            return None

    app.state.plugin_snapshot_factory = StaticPluginSnapshotFactory(
        build_plugin_snapshot(
            policy=app.state.web_plugin_policy,
            catalog=app.state.catalog_service,
            profiles=app.state.operator_profile_registry,
            principal_scope="local:alice",
            secret_inventory=_EmptyInventory(),
            generation_key=b"freeform-planner-failure-policy-key",
        )
    )
    if pending_llm_review:
        # The real Web profile root is required for a profile-authored LLM
        # candidate; the legacy unrestricted root has no profile registry.
        composer = ComposerServiceImpl(
            app.state.catalog_service,
            settings,
            sessions_service=sessions,
            session_engine=engine,
            plugin_snapshot_factory=app.state.plugin_snapshot_factory.for_user_id,
            operator_profile_registry=app.state.operator_profile_registry,
        )
        app.state.composer_service = composer
        app.state.interpretation_surfacing = composer._interpretation_surfacing
    app.state.composer_progress_registry = ComposerProgressRegistry()
    app.include_router(create_session_router())
    install_composer_async_worker(app)

    # A truly unhandled route exception must surface as a 500 response (not be
    # re-raised into the test) so the pre-fix regression asserts on the wrong
    # STATUS rather than crashing — the point of the deterministic safe-response
    # criterion. Post-fix the route raises HTTPException, which FastAPI turns
    # into the expected safe status either way.
    client = SyncASGITestClient(app, raise_server_exceptions=False)
    return client, engine, sessions


def _disposition_rows(engine: Any) -> list[Any]:
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.tool_calls, chat_messages_table.c.content)).all()
    return [
        row for row in rows if row.role == "audit" and row.tool_calls and row.tool_calls[0].get("_kind") == "planner_failure_disposition"
    ]


def _llm_audit_rows(engine: Any) -> list[Any]:
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.tool_calls)).all()
    return [row for row in rows if row.role == "audit" and row.tool_calls and row.tool_calls[0].get("_kind") == "llm_call_audit"]


def _assert_no_sentinel_leak(
    engine: Any,
    response_text: str,
    *,
    expected_withheld_replies: int,
    expected_provider_failures: int = 0,
    expected_failure_class: str = "APIError",
    stored_rows: list[dict[str, Any]] | None = None,
) -> None:
    assert _PROVIDER_LEAK_SENTINEL not in response_text
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table)).mappings().all()
        operation = conn.execute(select(composer_async_operations_table)).mappings().one() if expected_provider_failures else None
    # Derive correlation from the physical-call records and persisted fence,
    # independently of the private envelopes being checked below.
    failure_status, failure_text = {
        "APIError": ("api_error", f"litellm.APIError: provider unavailable {_PROVIDER_LEAK_SENTINEL}"),
        "TimeoutError": ("timeout", _PROVIDER_LEAK_SENTINEL),
    }[expected_failure_class]
    failed_calls = [
        row
        for row in rows
        if row["role"] == "audit"
        and row["tool_calls"]
        and row["tool_calls"][0].get("_kind") == "llm_call_audit"
        and row["tool_calls"][0]["call"]["status"] == failure_status
    ]
    call_ids = {row["tool_calls"][0]["call"]["call_id"] for row in failed_calls}
    assert len(failed_calls) == len(call_ids) == expected_provider_failures
    if operation is not None:
        assert operation["status"] == "failed"
        assert all(row["session_id"] == operation["session_id"] and row["writer_principal"] == "compose_loop" for row in failed_calls)
    withheld_count = 0
    private_call_ids: set[str] = set()
    private_request_call_ids: set[str] = set()
    calls_by_id = {row["tool_calls"][0]["call"]["call_id"]: row["tool_calls"][0]["call"] for row in failed_calls}
    for row in stored_rows if stored_rows is not None else rows:
        fields = deepcopy(dict(row))
        calls = fields["tool_calls"]
        if calls and calls[0].get("_kind") == "composer_withheld_reply":
            assert fields["role"] == "audit"
            assert fields["content"] == _WITHHELD_PROSE
            assert calls == [
                {
                    "_kind": "composer_withheld_reply",
                    "schema": "composer.withheld-reply.v1",
                    "origin": "planner_prose_unadmitted",
                    "content_hash": hashlib.sha256(_WITHHELD_PROSE.encode()).hexdigest(),
                }
            ]
            withheld_count += 1
            # Only this precisely verified content is intentionally retained.
            # Check every other field without SQLAlchemy Row repr truncation.
            del fields["content"]
        if expected_provider_failures and calls and calls[0].get("_kind") == "composer_diagnostic_evidence":
            assert len(calls) == 1
            assert fields["role"] == "audit"
            assert fields["writer_principal"] == "compose_loop"
            if calls[0]["evidence"]["status"] == "operation_failure":
                assert operation is not None
                assert fields["content"] == "Private credential-filtered operation failure facts"
                assert fields["session_id"] == operation["session_id"]
                evidence = ComposerDiagnosticEvidence.from_envelope(
                    calls[0], expected_session_id=operation["session_id"], expected_operation_id=operation["session_operation_id"]
                )
                correlation = "operation:" + operation["operation_id"]
                assert evidence.tool_name == "composer_failure" and evidence.tool_call_id == correlation
                assert evidence.evidence_id == diagnostic_evidence_identity("operation-failure", correlation)
                assert evidence.operation_epoch == operation["session_operation_epoch"]
                assert evidence.arguments.content is not None and evidence.result.content is not None
                assert json.loads(evidence.arguments.content) == {
                    "durable_operation_id": operation["operation_id"],
                    "request_id": operation["request_id"],
                    "diagnostic_id": json.loads(operation["result_json"])["diagnostic_id"],
                    "failure_code": operation["failure_code"],
                    "correlation_kind": "internal_operation",
                }
                facts = json.loads(evidence.result.content)
                assert facts["record_type"] == "failure_facts"
                assert len(evidence.result.content.encode("utf-8")) <= 8192
                assert any(
                    node["class"] == expected_failure_class and {"disposition": "retained", "text": failure_text} in node["arguments"]
                    for node in facts["nodes"]
                )
                del calls[0]["evidence"]["result"]["content"]
                assert _PROVIDER_LEAK_SENTINEL not in json.dumps(fields, default=str)
                continue
            assert fields["content"] == "Private credential-filtered diagnostic facts"
            assert all(fields[key] is None for key in ("raw_content", "tool_call_id", "composition_state_id", "parent_assistant_id"))
            assert operation is not None
            assert fields["session_id"] == operation["session_id"]
            evidence = ComposerDiagnosticEvidence.from_envelope(
                calls[0], expected_session_id=operation["session_id"], expected_operation_id=operation["session_operation_id"]
            )
            assert evidence.operation_epoch == operation["session_operation_epoch"]
            if evidence.status == "provider_request":
                assert evidence.tool_name == "composer_provider_context"
                assert evidence.evidence_id == diagnostic_evidence_identity("provider-request", evidence.tool_call_id)
                assert evidence.tool_call_id in call_ids
                assert evidence.tool_call_id not in private_request_call_ids
                private_request_call_ids.add(evidence.tool_call_id)
                assert evidence.authority_arguments is None
                assert evidence.arguments.disposition == "retained"
                assert evidence.arguments.reasons == ()
                assert evidence.arguments.content is not None
                request = json.loads(evidence.arguments.content)
                assert set(request) == {"schema_version", "capture_boundary", "messages", "tools"}
                assert request["schema_version"] == "composer.provider-context.v1"
                assert request["capture_boundary"] == "prepared_litellm_messages_and_tools"
                call = calls_by_id[evidence.tool_call_id]
                assert stable_hash(request["messages"]) == call["messages_hash"]
                assert stable_hash(request["tools"]) == call["tools_spec_hash"]
                assert evidence.result.disposition == "not_returned"
                assert evidence.result.content is None
                assert evidence.result.reasons == ()
                assert evidence.wire_arguments.disposition == "unavailable"
                assert evidence.wire_arguments.content is None
                assert evidence.wire_arguments.reasons == ("not_recorded",)
                # Request bytes receive no sentinel exemption. The provider
                # exception is not appended to messages by the retry loop.
            else:
                assert evidence.tool_name == "composer_failure"
                assert evidence.status == "provider_failure"
                assert evidence.evidence_id == diagnostic_evidence_identity("provider-failure", evidence.tool_call_id)
                assert evidence.tool_call_id in call_ids
                assert evidence.tool_call_id not in private_call_ids
                private_call_ids.add(evidence.tool_call_id)
                assert evidence.authority_arguments is None
                for payload in (evidence.arguments, evidence.wire_arguments):
                    assert payload.disposition == "unavailable"
                    assert payload.content is None
                    assert payload.reasons == ("not_recorded",)
                assert evidence.result.disposition == "retained"
                assert evidence.result.reasons == ()
                assert evidence.result.content is not None
                assert json.loads(evidence.result.content) == {
                    "record_type": "failure_facts",
                    "termination": "complete",
                    "termination_reasons": [],
                    "omitted_edges": 0,
                    "trimmed_arguments": 0,
                    "edges": [],
                    "nodes": [
                        {
                            "id": 0,
                            "relation": "exception",
                            "class": expected_failure_class,
                            "suppressed_context": False,
                            "arguments": [{"disposition": "retained", "text": failure_text}],
                            "omitted_arguments": 0,
                        }
                    ],
                }
                # Retain the nonsensitive exception facts only in this exact private
                # receipt. Hashes and every sibling/metadata field remain scanned.
                del calls[0]["evidence"]["result"]["content"]
        assert _PROVIDER_LEAK_SENTINEL not in json.dumps(fields, default=str)
    assert withheld_count == expected_withheld_replies
    assert private_call_ids == call_ids
    assert private_request_call_ids == call_ids


def _assert_withheld_prose_is_not_visible_or_replayed(client: SyncASGITestClient, sessions: SessionServiceImpl, session_id: str) -> None:
    from elspeth.web.sessions.routes._helpers import _composer_chat_history

    visible = client.get(f"/api/sessions/{session_id}/messages")
    assert visible.status_code == 200, visible.text
    assert _PROVIDER_LEAK_SENTINEL not in visible.text
    all_views = client.get(
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_raw_content=true&include_llm_audit=true&include_rejection_reasons=true"
    )
    assert all_views.status_code == 200, all_views.text
    assert _PROVIDER_LEAK_SENTINEL not in all_views.text
    stored = asyncio.run(sessions.get_messages(UUID(session_id)))
    assert _PROVIDER_LEAK_SENTINEL not in json.dumps(_composer_chat_history(stored))


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _registered_recipe_request(*, envelope_insertion: str) -> str:
    preamble = (
        "Please create a pipeline that processes the following customer rows. "
        "Each row should be processed two ways in parallel and combined into "
        "a single merged output row at outputs/merged.jsonl: path A keeps the "
        "original row unchanged, path B truncates the description field to 30 "
        "characters with suffix '...'. Combine both branches under separate "
        "keys `path_a` and `path_b` in each merged output row -- one input row "
        "produces one output row containing both branches side-by-side."
    )
    return (
        f"{preamble} {envelope_insertion} Customer rows (CSV):\n"
        "name,description\n"
        "alice,this is a moderately long description\n"
        "bob,short note"
    )


@pytest.mark.parametrize(
    "message",
    [
        "Do not build or run a pipeline",
        "Do anything but build the pipeline.",
        "Build no pipeline.",
        "Stop. Build nothing.",
        "Can you explain? Build nothing.",
        "Tell me: build or run?",
        "Do not: build a pipeline.",
        "Never: run the pipeline.",
        "Explain this pseudocode: BUILD pipeline",
        "Here is an example:\n build a pipeline",
        "If needed, build the pipeline.",
        "Tell me: then build the pipeline.",
        "Do not. Build the pipeline.",
        "Can you explain? Build the pipeline.",
        "Run the unit tests.",
        "Execute this shell command.",
        "Process my refund.",
        "Route my support ticket.",
        "Save this document.",
        "Wire the payment.",
        "Split the bill.",
        "Build the documentation.",
        "Build documentation for the pipeline.",
        "Build: no pipeline.",
        "Build the pipeline; without changing anything.",
        "Build or run the pipeline?",
        "Build the pipeline or run it.",
        "Build the pipeline if needed.",
        "Save this file.",
        "Process this data.",
        "Create a CSV.",
        "Do not then build the pipeline.",
        "Don't then build the pipeline.",
        "Never then run the pipeline.",
        "Do anything but then build the pipeline.",
        "Build neither a pipeline nor a workflow.",
        "Create neither a pipeline nor a workflow.",
        "Build none of the pipeline.",
        "Create zero pipeline.",
        "Build anything but a pipeline.",
        "Build anything except a pipeline.",
        "Build anything other than a pipeline.",
        "Build anything besides a pipeline.",
        "Build anything rather than a pipeline.",
        "Build the pipeline. Actually, do not.",
        "Build the pipeline. Actually, don't.",
        "Build! Never mind.",
        "Build the pipeline. Cancel that.",
        "Build the pipeline. Scratch that.",
        "Build the pipeline. Forget it.",
        "Build the pipeline. \"Actually, don't.",
        "Set this up for the meeting.",
        "Set it up for payroll.",
        "Set this up?",
        "Save this document about the pipeline.",
        "Execute this shell script for the pipeline.",
        "Route this bug report to the pipeline team.",
        "Process this request using pipeline terminology.",
        "Build the pipeline?",
        "Run the pipeline?",
        "Build the pipeline. Undo that.",
        "Build the pipeline. Revert that.",
        "Build the pipeline. Abort.",
        "Build the pipeline. Belay that.",
        "Build the pipeline. Hold off.",
        "Build the pipeline. Ignore that.",
        "Build the pipeline. Withdraw that request.",
        "Build the pipeline. I take that back.",
        "Build the pipeline. I changed my mind.",
        "Build the pipeline. On second thought, leave it.",
        "Build the pipeline. Actually, skip it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Please do not do that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Please don\u2019t do that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Please do not proceed.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I do not want that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Stop.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel it.",
        "Read the CSV documentation and explain how transforms work.",
        "Read customers.csv and explain how transforms work.",
        "Read customers.csv and explain the password policy.",
        "Read customers.csv and explain: write is a pipeline operation.",
        "Read customers.csv and summarize the data, then write a summary in this chat.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. It should not be built.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I do not want you to build it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel the request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel my request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Forget the build.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Disregard that request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I no longer want it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. This is only an example, not a request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Instead, explain what it would do.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not make any changes.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Don't change anything.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not process that request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not save anything.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not update it.",
        "Avoid then build the pipeline.",
        "Refrain, then build the pipeline.",
        "Document then build the pipeline.",
        "Say then build the pipeline.",
        "Mention then build the pipeline.",
        _registered_recipe_request(envelope_insertion="Actually, cancel that."),
        _registered_recipe_request(envelope_insertion="Do not build this pipeline."),
        _registered_recipe_request(envelope_insertion="I changed my mind."),
        _registered_recipe_request(envelope_insertion="Undo that request."),
        'Please "do not" build the pipeline.',
        "Please 'do not' build the pipeline.",
        "Please \u201cdo not\u201d build the pipeline.",
        "Please \u2018do not\u2019 build the pipeline.",
        '"Never" build the pipeline.',
        'Build "no" pipeline.',
        'Can you "not" build the pipeline?',
    ],
)
def test_non_authorizing_request_cannot_enter_planner_or_auto_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    """The HTTP route must keep non-authorizing requests conversational."""
    # Fixed expectations for the five measured information-only controls.
    # This table selects test expectations; the offline SDK never routes content.
    single_transition_requests = {
        "Read the CSV documentation and explain how transforms work.",
        "Read customers.csv and explain how transforms work.",
        "Read customers.csv and explain the password policy.",
        "Read customers.csv and explain: write is a pipeline operation.",
        "Read customers.csv and summarize the data, then write a summary in this chat.",
    }
    expected_primary_calls = 1 if message in single_transition_requests else 2
    completion = AsyncMock(spec=litellm.acompletion, side_effect=_conversational_completion())
    reconciliation_completion = AsyncMock(spec=litellm.acompletion, side_effect=_conversational_completion())
    requests = []
    primary_requests = []
    advisor_requests = []

    async def physical_completion(**kwargs):
        request = deepcopy({"model": kwargs["model"], "messages": kwargs["messages"], "tools": kwargs.get("tools")})
        requests.append(request)
        if kwargs["model"] != "test/planner":
            assert kwargs["model"] == composer._settings.composer_advisor_model
            advisor_requests.append(request)
            assert len(advisor_requests) == 1, "An unbudgeted additional END transition must not occur"
            return _Response(
                choices=[
                    _Choice(
                        message=_Message(
                            content=json.dumps(
                                {
                                    "verdict": "CLEAN",
                                    "category": "other",
                                    "steps": [],
                                    "findings": "The reply reports no pipeline changes and makes no completion claim.",
                                    "note": None,
                                    "publication_verdict": "ADMIT",
                                }
                            )
                        )
                    )
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            )
        primary_requests.append(request)
        assert len(primary_requests) <= expected_primary_calls, "An unbudgeted primary transition must not occur"
        if len(primary_requests) == 1:
            return await completion(**kwargs)
        if len(primary_requests) == 2:
            return await reconciliation_completion(**kwargs)
        raise AssertionError("An unbudgeted third provider transition must not occur")

    physical_sdk = AsyncMock(spec=litellm.acompletion, side_effect=physical_completion)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, physical_sdk)
    session_id = client.post("/api/sessions", json={"title": "negated build"}).json()["id"]
    composer = client.app.state.composer_service
    ordinary_loop = AsyncMock(
        spec=composer._compose_loop,
        wraps=composer._compose_loop,
    )
    monkeypatch.setattr(composer, "_compose_loop", ordinary_loop)

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": message, "operation_id": str(uuid4()), "state_id": None},
    )

    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    ordinary_loop.assert_awaited_once()
    completion.assert_awaited_once()
    assert len(primary_requests) == expected_primary_calls
    assert len(advisor_requests) == 1
    assert physical_sdk.await_count == len(requests) == expected_primary_calls + 1
    assert requests == [*primary_requests, *advisor_requests]
    assert advisor_requests[0]["tools"] is None
    assert requests[0]["messages"][-3:-1] == [
        {"role": "user", "content": message},
        {
            "role": "system",
            "content": json.dumps(
                {
                    "turn_start_state_id": None,
                    "turn_start_version": 0,
                    "current_state_id": None,
                    "current_version": 0,
                    "malformed_head": False,
                    "parked_graphs": [],
                },
                sort_keys=True,
            ),
        },
    ]
    assert requests[0]["messages"][-1]["role"] == "system"
    assert json.loads(requests[0]["messages"][-1]["content"]) == {
        "retained_operation_history": {
            "availability": "available",
            "has_more": False,
            "outcomes": [],
            "scope": "Bounded retained failures for this authenticated session and actor; not complete lifetime history.",
            "interpretation": (
                "Saved revision counts describe operation-local persistence, not requested edits or request satisfaction. "
                "Unknown attribution is not zero progress. No retained failure does not establish prior success. "
                "These outcomes do not describe the current session head or this request's effects."
            ),
        }
    }
    if expected_primary_calls == 2:
        reconciliation_completion.assert_awaited_once()
        assert requests[1]["model"] == requests[0]["model"]
        assert requests[1]["tools"] == requests[0]["tools"]
        assert requests[1]["messages"][:-2] == requests[0]["messages"]
        assert requests[1]["messages"][-2] == {
            "role": "assistant",
            "content": "No pipeline changes were requested.",
        }
        assert requests[1]["messages"][-1] == {
            "role": "user",
            "content": (
                "[composer-system] No tool has run this turn, and the pipeline has no source or nodes. "
                "Re-check the user's request against that state. If the user requested construction or generated "
                "source data, carry out that authorized work using the declared tools before claiming it is complete. "
                "If a required product fact is missing, start your no-tool reply with CLARIFY: and ask the concrete question. "
                "If the user asked only for "
                "explanation or revoked construction, answer that request without building. Do not describe data "
                "as saved, bound, or reviewed until tool results establish it."
            ),
        }
    else:
        reconciliation_completion.assert_not_awaited()
    audit_rows = _llm_audit_rows(engine)
    assert len(audit_rows) == expected_primary_calls + 1
    actual_calls = [row.tool_calls[0]["call"] for row in audit_rows]
    assert len({call["call_id"] for call in actual_calls}) == expected_primary_calls + 1
    assert {call["messages_hash"] for call in actual_calls} == {stable_hash(request["messages"]) for request in requests}
    for request in requests:
        matching = [call for call in actual_calls if call["messages_hash"] == stable_hash(request["messages"])]
        assert len(matching) == 1
        assert matching[0]["tools_spec_hash"] == (stable_hash(request["tools"]) if request["tools"] is not None else None)
        assert matching[0]["model_requested"] == request["model"]
        assert matching[0]["status"] == "success"
    assert asyncio.run(sessions.list_composition_proposals(UUID(session_id))) == []
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_proposals_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0


@pytest.mark.parametrize("message", _COMPLETE_MULTI_CLAUSE_REQUESTS)
def test_complete_multi_clause_request_enters_empty_pipeline_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    client, engine, _sessions = _build_app(tmp_path, monkeypatch, _timeout_completion())
    session_id = client.post("/api/sessions", json={"title": "multi-clause build"}).json()["id"]
    composer = client.app.state.composer_service
    ordinary_loop = AsyncMock(
        spec=composer._compose_loop,
        wraps=composer._compose_loop,
    )
    monkeypatch.setattr(composer, "_compose_loop", ordinary_loop)

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": message, "operation_id": str(uuid4()), "state_id": None},
    )

    status, body = settled.error()
    assert status == 504, settled.final.text
    ordinary_loop.assert_not_awaited()
    assert body["detail"]["failure_code"] == "provider_timeout"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_proposals_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0


@pytest.mark.parametrize(
    (
        "completion_factory",
        "expected_status",
        "expected_failure_code",
        "expected_planner_code",
        "expected_llm_audit_rows",
        "expected_withheld_replies",
    ),
    [
        # The prose (no-tool-call) reply is nudge-retried on its own bounded
        # budget, the overrun engages the escape hatch, and the advisor's
        # malformed reply spends it — so every nudged attempt AND the hatch
        # call land as durable audit evidence alongside the terminal
        # MALFORMED_RESPONSE.
        (_malformed_completion, 502, "invalid_provider_response", "MALFORMED_RESPONSE", _PROSE_NUDGE_BUDGET + 2, 3),
        (_timeout_completion, 504, "provider_timeout", "TIMEOUT", 1, 0),
        # LiteLLM API errors are the declared retryable provider failure, so
        # every configured physical attempt must be present in the audit.
        (_provider_error_completion, 503, "provider_unavailable", "PROVIDER_ERROR", 3, 0),
        (_cost_unavailable_completion, 503, "cost_unavailable", "COST_UNAVAILABLE", 1, 0),
    ],
    ids=["malformed", "timeout", "provider_error", "cost_unavailable"],
)
def test_send_message_freeform_planner_failure_is_translated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion_factory: Any,
    expected_status: int,
    expected_failure_code: str,
    expected_planner_code: str,
    expected_llm_audit_rows: int,
    expected_withheld_replies: int,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, engine, sessions = _build_app(tmp_path, monkeypatch, completion_factory())
    session_id = client.post("/api/sessions", json={"title": "freeform planner failure"}).json()["id"]

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": _EMPTY_INTENT, "operation_id": str(uuid4()), "state_id": None},
    )

    # (a) deliberate safe response, not an unhandled 500.
    status, body = settled.error()
    assert status == expected_status, settled.final.text
    assert body["detail"]["error_type"] == "composer_planner_failure"
    assert body["detail"]["failure_code"] == expected_failure_code
    # The raw planner code reaches the CLIENT, not just the durable audit row below.
    # `failure_code` buckets eleven codes into `invalid_provider_response`, so without
    # this a caller cannot tell a product terminal (the planner reached a verdict) from
    # a substrate fault (the process died) — they are the same 502 (elspeth-ad5628ecda).
    assert body["detail"]["planner_code"] == expected_planner_code

    # (b) a "failed" progress event is emitted.
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress["phase"] == "failed"
    assert progress["reason"] is not None
    if expected_failure_code == "cost_unavailable":
        assert progress["reason"] == "service_setup_failed"
        assert "pricing" in progress["likely_next"]
        assert "administrator" in body["detail"]["detail"]

    # (c) exactly one durable closed failure-disposition record.
    disposition_rows = _disposition_rows(engine)
    assert len(disposition_rows) == 1
    assert disposition_rows[0].tool_calls[0]["failure_code"] == expected_failure_code
    assert disposition_rows[0].tool_calls[0]["surface"] == "freeform"
    # The raw planner code is forensically durable — the closed failure_code
    # buckets many codes and is useless for root-causing a live 5xx.
    assert disposition_rows[0].tool_calls[0]["planner_code"] == expected_planner_code

    # The already-durable planner LLM-call audit evidence still lands (and is
    # NOT re-persisted as a duplicate) alongside the disposition record.
    assert len(_llm_audit_rows(engine)) == expected_llm_audit_rows

    # (d) refused prose and nonsensitive failure facts retain dedicated private custody.
    expected_provider_failures = expected_llm_audit_rows if expected_planner_code in {"PROVIDER_ERROR", "TIMEOUT"} else 0
    expected_failure_class = "TimeoutError" if expected_planner_code == "TIMEOUT" else "APIError"
    _assert_no_sentinel_leak(
        engine,
        settled.final.text,
        expected_withheld_replies=expected_withheld_replies,
        expected_provider_failures=expected_provider_failures,
        expected_failure_class=expected_failure_class,
    )
    _assert_withheld_prose_is_not_visible_or_replayed(client, sessions, session_id)
    captured = capsys.readouterr()
    assert _PROVIDER_LEAK_SENTINEL not in captured.out + captured.err + caplog.text

    if expected_provider_failures:
        with engine.connect() as conn:
            stored_rows = [dict(row) for row in conn.execute(select(chat_messages_table)).mappings()]
        # Each receipt family independently rejects corrupted metadata,
        # bindings, payloads, omissions and duplication.
        for receipt_status in ("provider_failure", "provider_request"):
            private_index = next(
                i
                for i, row in enumerate(stored_rows)
                if row["tool_calls"]
                and row["tool_calls"][0].get("_kind") == "composer_diagnostic_evidence"
                and row["tool_calls"][0]["evidence"]["status"] == receipt_status
            )
            for mutation in (
                "summary",
                "raw_content",
                "writer",
                "mixed",
                "hash",
                "operation",
                "epoch",
                "arguments",
                "missing",
                "duplicate",
            ):
                damaged = deepcopy(stored_rows)
                row = damaged[private_index]
                envelope = row["tool_calls"][0]
                if mutation == "summary":
                    row["content"] += _PROVIDER_LEAK_SENTINEL
                elif mutation == "raw_content":
                    row["raw_content"] = _PROVIDER_LEAK_SENTINEL
                elif mutation == "writer":
                    row["writer_principal"] = "route_user_message"
                elif mutation == "mixed":
                    row["tool_calls"].append({"_kind": "audit"})
                elif mutation == "hash":
                    envelope["evidence_sha256"] = "0" * 64
                elif mutation in {"operation", "epoch", "arguments"}:
                    # Recompute the envelope hash so the binding/payload assertion,
                    # rather than only the integrity hash, must reject the record.
                    if mutation == "operation":
                        envelope["operation_id"] = envelope["evidence"]["operation_id"] = str(uuid4())
                    elif mutation == "epoch":
                        envelope["operation_epoch"] += 1
                        envelope["evidence"]["operation_epoch"] = envelope["operation_epoch"]
                    else:
                        envelope["evidence"]["arguments"] = deepcopy(envelope["evidence"]["result"])
                    envelope["evidence_sha256"] = hashlib.sha256(canonical_json(envelope["evidence"]).encode()).hexdigest()
                elif mutation == "missing":
                    damaged.pop(private_index)
                else:
                    damaged.append(deepcopy(row))
                with pytest.raises((AssertionError, AuditIntegrityError)):
                    _assert_no_sentinel_leak(
                        engine,
                        settled.final.text,
                        expected_withheld_replies=expected_withheld_replies,
                        expected_provider_failures=expected_provider_failures,
                        expected_failure_class=expected_failure_class,
                        stored_rows=damaged,
                    )

    # Negative control: a marker leaked to an ordinary row must still fail,
    # even when the same database contains valid withheld-reply records.
    asyncio.run(
        sessions.add_message(
            UUID(session_id),
            "user",
            _PROVIDER_LEAK_SENTINEL,
            writer_principal="route_user_message",
        )
    )
    with pytest.raises(AssertionError):
        _assert_no_sentinel_leak(
            engine,
            settled.final.text,
            expected_withheld_replies=expected_withheld_replies,
            expected_provider_failures=expected_provider_failures,
            expected_failure_class=expected_failure_class,
        )


def test_recompose_freeform_planner_failure_is_translated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, engine, sessions = _build_app(tmp_path, monkeypatch, _malformed_completion())
    session_id = client.post("/api/sessions", json={"title": "recompose planner failure"}).json()["id"]

    # Recompose requires the transcript to end at a user turn; seed one directly.
    from uuid import UUID

    user_message = asyncio.run(
        sessions.add_message(
            UUID(session_id),
            "user",
            _EMPTY_INTENT,
            writer_principal="route_user_message",
        )
    )

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/recompose",
        body={"expected_user_message_id": str(user_message.id), "operation_id": str(uuid4()), "state_id": None},
    )

    status, body = settled.error()
    assert status == 502, settled.final.text
    assert body["detail"]["error_type"] == "composer_planner_failure"
    assert body["detail"]["failure_code"] == "invalid_provider_response"

    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress["phase"] == "failed"

    disposition_rows = _disposition_rows(engine)
    assert len(disposition_rows) == 1
    assert disposition_rows[0].tool_calls[0]["failure_code"] == "invalid_provider_response"

    _assert_no_sentinel_leak(engine, settled.final.text, expected_withheld_replies=3)
    _assert_withheld_prose_is_not_visible_or_replayed(client, sessions, session_id)
    captured = capsys.readouterr()
    assert _PROVIDER_LEAK_SENTINEL not in captured.out + captured.err + caplog.text


# Every ``code=`` value raised by PipelinePlannerError in pipeline_planner.py.
# Keeping this explicit (rather than deriving) makes a newly-added planner code
# fail loudly here until both surfaces classify it.
_ALL_PLANNER_CODES = (
    "COMPLETION_TOKENS_EXCEEDED",
    "COMPOSITION_EXHAUSTED",
    "COST_CAP_EXCEEDED",
    "COST_UNAVAILABLE",
    "DECLINED",
    "DISCOVERY_CYCLE",
    "DISCOVERY_EXHAUSTED",
    "DISCOVERY_ONLY",
    "MALFORMED_RESPONSE",
    "PROVIDER_CALLS_EXHAUSTED",
    "PROVIDER_ERROR",
    "REPAIR_EXHAUSTED",
    "REQUEST_BYTES_EXHAUSTED",
    "RESPONSE_TRUNCATED",
    "TIMEOUT",
    "TOOL_CALLS_EXHAUSTED",
    "VALIDATION_FAILED",
)


def test_freeform_progress_reason_stops_blaming_the_provider_for_planner_faults() -> None:
    """The freeform failed-progress event hardcoded ``provider_unavailable`` for EVERY planner code.

    ``ComposerProgressReason`` already carries the codes this needs — ``planner_repair_exhausted``
    exists verbatim to distinguish planner faults from provider faults. A discovery-budget exhaustion, a tool-call
    cap, and a genuine provider outage were one indistinguishable reason on this surface. That is what
    ``/composer-progress`` reports, which is the terminal a client reads when its request is cut
    (elspeth-ad5628ecda).
    """
    from elspeth.web.sessions.routes._helpers import freeform_planner_progress_reason

    planner_owned = {
        "REPAIR_EXHAUSTED": "planner_repair_exhausted",
        "COMPOSITION_EXHAUSTED": "convergence_composition_budget",
        "DISCOVERY_EXHAUSTED": "convergence_discovery_budget",
        "TOOL_CALLS_EXHAUSTED": "tool_call_cap_exceeded",
        "TIMEOUT": "convergence_wall_clock_timeout",
    }
    for code, expected in planner_owned.items():
        assert freeform_planner_progress_reason(code) == expected, code

    # A genuine upstream fault still names the provider — the fix is attribution, not avoidance.
    assert freeform_planner_progress_reason("PROVIDER_ERROR") == "provider_unavailable"
    assert freeform_planner_progress_reason("MALFORMED_RESPONSE") == "provider_unavailable"

    # Total over the live corpus: every code resolves to a member of the closed vocabulary.
    from typing import get_args

    from elspeth.contracts.composer_progress import ComposerProgressReason

    allowed = set(get_args(ComposerProgressReason.__value__))
    for code in _ALL_PLANNER_CODES:
        assert freeform_planner_progress_reason(code) in allowed, code


# The detail-code shapes a rejection can carry. ``policy_blocked`` is keyed on
# THIS axis, not on the planner code, so test the full cross product.
_DETAIL_CODE_CASES: tuple[tuple[str, ...], ...] = (
    # Non-rejection failure (timeout, provider error): no codes at all.
    (),
    # Ordinary wiring rejection — must NOT read as a policy refusal.
    ("interpretation_review_contract_unsatisfied",),
    ("plugin_options_invalid", "schema_contract_violation"),
    # Categorical deployment-policy refusals, from each of the two producing
    # seams (the authoritative source gate and the plugin snapshot).
    ("aws_s3_source_not_allowed",),
    ("plugin_not_allowed_on_web",),
    # A policy refusal alongside ordinary wiring noise: the policy code wins,
    # because no repair to the wiring can clear it.
    ("plugin_options_invalid", "aws_s3_source_not_allowed"),
)


@pytest.mark.parametrize("code", _ALL_PLANNER_CODES)
@pytest.mark.parametrize("detail_codes", _DETAIL_CODE_CASES)
def test_policy_detail_codes_decide_policy_blocked(code: str, detail_codes: tuple[str, ...]) -> None:
    """A categorical policy refusal is permanent under EVERY planner code.

    The observed failure surfaced as ``VALIDATION_FAILED`` (the server-derived
    pass-through gate), but the same refusal reaches ``REPAIR_EXHAUSTED`` when the
    model burns its repair budget re-authoring the prohibited component — so the
    classification cannot be keyed on the planner code. Conversely an ordinary
    wiring rejection must NOT be promoted to a policy refusal.
    """
    from elspeth.web.composer.pipeline_planner import PipelinePlannerError
    from elspeth.web.sessions.routes._helpers import PLANNER_POLICY_DETAIL_CODES, _freeform_planner_failure_code

    exc = PipelinePlannerError("planner failure", code=code, detail_codes=detail_codes)
    expected_policy = any(detail in PLANNER_POLICY_DETAIL_CODES for detail in detail_codes)

    assert (_freeform_planner_failure_code(exc) == "policy_blocked") is expected_policy


def test_every_reachable_failure_code_has_an_http_envelope() -> None:
    """Every planner classification must have a safe HTTP envelope."""
    from elspeth.web.composer.pipeline_planner import PipelinePlannerError
    from elspeth.web.sessions.routes._helpers import _FREEFORM_PLANNER_FAILURE_HTTP, _freeform_planner_failure_code

    reachable: set[str] = set()
    for code in _ALL_PLANNER_CODES:
        for detail_codes in _DETAIL_CODE_CASES:
            exc = PipelinePlannerError("planner failure", code=code, detail_codes=detail_codes)
            reachable.add(_freeform_planner_failure_code(exc))

    assert "policy_blocked" in reachable
    assert reachable <= set(_FREEFORM_PLANNER_FAILURE_HTTP)


def test_freeform_policy_blocked_copy_blames_neither_provider_nor_operation_id() -> None:
    """The freeform copy is policy-aware too, not a provider-fault retread."""
    from elspeth.web.sessions.routes._helpers import _FREEFORM_PLANNER_FAILURE_HTTP

    status, copy = _FREEFORM_PLANNER_FAILURE_HTTP["policy_blocked"]
    lowered = copy.lower()

    assert status == 422
    assert "provider" not in lowered
    assert "operation id" not in lowered
    assert "composer model" not in lowered
    assert "deployment policy" in lowered
    # Freeform chat has no component highlight.
    # pins the blocked component — so this copy must not say "highlighted".
    assert "highlighted" not in lowered


_DECLINE_TEXT = "I must decline: this request needs a streaming-join capability no available plugin provides."


def _decline_after_exhaustion_completion() -> Any:
    """Primary planner over-explores to discovery exhaustion; the escape-hatch
    advisor turn answers in text — the honest decline."""
    counter = {"calls": 0}
    discovery = ("list_sources", "list_sinks", "list_transforms")

    async def completion(**kwargs: Any) -> _Response:
        if kwargs["model"] != "test/planner":
            return _Response(
                choices=[_Choice(message=_Message(content=_DECLINE_TEXT, tool_calls=[]))],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            )
        name = discovery[counter["calls"] % len(discovery)]
        counter["calls"] += 1
        return _Response(
            choices=[
                _Choice(
                    message=_Message(
                        content=None,
                        tool_calls=[_ToolCall(id=f"call-{counter['calls']}", function=_Function(name=name, arguments="{}"))],
                    )
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    return completion


def test_send_message_freeform_planner_decline_is_a_normal_assistant_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An escape-hatch decline is a successful conversational outcome, not a
    provider failure: 200 with the advisor's own words, no failure disposition,
    no 'unusable pipeline plan' misattribution."""
    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END

    request = "Build a pipeline that joins two unbounded streams by matching keys."
    planner_completion = _decline_after_exhaustion_completion()
    planner_requests: list[dict[str, Any]] = []
    end_requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> _Response:
        model = kwargs["model"]
        if "response_format" in kwargs:
            assert model == composer._settings.composer_advisor_model
            end_requests.append(deepcopy(kwargs))
            assert len(end_requests) == 1, "Only one END SDK response is supplied"
            assert [item["model"] for item in planner_requests] == ["test/planner", "test/planner", model]
            review_json = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(review_json.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            assert review["current_request"] == request
            assert review["candidate_reply"] == _DECLINE_TEXT
            assert review["saved_effects"]["saved_revision_count"] == 0
            assert review["saved_effects"]["net_changed"] is False
            return _Response(
                choices=[
                    _Choice(
                        message=_Message(
                            content=json.dumps(
                                {
                                    "verdict": "CLEAN",
                                    "category": "other",
                                    "steps": [],
                                    "findings": "",
                                    "note": None,
                                    "publication_verdict": "ADMIT",
                                }
                            )
                        )
                    )
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                model=model,
                id="decline-end-review",
            )
        planner_requests.append(deepcopy(kwargs))
        assert len(planner_requests) <= 3, "Only the original two discovery turns and hatch are supplied"
        assert model == ("test/planner" if len(planner_requests) <= 2 else composer._settings.composer_advisor_model)
        return await planner_completion(**kwargs)

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, completion)
    composer = client.app.state.composer_service
    session_id = client.post("/api/sessions", json={"title": "freeform planner decline"}).json()["id"]

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": request, "operation_id": str(uuid4()), "state_id": None},
    )

    assert settled.final.status_code == 200, settled.final.text
    body = settled.result()
    assert len(planner_requests) == 3 and len(end_requests) == 1
    assert body["message"]["content"] == _DECLINE_TEXT
    assert _DECLINE_TEXT in settled.final.text
    assert "unusable pipeline plan" not in settled.final.text

    # Not a failure: no disposition row, and progress is not "failed".
    assert _disposition_rows(engine) == []
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress.get("phase") != "failed"

    # Original discovery retry/hatch semantics remain; publication adds one
    # separately accounted physical END call with an actual accepted audit.
    assert len(_llm_audit_rows(engine)) == 4

    # The decline lands in the visible conversation as an assistant message.
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.content, chat_messages_table.c.tool_calls)).all()
    assistant_rows = [row for row in rows if row.role == "assistant" and _DECLINE_TEXT in (row.content or "")]
    assert len(assistant_rows) == 1
    passes = [
        row.tool_calls[0]["pass"]
        for row in rows
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert len(passes) == 1
    assert passes[0]["phase"] == "end" and passes[0]["source"] == "model"
    assert passes[0]["verdict"] == "clean" and passes[0]["publication_verdict"] == "ADMIT"
    assert passes[0]["provider_attempts"] == 1 and passes[0]["first_attempt_accepted"] is True
    assert type(passes[0]["publication_binding"]) is str and len(passes[0]["publication_binding"]) == 64


def _marker_decline_completion() -> Any:
    """The planner declines on its first, decline-eligible ordinary turn."""

    async def completion(**_kwargs: Any) -> _Response:
        return _Response(
            choices=[_Choice(message=_Message(content=f"DECLINE: {_DECLINE_TEXT}", tool_calls=[]))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    return completion


def _observe_publication_reviews(monkeypatch: pytest.MonkeyPatch):
    """Observe real derived bindings without substituting publication permission."""
    from elspeth.web.composer.turn_effects import AdvisorPublicationReview, TurnEvidence

    original = TurnEvidence.review_candidate
    reviews: list[AdvisorPublicationReview] = []

    def observe(self: TurnEvidence, **kwargs: Any) -> AdvisorPublicationReview:
        review = original(self, **kwargs)
        reviews.append(review)
        return review

    monkeypatch.setattr(TurnEvidence, "review_candidate", observe)
    return reviews


@pytest.mark.parametrize("publication_verdict", ["ADMIT", "WITHHOLD"])
def test_send_message_ordinary_turn_marker_decline_is_a_normal_assistant_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_verdict: str,
) -> None:
    """One planner decision publishes only after exact-candidate END admission."""
    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END
    from elspeth.web.sessions.routes._helpers import _composer_chat_history
    from elspeth.web.validation import _redact_sensitive_content

    reviews = _observe_publication_reviews(monkeypatch)
    planner_completion = _marker_decline_completion()
    primary_requests: list[dict[str, Any]] = []
    end_requests: list[dict[str, Any]] = []
    reviewed_payloads = []

    async def completion(**kwargs: Any) -> _Response:
        if "response_format" in kwargs:
            end_requests.append(deepcopy(kwargs))
            assert len(primary_requests) == len(end_requests) == 1, "No additional SDK dispatch is supplied"
            assert kwargs["model"] == client.app.state.composer_service._settings.composer_advisor_model
            assert kwargs.get("tools") is None
            summary = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(summary.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            assert review["current_request"] == _EMPTY_INTENT and review["candidate_reply"] == _DECLINE_TEXT
            assert review["saved_effects"]["saved_revision_count"] == 0
            assert review["saved_effects"]["net_changed"] is False
            reviewed_payloads.append(review)
            return _Response(
                choices=[
                    _Choice(
                        message=_Message(
                            content=json.dumps(
                                {
                                    "verdict": "CLEAN",
                                    "category": "other",
                                    "steps": [],
                                    "findings": "The supplied evidence does not support publishing this capability denial."
                                    if publication_verdict == "WITHHOLD"
                                    else "",
                                    "note": None,
                                    "publication_verdict": publication_verdict,
                                }
                            )
                        )
                    )
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                model=kwargs["model"],
                id="marker-decline-end",
            )
        primary_requests.append(deepcopy(kwargs))
        assert len(primary_requests) == 1 and not end_requests, "Only the original planner decision is supplied"
        assert kwargs["model"] == "test/planner"
        return await planner_completion(**kwargs)

    physical_sdk = AsyncMock(spec=litellm.acompletion, side_effect=completion)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, physical_sdk)
    session_id = client.post("/api/sessions", json={"title": "ordinary turn decline"}).json()["id"]

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": _EMPTY_INTENT, "operation_id": str(uuid4()), "state_id": None},
    )

    assert settled.final.status_code == 200, settled.final.text
    body = settled.result()
    assert physical_sdk.await_count == 2
    assert len(primary_requests) == len(end_requests) == 1
    if publication_verdict == "ADMIT":
        assert body["message"]["content"] == _DECLINE_TEXT
    else:
        assert _DECLINE_TEXT not in settled.final.text
    assert "DECLINE:" not in body["message"]["content"]
    assert body["state"] is None

    # Not a failure: no disposition row, and progress is not "failed".
    assert _disposition_rows(engine) == []
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress.get("phase") != "failed"

    # The planner decision remains one call; publication has its own END audit.
    assert len(_llm_audit_rows(engine)) == 2

    # The decline lands in the visible conversation as an assistant message,
    # marker stripped, body verbatim.
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.content, chat_messages_table.c.tool_calls)).all()
    assistant_rows = [row for row in rows if row.role == "assistant" and (row.content or "") == _DECLINE_TEXT]
    assert len(assistant_rows) == (1 if publication_verdict == "ADMIT" else 0)
    visible = client.get(f"/api/sessions/{session_id}/messages")
    assert visible.status_code == 200, visible.text
    replay = json.dumps(_composer_chat_history(asyncio.run(sessions.get_messages(UUID(session_id)))))
    if publication_verdict == "WITHHOLD":
        assert _DECLINE_TEXT not in visible.text and _DECLINE_TEXT not in replay
        withheld = [
            row
            for row in rows
            if row.role == "audit"
            and row.tool_calls
            and row.tool_calls[0]["_kind"] == "composer_withheld_reply"
            and row.tool_calls[0]["origin"] == "advisor_publication_withheld"
        ]
        assert len(withheld) == 1 and withheld[0].content == _DECLINE_TEXT
        assert withheld[0].tool_calls[0]["content_hash"] == hashlib.sha256(_DECLINE_TEXT.encode()).hexdigest()
    else:
        assert _DECLINE_TEXT in visible.text and _DECLINE_TEXT in replay
    passes = [
        row.tool_calls[0]["pass"]
        for row in rows
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert len(passes) == 1
    assert passes[0]["phase"] == "end" and passes[0]["source"] == "model"
    assert passes[0]["verdict"] == "clean" and passes[0]["publication_verdict"] == publication_verdict
    assert passes[0]["provider_attempts"] == 1 and passes[0]["first_attempt_accepted"] is True
    assert passes[0]["publication_binding"] in {
        review.binding for review in reviews if json.loads(_redact_sensitive_content(review.payload_json)) == reviewed_payloads[0]
    }


def _valid_pipeline_completion(tmp_path: Path, session_id_holder: dict[str, str]) -> Any:
    """One-shot planner completion emitting a committable csv→json pipeline."""
    import json as _json

    async def completion(**_kwargs: Any) -> _Response:
        session_id = session_id_holder["id"]
        pipeline = {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "path": str(tmp_path / "blobs" / session_id / "input.csv"),
                    "schema": {"mode": "flexible", "fields": ["name: str"]},
                },
                "on_validation_failure": "discard",
            },
            "nodes": [],
            "edges": [],
            "outputs": [
                {
                    "sink_name": "rows",
                    "plugin": "json",
                    "options": {
                        "path": str(tmp_path / "outputs" / session_id / "result.json"),
                        "schema": {"mode": "observed"},
                        "format": "json",
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
        }
        return _Response(
            choices=[
                _Choice(
                    message=_Message(
                        content=None,
                        tool_calls=[
                            _ToolCall(
                                id="call-1",
                                function=_Function(name="emit_pipeline_proposal", arguments=_json.dumps({"pipeline": pipeline})),
                            )
                        ],
                    )
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    return completion


def test_later_explicit_imperative_reaches_planner_and_auto_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later explicit command must outrank an earlier informational clause."""
    (tmp_path / "outputs").mkdir(exist_ok=True)
    session_id_holder: dict[str, str] = {}
    client, engine, _sessions = _build_app(
        tmp_path,
        monkeypatch,
        _valid_pipeline_completion(tmp_path, session_id_holder),
    )
    session_id = client.post("/api/sessions", json={"title": "later explicit build"}).json()["id"]
    session_id_holder["id"] = session_id

    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": "How does this work? Now build it.", "operation_id": str(uuid4()), "state_id": None},
    )

    assert settled.final.status_code == 200, settled.final.text
    body = settled.result()
    assert body["state"] is not None
    assert body["proposals"] == []
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_proposals_table)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1


@pytest.mark.parametrize("base_kind", ["absent", "present"])
def test_freeform_auto_commit_surfaces_interpretation_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_kind: str,
) -> None:
    """Planner-authored LLM requirements must have durable resolvable events.

    Historical intent: the first planner-authored LLM pipeline in session
    ff368dcb committed pending requirements without resolvable events. Keep
    this registration as the regression. Current admission deliberately stages
    pending-review candidates. This normal Web request uses the registered
    compose-loop set_pipeline dialect: an actual explicit-approval preference
    change and subsequent HTTP acceptance exercise atomic publication/evidence.
    The separate later-imperative test retains the green auto-commit witness.
    """
    import socket

    from elspeth.web.composer.advisor_audit import (
        ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
        ADVISOR_GRAPH_REVIEW_AUDIT_KIND,
        AdvisorGraphReviewFact,
    )
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END
    from elspeth.web.composer.pipeline_proposal import composition_content_hash
    from elspeth.web.interpretation_state import approved_prompt_artifact_hash_from_options, interpretation_sites
    from elspeth.web.sessions.models import (
        composer_async_operations_table,
        interpretation_events_table,
        proposal_events_table,
        quota_provider_attempts_table,
        token_usage_ledger_table,
    )
    from elspeth.web.sessions.pending_interpretation import _interpretation_hash_domain_v2
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.validation import _redact_sensitive_content

    # This test scripts only the physical planner SDK boundary. No provider
    # bootstrap, credential discovery, socket connect, or row execution is
    # authorized. A surprising network/runtime-provider path fails the test.
    def denied_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("offline interpretation fixture attempted a socket connection")

    def denied_runtime_completion(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("authoring fixture unexpectedly executed the row provider")

    monkeypatch.setattr(socket.socket, "connect", denied_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", denied_connection)
    monkeypatch.setattr(litellm, "completion", denied_runtime_completion)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    (tmp_path / "outputs").mkdir(exist_ok=True)
    session_id_holder: dict[str, str] = {}
    baseline_completion = _valid_pipeline_completion(tmp_path, session_id_holder)
    physical_requests: list[dict[str, Any]] = []
    reviews = _observe_publication_reviews(monkeypatch)
    end_payloads = []
    primary_count = 3 if base_kind == "absent" else 2
    candidate_reply = (
        "The pipeline proposal awaits your approval; no pipeline state has been committed."
        if base_kind == "absent"
        else "The proposed pipeline is waiting for your approval."
    )

    async def pending_completion(**kwargs: Any) -> _Response:
        physical_requests.append(deepcopy(kwargs))
        ordinal = len(physical_requests)
        if "response_format" in kwargs:
            assert ordinal == primary_count + 1 and not end_payloads, "Exactly one END follows the original primary calls"
            assert kwargs.get("tools") is None
            assert kwargs["model"] == client.app.state.composer_service._settings.composer_advisor_model
            summary = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(summary.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            assert review["current_request"] == "Build a CSV to JSONL pipeline with an LLM description of each name."
            assert review["candidate_reply"] == candidate_reply
            assert review["saved_effects"]["saved_revision_count"] == 0 and review["saved_effects"]["net_changed"] is False
            end_payloads.append(review)
            return _Response(
                choices=[
                    _Choice(
                        message=_Message(
                            content=json.dumps(
                                {
                                    "verdict": "FLAGGED",
                                    "category": "other",
                                    "steps": [],
                                    "findings": "The proposed change is pending approval; this turn has not committed pipeline changes.",
                                    "note": None,
                                    "publication_verdict": "ADMIT",
                                }
                            )
                        )
                    )
                ],
                usage={"prompt_tokens": 16, "completion_tokens": 8, "total_tokens": 24, "cost": 0.04},
                model=kwargs["model"],
                id="pending-proposal-end-review",
            )
        assert ordinal <= primary_count, "only the authored mutation, post-tool reply, and applicable empty-graph canary are allowed"
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        assert "set_pipeline" in names
        assert "emit_pipeline_proposal" not in names
        (declaration,) = [tool["function"] for tool in kwargs["tools"] if tool["function"]["name"] == "set_pipeline"]
        parameters = declaration["parameters"]
        assert parameters["type"] == "object"
        assert set(parameters["properties"]) == {"pipeline"}
        assert parameters["required"] == ["pipeline"]
        assert parameters["additionalProperties"] is False
        assert parameters["properties"]["pipeline"]["type"] == "object"
        if ordinal == 3:
            assert base_kind == "absent", "A populated base does not take the empty-graph canary"
            assert kwargs["model"] == physical_requests[1]["model"]
            assert kwargs["tools"] == physical_requests[1]["tools"]
            assert kwargs["messages"][:-2] == physical_requests[1]["messages"]
            assert kwargs["messages"][-2] == {
                "role": "assistant",
                "content": "The proposed pipeline is waiting for your approval.",
            }
            assert kwargs["messages"][-1] == {
                "role": "user",
                "content": (
                    "[composer-system] No composition-state mutation completed successfully this turn, "
                    "and the pipeline has no source or nodes. Re-check the user's request against that state. "
                    "If the user requested construction or generated source data, carry out that authorized work "
                    "using the declared tools before claiming it is complete. If a required product fact is missing, "
                    "start your no-tool reply with CLARIFY: and ask the concrete question. "
                    "If the user asked only for explanation or revoked construction, "
                    "answer that request without building. Do not describe data as saved, bound, or reviewed "
                    "until tool results establish it."
                ),
            }
            return _Response(
                choices=[
                    _Choice(message=_Message(content="The pipeline proposal awaits your approval; no pipeline state has been committed."))
                ],
                usage={"prompt_tokens": 14, "completion_tokens": 7, "total_tokens": 21, "cost": 0.03},
                model="provider/canary-reply-v1",
                id="canary-request-3",
            )
        if ordinal == 2:
            assert kwargs["model"] == physical_requests[0]["model"]
            assert kwargs["tools"] == physical_requests[0]["tools"]
            assert kwargs["messages"][:-2] == physical_requests[0]["messages"]
            authored = kwargs["messages"][-2]
            assert authored["role"] == "assistant"
            (tool_call,) = authored["tool_calls"]
            assert tool_call["id"] == "call-1"
            assert tool_call["function"]["name"] == "set_pipeline"
            feedback = kwargs["messages"][-1]
            assert feedback["role"] == "tool"
            assert feedback["tool_call_id"] == "call-1"
            tool_result = json.loads(feedback["content"])
            assert "error" not in tool_result, tool_result
            assert tool_result["success"] is True
            assert tool_result["data"]["status"] == "APPROVAL_REQUIRED"
            assert tool_result["data"]["tool_name"] == "set_pipeline"
            return _Response(
                choices=[_Choice(message=_Message(content="The proposed pipeline is waiting for your approval."))],
                usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18, "cost": 0.02},
                model="provider/post-tool-v1",
                id="post-tool-request-2",
            )
        assert ordinal == 1
        response = await baseline_completion(**kwargs)
        function = response.choices[0].message.tool_calls[0].function
        pipeline = json.loads(function.arguments)["pipeline"]
        pipeline["source"]["on_success"] = "input_rows"
        pipeline["nodes"] = [
            {
                "id": "interpret_text",
                "node_type": "transform",
                "plugin": "llm",
                "input": "input_rows",
                "on_success": "rows",
                "on_error": "discard",
                "options": {
                    "profile": "offline-review",
                    "system_prompt": "Describe the supplied name. Reply with one short factual description.",
                    "prompt_template": "Describe {{ row.name }} using Use a concise factual tone.",
                    "prompt_template_parts": [
                        {"kind": "text", "text": "Describe {{ row.name }} using "},
                        {"kind": "interpretation_ref", "requirement_id": "tone:interpret_text"},
                    ],
                    "required_input_fields": ["name"],
                    "response_field": "summary",
                    "schema": {"mode": "observed", "guaranteed_fields": ["summary"]},
                    "interpretation_requirements": [
                        {
                            "kind": "vague_term",
                            "user_term": "tone",
                            "draft": "Use a concise factual tone.",
                        }
                    ],
                },
            }
        ]
        # The physical Web dialect wraps the canonical object in the
        # declared pipeline field; the wire decoder unwraps it before dispatch.
        # The provider authors the object; backend validation remains unchanged.
        function.name = "set_pipeline"
        function.arguments = json.dumps({"pipeline": pipeline})
        return response

    physical_sdk = AsyncMock(spec=litellm.acompletion, side_effect=pending_completion)
    client, engine, _sessions = _build_app(
        tmp_path,
        monkeypatch,
        physical_sdk,
        pending_llm_review=True,
    )
    surfacing = client.app.state.composer_service._interpretation_surfacing
    original_prepare = surfacing.prepare_pending_interpretation_reviews
    prepared_calls = []

    def record_preparation(state: CompositionState):
        drafts = original_prepare(state)
        prepared_calls.append((state, drafts))
        return drafts

    monkeypatch.setattr(surfacing, "prepare_pending_interpretation_reviews", record_preparation)
    session_id = client.post("/api/sessions", json={"title": "auto-commit surfacer"}).json()["id"]
    session_id_holder["id"] = session_id
    preferences = client.get(f"/api/sessions/{session_id}/composer/preferences")
    assert preferences.status_code == 200, preferences.text
    assert preferences.json()["trust_mode"] == "auto_commit"
    changed_preferences = client.patch(
        f"/api/sessions/{session_id}/composer/preferences",
        json={"trust_mode": "explicit_approve", "density_default": preferences.json()["density_default"]},
    )
    assert changed_preferences.status_code == 200, changed_preferences.text
    assert changed_preferences.json()["session_id"] == session_id
    assert changed_preferences.json()["trust_mode"] == "explicit_approve"
    assert changed_preferences.json()["density_default"] == preferences.json()["density_default"]
    source_dir = tmp_path / "blobs" / session_id
    source_dir.mkdir(parents=True)
    (source_dir / "input.csv").write_text("name\nAlice\n", encoding="utf-8")
    base_record = None
    if base_kind == "present":
        base_record = asyncio.run(
            _sessions.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources={
                        "source": {
                            "plugin": "csv",
                            "on_success": "rows",
                            "on_validation_failure": "discard",
                            "options": {"path": str(source_dir / "input.csv"), "schema": {"mode": "flexible", "fields": ["name: str"]}},
                        }
                    },
                    nodes=[],
                    edges=[],
                    outputs=[
                        {
                            "name": "rows",
                            "sink_name": "rows",
                            "plugin": "json",
                            "on_write_failure": "discard",
                            "options": {
                                "path": str(tmp_path / "outputs" / session_id / "before.json"),
                                "schema": {"mode": "observed"},
                                "format": "json",
                                "mode": "write",
                                "collision_policy": "auto_increment",
                            },
                        }
                    ],
                    metadata_={"name": "Before proposal", "description": ""},
                    is_valid=True,
                ),
                provenance="session_seed",
            )
        )
    with engine.connect() as conn:
        immutable_before = conn.execute(select(composition_states_table)).mappings().all()
    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body={
            "content": "Build a CSV to JSONL pipeline with an LLM description of each name.",
            "operation_id": str(uuid4()),
            "state_id": str(base_record.id) if base_record is not None else None,
        },
    )
    assert settled.final.status_code == 200, settled.final.text
    result = settled.result()
    assert result["message"]["content"].startswith(candidate_reply)
    assert "Composer completion is withheld." in result["message"]["content"], "Prose admission must not clear the pending graph"
    assert len(end_payloads) == 1
    # A committed private review fact blocks completion without manufacturing
    # a graph checkpoint that immediately invalidates the proposal's AbsentBase.
    if base_record is None:
        assert result["state"] is None
    else:
        assert result["state"]["id"] == str(base_record.id)
        assert result["state"]["version"] == base_record.version
        assert result["state"]["composer_meta"]["completion_gates"]["advisor_signoff"]["status"] == "blocked"
    with engine.connect() as conn:
        assert conn.execute(select(composition_states_table)).mappings().all() == immutable_before
        rows = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.role == "audit")).all()
        (envelope,) = [
            row.tool_calls[0] for row in rows if row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_GRAPH_REVIEW_AUDIT_KIND
        ]
        review_fact = AdvisorGraphReviewFact.from_envelope(envelope)
        assert review_fact.state_id == (base_record.id if base_record is not None else None)
        assert review_fact.session_id == UUID(session_id)
        assert review_fact.terminal_operation_id == settled.accepted.json()["operation_id"]
        assert review_fact.action == "blocked" and review_fact.identity.source == "model"
        assert review_fact.gates.advisor_signoff is not None
        assert review_fact.gates.advisor_signoff.cause.value == "graph_rejected"
        assert _sessions._graph_review_on_connection(conn, session_id=UUID(session_id), stored=base_record) == review_fact
    before_acceptance = client.get(f"/api/sessions/{session_id}/state")
    assert before_acceptance.status_code == 200, before_acceptance.text
    if base_record is None:
        assert before_acceptance.json() is None
    else:
        assert before_acceptance.json()["id"] == str(base_record.id)
        assert before_acceptance.json()["composer_meta"]["completion_gates"]["advisor_signoff"]["status"] == "blocked"
        explicit = asyncio.run(_sessions.get_state(base_record.id))
        assert explicit.review_projection is not None and explicit.review_projection.stored == base_record
    visible_history = client.get(f"/api/sessions/{session_id}/messages")
    assert visible_history.status_code == 200, visible_history.text
    assert result["message"]["content"] in [item["content"] for item in visible_history.json()]
    (proposal,) = result["proposals"]
    assert proposal["status"] == "pending"
    assert proposal["tool_name"] == "set_pipeline"
    with engine.connect() as conn:
        proposal_row = (
            conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal["id"])).mappings().one()
        )
    assert proposal_row["base_state_id"] == (str(base_record.id) if base_record is not None else None)
    assert prepared_calls == [], "atomic cohort preparation belongs to publication"
    accepted = client.post(
        f"/api/sessions/{session_id}/proposals/{proposal['id']}/accept",
        json={"draft_hash": proposal["pipeline_metadata"]["draft_hash"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "committed"
    ((prepared_state, drafts),) = prepared_calls
    sites = interpretation_sites(prepared_state)
    assert ("interpret_text", "vague_term", "tone") in {(site.component_id, site.kind.value, site.user_term) for site in sites}
    assert len(sites) == len(drafts) > 0
    assert {(site.component_id, site.kind.value, site.user_term) for site in sites} == {
        (draft.affected_node_id, draft.kind.value, draft.user_term) for draft in drafts
    }
    current = client.get(f"/api/sessions/{session_id}/state")
    assert current.status_code == 200, current.text
    state = current.json()
    assert accepted.json()["committed_state_id"] == state["id"]
    assert state["composer_meta"]["completion_gates"]["advisor_signoff"]["status"] == "blocked"
    assert state["composer_meta"]["completion_gates"]["advisor_signoff"]["for_graph"] == review_fact.identity.for_graph
    composition = CompositionState.from_dict({key: state[key] for key in ("sources", "nodes", "edges", "outputs", "metadata", "version")})
    from elspeth.web.execution.completion_gates import ADVISOR_SIGNOFF_PENDING_DETAIL, merge_completion_gates, parse_completion_gates
    from tests.unit.web.sessions.test_completion_gate_roundtrip import _green_result

    readiness = merge_completion_gates(_green_result(), parse_completion_gates(state["composer_meta"]), composition).readiness
    assert not readiness.completion_ready
    assert any(blocker.detail == ADVISOR_SIGNOFF_PENDING_DETAIL for blocker in readiness.blockers)
    assert composition_content_hash(composition) == composition_content_hash(prepared_state)
    pending = client.get(f"/api/sessions/{session_id}/interpretations?status=pending")
    assert pending.status_code == 200, pending.text
    events = pending.json()["events"]
    assert {event["id"] for event in events} == {str(draft.event_id) for draft in drafts}
    assert all(event["composition_state_id"] == state["id"] and event["choice"] == "pending" for event in events)
    with engine.connect() as conn:
        actual_rows = conn.execute(select(interpretation_events_table)).mappings().all()
        accepted_payload = conn.execute(
            select(proposal_events_table.c.payload).where(
                proposal_events_table.c.proposal_id == proposal["id"],
                proposal_events_table.c.event_type == "proposal.accepted",
            )
        ).scalar_one()
    assert {row["id"] for row in actual_rows} == {str(draft.event_id) for draft in drafts}
    cohort = accepted_payload["review_cohort"]
    assert {member["event_id"] for member in cohort} == {event["id"] for event in events}
    assert len(cohort) == len(drafts)
    assert accepted_payload["committed_state_content_hash"] == composition_content_hash(composition)
    assert accepted_payload["final_state_content_hash"] == composition_content_hash(composition)
    by_event = {event["id"]: event for event in events}
    for ordinal, (member, draft) in enumerate(zip(cohort, drafts, strict=True)):
        event = by_event[member["event_id"]]
        material = member["semantic_material"]
        assert member["ordinal"] == material["ordinal"] == ordinal
        assert member["candidate_state_id"] == material["candidate_state_id"] == state["id"]
        assert member["event_id"] == str(draft.event_id)
        assert member["tool_call_id"] == event["tool_call_id"] == draft.tool_call_id
        assert material["affected_node_id"] == event["affected_node_id"] == draft.affected_node_id
        assert material["kind"] == event["kind"] == draft.kind.value
        assert material["user_term"] == event["user_term"] == draft.user_term
        assert material["llm_draft"] == event["llm_draft"] == draft.llm_draft
        assert material["surface_origin"] == event["surface_origin"] == draft.surface_origin.value
        for field_name in ("model_identifier", "model_version", "provider", "composer_skill_hash"):
            assert material[field_name] == event[field_name]
        assert member["semantic_hash"] == stable_hash({"schema": "composer.pipeline-review-semantic.v1", "material": material})
        assert member["initial_disposition"] == "pending"
        assert member["initial_resolution_hash"] == stable_hash(member["initial_resolution"])
    (tone_event,) = [event for event in events if event["kind"] == "vague_term" and event["user_term"] == "tone"]
    resolved = client.post(
        f"/api/sessions/{session_id}/interpretations/{tone_event['id']}/resolve",
        json={"choice": "amended", "amended_value": "Use a precise formal tone."},
    )
    assert resolved.status_code == 200, resolved.text
    resolution = resolved.json()
    event = resolution["event"]
    assert event["id"] == tone_event["id"]
    assert event["composition_state_id"] == state["id"]
    assert event["choice"] == "amended"
    assert event["llm_draft"] == tone_event["llm_draft"] == "Use a concise factual tone."
    assert event["accepted_value"] == "Use a precise formal tone."
    assert event["hash_domain_version"] == "v2"
    assert event["arguments_hash"] == stable_hash(
        _interpretation_hash_domain_v2(
            **{
                key: event[key]
                for key in (
                    "session_id",
                    "composition_state_id",
                    "affected_node_id",
                    "tool_call_id",
                    "user_term",
                    "kind",
                    "llm_draft",
                    "accepted_value",
                    "actor",
                    "model_identifier",
                    "model_version",
                    "provider",
                    "composer_skill_hash",
                )
            },
            context="offline planner interpretation regression",
        )
    )
    (resolved_node,) = resolution["new_state"]["nodes"]
    assert "{{interpretation:tone}}" not in resolved_node["options"]["prompt_template"]
    assert "Use a precise formal tone." in resolved_node["options"]["prompt_template"]
    assert resolved_node["options"]["prompt_template"] != prepared_state.nodes[0].options["prompt_template"]
    expected_artifact_hash = approved_prompt_artifact_hash_from_options(resolved_node["options"])
    assert expected_artifact_hash is not None
    assert event["approved_prompt_artifact_hash"] == resolved_node["options"]["approved_prompt_artifact_hash"] == expected_artifact_hash
    (resolved_requirement,) = [
        requirement
        for requirement in resolved_node["options"]["interpretation_requirements"]
        if requirement["kind"] == "vague_term" and requirement["user_term"] == "tone"
    ]
    assert resolved_requirement["status"] == "resolved"
    assert resolved_requirement["event_id"] == event["id"]
    assert resolved_requirement["accepted_value"] == event["accepted_value"]
    assert resolved_requirement["resolved_prompt_template_hash"] == stable_hash(event["accepted_value"])
    reloaded = client.get(f"/api/sessions/{session_id}/interpretations")
    assert reloaded.status_code == 200, reloaded.text
    assert next(item for item in reloaded.json()["events"] if item["id"] == event["id"]) == event
    resolved_current = client.get(f"/api/sessions/{session_id}/state")
    assert resolved_current.status_code == 200, resolved_current.text
    assert resolved_current.json()["id"] == resolution["new_state"]["id"]
    assert composition_content_hash(
        CompositionState.from_dict(
            {key: resolved_current.json()[key] for key in ("sources", "nodes", "edges", "outputs", "metadata", "version")}
        )
    ) == composition_content_hash(
        CompositionState.from_dict(
            {key: resolution["new_state"][key] for key in ("sources", "nodes", "edges", "outputs", "metadata", "version")}
        )
    )
    with engine.connect() as conn:
        resolved_row = (
            conn.execute(
                select(interpretation_events_table).where(
                    interpretation_events_table.c.id == event["id"],
                )
            )
            .mappings()
            .one()
        )
    assert resolved_row["arguments_hash"] == event["arguments_hash"]
    assert resolved_row["approved_prompt_artifact_hash"] == expected_artifact_hash
    assert physical_sdk.await_count == len(physical_requests) == primary_count + 1
    audits = _llm_audit_rows(engine)
    assert len(audits) == primary_count + 1
    calls = [audit.tool_calls[0]["call"] for audit in audits]
    assert len({call["call_id"] for call in calls}) == primary_count + 1
    assert {call["messages_hash"] for call in calls} == {stable_hash(request["messages"]) for request in physical_requests}
    assert {call["provider_request_id"] for call in calls} == {
        "planner-request-1",
        "post-tool-request-2",
        "pending-proposal-end-review",
    } | ({"canary-request-3"} if base_kind == "absent" else set())
    assert {call["model_returned"] for call in calls} == {
        "provider/planner-v1",
        "provider/post-tool-v1",
        client.app.state.composer_service._settings.composer_advisor_model,
    } | ({"provider/canary-reply-v1"} if base_kind == "absent" else set())
    for physical in physical_requests:
        (call,) = [call for call in calls if call["messages_hash"] == stable_hash(physical["messages"])]
        assert call["status"] == "success"
        assert call["model_requested"] == physical["model"]
        assert call["tools_spec_hash"] == (stable_hash(physical["tools"]) if physical.get("tools") is not None else None)
    with engine.connect() as conn:
        audit_rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.tool_calls)).all()
    passes = [
        row.tool_calls[0]["pass"]
        for row in audit_rows
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert len(passes) == 1
    assert passes[0]["phase"] == "end" and passes[0]["source"] == "model"
    assert passes[0]["verdict"] == "flagged" and passes[0]["publication_verdict"] == "ADMIT"
    assert passes[0]["provider_attempts"] == 1 and passes[0]["first_attempt_accepted"] is True
    assert passes[0]["publication_binding"] in {
        review.binding for review in reviews if json.loads(_redact_sensitive_content(review.payload_json)) == end_payloads[0]
    }
    with engine.connect() as conn:
        operation = (
            conn.execute(
                select(composer_async_operations_table).where(
                    composer_async_operations_table.c.session_id == session_id,
                    composer_async_operations_table.c.operation_id == settled.accepted.json()["operation_id"],
                )
            )
            .mappings()
            .one()
        )
        attempts = (
            conn.execute(
                select(quota_provider_attempts_table).where(
                    quota_provider_attempts_table.c.session_id == session_id,
                )
            )
            .mappings()
            .all()
        )
        ledger = (
            conn.execute(
                select(token_usage_ledger_table).where(
                    token_usage_ledger_table.c.session_id == session_id,
                )
            )
            .mappings()
            .all()
        )
    assert operation["status"] == "completed"
    assert len(attempts) == len(ledger) == primary_count + 1
    assert {attempt["attempt_id"] for attempt in attempts} == {call["call_id"] for call in calls}
    assert len({attempt["ledger_entry_id"] for attempt in attempts}) == primary_count + 1
    assert {attempt["ledger_entry_id"] for attempt in attempts} == {entry["entry_id"] for entry in ledger}
    for attempt in attempts:
        assert attempt["settled_at"] is not None
        assert attempt["identity_id"] == operation["actor_user_id"] == "alice"
        assert attempt["source"] == "composer"
        assert attempt["operation_id"] == operation["session_operation_id"]
        assert attempt["operation_epoch"] == operation["session_operation_epoch"]
        assert attempt["lease_token"] == operation["session_operation_lease_token"]
        (entry,) = [entry for entry in ledger if entry["entry_id"] == attempt["ledger_entry_id"]]
        (call,) = [call for call in calls if call["call_id"] == attempt["attempt_id"]]
        assert entry["identity_id"] == "alice"
        assert entry["source"] == "composer"
        assert entry["model"] == call["model_returned"]
        assert entry["prompt_tokens"] == call["prompt_tokens"]
        assert entry["completion_tokens"] == call["completion_tokens"]
