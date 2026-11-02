"""Original uninvoked issuer and action-expiry custody controls."""

from dataclasses import dataclass

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation_classifier_owned_source import ClassifierOwnedSourceObservation
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import continuation_classifier_primary as classifier
from elspeth.web.sessions import continuation_driver as driver
from elspeth.web.sessions import continuation_sql_deadline as sql_deadline
from tests.unit.web.sessions.test_continuation_classifier_primary import _classifier_case
from tests.unit.web.sessions.test_continuation_classifier_primary import operation_store as _operation_store

operation_store = _operation_store


@dataclass(frozen=True)
class _ClockSample:
    now: float

    def monotonic(self) -> float:
        return self.now


def _original_deadline(issuer):
    owner = issuer.work.coordinator.sql_deadline_owner
    now = classifier.time.monotonic()
    deadline = min(
        issuer.runtime.bindings.deadline,
        issuer.clock.started_monotonic + issuer.clock.policy.total,
        now + owner.remaining_dispatch(monotonic_now=now),
    )
    issuer.bind_reply_deadline(deadline)
    return deadline


def _observation(issuer, deadline):
    fence = issuer.running.session_operation_context.fence
    return ClassifierOwnedSourceObservation(
        kind="reply_deadline_exhausted",
        source_checkpoint_id=issuer.selected.checkpoint_id,
        source_cursor=issuer.selected.stack.cursor,
        state_sha256=stable_hash(issuer.runtime.live.state.to_dict()),
        physical_operation_id=fence.operation_id,
        physical_operation_epoch=fence.operation_epoch,
        deadline=deadline,
        observed_monotonic=deadline + 0.001,
        clock_started_monotonic=issuer.clock.started_monotonic,
        clock_policy_json=canonical_json(issuer.clock.policy.model_dump(mode="json")),
    )


@pytest.mark.asyncio
async def test_original_uninvoked_reply_seal_closes_dispatch_and_rejects_copy_reuse(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (_phases, _running, runtime, issuer, prepared, sdk):
        deadline = _original_deadline(issuer)
        observation = _observation(issuer, deadline)
        with monkeypatch.context() as scoped:
            scoped.setattr(classifier, "time", _ClockSample(deadline + 0.002))
            token = issuer.seal_reply_deadline(observation, runtime_preflight_json=None)
            assert issuer.require_reply_deadline(token) is observation
            with pytest.raises(AuditIntegrityError, match="original never-invoked"):
                issuer.seal_reply_deadline(observation, runtime_preflight_json=None)
        with pytest.raises(AuditIntegrityError, match="exact original issuer"):
            issuer.require_reply_deadline(classifier.ClassifierReplyDeadline(observation, None))
        with pytest.raises(AuditIntegrityError, match="permanently closed"):
            await issuer.invoke(branch="staged_review", prepared_input=prepared, recorder=runtime.live.recorder, deadline=deadline)
        assert issuer.sdk_entered is False
        assert len(sdk) == 1  # The real fixture's original PRIMARY only.


@pytest.mark.asyncio
async def test_reply_seal_refuses_substituted_source_clock_and_elapsed_observation(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (_phases, _running, _runtime, issuer, _prepared, _sdk):
        deadline = _original_deadline(issuer)
        observation = _observation(issuer, deadline)
        opponents = (
            {"deadline": deadline - 1.0},
            {"observed_monotonic": deadline - 1.0},
            {"observed_monotonic": deadline + 1.0},
            {"state_sha256": "0" * 64},
            {"source_cursor": observation.source_cursor + 1},
            {"physical_operation_epoch": observation.physical_operation_epoch + 1},
            {"clock_started_monotonic": issuer.clock.started_monotonic - 1.0},
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(classifier, "time", _ClockSample(deadline + 0.002))
            for changed in opponents:
                with pytest.raises(AuditIntegrityError, match="original never-invoked"):
                    issuer.seal_reply_deadline(observation.model_copy(update=changed), runtime_preflight_json=None)
            token = issuer.seal_reply_deadline(observation, runtime_preflight_json=None)
            assert issuer.require_reply_deadline(token) is observation


@pytest.mark.asyncio
async def test_started_but_unentered_classifier_cannot_become_uninvoked_deadline(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (_phases, _running, runtime, issuer, prepared, sdk):
        deadline = _original_deadline(issuer)
        # The real issuer starts, then refuses before preparation. Even an
        # absent SQL anchor plus false sdk_entered cannot erase that invocation.
        with (
            prompt_guard_user_scope(runtime.bindings.message),
            pytest.raises(TimeoutError, match="expired before preparation"),
        ):
            await issuer.invoke(branch="staged_review", prepared_input=prepared, recorder=runtime.live.recorder, deadline=0.0)
        assert issuer.sdk_entered is False
        with monkeypatch.context() as scoped:
            scoped.setattr(classifier, "time", _ClockSample(deadline + 0.002))
            with pytest.raises(AuditIntegrityError, match="original never-invoked"):
                issuer.seal_reply_deadline(_observation(issuer, deadline), runtime_preflight_json=None)
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_sealed_reply_cannot_follow_another_action_or_renew_its_reserve(operation_store, tmp_path, monkeypatch):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (_phases, running, _runtime, issuer, _prepared, _sdk):
        deadline = _original_deadline(issuer)
        owner = issuer.work.coordinator.sql_deadline_owner
        action_expires = min(owner.hard_expires_at, issuer.clock.started_monotonic + issuer.clock.policy.total)
        owner.require_action_clock(issuer.clock)
        foreign = ContinuationActionClock(owner.policy.actions.primary)
        with pytest.raises(AuditIntegrityError, match="original active action"):
            owner.require_action_clock(foreign)
        with monkeypatch.context() as scoped:
            scoped.setattr(classifier, "time", _ClockSample(deadline + 0.002))
            token = issuer.seal_reply_deadline(_observation(issuer, deadline), runtime_preflight_json=None)
            scoped.setattr(sql_deadline, "time", _ClockSample(action_expires + 0.002))
            with pytest.raises(TimeoutError, match="original action/drain"):
                owner.remaining_statement(session_id=str(running.claim.session_id))
            owner.begin_settlement()
            assert owner.remaining_statement(session_id=str(running.claim.session_id)) > 0
            owner.begin_settlement()
            scoped.setattr(sql_deadline, "time", _ClockSample(action_expires + owner.policy.segment.reserve + 1.0))
            with pytest.raises(TimeoutError, match="original action/drain"):
                owner.remaining_statement(session_id=str(running.claim.session_id))
        owner.begin_action(foreign.policy, started_at=foreign.started_monotonic)
        with pytest.raises(AuditIntegrityError, match="original active action"):
            issuer.require_reply_deadline(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_first", (False, True))
async def test_actual_deadline_signal_preserves_cause_and_distinguishes_timer_first(
    operation_store, tmp_path, monkeypatch, completed_first
):
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, _running, _runtime, issuer, _prepared, _sdk):
        producer = phases.work.coordinator.reserve(RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER, recurrence_ordinal=991)
        signal = phases.work.coordinator.reserve(RequiredWorkSource.DEADLINE_SIGNAL, recurrence_ordinal=991)
        deadline = issuer.clock.started_monotonic + issuer.clock.policy.total
        expiry = driver._ActionExpiry(phases, producer, signal, deadline)
        with monkeypatch.context() as scoped:
            scoped.setattr(driver, "time", _ClockSample(deadline + 0.002))
            if completed_first:
                expiry.completed_at = deadline + 0.001
                assert expiry.close(None) == ()
            else:
                expiry.fire()
                expiry.completed_at = deadline + 0.001
                assert expiry.close(None) == ()
        assert expiry.expired is True
        assert expiry.interrupted is (not completed_first)
        assert phases.cancellation_requested.is_set() is (not completed_first)
        receipts = signal.receipts()
        assert len(receipts) == 1
        assert receipts[0].original_root is expiry.cause
        assert receipts[0].key == signal.key
        assert receipts[0].original_category_witnesses == (expiry.cause,)
        assert producer.complete and signal.complete
