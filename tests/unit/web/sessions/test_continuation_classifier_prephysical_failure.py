"""Actual classifier preparation failure custody and atomic terminal controls."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from time import monotonic
from uuid import uuid4
from weakref import ref

import litellm
import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_semantic_advisor_return import observe_continuation_iteration_classification
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.required_work import RequiredWorkIncomplete, RequiredWorkSource
from elspeth.web.sessions import continuation_classifier_prephysical_failure as owner
from elspeth.web.sessions.continuation_classifier_primary import _stage_key
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import _v4_classifier_case
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import operation_store as _operation_store

operation_store = _operation_store


@asynccontextmanager
async def _original_prephysical_case(store, tmp_path, monkeypatch):
    original_input = phase_fixture.initialized_primary_input

    async def capped(current_store, current_path, current_patch, *, settings_overrides=None):
        return await original_input(
            current_store,
            current_path,
            current_patch,
            settings_overrides={**(settings_overrides or {}), "composer_max_composition_turns": 1},
        )

    monkeypatch.setattr(phase_fixture, "initialized_primary_input", capped)
    async with _v4_classifier_case(store, tmp_path, monkeypatch) as (v4_store, case):
        phases, running, _runtime, _unused_issuer, _prepared_input, prior_sdk = case
        assert phases.runtime is not None and phases.action_clock is not None
        await phases.restore_finished_batch(phases.runtime.source.selected)
        runtime = phases.runtime
        assert runtime is not None and phases.finished_batch is not None
        assert phases.service._max_composition_turns == 1 and len(prior_sdk) == 1
        dispatch, persist, _prestate = phases.finished_batch
        physical_entries = []

        async def forbidden_sdk(**kwargs):
            physical_entries.append(kwargs)
            raise AssertionError("prephysical failure entered another SDK")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        # Spend the actual existing preparation allowance; do not alter a
        # clock, forge a failure, or invent a physical/no-submission witness.
        await asyncio.sleep(max(0.0, phases.action_clock.preparation_deadline_monotonic - monotonic()) + 0.001)
        try:
            await observe_continuation_iteration_classification(
                phases, runtime, selected=runtime.source.selected, dispatch=dispatch, persist=persist
            )
        except ComposerConvergenceError as original:
            marker = owner.original_classifier_prephysical_failure(phases, original)
            assert type(marker) is owner.ClassifierPrephysicalFailure
            assert type(original.__context__) is TimeoutError and not physical_entries
            assert phases.work.coordinator.sql_deadline_owner is not None
            assert phases.work.coordinator.sql_deadline_owner.remaining_action(monotonic_now=monotonic()) > 0
            yield v4_store, phases, running, original, marker
            assert not physical_entries and len(prior_sdk) == 1
        else:
            raise AssertionError("original classifier preparation did not expire")


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for member in error.exceptions for leaf in _leaves(member))
    return (error,)


def _cold(conn, running):
    return owner.read_classifier_prephysical_terminal_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id
    )


@pytest.mark.asyncio
async def test_actual_preparation_failure_commits_once_and_cold_reproves_all_original_semantics(operation_store, tmp_path, monkeypatch):
    async with _original_prephysical_case(operation_store, tmp_path, monkeypatch) as (store, phases, running, original, marker):
        sid, oid = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            old_attempts = tuple(conn.execute(select(physical).where(physical.c.session_id == sid)))
            old_holds = tuple(conn.execute(select(holds).where(holds.c.session_id == sid)))
            old_states = tuple(conn.execute(select(states).where(states.c.session_id == sid)))
            paid = tuple(conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.owner != "composer_iteration")))
        committed = await owner.fail_classifier_prephysical_operation(store[3], running, work=phases.work, phases=phases, custody=marker)
        assert committed.original is original and not committed.original_errors and not committed.deferred_cancellations
        assert committed.terminal.status == "failed" and committed.terminal.failure_code == "http_error"
        result = json.loads(committed.terminal.result_json)
        detail = result["body"]["detail"]
        assert result["http_status"] == 422 and result["error_type"] == detail["error_type"] == "convergence"
        assert detail["budget_exhausted"] == "timeout" and detail["turns_used"] == 1
        assert "timeout_seconds" not in detail and detail["partial_state"]["metadata"]["name"] == "V4 original model metadata"
        with store[0].connect() as conn:
            assert _cold(conn, running) == committed.verified
            assert tuple(conn.execute(select(physical).where(physical.c.session_id == sid))) == old_attempts
            assert tuple(conn.execute(select(holds).where(holds.c.session_id == sid))) == old_holds
            assert tuple(conn.execute(select(states).where(states.c.session_id == sid))) == old_states
            assert tuple(conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.owner != "composer_iteration"))) == paid
            assert tuple(
                conn.execute(
                    select(events.c.tag)
                    .where(
                        events.c.session_id == sid,
                        events.c.operation_id == oid,
                        events.c.sequence > committed.verified.original_source.cursor,
                    )
                    .order_by(events.c.sequence)
                ).scalars()
            ) == ("effect_unknown", "settled")
            row = conn.execute(
                select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == owner._key(oid))
            ).one()
            descriptor = owner._Descriptor.model_validate_json(row.payload)
            private = conn.execute(select(messages).where(messages.c.id == str(descriptor.message_id))).one()
            assert private.role == "audit"

        # Each semantic opponent rewrites its own chunk digest/length too,
        # ensuring the cold source/private proof itself detects the mutation.
        mutations = (
            ("private_facts", descriptor.private_facts + " "),
            ("max_turns", descriptor.max_turns + 1),
            ("initial_version", descriptor.initial_version + 1),
            ("state_json", canonical_json({**json.loads(descriptor.state_json), "version": 999})),
            ("clock_policy_json", "{}"),
            ("clock_started", descriptor.clock_started + 1.0),
        )
        with store[0].begin() as conn:
            for column in ("sha256", "byte_length"):
                savepoint = conn.begin_nested()
                try:
                    value = "0" * 64 if column == "sha256" else row.byte_length + 1
                    conn.execute(
                        update(chunks)
                        .where(chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == row.chunk_key)
                        .values(**{column: value})
                    )
                    with pytest.raises(AuditIntegrityError):
                        _cold(conn, running)
                finally:
                    savepoint.rollback()
            for field, value in mutations:
                savepoint = conn.begin_nested()
                try:
                    document = descriptor.model_copy(update={field: value})
                    payload = canonical_json(document.model_dump(mode="json")).encode()
                    conn.execute(
                        update(chunks)
                        .where(chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == row.chunk_key)
                        .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                    )
                    with pytest.raises(AuditIntegrityError):
                        _cold(conn, running)
                finally:
                    savepoint.rollback()
            savepoint = conn.begin_nested()
            try:
                conn.execute(
                    update(effects)
                    .where(effects.c.session_id == sid, effects.c.operation_id == oid, effects.c.effect_key == descriptor.effect_key)
                    .values(result_chunk_key=row.chunk_key)
                )
                with pytest.raises(AuditIntegrityError, match="logical effect shape"):
                    _cold(conn, running)
            finally:
                savepoint.rollback()
            savepoint = conn.begin_nested()
            try:
                conn.execute(update(messages).where(messages.c.id == str(descriptor.message_id)).values(content="changed private failure"))
                with pytest.raises(AuditIntegrityError, match="private cohort"):
                    _cold(conn, running)
            finally:
                savepoint.rollback()
            owner._require_candidate(_cold(conn, running), descriptor)
            with pytest.raises(AuditIntegrityError, match="prepared terminal candidate"):
                owner._require_candidate(committed.verified, descriptor.model_copy(update={"message_id": uuid4()}))
        # Cold DATA is independent of the original process-local permit.
        marker_ref = ref(marker)
        owner._NATIVE.pop(marker)
        phases.classifier_prephysical_failure = None
        del marker
        with store[0].connect() as conn:
            assert _cold(conn, running) == committed.verified
        phases.work.coordinator.assert_completed()
    # The original must stay strongly retained through the physical close.
    # Only after that scope ends may its traceback release the generator's
    # original marker local; the registry itself must not retain that cycle.
    original.__traceback__ = None
    if original.__context__ is not None:
        original.__context__.__traceback__ = None
    gc.collect()
    assert marker_ref() is None


@pytest.mark.asyncio
async def test_original_custody_refuses_foreign_clock_and_unjoined_actual_sql(operation_store, tmp_path, monkeypatch):
    async with _original_prephysical_case(operation_store, tmp_path, monkeypatch) as (store, phases, running, original, marker):
        for foreign in (owner.ClassifierPrephysicalFailure(), replace(marker)):
            with pytest.raises(AuditIntegrityError, match="issued custody"):
                await owner.fail_classifier_prephysical_operation(store[3], running, work=phases.work, phases=phases, custody=foreign)
        other = ComposerConvergenceError.capture(
            max_turns=original.max_turns,
            budget_exhausted="timeout",
            state=phases.runtime.live.state,
            initial_version=phases.runtime.live.initial_version,
        )
        assert owner.original_classifier_prephysical_failure(phases, other) is None
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        sql, projection = phases.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
        )

        def physical_read():
            loop.call_soon_threadsafe(entered.set)
            release.wait(30)
            return None

        task = asyncio.create_task(run_required_sql_finish_once(sql, physical_read))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(RequiredWorkIncomplete):
                await owner.fail_classifier_prephysical_operation(store[3], running, work=phases.work, phases=phases, custody=marker)
        finally:
            release.set()
            actual = await task
            phases.work.coordinator.validate_continuation_sql_outcome(
                ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=actual
            )
            projection.begin_projection()
            projection.complete_owned()
        clock = phases.work.coordinator.sql_deadline_owner
        clock.begin_action(clock.policy.actions.session_sql, started_at=monotonic())
        with pytest.raises(AuditIntegrityError, match="active action clock"):
            await owner.fail_classifier_prephysical_operation(store[3], running, work=phases.work, phases=phases, custody=marker)
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_original_prephysical_source_refuses_each_orphan_without_publishing(operation_store, tmp_path, monkeypatch):
    async with _original_prephysical_case(operation_store, tmp_path, monkeypatch) as (store, phases, running, _original, _marker):
        sid, oid = str(running.claim.session_id), running.claim.operation_id
        checkpoint = phases.runtime.source.selected.checkpoint_id
        with store[0].begin() as conn:
            source = owner.read_classifier_prephysical_source_on_connection(
                conn, session_id=running.claim.session_id, operation_id=oid, checkpoint_id=checkpoint
            )
            prior = conn.execute(select(physical).where(physical.c.session_id == sid)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == oid)).one()
            for kind in ("anchor", "source", "hold", "physical", "other_history_physical", "unresolved"):
                savepoint = conn.begin_nested()
                try:
                    if kind in ("anchor", "source"):
                        key = _stage_key(source.effect_key) if kind == "anchor" else "classifier-primary-source:provider:0:999"
                        payload = b"{}"
                        conn.execute(
                            insert(chunks).values(
                                session_id=sid,
                                operation_id=oid,
                                chunk_key=key,
                                codec="composer.classifier-primary-stage.v1"
                                if kind == "anchor"
                                else "composer.classifier-primary-source.v1",
                                payload=payload,
                                sha256=sha256(payload).hexdigest(),
                                byte_length=len(payload),
                            )
                        )
                    elif kind == "hold":
                        values = dict(hold._mapping)
                        values["effect_key"] = "provider:0:999"
                        conn.execute(insert(holds).values(**values))
                    elif kind in ("physical", "other_history_physical"):
                        values = dict(prior._mapping)
                        values.update(attempt_id=str(uuid4()), settled_at=None, ledger_entry_id=None)
                        if kind == "other_history_physical":
                            history = conn.execute(
                                select(attempts).where(
                                    attempts.c.session_id == sid,
                                    attempts.c.operation_id == oid,
                                    attempts.c.generation == running.claim.ownership_generation,
                                )
                            ).one()
                            creator = dict(history._mapping)
                            creator.update(generation=running.claim.ownership_generation + 1, session_operation_id=str(uuid4()))
                            conn.execute(insert(attempts).values(**creator))
                            values["operation_id"] = creator["session_operation_id"]
                        conn.execute(insert(physical).values(**values))
                    else:
                        logical = conn.execute(
                            select(effects).where(
                                effects.c.session_id == sid, effects.c.operation_id == oid, effects.c.effect_key == source.effect_key
                            )
                        ).one()
                        values = dict(logical._mapping)
                        values["effect_key"] = "unexpected-advance"
                        conn.execute(insert(effects).values(**values))
                    with pytest.raises(AuditIntegrityError):
                        owner.read_classifier_prephysical_source_on_connection(
                            conn, session_id=running.claim.session_id, operation_id=oid, checkpoint_id=checkpoint
                        )
                finally:
                    savepoint.rollback()
            assert _cold(conn, running) is None
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_rolled_back_writer_retains_actual_readback_error_and_deferred_cancellation(operation_store, tmp_path, monkeypatch):
    async with _original_prephysical_case(operation_store, tmp_path, monkeypatch) as (store, phases, running, original, marker):
        write_failure = AuditIntegrityError("actual classifier private cohort failure")
        original_cohort = store[3]._write_audit_cohort_on_connection
        original_run = owner.run_required_sql_finish_once
        read_errors, outcomes = [], []
        loop, task = asyncio.get_running_loop(), asyncio.current_task()

        def failed_cohort(*args, **kwargs):
            original_cohort(*args, **kwargs)
            loop.call_soon_threadsafe(task.cancel, "actual writer delivery")
            raise write_failure

        async def observed_run(ticket, operation):
            if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL:
                actual_operation = operation

                def operation():
                    # Real SQLite SQL error under the actual registered read.
                    try:
                        with store[0].connect() as conn:
                            conn.execute(text("SELECT * FROM classifier_missing_readback_table"))
                    except OperationalError as error:
                        read_errors.append(error)
                        raise
                    return actual_operation()

            result = await original_run(ticket, operation)
            outcomes.append(result)
            return result

        monkeypatch.setattr(store[3], "_write_audit_cohort_on_connection", failed_cohort)
        monkeypatch.setattr(owner, "run_required_sql_finish_once", observed_run)
        with pytest.raises(BaseExceptionGroup) as caught:
            await owner.fail_classifier_prephysical_operation(store[3], running, work=phases.work, phases=phases, custody=marker)
        leaves = _leaves(caught.value)
        assert original in leaves and write_failure in leaves and len(read_errors) == 1 and read_errors[0] in leaves
        deferred = tuple(item for outcome in outcomes for item in outcome.deferred_cancellations)
        assert deferred and all(any(item is leaf for leaf in leaves) for item in deferred)
        assert len({id(item) for item in leaves}) == len(leaves)
        with store[0].connect() as conn:
            assert _cold(conn, running) is None
        phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_commit_then_raised_delivery_uses_exact_cold_candidate_without_second_write(operation_store, tmp_path, monkeypatch):
    async with _original_prephysical_case(operation_store, tmp_path, monkeypatch) as (store, phases, running, original, marker):
        lost_delivery = OSError("actual postcommit classifier delivery")
        original_run = owner.run_required_sql_finish_once
        writes = []

        async def after_commit(ticket, operation):
            if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL:
                actual_operation = operation

                def operation():
                    result = actual_operation()
                    writes.append(result)
                    raise lost_delivery

            return await original_run(ticket, operation)

        monkeypatch.setattr(owner, "run_required_sql_finish_once", after_commit)
        committed = await owner.fail_classifier_prephysical_operation(store[3], running, work=phases.work, phases=phases, custody=marker)
        assert len(writes) == 1 and committed.original is original and committed.original_errors == (lost_delivery,)
        with store[0].connect() as conn:
            assert _cold(conn, running) == committed.verified
        phases.work.coordinator.assert_completed()
