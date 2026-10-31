"""Initial request authority survives actual held-provider preference changes."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import litellm
import pytest
from fastapi import Depends, Request
from sqlalchemy import select

from elspeth.contracts.blobs import BlobDeletionPlan, BlobRecord, BlobReplacementPlan
from elspeth.web.auth.middleware import get_current_user, require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer import tool_batch as tool_batch_module
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf, encode_message_shelf
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerMutationAuthorityRevoked
from elspeth.web.sessions.composer_operation_versions import COMPOSER_OPERATION_VERSION_HEADER
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
from tests.helpers.advisor_review import parse_advisor_publication_review
from tests.helpers.composer_operations import message_body, running_operation_sync, settle_sync, submit_and_settle
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
from tests.integration.web.composer.test_freeform_planner_failure_translation import (
    _build_app,
    _Choice,
    _Function,
    _llm_audit_rows,
    _Message,
    _Response,
    _ToolCall,
    _valid_pipeline_completion,
)


def _exercise_request(tmp_path, monkeypatch, upgrade, mutation_count, mixed_order=None, downgrade=False, late_p4=False):
    """Exercise initial and current authority through the actual held SDK call."""
    holder: dict[str, str] = {}
    authored = _valid_pipeline_completion(tmp_path, holder)
    entered, release = asyncio.Event(), asyncio.Event()
    p4_entered, p4_release = asyncio.Event(), asyncio.Event()
    requests: list[dict[str, Any]] = []
    primaries = 0

    async def sdk(**kwargs):
        nonlocal primaries
        requests.append(deepcopy(kwargs))
        ordinal = len(requests)
        assert ordinal <= (5 if mixed_order else 4), "the SDK script is finite"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}
        if "response_format" in kwargs:
            verdict = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
            if primaries >= 2:
                verdict["publication_verdict"] = "ADMIT"
            return _Response([_Choice(_Message(json.dumps(verdict)))], usage, model=kwargs["model"], id=f"a1-advisor-{ordinal}")
        primaries += 1
        if primaries == 1:
            assert "set_pipeline" in {tool["function"]["name"] for tool in kwargs["tools"]}
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            response = await authored(**kwargs)
            original = response.choices[0].message.tool_calls[0]
            calls = []
            for index in range(mutation_count):
                call = deepcopy(original)
                call.id = f"a1-governed-{index}"
                call.function.name = "set_pipeline"
                calls.append(call)
            response.choices[0].message.tool_calls = calls
            if mixed_order:
                by_name = {"set_pipeline": calls[0], "list_sources": _ToolCall("a1-discovery", _Function("list_sources", "{}"))}
                response.choices[0].message.tool_calls = [by_name[name] for name in mixed_order]
            response.id = "a1-primary-mutations"
            return response
        if mixed_order and primaries == 2:
            return _Response(
                [_Choice(_Message(None, [_ToolCall("a1-second-batch", _Function("get_pipeline_state", "{}"))]))],
                usage,
                id="a1-primary-inspect",
            )
        assert primaries == (3 if mixed_order else 2)
        return _Response([_Choice(_Message("The requested pipeline update is ready for review."))], usage, id="a1-primary-reply")

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    actual_p4 = _sessions.persist_compose_turn_async

    async def held_p4(**kwargs):
        if late_p4:
            assert any(row.composition_state_payload is not None for row in kwargs["redacted_tool_rows"])
            p4_entered.set()
            await asyncio.wait_for(p4_release.wait(), timeout=10)
        return await actual_p4(**kwargs)

    monkeypatch.setattr(_sessions, "persist_compose_turn_async", held_p4)
    observed = _observe_authority(monkeypatch, client.app.state.composer_service) if mixed_order else None
    created = client.post("/api/sessions", json={"title": "A1 initial authority witness"})
    assert created.status_code == 201, created.text
    holder["id"] = created.json()["id"]
    session_id = holder["id"]
    preferences_path = f"/api/sessions/{session_id}/composer/preferences"
    prefs = client.get(preferences_path)
    assert prefs.status_code == 200, prefs.text
    density = prefs.json()["density_default"]
    initial_mode = "auto_commit" if mixed_order or downgrade or late_p4 else "explicit_approve"
    initial = client.patch(preferences_path, json={"trust_mode": initial_mode, "density_default": density})
    assert initial.status_code == 200, initial.text
    assert initial.json()["trust_mode"] == initial_mode
    source_dir = tmp_path / "blobs" / session_id
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "input.csv").write_text("name\nAlice\n", encoding="utf-8")
    (tmp_path / "outputs" / session_id).mkdir(parents=True, exist_ok=True)

    async def observe(http, operation):
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            held = await http.get(preferences_path)
            assert held.status_code == 200 and held.json()["trust_mode"] == initial_mode, held.text
            if upgrade or downgrade:
                current_mode = "explicit_approve" if downgrade else "auto_commit"
                changed = await http.patch(preferences_path, json={"trust_mode": current_mode, "density_default": density})
                assert changed.status_code == 200, changed.text
                assert changed.json()["trust_mode"] == current_mode
            release.set()
            if late_p4:
                await asyncio.wait_for(p4_entered.wait(), timeout=10)
                changed = await http.patch(preferences_path, json={"trust_mode": "explicit_approve", "density_default": density})
                assert changed.status_code == 200, changed.text
                assert changed.json()["trust_mode"] == "explicit_approve"
                p4_release.set()
            await operation.drive
            final = await http.get(operation.poll_path)
            assert final.status_code == 200, final.text
            return final.json()
        finally:
            release.set()
            p4_release.set()

    terminal = running_operation_sync(
        client,
        client.app,
        path=f"/api/sessions/{session_id}/messages",
        body=message_body("Build a CSV to JSON pipeline from input.csv.", state_id=None),
        observe=observe,
    )
    with engine.connect() as conn:
        states = list(conn.execute(select(composition_states_table)).mappings())
        proposals = list(conn.execute(select(composition_proposals_table)).mappings())
        ledger = list(conn.execute(select(token_usage_ledger_table)).mappings())
        attempts = list(conn.execute(select(quota_provider_attempts_table)).mappings())
        users = list(conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "user")).mappings())
    audits = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    facts = {
        "upgrade": upgrade,
        "downgrade": downgrade,
        "late_p4": late_p4,
        "mutation_count": mutation_count,
        "terminal": terminal,
        "states": len(states),
        "proposals": len(proposals),
        "physical_requests": len(requests),
        "llm_audits": len(audits),
        "ledger": len(ledger),
        "attempts": len(attempts),
        "user_message_ids": [row["id"] for row in users],
    }
    print("A1_AUTHORITY_FACTS=" + json.dumps(facts, sort_keys=True))
    # Verify custody and positive evidence before the missing-authority assertion.
    assert len(users) == 1
    assert len(requests) == len(audits) == len(ledger) == len(attempts)
    assert all(call["provider_cost"] == 0.01 for call in audits)
    assert len({call["call_id"] for call in audits}) == len(audits)
    if mixed_order:
        assert terminal["status"] == "completed", terminal
        assert terminal["result"]["state"] is not None and proposals == []
        ceiling, evidence = observed["loop"][0]
        assert ceiling.request_id == evidence.request_id == users[0]["id"]
        assert ceiling.session_id == session_id
        assert len(observed["loop"]) == 1 and len(observed["p4"]) == 2
        assert all(item is ceiling for item in observed["p4"])
        assert observed["candidate"] == [ceiling]
        names = [name for name, _ in observed["worker"]]
        assert names == [*mixed_order, "get_pipeline_state"]
        for (name, selected), context in zip(observed["worker"], observed["contexts"], strict=True):
            expected = ceiling if name == "set_pipeline" else None
            assert selected is expected
            assert context.mutation_ceiling is expected
        assert all(item is ceiling for item in observed["batches"])
        assert len(observed["batches"]) == 2
    elif late_p4:
        assert p4_entered.is_set(), "the real P4 candidate boundary must be reached"
        assert terminal["status"] == "failed", terminal
        assert terminal["error"]["http_status"] == 409, terminal
        assert terminal["error"]["error_type"] == "composer_mutation_authority_revoked", terminal
        assert len(requests) == 2, "completed PRIMARY/EARLY remain; no later PRIMARY/END"
        assert states == [] and proposals == []
    elif mutation_count == 11:
        assert terminal["status"] == "failed", "the original explicit request must retain its proposal cap"
        assert terminal["error"]["http_status"] == 422, terminal
        assert len(requests) == 1
        assert states == [] and proposals == [], "over-cap review batch cannot publish effects"
    else:
        assert terminal["status"] == "completed", terminal
        assert states == [], "the request must obey both initial and current review restrictions"
        assert len(proposals) == 1
        assert proposals[0]["status"] == "pending"


def _observe_authority(monkeypatch, composer):
    from elspeth.web.composer import tool_batch, turn_audit
    from elspeth.web.composer.tools import _dispatch

    observed = {name: [] for name in ("loop", "batches", "candidate", "worker", "contexts", "p4")}
    actual_loop, actual_batch = composer._compose_loop, composer._dispatch_tool_batch
    actual_candidate, actual_worker = tool_batch._finalize_complete_set_pipeline_candidate, tool_batch.execute_tool
    actual_context, actual_p4 = _dispatch.ToolContext, turn_audit.persist_turn_audit

    async def loop(*args, **kwargs):
        observed["loop"].append((kwargs["mutation_ceiling"], kwargs["turn_evidence"]))
        return await actual_loop(*args, **kwargs)

    async def batch(**kwargs):
        observed["batches"].append(kwargs["mutation_ceiling"])
        return await actual_batch(**kwargs)

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

    monkeypatch.setattr(composer, "_compose_loop", loop)
    monkeypatch.setattr(composer, "_dispatch_tool_batch", batch)
    monkeypatch.setattr(tool_batch, "_finalize_complete_set_pipeline_candidate", candidate)
    monkeypatch.setattr(tool_batch, "execute_tool", worker)
    monkeypatch.setattr(_dispatch, "ToolContext", context)
    monkeypatch.setattr(turn_audit, "persist_turn_audit", p4)
    return observed


@pytest.mark.parametrize(
    "upgrade,mutation_count",
    [
        pytest.param(False, 11, id="stable-explicit-cap-control"),
        pytest.param(True, 1, id="upgrade-must-still-propose"),
        pytest.param(True, 11, id="upgrade-must-retain-proposal-cap"),
    ],
)
def test_initial_explicit_authority_is_not_widened(tmp_path, monkeypatch, upgrade, mutation_count):
    _exercise_request(tmp_path, monkeypatch, upgrade, mutation_count)


def test_initial_auto_authority_obeys_current_explicit_review(tmp_path, monkeypatch):
    _exercise_request(tmp_path, monkeypatch, False, 1, downgrade=True)


def test_late_p4_downgrade_refuses_actual_graph_publication(tmp_path, monkeypatch):
    _exercise_request(tmp_path, monkeypatch, False, 1, late_p4=True)


@pytest.mark.parametrize("order", [("set_pipeline", "list_sources"), ("list_sources", "set_pipeline")])
def test_mixed_batch_forwards_each_selected_automatic_authority(tmp_path, monkeypatch, order):
    _exercise_request(tmp_path, monkeypatch, False, 1, mixed_order=order)


def mutation_response(*, pipeline, ordinal):
    return _Response(
        choices=[
            _Choice(
                _Message(
                    None,
                    [
                        _ToolCall(
                            f"authority-set-pipeline-{ordinal}",
                            _Function("set_pipeline", json.dumps({"pipeline": pipeline})),
                        )
                    ],
                )
            )
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        model="provider/common-authority",
        id=f"authority-response-{ordinal}",
    )


def run_authority_case(
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
    explicit_v1=False,
    observed=None,
):
    entered, release = threading.Event(), threading.Event()
    requests, phases = [], []
    current_phase = None
    primary_count = 0
    selected_ceilings, lost_acks = [], []
    pipeline = None
    request_text = "Build a CSV to JSONL pipeline."
    expected_auto = (initial == "auto_commit" and (final == "auto_commit" or barrier == "p4_locked")) and not blob_only
    candidate_reply = (
        "The blob was saved." if blob_only else "The draft was saved." if expected_auto else "The proposal is waiting for approval."
    )
    mutation_ordinal = 2 if discovery_first else 1

    async def complete(**kwargs):
        nonlocal primary_count
        requests.append(deepcopy(kwargs))
        if kwargs["model"] == "test/planner":
            primary_count += 1
            assert primary_count <= mutation_ordinal + 1, "Unexpected extra primary call"
            names = {entry["function"]["name"] for entry in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            if discovery_first and primary_count == 1:
                return _Response(
                    choices=[
                        _Choice(
                            _Message(
                                None,
                                [
                                    _ToolCall(
                                        "authority-discovery-1",
                                        _Function(
                                            "get_plugin_schema",
                                            json.dumps(
                                                {
                                                    "plugin_type": "source",
                                                    "name": "csv",
                                                }
                                            ),
                                        ),
                                    )
                                ],
                            )
                        )
                    ],
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                    id="authority-discovery-response",
                )
            if primary_count == mutation_ordinal:
                if barrier in {"provider", "second_provider"}:
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 15)
                assert pipeline is not None
                if blob_only:
                    return _Response(
                        choices=[
                            _Choice(
                                _Message(
                                    None,
                                    [
                                        _ToolCall(
                                            "authority-create-blob-1",
                                            _Function(
                                                "create_blob",
                                                json.dumps(
                                                    {
                                                        "filename": "immediate.csv",
                                                        "mime_type": "text/csv",
                                                        "content": "value\n42\n",
                                                        "description": "Actual provider-created data",
                                                    }
                                                ),
                                            ),
                                        )
                                    ],
                                )
                            )
                        ],
                        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                        id="authority-create-response",
                    )
                result = mutation_response(pipeline=pipeline, ordinal=mutation_ordinal)
                if rootless_mixed:
                    inline_pipeline = deepcopy(pipeline)
                    inline_pipeline["source"]["options"].pop("path")
                    inline_pipeline["source"]["inline_blob"] = {
                        "filename": "refused.csv",
                        "mime_type": "text/csv",
                        "content": "value\n43\n",
                    }
                    result.choices[0].message.tool_calls.extend(
                        [
                            _ToolCall("authority-inline-refused", _Function("set_pipeline", json.dumps({"pipeline": inline_pipeline}))),
                            _ToolCall("authority-never-dispatch", _Function("list_sources", "{}")),
                        ]
                    )
                if batch_count > 1:
                    result.choices[0].message.tool_calls = [
                        _ToolCall(f"authority-set-pipeline-{n}", _Function("set_pipeline", json.dumps({"pipeline": pipeline})))
                        for n in range(1, batch_count + 1)
                    ]
                return result
            return _Response(
                choices=[_Choice(_Message(candidate_reply))],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
                id="authority-primary-2",
            )
        assert kwargs["model"] == composer._settings.composer_advisor_model
        assert current_phase in {"early", "end"}
        assert phases.count(current_phase) == 1, "Unexpected repeated checkpoint"
        assert kwargs.get("tools") is None and "response_format" in kwargs
        payload = {
            "verdict": "CLEAN",
            "category": "other",
            "steps": [],
            "findings": "The supplied evidence supports the stated draft outcome.",
            "note": None,
        }
        if current_phase == "end":
            review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            assert review["current_request"] == request_text
            assert review["candidate_reply"] == candidate_reply
            assert review["saved_effects"]["saved_revision_count"] == (1 if expected_auto else 0)
            payload["publication_verdict"] = "ADMIT"
        return _Response(
            choices=[_Choice(_Message(json.dumps(payload)))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model=kwargs["model"],
            id=f"authority-{current_phase}",
        )

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=complete)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Authority race"}).json()["id"]
    if observed is not None:
        observed.update(sessions=sessions, sid=sid)
    pipeline = _pipeline(tmp_path, sid)
    if inline:
        pipeline["source"] = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
            "inline_blob": {
                "filename": "candidate.csv",
                "mime_type": "text/csv",
                "content": "value\n42\n",
                "description": "Provider inline source",
            },
        }
    input_path = tmp_path / "blobs" / sid / "input.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
    # Existing graph fixtures are explicitly session_seed input. This write
    # is not attributed to the request under test or to a provider response.
    base_record = None
    if nonempty:
        base_record = asyncio.run(
            sessions.save_composition_state(
                UUID(sid),
                CompositionStateData(
                    sources={"source": deepcopy(pipeline["source"])},
                    nodes=[],
                    edges=[],
                    outputs=[{**deepcopy(pipeline["outputs"][0]), "name": "rows"}],
                    metadata_={"name": "Existing fixture", "description": ""},
                    is_valid=True,
                ),
                provenance="session_seed",
            )
        )
    with engine.connect() as connection:
        baseline_rows = (
            connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
        )
    body = {"content": request_text, "operation_id": str(uuid4()), "state_id": str(base_record.id) if base_record is not None else None}
    path = f"/api/sessions/{sid}/messages"
    if recompose:
        user = asyncio.run(sessions.add_message(UUID(sid), "user", request_text, writer_principal="route_user_message"))
        body = {"operation_id": body["operation_id"], "state_id": body["state_id"], "expected_user_message_id": str(user.id)}
        path = f"/api/sessions/{sid}/recompose"

    def change_mode(mode):
        response = client.patch(f"/api/sessions/{sid}/composer/preferences", json={"trust_mode": mode, "density_default": "high"})
        assert response.status_code == 200, response.text
        assert response.json()["trust_mode"] == mode

    change_mode(initial)
    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint

    async def observe_checkpoint(**kwargs):
        nonlocal current_phase
        current_phase = kwargs["phase"]
        phases.append(current_phase)
        return await original_checkpoint(**kwargs)

    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)
    original_p4 = sessions.persist_compose_turn_async

    async def held_p4(**kwargs):
        if observed is not None:
            observed["sql_input"] = kwargs
        if barrier == "p4":
            assert any(row.composition_state_payload is not None for row in kwargs["redacted_tool_rows"])
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
        return await original_p4(**kwargs)

    monkeypatch.setattr(sessions, "persist_compose_turn_async", held_p4)
    original_execute = tool_batch_module.execute_tool

    def held_tool(*args, **kwargs):
        selected_ceilings.append(kwargs.get("mutation_ceiling"))
        if barrier == "tool" and (not rootless_mixed or len(selected_ceilings) == 2):
            assert args[0] == ("create_blob" if blob_only else "set_pipeline")
            entered.set()
            assert release.wait(15)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", held_tool)
    original_insert = sessions._insert_composition_checkpoint

    def held_insert(*args, **kwargs):
        if barrier == "p4_locked":
            entered.set()
            assert release.wait(15)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(sessions, "_insert_composition_checkpoint", held_insert)
    preference_attempted = threading.Event()

    async def observe_pipeline_admission(
        request: Request,
        user: Annotated[UserIdentity, Depends(get_current_user)],
    ):
        if barrier == "p4_locked" and request.method == "PATCH" and request.url.path.endswith("/composer/preferences"):
            assert entered.is_set() and not release.is_set()
            preference_attempted.set()
        return await require_pipeline_user(request, user)

    client.app.dependency_overrides[require_pipeline_user] = observe_pipeline_admission
    repository = sessions.session_operation_authority
    actual_mutate = repository.mutate

    def lose_committed_create_ack(context, callback, *, auto_mutation=None):
        value = actual_mutate(context, callback, auto_mutation=auto_mutation)
        if create_ack_loss and not lost_acks and type(value) is BlobRecord and value.status == "ready":
            assert selected_ceilings and auto_mutation is selected_ceilings[-1]
            lost_acks.append(value)
            raise ConnectionError("actual activated inline-create acknowledgement lost")
        return value

    monkeypatch.setattr(repository, "mutate", lose_committed_create_ack)
    original_compose = composer.compose

    async def observe_compose(*args, **kwargs):
        try:
            return await original_compose(*args, **kwargs)
        except ComposerConvergenceError as error:
            if cap_errors is not None:
                cap_errors.append(error)
            raise

    monkeypatch.setattr(composer, "compose", observe_compose)
    original_module_p4 = composer._persist_turn_audit

    async def observe_module_p4(**kwargs):
        if observed is not None:
            observed["module_input"] = kwargs
        result = await original_module_p4(**kwargs)
        if observed is not None:
            observed["module_output"] = result
        return result

    monkeypatch.setattr(composer, "_persist_turn_audit", observe_module_p4)

    async def settle_explicit_v1(http):
        http.headers[COMPOSER_OPERATION_VERSION_HEADER] = "1"
        return await submit_and_settle(http, client.app, path=path, body=body)

    with ThreadPoolExecutor(max_workers=2) as executor:
        if explicit_v1:
            pending = executor.submit(client.run_in_one_loop, settle_explicit_v1)
        else:
            pending = executor.submit(settle_sync, client, client.app, path=path, body=body)
        try:
            assert entered.wait(15), "Actual selected owner was never reached"
            if barrier == "p4_locked":
                preference_change = executor.submit(change_mode, final)
                assert preference_attempted.wait(15)
                assert not preference_change.done()
                release.set()
                preference_change.result(timeout=15)
            else:
                change_mode(final)
        finally:
            release.set()
        settled = pending.result(timeout=30)
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
        operation = (
            conn.execute(select(composer_async_operations_table).where(composer_async_operations_table.c.session_id == sid))
            .mappings()
            .one()
        )
        if explicit_v1:
            assert settled.accepted.request.headers[COMPOSER_OPERATION_VERSION_HEADER] == "1"
            assert settled.accepted.headers[COMPOSER_OPERATION_VERSION_HEADER] == "1"
            assert operation["operation_id"] == body["operation_id"]
            assert operation["lifecycle_version"] == 1
        blobs = conn.execute(select(blobs_table).where(blobs_table.c.session_id == sid)).mappings().all()
        rejections = conn.execute(
            select(composition_rejection_events_table).where(composition_rejection_events_table.c.session_id == sid)
        ).all()
        journals = tuple(
            tuple(conn.execute(select(table).where(table.c.session_id == sid)).all())
            for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table)
        )
    old_ids = {row["id"] for row in baseline_rows}
    assert [row for row in states if row["id"] in old_ids] == baseline_rows
    states = [row for row in states if row["id"] not in old_ids]
    # These are actual SDK invocations: bind each physical provider response to
    # its durable call receipt and independently authenticated request sidecar.
    calls = [
        row["tool_calls"][0]["call"]
        for row in chat
        if row["role"] == "audit" and row["tool_calls"] and row["tool_calls"][0].get("_kind") == "llm_call_audit"
    ]
    assert len(calls) == sdk.await_count
    assert len({call["call_id"] for call in calls}) == len(calls)
    assert all(call["status"] == "success" for call in calls)
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
    for call in calls:
        evidence = requests_by_id[call["call_id"]]
        assert evidence.arguments.content is not None
        request = json.loads(evidence.arguments.content)
        assert request["capture_boundary"] and request["messages"]
    if inline or blob_only:
        expected_blobs = int(blob_only or barrier == "p4" or expected_auto)
        assert len(blobs) == expected_blobs
        for blob in blobs:
            assert blob["status"] == "ready"
            assert Path(blob["storage_path"]).read_bytes() == b"value\n42\n"
    assert bool(lost_acks) is create_ack_loss
    assert journals == ((), ())
    if barrier in {"tool", "p4"} and not blob_only:
        assert rejections == [], "An unseen refusal must not invent planner-feedback provenance"
    return settled, states, proposals, chat, phases, primary_count, sdk.await_count


def _assert_actual_graph_publications(result, states, chat, applied):
    """Distinguish P4's checkpoint from the existing route's final projection."""
    assert [row["provenance"] for row in states] == (["tool_call", "post_compose"] if applied else [])
    if not applied:
        return
    checkpoint, final = states
    assert final["version"] == checkpoint["version"] + 1
    for field in ("source", "sources", "nodes", "edges", "outputs", "metadata_"):
        assert final[field] == checkpoint[field]
    tool = next(row for row in chat if row["role"] == "tool" and row["tool_call_id"] == "authority-set-pipeline-1")
    assert tool["composition_state_id"] == checkpoint["id"]
    assert result["state"]["id"] == final["id"]
    assert result["message"]["composition_state_id"] == final["id"]
    assert result["message"]["id"] == chat[-1]["id"]
    assert result["message"]["segments"][-1]["kind"] == "trusted_system_notice"
    assert "covers 1 saved draft revisions" in result["message"]["segments"][-1]["content"]


@pytest.mark.parametrize(
    "initial,final,applied",
    [
        ("auto_commit", "auto_commit", True),
        ("explicit_approve", "explicit_approve", False),
        ("auto_commit", "explicit_approve", False),
        ("explicit_approve", "auto_commit", False),
    ],
)
def test_held_primary_preserves_initial_and_current_authority(tmp_path, monkeypatch, initial, final, applied):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial=initial, final=final, barrier="provider"
    )
    assert settled.final.status_code == 200, settled.final.text
    result = settled.result()
    _assert_actual_graph_publications(result, states, chat, applied)
    assert len(proposals) == int(not applied)
    assert all(row["status"] == "pending" for row in proposals)
    assert primary == 2
    assert phases == (["early", "end"] if applied else ["end"])
    assert physical == (4 if applied else 3)
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-set-pipeline-1"]
    assert tools[0]["composition_state_id"] == (states[0]["id"] if applied else None)


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
def test_same_authority_for_existing_graph_and_recompose(
    tmp_path,
    monkeypatch,
    nonempty,
    recompose,
    initial,
    final,
    applied,
):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial=initial, final=final, barrier="provider", nonempty=nonempty, recompose=recompose
    )
    result = settled.result()
    assert result["message"]["role"] == "assistant"
    _assert_actual_graph_publications(result, states, chat, applied)
    assert len(proposals) == int(not applied)
    assert all(row["status"] == "pending" for row in proposals)
    early = applied and not nonempty
    assert phases == (["early", "end"] if early else ["end"])
    assert primary == 2 and physical == 2 + len(phases)
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


def test_late_downgrade_refuses_graph_but_keeps_actual_p4_cohort(tmp_path, monkeypatch):
    observed = {}
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4", explicit_v1=True, observed=observed
    )
    assert states == [] and proposals == []
    status, body = settled.error()
    assert status == 409, body
    assert body["detail"]["error_type"] == "composer_mutation_authority_revoked"
    assert primary == 1 and phases == ["early"] and physical == 2
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
        == 2
    )
    # A later Advisor rebuilds history from these rows; the revoked graph must
    # replay as the recorded refusal, never as the success the model first saw.
    sessions, sid = observed["sessions"], observed["sid"]
    user = next(row for row in chat if row["role"] == "user")
    history = AdvisorTranscriptHistory.from_records(
        session_id=sid,
        actor_user_id="alice",
        current_request_id=str(user["id"]),
        request=user["content"],
        records=asyncio.run(sessions.get_messages(UUID(sid))),
    )
    owner = AdvisorTranscriptOwner(request=user["content"], history=history)
    _assert_revoked_graph_replays_as_refusal(_revoked_graph_history_event(owner.snapshot().provider_json))
    chunk = encode_message_shelf(owner.snapshot_for_continuation([]))
    cold, _ = AdvisorTranscriptOwner.for_continuation(
        decode_message_shelf(PrivateChunk(chunk.reference, chunk.canonical)),
        session_id=sid,
        actor_user_id="alice",
        request_id=str(user["id"]),
        request=user["content"],
    )
    _assert_revoked_graph_replays_as_refusal(_revoked_graph_history_event(cold.snapshot().provider_json))


def test_graph_commit_precedes_later_preference_revocation(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4_locked"
    )
    assert settled.final.status_code == 200, settled.final.text
    result = settled.result()
    _assert_actual_graph_publications(result, states, chat, True)
    assert proposals == []
    assert primary == 2 and physical == 4 and phases == ["early", "end"]
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["composition_state_id"] == states[0]["id"]
    assert json.loads(tool["content"])["success"] is True


def test_upgrade_between_iterations_does_not_reauthorize(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="explicit_approve", final="auto_commit", barrier="second_provider", discovery_first=True
    )
    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    assert states == [] and len(proposals) == 1 and proposals[0]["status"] == "pending"
    assert phases == ["end"] and primary == 3 and physical == 4
    assert [row["tool_call_id"] for row in chat if row["role"] == "tool"] == ["authority-discovery-1", "authority-set-pipeline-2"]


def test_upgraded_request_still_uses_pending_proposal_cap(tmp_path, monkeypatch):
    cap_errors = []
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="explicit_approve", final="auto_commit", barrier="provider", batch_count=11, cap_errors=cap_errors
    )
    status, body = settled.error()
    assert status == 422, body
    detail = body["detail"]
    assert detail["reason"] == "convergence_composition_budget"
    assert len(cap_errors) == 1
    assert cap_errors[0].evidence == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
    assert len(cap_errors[0].llm_calls) == 1
    assert states == [] and proposals == []
    assert not [row for row in chat if row["role"] == "tool"]
    assert primary == physical == 1 and phases == []


@pytest.mark.parametrize(
    "barrier,physical,phases",
    [
        ("tool", 1, []),
        ("p4", 2, ["early"]),
    ],
)
def test_inline_source_respects_custody_and_retains_actual_prefix(tmp_path, monkeypatch, barrier, physical, phases):
    settled, states, proposals, chat, actual_phases, primary, actual_physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier=barrier, inline=True
    )
    status, body = settled.error()
    assert status == 409, body
    assert states == [] and proposals == []
    assert primary == 1 and actual_physical == physical and actual_phases == phases
    tools = [row for row in chat if row["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["composition_state_id"] is None
    assert json.loads(tools[0]["content"])["success"] is False


def test_create_blob_remains_immediate_after_downgrade(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="tool", blob_only=True
    )
    assert settled.final.status_code == 200, settled.final.text
    settled.result()
    assert states == [] and proposals == []
    assert primary == 2 and physical == 3 and phases == ["end"]
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["authority-create-blob-1"]
    assert json.loads(tools[0]["content"])["success"] is True


def test_activated_inline_create_reconciles_lost_ack_before_p4_refusal(tmp_path, monkeypatch):
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="p4", inline=True, create_ack_loss=True
    )
    assert settled.error()[0] == 409
    assert states == proposals == []
    assert primary == 1 and physical == 2 and phases == ["early"]
    tool = next(row for row in chat if row["role"] == "tool")
    assert tool["composition_state_id"] is None
    payload = json.loads(tool["content"])
    assert payload["success"] is False and payload["validation"]["errors"][0]["error_code"] == "auto_mutation_revoked"


def test_rootless_mixed_refusal_skips_early_and_refuses_earlier_candidate(tmp_path, monkeypatch):
    observed = {}
    settled, states, proposals, chat, phases, primary, physical = run_authority_case(
        tmp_path, monkeypatch, initial="auto_commit", final="explicit_approve", barrier="tool", rootless_mixed=True, observed=observed
    )
    assert states == proposals == []
    assert settled.error()[0] == 409
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


@pytest.mark.parametrize("tool", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("final_mode,ack_loss", [("auto_commit", False), ("explicit_approve", False), ("auto_commit", True)])
def test_approval_governed_blob_custody_refuses_late_downgrade(tmp_path, monkeypatch, tool, final_mode, ack_loss):
    entered, release = threading.Event(), threading.Event()
    primary_count = 0
    physical, phases, executed = [], [], []
    created = {}
    phase = None
    selected_ceilings, lost_acks = [], []

    async def complete(**kwargs):
        nonlocal primary_count
        physical.append(deepcopy(kwargs))
        if kwargs["model"] == "test/planner":
            primary_count += 1
            assert primary_count <= 3, "Blob custody triggered an extra primary request"
            if primary_count == 1:
                message = _Message(
                    None,
                    [
                        _ToolCall(
                            "blob-create-actual",
                            _Function(
                                "create_blob",
                                json.dumps(
                                    {
                                        "filename": "owned.csv",
                                        "mime_type": "text/csv",
                                        "content": "value\n42\n",
                                        "description": "Provider-created custody control",
                                    }
                                ),
                            ),
                        )
                    ],
                )
            elif primary_count == 2:
                arguments = {"blob_id": created["blob_id"]}
                if tool == "update_blob":
                    arguments["content"] = "value\n43\n"
                message = _Message(None, [_ToolCall("blob-change-actual", _Function(tool, json.dumps(arguments)))])
            else:
                assert final_mode == "auto_commit"
                message = _Message("The requested blob change is saved.")
            return _Response(
                choices=[_Choice(message)],
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.01,
                },
                model=kwargs["model"],
                id=f"blob-primary-{primary_count}",
            )
        assert kwargs["model"] == composer._settings.composer_advisor_model
        assert phase == "end" and phases == ["end"]
        assert final_mode == "auto_commit"
        assert kwargs.get("tools") is None and "response_format" in kwargs
        return _Response(
            choices=[
                _Choice(
                    _Message(
                        json.dumps(
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
            model=kwargs["model"],
            id="blob-end-1",
        )

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=complete)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Actual blob custody"}).json()["id"]

    def journals():
        with engine.connect() as connection:
            return tuple(
                tuple(connection.execute(select(table).where(table.c.session_id == sid).order_by(*table.primary_key.columns)).all())
                for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table)
            )

    response = client.patch(f"/api/sessions/{sid}/composer/preferences", json={"trust_mode": "auto_commit", "density_default": "high"})
    assert response.status_code == 200
    original_execute = tool_batch_module.execute_tool

    def execute(name, *args, **kwargs):
        executed.append(name)
        if name == tool:
            selected_ceilings.append(kwargs["mutation_ceiling"])
            entered.set()
            assert release.wait(15)
        result = original_execute(name, *args, **kwargs)
        if name == "create_blob":
            assert result.success
            created["blob_id"] = result.data["blob_id"]
        return result

    original_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint

    async def checkpoint(**kwargs):
        nonlocal phase
        phase = kwargs["phase"]
        phases.append(phase)
        return await original_checkpoint(**kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", execute)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", checkpoint)
    repository = sessions.session_operation_authority
    actual_mutate = repository.mutate

    def lose_committed_change_ack(context, callback, *, auto_mutation=None):
        value = actual_mutate(context, callback, auto_mutation=auto_mutation)
        selected = (tool == "update_blob" and type(value) is BlobReplacementPlan and value.phase == "purge_pending") or (
            tool == "delete_blob" and type(value) is BlobDeletionPlan and value.phase == "purge_pending"
        )
        if ack_loss and selected and not lost_acks:
            assert selected_ceilings and auto_mutation is selected_ceilings[-1]
            lost_acks.append(value)
            raise ConnectionError("actual activated blob-change acknowledgement lost")
        return value

    monkeypatch.setattr(repository, "mutate", lose_committed_change_ack)
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(
            settle_sync,
            client,
            client.app,
            path=f"/api/sessions/{sid}/messages",
            body={
                "content": "Create the CSV then apply the requested blob change.",
                "state_id": None,
                "operation_id": str(uuid4()),
            },
        )
        try:
            assert entered.wait(15), "Real blob change was never dispatched"
            with engine.connect() as connection:
                before = connection.execute(select(blobs_table).where(blobs_table.c.id == created["blob_id"])).mappings().one()
            original_bytes = Path(before["storage_path"]).read_bytes()
            before_journals = journals()
            assert original_bytes == b"value\n42\n"
            response = client.patch(f"/api/sessions/{sid}/composer/preferences", json={"trust_mode": final_mode, "density_default": "high"})
            assert response.status_code == 200
        finally:
            release.set()
        settled = task.result(timeout=30)
    with engine.connect() as connection:
        after = connection.execute(select(blobs_table).where(blobs_table.c.id == created["blob_id"])).mappings().one_or_none()
        states = connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).all()
        proposals = connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).all()
        chat = (
            connection.execute(
                select(chat_messages_table).where(chat_messages_table.c.session_id == sid).order_by(chat_messages_table.c.sequence_no)
            )
            .mappings()
            .all()
        )
    assert states == proposals == []
    assert executed == ["create_blob", tool]
    assert sdk.await_count == len(physical)
    audit_calls = [envelope["call"] for row in chat for envelope in row["tool_calls"] or () if envelope.get("_kind") == "llm_call_audit"]
    assert len(audit_calls) == len(physical)
    assert len({call["call_id"] for call in audit_calls}) == len(physical)
    tools = [row for row in chat if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == ["blob-create-actual", "blob-change-actual"]
    assert all(row["composition_state_id"] is None for row in tools)
    assert bool(lost_acks) is ack_loss
    if final_mode == "explicit_approve":
        assert after == before
        assert journals() == before_journals
        assert Path(before["storage_path"]).read_bytes() == original_bytes
        status, body = settled.error()
        assert status == 409 and body["detail"]["error_type"] == "composer_mutation_authority_revoked"
        assert primary_count == len(physical) == 2 and phases == []
        assert "auto_mutation_revoked" in tools[-1]["content"]
    else:
        assert settled.result()
        assert primary_count == 3 and phases == ["end"] and len(physical) == 4
        assert journals() == ((), ())
        if tool == "update_blob":
            assert after is not None and after["status"] == "ready"
            assert after["content_hash"] != before["content_hash"]
            assert Path(after["storage_path"]).read_bytes() == b"value\n43\n"
        else:
            assert after is None
            assert not Path(before["storage_path"]).exists()


def test_sticky_refusal_selects_entire_mixed_cohort_and_preserves_prior_checkpoint(tmp_path, monkeypatch):
    from elspeth.contracts.composer_mutation import ComposerAutoMutationRefused
    from elspeth.web.composer.visible_tool_event import VisibleToolEvent

    holder, created = {}, {}
    authored = _valid_pipeline_completion(tmp_path, holder)
    worker_entered, worker_release = threading.Event(), threading.Event()
    p4_entered, p4_release = asyncio.Event(), asyncio.Event()
    requests, phases, executed, batches, p4_inputs, p4_outcomes, refusals, public_errors, module_outcomes = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    raw_candidate = "PRIVATE second-batch graph candidate prose"
    primary_count = 0
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}

    def create_call(call_id, value):
        return _ToolCall(
            call_id,
            _Function("create_blob", json.dumps({"filename": f"{call_id}.csv", "mime_type": "text/csv", "content": f"value\n{value}\n"})),
        )

    async def complete(**kwargs):
        nonlocal primary_count
        requests.append(deepcopy(kwargs))
        assert len(requests) <= 3, "Refusal must stop later PRIMARY, EARLY and END dispatch"
        if "response_format" in kwargs:
            assert phases == ["early"] and primary_count == 1
            return _Response(
                [_Choice(_Message(json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None})))],
                usage,
                id="sticky-early",
            )
        primary_count += 1
        if primary_count == 1:
            response = await authored(**kwargs)
            response.choices[0].message.tool_calls[0].function.name = "set_pipeline"
            response.choices[0].message.tool_calls[0].id = "sticky-prefix-graph"
            response.choices[0].message.tool_calls.insert(0, create_call("sticky-prefix-blob", 42))
            response.id = "sticky-primary-1"
            return response
        assert primary_count == 2 and "sticky-prefix-blob" in created
        calls = [
            _ToolCall("sticky-graph-a", _Function("set_metadata", json.dumps({"patch": {"name": "PRIVATE candidate A"}}))),
            create_call("sticky-independent-blob", 99),
            _ToolCall("sticky-graph-b", _Function("set_metadata", json.dumps({"patch": {"name": "PRIVATE candidate B"}}))),
            _ToolCall(
                "sticky-refused-update",
                _Function("update_blob", json.dumps({"blob_id": created["sticky-prefix-blob"], "content": "value\n43\n"})),
            ),
            _ToolCall("sticky-never-dispatch", _Function("list_sources", "{}")),
        ]
        return _Response([_Choice(_Message(raw_candidate, calls))], usage, id="sticky-primary-2")

    sdk = AsyncMock(spec=litellm.acompletion, side_effect=complete)
    client, engine, sessions = _build_app(tmp_path, monkeypatch, sdk)
    composer = client.app.state.composer_service
    sid = client.post("/api/sessions", json={"title": "Sticky real mixed cohort"}).json()["id"]
    holder["id"] = sid
    directory = tmp_path / "blobs" / sid
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.csv").write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / sid).mkdir(parents=True, exist_ok=True)
    preference_path = f"/api/sessions/{sid}/composer/preferences"
    assert client.patch(preference_path, json={"trust_mode": "auto_commit", "density_default": "high"}).status_code == 200
    actual_execute, actual_batch = tool_batch_module.execute_tool, composer._dispatch_tool_batch
    actual_p4, actual_compose = sessions.persist_compose_turn_async, composer.compose
    actual_module_p4 = composer._persist_turn_audit
    actual_checkpoint = composer._advisor_checkpoint._run_advisor_checkpoint

    def execute(name, arguments, *args, **kwargs):
        executed.append((name, kwargs["mutation_ceiling"]))
        if name == "update_blob":
            worker_entered.set()
            assert worker_release.wait(15)
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
        batches.append((kwargs["state"], kwargs["mutation_ceiling"]))
        return await actual_batch(**kwargs)

    async def p4(**kwargs):
        p4_inputs.append(kwargs)
        if len(p4_inputs) == 2:
            assert kwargs["authority_revocation"] is refusals[0]
            p4_entered.set()
            await asyncio.wait_for(p4_release.wait(), 15)
        result = await actual_p4(**kwargs)
        p4_outcomes.append(result)
        return result

    async def compose(*args, **kwargs):
        try:
            return await actual_compose(*args, **kwargs)
        except ComposerMutationAuthorityRevoked as error:
            public_errors.append(error)
            raise

    async def module_p4(**kwargs):
        result = await actual_module_p4(**kwargs)
        module_outcomes.append(result)
        return result

    async def checkpoint(**kwargs):
        phases.append(kwargs["phase"])
        return await actual_checkpoint(**kwargs)

    monkeypatch.setattr(tool_batch_module, "execute_tool", execute)
    monkeypatch.setattr(composer, "_dispatch_tool_batch", batch)
    monkeypatch.setattr(sessions, "persist_compose_turn_async", p4)
    monkeypatch.setattr(composer, "compose", compose)
    monkeypatch.setattr(composer, "_persist_turn_audit", module_p4)
    monkeypatch.setattr(composer._advisor_checkpoint, "_run_advisor_checkpoint", checkpoint)
    prefix = []

    async def observe(http, operation):
        try:
            assert await asyncio.to_thread(worker_entered.wait, 15)
            with engine.connect() as connection:
                prefix.extend(
                    connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings()
                )
            assert len(prefix) == 1 and prefix[0]["provenance"] == "tool_call"
            response = await http.patch(preference_path, json={"trust_mode": "explicit_approve", "density_default": "high"})
            assert response.status_code == 200 and response.json()["trust_mode"] == "explicit_approve"
            worker_release.set()
            await asyncio.wait_for(p4_entered.wait(), 15)
            response = await http.patch(preference_path, json={"trust_mode": "auto_commit", "density_default": "high"})
            assert response.status_code == 200 and response.json()["trust_mode"] == "auto_commit"
            p4_release.set()
            await operation.drive
            response = await http.get(operation.poll_path)
            assert response.status_code == 200
            return response.json()
        finally:
            worker_release.set()
            p4_release.set()

    terminal = running_operation_sync(
        client,
        client.app,
        path=f"/api/sessions/{sid}/messages",
        body=message_body("Build the CSV draft then apply the requested changes."),
        observe=observe,
    )
    with engine.connect() as connection:
        states = connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).mappings().all()
        chat = (
            connection.execute(
                select(chat_messages_table).where(chat_messages_table.c.session_id == sid).order_by(chat_messages_table.c.sequence_no)
            )
            .mappings()
            .all()
        )
        blobs = connection.execute(select(blobs_table).where(blobs_table.c.session_id == sid)).mappings().all()
        proposals = connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).all()
        rejections = connection.execute(
            select(composition_rejection_events_table).where(composition_rejection_events_table.c.session_id == sid)
        ).all()
        attempts = (
            connection.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == sid))
            .mappings()
            .all()
        )
        ledger = connection.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == sid)).mappings().all()
        journals = tuple(
            tuple(connection.execute(select(table).where(table.c.session_id == sid)).all())
            for table in (blob_deletion_cleanups_table, blob_replacement_cleanups_table)
        )
    assert states == prefix and proposals == rejections == [] and journals == ((), ())
    assert terminal["status"] == "failed" and terminal["error"]["http_status"] == 409
    assert terminal["error"]["error_type"] == "composer_mutation_authority_revoked"
    assert "PRIVATE" not in json.dumps(terminal)
    history = client.get(f"/api/sessions/{sid}/operations", params={"operation_id": terminal["operation_id"]})
    assert history.status_code == 200
    (failure_outcome,) = history.json()["items"]
    assert failure_outcome["saved_revisions"] == {
        "attribution": "fenced",
        "count": 1,
        "latest_state_id": prefix[0]["id"],
        "latest_version": prefix[0]["version"],
    }
    assert failure_outcome["error_type"] == "composer_mutation_authority_revoked"
    assert phases == ["early"] and primary_count == 2 and sdk.await_count == len(requests) == 3
    calls = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    assert len(calls) == len(attempts) == len(ledger) == 3
    assert {call["call_id"] for call in calls} == {row["attempt_id"] for row in attempts}
    assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in ledger}
    assert {call["provider_request_id"] for call in calls} == {"sticky-primary-1", "sticky-primary-2", "sticky-early"}
    assert all(call["provider_cost"] == 0.01 and call["prompt_tokens"] == 10 and call["completion_tokens"] == 5 for call in calls)
    assert len(batches) == len(p4_inputs) == len(p4_outcomes) == 2
    ceiling = batches[0][1]
    assert batches[1][1] is ceiling and refusals[0].ceiling is ceiling
    assert p4_outcomes[1].authority_revocation is refusals[0]
    assert public_errors[0].revocation is refusals[0] and public_errors[0].partial_state is batches[1][0]
    assert p4_outcomes[1].current_state_id == prefix[0]["id"] and p4_outcomes[1].checkpoint_disposition == "no_new_checkpoint"
    assert p4_outcomes[1].published_assistant_content == ""
    assert module_outcomes[1].audit_outcome is p4_outcomes[1]
    assert module_outcomes[1].redacted_tool_rows is p4_outcomes[1].published_tool_rows
    assert module_outcomes[1].persisted_assistant_content == p4_outcomes[1].published_assistant_content
    assert module_outcomes[1].authority_revocation is refusals[0]
    assert module_outcomes[1].persisted_assistant_matches_current_dispatch is False
    expected_tools = ["create_blob", "set_pipeline", "set_metadata", "create_blob", "set_metadata", "update_blob"]
    assert [name for name, _ in executed] == expected_tools
    assert all(selected is (None if name == "create_blob" else ceiling) for name, selected in executed)
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
    print(
        "STICKY_MIXED_FACTS="
        + json.dumps(
            {
                "states": len(states),
                "tools": len(tools),
                "blobs": len(blobs),
                "physical": len(requests),
                "audits": len(calls),
                "attempts": len(attempts),
                "ledger": len(ledger),
                "executed": expected_tools,
                "saved_revisions": failure_outcome["saved_revisions"],
            },
            sort_keys=True,
        )
    )
