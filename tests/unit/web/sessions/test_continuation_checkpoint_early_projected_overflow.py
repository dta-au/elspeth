"""Original complete EARLY projection failure closes D under its existing hold."""

import json
from dataclasses import replace

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer import continuation_evidence as evidence_codec
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
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness
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


def _reasoning_details(*, early):
    # Supported provider artifacts whose individual strings survive both the
    # generic credential traversal and the smaller diagnostic artifact bound.
    count, size, character = (6, 49_000, "e") if early else (7, 50_000, "r")
    return [{"type": "reasoning.text", "text": character * size} for _ in range(count)]


def _install_primary_response_profile(monkeypatch, *, oversized):
    original_response = phase_fixture.ModelResponse

    def primary_response(**kwargs):
        if oversized:
            kwargs["choices"][0]["message"]["content"] = "p" * 260_000
            kwargs["choices"][0]["message"]["reasoning_details"] = _reasoning_details(early=False)
        return original_response(**kwargs)

    monkeypatch.setattr(phase_fixture, "ModelResponse", primary_response)


def _early_response_profile(*, oversized):
    response = _reply(_CLEAN)
    if oversized:
        response.choices[0].message.reasoning_details = _reasoning_details(early=True)
    return response


def _assert_fitting_primary_profile(runtime, *, oversized):
    # Measure the already admitted original chunks, without another capture or
    # replacement clock. The complete prefix must fit before the paid EARLY call.
    evidence = runtime.source.admitted_evidence
    lengths = tuple(len(chunk.canonical.encode("utf-8")) for chunk in (evidence.source_chunk, evidence.recorder_chunk))
    assert lengths == (evidence.source_chunk.reference.byte_length, evidence.recorder_chunk.reference.byte_length)
    assert 0 < sum(lengths) <= 1_048_576, lengths
    if oversized:
        calls = json.loads(evidence.recorder_chunk.canonical)["snapshot"]["llm_calls"]
        originals = [call for call in calls if call["reasoning_details"] == _reasoning_details(early=False)]
        assert len(originals) == 1 and originals[0]["status"] == "success"


def _assert_complete_early_profile(returned, *, oversized):
    assert type(returned) is CheckpointReturnedResponseObservation
    assert type(returned.provider_metadata.v4_completeness) is V4ProviderResultCompleteness
    response_bytes = len(returned.response.canonical.encode("utf-8"))
    assert response_bytes == returned.response.reference.byte_length
    assert 0 < response_bytes <= 1_048_576, response_bytes
    assert returned.audit_call.status.value == "success"
    if oversized:
        expected = _reasoning_details(early=True)
        assert deep_thaw(returned.provider_metadata.reasoning_details) == expected
        assert deep_thaw(returned.audit_call.reasoning_details) == expected


def _observe_original_pair_capacity(monkeypatch):
    original = evidence_codec._require_evidence_pair_capacity
    overflows = []

    def observe(*, evidence_bytes, recorder_chunk, mandatory_retention):
        recorder_bytes = len(recorder_chunk.canonical.encode("utf-8"))
        assert recorder_bytes == recorder_chunk.reference.byte_length
        if mandatory_retention and evidence_bytes + recorder_bytes > 1_048_576:
            # The producer admits its exact evidence-document size before this
            # call, but serializes only AFTER the genuine pair guard succeeds.
            # Keep that refusal; do not manufacture a replacement encoding.
            assert 0 < evidence_bytes <= 1_048_576, evidence_bytes
            assert 0 < recorder_bytes <= 1_048_576, recorder_bytes
            overflows.append((evidence_bytes, recorder_bytes))
        return original(evidence_bytes=evidence_bytes, recorder_chunk=recorder_chunk, mandatory_retention=mandatory_retention)

    monkeypatch.setattr(evidence_codec, "_require_evidence_pair_capacity", observe)
    return overflows


def _assert_pre_h_projection_overflow(projected, pair_overflows):
    assert type(projected) is ProjectedEarlyCheckpointOverflow
    # This is the original projection capture witness, before H/settlement,
    # never the independent 16 MiB retained-tail aggregate admission.
    witness = projected.witness
    assert witness.unit == "evidence_pair" and witness.codec == "composer.evidence.v2"
    assert witness.limit_bytes == 1_048_576
    assert witness.size_is_exact
    assert len(pair_overflows) == 1, pair_overflows
    assert witness.observed_bytes == sum(pair_overflows[0]) > witness.limit_bytes, witness


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", (False, True), ids=("fitting-capture-is-not-overflow", "original-capture-overflow"))
async def test_original_early_projection_overflow_closes_d_without_retry(operation_store, tmp_path, monkeypatch, oversized):
    # These are external provider bytes, before PRIMARY admission. Each physical
    # response fits separately; their complete recorder plus transcript pair does
    # not fit after EARLY. No encoder, size gate, clock or witness is replaced.
    _install_primary_response_profile(monkeypatch, oversized=oversized)
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, _running, selected, primary_sdk):
        _assert_fitting_primary_profile(owner.runtime, oversized=oversized)
        sdk = []

        async def completion(**_kwargs):
            sdk.append(1)
            return _early_response_profile(oversized=oversized)

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
            _assert_complete_early_profile(returned, oversized=oversized)
            current = await load_selected_continuation(
                authority=phases.authority, running=prepared.running, selected=admission.selected, required_work=phases.work
            )
            inventory = await materialize_continuation_env_inventory(
                advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=current, required_work=phases.work
            )
            with monkeypatch.context() as projection_patch:
                pair_overflows = _observe_original_pair_capacity(projection_patch)
                try:
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
                except BaseExceptionGroup:
                    # WP2 adds a genuine final-source refusal after capture.
                    # Require the pair cut even when no projection is returned,
                    # then preserve that original group and its native children.
                    assert len(pair_overflows) == 1, pair_overflows
                    raise
            service = prepared.composer._require_sessions_service()
            args = {"service": service, "prepared": prepared, "admission": admission, "returned": returned, "projected": projected}
            before = stored_rows(service._engine)
            if not oversized:
                assert type(projected) is ProjectedEarlyCheckpoint
                assert pair_overflows == []
                with pytest.raises(AuditIntegrityError):
                    settle_early_projected_overflow_sync(**args)
                assert stored_rows(service._engine) == before
                return
            _assert_pre_h_projection_overflow(projected, pair_overflows)
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
