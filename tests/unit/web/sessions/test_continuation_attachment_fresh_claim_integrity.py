"""Complete warm positives, then genuine fresh-claim no-write corrupt-source checks.

The renewing original lifetime is fully closed before recovery. The new current
claim has no background renewal inside its exact eight-table no-write window.
These tests do not replace the preserved same-claim corruption tests silently.
"""

from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_attachment_application_driver import apply_current_attachment_runtime
from elspeth.web.composer.continuation_attachment_history import decode_attachment_history, encode_attachment_history
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.continuation_attachment_result import _chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners, recovery_rows
from tests.unit.web.sessions import test_continuation_attachment_application as application_cases
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = application_cases.operation_store


def _recover_released_application(store, original):
    current = new_owners(store)
    _engine, repository, authority, _sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=original.claim.operation_id,
            expected_generation=original.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    assert context.fence != original.session_operation_context.fence
    assert claim.operation_id == original.claim.operation_id
    return current, ComposerOperationRunning(claim=claim, session_operation_context=context)


async def _fresh_arguments(current, running, work):
    selected, inventory = await current_selected(current, running)
    service, _actors = current_service(current)
    assert inventory.running is running and work.running is running
    return {
        "service": service,
        "authority": current[2],
        "selected": selected,
        "inventory": inventory,
        "required_work": work,
        "provider_owner": ProviderInvocationOwner(service=current[3], required_work=work),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("graph", "receipt", "source"))
async def test_fresh_claim_preserves_prior_p4_proofs_after_complete_attachment_positive(operation_store, tmp_path, monkeypatch, change):
    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_cases.application_case(
        operation_store, tmp_path, monkeypatch, post_p4=True
    )
    original = arguments["inventory"].running
    prior, result, _historical_latest, cold = await application_cases._complete_prior_p4_with_live_lease(
        operation_store, arguments, runtime, ctx, call_model, source
    )
    assert cold.graph.applications.applications == prior.applications
    assert cold.graph.batch.batch.next_tool_index == 1 and result.application.effect_key == source.effect_key
    current, running = _recover_released_application(operation_store, original)
    work = inventory_work(running)
    try:
        latest = await _fresh_arguments(current, running, work)
        # The complete old positive has already run. This new physical claim
        # only checks current immutable history; it does not dispatch anything.
        if change == "graph":
            selected = latest["selected"]
            top = selected.stack.frames[-1]
            changed = replace(
                selected,
                stack=selected.stack.model_copy(
                    update={"frames": (selected.stack.frames[0], top.model_copy(update={"next_tool_index": top.next_tool_index + 1}))}
                ),
            )
            latest = {**latest, "selected": changed}
            message = "cold evidence changed the complete selected root"
        else:
            record = prior.applications[0]
            with current[0].begin() as conn:
                if change == "receipt":
                    changed = conn.execute(
                        update(effects)
                        .where(
                            effects.c.session_id == str(current[4]),
                            effects.c.operation_id == running.claim.operation_id,
                            effects.c.effect_key == record.effect_key,
                        )
                        .values(receipt_key="missing-original-p4-receipt")
                    )
                    message = "application journal lost its actual committed effect"
                else:
                    raw = b'{"coherently_changed_original_p4_source":true}'
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(current[4]),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == f"value:{record.result.codec}:{record.result.digest}",
                        )
                        .values(payload=raw, byte_length=len(raw), sha256=sha256(raw).hexdigest())
                    )
                    message = "private chunk bytes differ from their binding"
                assert changed.rowcount == 1
        before = recovery_rows(current[0])
        with pytest.raises(AuditIntegrityError, match=message):
            await restore_continuation_runtime(**latest)
        assert recovery_rows(current[0]) == before
    finally:
        work.coordinator.assert_completed()
        current[1].release(running.session_operation_context)


async def _complete_history_with_live_lease(store, arguments, runtime, ctx, call_model, source, sdk):
    running = arguments["inventory"].running
    work = arguments["required_work"]
    lifecycle_owns_context = False
    try:
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
        ) as lease:
            assert lease.context is running.session_operation_context and lease.required_work is work.coordinator
            pending = await lease.create_task(restore_continuation_runtime(**arguments), name="attachment-history-unfinished-positive")
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
                name="attachment-history-application",
            )
            latest = await lease.create_task(application_cases._latest(arguments), name="attachment-history-latest")
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="attachment-history-completed-positive")
            assert cold.graph.batch.batch.next_tool_index == 1 and len(sdk) == 1
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        work.coordinator.assert_completed()
        return applied
    finally:
        if not lifecycle_owns_context:
            store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("missing_application", "rewound_caches"))
async def test_fresh_claim_refuses_current_attachment_event_or_cache_loss_after_complete_positive(
    operation_store, tmp_path, monkeypatch, change
):
    arguments, runtime, ctx, call_model, source, _audit, sdk, _path = await application_cases.application_case(
        operation_store, tmp_path, monkeypatch
    )
    original = arguments["inventory"].running
    applied = await _complete_history_with_live_lease(operation_store, arguments, runtime, ctx, call_model, source, sdk)
    current, running = _recover_released_application(operation_store, original)
    work = inventory_work(running)
    try:
        latest = await _fresh_arguments(current, running, work)
        selected = latest["selected"]
        compose = selected.stack.frames[0]
        if change == "missing_application":
            history = decode_attachment_history(
                _chunk(selected, compose.attachment_admissions),
                session_id=selected.stack.logical.session_id,
                operation_id=selected.stack.logical.operation_id,
                env_ref_names=latest["inventory"].names,
            )
            assert len(history.applications) == 1 and history.applications[0] == applied.application
            changed = encode_attachment_history(history.model_copy(update={"applications": ()}), env_ref_names=latest["inventory"].names)
            changed_compose = compose.model_copy(update={"attachment_admissions": changed.reference})
        else:
            changed = encode_private_chunk(
                codec="composer.caches.v1", canonical=applied.application.before_caches_json, env_ref_names=latest["inventory"].names
            )
            assert changed.reference != compose.caches
            changed_compose = compose.model_copy(update={"caches": changed.reference})
        root = selected.stack.model_copy(update={"frames": (changed_compose, *selected.stack.frames[1:])})
        with locked_session_transaction(current[0], str(current[4])) as conn:
            put_chunk(
                conn,
                session_id=str(current[4]),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(changed),
                payload=changed.canonical.encode("utf-8"),
                codec=changed.reference.codec,
            )
            append_complete_snapshot(
                conn,
                session_id=str(current[4]),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root),
                event_batch=(ContinuationEvent("checkpoint", '{"fixture":"coherent current historical owner loss after fresh claim"}'),),
                handoff=False,
            )
        latest = await application_cases._latest(latest)
        before = recovery_rows(current[0])
        message = "complete actual application cohort" if change == "missing_application" else "complete cache observation history"
        with pytest.raises(AuditIntegrityError, match=message):
            await restore_continuation_runtime(**latest)
        assert recovery_rows(current[0]) == before and len(sdk) == 1
    finally:
        work.coordinator.assert_completed()
        current[1].release(running.session_operation_context)
