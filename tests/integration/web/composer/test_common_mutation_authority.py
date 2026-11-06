"""Initial request authority survives actual held-provider preference changes."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, Request
from sqlalchemy import select

from elspeth.contracts.blobs import BlobDeletionPlan, BlobRecord, BlobReplacementPlan
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user, require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf, encode_message_shelf
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.protocol import ComposerMutationAuthorityRevoked
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gates_meta_from_facts
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blob_replacement_cleanups_table,
    blobs_table,
    chat_messages_table,
    composer_async_operations_table,
    composition_proposals_table,
    composition_rejection_events_table,
    composition_states_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    PRIMARY_MODEL,
    ScriptedSDK,
    continuation_app,
    end_admit,
    install_sdk,
    semantic_primary_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline


def _observe_authority(monkeypatch, composer):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.tools import _dispatch

    observed = {name: [] for name in ("loop", "batches", "candidate", "worker", "contexts", "p4")}
    actual_initialize = tool_batch._initialize_continuation_batch_live
    actual_candidate, actual_worker = tool_batch._finalize_complete_set_pipeline_candidate, tool_batch.execute_tool
    actual_context, actual_p4 = _dispatch.ToolContext, composer._persist_turn_audit

    async def initialize(**kwargs):
        context = kwargs["ctx"]
        if not observed["loop"]:
            observed["loop"].append((context.mutation_ceiling, context.turn_evidence))
        observed["batches"].append(context.mutation_ceiling)
        return await actual_initialize(**kwargs)

    async def candidate(arguments, state, context, **kwargs):
        observed["candidate"].append(context.mutation_ceiling)
        return await actual_candidate(arguments, state, context, **kwargs)

    def worker(name, *args, **kwargs):
        observed["worker"].append((name, kwargs.get("mutation_ceiling")))
        return actual_worker(name, *args, **kwargs)

    def context(**kwargs):
        result = actual_context(**kwargs)
        observed["contexts"].append(result)
        return result

    async def p4(**kwargs):
        observed["p4"].append(kwargs["mutation_ceiling"])
        return await actual_p4(**kwargs)

    monkeypatch.setattr(tool_batch, "_initialize_continuation_batch_live", initialize)
    monkeypatch.setattr(tool_batch, "_finalize_complete_set_pipeline_candidate", candidate)
    monkeypatch.setattr(tool_batch, "execute_tool", worker)
    monkeypatch.setattr(_dispatch, "ToolContext", context)
    monkeypatch.setattr(composer, "_persist_turn_audit", p4)
    return observed


async def run_authority_case(
    tmp_path,
    monkeypatch,
    *,
    initial,
    final,
    barrier,
    discovery_first=False,
    inline=False,
    blob_only=False,
    batch_count=1,
    nonempty=False,
    recompose=False,
    cap_errors=None,
    create_ack_loss=False,
    rootless_mixed=False,
    observed=None,
    mixed_order=None,
):
    """Reach the selected authority boundary in the actual current worker."""
    async with continuation_app(tmp_path, monkeypatch) as harness, harness.client() as client:
        app, sid = harness.app, str(harness.session_id)
        engine, sessions = app.state.session_engine, app.state.session_service
        composer = app.state.composer_service
        if mixed_order:
            assert observed is not None
            observed["authority"] = _observe_authority(monkeypatch, composer)
        if observed is not None:
            observed.update(sessions=sessions, sid=sid)
        pipeline = _pipeline(tmp_path, sid)
        if inline:
            pipeline["source"]["options"].pop("path")
            pipeline["source"]["inline_blob"] = {
                "filename": "candidate.csv",
                "mime_type": "text/csv",
                "content": "value\n42\n",
                "description": "Provider inline source",
            }
        input_path = tmp_path / "blobs" / sid / "input.csv"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text("name\nAda\n", encoding="utf-8")
        (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
        base_record = None
        user = None
        request_text = "Build a CSV to JSONL pipeline."
        if nonempty or recompose:
            seed = sessions.session_operation_authority.acquire(
                session_id=harness.session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id="authority-fixture-seed",
                lease_seconds=30,
            )
            try:
                if nonempty:
                    base_record = await sessions.save_composition_state(
                        harness.session_id,
                        CompositionStateData(
                            sources={"source": deepcopy(pipeline["source"])},
                            nodes=[],
                            edges=[],
                            outputs=[{**deepcopy(pipeline["outputs"][0]), "name": "rows"}],
                            metadata_={"name": "Existing fixture", "description": ""},
                            is_valid=True,
                            composer_meta={"completion_gates": completion_gates_meta_from_facts(CompletionGateFacts(None))},
                        ),
                        provenance="session_seed",
                        session_operation_context=seed,
                    )
                if recompose:
                    user = await sessions.add_message(
                        harness.session_id, "user", request_text, writer_principal="route_user_message", session_operation_context=seed
                    )
            finally:
                sessions.session_operation_authority.release(seed)
        with engine.connect() as conn:
            baseline = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
        operation_id = str(uuid4())
        body = message_body(request_text, operation_id=operation_id, state_id=None if base_record is None else str(base_record.id))
        route = "messages"
        if recompose:
            assert user is not None
            body = {"operation_id": operation_id, "state_id": body["state_id"], "expected_user_message_id": str(user.id)}
            route = "recompose"
        preference_path = f"/api/sessions/{sid}/composer/preferences"

        async def change_mode(mode):
            response = await client.patch(preference_path, json={"trust_mode": mode, "density_default": "high"})
            assert response.status_code == 200, response.text
            assert response.json()["trust_mode"] == mode

        await change_mode(initial)
        entered, release = threading.Event(), threading.Event()
        selected_ceilings, lost_acks = [], []
        mutation_ordinal = 2 if discovery_first else 1
        expected_auto = initial == "auto_commit" and (final == "auto_commit" or barrier == "p4_locked") and not blob_only
        refused = barrier in {"tool", "p4"} and final == "explicit_approve" and not blob_only
        candidate = (
            "The blob was saved." if blob_only else "The draft was saved." if expected_auto else "The proposal is waiting for approval."
        )

        def author(app):
            if blob_only:
                return None, [
                    tool_call(
                        app,
                        "authority-create-blob-1",
                        "create_blob",
                        {
                            "filename": "immediate.csv",
                            "mime_type": "text/csv",
                            "content": "value\n42\n",
                            "description": "Provider-created data",
                        },
                    )
                ]
            calls = [tool_call(app, f"authority-set-pipeline-{n}", "set_pipeline", pipeline) for n in range(1, batch_count + 1)]
            if discovery_first:
                calls[0]["id"] = "authority-set-pipeline-2"
            if rootless_mixed:
                other = deepcopy(pipeline)
                other["source"]["options"].pop("path")
                other["source"]["inline_blob"] = {"filename": "refused.csv", "mime_type": "text/csv", "content": "value\n43\n"}
                calls.extend(
                    [
                        tool_call(app, "authority-inline-refused", "set_pipeline", other),
                        tool_call(app, "authority-never-dispatch", "list_sources", {}),
                    ]
                )
            if mixed_order:
                by_name = {"set_pipeline": calls[0], "list_sources": tool_call(app, "authority-discovery", "list_sources", {})}
                calls = [by_name[name] for name in mixed_order]
            return None, calls

        steps = []
        if discovery_first:
            steps.extend(
                [
                    (
                        "primary",
                        lambda app: (
                            None,
                            [tool_call(app, "authority-discovery-1", "get_plugin_schema", {"plugin_type": "source", "name": "csv"})],
                        ),
                    ),
                    ("semantic", semantic_primary_response),
                ]
            )
        steps.append(("primary", author))
        if not refused and batch_count <= 10:
            if expected_auto and not nonempty:
                steps.append(
                    ("early", {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The draft is coherent.", "note": None})
                )
            if mixed_order:
                steps.extend(
                    [
                        ("semantic", semantic_primary_response),
                        ("primary", lambda app: (None, [tool_call(app, "authority-second-batch", "get_pipeline_state", {})])),
                    ]
                )
            steps.extend(
                [
                    ("semantic", semantic_primary_response),
                    ("primary", (candidate, None)),
                    ("semantic", semantic_review_response),
                    ("end", end_admit()),
                    ("semantic", semantic_stop_response),
                ]
            )
        sdk = ScriptedSDK(steps, app_ref=[app])
        primary_count = 0

        async def complete(**kwargs):
            nonlocal primary_count
            if kwargs["model"] == PRIMARY_MODEL:
                primary_count += 1
                if primary_count == mutation_ordinal and barrier in {"provider", "second_provider"}:
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 30)
            return await sdk(**kwargs)

        install_sdk(monkeypatch, complete)
        original_p4 = sessions.persist_continuation_compose_turn

        async def held_p4(**kwargs):
            if observed is not None:
                observed["sql_input"] = kwargs
            if barrier == "p4":
                assert any(row.composition_state_payload is not None for row in kwargs["redacted_tool_rows"])
                entered.set()
                assert await asyncio.to_thread(release.wait, 30)
            return await original_p4(**kwargs)

        monkeypatch.setattr(sessions, "persist_continuation_compose_turn", held_p4)
        actual_execute = tool_batch_module.execute_tool

        def held_tool(*args, **kwargs):
            selected_ceilings.append(kwargs.get("mutation_ceiling"))
            if barrier == "tool" and (not rootless_mixed or len(selected_ceilings) == 2):
                assert args[0] == ("create_blob" if blob_only else "set_pipeline")
                entered.set()
                assert release.wait(30)
            return actual_execute(*args, **kwargs)

        monkeypatch.setattr(tool_batch_module, "execute_tool", held_tool)
        actual_insert = sessions._insert_composition_checkpoint

        def held_insert(*args, **kwargs):
            if barrier == "p4_locked":
                entered.set()
                assert release.wait(30)
            return actual_insert(*args, **kwargs)

        monkeypatch.setattr(sessions, "_insert_composition_checkpoint", held_insert)
        preference_attempted = asyncio.Event()

        async def observe_pipeline_admission(request: Request, user: Annotated[UserIdentity, Depends(get_current_user)]):
            if barrier == "p4_locked" and request.method == "PATCH" and request.url.path.endswith("/composer/preferences"):
                assert entered.is_set() and not release.is_set()
                preference_attempted.set()
            return await require_pipeline_user(request, user)

        app.dependency_overrides[require_pipeline_user] = observe_pipeline_admission
        repository = sessions.session_operation_authority
        actual_mutate = repository.mutate

        def lose_create_ack(context, callback, *, auto_mutation=None):
            value = actual_mutate(context, callback, auto_mutation=auto_mutation)
            if create_ack_loss and not lost_acks and type(value) is BlobRecord and value.status == "ready":
                assert selected_ceilings and auto_mutation is selected_ceilings[-1]
                lost_acks.append(value)
                raise ConnectionError("actual activated inline-create acknowledgement lost")
            return value

        monkeypatch.setattr(repository, "mutate", lose_create_ack)
        actual_cap = tool_batch_module._pending_proposal_cap_error

        def cap(observed_count, **kwargs):
            error = actual_cap(observed_count, **kwargs)
            if cap_errors is not None:
                cap_errors.append(error)
            return error

        monkeypatch.setattr(tool_batch_module, "_pending_proposal_cap_error", cap)
        actual_module_p4 = composer._persist_turn_audit

        async def module_p4(**kwargs):
            if observed is not None:
                observed["module_input"] = kwargs
            value = await actual_module_p4(**kwargs)
            if observed is not None:
                observed["module_output"] = value
            return value

        monkeypatch.setattr(composer, "_persist_turn_audit", module_p4)
        pending = asyncio.create_task(submit_and_settle(harness, body, route=route, rounds=720))
        change = None
        try:
            assert await asyncio.to_thread(entered.wait, 60), "Actual selected authority boundary was never reached"
            if barrier == "p4_locked":
                change = asyncio.create_task(change_mode(final))
                await asyncio.wait_for(preference_attempted.wait(), 30)
                assert not change.done()
                release.set()
                await change
            else:
                await change_mode(final)
        finally:
            release.set()
            if change is not None and not change.done():
                await change
        terminal = await pending
        assert sdk.roles == [role for role, _payload in steps] and not sdk.steps
        phases = [role for role in sdk.roles if role in {"early", "end"}]
        with engine.connect() as conn:
            states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
            proposals = (
                conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).mappings().all()
            )
            chat = (
                conn.execute(
                    select(chat_messages_table).where(chat_messages_table.c.session_id == sid).order_by(chat_messages_table.c.sequence_no)
                )
                .mappings()
                .all()
            )
            blobs = conn.execute(select(blobs_table).where(blobs_table.c.session_id == sid)).mappings().all()
            rejections = conn.execute(
                select(composition_rejection_events_table).where(composition_rejection_events_table.c.session_id == sid)
            ).all()
            attempts = (
                conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == sid))
                .mappings()
                .all()
            )
            ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == sid)).mappings().all()
            journals = tuple(
                tuple(conn.execute(select(table).where(table.c.session_id == sid)).all())
                for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table)
            )
        old_ids = {row["id"] for row in baseline}
        assert [row for row in states if row["id"] in old_ids] == baseline
        states = [row for row in states if row["id"] not in old_ids]
        calls = [
            row["tool_calls"][0]["call"]
            for row in chat
            if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0].get("_kind") == "llm_call_audit"
        ]
        assert len(calls) == len(sdk.requests) == len(attempts) == len(ledger)
        assert {call["call_id"] for call in calls} == {row["attempt_id"] for row in attempts}
        assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in ledger}
        assert all(row["settled_at"] is not None for row in attempts)
        assert all(call["status"] == "success" for call in calls)
        with engine.connect() as conn:
            operation = (
                conn.execute(
                    select(composer_async_operations_table).where(
                        composer_async_operations_table.c.session_id == sid,
                        composer_async_operations_table.c.operation_id == operation_id,
                    )
                )
                .mappings()
                .one()
            )
        private = [
            ComposerDiagnosticEvidence.from_envelope(
                row["tool_calls"][0], expected_session_id=sid, expected_operation_id=operation["session_operation_id"]
            )
            for row in chat
            if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0].get("_kind") == "composer_diagnostic_evidence"
        ]
        requests_by_id = {item.tool_call_id: item for item in private if item.status == "provider_request"}
        assert set(requests_by_id) == {call["call_id"] for call in calls}
        assert all(item.operation_epoch == operation["session_operation_epoch"] for item in private)
        for evidence in requests_by_id.values():
            assert evidence.arguments.content is not None
            request = json.loads(evidence.arguments.content)
            assert request["capture_boundary"] and request["messages"]
        if inline or blob_only:
            assert len(blobs) == int(blob_only or barrier == "p4" or expected_auto)
            for blob in blobs:
                assert blob["status"] == "ready" and Path(blob["storage_path"]).read_bytes() == b"value\n42\n"
        assert bool(lost_acks) is create_ack_loss
        assert journals == ((), ())
        if barrier in {"tool", "p4"} and not blob_only:
            assert rejections == []
        return terminal, states, proposals, chat, phases, primary_count, len(sdk.requests)


def _assert_actual_graph_publications(result, states, chat, applied):
    """Current P4 publishes one checkpoint; the final route does not duplicate it."""
    assert [row["provenance"] for row in states] == (["tool_call"] if applied else [])
    if not applied:
        return
    (checkpoint,) = states
    tool = next(row for row in chat if row["role"] == "tool" and row["tool_call_id"] == "authority-set-pipeline-1")
    assert tool["composition_state_id"] == checkpoint["id"]
    assert result["state"]["id"] == checkpoint["id"]
    assert result["message"]["composition_state_id"] == checkpoint["id"]
    assert result["message"]["role"] == "assistant"
    assert result["message"]["id"] == chat[-1]["id"]
    assert result["message"]["segments"][-1]["kind"] == "trusted_system_notice"
    assert "covers 1 saved draft revisions" in result["message"]["segments"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,final,applied",
    [
        ("auto_commit", "auto_commit", True),
        ("explicit_approve", "explicit_approve", False),
        ("auto_commit", "explicit_approve", False),
        ("explicit_approve", "auto_commit", False),
    ],
)
async def test_held_primary_preserves_initial_and_current_authority(tmp_path, monkeypatch, initial, final, applied):
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial=initial, final=final, barrier="provider"
    )
    assert settled.status == "completed", settled.error
    assert settled.result is not None
    result = settled.result.model_dump(mode="json")
    _assert_actual_graph_publications(result, states, chat, applied)
    assert len(proposals) == int(not applied)
    assert all(row["status"] == "pending" for row in proposals)
    assert primary == 2
    assert phases == (["early", "end"] if applied else ["end"])
    assert physical == (7 if applied else 6)
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-set-pipeline-1"]
    assert tools[0]["composition_state_id"] == (states[0]["id"] if applied else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("nonempty,recompose", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize(
    "initial,final,applied",
    [
        ("auto_commit", "auto_commit", True),
        ("explicit_approve", "explicit_approve", False),
        ("auto_commit", "explicit_approve", False),
        ("explicit_approve", "auto_commit", False),
    ],
)
async def test_same_authority_for_existing_graph_and_recompose(
    tmp_path,
    monkeypatch,
    nonempty,
    recompose,
    initial,
    final,
    applied,
):
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial=initial, final=final, barrier="provider", nonempty=nonempty, recompose=recompose
    )
    assert settled.result is not None
    result = settled.result.model_dump(mode="json")
    assert result["message"]["role"] == "assistant"
    _assert_actual_graph_publications(result, states, chat, applied)
    assert len(proposals) == int(not applied)
    assert all(row["status"] == "pending" for row in proposals)
    early = applied and not nonempty
    assert phases == (["early", "end"] if early else ["end"])
    assert primary == 2 and physical == 5 + len(phases)
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["tool_call_id"] == "authority-set-pipeline-1"
    if applied:
        assert tool["composition_state_id"] == states[0]["id"]
    else:
        assert tool["composition_state_id"] is None


def _revoked_graph_history_event(provider_json):
    [event] = [item for item in json.loads(provider_json)["events"] if item.get("tool_call_id") == "authority-set-pipeline-1"]
    return event


def _assert_revoked_graph_replays_as_refusal(event):
    replayed = json.loads(event["content"])
    assert replayed["success"] is False
    assert [item["error_code"] for item in replayed["validation"]["errors"]] == ["auto_mutation_revoked"]
    assert event["selected_result_retained"] is False
    assert "selected_result_authority_revoked" in event["visible_event_availability"]


@pytest.mark.asyncio
async def test_late_downgrade_refuses_graph_but_keeps_actual_p4_cohort(tmp_path, monkeypatch):
    observed = {}
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4", observed=observed
    )
    assert states == [] and proposals == []
    assert settled.error is not None
    status, body = settled.error.http_status, settled.error.body
    assert status == 409, body
    assert body["detail"]["error_type"] == "composer_mutation_authority_revoked"
    assert primary == physical == 1 and phases == []
    tools = [row for row in chat if row["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["tool_call_id"] == "authority-set-pipeline-1"
    assert tools[0]["composition_state_id"] is None
    public = json.loads(tools[0]["content"])
    assert public["success"] is False
    assert [item["error_code"] for item in public["validation"]["errors"]] == ["auto_mutation_revoked"]
    assistant = next(row for row in chat if row["id"] == tools[0]["parent_assistant_id"])
    assert assistant["content"] == ""
    assert (
        len([row for row in chat if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0].get("_kind") == "llm_call_audit"])
        == 1
    )
    # A later Advisor rebuilds history from these rows; the revoked graph must
    # replay as the recorded refusal, never as the success the model first saw.
    sessions, sid = observed["sessions"], observed["sid"]
    user = next(row for row in chat if row["role"] == "user")
    history = AdvisorTranscriptHistory.from_records(
        session_id=sid,
        actor_user_id="continuation-user",
        current_request_id=str(user["id"]),
        request=user["content"],
        records=await sessions.get_messages(UUID(sid)),
    )
    owner = AdvisorTranscriptOwner(request=user["content"], history=history)
    _assert_revoked_graph_replays_as_refusal(_revoked_graph_history_event(owner.snapshot().provider_json))
    chunk = encode_message_shelf(owner.snapshot_for_continuation([]))
    cold, _ = AdvisorTranscriptOwner.for_continuation(
        decode_message_shelf(PrivateChunk(chunk.reference, chunk.canonical)),
        session_id=sid,
        actor_user_id="continuation-user",
        request_id=str(user["id"]),
        request=user["content"],
    )
    _assert_revoked_graph_replays_as_refusal(_revoked_graph_history_event(cold.snapshot().provider_json))


@pytest.mark.asyncio
async def test_graph_commit_precedes_later_preference_revocation(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4_locked"
    )
    assert settled.status == "completed", settled.error
    assert settled.result is not None
    result = settled.result.model_dump(mode="json")
    _assert_actual_graph_publications(result, states, chat, True)
    assert proposals == []
    assert primary == 2 and physical == 7 and phases == ["early", "end"]
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["composition_state_id"] == states[0]["id"]
    assert json.loads(tool["content"])["success"] is True


@pytest.mark.asyncio
async def test_upgrade_between_iterations_does_not_reauthorize(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="explicit_approve", final="auto_commit", barrier="second_provider", discovery_first=True
    )
    assert settled.status == "completed" and settled.result is not None, settled.error
    assert states == [] and len(proposals) == 1 and proposals[0]["status"] == "pending"
    assert phases == ["end"] and primary == 3 and physical == 8
    assert [row["tool_call_id"] for row in chat if row["role"] == "tool"] == ["authority-discovery-1", "authority-set-pipeline-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("final_mode", ["explicit_approve", "auto_commit"])
async def test_upgraded_request_still_uses_pending_proposal_cap(tmp_path, monkeypatch, final_mode):
    cap_errors = []
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="explicit_approve", final=final_mode, barrier="provider", batch_count=11, cap_errors=cap_errors
    )
    assert settled.error is not None
    status, body = settled.error.http_status, settled.error.body
    assert status == 422, body
    detail = body["detail"]
    assert detail["reason"] == "convergence_composition_budget"
    assert len(cap_errors) == 1
    assert cap_errors[0].evidence == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
    assert len(cap_errors[0].llm_calls) == 1
    assert states == [] and proposals == []
    assert not [row for row in chat if row["role"] == "tool"]
    assert primary == physical == 1 and phases == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "barrier,physical,phases",
    [
        ("tool", 1, []),
        ("p4", 1, []),
    ],
)
async def test_inline_source_respects_custody_and_retains_actual_prefix(tmp_path, monkeypatch, barrier, physical, phases):
    settled, states, proposals, chat, actual_phases, primary, actual_physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier=barrier, inline=True
    )
    assert settled.error is not None
    status, body = settled.error.http_status, settled.error.body
    assert status == 409, body
    assert states == [] and proposals == []
    assert primary == 1 and actual_physical == physical and actual_phases == phases
    tools = [row for row in chat if row["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["composition_state_id"] is None
    assert json.loads(tools[0]["content"])["success"] is False


@pytest.mark.asyncio
async def test_create_blob_remains_immediate_after_downgrade(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="tool", blob_only=True
    )
    assert settled.status == "completed" and settled.result is not None, settled.error
    assert states == [] and proposals == []
    assert primary == 2 and physical == 6 and phases == ["end"]
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-create-blob-1"]
    assert json.loads(tools[0]["content"])["success"] is True


@pytest.mark.asyncio
async def test_activated_inline_create_reconciles_lost_ack_before_p4_refusal(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4", inline=True, create_ack_loss=True
    )
    assert settled.status == "failed" and settled.error is not None
    assert settled.error.http_status == 409
    assert states == proposals == []
    assert primary == physical == 1 and phases == []
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["composition_state_id"] is None
    payload = json.loads(tool["content"])
    assert payload["success"] is False and payload["validation"]["errors"][0]["error_code"] == "auto_mutation_revoked"


@pytest.mark.asyncio
async def test_rootless_mixed_refusal_skips_early_and_refuses_earlier_candidate(tmp_path, monkeypatch):
    observed = {}
    settled, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="tool", rootless_mixed=True, observed=observed
    )
    assert states == proposals == []
    assert settled.status == "failed" and settled.error is not None
    assert settled.error.http_status == 409
    assert phases == [] and primary == physical == 1
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-set-pipeline-1", "authority-inline-refused"]
    base = observed["module_input"]["base_state"]
    assert base.sources == {} and base.nodes == () and base.outputs == ()
    selected = observed["module_output"].audit_outcome
    assert selected.authority_revocation.ceiling is observed["module_input"]["mutation_ceiling"]
    assert selected.published_tool_rows[0].composition_state_payload is None
    assert selected.published_tool_rows[1].content == tools[1]["content"]
    assert selected.published_tool_rows[1] is observed["sql_input"]["redacted_tool_rows"][1]
    assert observed["sql_input"]["revoked_tool_rows"][1] is observed["sql_input"]["redacted_tool_rows"][1]
    for tool in tools:
        assert tool["composition_state_id"] is None
        payload = json.loads(tool["content"])
        assert payload["success"] is False
        assert payload["validation"]["errors"][0]["error_code"] == "auto_mutation_revoked"
    assert json.loads(tools[0]["content"])["version"] == base.version
    assert json.loads(tools[1]["content"])["version"] == observed["module_input"]["tool_outcomes"][1].response.updated_state.version


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("final_mode,ack_loss", [("auto_commit", False), ("explicit_approve", False), ("auto_commit", True)])
async def test_approval_governed_blob_custody_refuses_late_downgrade(tmp_path, monkeypatch, tool, final_mode, ack_loss):
    async with continuation_app(tmp_path, monkeypatch) as harness, harness.client() as client:
        app, sid = harness.app, str(harness.session_id)
        engine, sessions = app.state.session_engine, app.state.session_service
        entered, release = threading.Event(), threading.Event()
        created, executed, selected_ceilings, lost_acks, returned_ids = {}, [], [], [], []
        operation_id = str(uuid4())

        def create(app):
            return None, [
                tool_call(
                    app,
                    "blob-create-actual",
                    "create_blob",
                    {
                        "filename": "owned.csv",
                        "mime_type": "text/csv",
                        "content": "value\n42\n",
                        "description": "Provider-created custody control",
                    },
                )
            ]

        def change(app):
            arguments = {"blob_id": created["blob_id"]}
            if tool == "update_blob":
                arguments["content"] = "value\n43\n"
            return None, [tool_call(app, "blob-change-actual", tool, arguments)]

        steps = [("primary", create), ("semantic", semantic_primary_response), ("primary", change)]
        if final_mode == "auto_commit":
            steps.extend(
                [
                    ("semantic", semantic_primary_response),
                    ("primary", ("The requested blob change is saved.", None)),
                    ("semantic", semantic_review_response),
                    ("end", end_admit()),
                    ("semantic", semantic_stop_response),
                ]
            )
        sdk = ScriptedSDK(steps, app_ref=[app])

        async def complete(**kwargs):
            response = await sdk(**kwargs)
            returned_ids.append(response.id)
            return response

        install_sdk(monkeypatch, complete)
        preference_path = f"/api/sessions/{sid}/composer/preferences"
        initial = await client.patch(preference_path, json={"trust_mode": "auto_commit", "density_default": "high"})
        assert initial.status_code == 200
        actual_execute = tool_batch_module.execute_tool

        def execute(name, *args, **kwargs):
            executed.append(name)
            if name == tool:
                selected_ceilings.append(kwargs["mutation_ceiling"])
                entered.set()
                assert release.wait(30)
            result = actual_execute(name, *args, **kwargs)
            if name == "create_blob":
                assert result.success
                created["blob_id"] = result.data["blob_id"]
            return result

        monkeypatch.setattr(tool_batch_module, "execute_tool", execute)
        repository = sessions.session_operation_authority
        actual_mutate = repository.mutate

        def lose_change_ack(context, callback, *, auto_mutation=None):
            value = actual_mutate(context, callback, auto_mutation=auto_mutation)
            selected = (tool == "update_blob" and type(value) is BlobReplacementPlan and value.phase == "purge_pending") or (
                tool == "delete_blob" and type(value) is BlobDeletionPlan and value.phase == "purge_pending"
            )
            if ack_loss and selected and not lost_acks:
                assert selected_ceilings and auto_mutation is selected_ceilings[-1]
                lost_acks.append(value)
                raise ConnectionError("actual activated blob-change acknowledgement lost")
            return value

        monkeypatch.setattr(repository, "mutate", lose_change_ack)

        def journals():
            with engine.connect() as conn:
                return tuple(
                    tuple(conn.execute(select(table).where(table.c.session_id == sid).order_by(*table.primary_key.columns)).all())
                    for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table)
                )

        pending = asyncio.create_task(
            submit_and_settle(
                harness, message_body("Create the CSV then apply the requested blob change.", operation_id=operation_id), rounds=720
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 90), "Real blob change was never dispatched"
            with engine.connect() as conn:
                before = conn.execute(select(blobs_table).where(blobs_table.c.id == created["blob_id"])).mappings().one()
            original_bytes = Path(before["storage_path"]).read_bytes()
            before_journals = journals()
            assert original_bytes == b"value\n42\n"
            response = await client.patch(preference_path, json={"trust_mode": final_mode, "density_default": "high"})
            assert response.status_code == 200
        finally:
            release.set()
        terminal = await pending
        assert sdk.roles == [role for role, _payload in steps] and not sdk.steps
        with engine.connect() as conn:
            after = conn.execute(select(blobs_table).where(blobs_table.c.id == created["blob_id"])).mappings().one_or_none()
            states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).all()
            proposals = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).all()
            chat = (
                conn.execute(
                    select(chat_messages_table).where(chat_messages_table.c.session_id == sid).order_by(chat_messages_table.c.sequence_no)
                )
                .mappings()
                .all()
            )
            attempts = (
                conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == sid))
                .mappings()
                .all()
            )
            ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == sid)).mappings().all()
        assert states == proposals == []
        assert executed == ["create_blob", tool]
        audits = [envelope["call"] for row in chat for envelope in row["tool_calls"] or () if envelope.get("_kind") == "llm_call_audit"]
        assert len(audits) == len(sdk.requests) == len(attempts) == len(ledger)
        assert {row["provider_request_id"] for row in audits} == set(returned_ids)
        assert {row["call_id"] for row in audits} == {row["attempt_id"] for row in attempts}
        assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in ledger}
        assert all(row["settled_at"] is not None for row in attempts)
        tools = [row for row in chat if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in tools] == ["blob-create-actual", "blob-change-actual"]
        assert all(row["composition_state_id"] is None for row in tools)
        assert bool(lost_acks) is ack_loss
        if final_mode == "explicit_approve":
            assert after == before and journals() == before_journals
            assert Path(before["storage_path"]).read_bytes() == original_bytes
            assert terminal.status == "failed" and terminal.error is not None
            assert terminal.error.http_status == 409 and terminal.error.error_type == "composer_mutation_authority_revoked"
            assert "auto_mutation_revoked" in tools[-1]["content"]
        else:
            assert terminal.status == "completed" and terminal.result is not None, terminal.error
            assert journals() == ((), ())
            if tool == "update_blob":
                assert after is not None and after["status"] == "ready"
                assert after["content_hash"] != before["content_hash"]
                assert Path(after["storage_path"]).read_bytes() == b"value\n43\n"
            else:
                assert after is None and not Path(before["storage_path"]).exists()


@pytest.mark.asyncio
async def test_sticky_refusal_selects_entire_mixed_cohort_and_preserves_prior_checkpoint(tmp_path, monkeypatch):
    from elspeth.contracts.composer_mutation import ComposerAutoMutationRefused
    from elspeth.web.composer.visible_tool_event import VisibleToolEvent
    from elspeth.web.sessions import continuation_driver

    async with continuation_app(tmp_path, monkeypatch) as harness, harness.client() as client:
        app, sid = harness.app, str(harness.session_id)
        engine, sessions, composer = app.state.session_engine, app.state.session_service, app.state.composer_service
        created = {}
        worker_entered, worker_release = threading.Event(), threading.Event()
        p4_entered, p4_release = asyncio.Event(), asyncio.Event()
        executed, batches, p4_inputs, refusals, public_errors, module_outcomes, returned_ids = [], [], [], [], [], [], []
        raw_candidate = "PRIVATE second-batch graph candidate prose"
        operation_id = str(uuid4())
        directory = tmp_path / "blobs" / sid
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "input.csv").write_text("name\nAda\n", encoding="utf-8")
        (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
        preference_path = f"/api/sessions/{sid}/composer/preferences"
        initial = await client.patch(preference_path, json={"trust_mode": "auto_commit", "density_default": "high"})
        assert initial.status_code == 200

        def create_call(app, call_id, value):
            return tool_call(
                app, call_id, "create_blob", {"filename": f"{call_id}.csv", "mime_type": "text/csv", "content": f"value\n{value}\n"}
            )

        def prefix_author(app):
            return None, [
                create_call(app, "sticky-prefix-blob", 42),
                tool_call(app, "sticky-prefix-graph", "set_pipeline", _pipeline(tmp_path, sid)),
            ]

        def mixed_author(app):
            assert "sticky-prefix-blob" in created
            return raw_candidate, [
                tool_call(app, "sticky-graph-a", "set_metadata", {"patch": {"name": "PRIVATE candidate A"}}),
                create_call(app, "sticky-independent-blob", 99),
                tool_call(app, "sticky-graph-b", "set_metadata", {"patch": {"name": "PRIVATE candidate B"}}),
                tool_call(
                    app, "sticky-refused-update", "update_blob", {"blob_id": created["sticky-prefix-blob"], "content": "value\n43\n"}
                ),
                tool_call(app, "sticky-never-dispatch", "list_sources", {}),
            ]

        steps = [
            ("primary", prefix_author),
            ("early", {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The first draft is coherent.", "note": None}),
            ("semantic", semantic_primary_response),
            ("primary", mixed_author),
        ]
        sdk = ScriptedSDK(steps, app_ref=[app])

        async def complete(**kwargs):
            response = await sdk(**kwargs)
            returned_ids.append(response.id)
            return response

        install_sdk(monkeypatch, complete)
        actual_execute, actual_batch = tool_batch_module.execute_tool, tool_batch_module._initialize_continuation_batch_live
        actual_p4, actual_iteration = sessions.persist_continuation_compose_turn, continuation_driver.advance_continuation_iteration
        actual_module_p4 = composer._persist_turn_audit

        def execute(name, arguments, *args, **kwargs):
            executed.append((name, kwargs["mutation_ceiling"]))
            if name == "update_blob":
                worker_entered.set()
                assert worker_release.wait(30)
            try:
                result = actual_execute(name, arguments, *args, **kwargs)
            except ComposerAutoMutationRefused as error:
                refusals.append(error.revocation)
                raise
            if name == "create_blob":
                assert result.success
                created["sticky-prefix-blob" if len(created) == 0 else "sticky-independent-blob"] = result.data["blob_id"]
            return result

        async def batch(**kwargs):
            batches.append((kwargs["acc"].state, kwargs["ctx"].mutation_ceiling))
            return await actual_batch(**kwargs)

        async def p4(**kwargs):
            p4_inputs.append(kwargs)
            if len(p4_inputs) == 2:
                assert kwargs["authority_revocation"] == refusals[0]
                p4_entered.set()
                await asyncio.wait_for(p4_release.wait(), 30)
            return await actual_p4(**kwargs)

        async def iteration(*args, **kwargs):
            try:
                return await actual_iteration(*args, **kwargs)
            except ComposerMutationAuthorityRevoked as error:
                public_errors.append(error)
                raise

        async def module_p4(**kwargs):
            result = await actual_module_p4(**kwargs)
            module_outcomes.append(result)
            return result

        monkeypatch.setattr(tool_batch_module, "execute_tool", execute)
        monkeypatch.setattr(tool_batch_module, "_initialize_continuation_batch_live", batch)
        monkeypatch.setattr(sessions, "persist_continuation_compose_turn", p4)
        monkeypatch.setattr(continuation_driver, "advance_continuation_iteration", iteration)
        monkeypatch.setattr(composer, "_persist_turn_audit", module_p4)
        pending = asyncio.create_task(
            submit_and_settle(
                harness, message_body("Build the CSV draft then apply the requested changes.", operation_id=operation_id), rounds=720
            )
        )
        try:
            assert await asyncio.to_thread(worker_entered.wait, 120)
            with engine.connect() as conn:
                prefix = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
            assert len(prefix) == 1 and prefix[0]["provenance"] == "tool_call"
            response = await client.patch(preference_path, json={"trust_mode": "explicit_approve", "density_default": "high"})
            assert response.status_code == 200 and response.json()["trust_mode"] == "explicit_approve"
            worker_release.set()
            await asyncio.wait_for(p4_entered.wait(), 60)
            response = await client.patch(preference_path, json={"trust_mode": "auto_commit", "density_default": "high"})
            assert response.status_code == 200 and response.json()["trust_mode"] == "auto_commit"
            p4_release.set()
        finally:
            worker_release.set()
            p4_release.set()
        terminal = await pending
        assert sdk.roles == [role for role, _payload in steps] and not sdk.steps
        with engine.connect() as conn:
            states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
            chat = (
                conn.execute(
                    select(chat_messages_table).where(chat_messages_table.c.session_id == sid).order_by(chat_messages_table.c.sequence_no)
                )
                .mappings()
                .all()
            )
            blobs = conn.execute(select(blobs_table).where(blobs_table.c.session_id == sid)).mappings().all()
            proposals = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).all()
            rejections = conn.execute(
                select(composition_rejection_events_table).where(composition_rejection_events_table.c.session_id == sid)
            ).all()
            attempts = (
                conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == sid))
                .mappings()
                .all()
            )
            ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == sid)).mappings().all()
            journals = tuple(
                tuple(conn.execute(select(table).where(table.c.session_id == sid)).all())
                for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table)
            )
        assert states == prefix and proposals == rejections == [] and journals == ((), ())
        assert terminal.status == "failed" and terminal.error is not None
        assert terminal.error.http_status == 409 and terminal.error.error_type == "composer_mutation_authority_revoked"
        assert "PRIVATE" not in terminal.model_dump_json()
        history = await client.get(f"/api/sessions/{sid}/operations", params={"operation_id": operation_id})
        assert history.status_code == 200
        (failure_outcome,) = history.json()["items"]
        assert failure_outcome["saved_revisions"] == {
            "attribution": "fenced",
            "count": 1,
            "latest_state_id": prefix[0]["id"],
            "latest_version": prefix[0]["version"],
        }
        assert failure_outcome["error_type"] == "composer_mutation_authority_revoked"
        calls = [
            row["tool_calls"][0]["call"]
            for row in chat
            if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0].get("_kind") == "llm_call_audit"
        ]
        assert len(calls) == len(attempts) == len(ledger) == len(sdk.requests)
        assert {call["call_id"] for call in calls} == {row["attempt_id"] for row in attempts}
        assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in ledger}
        assert {call["provider_request_id"] for call in calls} == set(returned_ids)
        assert all(call["provider_cost"] == 0.01 and call["prompt_tokens"] == 7 and call["completion_tokens"] == 3 for call in calls)
        assert all(row["settled_at"] is not None for row in attempts)
        assert len(batches) == len(p4_inputs) == len(module_outcomes) == 2
        p4_outcomes = [item.audit_outcome for item in module_outcomes]
        ceiling = batches[0][1]
        assert batches[1][1] == ceiling and refusals[0].ceiling == ceiling
        assert p4_outcomes[1].authority_revocation == refusals[0]
        assert public_errors[0].revocation == refusals[0] and public_errors[0].partial_state == batches[1][0]
        assert p4_outcomes[1].current_state_id == prefix[0]["id"] and p4_outcomes[1].checkpoint_disposition == "no_new_checkpoint"
        assert p4_outcomes[1].published_assistant_content == ""
        assert module_outcomes[1].redacted_tool_rows is p4_outcomes[1].published_tool_rows
        assert module_outcomes[1].persisted_assistant_content == p4_outcomes[1].published_assistant_content
        assert module_outcomes[1].authority_revocation == refusals[0]
        assert module_outcomes[1].persisted_assistant_matches_current_dispatch is False
        expected_tools = ["create_blob", "set_pipeline", "set_metadata", "create_blob", "set_metadata", "update_blob"]
        assert [name for name, _ in executed] == expected_tools
        assert all(selected == (None if name == "create_blob" else ceiling) for name, selected in executed)
        tools = [row for row in chat if row["role"] == "tool"]
        assert len(tools) == 6 and "sticky-never-dispatch" not in {row["tool_call_id"] for row in tools}
        inputs = p4_inputs[1]
        assert [row.tool_call_id for row in inputs["redacted_tool_rows"]] == [
            "sticky-graph-a",
            "sticky-independent-blob",
            "sticky-graph-b",
            "sticky-refused-update",
        ]
        parent = next(row for row in chat if row["id"] == tools[-1]["parent_assistant_id"])
        assert parent["content"] == "" and parent["raw_content"] == raw_candidate
        for original, alternative, saved in zip(inputs["redacted_tool_rows"], inputs["revoked_tool_rows"], tools[2:], strict=True):
            assert saved["content"] == alternative.content and saved["composition_state_id"] is None
            if original.composition_state_payload is None:
                assert alternative is original
            else:
                payload = json.loads(saved["content"])
                assert payload["success"] is False and payload["version"] == batches[1][0].version
                assert payload["validation"]["errors"][0]["error_code"] == "auto_mutation_revoked"
                assert alternative.composition_state_payload is None
                assert alternative.visible_event == original.visible_event.with_authority_revocation(alternative.content)
            assert VisibleToolEvent.from_envelope(saved["tool_calls"][0]) == alternative.visible_event.with_parent(parent["id"])
        expected_private = {evidence.tool_call_id: evidence for evidence in inputs["private_tool_evidence"]}
        actual_private = {
            item.tool_call_id: item
            for row in chat
            if row["role"] == "audit"
            for envelope in row["tool_calls"] or ()
            if envelope.get("_kind") == "composer_diagnostic_evidence"
            for item in [
                ComposerDiagnosticEvidence.from_envelope(
                    envelope, expected_session_id=sid, expected_operation_id=inputs["session_operation_context"].fence.operation_id
                )
            ]
            if item.tool_call_id in expected_private
        }
        assert actual_private == expected_private
        assert {row["id"] for row in blobs} == set(created.values())
        assert all(row["status"] == "ready" for row in blobs)
        assert {Path(row["storage_path"]).read_bytes() for row in blobs} == {b"value\n42\n", b"value\n99\n"}


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("set_pipeline", "list_sources"), ("list_sources", "set_pipeline")])
async def test_mixed_batch_forwards_each_selected_automatic_authority(tmp_path, monkeypatch, order):
    observed = {}
    terminal, states, proposals, chat, phases, primary, physical = await run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="auto_commit", barrier="provider", mixed_order=order, observed=observed
    )
    assert terminal.status == "completed" and terminal.result is not None, terminal.error
    assert proposals == [] and len(states) == 1
    assert phases == ["early", "end"] and primary == 3 and physical == 9
    authority = observed["authority"]
    users = [row for row in chat if row["role"] == "user"]
    (user,) = users
    ceiling, evidence = authority["loop"][0]
    assert ceiling.request_id == evidence.request_id == user["id"]
    assert ceiling.session_id == observed["sid"]
    assert len(authority["loop"]) == 1 and len(authority["p4"]) == 2
    assert all(item == ceiling for item in authority["p4"])
    assert authority["candidate"] == [ceiling]
    assert [name for name, _ in authority["worker"]] == [*order, "get_pipeline_state"]
    for (name, selected), context in zip(authority["worker"], authority["contexts"], strict=True):
        expected = ceiling if name == "set_pipeline" else None
        assert selected == expected and context.mutation_ceiling == expected
    assert len(authority["batches"]) == 2 and all(item == ceiling for item in authority["batches"])
