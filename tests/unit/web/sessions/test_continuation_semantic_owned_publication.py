"""Actual classified recovery, original paid Stop and cold source publication."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_semantic_advisor_phase as semantic_phase
from elspeth.web.composer.continuation_progress import decode_continuation_frontier
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_iteration_return import read_original_owned_iteration_return_on_connection
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.schemas import MessageWithStateResponse
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    effects,
    install_sdk,
    observe_original_driver_failures,
    semantic_primary_response,
    semantic_review_response,
    submit_and_settle,
    tool_call,
)


@asynccontextmanager
async def actual_owned_recovery_case(tmp_path, monkeypatch):
    """One canonical app, genuine P4 and source-derived semantic SDK reply.

    The composition cap is part of original admission. The first real model
    mutates metadata, so the original classifier takes its last-chance PRIMARY;
    that real provider response requests recovery rather than an END approval.
    """
    operation_id = str(uuid4())
    reply = "I have parked the named draft so we can continue later."
    recovery_arguments = {"action": "park", "message": reply}
    observed_inputs = []
    app_ref = []

    def stop(request):
        answer = semantic_primary_response(request)
        retained = json.loads(request["messages"][-1]["content"])
        original = retained["iteration_return"]
        assert original["schema_version"] == "composer.continuation-result.v1"
        assert original["graph_recovery_intent"] == {
            "action": "park",
            "message": reply,
            "park_id": None,
            "tool_call_id": "owned-park",
        }
        assert original["message"] == reply and original["pipeline_commit_intent"] is None
        observed_inputs.append(original)
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
            reason="The original named draft and provider-authored park request complete the user's request.",
        )
        return answer

    sdk = ScriptedSDK(
        [
            (
                "primary",
                lambda app: (
                    "I will name the draft before parking it.",
                    [tool_call(app, "name-draft", "set_metadata", {"patch": {"name": "Named recovery draft"}})],
                ),
            ),
            (
                "primary",
                (
                    None,
                    [
                        {
                            "id": "owned-park",
                            "type": "function",
                            "function": {"name": "handoff_to_user", "arguments": canonical_json(recovery_arguments)},
                        }
                    ],
                ),
            ),
            ("semantic", stop),
        ],
        app_ref=app_ref,
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch, composer_max_composition_turns=1) as harness:
        app_ref.append(harness.app)
        yield harness, sdk, operation_id, reply, recovery_arguments, observed_inputs


@pytest.mark.asyncio
async def test_actual_classifier_recovery_requires_paid_stop_and_replays_original_cold_source(tmp_path, monkeypatch):
    async with actual_owned_recovery_case(tmp_path, monkeypatch) as case:
        harness, sdk, operation_id, reply, recovery_arguments, observed_inputs = case
        with monkeypatch.context() as diagnostic_patch:
            original_failures = observe_original_driver_failures(diagnostic_patch, operation_id, session_id=harness.session_id)
            snapshot = await submit_and_settle(
                harness,
                message_body("Name the draft 'Named recovery draft', then park it and finish this request.", operation_id=operation_id),
            )
        if snapshot.status != "completed":
            print("classifier original driver failures:", json.dumps(original_failures[-12:], sort_keys=True))
        assert snapshot.status == "completed", (snapshot.error, sdk.roles, original_failures[-12:])
        assert sdk.roles == ["primary", "primary", "semantic"] and not sdk.steps
        assert len(observed_inputs) == 1
        record = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert record is not None and record.result_json is not None
        response = MessageWithStateResponse.model_validate_json(record.result_json)
        assert response.message.content == reply
        assert response.recovery_notice is not None and "parked" in response.recovery_notice.content

        by_key = {row.effect_key: row for row in effects(harness.app, operation_id)}
        classifier, paid_stop = by_key["provider:0:1"], by_key["provider:1:0"]
        assert classifier.disposition == paid_stop.disposition == "committed"
        assert all(row.owner != "composer_end" for row in by_key.values())
        engine = harness.app.state.session_engine
        with engine.connect() as conn:
            classifier_attempt = conn.execute(select(physical).where(physical.c.attempt_id == classifier.provider_attempt_id)).one()
            attempts = conn.execute(
                select(physical).where(physical.c.session_id == str(harness.session_id), physical.c.source == "composer")
            ).all()
            assert len(attempts) == 3 and all(row.settled_at is not None for row in attempts)
            checkpoints = conn.execute(
                select(events.c.sequence, events.c.payload_chunk_key).where(
                    events.c.session_id == str(harness.session_id),
                    events.c.operation_id == operation_id,
                    events.c.tag == "checkpoint",
                )
            ).all()
            stopped = [
                row
                for row in checkpoints
                if json.loads(
                    read_chunk(conn, session_id=str(harness.session_id), operation_id=operation_id, key=row.payload_chunk_key)
                ).get("kind")
                == "semantic_iteration_owned_stop_ready"
            ]
            assert len(stopped) == 1
            selected = semantic_sql._historical_snapshot(conn, str(harness.session_id), operation_id, cursor=stopped[0].sequence)
            proof = semantic_sql._semantic_owned_iteration_publication_data(conn, str(harness.session_id), operation_id, selected)
            assert proof is not None and proof.outcome_kind == "owned_recovery" and proof.source_kind == "graph_recovery"
            assert proof.returned.terminal_kind == "owned_recovery" and proof.returned.end_disposition is None
            assert proof.returned.end_pass_message_id is None and proof.returned.end_pass_index is None
            assert proof.returned.result.pipeline_commit_intent is None and proof.returned.publication_binding is None
            original = read_original_owned_iteration_return_on_connection(
                conn, session_id=harness.session_id, operation_id=operation_id, effect_key=proof.returned.owner_effect_key
            )
            assert original.kind == "graph_recovery" and original.returned == proof.returned
            assert original.recovery_context == proof.recovery_context
            assert proof.admission.paid.audit_call.call_id == paid_stop.provider_attempt_id
            assert len(sdk.requests) == 3

        messages = await harness.app.state.session_service.get_messages(harness.session_id, limit=None, include_private_evidence=True)
        controls = [
            ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(harness.session_id))
            for row in messages
            if row.tool_calls and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
        ]
        (control,) = [item for item in controls if item.tool_name == "handoff_to_user"]
        assert json.loads(control.arguments.content) == recovery_arguments
        facts = json.loads(control.result.content)
        assert facts["provider_tool_call_id"] == "owned-park" and facts["physical_call_id"] == classifier.provider_attempt_id
        assert control.operation_id == classifier_attempt.operation_id and control.operation_epoch == classifier_attempt.operation_epoch


def _ordinary_recovery_stop(request, *, reply: str):
    retained = json.loads(request["messages"][-1]["content"])
    candidate = retained["end_candidate"]
    assert candidate["kind"] == "recovery" and candidate["candidate_text"] == reply
    assert retained.get("iteration_return") is None
    frontier = decode_continuation_frontier(retained["frontier"])
    answer = semantic_primary_response(request)
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
        reason="The provider-authored handoff answers the whole original request.",
    )
    return answer


@pytest.mark.asyncio
async def test_actual_ordinary_primary_recovery_requires_paid_stop_and_cold_source(tmp_path, monkeypatch):
    operation_id = str(uuid4())
    reply = "I have parked the draft so we can continue later."
    recovery_arguments = {"action": "park", "message": reply}
    sdk = ScriptedSDK(
        [
            (
                "primary",
                (
                    None,
                    [
                        {
                            "id": "ordinary-park",
                            "type": "function",
                            "function": {"name": "handoff_to_user", "arguments": canonical_json(recovery_arguments)},
                        }
                    ],
                ),
            ),
            ("semantic", lambda request: _ordinary_recovery_stop(request, reply=reply)),
        ]
    )
    install_sdk(monkeypatch, sdk)
    actual_scope = semantic_phase.v4_preparation_memo_scope
    actual_sdk = litellm.acompletion
    carriers = []
    sdk_calls = 0

    @contextmanager
    def observed_scope(clock):
        with actual_scope(clock) as memo:
            carriers.append(memo)
            yield memo

    async def observed_sdk(**options):
        nonlocal sdk_calls
        sdk_calls += 1
        if sdk_calls == 2:
            assert len(carriers) == 1 and not carriers[0].active
        return await actual_sdk(**options)

    monkeypatch.setattr(semantic_phase, "v4_preparation_memo_scope", observed_scope)
    monkeypatch.setattr(litellm, "acompletion", observed_sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        with monkeypatch.context() as diagnostic_patch:
            observed = observe_original_driver_failures(diagnostic_patch, operation_id, session_id=harness.session_id)
            snapshot = await submit_and_settle(
                harness,
                message_body("Park this draft and tell me when it is ready to resume.", operation_id=operation_id),
            )
        if snapshot.status != "completed":
            failures = [
                {"boundary": row["boundary"], "cursor": row["cursor"], "original": row["original"]} for row in observed if "original" in row
            ]
            print("ordinary recovery original driver failures:", json.dumps(failures, sort_keys=True))
        assert snapshot.status == "completed", (snapshot.error, sdk.roles)
        assert sdk.roles == ["primary", "semantic"] and not sdk.steps
        assert sdk_calls == 2 and len(carriers) == 1 and not carriers[0].active
        record = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation_id)
        assert record is not None and record.result_json is not None
        response = MessageWithStateResponse.model_validate_json(record.result_json)
        assert response.message.content == reply and response.recovery_notice is not None
        by_key = {row.effect_key: row for row in effects(harness.app, operation_id)}
        assert by_key["provider:0:0"].disposition == by_key["provider:1:0"].disposition == "committed"
        assert all(row.owner != "composer_end" for row in by_key.values())
        engine = harness.app.state.session_engine
        with engine.connect() as conn:
            attempts = conn.execute(
                select(physical).where(physical.c.session_id == str(harness.session_id), physical.c.source == "composer")
            ).all()
            assert len(attempts) == 2 and all(row.settled_at is not None for row in attempts)
            checkpoints = conn.execute(
                select(events.c.sequence, events.c.payload_chunk_key).where(
                    events.c.session_id == str(harness.session_id),
                    events.c.operation_id == operation_id,
                    events.c.tag == "checkpoint",
                )
            ).all()
            stops = [
                row
                for row in checkpoints
                if json.loads(
                    read_chunk(conn, session_id=str(harness.session_id), operation_id=operation_id, key=row.payload_chunk_key)
                ).get("kind")
                == "semantic_ordinary_recovery_stop_ready"
            ]
            assert len(stops) == 1
            selected = semantic_sql._historical_snapshot(conn, str(harness.session_id), operation_id, cursor=stops[0].sequence)
            proof = semantic_sql._semantic_ordinary_recovery_publication_data(conn, str(harness.session_id), operation_id, selected)
            assert proof is not None and proof.result.graph_recovery_intent is not None
            assert proof.result.graph_recovery_intent.tool_call_id == "ordinary-park"
            assert proof.admission.paid.audit_call.call_id == by_key["provider:1:0"].provider_attempt_id

            prior = next(row for row in checkpoints if row.sequence < stops[0].sequence)
            conn.execute(
                update(events)
                .where(
                    events.c.session_id == str(harness.session_id),
                    events.c.operation_id == operation_id,
                    events.c.sequence == stops[0].sequence,
                )
                .values(payload_chunk_key=prior.payload_chunk_key)
            )
            with pytest.raises(AuditIntegrityError):
                semantic_sql._semantic_ordinary_recovery_publication_data(conn, str(harness.session_id), operation_id, selected)
            conn.rollback()


@pytest.mark.asyncio
async def test_ordinary_primary_recovery_refuses_review_without_second_paid_primary(tmp_path, monkeypatch):
    operation_id = str(uuid4())
    sdk = ScriptedSDK(
        [
            (
                "primary",
                (
                    None,
                    [
                        {
                            "id": "ordinary-park",
                            "type": "function",
                            "function": {
                                "name": "handoff_to_user",
                                "arguments": canonical_json({"action": "park", "message": "I have parked the draft."}),
                            },
                        }
                    ],
                ),
            ),
            ("semantic", semantic_review_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        snapshot = await submit_and_settle(
            harness,
            message_body("Park this draft and tell me when it is ready to resume.", operation_id=operation_id),
        )
        assert snapshot.status == "failed", (snapshot.error, sdk.roles)
        assert sdk.roles == ["primary", "semantic"] and not sdk.steps
        assert all(row.owner != "composer_end" for row in effects(harness.app, operation_id))
