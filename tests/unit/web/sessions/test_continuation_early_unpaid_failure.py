"""Generic failure disposition at an actual unpaid EARLY predecessor.

The shared primitive fixture runs PRIMARY/tool/P4 and binds its own original
phase clock. These prove SQL disposition, not app/worker deadline acceptance.
"""

from __future__ import annotations

import json
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_next_action import validate_continuation_structure_on_connection
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions import continuation as continuation_store
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_checkpoint_early_return_shapes import _admit_original_retry, _complete_original
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("opponent", ("ledger", "early_input"))
async def test_acquired_early_retry_still_reproves_late_child_corruption_before_second_sdk(
    operation_store, tmp_path, monkeypatch, opponent
):
    from elspeth.web.composer import continuation_checkpoint_early_physical as physical_boundary
    from elspeth.web.sessions import continuation_checkpoint_early_retry as retry_sql

    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, primary_sdk):
        sdk = []

        async def malformed_first(**_request):
            sdk.append(1)
            return _reply(None)

        monkeypatch.setattr(litellm, "acompletion", malformed_first)
        async with _early_attempt(owner, selected) as first:
            with pytest.raises(_MalformedLLMResponseError):
                await _invoke_early(first)
            completed, _returned, _projected = await _complete_original(first)
            assert completed.retry is not None
        assert len(sdk) == 1 and len(primary_sdk) == 1
        selected = await _admit_original_retry(owner, running, completed.completed)
        sessions = owner.service._sessions_service
        original_prepare = sessions.prepare_v4_checkpoint_source_sync
        original_retry = retry_sql.read_early_format_retry_on_connection
        preparation_reads = []
        reached = []
        preparing = False
        engine = operation_store[0]
        sid, operation = str(running.claim.session_id), running.claim.operation_id

        def read_retry(conn, **kwargs):
            if preparing:
                assert conn.in_transaction()
                preparation_reads.append(kwargs["selected"])
            return original_retry(conn, **kwargs)

        async with _early_attempt(owner, selected) as source:
            acquired = physical_boundary.checkpoint_early_format_retry(source)
            assert acquired is not None and acquired is source.phase_owner.phase_source.early_retry
            assert acquired.selected == completed.completed

            def corrupt_before_prepare(*, prepared):
                nonlocal preparing
                assert not reached and prepared.selected is selected
                assert physical_boundary.checkpoint_early_format_retry(source) is acquired
                before = stored_rows(engine)
                with engine.begin() as conn:
                    assert load_snapshot(conn, session_id=sid, operation_id=operation) == acquired.selected
                    if opponent == "ledger":
                        attempt = conn.execute(select(physical).where(physical.c.attempt_id == acquired.child.owed.attempt_id)).one()
                        assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
                        row = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).one()
                        changed = conn.execute(
                            update(ledger).where(ledger.c.entry_id == row.entry_id).values(prompt_tokens=row.prompt_tokens + 1)
                        )
                    else:
                        reference = acquired.child.owed.source.early_input_ref
                        assert reference is not None
                        key = f"value:{reference.codec}:{reference.digest}"
                        row = conn.execute(
                            select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
                        ).one()
                        changed = conn.execute(
                            update(chunks)
                            .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
                            .values(payload=b"{}", byte_length=2, sha256=sha256(b"{}").hexdigest())
                        )
                    assert changed.rowcount == 1
                    assert load_snapshot(conn, session_id=sid, operation_id=operation) == acquired.selected
                reached.append(opponent)
                preparing = True
                try:
                    return original_prepare(prepared=prepared)
                finally:
                    preparing = False
                    with engine.begin() as conn:
                        if opponent == "ledger":
                            restored = conn.execute(
                                update(ledger).where(ledger.c.entry_id == row.entry_id).values(prompt_tokens=row.prompt_tokens)
                            )
                        else:
                            restored = conn.execute(
                                update(chunks)
                                .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == row.chunk_key)
                                .values(payload=row.payload, byte_length=row.byte_length, sha256=row.sha256)
                            )
                        assert restored.rowcount == 1
                    assert stored_rows(engine) == before

            with monkeypatch.context() as corrupt:
                corrupt.setattr(sessions, "prepare_v4_checkpoint_source_sync", corrupt_before_prepare)
                corrupt.setattr(retry_sql, "read_early_format_retry_on_connection", read_retry)
                with pytest.raises(AuditIntegrityError):
                    await _invoke_early(source)
            assert reached == [opponent]
            assert acquired.selected in preparation_reads
            assert physical_boundary.checkpoint_early_format_retry(source) is acquired
        assert len(sdk) == 1 and len(primary_sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", (False, True))
async def test_actual_early_prepared_source_failure_proves_input_and_original_retry(operation_store, tmp_path, monkeypatch, retry):
    from elspeth.web.composer import continuation_checkpoint_end_detached as detached

    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, primary_sdk):
        sdk = []

        async def malformed_first(**_request):
            sdk.append(1)
            return _reply(None)

        monkeypatch.setattr(litellm, "acompletion", malformed_first)
        if retry:
            async with _early_attempt(owner, selected) as first:
                with pytest.raises(_MalformedLLMResponseError):
                    await _invoke_early(first)
                completed, _returned, _projected = await _complete_original(first)
                assert completed.retry is not None
            selected = await _admit_original_retry(owner, running, completed.completed)
        reached = []
        failure = TimeoutError("controlled original prepared-source restore failure")

        async def fail_actual_prepared_restore(*, phase_owner, prepared, capture_data):
            assert phase_owner.phases is owner and phase_owner.runtime is owner.runtime
            assert prepared.source.checkpoint_id == selected.checkpoint_id
            assert prepared.prepared_selected.cursor == selected.stack.cursor + 1
            assert capture_data.captured.selected is selected
            reached.append(prepared)
            raise failure

        monkeypatch.setattr(detached, "restore_captured_checkpoint_runtime", fail_actual_prepared_restore)
        async with _early_attempt(owner, selected) as source:
            with pytest.raises(TimeoutError) as caught:
                await _invoke_early(source)
            assert caught.value is failure and len(reached) == 1
        assert len(primary_sdk) == 1 and len(sdk) == int(retry)
        assert owner.runtime is not None
        engine, names = operation_store[0], owner.runtime.env_ref_names
        with engine.connect() as conn:
            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            current = SelectedContinuationState(
                stored.checkpoint_id,
                decode_continuation_snapshot(stored.payload_json),
                load_snapshot_private_data_on_connection(
                    conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, selected=stored
                ),
            )
            (outer,) = validate_continuation_structure_on_connection(conn, running, current, env_ref_names=names)
        assert stored == reached[0].prepared_selected
        for opponent in ("prepared_event", "input_bytes"):
            before = stored_rows(engine)
            with engine.begin() as conn:
                transaction = conn.begin_nested()
                if opponent == "prepared_event":
                    key = conn.execute(
                        select(events.c.payload_chunk_key).where(
                            events.c.session_id == str(running.claim.session_id),
                            events.c.operation_id == running.claim.operation_id,
                            events.c.sequence == stored.cursor,
                        )
                    ).scalar_one()
                else:
                    reference = reached[0].early_input_ref
                    assert reference is not None
                    key = f"value:{reference.codec}:{reference.digest}"
                if opponent == "prepared_event":
                    payload = conn.execute(
                        select(chunks.c.payload).where(
                            chunks.c.session_id == str(running.claim.session_id),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == key,
                        )
                    ).scalar_one()
                    event = json.loads(payload)
                    event["creator_generation"] += 1
                    changed = canonical_json(event).encode("utf-8")
                    refusal = "checkpoint preparation changed its exact source event"
                else:
                    changed = b"{}"
                    refusal = "EARLY input changed its bounded immutable reference"
                conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(running.claim.session_id),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == key,
                    )
                    .values(payload=changed, byte_length=len(changed), sha256=sha256(changed).hexdigest())
                )
                with pytest.raises(AuditIntegrityError, match=refusal):
                    validate_continuation_structure_on_connection(conn, running, current, env_ref_names=names)
                transaction.rollback()
                assert validate_continuation_structure_on_connection(conn, running, current, env_ref_names=names) == (outer,)
            assert stored_rows(engine) == before
        error = project_composer_operation_error(failure, request_id=None)
        terminal = owner.authority.settle_continuation_owner(running, outcome=error, env_ref_names=names)
        assert terminal.status == "failed" and terminal.failure_code == error.failure_code
        assert terminal.result_json is not None and json.loads(terminal.result_json) == error.model_dump(mode="json")
        assert len(primary_sdk) == 1 and len(sdk) == int(retry)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("request_bytes", "preparation_timeout", "stop"))
async def test_original_unpaid_early_preserves_failure_and_marks_only_outer_unknown(operation_store, tmp_path, monkeypatch, failure):
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, sdk):
        assert owner.runtime is not None and len(sdk) == 1
        names = owner.runtime.env_ref_names
        engine = operation_store[0]
        with engine.connect() as conn:
            (outer,) = validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=names)
            paid_before = tuple(conn.execute(select(physical)).all())
            usage_before = tuple(conn.execute(select(ledger)).all())
        assert outer.startswith("advance-early:")
        original = ComposerContextUnsupported("prepared_request_bytes") if failure != "preparation_timeout" else TimeoutError()
        error = project_composer_operation_error(original, request_id=None)
        if failure == "stop":
            owner.authority.request_cancel(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        # A failure after the real terminal append must roll back, not consume
        # the predecessor or manufacture a second physical call on retry.
        before = stored_rows(engine)
        actual_append = continuation_store.append_complete_snapshot
        reached = []

        def append_then_fail(*args, **kwargs):
            result = actual_append(*args, **kwargs)
            reached.append(result)
            raise RuntimeError("controlled failure after actual terminal append")

        with monkeypatch.context() as patch:
            patch.setattr(continuation_store, "append_complete_snapshot", append_then_fail)
            with pytest.raises(RuntimeError, match="controlled failure after actual terminal append"):
                owner.authority.settle_continuation_owner(running, outcome=error, env_ref_names=names)
        assert len(reached) == 1 and stored_rows(engine) == before
        terminal = owner.authority.settle_continuation_owner(running, outcome=error, env_ref_names=names)
        assert terminal.status == "failed" and terminal.result_json is not None
        if failure == "stop":
            assert terminal.failure_code == "request_cancelled"
        else:
            assert terminal.failure_code == error.failure_code
            assert json.loads(terminal.result_json) == error.model_dump(mode="json")
        if failure == "request_bytes":
            assert error.http_status == 422 and error.error_type == "composer_context_unsupported"
        with engine.connect() as conn:
            current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            root = decode_continuation_snapshot(current.payload_json)
            assert tuple(item.effect_id for item in root.effect_frontier if item.disposition == "unknown") == (outer,)
            appended = conn.execute(
                select(events)
                .where(events.c.session_id == str(running.claim.session_id), events.c.operation_id == running.claim.operation_id)
                .order_by(events.c.sequence)
            ).all()
            assert [row.tag for row in appended[-2:]] == ["effect_unknown", "settled"]
            assert tuple(conn.execute(select(physical)).all()) == paid_before
            assert tuple(conn.execute(select(ledger)).all()) == usage_before
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_unpaid_early_refuses_changed_outer_physical_hold_ledger_and_lease(operation_store, tmp_path, monkeypatch):
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, sdk):
        assert owner.runtime is not None and len(sdk) == 1
        engine, names = operation_store[0], owner.runtime.env_ref_names
        with engine.connect() as conn:
            (outer,) = validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=names)
            attempt = conn.execute(select(physical)).one()
            held = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt.attempt_id)).one()
        for opponent in ("outer_input", "orphan_physical", "unsettled", "active_h", "ledger", "lease"):
            before = stored_rows(engine)
            with engine.begin() as conn:
                transaction = conn.begin_nested()
                if opponent == "outer_input":
                    conn.execute(update(effects).where(effects.c.effect_key == outer).values(input_sha256="0" * 64))
                elif opponent == "orphan_physical":
                    values = dict(attempt._mapping)
                    values.update(attempt_id=str(uuid4()), settled_at=None, ledger_entry_id=None)
                    conn.execute(insert(physical).values(**values))
                elif opponent == "unsettled":
                    conn.execute(
                        update(physical).where(physical.c.attempt_id == attempt.attempt_id).values(settled_at=None, ledger_entry_id=None)
                    )
                elif opponent == "active_h":
                    conn.execute(update(holds).where(holds.c.effect_key == held.effect_key).values(state="active", consumed_at=None))
                elif opponent == "ledger":
                    conn.execute(
                        update(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id).values(prompt_tokens=ledger.c.prompt_tokens + 1)
                    )
                else:
                    conn.execute(
                        update(fences)
                        .where(fences.c.session_id == str(running.claim.session_id))
                        .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                    )
                with pytest.raises(ComposerOperationFenceLost if opponent == "lease" else AuditIntegrityError):
                    validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=names)
                transaction.rollback()
                assert validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=names) == (outer,)
            assert stored_rows(engine) == before
        error = project_composer_operation_error(ComposerContextUnsupported("prepared_request_bytes"), request_id=None)
        terminal = owner.authority.settle_continuation_owner(running, outcome=error, env_ref_names=names)
        assert terminal.failure_code == error.failure_code and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True), ids=("original-timeout", "stop"))
async def test_supported_review_end_prepared_failure_preserves_original_and_rejects_changed_source(tmp_path, monkeypatch, stop):
    from elspeth.web.composer import continuation_checkpoint_end_detached as detached
    from elspeth.web.composer.continuation import EndFrame
    from elspeth.web.composer.continuation_next_action import unpaid_checkpoint_outer_on_connection
    from elspeth.web.sessions import continuation_driver as driver_module
    from tests.helpers.composer_operations import message_body
    from tests.helpers.continuation_app import (
        ACTOR,
        ScriptedSDK,
        continuation_app,
        install_sdk,
        observe_supported_path,
        semantic_primary_response,
        semantic_review_response,
        submit_and_settle,
        tool_call,
    )

    operation_id = str(uuid4())
    failure = TimeoutError("controlled original unpaid END preparation failure")
    original_restore = detached.restore_captured_checkpoint_runtime
    original_project = driver_module.project_composer_operation_error
    reached, projected, rejected = [], [], []

    def project_original(original, **arguments):
        result = original_project(original, **arguments)
        if original is failure:
            projected.append(result)
        return result

    monkeypatch.setattr(driver_module, "project_composer_operation_error", project_original)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        engine = harness.app.state.session_engine
        policy = harness.app.state.composer_async_operation_authority._continuation_policy
        assert policy is not None and policy.actions.advisor.preparation == 5.0
        sid = str(harness.session_id)

        def rows(conn):
            return tuple(
                tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
                for table in (chunks, events, effects, physical, ledger, holds)
            )

        async def restore_then_fail(*, phase_owner, prepared, capture_data):
            restored = await original_restore(phase_owner=phase_owner, prepared=prepared, capture_data=capture_data)
            running = phase_owner.phases.work.running
            if running is None or running.claim.operation_id != operation_id or prepared.phase != "end":
                return restored
            assert not reached
            assert prepared.source.checkpoint_id == phase_owner.selected.checkpoint_id
            assert prepared.prepared_selected.cursor == prepared.source.cursor + 1
            assert prepared.end_input_ref is not None
            names = phase_owner.inventory.names
            with engine.connect() as conn:
                stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
                assert stored == prepared.prepared_selected
                root = decode_continuation_snapshot(stored.payload_json)
                assert type(root.frames[-1]) is EndFrame and root.frames[-1].phase == "review_needed"
                assert root.active_action_json is not None and json.loads(root.active_action_json)["kind"] == "review"
                current = SelectedContinuationState(
                    stored.checkpoint_id,
                    root,
                    load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation_id, selected=stored),
                )
                (outer,) = validate_continuation_structure_on_connection(conn, running, current, env_ref_names=names)
                assert outer == prepared.outer_effect_key
                event_key = conn.execute(
                    select(events.c.payload_chunk_key).where(
                        events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence == stored.cursor
                    )
                ).scalar_one()
                event_bytes = conn.execute(
                    select(chunks.c.payload).where(
                        chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == event_key
                    )
                ).scalar_one()
                source_event = json.loads(event_bytes)
                assert source_event["owner"] == "composer_checkpoint_source" and source_event["phase"] == "end"
                assert source_event["stage"] == "source_prepared" and source_event["kind"] == "end_review"
                assert source_event["source_checkpoint_id"] == prepared.source.checkpoint_id
                paid_before = tuple(conn.execute(select(physical).where(physical.c.session_id == sid)).all())
                usage_before = tuple(conn.execute(select(ledger).where(ledger.c.session_id == sid)).all())
                holds_before = tuple(
                    conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation_id)).all()
                )
                prior_effects = tuple(
                    conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id)).all()
                )
            assert sdk.roles == ["primary", "semantic", "primary", "semantic"] and not sdk.steps
            assert len(paid_before) == len(usage_before) == len(holds_before) == 4
            assert all(item.settled_at is not None and item.ledger_entry_id is not None for item in paid_before)
            assert all(item.state == "consumed" for item in holds_before)
            assert {item.attempt_id for item in paid_before} == {item.provider_attempt_id for item in holds_before}
            assert all(
                any(item.provider_attempt_id == attempt.attempt_id and item.disposition == "committed" for item in prior_effects)
                for attempt in paid_before
            )
            if not stop:
                for opponent in ("source_checkpoint", "end_input", "paid_h"):
                    with engine.begin() as conn:
                        before = rows(conn)
                        savepoint = conn.begin_nested()
                        if opponent == "paid_h":
                            actual_hold = holds_before[-1]
                            changed = conn.execute(
                                update(holds)
                                .where(
                                    holds.c.session_id == sid,
                                    holds.c.operation_id == operation_id,
                                    holds.c.effect_key == actual_hold.effect_key,
                                    holds.c.provider_attempt_id == actual_hold.provider_attempt_id,
                                )
                                .values(state="active", consumed_at=None)
                            )
                        else:
                            if opponent == "source_checkpoint":
                                changed_event = dict(source_event)
                                changed_event["source_checkpoint_id"] = stored.checkpoint_id
                                payload = canonical_json(changed_event).encode("utf-8")
                                key = event_key
                            else:
                                reference = prepared.end_input_ref
                                key = f"value:{reference.codec}:{reference.digest}"
                                payload = b"{}"
                            changed = conn.execute(
                                update(chunks)
                                .where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
                                .values(payload=payload, byte_length=len(payload), sha256=sha256(payload).hexdigest())
                            )
                        assert changed.rowcount == 1
                        changed_rows = rows(conn)
                        with pytest.raises(AuditIntegrityError):
                            unpaid_checkpoint_outer_on_connection(
                                conn,
                                session_id=harness.session_id,
                                operation_id=operation_id,
                                sequence=running.claim.segment_sequence,
                                generation=running.claim.ownership_generation,
                                selected=current,
                                env_ref_names=names,
                            )
                        assert rows(conn) == changed_rows
                        savepoint.rollback()
                        assert rows(conn) == before
                        assert validate_continuation_structure_on_connection(conn, running, current, env_ref_names=names) == (outer,)
                    rejected.append(opponent)
            reached.append((prepared, paid_before, usage_before, holds_before, outer))
            if stop:
                cancelled = harness.app.state.composer_async_operation_authority.request_owner_cancel(
                    session_id=harness.session_id, operation_id=operation_id, actor_user_id=ACTOR, auth_provider_type="local"
                )
                assert cancelled is not None and cancelled.cancel_requested_at is not None
            raise failure

        monkeypatch.setattr(detached, "restore_captured_checkpoint_runtime", restore_then_fail)
        sdk = ScriptedSDK(
            [
                (
                    "primary",
                    lambda app: ("Inspecting the current pipeline.", [tool_call(app, "unpaid_end_read", "get_pipeline_state", {})]),
                ),
                ("semantic", semantic_primary_response),
                ("primary", ("The inspected pipeline is ready for its original END review.", None)),
                ("semantic", semantic_review_response),
            ],
            app_ref=[harness.app],
        )
        install_sdk(monkeypatch, sdk)
        artifact = tmp_path / "unpaid-end-prepared-driver.json"
        with observe_supported_path(monkeypatch, operation_id=operation_id, session_id=harness.session_id, artifact=artifact) as observed:
            terminal = await submit_and_settle(
                harness, message_body("Inspect the pipeline and explain its current state.", operation_id=operation_id), rounds=720
            )
            observed.require_driver_stages()
            assert len(reached) == 1, f"Original Review/END preparation was not reached; safe metadata: {artifact}"
            assert terminal.status == "failed" and terminal.error is not None and terminal.result is None
            if stop:
                assert terminal.error.failure_code == "request_cancelled" and terminal.error.http_status == 499
                assert not projected and not rejected
            else:
                assert len(projected) == 1 and terminal.error == projected[0]
                assert terminal.error.failure_code == "operation_failed" and terminal.error.http_status == 500
                assert rejected == ["source_checkpoint", "end_input", "paid_h"]
            prepared, paid_before, usage_before, holds_before, outer = reached[0]
            with engine.connect() as conn:
                current = load_snapshot(conn, session_id=sid, operation_id=operation_id)
                root = decode_continuation_snapshot(current.payload_json)
                assert tuple(item.effect_id for item in root.effect_frontier if item.disposition == "unknown") == (outer,)
                appended = conn.execute(
                    select(events).where(events.c.session_id == sid, events.c.operation_id == operation_id).order_by(events.c.sequence)
                ).all()
                assert [item.tag for item in appended[-2:]] == ["effect_unknown", "settled"]
                assert current.cursor == prepared.prepared_selected.cursor + 2
                assert tuple(conn.execute(select(physical).where(physical.c.session_id == sid)).all()) == paid_before
                assert tuple(conn.execute(select(ledger).where(ledger.c.session_id == sid)).all()) == usage_before
                assert (
                    tuple(conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation_id)).all())
                    == holds_before
                )
            assert sdk.roles == ["primary", "semantic", "primary", "semantic"] and not sdk.steps
