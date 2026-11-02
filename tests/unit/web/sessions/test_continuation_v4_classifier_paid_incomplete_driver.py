"""The applied V4 classifier's paid incomplete response reaches a cold public terminal."""

from __future__ import annotations

import asyncio
import json

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions.continuation_classifier_primary import VerifiedClassifierPaidIncompleteTerminal
from elspeth.web.sessions.continuation_classifier_primary_incomplete_terminal import (
    read_v4_classifier_paid_incomplete_terminal_on_connection,
)
from elspeth.web.sessions.continuation_driver import ContinuationSegmentSettled
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import _incomplete_reply, _v4_applied_timed_case
from tests.unit.web.sessions.test_continuation_tool_failure import _actual_failure_driver

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_v4_classifier_returned_incomplete_reaches_one_cold_public_507_without_retry(operation_store, tmp_path, monkeypatch):
    async with _v4_applied_timed_case(operation_store, tmp_path, monkeypatch) as (
        store,
        prior_owner,
        running,
        _selected,
        _inventory,
        _captured,
        prior_sdk,
    ):
        work, lease = prior_owner.work, prior_owner.lease
        timing = work.coordinator.sql_deadline_owner
        assert timing is not None and lease.required_work is work.coordinator
        phases = ContinuationLivePhases(prior_owner.service, prior_owner.authority, work, prior_owner.provider_owner, lease)
        driver = _actual_failure_driver(store, phases, running, drain_signal=timing.drain_signal)
        driver.work, driver.provider = work, prior_owner.provider_owner
        key = (running.claim.session_id, running.claim.operation_id)
        local_cancel = asyncio.Event()
        assert key not in driver.worker._local_cancels
        driver.worker._local_cancels[key] = local_cancel
        classifier_calls = []
        observed: list[VerifiedClassifierPaidIncompleteTerminal] = []
        advances: list[tuple[int, str, str]] = []
        actual_advance = driver_module._SegmentDriver.advance

        async def completion(**kwargs):
            classifier_calls.append(kwargs)
            return _incomplete_reply()

        async def observe_advance(self, stored, selected):
            frame = selected.stack.frames[-1]
            result = await actual_advance(self, stored, selected)
            advances.append(
                (stored.cursor, frame.phase if frame.frame_kind != "initialize" else frame.ingress_phase, type(result).__name__)
            )
            if type(result) is VerifiedClassifierPaidIncompleteTerminal:
                observed.append(result)
            return result

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
        try:
            outcome = await driver.run(None)
            assert type(outcome) is ContinuationSegmentSettled and outcome.terminal.status == "failed"
            with store[0].connect() as conn:
                parent = conn.execute(
                    select(jobs).where(
                        jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id
                    )
                ).one()
                persisted_effects = conn.execute(
                    select(effects).where(
                        effects.c.session_id == str(running.claim.session_id), effects.c.operation_id == running.claim.operation_id
                    )
                ).all()
                attempts = conn.execute(select(physical).where(physical.c.session_id == str(running.claim.session_id))).all()
                ledger = conn.execute(select(usage).where(usage.c.session_id == str(running.claim.session_id))).all()
            assert len(prior_sdk) == 1 and len(classifier_calls) == len(observed) == 1, (
                advances,
                parent.status,
                parent.result_json,
                tuple((type(error).__name__, str(error).splitlines()[0]) for error in driver._consumed_native),
                tuple((effect.effect_key, effect.disposition, effect.provider_attempt_id) for effect in persisted_effects),
            )
            verified = observed[0].verified
            with store[0].connect() as conn:
                cold = read_v4_classifier_paid_incomplete_terminal_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    effect_key=verified.receipt.identity.effect_key,
                )
                hold = conn.execute(
                    select(holds).where(
                        holds.c.session_id == str(running.claim.session_id), holds.c.effect_key == verified.receipt.identity.effect_key
                    )
                ).one()
            assert cold == verified and cold is not None and cold.hold_state == "consumed"
            public = json.loads(parent.result_json)
            assert public["http_status"] == 507 and public["error_type"] == "paid_result_incomplete"
            assert public["body"]["retryable"] is False and public["body"]["evidence_status"] == "incomplete"
            assert (outcome.terminal.checkpoint_id, outcome.terminal.journal_cursor) == (
                verified.terminal.checkpoint_id,
                verified.terminal.cursor,
            )
            assert hold.state == "consumed" and len(attempts) == len(ledger) == 2
            assert len({attempt.ledger_entry_id for attempt in attempts}) == 2
            assert verified.receipt.retryable is False and verified.receipt.provider_returned is True
            work.coordinator.assert_completed()
        finally:
            await driver.close_progress()
            assert driver.worker._local_cancels.pop(key) is local_cancel
