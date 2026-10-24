from __future__ import annotations

import asyncio
import json
import sys
import threading
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.web.composer import service as composer_module
from elspeth.web.composer import tool_batch as batch_module
from elspeth.web.composer.audit import ComposerToolCancellation, ComposerToolFailure
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.coordination.quota_authority import token_usage_entry_from_llm_call_envelope
from elspeth.web.sessions.composer_operations import COMPOSER_CANCEL_REQUESTED
from elspeth.web.sessions.models import composer_async_operations_table
from tests.helpers.composer_operations import running_operation_sync
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
from tests.integration.web.composer.test_freeform_planner_failure_translation import (
    _build_app,
    _Choice,
    _Function,
    _Message,
    _Response,
    _ToolCall,
)
from tests.unit.web.sessions.test_common_mutation_authority_workers import (
    captured,
    reachable,
    read_rows,
    thread_event,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [AssertionError, AuditIntegrityError])
async def test_actual_join_owner_preserves_fatal_cause_and_three_original_cancellations(monkeypatch, failure_type):
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    started, release = asyncio.Event(), asyncio.Event()
    observed = [asyncio.Event() for _ in range(3)]
    originals = []
    primary, prior = failure_type("actual fatal original"), ValueError("actual prior cause")
    primary.__cause__ = prior
    child_failure = ComposerToolFailure(primary, tool_invocations=(), llm_calls=())
    actual_shield = asyncio.shield

    async def observe_shield(awaitable):
        try:
            return await actual_shield(awaitable)
        except asyncio.CancelledError as original:
            originals.append(original)
            observed[len(originals) - 1].set()
            raise

    async def physical():
        started.set()
        await release.wait()
        raise child_failure from primary

    monkeypatch.setattr(composer_module.asyncio, "shield", observe_shield)
    requested = asyncio.Event()
    task = asyncio.create_task(composer_module._await_tool_turn_with_deferred_cancellation(physical(), cancellation_requested=requested))
    try:
        await asyncio.wait_for(started.wait(), 5)
        for index in range(3):
            task.cancel(f"actual-fatal-cancel-{index}")
            await asyncio.wait_for(observed[index].wait(), 5)
            assert not task.done() and requested.is_set()
        release.set()
        outcome = await captured(task)
        assert type(outcome) is ComposerToolCancellation
        assert len(originals) == len({id(item) for item in originals}) == 3
        assert [item.args for item in originals] == [(f"actual-fatal-cancel-{index}",) for index in range(3)]
        assert outcome.original_cancellation is originals[0]
        assert outcome.__cause__ is primary and primary.__cause__ is prior
        graph = reachable(outcome)
        assert all(any(item is original for item in graph) for original in (*originals, child_failure, primary, prior))
        if failure_type is AuditIntegrityError:
            projected = project_composer_operation_error(outcome, request_id="actual-fatal-control")
            assert projected.http_status == 500 and projected.error_type == "audit_integrity_error"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def run_public_cancel_case(tmp_path, monkeypatch, *, seam, fail_sql=False, count=1, durable_stop=False):
    assert seam in {"before_tool", "started_p4"}
    assert not fail_sql or seam == "started_p4"
    assert not durable_stop or (seam == "before_tool" and count == 1)
    entered, release = threading.Event(), threading.Event()
    cancellation_seen = [threading.Event() for _ in range(count)]
    originals, requests, response_ids, phases = [], [], [], []
    compose_errors, compose_tasks, dispatches = [], [], []
    sql_inputs, sql_returns, projected = [], [], []
    phase = None
    pipeline = None
    fault = OperationalError("public common-loop P4", {}, RuntimeError("original public driver failure"))
    request_text = "Build a CSV to JSONL pipeline."

    async def physical_sdk(**kwargs):
        requests.append(deepcopy(kwargs))
        if kwargs["model"] == "test/planner":
            assert len(requests) == 1, "Cancellation permitted an extra primary call"
            names = {item["function"]["name"] for item in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            assert pipeline is not None
            response_id = "cancel-primary-1"
            message = _Message(None, [_ToolCall("cancel-set-pipeline-1", _Function("set_pipeline", json.dumps({"pipeline": pipeline})))])
        else:
            assert seam == "started_p4", "New advisor work started after pre-tool cancellation"
            assert len(requests) == 2, "Cancellation permitted an extra physical request"
            assert kwargs["model"] == composer._settings.composer_advisor_model
            assert phase == "early" and phases == ["early"]
            assert kwargs.get("tools") is None
            assert "response_format" in kwargs
            response_id = "cancel-early-1"
            message = _Message(
                json.dumps(
                    {
                        "verdict": "CLEAN",
                        "category": "other",
                        "steps": [],
                        "findings": "The supplied candidate meets the requested shape.",
                        "note": None,
                    }
                )
            )
        response_ids.append(response_id)
        return _Response(
            choices=[_Choice(message)],
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": 0.01,
            },
            model=kwargs["model"],
            id=response_id,
        )

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=physical_sdk)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Common cancellation"}).json()["id"]
    pipeline = _pipeline(tmp_path, sid)
    source = tmp_path / "blobs" / sid / "input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
    changed = client.patch(f"/api/sessions/{sid}/composer/preferences", json={"trust_mode": "auto_commit", "density_default": "high"})
    assert changed.status_code == 200, changed.text

    original_compose = composer.compose

    async def observe_compose(*args, **kwargs):
        actual_task = asyncio.current_task()
        assert actual_task is not None
        compose_tasks.append(actual_task)
        try:
            return await original_compose(*args, **kwargs)
        except BaseException as error:
            compose_errors.append(error)
            raise

    monkeypatch.setattr(composer, "compose", observe_compose)

    original_join = composer_module._await_tool_turn_with_deferred_cancellation

    async def observe_join(awaitable, *, cancellation_requested):
        original_set = cancellation_requested.set

        def observe_set():
            # Python retains the caller's handled exception across this call.
            # This is the actual cancellation caught by production, not a new one.
            original = sys.exception()
            if isinstance(original, asyncio.CancelledError) and all(original is not prior for prior in originals):
                originals.append(original)
                assert len(originals) <= count
                cancellation_seen[len(originals) - 1].set()
            original_set()

        monkeypatch.setattr(cancellation_requested, "set", observe_set)
        return await original_join(awaitable, cancellation_requested=cancellation_requested)

    monkeypatch.setattr(composer_module, "_await_tool_turn_with_deferred_cancellation", observe_join)

    original_batch = composer._dispatch_tool_batch

    async def held_batch(**kwargs):
        if seam == "before_tool":
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            assert kwargs["cancellation_requested"].is_set()
        return await original_batch(**kwargs)

    monkeypatch.setattr(composer, "_dispatch_tool_batch", held_batch)

    original_execute = batch_module.execute_tool

    def observe_execute(name, *args, **kwargs):
        dispatches.append(name)
        return original_execute(name, *args, **kwargs)

    monkeypatch.setattr(batch_module, "execute_tool", observe_execute)

    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint

    async def observe_checkpoint(**kwargs):
        nonlocal phase
        phase = kwargs["phase"]
        phases.append(phase)
        return await original_checkpoint(**kwargs)

    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)

    original_p4 = sessions.persist_compose_turn_async

    async def observe_p4(**kwargs):
        sql_inputs.append(kwargs)
        result = await original_p4(**kwargs)
        sql_returns.append(result)
        return result

    monkeypatch.setattr(sessions, "persist_compose_turn_async", observe_p4)

    original_project = composer._persist_turn_audit

    async def observe_projection(**kwargs):
        result = await original_project(**kwargs)
        projected.append(result)
        return result

    monkeypatch.setattr(composer, "_persist_turn_audit", observe_projection)

    original_insert = sessions._insert_composition_checkpoint

    def held_insert(*args, **kwargs):
        assert seam == "started_p4"
        entered.set()
        assert release.wait(10)
        if fail_sql:
            raise fault
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(sessions, "_insert_composition_checkpoint", held_insert)

    async def observe_running(async_client, operation):
        try:
            await thread_event(entered)
            assert len(compose_tasks) == 1
            task = compose_tasks[0]
            if durable_stop:
                stopped = await async_client.post(f"{operation.poll_path}/cancel")
                assert stopped.status_code in (200, 202), stopped.text
            for index in range(count):
                if not durable_stop:
                    task.cancel(f"public-common-cancel-{index}")
                await thread_event(cancellation_seen[index])
                assert not task.done(), "Outer cancellation escaped the selected owned boundary"
                assert not operation.drive.done()
                if seam == "started_p4":
                    assert len(sql_inputs) == 1
                    assert not sql_inputs[0]["required_work"].complete
            release.set()
            await asyncio.wait_for(asyncio.shield(operation.drive), 20)
            response = await async_client.get(operation.poll_path)
            assert response.status_code == 200, response.text
            snapshot = response.json()
            assert snapshot["status"] == "failed"
            assert snapshot["result"] is None
            assert snapshot["error"] is not None
            if seam == "started_p4" and not fail_sql:
                assert len(sql_returns) == 1
                assert sql_returns[0].checkpoint_disposition == "committed"
                probe = await asyncio.wait_for(
                    async_client.patch(
                        f"/api/sessions/{sid}/composer/preferences",
                        json={"trust_mode": "auto_commit", "density_default": "high"},
                    ),
                    2,
                )
                assert probe.status_code == 200, probe.text
            return snapshot
        finally:
            release.set()

    snapshot = running_operation_sync(
        client,
        client.app,
        path=f"/api/sessions/{sid}/messages",
        body={
            "content": request_text,
            "operation_id": str(uuid4()),
            "state_id": None,
        },
        observe=observe_running,
    )
    assert len(compose_errors) == 1
    error = compose_errors[0]
    leaves = reachable(error)
    assert len(originals) == len({id(original) for original in originals}) == count
    if durable_stop:
        assert originals[0].args == (COMPOSER_CANCEL_REQUESTED,)
    else:
        assert [original.args for original in originals] == [(f"public-common-cancel-{index}",) for index in range(count)]
    assert all(any(leaf is original for leaf in leaves) for original in originals)
    if fail_sql:
        assert any(leaf is fault for leaf in leaves)

    chat, states, proposals = read_rows(engine, sid)
    assert proposals == []
    expected_calls = 1 if seam == "before_tool" else 2
    assert sdk.await_count == len(requests) == len(response_ids) == expected_calls
    assert phases == ([] if seam == "before_tool" else ["early"])
    assert dispatches == ([] if seam == "before_tool" else ["set_pipeline"])
    envelopes = [
        envelope
        for row in chat
        if row["role"] == "audit"
        for envelope in row["tool_calls"] or ()
        if envelope.get("_kind") == "llm_call_audit"
    ]
    assert len(envelopes) == expected_calls
    assert len({envelope["call"]["call_id"] for envelope in envelopes}) == expected_calls
    by_response = {envelope["call"]["provider_request_id"]: envelope for envelope in envelopes}
    assert set(by_response) == set(response_ids)
    for request, response_id in zip(requests, response_ids, strict=True):
        envelope = by_response[response_id]
        call = envelope["call"]
        assert call["status"] == "success"
        assert call["model_requested"] == request["model"]
        assert call["messages_hash"] == stable_hash(request["messages"])
        assert call["tools_spec_hash"] == (stable_hash(request["tools"]) if request.get("tools") else None)
        usage = token_usage_entry_from_llm_call_envelope(envelope)
        assert usage is not None
        assert usage.call_id == call["call_id"]
        assert usage.prompt_tokens == 10 and usage.completion_tokens == 5

    tools = [row for row in chat if row["role"] == "tool"]
    if seam == "before_tool":
        assert states == [] and tools == []
        assert sql_inputs == sql_returns == projected == []
    elif fail_sql:
        assert states == []
        assert sql_returns == projected == []
        assert len(sql_inputs) == 1 and sql_inputs[0]["required_work"].complete
        # Required terminal audit may retain the executed tool separately, but
        # it may not claim the rolled-back P4 state or original P4 assistant.
        assert all(row["composition_state_id"] is None for row in chat)
    else:
        assert len(states) == len(sql_inputs) == len(sql_returns) == len(projected) == 1
        assert sql_inputs[0]["required_work"].complete
        outcome = sql_returns[0]
        assert outcome.checkpoint_disposition == "committed"
        assert outcome.current_state_id == states[0]["id"]
        assert outcome.checkpoint is not None
        assert str(outcome.checkpoint.state.id) == states[0]["id"]
        assert projected[0].current_state_id == states[0]["id"]
        assert projected[0].persisted_assistant_message_id == outcome.assistant_id
        assert projected[0].persisted_tool_call_turn
        assert [row["tool_call_id"] for row in tools] == ["cancel-set-pipeline-1"]
        assert tools[0]["composition_state_id"] == states[0]["id"]
        assert tools[0]["parent_assistant_id"] == outcome.assistant_id
        private = [
            envelope
            for row in chat
            if row["role"] == "audit"
            for envelope in row["tool_calls"] or ()
            if envelope.get("_kind") == "composer_diagnostic_evidence" and envelope["tool_call_id"] == "cancel-set-pipeline-1"
        ]
        assert len(private) == 1
        evidence = ComposerDiagnosticEvidence.from_envelope(
            private[0], expected_session_id=sid, expected_tool_call_id="cancel-set-pipeline-1"
        )
        assert evidence == sql_inputs[0]["private_tool_evidence"][0]
    page = client.get(f"/api/sessions/{sid}/operations", params={"operation_id": snapshot["operation_id"]})
    assert page.status_code == 200, page.text
    (failure_outcome,) = page.json()["items"]
    saved = failure_outcome["saved_revisions"]
    assert saved["count"] == (1 if seam == "started_p4" and not fail_sql else 0)
    assert saved["latest_state_id"] == (states[0]["id"] if saved["count"] else None)
    assert saved["latest_version"] == (states[0]["version"] if saved["count"] else None)
    expected_type = "audit_integrity_error" if fail_sql else "request_cancelled" if durable_stop else "composer_operation_worker_lost"
    expected_status = 500 if fail_sql else 499 if durable_stop else 503
    expected_code = "http_error" if fail_sql else "request_cancelled" if durable_stop else "worker_lost"
    assert failure_outcome["error_type"] == expected_type
    assert failure_outcome["failure_code"] == expected_code
    assert snapshot["error"]["http_status"] == expected_status
    assert snapshot["error"]["error_type"] == expected_type
    assert snapshot["error"]["failure_code"] == expected_code
    # Terminal poll frames expose no pending-cancel flag; the durable marker
    # below distinguishes the completed Stop from direct task cancellation.
    assert snapshot["cancel_requested"] is False
    with engine.connect() as connection:
        durable = (
            connection.execute(
                select(composer_async_operations_table).where(
                    composer_async_operations_table.c.operation_id == snapshot["operation_id"],
                    composer_async_operations_table.c.session_id == sid,
                )
            )
            .mappings()
            .one()
        )
    assert (durable["cancel_requested_at"] is not None) is durable_stop
    worker = client.app.state.composer_async_worker
    assert worker._stopping is False
    assert not worker._instance_draining.is_set()
    return snapshot


def test_public_cancel_before_tool_keeps_completed_primary_and_starts_no_tool(tmp_path, monkeypatch):
    run_public_cancel_case(tmp_path, monkeypatch, seam="before_tool")


def test_public_stop_before_tool_records_durable_user_cancellation(tmp_path, monkeypatch):
    run_public_cancel_case(tmp_path, monkeypatch, seam="before_tool", durable_stop=True)


@pytest.mark.parametrize("fail_sql", [False, True])
def test_public_repeated_cancel_joins_actual_p4_and_retains_all_originals(tmp_path, monkeypatch, fail_sql):
    run_public_cancel_case(tmp_path, monkeypatch, seam="started_p4", fail_sql=fail_sql, count=3)
