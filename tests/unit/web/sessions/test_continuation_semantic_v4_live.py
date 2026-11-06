"""V4 semantic Advisor admits the original physical call and closes each retained outcome."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_semantic_advisor as semantic_physical
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
)
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_semantic_advisor_phase import (
    SemanticAdvisorNoSdkTerminal,
    SemanticAdvisorPaidIncompleteTerminal,
    run_continuation_semantic_advisor,
)
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
from elspeth.web.config import WebSettings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.sessions import continuation_primary as primary_sql
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.continuation import load_snapshot, read_chunk
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_semantic_advisor import read_semantic_advisor_outcome_on_connection
from elspeth.web.sessions.continuation_semantic_paid_incomplete_terminal import read_v4_semantic_paid_incomplete_terminal_on_connection
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import reconstruct_semantic_no_sdk_tail_batch
from elspeth.web.sessions.continuation_semantic_v4_no_sdk_terminal import read_v4_semantic_no_sdk_terminal_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_primary_v4_live as primary_fixture
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST
from tests.unit.web.sessions.test_continuation_semantic_advisor import _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


def _v4_store(store, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    return store[0], store[1], authority, store[3], store[4]


def _phases(case):
    runtime = case["runtime"]
    phases = ContinuationLivePhases(
        service=runtime.materialization.service,
        authority=runtime.materialization.authority,
        work=case["work"],
        provider_owner=runtime.bindings.provider_owner,
        lease=case["lease"],
        progress=runtime.bindings.progress,
    )
    phases.bind_initial(runtime)
    return phases


def _unretainable_reply(request):
    return ModelResponse(
        model="test/model",
        choices=[
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(_answer(request)),
                    "reasoning_details": [{"text": "x" * 200_000} for _ in range(7)],
                    "tool_calls": None,
                },
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


def _large_complete_reply(request):
    response = _reply(json.dumps(_answer(request)))
    response.choices[0].message.reasoning_content = "r" * 100_000
    return response


@pytest.mark.asyncio
async def test_v4_semantic_success_is_one_original_complete_result_and_consumed_hold(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert result is None and len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "committed" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert effect.requested_model == sdk[0]
        assert len(entries) == 1 and entries[0].model == "test/model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "deadline_observation"),
    (
        ("primary", "active_quota"),
        ("primary", "late_quota"),
        ("primary", "late_action"),
        ("semantic", "active_quota"),
        ("semantic", "after_preparation"),
        ("semantic", "late_quota"),
        ("semantic", "late_action"),
    ),
)
async def test_v4_original_admission_checks_quota_at_entry_and_after_sql(
    operation_store, tmp_path, monkeypatch, family, deadline_observation
):
    """The real SQL owner checks its original stage twice and rolls back expiry."""
    timely = deadline_observation in {"active_quota", "after_preparation"}
    async with AsyncExitStack() as stack:
        if family == "primary":
            store = operation_store
            kwargs, produced, recorder, _requests = await primary_input(store, tmp_path, monkeypatch)
            work = kwargs["required_work"]
            original_admit = primary_sql.admit_v4_primary_provider_on_connection
            original_service = SessionServiceImpl.begin_v4_continuation_primary_attempt

            async def invoke():
                with prompt_guard_user_scope(REQUEST):
                    return await primary_fixture.run_continuation_primary(
                        service=kwargs["service"],
                        authority=kwargs["authority"],
                        selected=kwargs["selected"],
                        producers=produced.producers,
                        messages=list(produced.messages),
                        tools=[],
                        recorder=recorder,
                        drained=primary_fixture.RecorderDrainCursors(),
                        required_work=work,
                        provider_owner=primary_fixture.ProviderInvocationOwner(service=store[3], required_work=work),
                        timeout=5,
                    )

        else:
            store = _v4_store(operation_store, monkeypatch)
            case = await stack.enter_async_context(_semantic_case(store, tmp_path, monkeypatch))
            work = case["work"]
            phases = _phases(case)
            original_admit = semantic_sql.admit_v4_semantic_provider_on_connection
            original_service = SessionServiceImpl.begin_v4_continuation_semantic_attempt

            async def invoke():
                return await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)

        clocks = []
        checks = []
        outcomes = []
        sdk = []
        original_check = ContinuationActionClock.require_active_quota_time

        def check_original_quota(self, *, monotonic_now=None):
            if not clocks or self is not clocks[0]:
                return original_check(self, monotonic_now=monotonic_now)
            assert self._active_stage == "quota" and self._guard_complete
            assert "preparation" in self._seen
            checks.append(self)
            if deadline_observation == "after_preparation":
                monotonic_now = self.preparation_deadline_monotonic + 0.001
                assert self._active_stage_deadline_monotonic is not None
                assert monotonic_now < min(self._active_stage_deadline_monotonic, self.started_monotonic + self.policy.total)
            elif len(checks) == 2 and not timely:
                assert self._active_stage_deadline_monotonic is not None
                if deadline_observation == "late_quota":
                    monotonic_now = self._active_stage_deadline_monotonic + 0.001
                    assert monotonic_now < self.started_monotonic + self.policy.total
                else:
                    monotonic_now = self.started_monotonic + self.policy.total + 0.001
            return original_check(self, monotonic_now=monotonic_now)

        def admit_original(conn, service, prepared, *, model, no_sdk_issuer):
            assert not clocks
            clocks.append(no_sdk_issuer.clock)
            return original_admit(conn, service, prepared, model=model, no_sdk_issuer=no_sdk_issuer)

        async def admit_through_original_service(self, *, prepared, requested_model, no_sdk_issuer, required_work):
            running = prepared.binding.running
            sid = str(running.claim.session_id)
            tables = (holds, attempts, usage)
            with store[0].connect() as conn:
                before = tuple(tuple(conn.execute(select(table).where(table.c.session_id == sid))) for table in tables)
            result = await original_service(
                self, prepared=prepared, requested_model=requested_model, no_sdk_issuer=no_sdk_issuer, required_work=required_work
            )
            outcomes.append(result)
            assert len(checks) == 2 and checks[0] is checks[1] is no_sdk_issuer.clock
            if timely:
                assert type(result) is RequiredSQLReturned
            else:
                assert type(result) is RequiredSQLRaised and type(result.error) is TimeoutError
                with store[0].connect() as conn:
                    assert load_snapshot(conn, session_id=sid, operation_id=running.claim.operation_id) == prepared.snapshot
                    assert tuple(tuple(conn.execute(select(table).where(table.c.session_id == sid))) for table in tables) == before
                    effect = conn.execute(
                        select(effects).where(
                            effects.c.session_id == sid,
                            effects.c.operation_id == running.claim.operation_id,
                            effects.c.effect_key == prepared.binding.effect_key,
                        )
                    ).one()
                assert effect.disposition == "prepared" and effect.provider_attempt_id is None
                assert not sdk
            return result

        async def completion(**_kwargs):
            sdk.append(1)
            return _reply(json.dumps(_answer(case["request"])) if family == "semantic" else "Original quota-stage PRIMARY reply")

        monkeypatch.setattr(ContinuationActionClock, "require_active_quota_time", check_original_quota)
        if family == "primary":
            monkeypatch.setattr(primary_sql, "admit_v4_primary_provider_on_connection", admit_original)
            monkeypatch.setattr(SessionServiceImpl, "begin_v4_continuation_primary_attempt", admit_through_original_service)
        else:
            monkeypatch.setattr(semantic_sql, "admit_v4_semantic_provider_on_connection", admit_original)
            monkeypatch.setattr(SessionServiceImpl, "begin_v4_continuation_semantic_attempt", admit_through_original_service)
        monkeypatch.setattr(litellm, "acompletion", completion)
        if family == "semantic" and not timely:
            with pytest.raises(TimeoutError, match="continuation quota or original action allowance expired"):
                await invoke()
        else:
            result = await invoke()
            if family == "primary":
                assert type(result) is (primary_fixture.PrimaryProviderResult if timely else primary_fixture.PrimaryNoSdkRefusalReceipt)
            else:
                assert result is None
        assert len(outcomes) == 1 and len(checks) == 2
        assert len(sdk) == (1 if timely else 0)
        work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_v4_semantic_large_original_is_retained_with_bounded_audit_and_cold_proof(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _large_complete_reply(case["request"])

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert result is None and len(sdk) == 1 and phases.latest_snapshot is not None
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            cold = read_semantic_advisor_outcome_on_connection(conn, running, selected=phases.latest_snapshot, effect_key=effect.effect_key)
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        assert effect.disposition == "committed" and hold.state == "consumed"
        assert cold.response is not None
        assert decode_continuation_response(cold.response).provider_metadata.reasoning_content == "r" * 100_000
        assert cold.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE


@pytest.mark.asyncio
async def test_v4_semantic_changed_original_metadata_is_rejected_before_paid_settlement(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _large_complete_reply(case["request"])

        original_capture = semantic_physical._capture_composer_llm_completion_fields

        def changed_original(*args, **kwargs):
            message, tool_calls, metadata = original_capture(*args, **kwargs)
            return message, tool_calls, replace(metadata, reasoning_content="changed after SDK return")

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(semantic_physical, "_capture_composer_llm_completion_fields", changed_original)
        phases = _phases(case)
        with pytest.raises(BaseExceptionGroup) as rejected:
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert len(rejected.value.exceptions) == 2
        assert all(
            type(error) is AuditIntegrityError and str(error) == "V4 result lost its original issued metadata capture"
            for error in rejected.value.exceptions
        )
        assert len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "dispatched" and hold.state == "active" and entries == []


@pytest.mark.asyncio
async def test_v4_semantic_changed_bounded_audit_is_rejected_before_paid_settlement(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _large_complete_reply(case["request"])

        original_record = semantic_physical.build_llm_call_record

        def changed_audit(*args, **kwargs):
            call = original_record(*args, **kwargs)
            return replace(call, reasoning_content="changed bounded audit") if call.status.value == "success" else call

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(semantic_physical, "build_llm_call_record", changed_audit)
        phases = _phases(case)
        with pytest.raises(AuditIntegrityError):
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "dispatched" and hold.state == "active" and entries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_model", (None, "openai/" + "m" * 204 + "\\" * 45), ids=("ordinary", "escaped-model"))
async def test_v4_semantic_pre_sdk_failure_closes_original_zero_use_and_cold_terminal(
    operation_store, tmp_path, monkeypatch, requested_model
):
    if requested_model is not None:
        original_settings = semantic_fixture._required_guard_settings

        def configured_settings(tmp_path, monkeypatch):
            settings, requests = original_settings(tmp_path, monkeypatch)
            return WebSettings.model_validate({**settings.model_dump(), "composer_advisor_model": requested_model}), requests

        monkeypatch.setattr(semantic_fixture, "_required_guard_settings", configured_settings)
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []
        admissions = []

        async def completion(**_kwargs):
            sdk.append(1)
            raise AssertionError("SDK must remain unentered")

        original = ProviderCallCustody.require_cost_dispatch

        def refuse_after_admission(self):
            if self.continuation_semantic_v4_admission is not None:
                admissions.append(self.continuation_semantic_v4_admission)
                raise TimeoutError("injected semantic pre-SDK after original H")
            return original(self)

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", refuse_after_admission)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert type(result) is SemanticAdvisorNoSdkTerminal and not sdk
        assert len(admissions) == 1
        admission = admissions[0]
        plan = admission.no_sdk_tail
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            cold = read_v4_semantic_no_sdk_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
            assert cold is not None
            terminal_published_at = restore_utc(
                conn.execute(
                    select(checkpoints.c.created_at).where(
                        checkpoints.c.session_id == sid,
                        checkpoints.c.operation_id == operation,
                        checkpoints.c.checkpoint_id == cold.terminal.checkpoint_id,
                    )
                ).scalar_one()
            )
            receipt_json = read_chunk(conn, session_id=sid, operation_id=operation, key=effect.result_chunk_key).decode("utf-8")
        assert cold is not None and cold.hold_state == "consumed"
        assert effect.disposition == "refused" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert len(entries) == 1 and entries[0].prompt_tokens == entries[0].completion_tokens == 0
        assert case["request"].options["model"] == plan.requested_model == effect.requested_model == entries[0].model
        assert cold.provider_attempt_id == admission.attempt.attempt_id == physical.attempt_id

        # Mutate coherent retained receipt bytes at the actual cold reader,
        # after the original H/zero-use settlement has completed. The storage
        # digest and length stay valid so each intended codec/binding check runs.
        for field, replacement, message in (
            ("schema_version", "composer.semantic-no-sdk-refusal.v9", "closed versioned codec"),
            ("receipt_key", effect.receipt_key + ":foreign", "original refusal receipt"),
        ):
            changed_receipt = json.loads(receipt_json)
            changed_receipt[field] = replacement
            changed_payload = canonical_json(changed_receipt).encode("utf-8")
            with store[0].begin() as conn:
                before = semantic_fixture._rows_on_connection(conn)
                savepoint = conn.begin_nested()
                try:
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == sid,
                            chunks.c.operation_id == operation,
                            chunks.c.chunk_key == effect.result_chunk_key,
                        )
                        .values(payload=changed_payload, sha256=sha256(changed_payload).hexdigest(), byte_length=len(changed_payload))
                    )
                    assert changed.rowcount == 1
                    damaged = semantic_fixture._rows_on_connection(conn)
                    with pytest.raises(AuditIntegrityError, match=message):
                        read_v4_semantic_no_sdk_terminal_on_connection(
                            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
                        )
                    assert semantic_fixture._rows_on_connection(conn) == damaged and not sdk
                finally:
                    savepoint.rollback()
                assert semantic_fixture._rows_on_connection(conn) == before
                assert (
                    read_v4_semantic_no_sdk_terminal_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
                    )
                    == cold
                )
                assert not sdk

        def reconstruct(tail_plan, retained_receipt=receipt_json):
            return reconstruct_semantic_no_sdk_tail_batch(
                tail_plan,
                receipt_json=retained_receipt,
                input_sha256=effect.input_sha256,
                effect_key=effect.effect_key,
                creator_sequence=effect.creator_sequence,
                creator_generation=effect.creator_generation,
                published_at=terminal_published_at,
            )

        if requested_model is not None:
            assert requested_model.isprintable() and len(requested_model.encode("utf-8")) == 256
            assert plan.requested_model == requested_model
            receipt_bytes = len(receipt_json.encode("utf-8"))
            assert len(str(plan.maximum_receipt_bytes)) == len(str(receipt_bytes)) + 1
            terminal_bytes = len(cold.terminal.payload_json.encode("utf-8"))
            assert terminal_bytes + 1 == plan.terminal_snapshot_bytes
            tail = reconstruct(plan)
            assert tail.append.snapshot_json == cold.terminal.payload_json
            actual_bytes = sum(len(item.payload) for item in tail.prospective)
            assert actual_bytes <= plan.reserved_bytes <= admission.hold.reserved_bytes

            # These copies are bound-check DATA opponents, never issued SQL authority.
            for smaller_bound in (
                replace(plan, terminal_snapshot_bytes=terminal_bytes - 1),
                replace(plan, reserved_bytes=actual_bytes - 1),
            ):
                with pytest.raises(AuditIntegrityError, match="cold tail exceeded its original H"):
                    reconstruct(smaller_bound)
            oversized_receipt = json.loads(receipt_json)
            oversized_receipt["guard_disposition"] = "x" * plan.maximum_receipt_bytes
            oversized_json = canonical_json(oversized_receipt)
            assert len(oversized_json.encode("utf-8")) > plan.maximum_receipt_bytes
            with pytest.raises(AuditIntegrityError, match="cold receipt changed its exact original source"):
                reconstruct(plan, oversized_json)

            root = decode_continuation_snapshot(cold.terminal.payload_json)
            frame = root.frames[-1]
            assert type(frame) is AdvisorFrame and frame.response is not None
            changed_frame = frame.model_copy(update={"response": frame.response.model_copy(update={"byte_length": receipt_bytes - 1})})
            changed_json = encode_continuation_snapshot(root.model_copy(update={"frames": (*root.frames[:-1], changed_frame)}))
            changed_payload = changed_json.encode("utf-8")
            assert changed_json != cold.terminal.payload_json and len(changed_payload) <= plan.terminal_snapshot_bytes
            with store[0].begin() as conn:
                savepoint = conn.begin_nested()
                try:
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == sid,
                            chunks.c.operation_id == operation,
                            chunks.c.chunk_key == f"snapshot:{cold.terminal.checkpoint_id}",
                        )
                        .values(payload=changed_payload, sha256=sha256(changed_payload).hexdigest(), byte_length=len(changed_payload))
                    )
                    assert changed.rowcount == 1
                    assert load_snapshot(conn, session_id=sid, operation_id=operation).payload_json == changed_json
                    with pytest.raises(AuditIntegrityError, match="lost its exact failed parent/root"):
                        read_v4_semantic_no_sdk_terminal_on_connection(
                            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
                        )
                finally:
                    savepoint.rollback()
                assert (
                    read_v4_semantic_no_sdk_terminal_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
                    )
                    == cold
                )


@pytest.mark.asyncio
async def test_v4_semantic_unretainable_returned_audit_is_one_paid_terminal_without_retry(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _unretainable_reply(case["request"])

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert type(result) is SemanticAdvisorPaidIncompleteTerminal and len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            cold = read_v4_semantic_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
        assert cold == result.verified and cold.hold_state == "consumed"
        assert cold.receipt.reason == "capture_budget_exceeded" and cold.receipt.capture_surface == "provider_metadata"
        assert effect.disposition == "paid_incomplete" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert len(entries) == 1 and entries[0].model == sdk[0]
    assert type(effect.provider_attempt_id) is str
    with store[0].connect() as conn:
        original_rows = conn.execute(
            select(messages).where(
                messages.c.session_id == sid, messages.c.role == "audit", messages.c.content == "Provider call result recorded."
            )
        ).all()
        original_audits = [row for row in original_rows if effect.provider_attempt_id in json.dumps(row.tool_calls)]
        assert len(original_audits) == 1
        original_row = original_audits[0]
        original_call = _closed_call(
            original_row,
            ProviderAuditSource(cold.source.session_id, cold.source.session_operation_id, cold.source.session_operation_epoch),
            effect.provider_attempt_id,
        )
    assert original_call.messages_hash != "0" * 64
    audit_source = ProviderAuditSource(cold.source.session_id, cold.source.session_operation_id, cold.source.session_operation_epoch)

    def write_public_call(call):
        envelope = llm_call_audit_envelope(call)
        gap = visible_response_gap_for_source(call, audit_source, envelope)
        with store[0].begin() as conn:
            conn.execute(update(messages).where(messages.c.id == original_row.id).values(tool_calls=[envelope, gap]))

    write_public_call(replace(original_call, messages_hash="0" * 64))
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original physical request"):
        read_v4_semantic_paid_incomplete_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
        )
    assert effect.response_finished_at is not None
    forged_finish = restore_utc(effect.response_finished_at) + timedelta(seconds=1)
    write_public_call(replace(original_call, finished_at=forged_finish))
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="recorded physical time bucket"):
        read_v4_semantic_paid_incomplete_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
        )
    write_public_call(original_call)
    with store[0].begin() as conn:
        assert (
            read_v4_semantic_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
            == cold
        )
        assert conn.dialect.name == "sqlite"
        original_trigger = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
        ).scalar_one()
        savepoint = conn.begin_nested()
        try:
            conn.exec_driver_sql("DROP TRIGGER trg_chat_messages_no_delete")
            conn.execute(delete(messages).where(messages.c.id == original_row.id))
            with pytest.raises(AuditIntegrityError, match="public row"):
                read_v4_semantic_paid_incomplete_terminal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
                )
        finally:
            savepoint.rollback()
        assert (
            conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'").scalar_one()
            == original_trigger
        )
        assert (
            read_v4_semantic_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
            == cold
        )
        with pytest.raises(IntegrityError, match="chat_messages rows are append-only"):
            conn.execute(delete(messages).where(messages.c.id == original_row.id))


@pytest.mark.asyncio
async def test_v4_semantic_direct_paid_incomplete_sql_rejects_forged_parser_exception(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _unretainable_reply(case["request"])

        monkeypatch.setattr(litellm, "acompletion", completion)
        original_sql = SessionServiceImpl.complete_v4_semantic_paid_incomplete_sync

        def forged_sql(self, *, prepared, admission, incomplete, no_sdk_issuer):
            copied = V4ProviderResultIncomplete(sdk_return=incomplete.sdk_return, metadata=incomplete.metadata, losses=incomplete.losses)
            return original_sql(self, prepared=prepared, admission=admission, incomplete=copied, no_sdk_issuer=no_sdk_issuer)

        monkeypatch.setattr(SessionServiceImpl, "complete_v4_semantic_paid_incomplete_sync", forged_sql)
        phases = _phases(case)
        with pytest.raises(AuditIntegrityError):
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "dispatched" and hold.state == "active"
        assert physical.settled_at is None and physical.ledger_entry_id is None and entries == []
