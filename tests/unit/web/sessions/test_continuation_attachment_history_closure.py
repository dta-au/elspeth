"""Whole cold positives plus same-claim SQL-boundary current-history refusals."""

import pytest

from elspeth.web.composer.continuation_attachment_application_driver import apply_current_attachment_runtime
from elspeth.web.composer.continuation_attachment_history import decode_attachment_history, encode_attachment_history
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.continuation_attachment_result import _chunk
from elspeth.web.sessions.locking import locked_session_transaction
from tests.unit.web.sessions import test_continuation_attachment_application as application_cases

operation_store = application_cases.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("missing_application", "rewound_caches"))
async def test_current_attachment_root_retains_complete_application_and_cache_history(operation_store, tmp_path, monkeypatch, change):
    arguments, runtime, ctx, call_model, source, _audit, sdk, _path = await application_cases.application_case(
        operation_store, tmp_path, monkeypatch
    )
    running = arguments["inventory"].running
    context = running.session_operation_context
    lifecycle_owns_context = False
    try:
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            operation_store[1], context, lease_seconds=30, required_work=arguments["required_work"].coordinator
        ) as lease:
            assert lease.context is context and lease.required_work is arguments["required_work"].coordinator
            # Published and inspected, but not finished, is still a legitimate
            # empty application cohort under the whole cold runtime.
            pending = await lease.create_task(restore_continuation_runtime(**arguments), name="same-claim-history-pending-cold")
            assert pending.graph.batch.batch.next_tool_index == 0
            applied = await lease.create_task(
                apply_current_attachment_runtime(
                    runtime=runtime,
                    selected=arguments["selected"],
                    inventory=arguments["inventory"],
                    ctx=ctx,
                    call_model=call_model,
                    effect_key=source.effect_key,
                ),
                name="same-claim-history-application",
            )
            latest = await lease.create_task(application_cases._latest(arguments), name="same-claim-history-current")
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="same-claim-history-completed-cold")
            assert cold.graph.batch.batch.next_tool_index == 1 and len(sdk) == 1
            await lease.create_task(
                application_cases._registered_runtime_sql_no_write(operation_store[0], latest), name="same-claim-history-sql-positive"
            )
            selected = latest["selected"]
            compose = selected.stack.frames[0]
            if change == "missing_application":
                history = decode_attachment_history(
                    _chunk(selected, compose.attachment_admissions),
                    session_id=selected.stack.logical.session_id,
                    operation_id=selected.stack.logical.operation_id,
                    env_ref_names=latest["inventory"].names,
                    source_chunks=(*arguments["selected"].chunks, *cold.source.admitted_evidence.dependency_chunks),
                )
                assert len(history.applications) == 1 and history.applications[0] == applied.application
                changed = encode_attachment_history(
                    history.model_copy(update={"applications": ()}), env_ref_names=latest["inventory"].names
                )
                changed_compose = compose.model_copy(update={"attachment_admissions": changed.reference})
            else:
                changed = encode_private_chunk(
                    codec="composer.caches.v1",
                    canonical=applied.application.before_caches_json,
                    env_ref_names=latest["inventory"].names,
                )
                assert changed.reference != compose.caches
                changed_compose = compose.model_copy(update={"caches": changed.reference})
            root = selected.stack.model_copy(update={"frames": (changed_compose, *selected.stack.frames[1:])})
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                put_chunk(
                    conn,
                    session_id=str(operation_store[4]),
                    operation_id=running.claim.operation_id,
                    key=private_chunk_key(changed),
                    payload=changed.canonical.encode("utf-8"),
                    codec=changed.reference.codec,
                )
                append_complete_snapshot(
                    conn,
                    session_id=str(operation_store[4]),
                    operation_id=running.claim.operation_id,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.stack.cursor,
                    snapshot_json=encode_continuation_snapshot(root),
                    event_batch=(ContinuationEvent("checkpoint", '{"fixture":"coherent current historical owner loss"}'),),
                    handoff=False,
                )
            latest = await lease.create_task(application_cases._latest(latest), name="same-claim-history-changed-current")
            message = "complete actual application cohort" if change == "missing_application" else "complete cache observation history"
            # Both complete snapshots and the actual refusal happen on one
            # registered current SQL connection while renewal remains live.
            await lease.create_task(
                application_cases._registered_runtime_sql_no_write(operation_store[0], latest, error=message),
                name="same-claim-history-sql-negative",
            )
            assert len(sdk) == 1
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        arguments["required_work"].coordinator.assert_completed()
    finally:
        if not lifecycle_owns_context:
            operation_store[1].release(context)
