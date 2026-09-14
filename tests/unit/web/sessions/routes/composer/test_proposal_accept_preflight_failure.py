"""Accept answers a named, non-conflict error when settle-time runtime preflight raises.

``_state_data_from_composer_state(preflight_exception_policy="raise")`` turns
an internal preflight failure (a timeout, an unexpected validator exception)
into ``ComposerRuntimePreflightError``. Unmapped, that is a bare ``text/plain``
500. It must not be a 409 either: the SPA's ``acceptProposal`` routes every
409 through ``isHttpConflict`` into ``staleProposalIds`` ("the state changed,
rebase"), which misdiagnoses a preflight failure. The proposal stays pending,
nothing commits, and the lease is closed so a retry can succeed.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.sessions.test_e2e_state_seed_route import _ready_readiness
from tests.unit.web.sessions.test_routes import (
    _async_return,
    _create_test_composition_proposal,
    _create_test_pipeline_composition_proposal,
    _make_app,
    _make_composer_mock,
)

from elspeth.contracts.hashing import stable_hash
from elspeth.web.execution.schemas import ValidationResult

_PREFLIGHT_TARGET = "elspeth.web.sessions.routes._helpers._runtime_preflight_for_state"


class _PreflightProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def raise_timeout(self, *_args: Any, **_kwargs: Any) -> ValidationResult:
        self.calls += 1
        raise TimeoutError("runtime preflight exceeded its budget")


def _assert_named_preflight_failure(response: Any) -> None:
    # Not 409: sessionStore.acceptProposal maps any 409 to a stale proposal.
    # A structured 500 matches the compose/message routes' mapping of the
    # same exception class.
    assert response.status_code == 500, response.text
    assert response.headers["content-type"].startswith("application/json")
    detail = response.json()["detail"]
    assert detail["error_type"] == "runtime_preflight_failed"
    assert "left pending" in detail["detail"]
    # The wrapped exception's message never reaches the client.
    assert "exceeded its budget" not in response.text


def _set_pipeline_arguments(input_path: Path) -> dict[str, Any]:
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": str(input_path), "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            }
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {
                    "path": "outputs/accepted.jsonl",
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "accepted-proposal"},
    }


def test_legacy_accept_maps_runtime_preflight_failure_and_leaves_proposal_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service = _make_app(tmp_path)
    app.state.session_engine = service._engine
    probe = _PreflightProbe()
    monkeypatch.setattr(_PREFLIGHT_TARGET, probe.raise_timeout)
    client = TestClient(app, raise_server_exceptions=False)
    session = client.post("/api/sessions", json={"title": "Accept preflight"}).json()
    session_id = uuid.UUID(session["id"])
    input_path = tmp_path / "blobs" / str(session_id) / "input.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("value\n1\n", encoding="utf-8")
    proposal = asyncio.run(
        _create_test_composition_proposal(
            service,
            session_id=session_id,
            tool_call_id="call_set_pipeline",
            tool_name="set_pipeline",
            summary="Replace the pipeline.",
            rationale="Requested by the current composer turn.",
            affects=("graph", "validation", "yaml"),
            arguments_json=_set_pipeline_arguments(input_path),
            arguments_redacted_json={"summary": "redacted"},
            base_state_id=None,
            actor="composer-web:user:alice",
        )
    )
    endpoint = f"/api/sessions/{session['id']}/proposals/{proposal.id}/accept"

    response = client.post(endpoint)

    assert probe.calls == 1, "the settle-time runtime preflight must actually run"
    _assert_named_preflight_failure(response)
    statuses = [row.status for row in asyncio.run(service.list_composition_proposals(session_id))]
    assert statuses == ["pending"]
    assert asyncio.run(service.get_current_state(session_id)) is None

    monkeypatch.setattr(
        _PREFLIGHT_TARGET,
        _async_return(ValidationResult(is_valid=True, checks=[], errors=[], readiness=_ready_readiness())),
    )
    retried = client.post(endpoint)

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "committed"


def test_pipeline_accept_maps_runtime_preflight_failure_and_leaves_proposal_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PlannerSurface
    from elspeth.web.composer.redaction import redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock()
    probe = _PreflightProbe()
    monkeypatch.setattr(_PREFLIGHT_TARGET, probe.raise_timeout)
    client = TestClient(app, raise_server_exceptions=False)
    session = client.post("/api/sessions", json={"title": "Pipeline accept preflight"}).json()
    session_id = uuid.UUID(session["id"])
    input_path = tmp_path / "blobs" / str(session_id) / "canonical.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("value\n1\n", encoding="utf-8")
    pipeline = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": str(input_path), "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": _set_pipeline_arguments(input_path)["outputs"],
    }
    envelope = PipelineProposal.create(
        pipeline=pipeline,
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.FREEFORM,
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )
    plan = PipelinePlanResult(
        proposal=envelope,
        tool_call_id="canonical-terminal-call",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
    )
    row = asyncio.run(
        _create_test_pipeline_composition_proposal(
            service,
            session_id=session_id,
            plan=plan,
            summary="Replace the pipeline.",
            rationale="Requested by the operator.",
            affects=("graph", "validation"),
            arguments_redacted_json=redact_tool_call_arguments("set_pipeline", pipeline, telemetry=NoopRedactionTelemetry()),
            actor="composer-web:user:alice",
            composer_model_identifier="planner-model",
            composer_model_version="planner-model-v1",
            composer_provider="provider",
        )
    )
    endpoint = f"/api/sessions/{session['id']}/proposals/{row.id}/accept"

    response = client.post(endpoint, json={"draft_hash": envelope.draft_hash})

    assert probe.calls == 1, "the settle-time runtime preflight must actually run"
    _assert_named_preflight_failure(response)
    statuses = [proposal.status for proposal in asyncio.run(service.list_composition_proposals(session_id))]
    assert statuses == ["pending"]
    assert asyncio.run(service.get_current_state(session_id)) is None

    monkeypatch.setattr(
        _PREFLIGHT_TARGET,
        _async_return(ValidationResult(is_valid=True, checks=[], errors=[], readiness=_ready_readiness())),
    )
    retried = client.post(endpoint, json={"draft_hash": envelope.draft_hash})

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "committed"
