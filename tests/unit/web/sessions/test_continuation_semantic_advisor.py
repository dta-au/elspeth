"""Family-1 SQL lifecycle controls before any live semantic caller.

Use the existing first-Compose, required guard and physical SDK fixtures.
No test manufactures a permit, announces SDK entry, or tells an issuer that
undispatched settlement succeeded. Those notifications must come from the
original physical owner. Cold decision installation accepts SQL locators,
never a caller-supplied decoded decision or action source.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import httpx
import litellm
import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_provider_context import PrivateProviderContext
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.advisor_output import parse_continuation_advisor_decision
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_continuation_text_response,
)
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call, encode_physical_audit_call
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor import prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_advisor_outcome import (
    SemanticAdvisorInvocationRaised,
)
from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation, semantic_advisor_action_key
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, provider_attempt_audit_drafts_for_source
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation import ContinuationCASConflict, load_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc
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
        for table in (jobs, effects, ordinals, chunks, checkpoints, events, physical, usage, messages, holds)
    )


def _rows(engine):
    with engine.connect() as conn:
        return _rows_on_connection(conn)


@asynccontextmanager
async def _semantic_case(store, tmp_path, monkeypatch, *, blocked=False, end_candidate=False, timed_read=False):
    settings, _requests = _required_guard_settings(tmp_path, monkeypatch)
    if timed_read:
        from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_fixture

        actual_work = initialization_fixture.inventory_work

        def timed_work(running):
            stored = store[2].load_continuation_snapshot(running)
            root = decode_continuation_snapshot(stored.payload_json)
            policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
            sampled_at = time.monotonic()
            with store[0].connect() as conn:
                observed_at = database_now(conn)
                deadline = conn.execute(
                    select(segments.c.deadline_at).where(
                        segments.c.session_id == str(running.claim.session_id),
                        segments.c.operation_id == running.claim.operation_id,
                        segments.c.sequence == running.claim.segment_sequence,
                    )
                ).scalar_one()
            assert deadline is not None
            remaining = (restore_utc(deadline) - observed_at).total_seconds()
            assert remaining > 0
            sql_deadline = ContinuationSQLDeadlineOwner(
                running,
                policy=policy,
                drain_signal=ComposerDrainSignal(),
                drain_seconds=settings.composer_async_drain_seconds,
                hard_expires_at=sampled_at + remaining,
            )
            return actual_work(running, sql_deadline_owner=sql_deadline)

        monkeypatch.setattr(initialization_fixture, "inventory_work", timed_work)
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
        if end_candidate:
            from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
            from elspeth.web.composer.continuation_primary import run_continuation_primary
            from elspeth.web.sessions.continuation import load_snapshot
            from elspeth.web.sessions.continuation_end import enter_continuation_end
            from elspeth.web.sessions.continuation_primary import PrimaryProviderResult

            async def primary_completion(**_kwargs):
                return _reply("This is the actual retained PRIMARY candidate awaiting its real END review.")

            monkeypatch.setattr(litellm, "acompletion", primary_completion)
            phases = ContinuationLivePhases(
                service=runtime.materialization.service,
                authority=runtime.materialization.authority,
                work=work,
                provider_owner=runtime.bindings.provider_owner,
                lease=lease,
                progress=runtime.bindings.progress,
            )
            phases.bind_initial(runtime)
            primary = await run_continuation_primary(
                service=phases.service,
                authority=phases.authority,
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=runtime.bindings.provider_owner,
                timeout=runtime.source.policy.actions.primary.total,
            )
            assert type(primary) is PrimaryProviderResult
            phases.accept_primary(primary)
            with locked_session_transaction(store[0], str(store[4])) as conn:
                current = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
            selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
            await enter_continuation_end(phases, selected)
            runtime = phases.runtime
            assert runtime is not None
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
            "sql_deadline": work.coordinator.sql_deadline_owner,
            "lease": lease,
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


def _semantic_phases(case):
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

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
    clock = case["clock"]
    phases.bind_action_clock(clock)
    deadline_owner = case["work"].coordinator.sql_deadline_owner
    if deadline_owner is not None:
        deadline_owner.begin_action(clock.policy, started_at=clock.started_monotonic)
    return phases


async def _run_paid_semantic(case, monkeypatch):
    """Run one original live action, stopping at its paid pre-install root."""
    from elspeth.web.composer.continuation_semantic_advisor_phase import run_continuation_semantic_advisor
    from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorProviderResult
    from elspeth.web.sessions.service import SessionServiceImpl

    runtime = case["runtime"]
    phases = _semantic_phases(case)
    original = SessionServiceImpl.complete_v4_semantic_result_sync
    completed = []

    def observe_completion(self, *, prepared, admission, observation, recorder, no_sdk_issuer):
        paid = original(
            self,
            prepared=prepared,
            admission=admission,
            observation=observation,
            recorder=recorder,
            no_sdk_issuer=no_sdk_issuer,
        )
        completed.append((prepared, paid))
        return paid

    with monkeypatch.context() as capture:
        capture.setattr(SessionServiceImpl, "complete_v4_semantic_result_sync", observe_completion)
        result = await run_continuation_semantic_advisor(phases, runtime.source.selected)
    assert result is None and len(completed) == 1
    prepared, paid = completed[0]
    assert type(paid) is SemanticAdvisorProviderResult
    assert phases.latest_snapshot == paid.snapshot
    root = decode_continuation_snapshot(paid.snapshot.payload_json)
    assert type(root.frames[-1]) is AdvisorFrame and root.frames[-1].phase == "response_retained"
    return prepared, paid


async def _runtime_source_at(case, stored):
    runtime = case["runtime"]
    authority = runtime.materialization.authority
    selected = await load_selected_continuation(authority=authority, running=case["running"], selected=stored, required_work=case["work"])
    inventory = await materialize_continuation_env_inventory(
        advisor=runtime.materialization.service._advisor_checkpoint,
        authority=authority,
        selected=selected,
        required_work=case["work"],
    )
    source = authority.read_continuation_runtime(case["running"], selected=selected, inventory=inventory)
    assert source.selected is selected
    return source


def _observe_unresolved_admission(case, monkeypatch):
    from elspeth.web.required_sql_outcomes import RequiredSQLReturned
    from elspeth.web.sessions.service import SessionServiceImpl

    actual = SessionServiceImpl.begin_v4_continuation_semantic_attempt
    admissions = []

    async def admit(self, *, prepared, requested_model, no_sdk_issuer, required_work):
        result = await actual(
            self,
            prepared=prepared,
            requested_model=requested_model,
            no_sdk_issuer=no_sdk_issuer,
            required_work=required_work,
        )
        assert type(result) is RequiredSQLReturned
        admissions.append(result.value)
        begun = decode_continuation_snapshot(result.value.selected.payload_json)
        original = case["runtime"].source.selected.stack
        assert begun.frames[:-1] == original.frames and type(begun.frames[-1]) is AdvisorFrame
        with pytest.raises(AuditIntegrityError, match="unresolved prepared/begun"):
            await _runtime_source_at(case, result.value.selected)
        return result

    monkeypatch.setattr(SessionServiceImpl, "begin_v4_continuation_semantic_attempt", admit)
    return admissions


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
async def test_semantic_review_preserves_actual_end_candidate_and_full_three_frame_paid_source(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql
    from elspeth.web.composer.advisor_output import ReviewContinuationAction
    from elspeth.web.composer.continuation import EndFrame
    from elspeth.web.composer.continuation_end_values import continuation_candidate_id, decode_end_candidate, encode_end_candidate
    from elspeth.web.sessions.continuation_selection import load_private_chunks
    from elspeth.web.sessions.continuation_semantic_actions import (
        install_semantic_action_on_connection,
        require_consumed_semantic_review_on_connection,
    )

    async with _semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=True) as case:
        original = case["runtime"].source.selected.stack
        assert len(original.frames) == 2 and type(original.frames[1]) is EndFrame
        end = original.frames[1]
        original_candidate = decode_end_candidate(
            next(chunk for chunk in case["runtime"].source.selected.chunks if chunk.reference == end.candidate)
        )
        document = json.loads(case["request"].prepared.evidence.messages[-1]["content"])
        assert document["end_candidate"] == original_candidate.model_dump(mode="json")
        answer = _answer(case["request"])
        prescribed = answer["prescribed_action"]
        prescribed.pop("instruction")
        prescribed.update(
            kind="review",
            candidate_ref=continuation_candidate_id(original_candidate),
            evidence_binding=case["request"].source.coverage_binding,
        )
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(answer))

        monkeypatch.setattr(litellm, "acompletion", completion)
        changed_candidate = encode_end_candidate(
            original_candidate.model_copy(update={"candidate_text": "A coherently shaped substitute."})
        )
        original_selected = case["runtime"].source.selected
        changed_root = original.model_copy(
            update={"frames": (original.frames[0], end.model_copy(update={"candidate": changed_candidate.reference}))}
        )
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            original_rows = _rows_on_connection(conn)
            savepoint = conn.begin_nested()
            try:
                put_chunk(
                    conn,
                    session_id=str(operation_store[4]),
                    operation_id=case["running"].claim.operation_id,
                    key=private_chunk_key(changed_candidate),
                    payload=changed_candidate.canonical.encode("utf-8"),
                    codec=changed_candidate.reference.codec,
                )
                changed_selected = replace(
                    original_selected,
                    stack=changed_root,
                    chunks=load_private_chunks(
                        conn, session_id=str(operation_store[4]), operation_id=case["running"].claim.operation_id, stack=changed_root
                    ),
                )
                unchanged = _rows_on_connection(conn)
                with pytest.raises(ContinuationCASConflict, match="complete selected root"):
                    _prepare(conn, case, _issuer(case), selected=changed_selected)
                assert _rows_on_connection(conn) == unchanged
            finally:
                savepoint.rollback()
            assert _rows_on_connection(conn) == original_rows
        admissions = _observe_unresolved_admission(case, monkeypatch)
        prepared, paid = await _run_paid_semantic(case, monkeypatch)
        assert len(admissions) == 1
        retained_root = decode_continuation_snapshot(paid.snapshot.payload_json)
        assert len(retained_root.frames) == 3 and retained_root.frames[1] == end
        restored_source = await _runtime_source_at(case, paid.snapshot)
        assert len(restored_source.selected.stack.frames) == 3 and restored_source.selected.stack.frames[1] == end
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            actual = semantic_sql.read_current_paid_semantic_decision_on_connection(
                conn, case["running"], selected=paid.snapshot, effect_key=prepared.binding.effect_key
            )
            assert type(actual.decision.prescribed_action) is ReviewContinuationAction
            installed = install_semantic_action_on_connection(
                conn, case["running"], selected=paid.snapshot, effect_key=prepared.binding.effect_key
            )
            assert (
                semantic_sql.validate_semantic_advisor_source_data_on_connection(
                    conn, session_id=operation_store[4], operation_id=case["running"].claim.operation_id, selected=installed
                )
                == ()
            )
            before = _rows_on_connection(conn)
            savepoint = conn.begin_nested()
            rolled_back = semantic_sql.consume_semantic_advisor_review_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
            assert rolled_back.cursor == installed.cursor + 1
            savepoint.rollback()
            assert _rows_on_connection(conn) == before
            consumed = semantic_sql.consume_semantic_advisor_review_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
            admitted = require_consumed_semantic_review_on_connection(
                conn, case["running"], selected=consumed, effect_key=prepared.binding.effect_key
            )
            root = decode_continuation_snapshot(consumed.payload_json)
            assert len(root.frames) == 2 and type(root.frames[1]) is EndFrame
            assert root.frames[1].phase == "review_needed" and root.frames[1].publication_binding is None
            assert admitted.review_candidate.candidate_text == original_candidate.candidate_text
            assert admitted.review_candidate.response == original_candidate.response
            assert admitted.review_candidate.primary_effect_key == original_candidate.primary_effect_key
            assert admitted.review_candidate.semantic_source == actual.source
            assert root.active_action_source == actual.source and root.active_action_json is not None
            semantic_effects = tuple(conn.execute(select(effects).where(effects.c.effect_key.like("provider:1:%"))).all())
            assert len(semantic_effects) == 1 and semantic_effects[0].disposition == "committed"
            attempts = tuple(conn.execute(select(physical)).all())
            assert len(attempts) == 2 and all(item.settled_at is not None and item.ledger_entry_id is not None for item in attempts)
            after = _rows_on_connection(conn)
            savepoint = conn.begin_nested()
            try:
                changed = conn.execute(
                    update(usage)
                    .where(
                        usage.c.entry_id
                        == _usage_entry_id(
                            session_id=str(operation_store[4]), source="composer", run_id=None, call_id=actual.audit_call.call_id
                        )
                    )
                    .values(prompt_tokens=123456)
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match="ledger"):
                    require_consumed_semantic_review_on_connection(
                        conn, case["running"], selected=consumed, effect_key=prepared.binding.effect_key
                    )
            finally:
                savepoint.rollback()
            assert _rows_on_connection(conn) == after
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_semantic_primary_bridge_reproves_paid_source_and_original_consume_without_allocating_primary(
    operation_store, tmp_path, monkeypatch
):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        admissions = _observe_unresolved_admission(case, monkeypatch)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        assert len(admissions) == 1
        paid_source = await _runtime_source_at(case, retained.snapshot)
        assert paid_source.selected.stack.frames[-1].phase == "response_retained"
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            installed = semantic_sql.install_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
        decision_source = await _runtime_source_at(case, installed)
        assert decision_source.selected.stack.frames[-1].phase == "decision_ready"
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            ordinal_before = tuple(conn.execute(select(ordinals)).all())
            savepoint = conn.begin_nested()
            rolled_back = semantic_sql.consume_semantic_advisor_primary_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
            assert rolled_back.cursor == installed.cursor + 1
            savepoint.rollback()
            assert _rows_on_connection(conn) == before
            consumed = semantic_sql.consume_semantic_advisor_primary_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
            assert tuple(conn.execute(select(ordinals)).all()) == ordinal_before
            root = decode_continuation_snapshot(consumed.payload_json)
            original = decode_continuation_snapshot(installed.payload_json)
            assert len(root.frames) == 1 and root.frames == (original.frames[0],)
            assert root.active_action_json == original.active_action_json
            assert root.active_action_source == original.active_action_source
            assert root.semantic_frontier == original.semantic_frontier
            action_json = semantic_sql.require_current_prescribed_primary_action_on_connection(conn, case["running"], root)
            assert action_json == root.active_action_json
            next_ordinal = conn.execute(
                select(ordinals.c.next_ordinal).where(
                    ordinals.c.session_id == str(operation_store[4]),
                    ordinals.c.operation_id == case["running"].claim.operation_id,
                    ordinals.c.family == 0,
                )
            ).scalar_one_or_none()
            primary_key = f"provider:0:{0 if next_ordinal is None else next_ordinal}"
            assert (
                semantic_sql.read_original_prescribed_primary_source_on_connection(
                    conn,
                    session_id=operation_store[4],
                    operation_id=case["running"].claim.operation_id,
                    action_json=action_json,
                    effect_key=primary_key,
                )
                == original.active_action_source
            )
            after = _rows_on_connection(conn)
            with pytest.raises(ContinuationCASConflict):
                semantic_sql.consume_semantic_advisor_primary_on_connection(
                    conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
                )
            assert _rows_on_connection(conn) == after
            for wrong_operation, wrong_action, wrong_effect in (
                (case["running"].claim.operation_id + "-foreign", action_json, primary_key),
                (case["running"].claim.operation_id, action_json + " ", primary_key),
                (case["running"].claim.operation_id, action_json, "provider:0:999999"),
            ):
                with pytest.raises(AuditIntegrityError):
                    semantic_sql.read_original_prescribed_primary_source_on_connection(
                        conn,
                        session_id=operation_store[4],
                        operation_id=wrong_operation,
                        action_json=wrong_action,
                        effect_key=wrong_effect,
                    )
            savepoint = conn.begin_nested()
            try:
                changed = conn.execute(
                    update(usage)
                    .where(
                        usage.c.entry_id
                        == _usage_entry_id(
                            session_id=str(operation_store[4]), source="composer", run_id=None, call_id=retained.audit_call.call_id
                        )
                    )
                    .values(prompt_tokens=123456)
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match="complete ledger"):
                    semantic_sql.read_original_prescribed_primary_source_on_connection(
                        conn,
                        session_id=operation_store[4],
                        operation_id=case["running"].claim.operation_id,
                        action_json=action_json,
                        effect_key=primary_key,
                    )
            finally:
                savepoint.rollback()
            assert _rows_on_connection(conn) == after
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_original_semantic_guard_refusal_precedes_physical_admission(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_phase import run_continuation_semantic_advisor

    async with _semantic_case(operation_store, tmp_path, monkeypatch, blocked=True) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer = _issuer(case)
        primary_custody = case["provider"].mint(ProviderInvocationFamily.PRIMARY)
        with pytest.raises(AuditIntegrityError, match="ADVISOR"):
            primary_custody.bind_semantic_no_sdk_issuer(issuer)
        assert primary_custody.continuation_semantic_clock is None
        with operation_store[0].connect() as conn:
            before_physical = tuple(conn.execute(select(physical)))
            before_usage = tuple(conn.execute(select(usage)))
        phases = _semantic_phases(case)
        with pytest.raises(PromptGuardRefused) as rejected:
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert sdk == [] and _checks(operation_store[0])[-1]["audit_id"] == rejected.value.audit_id
        assert case["recorder"].llm_calls == ()
        with operation_store[0].connect() as conn:
            assert tuple(conn.execute(select(physical))) == before_physical
            assert tuple(conn.execute(select(usage))) == before_usage
            current = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(operation_store[4]),
                    effects.c.operation_id == case["running"].claim.operation_id,
                    effects.c.effect_key.like("provider:1:%"),
                )
            ).all()
            assert len(current) == 1 and current[0].provider_attempt_id is None
            assert current[0].disposition == "prepared" and current[0].result_chunk_key is None


@pytest.mark.asyncio
async def test_entered_semantic_attempt_cannot_be_closed_as_no_sdk_after_actual_cleanup(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkRefusalReason
    from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalResult

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []
        original = TimeoutError("Original entered semantic SDK timeout")

        async def completion(**kwargs):
            sdk.append(kwargs)
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        actual_observe = phase.observe_semantic_advisor_invocation
        observed_calls = []

        async def observe_original(request, **kwargs):
            observed = await actual_observe(request, **kwargs)
            observed_calls.append((kwargs["no_sdk_issuer"], observed))
            return observed

        monkeypatch.setattr(phase, "observe_semantic_advisor_invocation", observe_original)
        result = await phase.run_continuation_semantic_advisor(_semantic_phases(case), case["runtime"].source.selected)
        assert type(result) is SdkTerminalResult and len(observed_calls) == 1
        issuer, observed = observed_calls[0]
        assert type(observed) is SemanticAdvisorInvocationRaised
        assert observed.observation.original_failure is original
        assert observed.observation.sdk_entered is True and observed.observation.attempt is not None
        assert observed.observation.audit_call.status is ComposerLLMCallStatus.TIMEOUT
        assert len(sdk) == 1
        with pytest.raises(AuditIntegrityError):
            issuer.reserve_refusal(reason=SemanticAdvisorNoSdkRefusalReason.PRE_SDK_DEADLINE, original_failure=original)
        assert result.verified.provider_attempt_id == observed.observation.attempt.attempt_id
        assert result.verified.hold_state == "consumed"
        with operation_store[0].connect() as conn:
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == result.verified.provider_attempt_id)).one()
            assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
        assert len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invented", ["read", "review", "iteration_return"])
async def test_semantic_prepare_refuses_coherent_unowned_result_document_before_any_effect(
    operation_store, tmp_path, monkeypatch, invented
):
    from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
    from elspeth.web.composer.continuation_semantic_advisor import (
        SemanticAdvisorIterationReturnInput,
        SemanticAdvisorReadInput,
        SemanticAdvisorRetainedInputs,
        SemanticAdvisorReviewInput,
    )
    from elspeth.web.composer.protocol import ComposerResult
    from elspeth.web.sessions.continuation_semantic_review_result import (
        RetainedSemanticReviewResult,
        encode_semantic_review_provider_input,
    )

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        runtime = case["runtime"]
        selected = runtime.source.selected
        value = canonical_json({"tool_name": "get_pipeline_state", "response_json": '{"success":true,"data":{}}'})
        read = SemanticAdvisorReadInput(
            ContinuationEvidenceRef(
                receipt_id="read:never_executed",
                source_kind="read",
                content_hash=stable_hash(value),
                original_attempt_id=None,
                coverage_binding=stable_hash("invented scope"),
            ),
            value,
            stable_hash(value),
            "provider:1:999",
            "read:never_executed",
            "not-a-committed-source",
            1,
        )
        result_json, provider_json = encode_semantic_review_provider_input(
            RetainedSemanticReviewResult(
                ComposerResult(message="An output the shared END owner never returned.", state=runtime.live.state),
                "a" * 64,
            )
        )
        review = SemanticAdvisorReviewInput(
            result_json,
            "end:never_executed",
            "not-a-reviewed-source",
            1,
            "clean",
            provider_json,
        )
        iteration_return = SemanticAdvisorIterationReturnInput(
            canonical_json({"message": "An output the actual classifier never returned."}),
            "advance:never_executed",
            "not-a-returned-source",
            1,
            selected.stack.frames[0].audit_buffer,
        )
        retained = SemanticAdvisorRetainedInputs(
            selected.stack.logical.session_id,
            selected.stack.logical.operation_id,
            selected.checkpoint_id,
            selected.stack.cursor,
            (read,) if invented == "read" else (),
            review if invented == "review" else None,
            iteration_return if invented == "iteration_return" else None,
        )
        request = prepare_semantic_advisor_request(
            owner=runtime.materialization.service._advisor_checkpoint,
            provider_owner=case["provider"],
            source=case["invocation"].invocation,
            frontier=case["invocation"].frontier,
            state=runtime.live.state,
            content_scope=ComposerContentScope(UUID(runtime.bindings.session_id), composition_content_hash(runtime.live.state)),
            user_message=runtime.bindings.message,
            runtime=runtime,
            env_ref_names=runtime.env_ref_names,
            retained_inputs=retained,
        )
        changed = {**case, "request": request}
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(AuditIntegrityError, match="complete original source/result DATA"),
        ):
            _prepare(conn, changed, _issuer(changed))
        assert _rows(operation_store[0]) == before


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


def _rewrite_actual_semantic_request(conn, sid, operation, effect, wire):
    """Deliberate coherent fixture repair, with actual chunk constraints enabled."""
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    descriptor = semantic_sql._decode(
        semantic_sql._Input,
        semantic_sql._blob(conn, sid, operation, f"semantic-provider-input:{effect.input_sha256}", "composer.semantic-provider-input.v1"),
    )
    physical_body = json.loads(descriptor.physical_request_json)
    physical_body["messages"] = wire
    physical_json = canonical_json(physical_body)
    digest = sha256(physical_json.encode("utf-8")).hexdigest()
    changed = descriptor.model_copy(update={"physical_request_json": physical_json})
    payload = semantic_sql._canonical(changed).encode("utf-8")
    conn.execute(
        insert(chunks).values(
            session_id=sid,
            operation_id=operation,
            chunk_key=f"semantic-provider-input:{digest}",
            codec="composer.semantic-provider-input.v1",
            payload=payload,
            sha256=sha256(payload).hexdigest(),
            byte_length=len(payload),
        )
    )
    conn.execute(
        update(effects)
        .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == effect.effect_key)
        .values(input_sha256=digest)
    )
    return conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == effect.effect_key)
    ).one()


@pytest.mark.asyncio
async def test_semantic_historical_request_compares_actual_unsafe_sql_and_keeps_physical_facts(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        sid, operation = str(operation_store[4]), case["running"].claim.operation_id
        captures = []

        def observe_capture(*, messages, tools):
            context = capture_provider_context(messages=messages, tools=tools)
            captures.append(context)
            return context

        monkeypatch.setattr(semantic_sql, "capture_provider_context", observe_capture)
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], sid) as conn:
            assert (
                semantic_sql.read_semantic_advisor_outcome_on_connection(
                    conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
                )
                == retained
            )
            assert captures == []
            original_effect = conn.execute(select(effects).where(effects.c.effect_key == prepared.binding.effect_key)).one()
            original_input = semantic_sql._descriptor_data(conn, sid, operation, original_effect).physical_input
            original_call = retained.audit_call
            semantic_sql._physical_source_data(conn, sid, operation, original_effect, original_call, original_input)
            physical_body = json.loads(semantic_sql._canonical(original_input))
            parity_wire = json.loads(canonical_json(list(original_input.messages)))
            parity_wire[0]["content"] += '\nCafé 😀 \\ "quoted"\n},"tools":null,{'
            parity_wire[0]["cache_control"] = {"\ue000": [None, True, -0.0, 1.0, 1.5, 1e-7, 1e30], "😀": "text"}
            parity_context = capture_provider_context(messages=parity_wire, tools=None)
            assert parity_context.disposition == "retained"
            parity_body = {**physical_body, "messages": parity_wire}
            parity_input = semantic_sql._decode(semantic_sql._PhysicalInput, canonical_json(parity_body))
            semantic_sql._require_historical_request_comparison(
                replace(original_call, messages_hash=stable_hash(parity_wire), private_request=parity_context), parity_input
            )
            encoded_input = semantic_sql._canonical(original_input)
            for malformed in ("{", " " + encoded_input, encoded_input[:-1] + ',"unexpected":null}', encoded_input + "{}"):
                with pytest.raises(AuditIntegrityError):
                    semantic_sql._decode(semantic_sql._PhysicalInput, malformed)
            envelope = {
                "schema_version": "composer.provider-context.v1",
                "capture_boundary": "prepared_litellm_messages_and_tools",
                "messages": list(original_input.messages),
                "tools": None,
            }
            original_size = len(canonical_json(envelope).encode("utf-8"))
            for offset in (-1, 0, 1):
                wire_at_bound = json.loads(canonical_json(list(original_input.messages)))
                additional_bytes = JSON_MAX_TOTAL_UTF8_BYTES + offset - original_size
                assert additional_bytes > 0
                wire_at_bound[0]["content"] += "é" * (additional_bytes // 2) + "x" * (additional_bytes % 2)
                expected = canonical_json({**envelope, "messages": wire_at_bound})
                assert len(expected.encode("utf-8")) == JSON_MAX_TOTAL_UTF8_BYTES + offset
                bounded_input = semantic_sql._decode(
                    semantic_sql._PhysicalInput, canonical_json({**physical_body, "messages": wire_at_bound})
                )
                bounded_call = replace(original_call, messages_hash=stable_hash(wire_at_bound))
                if offset > 0:
                    with pytest.raises(AuditIntegrityError, match="exact original physical request"):
                        semantic_sql._require_historical_request_comparison(bounded_call, bounded_input)
                else:
                    semantic_sql._require_historical_request_comparison(
                        replace(bounded_call, private_request=PrivateProviderContext(disposition="retained", canonical=expected)),
                        bounded_input,
                    )
            for changed_call in (
                replace(original_call, model_requested="different/model"),
                replace(original_call, max_completion_tokens_requested=original_call.max_completion_tokens_requested + 1),
                replace(original_call, temperature=0.125),
                replace(original_call, seed=123456),
                replace(original_call, messages_hash="0" * 64),
                replace(original_call, private_request=None),
                replace(
                    original_call,
                    private_request=PrivateProviderContext(disposition="unavailable", canonical=None, reasons=("bounds",)),
                ),
                replace(
                    original_call,
                    private_request=capture_provider_context(messages=[{"role": "user", "content": "Different"}], tools=None),
                ),
            ):
                with pytest.raises(AuditIntegrityError, match="exact original physical request"):
                    semantic_sql._physical_source_data(conn, sid, operation, original_effect, changed_call, original_input)
            assert len(captures) == 2
            captures.clear()
            wire = json.loads(canonical_json(list(original_input.messages)))
            wire[0]["content"] += "\nHistorical data:image/png;base64,AAAA"
            unavailable = capture_provider_context(messages=wire, tools=None)
            assert unavailable.disposition == "unavailable" and unavailable.reasons == ("unsupported",)
            # A defective earlier writer retained this complete unsafe context.
            # This constructor is comparison DATA, not fresh content admission.
            context = PrivateProviderContext(
                disposition="retained",
                canonical=canonical_json(
                    {
                        "schema_version": "composer.provider-context.v1",
                        "capture_boundary": "prepared_litellm_messages_and_tools",
                        "messages": wire,
                        "tools": None,
                    }
                ),
            )
            changed_call = replace(original_call, messages_hash=stable_hash(wire), private_request=context)
            changed = conn.begin_nested()
            changed_effect = _rewrite_actual_semantic_request(conn, sid, operation, original_effect, wire)
            # The real descriptor reader still reconstructs the entire original
            # source/evidence/invocation and third-message source document.
            physical_input = semantic_sql._descriptor_data(conn, sid, operation, changed_effect).physical_input
            manifest = semantic_sql._decode(
                semantic_sql._Outcome,
                semantic_sql._blob(conn, sid, operation, original_effect.result_chunk_key, "composer.semantic-provider-outcome.v1"),
            )
            decoded = decode_continuation_response(retained.response)
            response = encode_continuation_text_response(
                content=decoded.message.content, provider_metadata=decoded.provider_metadata, audit_call=changed_call
            )
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(response),
                payload=response.canonical.encode("utf-8"),
                codec=response.reference.codec,
            )
            changed_manifest = manifest.model_copy(
                update={
                    "input_sha256": changed_effect.input_sha256,
                    "audit_call_json": encode_physical_audit_call(changed_call),
                    "response": response.reference,
                }
            )
            manifest_text = semantic_sql._canonical(changed_manifest)
            manifest_key = f"semantic-provider-outcome:{sha256(manifest_text.encode('utf-8')).hexdigest()}"
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=manifest_key,
                payload=manifest_text.encode("utf-8"),
                codec="composer.semantic-provider-outcome.v1",
            )
            conn.execute(update(effects).where(effects.c.effect_key == original_effect.effect_key).values(result_chunk_key=manifest_key))
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == original_call.call_id)).one()
            source = ProviderAuditSource(attempt.session_id, attempt.operation_id, attempt.operation_epoch)
            old_items = tuple(
                item
                for draft in provider_attempt_audit_drafts_for_source(original_call, source)
                if draft.tool_calls is not None
                for item in draft.tool_calls
            )
            new_items = tuple(
                item
                for draft in provider_attempt_audit_drafts_for_source(changed_call, source)
                if draft.tool_calls is not None
                for item in draft.tool_calls
            )
            replacements = {canonical_json(old): new for old, new in zip(old_items, new_items, strict=True)}
            seen = []
            rows = conn.execute(
                select(messages).where(
                    messages.c.session_id == sid, messages.c.role == "audit", messages.c.writer_principal == "compose_loop"
                )
            ).all()
            for row in rows:
                if row.tool_calls is None:
                    continue
                updated = []
                for item in row.tool_calls:
                    key = canonical_json(item)
                    if key in replacements:
                        seen.append(key)
                        updated.append(replacements[key])
                    else:
                        updated.append(item)
                conn.execute(update(messages).where(messages.c.id == row.id).values(tool_calls=json.loads(canonical_json(updated))))
            assert sorted(seen) == sorted(replacements)
            actual_effect = conn.execute(select(effects).where(effects.c.effect_key == original_effect.effect_key)).one()
            actual_manifest = semantic_sql._decode(
                semantic_sql._Outcome,
                semantic_sql._blob(conn, sid, operation, actual_effect.result_chunk_key, "composer.semantic-provider-outcome.v1"),
            )
            actual_call = decode_physical_audit_call(actual_manifest.audit_call_json)
            assert actual_call == changed_call
            # Target the actual descriptor + physical SQL comparison seam.
            # Recorder/evidence/selected-root reconstruction remains separate;
            # this fixture does not claim a rebuilt whole paid-result history.
            semantic_sql._physical_source_data(conn, sid, operation, actual_effect, actual_call, physical_input)
            assert captures == []
            ledger_change = conn.begin_nested()
            conn.execute(update(usage).where(usage.c.entry_id == attempt.ledger_entry_id).values(prompt_tokens=999))
            with pytest.raises(AuditIntegrityError, match="original complete ledger"):
                semantic_sql._physical_source_data(conn, sid, operation, actual_effect, actual_call, physical_input)
            ledger_change.rollback()
            semantic_sql._physical_source_data(conn, sid, operation, actual_effect, actual_call, physical_input)
            # The non-retained branch still invokes the original capture and
            # accepts exactly its unavailable reason, as before this change.
            semantic_sql._require_historical_request_comparison(replace(actual_call, private_request=unavailable), physical_input)
            assert captures == [unavailable]
            changed.rollback()
            semantic_sql._physical_source_data(conn, sid, operation, original_effect, original_call, original_input)
        assert _rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_semantic_complete_preparation_keeps_full_actual_request_admission(operation_store, tmp_path, monkeypatch):
    import elspeth.web.sessions.continuation_semantic_advisor as semantic_sql
    import elspeth.web.sessions.continuation_semantic_v4_complete as complete_sql

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []
        checked = []
        actual_prepare = complete_sql.prepare_v4_semantic_complete_on_connection

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        def prepare(conn, *, prepared, admission, observation, recorder):
            positive = actual_prepare(conn, prepared=prepared, admission=admission, observation=observation, recorder=recorder)
            before = _rows_on_connection(conn)
            sid, operation = str(case["running"].claim.session_id), case["running"].claim.operation_id
            effect = conn.execute(select(effects).where(effects.c.effect_key == prepared.binding.effect_key)).one()
            original = semantic_sql._descriptor_data(conn, sid, operation, effect).physical_input
            wire = json.loads(canonical_json(list(original.messages)))
            wire[0]["content"] += "\nHistorical data:image/png;base64,AAAA"
            assert capture_provider_context(messages=wire, tools=None).disposition == "unavailable"
            context = PrivateProviderContext(
                disposition="retained",
                canonical=canonical_json(
                    {
                        "schema_version": "composer.provider-context.v1",
                        "capture_boundary": "prepared_litellm_messages_and_tools",
                        "messages": wire,
                        "tools": None,
                    }
                ),
            )
            call = replace(observation.audit_call, messages_hash=stable_hash(wire), private_request=context)
            changed = conn.begin_nested()
            changed_effect = _rewrite_actual_semantic_request(conn, sid, operation, effect, wire)
            root = decode_continuation_snapshot(admission.selected.payload_json)
            changed_root = root.model_copy(
                update={
                    "frames": (
                        *root.frames[:-1],
                        root.frames[-1].model_copy(update={"provider_input_sha256": changed_effect.input_sha256}),
                    )
                }
            )
            snapshot = encode_continuation_snapshot(changed_root).encode("utf-8")
            snapshot_key = conn.execute(
                select(checkpoints.c.snapshot_chunk_key).where(
                    checkpoints.c.session_id == sid,
                    checkpoints.c.operation_id == operation,
                    checkpoints.c.checkpoint_id == admission.selected.checkpoint_id,
                )
            ).scalar_one()
            conn.execute(
                update(chunks)
                .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == snapshot_key)
                .values(payload=snapshot, sha256=sha256(snapshot).hexdigest(), byte_length=len(snapshot))
            )
            selected = load_snapshot(conn, session_id=sid, operation_id=operation)
            assert selected.payload_json == snapshot.decode("utf-8")
            actual_input = semantic_sql._original_input_data(conn, sid, operation, changed_root, changed_effect).physical_input
            semantic_sql._require_historical_request_comparison(call, actual_input)
            changed_rows = _rows_on_connection(conn)
            with pytest.raises(AuditIntegrityError, match="exact original physical request"):
                actual_prepare(
                    conn,
                    prepared=prepared,
                    admission=replace(admission, selected=selected),
                    observation=replace(observation, audit_call=call),
                    recorder=recorder,
                )
            assert _rows_on_connection(conn) == changed_rows
            changed.rollback()
            assert _rows_on_connection(conn) == before
            checked.append(True)
            return positive

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(complete_sql, "prepare_v4_semantic_complete_on_connection", prepare)
        _prepared, retained = await _run_paid_semantic(case, monkeypatch)
        assert retained.audit_call.private_request.disposition == "retained"
        assert checked == [True] and len(sdk) == 1


@pytest.mark.asyncio
async def test_semantic_fresh_sdk_path_refuses_unsafe_complete_request(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_phase import run_continuation_semantic_advisor

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        owner = case["request"].owner
        monkeypatch.setattr(owner, "_composer_skill_text", owner._composer_skill_text + "\nUnsafe data:image/png;base64,AAAA")
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            raise AssertionError("unsafe semantic request must not reach the SDK")

        monkeypatch.setattr(litellm, "acompletion", completion)
        with pytest.raises(AuditIntegrityError, match="lost its complete physical request capture"):
            await run_continuation_semantic_advisor(_semantic_phases(case), case["runtime"].source.selected)
        assert sdk == []
        with operation_store[0].connect() as conn:
            assert conn.execute(select(physical)).all() == []
            assert conn.execute(select(usage)).all() == []


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
        from elspeth.web.sessions.service import SessionServiceImpl

        original_complete = SessionServiceImpl.complete_v4_semantic_result_sync
        completion_cuts = []

        def reject_substituted_observations(self, *, prepared, admission, observation, recorder, no_sdk_issuer):
            completion_cuts.append(observation)
            alternative = _answer(case["request"])
            alternative["reason"] = "Substituted words from a different paid decision."
            alternative["prescribed_action"]["instruction"] = "A different instruction this paid call did not produce."
            text = json.dumps(alternative)
            decision = parse_continuation_advisor_decision(text, actual_advisor_call_id=observation.audit_call.call_id)
            assert decision is not None and decision != observation.decision
            assert semantic_advisor_action_key(
                case["invocation"],
                decision,
                actual_advisor_call_id=observation.audit_call.call_id,
                permitted_read_tools=frozenset(),
                complete_required_coverage=False,
            ) != semantic_advisor_action_key(
                case["invocation"],
                observation.decision,
                actual_advisor_call_id=observation.audit_call.call_id,
                permitted_read_tools=frozenset(),
                complete_required_coverage=False,
            )
            changed = replace(
                observation,
                message=replace(observation.message, content=text),
                decision=decision,
                response=encode_continuation_text_response(
                    content=text, provider_metadata=observation.provider_metadata, audit_call=observation.audit_call
                ),
            )
            before = _rows(operation_store[0])
            for counterfeit in (changed, replace(observation, audit_call=replace(observation.audit_call, prompt_tokens=999))):
                with pytest.raises(AuditIntegrityError):
                    original_complete(
                        self,
                        prepared=prepared,
                        admission=admission,
                        observation=counterfeit,
                        recorder=recorder,
                        no_sdk_issuer=no_sdk_issuer,
                    )
                assert _rows(operation_store[0]) == before
            return original_complete(
                self,
                prepared=prepared,
                admission=admission,
                observation=observation,
                recorder=recorder,
                no_sdk_issuer=no_sdk_issuer,
            )

        monkeypatch.setattr(SessionServiceImpl, "complete_v4_semantic_result_sync", reject_substituted_observations)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        assert len(completion_cuts) == len(sdk) == 1
        assert completion_cuts[0].attempt.attempt_id == retained.audit_call.call_id == retained.decision.decision_id
        assert retained.audit_call.provider_cost == 0.01
        context = retained.audit_call.private_request
        assert context.disposition == "retained"
        original_wire = json.loads(context.canonical)
        assert original_wire["messages"] == case["request"].prepared.evidence.messages
        assert original_wire["tools"] is None
        budget = case["provider"].cost_budget.snapshot()

        async def forbidden_sdk(**kwargs):
            raise AssertionError("cold semantic admission cannot replay an original paid call")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            installed = semantic_sql.install_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
        root = decode_continuation_snapshot(installed.payload_json)
        assert root.active_action_json == canonical_json(retained.decision.prescribed_action.model_dump(mode="json"))
        assert root.active_action_source.effect_key == prepared.binding.effect_key
        assert root.active_action_source.input_sha256 == prepared.binding.input_sha256
        assert root.active_action_source.response == retained.response.reference
        assert sum(type(frame) is ComposeFrame for frame in root.frames) == 1
        with operation_store[0].connect() as conn:
            cold = semantic_sql.read_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=installed, effect_key=prepared.binding.effect_key
            )
        assert cold.decision == retained.decision and cold.source == root.active_action_source
        assert cold.action == retained.decision.prescribed_action
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            savepoint = conn.begin_nested()
            selected = installed
            try:
                if corruption == "ledger":
                    entry_id = _usage_entry_id(
                        session_id=str(operation_store[4]), source="composer", run_id=None, call_id=retained.audit_call.call_id
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
                        .where(physical.c.attempt_id == retained.audit_call.call_id)
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
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationV4Finalized
    from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
    from elspeth.web.sessions import continuation_semantic_v4_complete as complete_sql
    from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
    from elspeth.web.sessions.models import composer_segment_attempts_table as segment_attempts
    from elspeth.web.sessions.service import SessionServiceImpl

    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        original_text = "The actual paid response contains no valid semantic decision."
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(original_text)

        monkeypatch.setattr(litellm, "acompletion", completion)
        actual_observe = phase.observe_semantic_advisor_invocation
        observed_calls = []
        actual_complete = SessionServiceImpl.complete_v4_semantic_result_sync
        completion_cuts = []
        completion_admissions = []
        rollback_cuts = []

        class RollbackAfterTerminalWrite(RuntimeError):
            pass

        def settlement_rows(conn):
            return (
                _rows_on_connection(conn),
                tuple(conn.execute(select(segments).order_by(*segments.primary_key.columns))),
                tuple(conn.execute(select(segment_attempts).order_by(*segment_attempts.primary_key.columns))),
            )

        def complete_original(self, *, prepared, admission, observation, recorder, no_sdk_issuer):
            completion_cuts.append(observation)
            completion_admissions.append(admission)
            changed_text = "Different paid-invalid words."
            counterfeit = replace(
                observation,
                message=replace(observation.message, content=changed_text),
                response=encode_continuation_text_response(
                    content=changed_text, provider_metadata=observation.provider_metadata, audit_call=observation.audit_call
                ),
            )
            assert counterfeit.original_failure is observation.original_failure
            assert counterfeit.audit_call is observation.audit_call and counterfeit.attempt is observation.attempt
            before = _rows(operation_store[0])
            with pytest.raises(AuditIntegrityError):
                actual_complete(
                    self,
                    prepared=prepared,
                    admission=admission,
                    observation=counterfeit,
                    recorder=recorder,
                    no_sdk_issuer=no_sdk_issuer,
                )
            assert _rows(operation_store[0]) == before

            actual_settle = complete_sql.settle_composer_operation_on_connection
            probe = RollbackAfterTerminalWrite("rollback after the actual paid-invalid terminal write")
            with operation_store[0].connect() as conn:
                before_settlement = settlement_rows(conn)

            def fail_after_terminal_write(conn, running, **kwargs):
                terminal = actual_settle(conn, running, **kwargs)
                rollback_cuts.append(terminal)
                assert terminal.status == "failed"
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
                hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == admission.attempt.attempt_id)).one()
                assert attempt.settled_at is not None and attempt.ledger_entry_id is not None and hold.state == "consumed"
                assert conn.execute(select(usage.c.entry_id).where(usage.c.entry_id == attempt.ledger_entry_id)).scalar_one()
                parent = conn.execute(
                    select(jobs).where(
                        jobs.c.session_id == str(operation_store[4]),
                        jobs.c.operation_id == running.claim.operation_id,
                    )
                ).one()
                assert parent.status == "failed" and parent.checkpoint_id == terminal.checkpoint_id
                assert parent.journal_cursor == terminal.journal_cursor
                assert settlement_rows(conn) != before_settlement
                raise probe

            with monkeypatch.context() as rollback_patch:
                rollback_patch.setattr(complete_sql, "settle_composer_operation_on_connection", fail_after_terminal_write)
                with pytest.raises(RollbackAfterTerminalWrite) as rolled_back:
                    actual_complete(
                        self,
                        prepared=prepared,
                        admission=admission,
                        observation=observation,
                        recorder=recorder,
                        no_sdk_issuer=no_sdk_issuer,
                    )
                assert rolled_back.value is probe and len(rollback_cuts) == 1
            with operation_store[0].connect() as conn:
                assert settlement_rows(conn) == before_settlement
            assert len(sdk) == 1
            return actual_complete(
                self,
                prepared=prepared,
                admission=admission,
                observation=observation,
                recorder=recorder,
                no_sdk_issuer=no_sdk_issuer,
            )

        async def observe_original(request, **kwargs):
            observed = await actual_observe(request, **kwargs)
            observed_calls.append((kwargs["no_sdk_issuer"], kwargs["v4_prepared"], observed))
            return observed

        monkeypatch.setattr(phase, "observe_semantic_advisor_invocation", observe_original)
        monkeypatch.setattr(SessionServiceImpl, "complete_v4_semantic_result_sync", complete_original)
        terminal = await phase.run_continuation_semantic_advisor(_semantic_phases(case), case["runtime"].source.selected)
        assert type(terminal) is phase.SemanticAdvisorPaidInvalidTerminal
        assert len(observed_calls) == len(completion_cuts) == len(rollback_cuts) == 1
        _issuer, prepared, observed = observed_calls[0]
        assert type(observed) is SemanticAdvisorInvocationV4Finalized
        paid = observed.finalization.result
        assert type(paid) is semantic_sql.SemanticAdvisorProviderResult and terminal.paid is paid
        assert paid.outcome is semantic_sql.SemanticAdvisorProviderOutcome.PAID_INVALID and paid.decision is None
        assert type(terminal.original_error) is _MalformedLLMResponseError
        assert terminal.original_error is paid.original_error is observed.observation.original_failure
        assert terminal.original_error is completion_cuts[0].original_failure
        assert not terminal.deferred_cancellations
        assert paid.response is not None and decode_continuation_response(paid.response).message.content == original_text
        assert observed.observation.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        assert observed.observation.decision is None and observed.observation.message.content == original_text
        context = observed.observation.audit_call.private_request
        assert context.disposition == "retained"
        original_wire = json.loads(context.canonical)
        assert original_wire["messages"] == case["request"].prepared.evidence.messages
        assert original_wire["tools"] is None
        (admission,) = completion_admissions
        with operation_store[0].connect() as conn:
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == admission.attempt.attempt_id)).one()
            assert attempt.settled_at is not None and attempt.ledger_entry_id is not None and hold.state == "consumed"
            entry = conn.execute(
                select(usage).where(
                    usage.c.entry_id
                    == _usage_entry_id(
                        session_id=str(operation_store[4]),
                        source="composer",
                        run_id=None,
                        call_id=admission.attempt.attempt_id,
                    )
                )
            ).one()
            assert entry.entry_id == attempt.ledger_entry_id
            assert entry.prompt_tokens == paid.audit_call.prompt_tokens and entry.completion_tokens == paid.audit_call.completion_tokens
            cold, _data, _root = semantic_sql._paid_result_data(
                conn,
                str(operation_store[4]),
                case["running"].claim.operation_id,
                paid.snapshot,
                decode_continuation_snapshot(paid.snapshot.payload_json),
                paid.effect_key,
            )
            assert cold == paid and cold.outcome is semantic_sql.SemanticAdvisorProviderOutcome.PAID_INVALID
            assert cold.response is not None and decode_continuation_response(cold.response).message.content == original_text
            parent = conn.execute(
                select(jobs).where(
                    jobs.c.session_id == str(operation_store[4]),
                    jobs.c.operation_id == case["running"].claim.operation_id,
                )
            ).one()
            assert parent.status == "failed" and parent.journal_cursor == paid.snapshot.cursor + 1
        before = _rows(operation_store[0])
        with (
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
            pytest.raises(ComposerOperationFenceLost) as lost,
        ):
            semantic_sql.install_semantic_advisor_decision_on_connection(
                conn, case["running"], selected=paid.snapshot, effect_key=prepared.binding.effect_key
            )
        assert type(lost.value) is ComposerOperationFenceLost
        assert lost.value.session_id == operation_store[4]
        assert lost.value.operation_id == case["running"].claim.operation_id
        assert _rows(operation_store[0]) == before and len(sdk) == 1
