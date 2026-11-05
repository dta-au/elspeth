"""Original complete EARLY projection failure closes D under its existing hold."""

import json
from dataclasses import replace

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response
from elspeth.web.composer.continuation_checkpoint_early_physical import (
    checkpoint_early_detached_runtime,
    checkpoint_early_physical_custody,
)
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_checkpoint_projection import (
    CheckpointPassAccounting,
    ProjectedEarlyCheckpoint,
    ProjectedEarlyCheckpointOverflow,
    project_early_checkpoint_result,
)
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.continuation_checkpoint_early_projected_overflow import settle_early_projected_overflow_sync
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import read_v4_checkpoint_paid_overflow_terminal_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store
_CLEAN = json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "Coherent.", "note": None})


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", (False, True), ids=("fitting-capture-is-not-overflow", "original-capture-overflow"))
async def test_original_early_projection_overflow_closes_d_without_retry(operation_store, tmp_path, monkeypatch, oversized):
    # These are external provider bytes, before PRIMARY admission. Each physical
    # response fits separately; their complete recorder plus transcript pair does
    # not fit after EARLY. No encoder, size gate, clock or witness is replaced.
    original_response = phase_fixture.ModelResponse

    def primary_response(**kwargs):
        if oversized:
            kwargs["choices"][0]["message"]["content"] = "p" * 260_000
            kwargs["choices"][0]["message"]["reasoning_content"] = "r" * 350_000
        return original_response(**kwargs)

    monkeypatch.setattr(phase_fixture, "ModelResponse", primary_response)
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, _running, selected, primary_sdk):
        sdk = []

        async def completion(**_kwargs):
            sdk.append(1)
            response = _reply(_CLEAN)
            if oversized:
                response.choices[0].message.reasoning_content = "e" * 490_000
            return response

        monkeypatch.setattr(litellm, "acompletion", completion)
        async with _early_attempt(owner, selected) as source:
            await _invoke_early(source)
            custody = checkpoint_early_physical_custody(source)
            prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
            assert prepared is not None and admission is not None
            runtime, phases = owner.runtime, owner.phases
            returned = observe_v4_early_checkpoint_returned_response(
                source.capture.snapshot(),
                custody=custody,
                policy=runtime.source.policy,
                attempt=admission.attempt,
                context=runtime.bindings.session_operation_context,
                env_ref_names=runtime.env_ref_names,
            )
            assert type(returned) is CheckpointReturnedResponseObservation
            current = await load_selected_continuation(
                authority=phases.authority, running=prepared.running, selected=admission.selected, required_work=phases.work
            )
            inventory = await materialize_continuation_env_inventory(
                advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=current, required_work=phases.work
            )
            projected = await project_early_checkpoint_result(
                prepared=prepared,
                admission=admission,
                detached=checkpoint_early_detached_runtime(source),
                original=runtime,
                selected=current,
                inventory=inventory,
                custody=custody,
                returned=returned,
                accounting=CheckpointPassAccounting(0, 1, None, None, False),
                physical_source=source,
            )
            service = prepared.composer._require_sessions_service()
            args = {"service": service, "prepared": prepared, "admission": admission, "returned": returned, "projected": projected}
            before = stored_rows(service._engine)
            if not oversized:
                assert type(projected) is ProjectedEarlyCheckpoint
                with pytest.raises(AuditIntegrityError):
                    settle_early_projected_overflow_sync(**args)
                assert stored_rows(service._engine) == before
                return
            assert type(projected) is ProjectedEarlyCheckpointOverflow
            # Constructor copies and even the genuine value-only witness carry
            # no paid authority. Original return/source/ref substitutions fail.
            for changes in (
                {"projected": replace(projected)},
                {"projected": projected.witness},
                {"returned": replace(returned)},
                {"returned": replace(returned, phase="end")},
                {"admission": replace(admission)},
                {"prepared": replace(prepared)},
                {"prepared": replace(prepared, request_sha256="f" * 64)},
            ):
                with pytest.raises(AuditIntegrityError):
                    settle_early_projected_overflow_sync(**(args | changes))
                assert stored_rows(service._engine) == before
            verified = settle_early_projected_overflow_sync(**args)
            sid, operation = str(prepared.running.claim.session_id), prepared.running.claim.operation_id
            with locked_session_transaction(service._engine, sid) as conn:
                cold = read_v4_checkpoint_paid_overflow_terminal_on_connection(
                    conn,
                    session_id=prepared.running.claim.session_id,
                    operation_id=operation,
                    effect_key=admission.effect_key,
                )
                assert cold == verified
                assert cold.phase == "early" and cold.hold_state == "consumed"
                assert cold.receipt.representation == "retained_tail"
                assert cold.receipt.reason in ("private_document_bytes", "evidence_pair_bytes")
                assert cold.receipt.limit_bytes == 1_048_576
                assert cold.receipt.observed_bytes == projected.witness.observed_bytes
                assert cold.receipt.retryable is False and cold.receipt.evidence_status == "incomplete"
                assert cold.receipt.cost.status == "known" and float(cold.receipt.cost.amount) == 0.01
                assert cold.receipt.usage.prompt_tokens == returned.audit_call.prompt_tokens == 7
                assert cold.receipt.usage.completion_tokens == returned.audit_call.completion_tokens == 3
                assert conn.execute(select(holds.c.state).where(holds.c.effect_key == admission.effect_key)).scalar_one() == "consumed"
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
                assert attempt.settled_at is not None
                usage = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).one()
                assert usage.prompt_tokens == 7 and usage.completion_tokens == 3
                child = conn.execute(select(effects).where(effects.c.effect_key == admission.effect_key)).one()
                assert child.disposition == "paid_overflow"
                parent = conn.execute(select(jobs).where(jobs.c.operation_id == operation)).one()
                error = json.loads(parent.result_json)
                assert parent.status == "failed" and error["http_status"] == 507
                assert error["body"]["retryable"] is False and error["body"]["evidence_status"] == "incomplete"
            assert sdk == [1] and len(primary_sdk) == 1
