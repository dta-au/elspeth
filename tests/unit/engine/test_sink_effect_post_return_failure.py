"""A failure after the provider returned leaves an honest attempt record.

The external effect has already happened when the provider returns. If
anything fails before its result is durably recorded, the attempt must read
``response_lost`` (outcome unknown, reconcile next), never stay at ``intent``
(which claims no call was made).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from elspeth.contracts.sink_effects import SinkEffectCommitResult
from elspeth.core.landscape.execution.sink_effect_attempt_results import SinkEffectReturnedResult
from elspeth.engine.executors import sink_effects as sink_effects_module
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members
from tests.unit.engine.test_sink_effect_executor import _CumulativeObservableSink, _CumulativeTarget, _execution_request


def test_failure_after_the_provider_returned_records_the_commit_as_response_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    encode_returned_result = sink_effects_module.encode_sink_effect_returned_result

    def fail_after_commit_returned(result: SinkEffectReturnedResult) -> Mapping[str, object]:
        if type(result) is SinkEffectCommitResult:
            raise RuntimeError("injected failure after the provider returned")
        return encode_returned_result(result)

    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        # raising=True: if the fix renames this step, the test fails loudly instead of passing vacuously.
        monkeypatch.setattr(sink_effects_module, "encode_sink_effect_returned_result", fail_after_commit_returned, raising=True)

        with pytest.raises(RuntimeError, match="injected failure after the provider returned"):
            SinkEffectCoordinator(factory=factory, worker_id="worker-a", coordination_token=leader_token_for(db, run_id)).execute(
                _execution_request(run_id, sink_id, members), _CumulativeObservableSink(target)
            )

        assert target.published_rows == [[{"ordinal": 0}]]
        commit_states = [
            attempt.state.value
            for attempt in make_factory(db).execution.sink_effects.get_attempts_for_run(run_id)
            if attempt.action.value == "commit"
        ]
        assert commit_states == ["response_lost"]
    finally:
        db.close()
