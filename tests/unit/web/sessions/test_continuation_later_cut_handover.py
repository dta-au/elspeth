"""G2: actual later cuts hand over and resume with fresh worker-owned runtime.

Use the existing app/lifespan/worker harness. The probes only observe real
phase commits and force one action-fit yield at the selected actual cut.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_clock import SegmentFit
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_handoff import classify_drain_cut
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource, required_failure_leaves
from elspeth.web.sessions import continuation as continuation_store
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver, ContinuationSegmentSettled
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, effects, end_admit, install_sdk, submit_and_settle
from tests.unit.web.composer.test_continuation_cut_coverage import HANDOFF_SAFE_CUTS, _selected


@asynccontextmanager
async def _later_cut_app(tmp_path, monkeypatch, *, expect_unknown):
    if not expect_unknown:
        async with continuation_app(tmp_path, monkeypatch) as harness:
            yield harness
        return
    lifetime = continuation_app(tmp_path, monkeypatch)
    harness = await lifetime.__aenter__()
    try:
        yield harness
    finally:
        # Body assertions remain outside this exact negative lifecycle
        # expectation; another teardown failure still escapes unchanged.
        with pytest.raises(AuditIntegrityError, match="Composer worker still owns unfinished shutdown work"):
            await lifetime.__aexit__(None, None, None)


async def _admit_only(harness, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {harness.token}"},
    ) as client:
        accepted = await client.post(f"/api/sessions/{harness.session_id}/messages", json=body)
        assert accepted.status_code == 202, accepted.text


async def _run_later_cut_handover(tmp_path, monkeypatch, cut, *, mode="action-fit"):
    words = "The provider completed the request after a same-sequence handover."
    # A real END publication refusal requests a prose-only correction, whose
    # committed repair transition produces the next Compose action_ready.
    # This avoids requiring unrelated tool/P4 publication to reach the cut.
    repair_prefix = (
        ("primary", ("This reply still needs a correction.", None)),
        (
            "end",
            {
                **end_admit("The reply has unsupported claims; correct the reply without changing the pipeline."),
                "publication_verdict": "WITHHOLD",
            },
        ),
    )
    sdk = ScriptedSDK([*(() if cut == "end-review" else repair_prefix), ("primary", (words, None)), ("end", end_admit())])

    async def priced_sdk(**kwargs):
        reply = await sdk(**kwargs)
        assert isinstance(reply, ModelResponse) and reply.usage is not None
        # Both synthetic roles report finite SDK cost; they have no public
        # price catalogue. Keep real cost admission and accounting intact.
        reply.usage = reply.usage.model_copy(update={"cost": 0.01})
        return reply

    install_sdk(monkeypatch, priced_sdk)
    operation_id = str(uuid4())
    selected_before: list[SelectedContinuationState] = []
    selected_after: list[SelectedContinuationState] = []
    resumed_cold: list[bool] = []
    old_claims = []
    old_phases = []
    old_runtimes = []
    old_effects = []
    outcomes = []
    owner_jobs = []
    suspend_attempts, committed, verified, readbacks, immutable_verified, released = [], [], [], [], [], []
    read_receipts = []
    original = RuntimeError("controlled actual later-cut suspend failure")
    rolled_back = False
    phase_trace = []
    disposition_finished = asyncio.Event()
    force_handover = False
    forced = False
    actual_advance = driver_module._SegmentDriver.advance
    actual_dispose = driver_module._SegmentDriver.dispose
    actual_settle = driver_module._SegmentDriver.settle
    actual_fit = driver_module.admit_action
    actual_init = driver_module._SegmentDriver.__init__

    def observe_init(driver, *args, **kwargs):
        actual_init(driver, *args, **kwargs)
        if (
            driver.running.claim.operation_id == operation_id
            and old_claims
            and driver.running.claim.ownership_generation == old_claims[0].ownership_generation + 1
        ):
            resumed_cold.append(driver.phases.runtime is None)
            assert driver.phases is not old_phases[0]
            assert driver.work.coordinator is not old_phases[0].work.coordinator

    def matches(selected):
        top = selected.stack.frames[-1]
        if cut == "later-compose":
            return type(top) is ComposeFrame and top.phase == "action_ready"
        return type(top) is EndFrame and top.phase == "review_needed"

    def failure_trace(original):
        if original is None:
            return None
        locations = []
        traceback = original.__traceback__
        while traceback is not None:
            locations.append((traceback.tb_frame.f_code.co_name, traceback.tb_lineno))
            traceback = traceback.tb_next
        cancellation = original.args if isinstance(original, asyncio.CancelledError) else ()
        # Only owned integrity/cancellation diagnostics and this fixture's
        # own assertions have safe messages; never print a provider payload.
        first_line = (
            str(original).splitlines()[:1]
            if isinstance(original, (AuditIntegrityError, asyncio.CancelledError, AssertionError))
            else ["original message omitted; frame names retained"]
        )
        return type(original).__name__, first_line, cancellation, locations

    async def observe_advance(driver, stored, selected):
        nonlocal force_handover
        if selected.stack.logical.operation_id != operation_id:
            return await actual_advance(driver, stored, selected)
        first_resumed_phase = (
            old_claims and driver.running.claim.ownership_generation == old_claims[0].ownership_generation + 1 and not selected_after
        )
        if first_resumed_phase:
            selected_after.append(selected)
        generation = driver.running.claim.ownership_generation
        phase_trace.append(("entered", generation, selected.stack.frames[-1].frame_kind, selected.stack.cursor))
        try:
            result = await actual_advance(driver, stored, selected)
        except BaseException as original:
            phase_trace.append(("raised", generation, failure_trace(original)))
            raise
        phase_trace.append(("returned", generation, selected.stack.frames[-1].frame_kind, selected.stack.cursor))
        if first_resumed_phase:
            # Control preflight may restore before advance; current ownership
            # and object identity must still be fresh for the claimed segment.
            runtime = driver.phases.runtime
            previous = old_runtimes[0]
            assert runtime is not None and previous is not None and runtime is not previous
            assert runtime.bindings is not previous.bindings
            assert runtime.materialization is not previous.materialization
            assert runtime.bindings.required_work is driver.work
            assert runtime.bindings.session_operation_context is driver.running.session_operation_context
            assert runtime.bindings.provider_owner is driver.provider
        # Observe later Compose only after the actual END repair owner has
        # returned; the selected root is then re-read from its committed SQL.
        probe_produced_cut = cut != "later-compose" or type(selected.stack.frames[-1]) is EndFrame
        if result is None and not selected_before and probe_produced_cut:
            following, current = await driver.selected()
            if matches(current):
                assert current.checkpoint_id != selected.checkpoint_id
                assert current.stack.cursor > selected.stack.cursor
                assert (following.checkpoint_id, following.cursor) == (current.checkpoint_id, current.stack.cursor)
                selected_before.append(current)
                old_claims.append(driver.running.claim)
                old_phases.append(driver.phases)
                old_runtimes.append(driver.phases.runtime)
                old_effects.extend(effects(harness.app, operation_id))
                force_handover = True
                if mode == "drain":
                    harness.app.state.composer_drain_signal.request()
        return result

    def refuse_one_fit(*args, **kwargs):
        nonlocal forced
        if mode != "drain" and force_handover and not forced:
            forced = True
            return SegmentFit.YIELD
        return actual_fit(*args, **kwargs)

    async def observe_dispose(driver, original=None):
        if driver.running.claim.operation_id == operation_id:
            phase_trace.append(("dispose", driver.running.claim.ownership_generation, failure_trace(original)))
            key = (driver.running.claim.session_id, operation_id)
            job = driver.worker._jobs[key]
            assert job is asyncio.current_task()
            owner_jobs.append(job)
            if mode == "drain":
                assert driver.worker.drain_deadline is not None
        try:
            outcome = await actual_dispose(driver, original)
        except BaseException as failure:
            if driver.running.claim.operation_id == operation_id:
                outcomes.append(failure)
                disposition_finished.set()
            raise
        if driver.running.claim.operation_id == operation_id:
            outcomes.append(outcome)
            disposition_finished.set()
        return outcome

    async def observe_settle(driver, record, original=None, **kwargs):
        if driver.running.claim.operation_id == operation_id:
            phase_trace.append(("settle", driver.running.claim.ownership_generation, failure_trace(original)))
        return await actual_settle(driver, record, original, **kwargs)

    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
    monkeypatch.setattr(driver_module._SegmentDriver, "__init__", observe_init)
    monkeypatch.setattr(driver_module._SegmentDriver, "dispose", observe_dispose)
    monkeypatch.setattr(driver_module._SegmentDriver, "settle", observe_settle)
    monkeypatch.setattr(driver_module, "admit_action", refuse_one_fit)
    async with _later_cut_app(tmp_path, monkeypatch, expect_unknown=mode == "rollback-before-release") as harness:
        sdk.app_ref.append(harness.app)
        repository = harness.app.state.session_service.session_operation_authority
        repository_type = type(repository)
        actual_suspend = repository_type.suspend_composer_segment
        actual_verify = repository_type.verify_recovery
        actual_readback = repository_type.read_suspend_outcome
        actual_immutable_verify = continuation_store.verify_recovery_on_connection
        actual_release = repository_type.release
        actual_recover = continuation_store.recover_segment_on_connection

        def suspend(owner, running, **kwargs):
            if running.claim.operation_id != operation_id:
                return actual_suspend(owner, running, **kwargs)
            suspend_attempts.append(running)
            if mode == "stop-before-suspend":
                stopped = harness.app.state.composer_async_operation_authority.request_cancel(
                    session_id=running.claim.session_id, operation_id=operation_id
                )
                assert stopped is not None and stopped.cancel_requested_at is not None
            receipt = actual_suspend(owner, running, **kwargs)
            committed.append(receipt)
            if mode == "ambiguous-after-commit":
                raise original
            return receipt

        def recover(conn, **kwargs):
            nonlocal rolled_back
            receipt = actual_recover(conn, **kwargs)
            if mode == "rollback-before-release" and kwargs["operation_id"] == operation_id and not rolled_back:
                rolled_back = True
                # This raises inside the real repository transaction after
                # its recovery writes, so those writes actually roll back.
                # The real drain also stops this process from subsequently
                # claiming unrelated recovery work while rollback is checked.
                harness.app.state.composer_drain_signal.request()
                raise original
            return receipt

        def verify(owner, receipt, **kwargs):
            result = actual_verify(owner, receipt, **kwargs)
            if receipt.operation_id == operation_id:
                verified.append(receipt)
            return result

        def readback(owner, observation, **kwargs):
            if observation.running.claim.operation_id == operation_id:
                readbacks.append(observation)
            receipt = actual_readback(owner, observation, **kwargs)
            if observation.running.claim.operation_id == operation_id:
                read_receipts.append(receipt)
            return receipt

        def immutable_verify(conn, receipt):
            result = actual_immutable_verify(conn, receipt)
            if receipt.operation_id == operation_id:
                immutable_verified.append(receipt)
            return result

        def release(owner, context, **kwargs):
            released.append(context)
            return actual_release(owner, context, **kwargs)

        monkeypatch.setattr(repository_type, "suspend_composer_segment", suspend)
        monkeypatch.setattr(repository_type, "verify_recovery", verify)
        monkeypatch.setattr(repository_type, "read_suspend_outcome", readback)
        monkeypatch.setattr(repository_type, "release", release)
        monkeypatch.setattr(continuation_store, "recover_segment_on_connection", recover)
        monkeypatch.setattr(continuation_store, "verify_recovery_on_connection", immutable_verify)
        body = message_body("Explain the current pipeline.", operation_id=operation_id)
        admission_only = mode in ("drain", "rollback-before-release")
        settling = asyncio.create_task(_admit_only(harness, body) if admission_only else submit_and_settle(harness, body))
        disposed = asyncio.create_task(disposition_finished.wait())
        try:
            # Test-only failure guard uses the existing canonical app's
            # configured duration; it grants no lease or production budget.
            try:
                async with asyncio.timeout(harness.app.state.settings.composer_timeout_seconds):
                    if admission_only:
                        await asyncio.shield(settling)
                        # A process drain deliberately withdraws HTTP executor
                        # admission. Its public poller is not the producer's
                        # completion oracle; wait for the actual dispose owner.
                        await asyncio.shield(disposed)
                    else:
                        await asyncio.wait((settling, disposed), return_when=asyncio.FIRST_COMPLETED)
            except TimeoutError as timeout:
                if cut == "later-compose":
                    raise TimeoutError(
                        f"actual later Compose disposition was not reached; phases={phase_trace!r}; sdk_roles={sdk.roles!r}"
                    ) from timeout
                raise
            assert selected_before, "the actual live owner did not produce the requested later cut"
            assert outcomes and (forced if mode != "drain" else not forced), "the actual worker did not attempt the required disposition"
            assert len(suspend_attempts) == 1
            expected_before = ["primary", "end"] if cut == "later-compose" else ["primary"]
            old_context = old_phases[0].lease.context
            if mode in ("stop-before-suspend", "rollback-before-release"):
                assert committed == [] and verified == []
                assert read_receipts == [] and immutable_verified == []
                if mode == "stop-before-suspend":
                    assert type(outcomes[0]) is ContinuationSegmentSettled
                    terminal = await settling
                    assert terminal.status == "failed" and terminal.error is not None
                    assert terminal.error.failure_code == "request_cancelled"
                    assert sum(context is old_context for context in released) == 1
                else:
                    assert rolled_back and isinstance(outcomes[0], BaseException)
                    assert len(owner_jobs) == 1
                    job = owner_jobs[0]
                    try:
                        # Bound only the test's join; shielding preserves the
                        # actual owner if cleanup cannot complete in this slice.
                        await asyncio.wait_for(asyncio.shield(job), timeout=harness.app.state.settings.composer_timeout_seconds)
                    except BaseException as owner_failure:
                        leaves = required_failure_leaves(owner_failure)
                        if not leaves or any(leaf is not original for leaf in leaves):
                            raise
                    assert job.done() and not job.cancelled()
                    handoff = tuple(
                        ticket
                        for ticket in old_phases[0].work.coordinator.tickets
                        if ticket.key.source is RequiredWorkSource.CONTINUATION_HANDOFF_SQL
                    )
                    assert len(handoff) == 1
                    actual = handoff[0]._finish_once_handoff
                    assert type(actual) is RequiredSQLRaised and actual.error is original
                    old_phases[0].work.coordinator.assert_completed()
                with harness.app.state.session_engine.connect() as conn:
                    assert (
                        conn.execute(select(events).where(events.c.operation_id == operation_id, events.c.tag == "recovered")).all() == []
                    )
                    assert conn.execute(
                        select(segments.c.sequence, segments.c.generation).where(segments.c.operation_id == operation_id)
                    ).all() == [(1, 1)]
                    if mode == "rollback-before-release":
                        (attempt,) = conn.execute(select(attempts).where(attempts.c.operation_id == operation_id)).all()
                        assert attempt.sequence == 1 and attempt.generation == 1
                        assert attempt.revoked_at is None and attempt.outcome is None
                        assert (attempt.session_operation_id, attempt.session_operation_epoch) == (
                            old_context.fence.operation_id,
                            old_context.fence.operation_epoch,
                        )
                        fence = conn.execute(select(fences).where(fences.c.session_id == str(harness.session_id))).one()
                        assert (fence.operation_id, fence.operation_epoch) == (
                            old_context.fence.operation_id,
                            old_context.fence.operation_epoch,
                        )
                        # The actual rollback is known to this injected SQL
                        # body, but its generic raised result is no production
                        # no-commit capability. Assert conservative retention,
                        # including the worker adoption, rather than clean exit.
                        lease = old_phases[0].lease
                        assert lease.disposition is SessionOperationLeaseDisposition.UNKNOWN
                        assert not lease.continuation_finished
                        assert fence.released_at is None
                        assert sum(context is old_context for context in released) == 0
                        key = (harness.session_id, operation_id)
                        adoption = harness.app.state.composer_async_worker._continuation_adoptions[key]
                        assert adoption.lease is lease
                assert sdk.roles == expected_before
                final_effects = {row.effect_key: row for row in effects(harness.app, operation_id)}
                assert final_effects == {row.effect_key: row for row in old_effects}
                return
            handover = outcomes[0]
            assert type(handover) is ContinuationSegmentHandedOver, handover
            assert (handover.receipt.sequence, handover.receipt.previous_generation, handover.receipt.generation) == (1, 1, 2)
            assert handover.receipt.released_context is not None
            assert len(committed) == 1 and committed[0] == handover.receipt
            assert len(immutable_verified) == 1 and immutable_verified[0] is committed[0]
            if mode == "ambiguous-after-commit":
                # Raised-after-commit uses the actual candidate readback,
                # whose immutable verification does not call verify_recovery.
                assert verified == [] and len(readbacks) == len(read_receipts) == 1
                observation, read_receipt = readbacks[0], read_receipts[0]
                assert observation.running is suspend_attempts[0]
                assert observation.running.claim is old_claims[0]
                assert observation.running.session_operation_context is old_context
                assert observation.candidate is read_receipt is committed[0] is handover.receipt
                assert handover.receipt.released_context is old_context
            else:
                assert len(verified) == 1 and verified[0] is committed[0]
                assert readbacks == read_receipts == []
            assert sum(context is old_context for context in released) == 0
            if mode == "drain":
                assert len(owner_jobs) == 1
                job = owner_jobs[0]
                # Join the actual disposing owner; run_until_idle would start
                # new reaper/claim work on the deliberately drained executor.
                await asyncio.wait_for(asyncio.shield(job), timeout=harness.app.state.settings.composer_timeout_seconds)
                assert job.done() and not job.cancelled()
                worker = harness.app.state.composer_async_worker
                key = (harness.session_id, operation_id)
                assert key not in worker._jobs
                assert key not in worker._continuation_adoptions and key not in worker._continuation_starts
                assert old_phases[0].lease.continuation_finished
                old_phases[0].work.coordinator.assert_completed()
                with harness.app.state.session_engine.connect() as conn:
                    retained = continuation_store.load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
                    after = decode_continuation_snapshot(retained.payload_json)
                    assert conn.execute(
                        select(segments.c.sequence, segments.c.generation, segments.c.state).where(segments.c.operation_id == operation_id)
                    ).all() == [(1, 2, "interrupted")]
                    (recovered,) = conn.execute(
                        select(events).where(events.c.operation_id == operation_id, events.c.tag == "recovered")
                    ).all()
                    assert recovered.sequence == handover.receipt.cursor
                before = selected_before[0].stack
                assert after.frames == before.frames and after.effect_frontier == before.effect_frontier
                assert after.semantic_frontier == before.semantic_frontier
                assert sdk.roles == expected_before and resumed_cold == []
                assert {row.effect_key: row for row in effects(harness.app, operation_id)} == {row.effect_key: row for row in old_effects}
                return
            if mode == "ambiguous-after-commit":
                handoff = tuple(
                    ticket
                    for ticket in old_phases[0].work.coordinator.tickets
                    if ticket.key.source is RequiredWorkSource.CONTINUATION_HANDOFF_SQL
                )
                assert len(handoff) == 1
                actual = handoff[0]._finish_once_handoff
                assert type(actual) is RequiredSQLRaised and actual.error is original
                read_work = tuple(
                    ticket
                    for ticket in old_phases[0].work.coordinator.tickets
                    if ticket.key.source is RequiredWorkSource.CONTINUATION_HANDOFF_READBACK_SQL
                )
                assert len(read_work) == 1 and read_work[0].complete
                actual_read = read_work[0]._finish_once_handoff
                assert type(actual_read) is RequiredSQLReturned and actual_read.value is handover.receipt
            terminal = await settling
            if terminal.status != "completed":
                error = terminal.error
                pytest.fail(
                    "\n".join(
                        (
                            f"actual successor did not complete: status={terminal.status!r}",
                            f"error={(error.http_status, error.failure_code, error.error_type) if error is not None else None!r}",
                            f"resumed_cold={resumed_cold!r}; sdk_roles={sdk.roles!r}",
                            *(f"phase[{index}]={entry!r}" for index, entry in enumerate(phase_trace)),
                        )
                    ),
                    pytrace=False,
                )
            assert terminal.result is not None
            assert terminal.result.message.content.split("\n\n---\n\n")[0] == words
            assert resumed_cold == [True], "a newly claimed generation must construct a fresh runtime owner"
            assert len(selected_after) == 1
            before, after = selected_before[0].stack, selected_after[0].stack
            assert after.frames == before.frames, "handover must preserve the actual paid/counter/source cohort"
            assert after.effect_frontier == before.effect_frontier
            assert after.semantic_frontier == before.semantic_frontier
            assert after.pending_settlement_ids == before.pending_settlement_ids == ()
            assert after.pending_owner_results == before.pending_owner_results == ()
            expected_roles = ["primary", "end", "primary", "end"] if cut == "later-compose" else ["primary", "end"]
            assert sdk.roles == expected_roles and not sdk.steps
            final_effects = {row.effect_key: row for row in effects(harness.app, operation_id)}
            for previous in old_effects:
                assert final_effects[previous.effect_key] == previous, "handover must not rewrite an existing effect/receipt"
            with harness.app.state.session_engine.connect() as conn:
                assert conn.execute(
                    select(segments.c.sequence, segments.c.generation, segments.c.state).where(segments.c.operation_id == operation_id)
                ).all() == [(1, 2, "settled")]
                history = conn.execute(
                    select(attempts).where(attempts.c.operation_id == operation_id).order_by(attempts.c.generation)
                ).all()
                assert [(row.sequence, row.generation, row.outcome) for row in history] == [(1, 1, "interrupted"), (1, 2, "settled")]
                assert history[0].revoked_at is not None and history[1].revoked_at is not None
                assert history[1].session_operation_epoch > history[0].session_operation_epoch
                (recovered,) = conn.execute(select(events).where(events.c.operation_id == operation_id, events.c.tag == "recovered")).all()
                assert recovered.sequence == handover.receipt.cursor
        finally:
            for task in (disposed, settling):
                if not task.done():
                    task.cancel()
            await asyncio.gather(disposed, settling, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("later-compose", "end-review"))
async def test_live_later_cut_hands_over_and_cold_resumes_without_repeating_work(tmp_path, monkeypatch, cut):
    await _run_later_cut_handover(tmp_path, monkeypatch, cut)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("drain", "stop-before-suspend", "ambiguous-after-commit", "rollback-before-release"))
async def test_live_later_cut_drain_stop_and_actual_suspend_outcomes(tmp_path, monkeypatch, mode):
    await _run_later_cut_handover(tmp_path, monkeypatch, "end-review", mode=mode)


@pytest.mark.parametrize("cut", ("advisor-input", "tool-ready", "end-repair", "end-publication"))
def test_future_only_cut_still_has_no_live_handover_owner(cut):
    root, _allowance = HANDOFF_SAFE_CUTS[cut]
    with pytest.raises(AuditIntegrityError, match="handoff-lineage owner"):
        classify_drain_cut(_selected(root))
