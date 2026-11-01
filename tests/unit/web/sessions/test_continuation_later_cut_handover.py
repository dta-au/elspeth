"""G2: actual later cuts hand over and resume with fresh worker-owned runtime.

Use the existing app/lifespan/worker harness. The probes only observe real
phase commits and force one action-fit yield at the selected actual cut.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_clock import SegmentFit
from elspeth.web.composer.continuation_handoff import classify_drain_cut
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions.continuation_driver import ContinuationSegmentHandedOver
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, effects, end_admit, install_sdk, submit_and_settle
from tests.unit.web.composer.test_continuation_cut_coverage import HANDOFF_SAFE_CUTS, _selected


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("later-compose", "end-review"))
async def test_live_later_cut_hands_over_and_cold_resumes_without_repeating_work(tmp_path, monkeypatch, cut):
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
        return result

    def refuse_one_fit(*args, **kwargs):
        nonlocal forced
        if force_handover and not forced:
            forced = True
            return SegmentFit.YIELD
        return actual_fit(*args, **kwargs)

    async def observe_dispose(driver, original=None):
        if driver.running.claim.operation_id == operation_id:
            phase_trace.append(("dispose", driver.running.claim.ownership_generation, failure_trace(original)))
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

    async def observe_settle(driver, record, original=None):
        if driver.running.claim.operation_id == operation_id:
            phase_trace.append(("settle", driver.running.claim.ownership_generation, failure_trace(original)))
        return await actual_settle(driver, record, original)

    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
    monkeypatch.setattr(driver_module._SegmentDriver, "__init__", observe_init)
    monkeypatch.setattr(driver_module._SegmentDriver, "dispose", observe_dispose)
    monkeypatch.setattr(driver_module._SegmentDriver, "settle", observe_settle)
    monkeypatch.setattr(driver_module, "admit_action", refuse_one_fit)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        settling = asyncio.create_task(submit_and_settle(harness, message_body("Explain the current pipeline.", operation_id=operation_id)))
        disposed = asyncio.create_task(disposition_finished.wait())
        try:
            # Test-only failure guard uses the existing canonical app's
            # configured duration; it grants no lease or production budget.
            try:
                await asyncio.wait_for(
                    asyncio.wait((settling, disposed), return_when=asyncio.FIRST_COMPLETED),
                    timeout=harness.app.state.settings.composer_timeout_seconds,
                )
            except TimeoutError as timeout:
                if cut == "later-compose":
                    raise TimeoutError(
                        f"actual later Compose disposition was not reached; phases={phase_trace!r}; sdk_roles={sdk.roles!r}"
                    ) from timeout
                raise
            assert selected_before, "the actual live owner did not produce the requested later cut"
            assert forced and outcomes, "the actual worker did not attempt disposition at that cut"
            handover = outcomes[0]
            assert type(handover) is ContinuationSegmentHandedOver, handover
            assert (handover.receipt.sequence, handover.receipt.previous_generation, handover.receipt.generation) == (1, 1, 2)
            assert handover.receipt.released_context is not None
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


@pytest.mark.parametrize("cut", ("advisor-input", "tool-ready", "end-repair", "end-publication"))
def test_future_only_cut_still_has_no_live_handover_owner(cut):
    root, _allowance = HANDOFF_SAFE_CUTS[cut]
    with pytest.raises(AuditIntegrityError, match="handoff-lineage owner"):
        classify_drain_cut(_selected(root))
