"""Actual checkpoint capture DATA, independent owners and fresh source checks."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_checkpoint_early_physical as early_module
from elspeth.web.composer import continuation_checkpoint_end_detached as detached_module
from elspeth.web.composer import continuation_checkpoint_end_physical as physical_module
from elspeth.web.composer import continuation_runtime as runtime_module
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_end import enter_continuation_end, execute_continuation_end_review
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_return_shapes import _CLEAN, _admit_original_retry, _complete_original
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


def test_retained_shelf_observation_preserves_alias_recipes_and_pending_adoption():
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner, observe_retained_message_shelf
    from elspeth.web.composer.continuation_prompt_recipes import InitialMessageRecipe

    received = {
        "role": "assistant",
        "content": "Inspecting",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
    }
    current = json.loads(json.dumps(received))
    first = {"role": "user", "content": "same text"}
    second = dict(first)
    messages = [current, first, second]
    owner = AdvisorTranscriptOwner(request="inspect")
    owner.record_provider_reply(provider_call_id="physical-1", message=received)
    before = owner.snapshot_for_continuation(messages)
    recipes = tuple(InitialMessageRecipe(index=index) for index in range(len(before.messages_json)))
    retained = before.model_copy(update={"producer_recipes": recipes})
    projected = observe_retained_message_shelf(retained)
    owner.observe_messages(messages)
    expected = owner.snapshot_for_continuation(messages)
    assert projected.model_copy(update={"producer_recipes": ()}) == expected
    assert projected.producer_recipes == recipes[:3]
    assert projected.current_indexes == (0, 1, 2)
    assert projected.messages_json[1] == projected.messages_json[2]
    assert projected.observed[0].message_index == 0 and not projected.observed[0].pending_adoption
    assert before.observed[0].pending_adoption
    assert projected.history is retained.history
    ambiguous = retained.model_copy(update={"observed": (*retained.observed, retained.observed[0])})
    with pytest.raises(AuditIntegrityError, match="ambiguous tool-call identities"):
        observe_retained_message_shelf(ambiguous)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_pending,opponent", ((False, None), (True, None), (False, "caches"), (False, "transcript")))
async def test_actual_early_cold_projection_preserves_history_and_exact_observation(
    operation_store, tmp_path, monkeypatch, retry_pending, opponent
):
    """Actual completion agrees exactly; a changed observation still refuses."""
    from elspeth.web.composer.continuation_caches import (
        capture_empty_continuation_cache_observation,
        decode_continuation_caches,
        encode_continuation_caches,
    )
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_message_shelf import decode_message_shelf, encode_message_shelf
    from elspeth.web.sessions import continuation_checkpoint_early_return as writer

    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, _running, selected, primary_sdk):

        async def complete(**_arguments):
            return _reply(None if retry_pending else _CLEAN)

        monkeypatch.setattr(litellm, "acompletion", complete)
        admitted = []
        observed = []
        original_admit = writer.require_original_early_checkpoint_projection
        original_expected = writer._expected_child_frames

        def admit(*args, **kwargs):
            captured = original_admit(*args, **kwargs)
            admitted.append(captured)
            return captured

        def expected(conn, owed, **kwargs):
            result = original_expected(conn, owed, **kwargs)
            frame, batch, chunks = result
            captured = admitted[-1]
            assert captured.stack.frames == (frame, batch)
            assert all(chunk in captured.chunks for chunk in chunks)
            prior_frame = decode_continuation_snapshot(owed.selected.payload_json).frames[0]
            names = owed.source.env_ref_names
            prior_caches = decode_continuation_caches(writer._held_chunk(conn, owed, prior_frame.caches), env_ref_names=names)
            cache_chunk = next(chunk for chunk in chunks if chunk.reference == frame.caches)
            caches = decode_continuation_caches(cache_chunk, env_ref_names=names)
            assert caches.capture_count == prior_caches.capture_count + 1
            assert caches.capture_creators[:-1] == prior_caches.capture_creators
            assert caches.capture_creators[-1].operation_id == owed.new_hold.session_operation_id
            assert caches.capture_creators[-1].operation_epoch == owed.new_hold.session_operation_epoch
            assert caches.discovery == prior_caches.discovery and caches.preflight == prior_caches.preflight
            prior_shelf = decode_message_shelf(writer._held_chunk(conn, owed, prior_frame.transcript))
            transcript_chunk = next(chunk for chunk in chunks if chunk.reference == frame.transcript)
            shelf = decode_message_shelf(transcript_chunk)
            # The actual post-P4 fold already observed these messages before
            # EARLY; observing the unchanged shelf preserves that history.
            assert shelf.observed == prior_shelf.observed
            observed.append(result)
            if opponent == "caches":
                changed = encode_continuation_caches(
                    capture_empty_continuation_cache_observation(caches, creator=caches.capture_creators[-1], env_ref_names=names),
                    env_ref_names=names,
                )
                return frame.model_copy(update={"caches": changed.reference}), batch, (*chunks, changed)
            if opponent == "transcript":
                changed = encode_message_shelf(shelf.model_copy(update={"observed": shelf.observed[:-1]}), env_ref_names=names)
                return frame.model_copy(update={"transcript": changed.reference}), batch, (*chunks, changed)
            return result

        monkeypatch.setattr(writer, "require_original_early_checkpoint_projection", admit)
        monkeypatch.setattr(writer, "_expected_child_frames", expected)
        async with _early_attempt(owner, selected) as source:
            if retry_pending:
                with pytest.raises(_MalformedLLMResponseError):
                    await _invoke_early(source)
            else:
                await _invoke_early(source)
            if opponent is None:
                completed, _returned, _projected = await _complete_original(source)
                assert (completed.retry is not None) is retry_pending
            else:
                with pytest.raises(AuditIntegrityError, match="EARLY completion substituted its exact cold-derived projection"):
                    await _complete_original(source)
        assert len(primary_sdk) == 1 and observed


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", (False, True))
async def test_actual_early_capture_pair_preserves_isolation_and_rejects_late_mutation(operation_store, tmp_path, monkeypatch, retry):
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, primary_sdk):
        requests = []

        async def complete(**arguments):
            requests.append(arguments)
            return _reply(None if retry and len(requests) == 1 else _CLEAN)

        monkeypatch.setattr(litellm, "acompletion", complete)
        first_admission = None
        if retry:
            async with _early_attempt(owner, selected) as first:
                with pytest.raises(_MalformedLLMResponseError):
                    await _invoke_early(first)
                first_admission = early_module.checkpoint_early_detached_runtime(first).source.admitted_evidence
                completed, _returned, _projected = await _complete_original(first)
                assert completed.retry is not None
            selected = await _admit_original_retry(owner, running, completed.completed)

        pairs, bodies, final_reads = [], [], []
        original_capture = early_module.capture_continuation_runtime_data
        original_body = runtime_module._capture_owner_data
        original_read = owner.authority.read_continuation_runtime
        original_baseline = early_module.require_checkpoint_early_detached_source
        baseline_stages = []

        async def capture(*args, **kwargs):
            result = await original_capture(*args, **kwargs)
            pairs.append(result)
            return result

        def body(runtime, **kwargs):
            bodies.append(runtime)
            return original_body(runtime, **kwargs)

        def read(running, **kwargs):
            result = original_read(running, **kwargs)
            if pairs and kwargs["selected"].stack.cursor == pairs[0].captured.stack.cursor + 1:
                final_reads.append(result)
            return result

        def baseline(*args, **kwargs):
            baseline_stages.append("entered")
            result = original_baseline(*args, **kwargs)
            baseline_stages.append("accepted")
            return result

        with monkeypatch.context() as observe:
            observe.setattr(early_module, "capture_continuation_runtime_data", capture)
            observe.setattr(runtime_module, "_capture_owner_data", body)
            observe.setattr(owner.authority, "read_continuation_runtime", read)
            observe.setattr(early_module, "require_checkpoint_early_detached_source", baseline)
            async with _early_attempt(owner, selected) as source:
                await _invoke_early(source)
                detached = early_module.checkpoint_early_detached_runtime(source)
                warm = source.phase_owner.runtime
                assert len(pairs) == 1 and bodies == [warm]
                assert source.captured is pairs[0].captured
                assert detached.source.admitted_evidence is pairs[0].admitted_evidence
                assert detached.source.admitted_evidence is not first_admission
                assert len(final_reads) == 1 and final_reads[0].selected is detached.source.selected
                assert final_reads[0].admitted_evidence is not pairs[0].admitted_evidence
                detached_module.require_detached_end_owners(detached, warm)
                early_module.require_checkpoint_early_detached_source(source, detached=detached, original=warm)

                state = detached.live.state
                detached.live.state = replace(state, version=state.version + 1)
                assert warm.live.state.version == state.version
                with pytest.raises(AuditIntegrityError):
                    await _complete_original(source)
                assert baseline_stages[-1] == "entered"
                detached.live.state = state
                early_module.require_checkpoint_early_detached_source(source, detached=detached, original=warm)

                warm_messages = list(warm.live.llm_messages)
                detached.live.llm_messages.append({"role": "user", "content": "late detached-only change"})
                assert warm.live.llm_messages == warm_messages
                with pytest.raises(AuditIntegrityError):
                    await _complete_original(source)
                assert baseline_stages[-1] == "entered"
                detached.live.llm_messages.pop()
                completed, _returned, _projected = await _complete_original(source)
                assert completed.retry is None
        assert len(primary_sdk) == 1 and len(requests) == 1 + int(retry)


class _PreparedEndObserved(Exception):
    """Stop this source-only control before any END provider request."""


@asynccontextmanager
async def _actual_end(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    runtime, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    try:
        calls = []

        async def complete(**_arguments):
            calls.append(1)
            return _reply("The actual PRIMARY candidate requires its original END review.")

        monkeypatch.setattr(litellm, "acompletion", complete)
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
        assert type(primary) is PrimaryProviderResult and calls == [1]
        phases.accept_primary(primary)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            current = load_snapshot(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
        selected = await load_selected_continuation(authority=phases.authority, running=running, selected=current, required_work=work)
        await enter_continuation_end(phases, selected)
        entered = phases.runtime
        assert entered is not None and phases.action_clock is None
        clock = ContinuationActionClock(entered.source.policy.actions.advisor)
        deadline = work.coordinator.sql_deadline_owner
        if deadline is not None:
            deadline.begin_action(clock.policy, started_at=clock.started_monotonic)
            deadline.require_action_clock(clock)
        phases.bind_action_clock(clock)
        yield phases, entered.source.selected, primary
        assert calls == [1]
    finally:
        await lease.close()
        work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_end_reuses_capture_admission_and_constructs_isolated_owners(operation_store, tmp_path, monkeypatch):
    async with _actual_end(operation_store, tmp_path, monkeypatch) as (phases, selected, _primary):
        pairs = []
        final_reads = []
        bodies = []
        original_capture = physical_module.capture_continuation_runtime_data
        original_body = runtime_module._capture_owner_data
        original_read = phases.authority.read_continuation_runtime

        async def capture(*args, **kwargs):
            result = await original_capture(*args, **kwargs)
            pairs.append(result)
            return result

        def body(runtime, **kwargs):
            bodies.append(runtime)
            return original_body(runtime, **kwargs)

        def read(running, **kwargs):
            result = original_read(running, **kwargs)
            if pairs and kwargs["selected"].stack.cursor == pairs[0].captured.stack.cursor + 1:
                final_reads.append(result)
            return result

        async def gate(**arguments):
            owner = arguments["checkpoint_phase_owner"]
            with monkeypatch.context() as observe:
                observe.setattr(physical_module, "capture_continuation_runtime_data", capture)
                observe.setattr(runtime_module, "_capture_owner_data", body)
                observe.setattr(phases.authority, "read_continuation_runtime", read)
                source = await physical_module.prepare_checkpoint_end_physical_source(
                    phase_owner=owner, gate_locals=arguments["checkpoint_end_input"]
                )
            assert len(pairs) == 1 and bodies == [owner.runtime]
            assert source.captured is pairs[0].captured
            assert source.detached.source.admitted_evidence is pairs[0].admitted_evidence
            assert len(final_reads) == 1
            assert final_reads[0].selected is source.detached.source.selected
            assert final_reads[0].admitted_evidence is not pairs[0].admitted_evidence
            detached_module.require_detached_end_owners(source.detached, owner.runtime)
            # Constructor equivalence belongs in this focused control; later
            # consequence owners still check the actual graph before using it.
            physical_module.require_checkpoint_end_detached_source(source)
            state = source.detached.live.state
            source.detached.live.state = replace(state, version=state.version + 1)
            assert owner.runtime.live.state.version == state.version
            with pytest.raises(AuditIntegrityError):
                physical_module.require_checkpoint_end_detached_source(source)
            source.detached.live.state = state
            physical_module.require_checkpoint_end_detached_source(source)
            warm_messages = list(owner.runtime.live.llm_messages)
            source.detached.live.llm_messages.append({"role": "user", "content": "changed only detached DATA"})
            assert owner.runtime.live.llm_messages == warm_messages
            with pytest.raises(AuditIntegrityError):
                physical_module.require_checkpoint_end_detached_source(source)
            raise _PreparedEndObserved

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", gate)
        with pytest.raises(_PreparedEndObserved):
            await execute_continuation_end_review(phases, selected)
        assert len(pairs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ("recorder", "tools"))
async def test_prepared_capture_reads_complete_fresh_dependency_and_non_evidence_closure(operation_store, tmp_path, monkeypatch, leaf):
    async with _actual_end(operation_store, tmp_path, monkeypatch) as (phases, selected, _primary):
        actual_inputs = detached_module._prepared_capture_inputs
        reached = []

        def corrupt_after_positive(**kwargs):
            positive = actual_inputs(**kwargs)
            assert positive.source.admitted_evidence is kwargs["capture_data"].admitted_evidence
            frame = kwargs["selected"].stack.frames[0]
            reference = frame.audit_buffer if leaf == "recorder" else frame.tools
            assert reference is not None
            dependencies = kwargs["capture_data"].admitted_evidence.dependency_chunks
            assert any(chunk.reference == reference for chunk in dependencies) is (leaf == "recorder")
            payload = b"{}"
            running = phases.work.running
            assert running is not None
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                changed = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(operation_store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                    )
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
                assert changed.rowcount == 1
            with pytest.raises(AuditIntegrityError):
                actual_inputs(**kwargs)
            reached.append(leaf)
            raise _PreparedEndObserved

        async def gate(**arguments):
            with monkeypatch.context() as corrupt:
                corrupt.setattr(detached_module, "_prepared_capture_inputs", corrupt_after_positive)
                await physical_module.prepare_checkpoint_end_physical_source(
                    phase_owner=arguments["checkpoint_phase_owner"], gate_locals=arguments["checkpoint_end_input"]
                )
            raise AssertionError("corrupted prepared source reached detached construction")

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", gate)
        with pytest.raises(_PreparedEndObserved):
            await execute_continuation_end_review(phases, selected)
        assert reached == [leaf]


@pytest.mark.asyncio
async def test_captured_end_still_rejects_late_original_ledger_corruption_in_final_source_read(operation_store, tmp_path, monkeypatch):
    async with _actual_end(operation_store, tmp_path, monkeypatch) as (phases, selected, primary):
        restore_live = runtime_module._restore_live
        read_runtime = phases.authority.read_continuation_runtime
        reached = []

        def corrupt_before_final(**kwargs):
            assert kwargs["detached_disclosure"] is not None
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == primary.outcome.audit_call.call_id)).one()
                row = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
                changed = conn.execute(update(usage).where(usage.c.entry_id == row.entry_id).values(prompt_tokens=row.prompt_tokens + 1))
                assert changed.rowcount == 1
            final_reads = []

            def observe_final(running, **read_kwargs):
                assert read_kwargs["selected"] is kwargs["selected"]
                final_reads.append(read_kwargs["selected"])
                return read_runtime(running, **read_kwargs)

            with monkeypatch.context() as observe:
                observe.setattr(phases.authority, "read_continuation_runtime", observe_final)
                with pytest.raises(AuditIntegrityError):
                    restore_live(**kwargs)
            assert final_reads == [kwargs["selected"]]
            reached.append("fresh-final-source")
            raise _PreparedEndObserved

        async def gate(**arguments):
            with monkeypatch.context() as corrupt:
                corrupt.setattr(runtime_module, "_restore_live", corrupt_before_final)
                await physical_module.prepare_checkpoint_end_physical_source(
                    phase_owner=arguments["checkpoint_phase_owner"], gate_locals=arguments["checkpoint_end_input"]
                )
            raise AssertionError("corrupted historical ledger reached a physical END source")

        monkeypatch.setattr(phases.service._advisor_checkpoint, "_run_advisor_checkpoint", gate)
        with pytest.raises(_PreparedEndObserved):
            await execute_continuation_end_review(phases, selected)
        assert reached == ["fresh-final-source"]
