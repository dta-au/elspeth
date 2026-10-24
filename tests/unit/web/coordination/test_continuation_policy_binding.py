"""U-38: settings bind only at admission; an admitted operation keeps its policy.

Design §4.2. A start decodes the policy retained in the operation's snapshot,
never the current settings, so changing settings (or turning V2 off) cannot
change an operation already admitted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination.test_composer_continuation_claim_start import queued, start_segment
from tests.unit.web.coordination.test_composer_operation_authority import (
    operation_store as _operation_store,
)

from elspeth.web.composer.continuation_clock import size_segment
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.time_normalization import restore_utc

operation_store = _operation_store


@pytest.mark.parametrize("later", ("changed_settings", "flag_off"))
def test_a_settings_change_after_admit_does_not_touch_the_retained_policy(operation_store, later: str) -> None:
    engine, repository, _authority, _sessions, sid = operation_store
    admitted_policy = minimal_continuation_policy()
    _admitting, record = queued(operation_store, policy=admitted_policy)
    with engine.connect() as conn:
        retained = decode_continuation_snapshot(load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id).payload_json)
    assert retained.policy_hash == admitted_policy.policy_hash

    # The process restarts with other settings: a different nominal segment, or V2 off.
    current_policy = (
        minimal_continuation_policy(composer_continuation_segment_nominal_seconds=900.0) if later == "changed_settings" else None
    )
    assert current_policy is None or current_policy.segment.nominal != admitted_policy.segment.nominal
    current = ComposerAsyncOperationAuthority(
        engine, owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=current_policy
    )
    (claim,) = current.claim_segment(limit=1)
    start_segment(repository, claim)

    with engine.connect() as conn:
        segment = conn.execute(select(segments)).one()
        after = decode_continuation_snapshot(load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id).payload_json)
    assert after.policy_hash == admitted_policy.policy_hash == segment.policy_sha256
    assert after.admission_policy_json == retained.admission_policy_json
    started = (restore_utc(segment.deadline_at) - restore_utc(segment.started_at)).total_seconds()
    assert started == size_segment(admitted_policy.actions.session_sql, admitted_policy.segment)
