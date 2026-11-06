"""Actual diagnostics preparation and mandatory audit-drain precedence.

The endpoint is invoked directly with its real fenced session/SQL owners.
Only the external SDK/guard and diagnostics snapshot/status input are replaced;
these controls do not claim to exercise HTTP authentication middleware.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import litellm
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import Select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.quota_authority import llm_call_usage_entries
from elspeth.web.execution import routes
from elspeth.web.execution.schemas import RunDiagnosticsResponse, RunDiagnosticSummary, RunStatusResponse
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import (
    chat_messages_table,
    quota_provider_attempts_table,
    runs_table,
    sessions_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import RunDiagnosticsAuthorityLostError
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer.test_prompt_guard_wiring import _Client
from tests.unit.web.execution.test_routes import _execution_service, _request_for_app, _route_endpoint
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft


def _durable(engine):
    with engine.connect() as connection:
        return {
            "messages": connection.execute(
                select(chat_messages_table).order_by(chat_messages_table.c.created_at, chat_messages_table.c.id)
            ).all(),
            "attempts": connection.execute(
                select(quota_provider_attempts_table).order_by(quota_provider_attempts_table.c.attempt_id)
            ).all(),
            "usage": connection.execute(select(token_usage_ledger_table).order_by(token_usage_ledger_table.c.entry_id)).all(),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["no_dispatch", "buffered", "sql_failure", "authority_loss"])
async def test_actual_diagnostics_context_refusal_preserves_physical_baseline_and_mandatory_drain(tmp_path, monkeypatch, scenario):
    app, sessions = _make_app(tmp_path, quota_enabled=True)
    engine = sessions._engine
    client = _Client(PromptGuardDecision("allow", "clean"))
    guard = PromptGuardSettings(endpoint="https://diagnostics-guard.example.test", subscription_key=SecretStr("diagnostics-fixture-key"))
    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", lambda settings: client)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda *, model, **kwargs: ComposerAvailability(available=True, model=model, provider="test"),
    )
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_model": "anthropic/claude-opus-4-7",
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": guard,
            "composer_advisor_prompt_guard": guard,
            "composer_max_prepared_request_bytes": 1 if scenario == "no_dispatch" else 2 * 1024 * 1024,
        }
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service, settings=app.state.settings, sessions_service=sessions, session_engine=engine
    )
    app.state.composer_service = composer
    app.include_router(routes.create_execution_router())
    session = await sessions.create_session("alice", title="Diagnostics refusal", auth_provider_type="local")
    state = await _seed_output_only_draft(app, sessions, session.id)
    with fenced_operation_context(engine, session.id, operation_kind=SessionOperationKind.EXECUTE) as context:
        run = await sessions.create_run(session.id, state.id, session_operation_context=context)
    execution = _execution_service()
    execution.get_status.return_value = RunStatusResponse(
        run_id=str(run.id), status="pending", started_at=datetime.now(UTC), finished_at=None, error=None, landscape_run_id=None
    )
    snapshot = RunDiagnosticsResponse(
        # The real unlinked loader uses run_id as its effective snapshot ID;
        # the status retains the actual absent Landscape link above.
        run_id=str(run.id),
        landscape_run_id=str(run.id),
        run_status="pending",
        summary=RunDiagnosticSummary(
            token_count=0,
            preview_limit=50,
            preview_truncated=False,
            discard_count=0,
            state_counts={},
            operation_counts={},
            latest_activity_at=None,
        ),
        tokens=[],
        operations=[],
        artifacts=[],
        discards=[],
    )
    monkeypatch.setattr(routes, "load_run_diagnostics_for_settings", lambda *args, **kwargs: snapshot)
    initial = _durable(engine)
    sdk, baseline, proof, drains = [], {}, [], []
    in_drain = False
    sql_error = OperationalError("controlled diagnostics custody proof", {}, Exception("private fixture proof failure"))

    async def completion(**arguments):
        sdk.append(arguments)
        assert len(sdk) == 1, "the refused current preparation must never dispatch"
        return litellm.ModelResponse(
            model=arguments["model"],
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"headline": "Run pending", "evidence": [], "meaning": "No work has run.", "next_steps": []}),
                    }
                }
            ],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    explain = composer.explain_run_diagnostics

    async def prior_actual_call_then_current_refusal(snapshot, **arguments):
        # Controlled orchestration supplies a real already-settled call to
        # the existing recorder; the second real preparation authors refusal.
        # No row/call is synthesized or retrospectively attributed.
        await explain(snapshot, **arguments)
        baseline["calls"] = arguments["recorder"].llm_calls
        baseline["durable"] = _durable(engine)
        current = composer._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
        composer._settings = current
        composer._provider_gateway._settings = current
        try:
            return await explain(snapshot, **arguments)
        except ComposerContextUnsupported as refusal:
            baseline["refusal"] = refusal
            assert refusal.llm_calls == baseline["calls"]
            assert refusal.llm_calls[0] is baseline["calls"][0]
            raise

    if scenario != "no_dispatch":
        monkeypatch.setattr(composer, "explain_run_diagnostics", prior_actual_call_then_current_refusal)
    append = sessions.add_run_diagnostics_audit_messages_atomic

    async def observe_actual_drain(authority, drafts):
        nonlocal in_drain
        drains.append((authority, drafts))
        if scenario == "authority_loss":
            # Lose actual session custody after the provider has settled and
            # after the route's lease has closed, before the real write proof.
            # Existing message/call/usage rows are deliberately preserved.
            with engine.begin() as connection:
                connection.execute(
                    update(sessions_table).where(sessions_table.c.id == str(session.id)).values(archived_at=datetime.now(UTC))
                )
        in_drain = True
        try:
            return await append(authority, drafts)
        finally:
            in_drain = False

    monkeypatch.setattr(sessions, "add_run_diagnostics_audit_messages_atomic", observe_actual_drain)

    def actual_proof(connection, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        selected = compiled.statement if compiled is not None else None
        if (
            in_drain
            and isinstance(selected, Select)
            and tuple(column.name for column in selected.selected_columns) == ("session_id", "state_id")
            and any(table is runs_table for table in selected.get_final_froms())
        ):
            proof.append(True)
            if scenario == "sql_failure":
                raise sql_error

    event.listen(engine, "before_cursor_execute", actual_proof)
    try:
        endpoint = _route_endpoint(app, "evaluate_run_diagnostics")
        expected = (
            AuditIntegrityError
            if scenario == "sql_failure"
            else RunDiagnosticsAuthorityLostError
            if scenario == "authority_loss"
            else HTTPException
        )
        with pytest.raises(expected) as caught:
            await endpoint(run.id, _request_for_app(app), user=UserIdentity(user_id="alice", username="alice"), limit=50, service=execution)
        error = caught.value
        if scenario in {"no_dispatch", "buffered"}:
            assert error.status_code == 422 and error.detail == ComposerContextUnsupported("prepared_request_bytes").to_payload()
            assert isinstance(error.__cause__, ComposerContextUnsupported)
        elif scenario == "sql_failure":
            assert error.__cause__ is sql_error and proof == [True]
        else:
            assert error.reason == "session_archived" and error.session_id == session.id and error.run_id == run.id
        if scenario == "no_dispatch":
            assert sdk == [] and client.requests == [] and drains == []
            assert _durable(engine) == initial
        else:
            assert len(sdk) == len(client.requests) == len(drains) == 1
            assert client.requests[0].user_prompt == "", "the real diagnostics owner supplies no invented genuine user"
            assert len(baseline["calls"]) == 1 and baseline["refusal"].reason == "prepared_request_bytes"
            durable = baseline["durable"]
            assert _durable(engine) == durable
            call = baseline["calls"][0]
            assert len(durable["attempts"]) == len(durable["usage"]) == 1
            assert durable["attempts"][0].attempt_id == call.call_id
            assert durable["attempts"][0].settled_at is not None and durable["attempts"][0].ledger_entry_id is not None
            physical_ids = [entry.call_id for row in durable["messages"] for entry in llm_call_usage_entries(row.tool_calls or ())]
            assert physical_ids == [call.call_id]
            if scenario == "buffered":
                assert proof == [True], "positive proof control must reach the same recovery owner"
    finally:
        event.remove(engine, "before_cursor_execute", actual_proof)
        engine.dispose()
