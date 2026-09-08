"""A deadline completion refusal propagates without replaying adapter effects."""

from __future__ import annotations

from datetime import timedelta

import pytest

from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.sink_effects import SinkEffectAttemptAction, SinkEffectAttemptState, SinkEffectLease
from elspeth.core.landscape.lease_deadlines import LeaseDeadlineExpiredError
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, _SinkEffectLeaseHeartbeat
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_lifecycle import _claim, _reserved
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members
from tests.unit.engine.test_sink_effect_executor import _CumulativeObservableSink, _CumulativeTarget, _execution_request


def test_background_heartbeat_latches_original_completion_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        effect = _reserved(factory)
        claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
        token = leader_coordination_token(factory, effect.run_id)
        failure = LeaseDeadlineExpiredError("effect renewal reserve consumed")
        beats = 0

        def refuse(
            effect_id: str, *, owner: str, generation: int, ttl: timedelta, coordination_token: CoordinationToken
        ) -> SinkEffectLease:
            nonlocal beats
            assert (effect_id, owner, generation, coordination_token) == (claim.effect_id, claim.owner, claim.generation, token)
            beats += 1
            raise failure

        monkeypatch.setattr(factory.execution.sink_effects, "heartbeat_lease", refuse)
        heartbeat = _SinkEffectLeaseHeartbeat(
            effects=factory.execution.sink_effects, claim=claim, ttl=timedelta(milliseconds=30), coordination_token=token
        )
        heartbeat.start()
        try:
            assert heartbeat._failed_event.wait(5)
            with pytest.raises(LeaseDeadlineExpiredError) as caught:
                heartbeat.refresh_and_check(coordination_token=token)
            assert caught.value is failure
        finally:
            heartbeat.stop()
        assert beats == 1
    finally:
        db.close()


@pytest.mark.parametrize("wait_for_lease", (False, True))
def test_timeout_after_publication_is_preserved_without_retry(monkeypatch: pytest.MonkeyPatch, wait_for_lease: bool) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        request = _execution_request(run_id, sink_id, members)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        failure = LeaseDeadlineExpiredError("post-publication renewal reserve consumed")
        original_heartbeat = factory.execution.sink_effects.heartbeat_lease

        def heartbeat(
            effect_id: str, *, owner: str, generation: int, ttl: timedelta, coordination_token: CoordinationToken
        ) -> SinkEffectLease:
            if target.published_rows:
                raise failure
            return original_heartbeat(effect_id, owner=owner, generation=generation, ttl=ttl, coordination_token=coordination_token)

        monkeypatch.setattr(factory.execution.sink_effects, "heartbeat_lease", heartbeat)
        coordinator = SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-a",
            lease_ttl=timedelta(seconds=30),
            coordination_token=leader_coordination_token(factory, run_id),
        )
        with pytest.raises(LeaseDeadlineExpiredError) as caught:
            if wait_for_lease:
                coordinator.execute_with_lease_wait(request, sink)
            else:
                coordinator.execute(request, sink)
        assert caught.value is failure
        assert sink.commit_calls == 1
        assert target.published_rows == [[{"ordinal": 0}]]
        assert target.effect_id is not None
        commits = tuple(
            attempt
            for attempt in factory.execution.sink_effects.get_attempts(target.effect_id)
            if attempt.action is SinkEffectAttemptAction.COMMIT
        )
        assert len(commits) == 1
        assert commits[0].state is SinkEffectAttemptState.RESPONSE_LOST
    finally:
        db.close()
