"""Family-1 SQL lifecycle controls before any live semantic caller.

Use the existing first-Compose, required guard and physical SDK fixtures.
No test manufactures a permit, announces SDK entry, or tells an issuer that
undispatched settlement succeeded. Those notifications must come from the
original physical owner. Cold decision installation accepts SQL locators,
never a caller-supplied decoded decision or action source.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import httpx
import litellm
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.advisor_output import parse_continuation_advisor_decision
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_continuation_text_response,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_semantic_advisor import prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_advisor_outcome import (
    SemanticAdvisorInvocationRaised,
    SemanticAdvisorInvocationReturned,
    observe_semantic_advisor_invocation,
)
from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation, semantic_advisor_action_key
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions.continuation import ContinuationCASConflict, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.composer.test_explicit_provider_custody import _required_guard_settings
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions import test_continuation_prompt_producers as producer_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input
from tests.unit.web.sessions.test_continuation_primary_result import operation_store as _operation_store

operation_store = _operation_store


def _rows_on_connection(conn):
    return tuple(
        tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
        for table in (jobs, effects, ordinals, chunks, checkpoints, events, physical, usage, messages)
    )


def _rows(engine):
    with engine.connect() as conn:
        return _rows_on_connection(conn)


@asynccontextmanager
async def _semantic_case(store, tmp_path, monkeypatch, *, blocked=False):
    settings, _requests = _required_guard_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(producer_fixture, "WebSettings", lambda **values: settings.model_copy(update=values))
    runtime, work, _primary_guard_requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    assert len(runtime.source.selected.stack.frames) == 1
    assert type(runtime.source.selected.stack.frames[0]) is ComposeFrame
    owner = runtime.materialization.service._advisor_checkpoint
    if blocked:
        configuration = owner._settings.composer_advisor_prompt_guard
        assert configuration is not None

        def reject(request):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "userPromptAnalysis": {"attackDetected": True},
                    "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
                },
            )

        monkeypatch.setattr(
            owner,
            "_prompt_guard_mount",
            PromptGuardMount(AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(reject))),
        )
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:
        clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
        raw = capture_semantic_advisor_source(runtime)
        invocation = make_semantic_advisor_invocation(raw)
        request = prepare_semantic_advisor_request(
            owner=owner,
            provider_owner=runtime.bindings.provider_owner,
            source=invocation.invocation,
            frontier=invocation.frontier,
            state=runtime.live.state,
            content_scope=ComposerContentScope(UUID(runtime.bindings.session_id), composition_content_hash(runtime.live.state)),
            user_message=runtime.bindings.message,
            runtime=runtime,
            env_ref_names=runtime.env_ref_names,
        )
        yield {
            "runtime": runtime,
            "running": running,
            "work": work,
            "clock": clock,
            "invocation": invocation,
            "request": request,
            "provider": runtime.bindings.provider_owner,
            "recorder": runtime.live.recorder,
        }
    finally:
        await lease.close()
        work.coordinator.assert_completed()


def _issuer(case):
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer

    selected = case["runtime"].source.selected
    return SemanticAdvisorNoSdkIssuer(
        running=case["running"],
        work=case["work"],
        selected_checkpoint_id=selected.checkpoint_id,
        selected_cursor=selected.stack.cursor,
        input_sha256=case["request"].request_sha256,
        clock=case["clock"],
    )


def _prepare(conn, case, issuer, *, selected=None):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    return semantic_sql.prepare_semantic_advisor_on_connection(
        conn,
        case["running"],
        selected=case["runtime"].source.selected if selected is None else selected,
        invocation=case["invocation"],
        request=case["request"],
        inventory=case["runtime"].materialization.inventory,
        no_sdk_issuer=issuer,
    )


def _begin(store, case):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    issuer = _issuer(case)
    with locked_session_transaction(store[0], str(store[4])) as conn:
        prepared = _prepare(conn, case, issuer)
    issuer.bind_prepared(prepared)
    with locked_session_transaction(store[0], str(store[4])) as conn:
        dispatched = semantic_sql.begin_semantic_advisor_on_connection(conn, prepared, no_sdk_issuer=issuer)
    issuer.bind_dispatched(dispatched)
    return issuer, prepared, dispatched


async def _invoke(case, issuer):
    # This keyword requires the original helper/custody hook. The test does
    # not supply a custody, fake callbacks, or a preconstructed permit.
    return await observe_semantic_advisor_invocation(
        case["request"],
        provider_owner=case["provider"],
        recorder=case["recorder"],
        timeout=5,
        no_sdk_issuer=issuer,
    )


def _physical_preimage(request):
    return canonical_json(
        {
            "source": request.source.model_dump(mode="json"),
            "frontier": request.source.premise.frontier.model_dump(mode="json"),
            "messages": request.prepared.evidence.messages,
            "options": {key: value for key, value in deep_thaw(request.options).items() if key != "api_key"},
            "genuine_user": request.prepared.genuine_user,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [None, "unknown_version", "foreign_receipt"])
async def test_original_semantic_no_sdk_refusal_requires_exact_issuer_and_round_trips_cold(
    operation_store, tmp_path, monkeypatch, corruption
):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkRefusalReason

    async with _semantic_case(operation_store, tmp_path, monkeypatch, blocked=True) as case:
        before_root = case["runtime"].source.selected.stack
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, _dispatched = _begin(operation_store, case)
        primary_custody = case["provider"].mint(ProviderInvocationFamily.PRIMARY)
        with pytest.raises(AuditIntegrityError, match="ADVISOR"):
            primary_custody.bind_semantic_no_sdk_issuer(issuer)
        assert primary_custody.continuation_semantic_clock is None
        observed = await _invoke(case, issuer)
        assert type(observed) is SemanticAdvisorInvocationRaised
        assert type(observed.error) is PromptGuardRefused
        assert observed.observation.original_failure is observed.error
        assert observed.observation.sdk_entered is False and observed.observation.attempt is None
        assert observed.observation.audit_call is None and case["recorder"].llm_calls == ()
        assert sdk == [] and _checks(operation_store[0])[-1]["audit_id"] == observed.error.audit_id
        permit = issuer.reserve_refusal(reason=SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED, original_failure=observed.error)
        before = _rows(operation_store[0])
        for counterfeit in (observed.observation, replace(permit)):
            with (
                locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
                pytest.raises((TypeError, AuditIntegrityError)),
            ):
                semantic_sql.refuse_semantic_advisor_no_sdk_on_connection(conn, counterfeit)
        assert _rows(operation_store[0]) == before
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            receipt = semantic_sql.refuse_semantic_advisor_no_sdk_on_connection(conn, permit)
        root = decode_continuation_snapshot(receipt.snapshot.payload_json)
        assert root.logical == before_root.logical and root.admission_policy_json == before_root.admission_policy_json
        assert tuple(frame for frame in root.frames if type(frame) is ComposeFrame) == before_root.frames
        assert type(root.frames[-1]) is AdvisorFrame
        assert root.frames[-1].response.codec == "composer.semantic-no-sdk-refusal.v1"
        assert root.active_action_json is root.active_action_source is None
        with operation_store[0].connect() as conn:
            cold = semantic_sql.read_semantic_advisor_no_sdk_refusal_on_connection(
                conn, case["running"], selected=receipt.snapshot, effect_key=prepared.binding.effect_key
            )
            payload = conn.execute(select(chunks.c.payload).where(chunks.c.chunk_key == receipt.result_chunk_key)).scalar_one()
        assert cold == receipt
        document = json.loads(payload)
        assert document["schema_version"] == "composer.semantic-no-sdk-refusal.v1"
        assert document["effect_key"] == prepared.binding.effect_key
        assert document["input_sha256"] == prepared.binding.input_sha256 == case["request"].request_sha256
        with pytest.raises(ValidationError):
            PrivateChunkRef(codec="composer.semantic-no-sdk-refusal.v999", digest="0" * 64, byte_length=0)
        if corruption is not None:
            document["schema_version" if corruption == "unknown_version" else "receipt_key"] = (
                "composer.semantic-no-sdk-refusal.v999" if corruption == "unknown_version" else "foreign-original-receipt"
            )
            forged = encode_private_chunk(codec="composer.semantic-no-sdk-refusal.v1", canonical=canonical_json(document))
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                savepoint = conn.begin_nested()
                try:
                    put_chunk(
                        conn,
                        session_id=str(operation_store[4]),
                        operation_id=case["running"].claim.operation_id,
                        key=private_chunk_key(forged),
                        payload=forged.canonical.encode("utf-8"),
                        codec=forged.reference.codec,
                    )
                    changed = conn.execute(
                        update(effects)
                        .where(effects.c.effect_key == prepared.binding.effect_key)
                        .values(result_chunk_key=private_chunk_key(forged))
                    )
                    assert changed.rowcount == 1
                    with pytest.raises(AuditIntegrityError):
                        semantic_sql.read_semantic_advisor_no_sdk_refusal_on_connection(
                            conn, case["running"], selected=receipt.snapshot, effect_key=prepared.binding.effect_key
                        )
                finally:
                    savepoint.rollback()
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(AuditIntegrityError),
        ):
            semantic_sql.install_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=receipt.snapshot, effect_key=prepared.binding.effect_key
            )
        assert _rows(operation_store[0]) == before and sdk == []


@pytest.mark.asyncio
async def test_entered_semantic_attempt_cannot_be_closed_as_no_sdk_after_actual_cleanup(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkRefusalReason

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []
        original = TimeoutError("Original entered semantic SDK timeout")

        async def completion(**kwargs):
            sdk.append(kwargs)
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert type(observed) is SemanticAdvisorInvocationRaised
        assert observed.observation.original_failure is original
        assert observed.observation.sdk_entered is True and observed.observation.attempt is not None
        assert observed.observation.audit_call.status is ComposerLLMCallStatus.TIMEOUT
        assert len(sdk) == 1
        with pytest.raises(AuditIntegrityError):
            issuer.reserve_refusal(reason=SemanticAdvisorNoSdkRefusalReason.PRE_SDK_DEADLINE, original_failure=original)
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(AuditIntegrityError),
        ):
            semantic_sql.retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
        assert _rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_semantic_prepare_retains_exact_physical_preimage_and_rolls_back_complete_cohort(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            savepoint = conn.begin_nested()
            try:
                prepared = _prepare(conn, case, _issuer(case))
                assert prepared.binding.family == 1 and prepared.binding.effect_key == f"provider:1:{prepared.binding.ordinal}"
                descriptor = conn.execute(select(chunks).where(chunks.c.codec == "composer.semantic-provider-input.v1")).one()
                body = json.loads(descriptor.payload)
                assert body["physical_request_json"] == _physical_preimage(case["request"])
                assert sha256(body["physical_request_json"].encode("utf-8")).hexdigest() == prepared.binding.input_sha256
                assert body["raw_source_body_json"] == case["invocation"].source.source_body_json
                assert body["request_input_json"] == case["invocation"].request_input_json
                assert body["policy_preimage_json"] == case["invocation"].policy_preimage_json
                with pytest.raises((AuditIntegrityError, ContinuationCASConflict)):
                    semantic_sql.prepare_semantic_advisor_on_connection(
                        conn,
                        case["running"],
                        selected=case["runtime"].source.selected,
                        invocation=case["invocation"],
                        request=case["request"],
                        inventory=case["runtime"].materialization.inventory,
                        no_sdk_issuer=_issuer(case),
                    )
            finally:
                savepoint.rollback()
        assert _rows(operation_store[0]) == before


@pytest.mark.asyncio
async def test_semantic_prepare_refuses_same_locator_with_changed_complete_root_before_writes(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        selected = case["runtime"].source.selected
        frame = selected.stack.frames[0]
        changed = replace(
            selected,
            stack=selected.stack.model_copy(update={"frames": (frame.model_copy(update={"advisor_calls": frame.advisor_calls + 1}),)}),
        )
        assert changed.checkpoint_id == selected.checkpoint_id and changed.stack.cursor == selected.stack.cursor
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(ContinuationCASConflict),
        ):
            semantic_sql.prepare_semantic_advisor_on_connection(
                conn,
                case["running"],
                selected=changed,
                invocation=case["invocation"],
                request=case["request"],
                inventory=case["runtime"].materialization.inventory,
                no_sdk_issuer=_issuer(case),
            )
        assert _rows(operation_store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["ledger", "public_audit", "physical_creator", "input_sha256", "result_binding", "selected_body"])
async def test_semantic_decision_cold_join_reproves_actual_paid_source_before_action(operation_store, tmp_path, monkeypatch, corruption):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert type(observed) is SemanticAdvisorInvocationReturned
        assert observed.observation.attempt.attempt_id == observed.result.audit_call.call_id == observed.result.decision.decision_id
        assert len(sdk) == 1 and observed.result.audit_call.provider_cost == 0.01
        context = observed.result.audit_call.private_request
        assert context.disposition == "retained"
        original_wire = json.loads(context.canonical)
        assert original_wire["messages"] == case["request"].prepared.evidence.messages
        assert original_wire["tools"] is None
        copied = replace(observed, observation=replace(observed.observation, audit_call=replace(observed.result.audit_call)))
        alternative = _answer(case["request"])
        alternative["reason"] = "Substituted words from a different decision, with the same paid call."
        alternative["prescribed_action"]["instruction"] = "A different instruction that this paid call did not produce."
        text = json.dumps(alternative)
        substituted_decision = parse_continuation_advisor_decision(text, actual_advisor_call_id=observed.result.audit_call.call_id)
        assert substituted_decision is not None and substituted_decision != observed.result.decision
        assert semantic_advisor_action_key(
            case["invocation"],
            substituted_decision,
            actual_advisor_call_id=observed.result.audit_call.call_id,
            permitted_read_tools=frozenset(),
            complete_required_coverage=False,
        ) != semantic_advisor_action_key(
            case["invocation"],
            observed.result.decision,
            actual_advisor_call_id=observed.result.audit_call.call_id,
            permitted_read_tools=frozenset(),
            complete_required_coverage=False,
        )
        substituted_observation = replace(
            observed.observation,
            message=replace(observed.observation.message, content=text),
            decision=substituted_decision,
        )
        substituted_result = replace(
            observed.result,
            decision=substituted_decision,
            response=encode_continuation_text_response(
                content=text, provider_metadata=observed.observation.provider_metadata, audit_call=observed.result.audit_call
            ),
        )
        returned = replace(observed, observation=substituted_observation, result=substituted_result)
        raised = SemanticAdvisorInvocationRaised(substituted_observation, RuntimeError("A fabricated outward failure after the same call"))
        before = _rows(operation_store[0])
        for counterfeit in (copied, returned, raised):
            if counterfeit is not copied:
                assert counterfeit.observation.audit_call is observed.observation.audit_call
                assert counterfeit.observation.attempt is observed.observation.attempt
                assert counterfeit.observation.provider_metadata is observed.observation.provider_metadata
            with (
                locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
                pytest.raises(AuditIntegrityError, match="original effect, minted custody and complete outcome"),
            ):
                semantic_sql.retain_semantic_advisor_outcome_on_connection(
                    conn, prepared, selected=dispatched, outcome=counterfeit, recorder=case["recorder"], issuer=issuer
                )
            assert _rows(operation_store[0]) == before
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            retained = semantic_sql.retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
        budget = case["provider"].cost_budget.snapshot()

        async def forbidden_sdk(**kwargs):
            raise AssertionError("cold semantic admission cannot replay an original paid call")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            installed = semantic_sql.install_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
        root = decode_continuation_snapshot(installed.payload_json)
        assert root.active_action_json == canonical_json(observed.result.decision.prescribed_action.model_dump(mode="json"))
        assert root.active_action_source.effect_key == prepared.binding.effect_key
        assert root.active_action_source.input_sha256 == prepared.binding.input_sha256
        assert root.active_action_source.response == observed.result.response.reference
        assert sum(type(frame) is ComposeFrame for frame in root.frames) == 1
        with operation_store[0].connect() as conn:
            cold = semantic_sql.read_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
        assert cold.decision == observed.result.decision and cold.source == root.active_action_source
        assert cold.action == observed.result.decision.prescribed_action
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            savepoint = conn.begin_nested()
            selected = installed
            try:
                if corruption == "ledger":
                    entry_id = _usage_entry_id(
                        session_id=str(operation_store[4]), source="composer", run_id=None, call_id=observed.result.audit_call.call_id
                    )
                    changed = conn.execute(update(usage).where(usage.c.entry_id == entry_id).values(prompt_tokens=700))
                    assert changed.rowcount == 1
                elif corruption == "public_audit":
                    audit = next(
                        row
                        for row in conn.execute(select(messages))
                        if row.tool_calls and any(item.get("_kind") == "llm_call_audit" for item in row.tool_calls)
                    )
                    changed = conn.execute(update(messages).where(messages.c.id == audit.id).values(tool_calls=[]))
                    assert changed.rowcount == 1
                elif corruption == "physical_creator":
                    changed = conn.execute(
                        update(physical)
                        .where(physical.c.attempt_id == observed.result.audit_call.call_id)
                        .values(lease_token=case["running"].session_operation_context.fence.lease_token + "-foreign")
                    )
                    assert changed.rowcount == 1
                elif corruption == "input_sha256":
                    changed = conn.execute(
                        update(effects)
                        .where(effects.c.effect_key == prepared.binding.effect_key)
                        .values(input_sha256=stable_hash("different original physical request"))
                    )
                    assert changed.rowcount == 1
                elif corruption == "result_binding":
                    changed = conn.execute(
                        update(effects)
                        .where(effects.c.effect_key == prepared.binding.effect_key)
                        .values(result_chunk_key=private_chunk_key(case["invocation"].source.state))
                    )
                    assert changed.rowcount == 1
                else:
                    parent = root.frames[0]
                    altered = root.model_copy(
                        update={"frames": (parent.model_copy(update={"advisor_calls": parent.advisor_calls + 1}), *root.frames[1:])}
                    )
                    selected = replace(installed, payload_json=encode_continuation_snapshot(altered))
                with pytest.raises((AuditIntegrityError, ContinuationCASConflict)):
                    semantic_sql.read_semantic_advisor_decision_on_connection(
                        conn, case["running"], selected=selected, effect_key=prepared.binding.effect_key
                    )
            finally:
                savepoint.rollback()
            assert (
                semantic_sql.read_semantic_advisor_decision_on_connection(
                    conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
                )
                == cold
            )
        assert _rows(operation_store[0]) == before
        assert len(sdk) == 1 and case["provider"].cost_budget.snapshot() == budget


@pytest.mark.asyncio
async def test_paid_malformed_semantic_text_is_bound_to_original_raised_outcome(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        original_text = "The actual paid response contains no valid semantic decision."
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(original_text)

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert type(observed) is SemanticAdvisorInvocationRaised
        assert observed.observation.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        assert observed.observation.decision is None and observed.observation.message.content == original_text
        context = observed.observation.audit_call.private_request
        assert context.disposition == "retained"
        original_wire = json.loads(context.canonical)
        assert original_wire["messages"] == case["request"].prepared.evidence.messages
        assert original_wire["tools"] is None
        counterfeit = replace(
            observed,
            observation=replace(
                observed.observation, message=replace(observed.observation.message, content="Different paid-invalid words.")
            ),
        )
        assert counterfeit.observation.audit_call is observed.observation.audit_call
        assert counterfeit.observation.attempt is observed.observation.attempt and counterfeit.error is observed.error
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(AuditIntegrityError, match="original effect, minted custody and complete outcome"),
        ):
            semantic_sql.retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=counterfeit, recorder=case["recorder"], issuer=issuer
            )
        assert _rows(operation_store[0]) == before
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            retained = semantic_sql.retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
        assert retained.outcome == "paid_invalid" and retained.decision is None and retained.original_error is observed.error
        assert decode_continuation_response(retained.response).message.content == original_text
        with operation_store[0].connect() as conn:
            cold = semantic_sql.read_semantic_advisor_outcome_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
        assert cold.outcome == "paid_invalid" and cold.decision is None
        assert decode_continuation_response(cold.response).message.content == original_text
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(AuditIntegrityError),
        ):
            semantic_sql.install_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
        assert _rows(operation_store[0]) == before and len(sdk) == 1
