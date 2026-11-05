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

        async def fail_actual_prepared_restore(*, phase_owner, prepared, captured):
            assert phase_owner.phases is owner and phase_owner.runtime is owner.runtime
            assert prepared.source.checkpoint_id == selected.checkpoint_id
            assert prepared.prepared_selected.cursor == selected.stack.cursor + 1
            assert captured.selected is selected
            reached.append(prepared)
            raise failure

        monkeypatch.setattr(detached, "restore_prepared_end_runtime", fail_actual_prepared_restore)
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
