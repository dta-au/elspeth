"""V3 tool refusal at the selected call cut, with a synthetic paid source seam.

The synthetic source isolates the tool SQL writer. The live V3 PRIMARY owner
currently refuses before a paid response, so this fixture does not claim an
end-to-end V3 paid PRIMARY history.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import encode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_response, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import ContinuationEnvSelection, CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_phase_transitions import refuse_v3_tool_before_dispatch
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.continuation_tool_no_execution import read_v3_tool_no_execution_refusal_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_p4_owner as p4_cases
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_v3_paid_tail_preflight import _store_with_policy

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.asyncio
async def test_original_v3_tool_cut_refuses_before_issue_and_cold_rejects_unproven_primary(operation_store, tmp_path, monkeypatch):
    store, _policy = _store_with_policy(operation_store, monkeypatch, version="v3")
    kwargs, _produced, _recorder, _requests = await primary_input(store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    selected = kwargs["selected"]
    batch, acc, response, _prepared = p4_cases.completed_cut(running, selected.stack)
    batch = replace(batch, phase="call_ready", next_tool_index=0, tool_outcomes=[], private_authorings_by_call={})
    batch_chunk = encode_coupled_batch_chunk(batch=batch, accumulator=acc, calls=response.tool_batch.calls)
    response_chunk = encode_continuation_response(response)
    sid, operation = str(store[4]), running.claim.operation_id
    with locked_session_transaction(store[0], sid) as conn:
        for chunk in (batch_chunk, response_chunk):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode(),
                codec=chunk.reference.codec,
            )
        root = selected.stack.model_copy(
            update={
                "frames": (
                    *selected.stack.frames,
                    ToolBatchFrame(
                        phase="tool_ready",
                        response=response_chunk.reference,
                        accumulator=batch_chunk.reference,
                        next_tool_index=0,
                        settled_effect_ids=(),
                    ),
                )
            }
        )
        head = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("tool_prepared", '{"fixture":"synthetic historical paid source"}'),),
            handoff=False,
        )
    root = decode_continuation_snapshot(head.payload_json)
    selected = SelectedContinuationState(head.checkpoint_id, root, (*selected.chunks, response_chunk, batch_chunk))
    inventory = CurrentContinuationEnvInventory(
        running,
        ContinuationEnvSelection(head.checkpoint_id, root.cursor, encode_continuation_snapshot(root)),
        kwargs["inventory"].names,
    )
    # Isolate the refusal transaction after a historical committed PRIMARY
    # source. Production never substitutes this proof function.
    monkeypatch.setattr(
        "elspeth.web.sessions.continuation_tool_no_execution.read_committed_primary_response_on_connection",
        lambda _conn, _running, *, selected, reference: response_chunk,
    )
    receipt = refuse_v3_tool_before_dispatch(store[2], running, selected=selected, inventory=inventory)
    assert receipt.reason == "retained_tail_unbounded"
    with store[0].connect() as conn:
        original_parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
        effect = conn.execute(
            select(effects).where(
                effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == receipt.effect_key
            )
        ).one()
        assert effect.owner == "tool_dispatch" and effect.disposition == "refused"
        assert effect.provider_attempt_id is None and effect.physical_sequence is None
        assert (
            conn.execute(select(jobs.c.status).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).scalar_one() == "failed"
        )
        assert (
            conn.execute(select(segments.c.state).where(segments.c.session_id == sid, segments.c.operation_id == operation)).scalar_one()
            == "settled"
        )
        with pytest.raises(AuditIntegrityError):
            read_v3_tool_no_execution_refusal_on_connection(
                conn, session_id=store[4], operation_id=operation, effect_key=receipt.effect_key
            )
    with pytest.raises(ComposerOperationFenceLost):
        refuse_v3_tool_before_dispatch(store[2], running, selected=selected, inventory=inventory)
    with pytest.raises(IntegrityError, match="composer operation immutable transition"), store[0].begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation).values(continuation_policy_sha256="0" * 64)
        )
    with store[0].connect() as conn:
        assert conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one() == original_parent
        assert (
            conn.execute(
                select(effects).where(
                    effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == receipt.effect_key
                )
            ).one()
            == effect
        )
        with pytest.raises(AuditIntegrityError):
            read_v3_tool_no_execution_refusal_on_connection(
                conn, session_id=store[4], operation_id=operation, effect_key=receipt.effect_key
            )
