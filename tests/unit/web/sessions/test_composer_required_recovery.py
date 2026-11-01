"""Required recovery storage failures retain their audited storage verdict.

The partial-state recovery handlers (``routes._helpers._handle_*``) have no
src caller after the V1 lifecycle removal; they are held for U-40, which
decides the Continuation crash/convergence/preflight arms. These controls keep
the held handlers honest until then; they are not Continuation cover.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerPluginCrashError, ComposerRuntimePreflightError
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.composer_operations import ComposerRequiredRecoveryFailure
from tests.helpers.recovery_checkpoint import commit_p4_checkpoint
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.sessions.test_routes import _EMPTY_STATE, _llm_call, _llm_call_audit_rows, _make_app


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
    async with acquire_compose_context(service, session.id) as context:
        await commit_p4_checkpoint(service, session.id, _EMPTY_STATE, session_operation_context=context)

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
                session_operation_context=context,
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
