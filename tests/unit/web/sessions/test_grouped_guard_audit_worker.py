"""Real worker audit custody for grouped advisor guard/integrity failures."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import litellm
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_states_table,
    quota_provider_attempts_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.unit.web.composer.test_service import _create_session_blob_for_test
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft, _TestSnapshotFactory


def _leaves(failure):
    if isinstance(failure, BaseExceptionGroup):
        return tuple(leaf for member in failure.exceptions for leaf in _leaves(member))
    return (failure,)


def _call_envelopes(rows):
    return tuple(
        (row, envelope["call"])
        for row in rows
        if row.role == "audit"
        for envelope in (row.tool_calls or ())
        if envelope["_kind"] == "llm_call_audit"
    )


@pytest.fixture
def real_route_app(tmp_path):
    app, sessions = _make_app(tmp_path, quota_enabled=True)
    try:
        yield app, sessions
    finally:
        app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("verification_fails", [False, True], ids=["guard-only", "guard-and-integrity"])
@pytest.mark.parametrize("advisor_path", ["early", "manual"])
async def test_actual_worker_preserves_primary_audit_after_advisor_guard_refusal(
    tmp_path, monkeypatch, real_route_app, record_property, advisor_path, verification_fails
):
    app, sessions = real_route_app
    primary_settings = PromptGuardSettings(endpoint="https://primary-shield.example.test", subscription_key="offline-primary-shield")
    advisor_settings = PromptGuardSettings(endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield")
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "openai/gpt-4.1-mini",
            "composer_endpoint_base_url": "https://offline-model.example.test/v1",
            "composer_endpoint_api_key": SecretStr("offline-model-key"),  # secret-scan: allow-this-line
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": primary_settings,
            "composer_advisor_prompt_guard": advisor_settings,
        }
    )
    guard_http = []

    def shield_response(request):
        payload = json.loads(request.content)
        assert request.url.host in {"primary-shield.example.test", "advisor-shield.example.test"}
        advisor = request.url.host == "advisor-shield.example.test"
        guard_http.append((advisor, payload))
        # Actual Azure client, parser, enforcer and durable guard audit remain
        # active; only the external service's HTTP response is local.
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": advisor},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(settings):
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        create_catalog_service(), app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    session = await sessions.create_session("alice", "Existing authored session", "local")
    baseline = await _seed_output_only_draft(app, sessions, session.id)
    engine = app.state.session_engine
    blob_id = _create_session_blob_for_test(
        engine=engine, session_id=str(session.id), data_dir=tmp_path, filename="input.csv", content="name,score\nada,42\n"
    )
    with engine.connect() as connection:
        source_path = connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).scalar_one()
    pipeline = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "on_validation_failure": "discard",
            "options": {"path": source_path, "schema": {"mode": "observed"}},
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": "outputs/result.jsonl",
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            }
        ],
    }
    tool_calls = [
        {
            "id": "source-grouped-audit-1",
            "type": "function",
            "function": {"name": "set_pipeline", "arguments": json.dumps({"pipeline": pipeline})},
        }
    ]
    if advisor_path == "manual":
        tool_calls.append(
            {
                "id": "manual-grouped-audit-1",
                "type": "function",
                "function": {
                    "name": "request_advisor_hint",
                    "arguments": json.dumps(
                        {
                            "trigger": "proactive_security_safety",
                            "problem_summary": "Review the supplied pipeline evidence.",
                            "recent_errors": [],
                            "attempted_actions": ["set_pipeline"],
                        }
                    ),
                },
            }
        )
    expected_tool_ids = tuple(call["id"] for call in tool_calls)
    clear_tool_id = "clear-grouped-audit-1"
    clear_calls = [
        {
            "id": clear_tool_id,
            "type": "function",
            "function": {"name": "remove_output", "arguments": json.dumps({"sink_name": "existing_output"})},
        }
    ]
    expected_primary_calls = 2 if advisor_path == "early" else 1
    sdk_calls = []

    async def completion(**kwargs):
        sdk_calls.append(deepcopy(kwargs))
        assert len(sdk_calls) <= expected_primary_calls, "Advisor refusal must not dispatch another SDK request"
        assert "set_pipeline" in {tool["function"]["name"] for tool in kwargs["tools"]}
        # EARLY is an empty -> nonempty transition. Reach it honestly from
        # the existing draft, through two real ordinary-loop tool turns.
        batch = clear_calls if advisor_path == "early" and len(sdk_calls) == 1 else tool_calls
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": None, "tool_calls": batch}, "finish_reason": "tool_calls"}],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    guard_originals = []
    integrity_originals = []
    service_failures = []
    committed = []
    physically_settled = []
    original_advisor = (
        composer._advisor_checkpoint._call_advisor_with_audit
        if advisor_path == "manual"
        else composer._advisor_checkpoint._maybe_run_early_checkpoint
    )
    original_accept = TurnEvidence.accept_checkpoint
    original_compose = composer.compose
    original_finish = sessions.finish_provider_attempt

    async def observe_advisor(*args, **kwargs):
        try:
            return await original_advisor(*args, **kwargs)
        except PromptGuardRefused as refusal:
            guard_originals.append(refusal)
            raise

    async def observe_compose(*args, **kwargs):
        try:
            return await original_compose(*args, **kwargs)
        except BaseException as failure:
            service_failures.append(failure)
            raise

    async def observe_physical_settlement(**kwargs):
        await original_finish(**kwargs)
        # Read committed rows after the actual required SQL finishes. This is
        # observation, not a test-authored audit drain or replacement writer.
        with engine.connect() as connection:
            rows = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).all()
        physically_settled.append((kwargs["call"], _call_envelopes(rows)))

    def verify_checkpoint(evidence, witness, *, expected_state, tool_call_ids, assistant_message_id):
        with engine.connect() as connection:
            states = connection.execute(
                select(composition_states_table).where(composition_states_table.c.session_id == str(session.id))
            ).all()
            rows = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == str(session.id), chat_messages_table.c.role.in_(("assistant", "tool")))
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        final_checkpoint = advisor_path == "manual" or len(committed) == 1
        assert bool(expected_state.sources) == final_checkpoint
        batch_ids = expected_tool_ids if final_checkpoint else (clear_tool_id,)
        previous_states = {state.id for state in committed[-1][0]} if committed else {str(baseline.id)}
        assert len(states) == len(previous_states) + 1
        assert {state.id for state in states} == previous_states | {str(witness.state.id)}
        batch_rows = rows[-(1 + len(batch_ids)) :]
        assert [row.role for row in batch_rows] == ["assistant", *["tool"] * len(batch_ids)]
        assert batch_rows[0].id == assistant_message_id
        assert tuple(row.tool_call_id for row in batch_rows[1:]) == batch_ids
        if committed:
            assert rows[: len(committed[-1][1])] == committed[-1][1], "Earlier committed tool turn must not replay"
        committed.append((states, rows, assistant_message_id, evidence))
        try:
            original_accept(
                evidence,
                witness,
                expected_state=expected_state.with_metadata({"name": "controlled comparison mismatch"})
                if verification_fails and final_checkpoint
                else expected_state,
                tool_call_ids=tool_call_ids,
                assistant_message_id=assistant_message_id,
            )
        except AuditIntegrityError as failure:
            integrity_originals.append(failure)
            raise

    advisor_method = "_call_advisor_with_audit" if advisor_path == "manual" else "_maybe_run_early_checkpoint"
    monkeypatch.setattr(composer._advisor_checkpoint, advisor_method, observe_advisor)
    monkeypatch.setattr(composer, "compose", observe_compose)
    monkeypatch.setattr(sessions, "finish_provider_attempt", observe_physical_settlement)
    monkeypatch.setattr(TurnEvidence, "accept_checkpoint", verify_checkpoint)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settled = await submit_and_settle(
            # The existing output draft selects the ordinary compose loop;
            # the controlled model response still authors the actual graph.
            client,
            app,
            path=f"/api/sessions/{session.id}/messages",
            body=message_body("Please review the source data.", state_id=baseline.id),
        )
    status, body = settled.error()
    assert status == (500 if verification_fails else 422), body
    assert len(sdk_calls) == len(committed) == len(physically_settled) == expected_primary_calls
    assert len(guard_originals) == len(service_failures) == 1
    leaves = _leaves(service_failures[0])
    refusals = [leaf for leaf in leaves if isinstance(leaf, PromptGuardRefused)]
    assert len(refusals) == 1 and refusals[0].__cause__ is guard_originals[0]
    assert refusals[0].audit_id == guard_originals[0].audit_id
    assert refusals[0].tool_invocations == () and refusals[0].partial_state is None
    assert len(integrity_originals) == int(verification_fails)
    if verification_fails:
        assert integrity_originals[0] in leaves
        assert integrity_originals[0].failed_turn is refusals[0].failed_turn
        assert body["error_type"] == "audit_integrity_error"
        assert body["failed_turn"]["assistant_message_id"] == committed[-1][2]
        assert committed[-1][3].effects.verification == "unavailable"
    else:
        assert body["detail"]["error_type"] == "prompt_guard_blocked"
        assert body["detail"]["audit_id"] == refusals[0].audit_id
        assert committed[-1][3].effects.verification == "verified"
    with engine.connect() as connection:
        rows = connection.execute(
            select(chat_messages_table)
            .where(chat_messages_table.c.session_id == str(session.id))
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
        states = connection.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(session.id))).all()
        attempts = connection.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id))
        ).all()
        ledger = connection.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session.id))).all()
        fence = connection.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
        ).one()
    assert states == committed[-1][0]
    assert [row for row in rows if row.role in ("assistant", "tool")] == committed[-1][1], "Committed assistant/tool rows must never replay"
    checks = [
        envelope["check"]
        for row in rows
        if row.role == "audit"
        for envelope in (row.tool_calls or ())
        if envelope["_kind"] == "prompt_guard_audit"
    ]
    assert [(check["role"], check["disposition"]) for check in checks] == [
        *[("composer", "allow")] * expected_primary_calls,
        ("advisor", "blocked"),
    ]
    assert checks[-1]["audit_id"] == guard_originals[0].audit_id
    assert guard_http and any(advisor for advisor, _payload in guard_http)
    assert len(attempts) == len(ledger) == expected_primary_calls
    assert all(attempt.settled_at is not None for attempt in attempts)
    assert {attempt.ledger_entry_id for attempt in attempts} == {entry.entry_id for entry in ledger}
    assert all(entry.prompt_tokens == entry.completion_tokens == 5 for entry in ledger)
    final_calls = _call_envelopes(rows)
    assert len(final_calls) == expected_primary_calls, "Every physical call must retain one complete audit without a manual drain"
    assert {call["call_id"] for _row, call in final_calls} == {attempt.attempt_id for attempt in attempts}
    for call, physical_rows in physically_settled:
        assert call.status.value == "success" and call.finished_at >= call.started_at
        (terminal,) = [(row, value) for row, value in final_calls if value["call_id"] == call.call_id]
        assert terminal[1] == deep_thaw(llm_call_audit_envelope(call)["call"])
        assert terminal in physical_rows
    # This contrast distinguishes an actual durable physical checkpoint from
    # a buffered draft. Do not assume the top-level group is the only carrier.
    checkpoint = physically_settled[0][1]
    assert len(checkpoint) == 1
    assert checkpoint[0][1] == final_calls[0][1]
    assert checkpoint[0][0].id == final_calls[0][0].id
    assert fence.released_at is not None
    record_property(
        "actual_custody",
        json.dumps(
            {
                "advisor_path": advisor_path,
                "http_status": status,
                "grouped_integrity": verification_fails,
                "top_level_llm_calls_carrier": "llm_calls" in service_failures[0].__dict__,
                "physical_checkpoint_audits": len(checkpoint),
                "final_primary_audits": len(final_calls),
                "same_checkpoint_row": bool(checkpoint) and checkpoint[0][0].id == final_calls[0][0].id,
                "call_id": physically_settled[0][0].call_id,
                "audit_row_id": final_calls[0][0].id,
                "ledger_entry_id": ledger[0].entry_id,
                "started_at": str(attempts[0].started_at),
                "settled_at": str(attempts[0].settled_at),
                "fence_released": fence.released_at is not None,
            },
            sort_keys=True,
        ),
    )
