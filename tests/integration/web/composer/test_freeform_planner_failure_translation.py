"""Freeform planner failures are translated into safe HTTP outcomes.

Regression for the still-live half of elspeth-54c11243a3: a
``PipelinePlannerError`` raised on the freeform empty-pipeline path escaped
every route catch as an unhandled 500 with no ``failed`` progress event and no
durable closed failure-disposition record. The guided-full path already
translates the same exception (``routes/composer/guided_plan.py`` +
``fail_guided_operation_with_audit``); these tests exercise the freeform
``send_message`` and ``recompose`` routes through the real FastAPI stack and
assert parity: a deliberate safe status, a ``failed`` progress snapshot, a
durable redacted disposition audit row, and no raw provider content leak.

The planner's own LLM-call audit evidence (``attach_llm_calls`` +
``_plan_and_stage_empty_pipeline``) is already durable and is deliberately NOT
re-persisted here — these tests assert it survives alongside the new
disposition record, but the disposition record is a separate row.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog
from sqlalchemy import func, select

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.pipeline_planner import _PROSE_NUDGE_BUDGET
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, composition_proposals_table, composition_states_table
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web._sync_asgi_client import SyncASGITestClient

# Distinctive token planted in every scripted provider payload / error so the
# leak assertions can prove it never reaches the HTTP body or any persisted row.
_PROVIDER_LEAK_SENTINEL = "PROVIDER-LEAK-SENTINEL-9f13c7"

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
    """A non-tool response — trips MALFORMED_RESPONSE during terminal parsing."""

    async def completion(**_kwargs: Any) -> _Response:
        return _Response(
            choices=[_Choice(message=_Message(content=f"I cannot help. {_PROVIDER_LEAK_SENTINEL}", tool_calls=[]))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    return completion


def _timeout_completion() -> Any:
    """A provider timeout — trips the planner wall-clock TIMEOUT code."""

    async def completion(**_kwargs: Any) -> _Response:
        raise TimeoutError(_PROVIDER_LEAK_SENTINEL)

    return completion


def _provider_error_completion() -> Any:
    """An opaque provider crash — trips the PROVIDER_ERROR code."""

    async def completion(**_kwargs: Any) -> _Response:
        raise RuntimeError(f"provider unavailable {_PROVIDER_LEAK_SENTINEL}")

    return completion


def _build_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: Any,
) -> tuple[SyncASGITestClient, Any, SessionServiceImpl]:
    """Wire a minimal real FastAPI app whose composer is a real ComposerServiceImpl.

    Deliberately does NOT reuse the guided conftest's ``_DeterministicGuidedPlanner``
    double (it has no ``compose`` and constructs a proposal directly); the freeform
    routes must traverse the real ``ComposerServiceImpl.compose`` →
    ``_plan_and_stage_empty_pipeline`` → ``plan_pipeline`` path so the scripted
    completion drives a genuine ``PipelinePlannerError``.
    """
    from fastapi import FastAPI

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.sqlite3'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.freeform.planner.failure"),
    )

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_boot_probe_enabled=False,
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )

    monkeypatch.setattr(
        ComposerServiceImpl,
        "_compute_availability",
        lambda _self: ComposerAvailability(available=True, provider="test", model="test/planner", reason=None),
    )
    monkeypatch.setattr("elspeth.web.composer.service._litellm_acompletion", completion)

    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(),
        settings,
        sessions_service=sessions,
        session_engine=engine,
    )

    app = FastAPI()

    async def mock_user() -> UserIdentity:
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = mock_user
    app.state.session_service = sessions
    app.state.session_engine = engine
    app.state.scoped_secret_resolver = None
    app.state.settings = settings
    app.state.composer_service = composer
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

    app.state.plugin_snapshot_factory = lambda user: build_plugin_snapshot(
        policy=app.state.web_plugin_policy,
        catalog=app.state.catalog_service,
        profiles=app.state.operator_profile_registry,
        principal_scope=f"local:{user.user_id}",
        secret_inventory=_EmptyInventory(),
        generation_key=b"freeform-planner-failure-policy-key",
    )
    app.state.composer_progress_registry = ComposerProgressRegistry()
    app.include_router(create_session_router())

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


def _assert_no_sentinel_leak(engine: Any, response_text: str) -> None:
    assert _PROVIDER_LEAK_SENTINEL not in response_text
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table)).all()
    assert not any(_PROVIDER_LEAK_SENTINEL in str(row) for row in rows)


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
        "Read the CSV documentation and explain how transforms work.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. It should not be built.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I do not want you to build it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel the request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel my request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Forget the build.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Disregard that request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I no longer want it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. This is only an example, not a request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Instead, explain what it would do.",
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
    client, engine, sessions = _build_app(tmp_path, monkeypatch, _timeout_completion())
    session_id = client.post("/api/sessions", json={"title": "negated build"}).json()["id"]
    composer = client.app.state.composer_service
    ordinary_loop = AsyncMock(
        spec=composer._compose_loop,
        return_value=ComposerResult(message="No pipeline changes were requested.", state=_empty_state()),
    )
    monkeypatch.setattr(composer, "_compose_loop", ordinary_loop)

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": message},
    )

    assert response.status_code == 200, response.text
    ordinary_loop.assert_awaited_once()
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
        return_value=ComposerResult(message="ordinary conversational response", state=_empty_state()),
    )
    monkeypatch.setattr(composer, "_compose_loop", ordinary_loop)

    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": message})

    assert response.status_code == 504, response.text
    ordinary_loop.assert_not_awaited()
    assert response.json()["detail"]["failure_code"] == "provider_timeout"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_proposals_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 0


@pytest.mark.parametrize(
    ("completion_factory", "expected_status", "expected_failure_code", "expected_planner_code", "expected_llm_audit_rows"),
    [
        # The prose (no-tool-call) reply is nudge-retried on its own bounded
        # budget before the terminal MALFORMED_RESPONSE, so every nudged
        # attempt lands as durable audit evidence alongside the terminal one.
        (_malformed_completion, 502, "invalid_provider_response", "MALFORMED_RESPONSE", _PROSE_NUDGE_BUDGET + 1),
        (_timeout_completion, 504, "provider_timeout", "TIMEOUT", 1),
        (_provider_error_completion, 503, "provider_unavailable", "PROVIDER_ERROR", 1),
    ],
    ids=["malformed", "timeout", "provider_error"],
)
def test_send_message_freeform_planner_failure_is_translated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion_factory: Any,
    expected_status: int,
    expected_failure_code: str,
    expected_planner_code: str,
    expected_llm_audit_rows: int,
) -> None:
    client, engine, _sessions = _build_app(tmp_path, monkeypatch, completion_factory())
    session_id = client.post("/api/sessions", json={"title": "freeform planner failure"}).json()["id"]

    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": _EMPTY_INTENT})

    # (a) deliberate safe response, not an unhandled 500.
    assert response.status_code == expected_status, response.text
    body = response.json()
    assert body["detail"]["error_type"] == "composer_planner_failure"
    assert body["detail"]["failure_code"] == expected_failure_code

    # (b) a "failed" progress event is emitted.
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress["phase"] == "failed"
    assert progress["reason"] is not None

    # (c) exactly one durable closed failure-disposition record, mirroring guided.
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

    # (d) no raw provider content / usage / model metadata leaks anywhere.
    _assert_no_sentinel_leak(engine, response.text)


def test_recompose_freeform_planner_failure_is_translated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, engine, sessions = _build_app(tmp_path, monkeypatch, _malformed_completion())
    session_id = client.post("/api/sessions", json={"title": "recompose planner failure"}).json()["id"]

    # Recompose requires the transcript to end at a user turn; seed one directly.
    from uuid import UUID

    asyncio.run(
        sessions.add_message(
            UUID(session_id),
            "user",
            _EMPTY_INTENT,
            writer_principal="route_user_message",
        )
    )

    response = client.post(f"/api/sessions/{session_id}/recompose")

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["detail"]["error_type"] == "composer_planner_failure"
    assert body["detail"]["failure_code"] == "invalid_provider_response"

    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress["phase"] == "failed"

    disposition_rows = _disposition_rows(engine)
    assert len(disposition_rows) == 1
    assert disposition_rows[0].tool_calls[0]["failure_code"] == "invalid_provider_response"

    _assert_no_sentinel_leak(engine, response.text)


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


@pytest.mark.parametrize("code", _ALL_PLANNER_CODES)
def test_freeform_planner_failure_code_matches_guided(code: str) -> None:
    """The freeform surface must classify every planner code exactly as guided.

    Task 0 gates Task 3's cross-surface disposition parity; a divergence here
    (e.g. one surface returning invalid_provider_response/502 where the other
    returns operation_failed/500 for the same code) is precisely the bug Task 0
    exists to prevent.
    """
    from elspeth.web.composer.pipeline_planner import PipelinePlannerError
    from elspeth.web.sessions.routes._helpers import _freeform_planner_failure_code
    from elspeth.web.sessions.routes.composer.guided_plan import _guided_full_failure_code

    exc = PipelinePlannerError("planner failure", code=code)
    assert _freeform_planner_failure_code(exc) == _guided_full_failure_code(exc)


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
    client, engine, _sessions = _build_app(tmp_path, monkeypatch, _decline_after_exhaustion_completion())
    session_id = client.post("/api/sessions", json={"title": "freeform planner decline"}).json()["id"]

    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": _EMPTY_INTENT})

    assert response.status_code == 200, response.text
    assert _DECLINE_TEXT in response.text
    assert "unusable pipeline plan" not in response.text

    # Not a failure: no disposition row, and progress is not "failed".
    assert _disposition_rows(engine) == []
    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    assert progress.get("phase") != "failed"

    # The planner's LLM audit evidence is durable: three primary discovery
    # calls plus the advisor overtime turn.
    assert len(_llm_audit_rows(engine)) == 4

    # The decline lands in the visible conversation as an assistant message.
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.content)).all()
    assistant_rows = [row for row in rows if row.role == "assistant" and _DECLINE_TEXT in (row.content or "")]
    assert len(assistant_rows) == 1


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

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "How does this work? Now build it."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] is not None
    assert body["proposals"] == []
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_proposals_table)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1


def test_freeform_auto_commit_surfaces_interpretation_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipeline-proposal settlement must run the interpretation-review
    surfacer against the committed state. The planner path mints proposals
    without the compose loop's request_interpretation_review dispatch, so
    skipping the surfacer leaves committed states whose pending
    interpretation_requirements have NO resolvable event row — the run gate
    then 422s (interpretation_placeholder_unresolved) with nothing the user
    can resolve (live: first planner-authored llm pipeline, session ff368dcb).
    """
    from unittest.mock import AsyncMock

    (tmp_path / "outputs").mkdir(exist_ok=True)
    session_id_holder: dict[str, str] = {}
    client, _engine, _sessions = _build_app(
        tmp_path,
        monkeypatch,
        _valid_pipeline_completion(tmp_path, session_id_holder),
    )
    composer = client.app.state.composer_service
    spy = AsyncMock(wraps=composer.surface_pending_interpretation_reviews)
    monkeypatch.setattr(composer, "surface_pending_interpretation_reviews", spy)

    session_id = client.post("/api/sessions", json={"title": "auto-commit surfacer"}).json()["id"]
    session_id_holder["id"] = session_id
    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": _EMPTY_INTENT})

    assert response.status_code == 200, response.text
    assert "prepared and validated" in response.text
    assert spy.await_count == 1, "settlement must surface interpretation reviews for the committed state"
    kwargs = spy.await_args.kwargs
    assert kwargs["session_id"] == session_id
    assert kwargs["current_state_id"] is not None
