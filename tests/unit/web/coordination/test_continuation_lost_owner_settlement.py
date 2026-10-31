"""Real SQL liveness, Stop, archived claims and retained paid-output controls."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.unit.web.coordination.test_composer_continuation_claim_start import queued
from tests.unit.web.coordination.test_composer_operation_authority import _admission_rows
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_recovery_candidates import _started

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_recovery_candidates import list_inactive_continuations, list_recoverable_segments
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table, token_usage_ledger_table

operation_store = _operation_store


def reconcile(repository, candidate):
    return repository.reconcile_unavailable_composer_segment(
        candidate, owner_instance_id="recovery-peer", lease_seconds=30, auth_provider_type="local"
    )


def assert_progressed_candidate(engine, candidate):
    with engine.connect() as conn:
        parent = conn.execute(
            select(jobs).where(
                jobs.c.session_id == str(candidate.session_id),
                jobs.c.operation_id == candidate.operation_id,
            )
        ).one()
        segment = conn.execute(
            select(segments).where(
                segments.c.session_id == str(candidate.session_id),
                segments.c.operation_id == candidate.operation_id,
                segments.c.sequence == candidate.segment_sequence,
            )
        ).one()
        entry_cursor = conn.execute(
            select(checkpoints.c.cursor).where(
                checkpoints.c.session_id == str(candidate.session_id),
                checkpoints.c.operation_id == candidate.operation_id,
                checkpoints.c.checkpoint_id == segment.checkpoint_id,
            )
        ).scalar_one()
        head_cursor = conn.execute(
            select(checkpoints.c.cursor).where(
                checkpoints.c.session_id == str(candidate.session_id),
                checkpoints.c.operation_id == candidate.operation_id,
                checkpoints.c.checkpoint_id == parent.checkpoint_id,
            )
        ).scalar_one()
    assert candidate.checkpoint_id == parent.checkpoint_id
    assert candidate.segment_checkpoint_id == segment.checkpoint_id
    assert candidate.checkpoint_id != candidate.segment_checkpoint_id
    assert entry_cursor < head_cursor == parent.journal_cursor


async def failed_initialized_primary(store, tmp_path, monkeypatch):
    import litellm
    from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted
    from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

    from elspeth.web.composer.continuation_primary import run_continuation_primary
    from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning

    runtime, work, requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    running = work.running
    assert type(running) is ComposerOperationRunning
    assert runtime.source.selected.stack.semantic_frontier is not None
    sdk = []
    original = ValueError("original initialized PRIMARY physical failure")

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            store[0],
            running.session_operation_context,
            requests,
            role="composer",
            user_prompt=runtime.bindings.message,
        )
        with store[0].connect() as conn:
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(running.claim.session_id),
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.owner == "provider",
                )
            ).one()
        assert effect.disposition == "dispatched" and effect.result_chunk_key is None
        sdk.append(kwargs)
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(runtime.bindings.message), pytest.raises(ValueError) as caught:
        await run_continuation_primary(
            service=runtime.materialization.service,
            authority=store[2],
            selected=runtime.source.selected,
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=work,
            provider_owner=runtime.bindings.provider_owner,
            timeout=5,
        )
    assert caught.value is original
    work.coordinator.assert_completed()
    assert len(sdk) == len(runtime.live.recorder.llm_calls) == 1
    assert store[2].classify_continuation_owner(running, env_ref_names=runtime.env_ref_names) == "unresolved"
    with store[0].connect() as conn:
        assert len(conn.execute(select(physical)).all()) == len(conn.execute(select(token_usage_ledger_table)).all()) == 1
    return running, runtime.live.recorder, sdk


@pytest.mark.parametrize(
    "field",
    (
        "expected_generation",
        "segment_sequence",
        "claim_attempt",
        "checkpoint_id",
        "segment_checkpoint_id",
        "session_operation_id",
        "session_operation_epoch",
        "session_operation_lease_token",
        "owner_instance_id",
    ),
)
def test_stale_candidate_writes_nothing_and_exact_original_is_positive(operation_store, field):
    engine, repository, _, _, _ = operation_store
    _, _, _, context = _started(operation_store)
    repository.release(context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    wrong = {
        "expected_generation": candidate.expected_generation + 1,
        "segment_sequence": candidate.segment_sequence + 1,
        "claim_attempt": candidate.claim_attempt + 1,
        "checkpoint_id": str(uuid4()),
        "segment_checkpoint_id": str(uuid4()),
        "session_operation_id": str(uuid4()),
        "session_operation_epoch": candidate.session_operation_epoch + 1,
        "session_operation_lease_token": "foreign-token",
        "owner_instance_id": "foreign-owner",
    }
    before = _admission_rows(engine)
    assert reconcile(repository, replace(candidate, **{field: wrong[field]})) == "unchanged"
    assert _admission_rows(engine) == before
    assert reconcile(repository, candidate) == "recovered"
    after = _admission_rows(engine)
    assert reconcile(repository, candidate) == "unchanged"
    assert _admission_rows(engine) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ("head", "entry"))
async def test_actual_progressed_title_reproves_both_checkpoint_observations_without_writes(operation_store, tmp_path, monkeypatch, stale):
    from tests.unit.web.sessions.test_continuation_title_provider import title_case

    engine, repository, _, _, _ = operation_store
    running, _service, _work, _prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    repository.release(running.session_operation_context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    wrong = (
        replace(candidate, checkpoint_id=candidate.segment_checkpoint_id)
        if stale == "head"
        else replace(candidate, segment_checkpoint_id=candidate.checkpoint_id)
    )
    # Both wrong values name real same-operation checkpoints. A writer must
    # reprove the two observed pointers, rather than merely their existence.
    before = _admission_rows(engine)
    assert reconcile(repository, wrong) == "unchanged"
    assert _admission_rows(engine) == before
    assert reconcile(repository, candidate) == "recovered"


@pytest.mark.parametrize("started", (False, True))
def test_archived_session_settles_without_fence_advance(operation_store, started):
    engine, repository, _, _, sid = operation_store
    if started:
        authority, record, claim, context = _started(operation_store)
        repository.release(context)
    else:
        authority, record = queued(operation_store)
    with engine.begin() as conn:
        assert (
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn))).rowcount == 1
        )
        old_fence = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
    if started:
        (candidate,) = list_recoverable_segments(engine, limit=1)
        assert reconcile(repository, candidate) == "settled"
    else:
        (candidate,) = list_inactive_continuations(engine, limit=1)
        assert repository.settle_lost_inactive_continuation(candidate, auth_provider_type="local") is True
    with engine.connect() as conn:
        assert conn.execute(select(fences).where(fences.c.session_id == str(sid))).one() == old_fence
        parent = conn.execute(select(jobs).where(jobs.c.operation_id == record.operation_id)).one()
        segment = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id)).one()
        history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).all()
    assert parent.status == "failed" and parent.settled_by == "settle_lost_inactive_session"
    if started:
        assert segment.state == "settled" and history[0].revoked_at is not None
        assert history[0].outcome == "interrupted" and history[0].generation == claim.ownership_generation
    else:
        assert segment.state == "queued" and segment.started_at is segment.deadline_at is segment.cooperative_at is None
        assert history == []
    assert authority.claim_segment(limit=1) == ()


def test_archived_prestart_claim_is_not_stolen_before_expiry(operation_store):
    engine, repository, _, _, sid = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
    before = _admission_rows(engine)
    assert list_inactive_continuations(engine, limit=1) == ()
    assert _admission_rows(engine) == before
    with engine.begin() as conn:
        conn.execute(
            update(segments)
            .where(segments.c.operation_id == claim.operation_id)
            .values(claim_expires_at=database_now(conn) - timedelta(seconds=1))
        )
    (candidate,) = list_inactive_continuations(engine, limit=1)
    assert repository.settle_lost_inactive_continuation(candidate, auth_provider_type="local") is True


def test_archived_interrupted_generation_settles_without_inventing_an_attempt(operation_store):
    engine, repository, _, _, sid = operation_store
    _, _, _, context = _started(operation_store)
    repository.release(context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert reconcile(repository, candidate) == "recovered"
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
        old_attempts = conn.execute(select(attempts)).all()
    (inactive,) = list_inactive_continuations(engine, limit=1)
    assert repository.settle_lost_inactive_continuation(inactive, auth_provider_type="local") is True
    with engine.connect() as conn:
        assert conn.execute(select(attempts)).all() == old_attempts
        assert conn.execute(select(segments.c.state)).scalar_one() == "settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("prestart_claim", (False, True))
@pytest.mark.parametrize("stage", ("prepared", "ready"))
async def test_archived_unclaimed_title_recovery_uses_actual_predecessor_without_attempt(
    operation_store, tmp_path, monkeypatch, prestart_claim, stage
):
    from tests.unit.web.sessions.test_continuation_title_application import paid_title
    from tests.unit.web.sessions.test_continuation_title_provider import title_case

    from elspeth.web.composer.continuation_title_application import apply_current_title_success

    engine, repository, authority, _, sid = operation_store
    if stage == "prepared":
        running, _service, _work, _prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    else:
        running, service, work, current, _original, sdk, _calls = await paid_title(operation_store, tmp_path, monkeypatch)
        ready = await apply_current_title_success(service=service, selected=current.selected, required_work=work)
        assert ready.applied and len(sdk) == 1
    with engine.connect() as conn:
        physical_before = conn.execute(select(physical)).all()
        ledger_before = conn.execute(select(token_usage_ledger_table)).all()
    repository.release(running.session_operation_context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    assert reconcile(repository, candidate) == "recovered"
    if prestart_claim:
        (claim,) = authority.claim_segment(limit=1)
        assert claim.ownership_generation == candidate.expected_generation + 1
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
        old_attempts = conn.execute(select(attempts)).all()
        if prestart_claim:
            conn.execute(
                update(segments)
                .where(segments.c.operation_id == candidate.operation_id)
                .values(
                    claim_expires_at=database_now(conn) - timedelta(seconds=1),
                )
            )
    (inactive,) = list_inactive_continuations(engine, limit=1)
    assert repository.settle_lost_inactive_continuation(inactive, auth_provider_type="local") is True
    with engine.connect() as conn:
        assert conn.execute(select(attempts)).all() == old_attempts
        assert conn.execute(select(segments.c.state)).scalar_one() == "settled"
        assert conn.execute(select(physical)).all() == physical_before
        assert conn.execute(select(token_usage_ledger_table)).all() == ledger_before
        assert (
            conn.execute(select(events.c.tag).where(events.c.operation_id == candidate.operation_id)).scalars().all().count("recovered")
            == 1
        )


def test_corrupt_retained_private_source_is_not_converted_to_stop_settlement(operation_store):
    engine, repository, _, _, sid = operation_store
    _, record, _, context = _started(operation_store)
    repository.release(context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.operation_id == record.operation_id).values(cancel_requested_at=database_now(conn)))
        # Same byte length keeps the dialect's byte CHECK enabled. The digest
        # becomes false, rather than replacing validation with a schema error.
        row = conn.execute(
            select(chunks)
            .join(
                checkpoints,
                (checkpoints.c.session_id == chunks.c.session_id)
                & (checkpoints.c.operation_id == chunks.c.operation_id)
                & (checkpoints.c.snapshot_chunk_key == chunks.c.chunk_key),
            )
            .where(checkpoints.c.session_id == str(sid), checkpoints.c.checkpoint_id == candidate.checkpoint_id)
        ).one()
        payload = row.payload
        changed = bytes([payload[0] ^ 1]) + payload[1:]
        conn.execute(update(chunks).where(chunks.c.chunk_key == row.chunk_key, chunks.c.session_id == str(sid)).values(payload=changed))
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError):
        reconcile(repository, candidate)
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_actual_unknown_provider_cut_settles_without_replay_or_accounting_loss(operation_store, tmp_path, monkeypatch):
    running, recorder, sdk = await failed_initialized_primary(operation_store, tmp_path, monkeypatch)
    assert len(sdk) == len(recorder.llm_calls) == 1
    engine, repository, _, _, _sid = operation_store
    repository.release(running.session_operation_context)
    with engine.connect() as conn:
        quota_before = conn.execute(select(physical)).all(), conn.execute(select(token_usage_ledger_table)).all()
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    assert reconcile(repository, candidate) == "settled"
    with engine.connect() as conn:
        assert (conn.execute(select(physical)).all(), conn.execute(select(token_usage_ledger_table)).all()) == quota_before
        assert conn.execute(select(effects.c.disposition).where(effects.c.owner == "provider")).scalar_one() == "unknown"
        tags = conn.execute(select(events.c.tag).where(events.c.operation_id == running.claim.operation_id)).scalars().all()
        parent = conn.execute(select(jobs).where(jobs.c.operation_id == running.claim.operation_id)).one()
    assert tags.count("effect_unknown") == tags.count("settled") == 1 and "recovered" not in tags
    assert parent.status == "failed" and parent.failure_code == "worker_lost" and len(sdk) == 1


@pytest.mark.asyncio
async def test_coherently_changed_primary_descriptor_refuses_before_unknown_settlement(operation_store, tmp_path, monkeypatch):
    from hashlib import sha256

    running, _recorder, sdk = await failed_initialized_primary(operation_store, tmp_path, monkeypatch)
    engine, repository, _, _, sid = operation_store
    repository.release(running.session_operation_context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    with engine.begin() as conn:
        effect = conn.execute(select(effects).where(effects.c.owner == "provider")).one()
        key = f"provider-input:{effect.input_sha256}"
        original = conn.execute(select(chunks).where(chunks.c.session_id == str(sid), chunks.c.chunk_key == key)).one()
        changed = b"{}"
        assert (
            conn.execute(
                update(chunks)
                .where(chunks.c.session_id == str(sid), chunks.c.chunk_key == key)
                .values(
                    payload=changed,
                    byte_length=len(changed),
                    sha256=sha256(changed).hexdigest(),
                )
            ).rowcount
            == 1
        )
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError):
        reconcile(repository, candidate)
    assert _admission_rows(engine) == before and len(sdk) == 1
    with engine.begin() as conn:
        conn.execute(
            update(chunks)
            .where(chunks.c.session_id == str(sid), chunks.c.chunk_key == key)
            .values(
                payload=original.payload,
                byte_length=original.byte_length,
                sha256=original.sha256,
            )
        )
    assert reconcile(repository, candidate) == "settled", "restored actual source is the behavioral positive"
    assert len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("tool_source", "persisted_state"))
async def test_coherent_paid_p4_corruption_refuses_in_live_reader_and_lost_stop(operation_store, tmp_path, monkeypatch, channel):
    from tests.unit.web.composer.test_continuation_historical_checkpoint import _actual_p4

    from elspeth.web.sessions.models import composition_states_table as states

    engine, repository, _, _, sid = operation_store
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "checkpoint") as (owner, running, phase, sdk, _):
        # The actual publisher and original immutable read are the positive.
        assert phase.application.bundle.result.historical_effect is not None and len(sdk) == 1
        assert owner.authority.classify_continuation_owner(running, env_ref_names=phase.inventory.names) in (
            "handoff",
            "handover",
            "unresolved",
        )
        with engine.begin() as conn:
            if channel == "tool_source":
                assert (
                    conn.execute(
                        update(effects).where(effects.c.session_id == str(sid), effects.c.owner == "tool_dispatch").values(action_json="{}")
                    ).rowcount
                    == 1
                )
            else:
                saved = phase.application.bundle.result.receipt.inserted_state_ids
                assert len(saved) == 1
                assert (
                    conn.execute(
                        update(states)
                        .where(states.c.id == str(saved[0]), states.c.session_id == str(sid))
                        .values(created_at=database_now(conn) + timedelta(seconds=1))
                    ).rowcount
                    == 1
                )
        before = _admission_rows(engine)
        with pytest.raises(AuditIntegrityError):
            owner.authority.classify_continuation_owner(running, env_ref_names=phase.inventory.names)
        assert _admission_rows(engine) == before
        owner.authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError):
        reconcile(repository, candidate)
    assert _admission_rows(engine) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("physical_epoch", "accounting_tokens"))
@pytest.mark.parametrize("stage", ("pending", "ready"))
async def test_coherent_title_source_corruption_refuses_in_live_reader_and_lost_stop(
    operation_store, tmp_path, monkeypatch, channel, stage
):
    import asyncio

    import litellm
    from tests.unit.web.sessions.test_continuation_title_provider import raw_reply, title_case

    from elspeth.web.composer import continuation_title_provider as title_driver
    from elspeth.web.composer.continuation_title_application import apply_current_title_success, read_current_title_application
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner

    engine, repository, authority, session_service, sid = operation_store
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    sdk = []

    async def completion(**kwargs):
        sdk.append(kwargs)
        return await asyncio.sleep(0, result=raw_reply(kwargs["model"]))

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        await title_driver.run_prepared_title_provider(
            prepared=prepared,
            invocation_owner=ProviderInvocationOwner(service=session_service, required_work=work),
        )
        good = await title_driver.read_current_title_result(service=service, running=running, required_work=work)
        assert good.settled and len(sdk) == 1
        if stage == "ready":
            ready = await apply_current_title_success(service=service, selected=good.selected, required_work=work)
            assert ready.applied
            assert (await read_current_title_application(service=service, running=running, required_work=work)).applied
        with engine.begin() as conn:
            if channel == "physical_epoch":
                original = conn.execute(select(physical)).one()
                assert (
                    conn.execute(
                        update(physical)
                        .where(physical.c.attempt_id == original.attempt_id)
                        .values(
                            operation_epoch=original.operation_epoch + 1,
                        )
                    ).rowcount
                    == 1
                )
            else:
                original = conn.execute(select(token_usage_ledger_table)).one()
                assert (
                    conn.execute(
                        update(token_usage_ledger_table)
                        .where(token_usage_ledger_table.c.entry_id == original.entry_id)
                        .values(
                            prompt_tokens=original.prompt_tokens + 1,
                        )
                    ).rowcount
                    == 1
                )
        before = _admission_rows(engine)
        with pytest.raises(AuditIntegrityError):
            if stage == "ready":
                await read_current_title_application(service=service, running=running, required_work=work)
            else:
                await title_driver.read_current_title_result(service=service, running=running, required_work=work)
        assert _admission_rows(engine) == before
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error)
    finally:
        repository.release(running.session_operation_context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError):
        reconcile(repository, candidate)
    assert _admission_rows(engine) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_admitted_title_without_stop_recovers_with_original_physical_accounting_and_no_sdk_retry(
    operation_store, tmp_path, monkeypatch
):
    import litellm
    from tests.unit.web.sessions.test_continuation_title_provider import sql_rows, title_case

    from elspeth.web.composer import continuation_title_provider as title_driver
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner

    engine, repository, _, session_service, _sid = operation_store
    running, _service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    sdk = []
    original = RuntimeError("original TITLE SDK failure")

    async def completion(**kwargs):
        sdk.append(kwargs)
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with pytest.raises(RuntimeError) as caught:
            await title_driver.run_prepared_title_provider(
                prepared=prepared,
                invocation_owner=ProviderInvocationOwner(service=session_service, required_work=work),
            )
        assert caught.value is original
        work.coordinator.assert_completed()
        physical_before, ledger_before, messages_before = sql_rows(operation_store)
        assert len(physical_before) == len(ledger_before) == len(sdk) == 1
        root_before = load_root(engine, running.claim.session_id, running.claim.operation_id)
        assert root_before.effect_frontier[0].disposition == "dispatched"
        assert root_before.pending_settlement_ids == (physical_before[0].attempt_id,)
    finally:
        repository.release(running.session_operation_context)
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    assert reconcile(repository, candidate) == "recovered"
    root_after = load_root(engine, running.claim.session_id, running.claim.operation_id)
    assert root_after.frames == root_before.frames and root_after.effect_frontier == root_before.effect_frontier
    assert root_after.pending_settlement_ids == root_before.pending_settlement_ids
    assert sql_rows(operation_store) == (physical_before, ledger_before, messages_before) and len(sdk) == 1
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(segments.c.generation).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == candidate.segment_sequence,
                )
            ).scalar_one()
            == candidate.expected_generation + 1
        )
        assert (
            conn.execute(
                select(jobs.c.status).where(
                    jobs.c.session_id == str(running.claim.session_id),
                    jobs.c.operation_id == running.claim.operation_id,
                )
            ).scalar_one()
            == "running"
        )


@pytest.mark.asyncio
async def test_lost_stop_preserves_actual_committed_p4_and_saved_revisions(operation_store, tmp_path, monkeypatch):
    from tests.unit.web.composer.test_continuation_historical_checkpoint import _actual_p4
    from tests.unit.web.sessions.test_continuation_p4_owner import rows

    from elspeth.web.coordination.composer_operation_authority import _saved_failure_revisions

    engine, repository, _, _, sid = operation_store
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "checkpoint") as (owner, running, phase, sdk, _):
        assert not load_root(engine, sid, running.claim.operation_id).pending_owner_results
        business_before = rows(engine)
        with engine.connect() as conn:
            effects_before = conn.execute(select(effects)).all()
            quota_before = conn.execute(select(physical)).all(), conn.execute(select(token_usage_ledger_table)).all()
            retained = {row.chunk_key: row.payload for row in conn.execute(select(chunks)).all()}
        record = owner.authority.request_cancel(
            session_id=sid, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        assert record is not None and phase.application is not None
        authority = owner.authority
    (candidate,) = list_recoverable_segments(engine, limit=1)
    assert_progressed_candidate(engine, candidate)
    assert reconcile(repository, candidate) == "settled"
    terminal = authority.get(session_id=sid, operation_id=running.claim.operation_id)
    assert terminal is not None and terminal.status == "failed" and terminal.failure_code == "request_cancelled"
    with engine.connect() as conn:
        assert conn.execute(select(effects)).all() == effects_before
        assert (conn.execute(select(physical)).all(), conn.execute(select(token_usage_ledger_table)).all()) == quota_before
        actual_chunks = {row.chunk_key: row.payload for row in conn.execute(select(chunks)).all()}
        assert all(actual_chunks[key] == value for key, value in retained.items())
        saved = _saved_failure_revisions(conn, sid, (terminal,))
        tags = conn.execute(select(events.c.tag).where(events.c.operation_id == running.claim.operation_id)).scalars().all()
    assert rows(engine) == business_before and len(sdk) == 1
    assert saved[terminal.session_operation_id].count == len(phase.application.bundle.result.receipt.inserted_state_ids)
    assert "recovered" not in tags and tags.count("settled") == 1


def load_root(engine, sid, operation_id):
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    with engine.connect() as conn:
        return decode_continuation_snapshot(load_snapshot(conn, session_id=str(sid), operation_id=operation_id).payload_json)
