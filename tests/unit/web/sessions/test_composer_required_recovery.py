"""Required recovery storage failures retain their audited storage verdict."""

import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerPluginCrashError, ComposerRuntimePreflightError
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerRequiredRecoveryFailure
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.time_normalization import restore_utc
from tests.unit.web.sessions.test_composer_async_worker import _worker
from tests.unit.web.sessions.test_routes import _EMPTY_STATE, _llm_call, _llm_call_audit_rows, _make_app
from tests.unit.web.sessions.test_run_composer_turn import _CancelCommittingComposer, _run, _running_job
from tests.unit.web.sessions.test_unwind_persist_forensics import _tool_invocation


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["convergence", "plugin", "preflight"])
async def test_required_partial_storage_failure_outranks_stop_without_duplicate_audit(tmp_path: Path, arm: str) -> None:
    app, service = _make_app(tmp_path)
    call = _llm_call()
    if arm == "convergence":
        failure = ComposerConvergenceError(3, budget_exhausted="composition", partial_state=_EMPTY_STATE, llm_calls=(call,))
    elif arm == "plugin":
        failure = ComposerPluginCrashError(original_exc=ValueError("plugin failed"), partial_state=_EMPTY_STATE, llm_calls=(call,))
    else:
        failure = ComposerRuntimePreflightError(original_exc=ValueError("preflight failed"), partial_state=_EMPTY_STATE, llm_calls=(call,))
    jobs = []
    app.state.composer_service = _CancelCommittingComposer(jobs, raise_after=failure)
    session = await service.create_session("alice", "Required recovery", "local")
    original = OperationalError("synthetic", {}, RuntimeError("storage unavailable"))
    async with _running_job(app, service, session.id, kind="compose_message", content="Recover.") as job:
        jobs.append(job)
        with (
            patch.object(service._session_operation_authority, "mutate", side_effect=original),
            pytest.raises(ComposerRequiredRecoveryFailure) as caught,
        ):
            await _run(app, job)
        assert caught.value.__cause__ is original
        assert job.observation.audit_cohort_durable is True
        services = composer_app_services(app)
        from elspeth.web.sessions.composer_turn import persist_cancelled_turn_audit

        await persist_cancelled_turn_audit(services, job.running, job.observation)
        rows = _llm_call_audit_rows(await service.get_messages(session.id, limit=None))
        assert len(rows) == 1 and rows[0][1]["call"]["call_id"] == call.call_id
        terminal = await _worker(app, job.authority)._settle_failure(services, job.running, caught.value)
        assert terminal.cancel_requested_at is not None
        assert ComposerOperationError.model_validate_json(terminal.result_json).http_status == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["convergence", "plugin", "preflight"])
@pytest.mark.parametrize("required", [False, True])
async def test_recovery_storage_required_and_optional_controls(tmp_path: Path, arm: str, required: bool) -> None:
    from elspeth.web.sessions.routes import _helpers

    app, service = _make_app(tmp_path)
    services = composer_app_services(app)
    session = await service.create_session("alice", "Recovery controls", "local")
    call = _llm_call()
    if arm == "convergence":
        failure = ComposerConvergenceError(3, budget_exhausted="composition", partial_state=_EMPTY_STATE, llm_calls=(call,))
        handler = _helpers._handle_convergence_error
    elif arm == "plugin":
        failure = ComposerPluginCrashError(original_exc=ValueError("plugin"), partial_state=_EMPTY_STATE, llm_calls=(call,))
        handler = _helpers._handle_plugin_crash
    else:
        failure = ComposerRuntimePreflightError(original_exc=ValueError("preflight"), partial_state=_EMPTY_STATE, llm_calls=(call,))
        handler = _helpers._handle_runtime_preflight_failure
    original = OperationalError("SQL_RECOVERY_CANARY", {"parameter": "SQL_PARAMETER_CANARY"}, RuntimeError("SQL_CAUSE_CANARY"))
    async with _running_job(app, service, session.id, kind="compose_message", content="Control.") as job:

        async def invoke():
            return await handler(
                failure,
                service,
                session.id,
                "alice",
                "control",
                None,
                settings=services.settings,
                secret_service=services.scoped_secret_resolver,
                plugin_snapshot=services.plugin_snapshot_for_user_id("alice"),
                profile_registry=services.operator_profile_registry,
                catalog=services.catalog_service,
                session_operation_context=job.lease.context,
                required_audit=required,
            )

        with capture_logs() as logs, patch.object(service, "save_composition_state", side_effect=original):
            if required:
                with pytest.raises(ComposerRequiredRecoveryFailure) as caught:
                    await invoke()
                assert caught.value.__cause__ is original and caught.value.audit_cohort_durable
            else:
                body = await invoke()
                common = {"partial_state_save_failed": True, "partial_state_save_error": "OperationalError"}
                if arm == "convergence":
                    from elspeth.web.composer.progress import convergence_progress_event

                    progress = convergence_progress_event(budget_exhausted="composition")
                    expected = {
                        "error_type": "convergence",
                        "detail": str(failure),
                        "turns_used": 3,
                        "budget_exhausted": "composition",
                        "reason": progress.reason,
                        "recovery_text": progress.likely_next,
                        **common,
                    }
                elif arm == "plugin":
                    expected = {
                        "error_type": "composer_plugin_error",
                        "detail": "A composer plugin crashed during a composer tool call. The exception class is recorded in the structured server log event for triage. This is not a user-retryable error.",
                        **common,
                    }
                else:
                    expected = {
                        "error_type": "composer_plugin_error",
                        "detail": "A composer plugin crashed during runtime preflight. Diagnostic frames are recorded in the persisted state's validation_errors when a partial state was captured. This is not a user-retryable error.",
                        **common,
                    }
                assert body == expected
                assert "SQL_RECOVERY_CANARY" not in json.dumps(body)
                assert "SQL_CAUSE_CANARY" not in json.dumps(body)
                assert "SQL_PARAMETER_CANARY" not in json.dumps(body)
        assert "SQL_RECOVERY_CANARY" not in json.dumps(logs)
        assert "SQL_CAUSE_CANARY" not in json.dumps(logs)
        assert "SQL_PARAMETER_CANARY" not in json.dumps(logs)
        assert len(_llm_call_audit_rows(await service.get_messages(session.id, limit=None))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["convergence", "plugin", "preflight"])
async def test_recovery_and_actual_audit_sql_failure_retain_group_and_recover_once(tmp_path: Path, arm: str) -> None:
    from elspeth.web.sessions.composer_turn import persist_cancelled_turn_audit

    app, service = _make_app(tmp_path)
    services = composer_app_services(app)
    call = _llm_call()
    tool = _tool_invocation("list_sources")
    if arm == "convergence":
        failure = ComposerConvergenceError(
            3, budget_exhausted="composition", partial_state=_EMPTY_STATE, llm_calls=(call,), tool_invocations=(tool,)
        )
    elif arm == "plugin":
        failure = ComposerPluginCrashError(
            original_exc=ValueError("plugin failed"), partial_state=_EMPTY_STATE, llm_calls=(call,), tool_invocations=(tool,)
        )
    else:
        failure = ComposerRuntimePreflightError(
            original_exc=ValueError("preflight failed"), partial_state=_EMPTY_STATE, llm_calls=(call,), tool_invocations=(tool,)
        )
    jobs = []
    app.state.composer_service = _CancelCommittingComposer(jobs, raise_after=failure)
    session = await service.create_session("alice", "Recovery collision", "local")
    state_sql = OperationalError("STATE_SQL_CANARY", {}, RuntimeError("state private cause"))
    audit_sql = OperationalError("AUDIT_SQL_CANARY", {}, RuntimeError("audit private cause"))
    audit_insert_attempts = 0

    def fail_audit_insert(conn, cursor, statement, parameters, context, executemany):
        nonlocal audit_insert_attempts
        from sqlalchemy.sql.dml import Insert

        compiled = context.compiled
        if (
            compiled is not None
            and isinstance(compiled.statement, Insert)
            and compiled.statement.table.name == "chat_messages"
            and "llm_call_audit" in repr(parameters)
        ):
            audit_insert_attempts += 1
            raise audit_sql

    def ledger_snapshot():
        with services.session_engine.connect() as connection:
            return tuple(
                connection.execute(
                    select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(session.id))
                ).mappings()
            )

    async with _running_job(app, service, session.id, kind="compose_message", content="Recover.") as job:
        attempt = await service.begin_provider_attempt(session_operation_context=job.lease.context, source="composer")
        call = _llm_call(call_id=attempt.attempt_id, started_at=attempt.started_at)
        if arm == "convergence":
            failure = ComposerConvergenceError(
                3, budget_exhausted="composition", partial_state=_EMPTY_STATE, llm_calls=(call,), tool_invocations=(tool,)
            )
        elif arm == "plugin":
            failure = ComposerPluginCrashError(
                original_exc=ValueError("plugin failed"), partial_state=_EMPTY_STATE, llm_calls=(call,), tool_invocations=(tool,)
            )
        else:
            failure = ComposerRuntimePreflightError(
                original_exc=ValueError("preflight failed"), partial_state=_EMPTY_STATE, llm_calls=(call,), tool_invocations=(tool,)
            )
        app.state.composer_service = _CancelCommittingComposer(jobs, raise_after=failure)
        jobs.append(job)
        event.listen(services.session_engine, "before_cursor_execute", fail_audit_insert)
        try:
            with (
                patch.object(service._session_operation_authority, "mutate", side_effect=state_sql),
                pytest.raises(ComposerOwnedSettlementFailure) as caught,
            ):
                await _run(app, job)
        finally:
            event.remove(services.session_engine, "before_cursor_execute", fail_audit_insert)
        group = caught.value.__cause__
        assert isinstance(group, BaseExceptionGroup)
        assert len(group.exceptions) == 2 and group.exceptions[0] is state_sql
        audit_failure = group.exceptions[1]
        assert isinstance(audit_failure, AuditIntegrityError) and audit_failure.__cause__ is audit_sql
        assert audit_insert_attempts == 1
        assert job.observation.audit_cohort_durable is False
        assert _llm_call_audit_rows(await service.get_messages(session.id, limit=None)) == []
        assert ledger_snapshot() == ()
        assert all(message.role != "audit" for message in await service.get_messages(session.id, limit=None))
        await persist_cancelled_turn_audit(services, job.running, job.observation)
        assert job.observation.audit_cohort_durable is True
        rows = _llm_call_audit_rows(await service.get_messages(session.id, limit=None))
        assert len(rows) == 1 and deep_thaw(rows[0][1]) == llm_call_audit_envelope(call)
        first_row = rows[0][0]
        from elspeth.web.sessions.routes._helpers import redacted_tool_invocation_content_and_envelope

        tool_content, tool_envelope = redacted_tool_invocation_content_and_envelope(tool)
        cohort = tuple(message for message in await service.get_messages(session.id, limit=None) if message.role == "audit")
        assert len(cohort) == 2
        assert cohort[0].content == tool_content
        assert deep_thaw(cohort[0].tool_calls) == [tool_envelope]
        ledger = ledger_snapshot()
        assert len(ledger) == 1
        assert str(UUID(ledger[0]["entry_id"])) == ledger[0]["entry_id"]
        with services.session_engine.connect() as connection:
            settled_attempt = (
                connection.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == call.call_id))
                .mappings()
                .one()
            )
        assert settled_attempt["attempt_id"] == call.call_id
        assert settled_attempt["ledger_entry_id"] == ledger[0]["entry_id"]
        assert restore_utc(settled_attempt["started_at"]) == call.started_at
        assert settled_attempt["settled_at"] is not None
        assert ledger[0]["model"] == call.model_returned
        assert ledger[0]["prompt_tokens"] == call.prompt_tokens
        assert ledger[0]["completion_tokens"] == call.completion_tokens
        assert restore_utc(ledger[0]["recorded_at"]) == call.finished_at
        await persist_cancelled_turn_audit(services, job.running, job.observation)
        assert ledger_snapshot() == ledger
        assert tuple(message for message in await service.get_messages(session.id, limit=None) if message.role == "audit") == cohort
        with services.session_engine.connect() as connection:
            assert (
                connection.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == call.call_id))
                .mappings()
                .one()
                == settled_attempt
            )
        assert _llm_call_audit_rows(await service.get_messages(session.id, limit=None))[0][0] == first_row
        terminal = await _worker(app, job.authority)._settle_failure(services, job.running, caught.value)
        assert group.exceptions[0] is state_sql and audit_failure.__cause__ is audit_sql
        result = ComposerOperationError.model_validate_json(terminal.result_json)
        assert result.http_status == 500 and result.error_type == "audit_integrity_error"
        assert "CANARY" not in terminal.result_json
        assert ledger_snapshot() == ledger
        assert tuple(message for message in await service.get_messages(session.id, limit=None) if message.role == "audit") == cohort
