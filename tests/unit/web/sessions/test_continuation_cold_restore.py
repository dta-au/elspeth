from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.continuation import prepare_composer_continuation_admission
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, RepositoryRateLimitAuthority
from elspeth.web.sessions.composer_operations import (
    ComposerOperationFenceLost,
    ComposerOperationRunning,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.continuation import ContinuationCASConflict, load_snapshot
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


def admit_continuation(store):
    engine, _repository, authority, _service, sid = store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Preserve café and exact context")
    request_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request)
    policy = minimal_continuation_policy()
    action = policy.actions.session_sql
    with engine.connect() as conn:
        initial_trust_mode = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(sid))).scalar_one()
    seed = prepare_composer_continuation_admission(
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_json=request.model_dump_json(),
        request_hash=request_hash,
        base_state_id=None,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_action_json=action.model_dump_json(),
        initial_trust_mode=initial_trust_mode,
    )
    record, fresh = authority.admit(
        quota=ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_hash=request_hash,
        actor_user_id="alice",
        request_id="continuation",
        base_state_id=None,
        request_json=request.model_dump_json(),
        deadline_seconds=180,
        max_nonterminal=64,
        auth_provider_type="local",
        lifecycle_version=2,
        continuation_enabled=True,
        seed=seed,
    )
    assert fresh
    return record, seed, action


def start_continuation(repository, authority):
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim,
        owner_instance_id="test-owner",
        lease_seconds=30,
        auth_provider_type="local",
    )
    return ComposerOperationRunning(claim=claim, session_operation_context=context)


def test_provider_action_facades_return_the_actual_complete_checkpoint(operation_store):
    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    selected = authority.load_continuation_snapshot(running)
    prepared = authority.prepare_provider_action(
        running,
        family=0,
        input_sha256="a" * 64,
        snapshot_json=selected.payload_json,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
    assert prepared.snapshot.checkpoint_id != selected.checkpoint_id
    assert prepared.snapshot.cursor > selected.cursor
    observed = authority.observe_provider_action(prepared.binding)
    assert observed.snapshot == prepared.snapshot
    assert observed.effect_key == prepared.binding.effect_key and observed.disposition == "prepared"
    assert observed.response is None and observed.provider_attempt_id is None and observed.ledger_entry_id is None
    repository.release(running.session_operation_context)


@pytest.mark.parametrize(
    "entrypoint", ("prepare_provider_effect", "prepare_provider_action", "load_continuation_snapshot", "observe_provider_action")
)
@pytest.mark.parametrize(
    "change", ("claim_token", "claim_attempt", "no_history_context", "session", "expired_lease", "released_lease", "nominal_running")
)
def test_initial_provider_boundaries_require_actual_claim_without_writes(operation_store, entrypoint, change):
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.continuation import prepare_provider_effect_on_connection, require_continuation_mutation
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
    from elspeth.web.sessions.models import metadata
    from elspeth.web.sessions.models import session_operation_fences_table as fences

    engine, repository, authority, service, sid = operation_store
    ordinary = None
    if change == "no_history_context":
        ordinary = repository.acquire(
            session_id=sid,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="test-owner",
            lease_seconds=30,
        )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(attempts).where(
                        attempts.c.session_id == str(sid),
                        attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                    )
                ).one_or_none()
                is None
            )
            assert require_continuation_mutation(conn, ordinary) is False
        repository.release(ordinary)
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    other = None
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        # Exercise the actual SQL preparation positively, then roll back only this savepoint.
        # No retained receipt from this rollback is treated as committed authority.
        with locked_session_transaction(engine, str(sid)) as conn:
            original_rows = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            assert require_continuation_mutation(conn, running.session_operation_context) is True
            positive = conn.begin_nested()
            try:
                first = prepare_provider_effect_on_connection(
                    conn,
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                prepared = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
                assert first.effect_key == "provider:0:0" and first.input_sha256 == "a" * 64
                assert prepared.checkpoint_id != selected.checkpoint_id and prepared.cursor > selected.cursor
            finally:
                positive.rollback()
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == original_rows
        binding = None
        if entrypoint == "observe_provider_action":
            committed = authority.prepare_provider_action(
                running,
                family=0,
                input_sha256="a" * 64,
                snapshot_json=selected.payload_json,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.cursor,
            )
            binding = committed.binding
            selected = committed.snapshot
            assert authority.observe_provider_action(binding).snapshot == selected
        refused = running
        expected_error = AuditIntegrityError
        expected_message = "continuation boundary changed its current claim/history binding"
        if change in ("claim_token", "claim_attempt"):
            changed_claim = (
                replace(running.claim, claim_token="wrong-current-token")
                if change == "claim_token"
                else replace(
                    running.claim,
                    attempt=running.claim.attempt + 1,
                )
            )
            refused = ComposerOperationRunning(claim=changed_claim, session_operation_context=running.session_operation_context)
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, refused.session_operation_context) is True
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        elif change == "no_history_context":
            assert ordinary is not None
            refused = ComposerOperationRunning(claim=running.claim, session_operation_context=ordinary)
            expected_message = "continuation boundary requires current continuation authority"
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, ordinary) is False
                assert (
                    conn.execute(
                        select(attempts).where(
                            attempts.c.session_id == str(sid),
                            attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                        )
                    ).one_or_none()
                    is None
                )
        elif change == "session":
            second_session = repository.create_session_with_initial_fence(
                user_id="alice",
                title="Another actual session",
                auth_provider_type="local",
                owner_instance_id="test-owner",
                lease_seconds=30,
            )
            admit_continuation((engine, repository, authority, service, second_session.id))
            other = start_continuation(repository, authority)
            refused = ComposerOperationRunning(
                claim=replace(running.claim, session_id=second_session.id),
                session_operation_context=other.session_operation_context,
            )
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, other.session_operation_context) is True
                assert other.claim.operation_id != running.claim.operation_id
        elif change == "expired_lease":
            with engine.begin() as conn:
                conn.execute(
                    update(fences)
                    .where(
                        fences.c.session_id == str(sid),
                        fences.c.operation_id == running.session_operation_context.fence.operation_id,
                        fences.c.operation_epoch == running.session_operation_context.fence.operation_epoch,
                    )
                    .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                )
            expected_error = ComposerOperationFenceLost
            expected_message = None
        elif change == "released_lease":
            repository.release(running.session_operation_context)
            expected_error = ComposerOperationFenceLost
            expected_message = None
        else:
            refused = object()
            expected_error = TypeError
            expected_message = "continuation boundary requires nominal running custody"
        with engine.connect() as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        with pytest.raises(expected_error, match=expected_message):
            if entrypoint == "prepare_provider_effect":
                authority.prepare_provider_effect(
                    refused,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
            elif entrypoint == "prepare_provider_action":
                authority.prepare_provider_action(
                    refused,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
            elif entrypoint == "load_continuation_snapshot":
                authority.load_continuation_snapshot(refused)
            else:
                assert binding is not None
                authority.observe_provider_action(replace(binding, running=refused))
        with engine.connect() as conn:
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        if change not in ("expired_lease", "released_lease"):
            assert authority.load_continuation_snapshot(running) == selected
            if entrypoint == "prepare_provider_effect":
                restored = authority.prepare_provider_effect(
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                assert restored.effect_key == "provider:0:0"
            elif entrypoint == "prepare_provider_action":
                restored = authority.prepare_provider_action(
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                assert restored.snapshot.checkpoint_id != selected.checkpoint_id
            elif binding is not None:
                assert authority.observe_provider_action(binding).snapshot == selected
    finally:
        if other is not None:
            repository.release(other.session_operation_context)
        if change not in ("expired_lease", "released_lease"):
            repository.release(running.session_operation_context)


@pytest.mark.parametrize("entrypoint", ("prepare_provider_effect", "prepare_provider_action"))
@pytest.mark.parametrize("change", ("checkpoint", "cursor", "selected_stack", "digest", "family"))
def test_initial_provider_preparation_preserves_head_on_invalid_input(operation_store, entrypoint, change):
    from elspeth.web.sessions.continuation import prepare_provider_effect_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import metadata

    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        with locked_session_transaction(engine, str(sid)) as conn:
            original_rows = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            positive = conn.begin_nested()
            try:
                first = prepare_provider_effect_on_connection(
                    conn,
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                assert first.effect_key == "provider:0:0"
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id).checkpoint_id != selected.checkpoint_id
            finally:
                positive.rollback()
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == original_rows
        args = {
            "family": 0,
            "input_sha256": "a" * 64,
            "snapshot_json": selected.payload_json,
            "expected_checkpoint_id": selected.checkpoint_id,
            "expected_cursor": selected.cursor,
        }
        expected_error = ContinuationCASConflict
        expected_message = "continuation snapshot changed before effect admission"
        if change == "checkpoint":
            args["expected_checkpoint_id"] = str(uuid4())
        elif change == "cursor":
            args["expected_cursor"] = selected.cursor + 1
        elif change == "selected_stack":
            stack = decode_continuation_snapshot(selected.payload_json)
            frame = stack.frames[-1].model_copy(update={"request_json": stack.frames[-1].request_json + " "})
            args["snapshot_json"] = encode_continuation_snapshot(stack.model_copy(update={"frames": (frame,)}))
            expected_message = "provider preparation replaced the actual selected complete head"
        elif change == "digest":
            args["input_sha256"] = "invalid-digest"
            expected_error = ValueError
            expected_message = "provider preparation requires its actual input digest"
        else:
            args["family"] = True
            expected_error = ValueError
            expected_message = "unknown physical provider family"
        with engine.connect() as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
        with pytest.raises(expected_error, match=expected_message):
            if entrypoint == "prepare_provider_effect":
                authority.prepare_provider_effect(running, **args)
            else:
                authority.prepare_provider_action(running, **args)
        with engine.connect() as conn:
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        prepared = authority.prepare_provider_action(
            running,
            family=0,
            input_sha256="a" * 64,
            snapshot_json=selected.payload_json,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
        )
        assert prepared.snapshot.checkpoint_id != selected.checkpoint_id
        assert authority.observe_provider_action(prepared.binding).snapshot == prepared.snapshot
    finally:
        repository.release(running.session_operation_context)


def test_initial_provider_observation_binds_actual_selected_input_without_writes(operation_store, monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy import delete

    import elspeth.web.coordination.composer_operation_authority as authority_module
    from elspeth.web.sessions.continuation import put_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import metadata

    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        prepared = authority.prepare_provider_action(
            running,
            family=0,
            input_sha256="a" * 64,
            snapshot_json=selected.payload_json,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
        )
        first = authority.observe_provider_action(prepared.binding)
        assert first.snapshot == prepared.snapshot and first.disposition == "prepared"
        with engine.connect() as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
        with pytest.raises(AuditIntegrityError, match="actual selected provider locator"):
            authority.observe_provider_action(replace(prepared.binding, input_sha256="b" * 64))
        with engine.connect() as conn:
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
        assert authority.observe_provider_action(prepared.binding) == first
        actual_transaction = authority_module.locked_session_transaction
        checked_owners = []

        @contextmanager
        def observe_actual_transaction(actual_engine, actual_session_id):
            assert actual_engine is engine and actual_session_id == str(sid)
            with actual_transaction(actual_engine, actual_session_id) as owner_conn:
                owner_rows = {table.name: sorted((tuple(row) for row in owner_conn.execute(select(table))), key=repr) for table in tables}
                try:
                    yield owner_conn
                finally:
                    # Before the original SQL owner's exit/rollback; this remains a net-row oracle.
                    assert {
                        table.name: sorted((tuple(row) for row in owner_conn.execute(select(table))), key=repr) for table in tables
                    } == owner_rows
                    assert load_snapshot(owner_conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
                    checked_owners.append(True)

        with monkeypatch.context() as observation:
            observation.setattr(authority_module, "locked_session_transaction", observe_actual_transaction)
            assert authority.observe_provider_action(prepared.binding) == first
            assert checked_owners == [True]
            for unsupported in ("wrong_codec", "dispatched", "unknown"):
                with engine.begin() as fixture_conn:
                    if unsupported == "wrong_codec":
                        put_chunk(
                            fixture_conn,
                            session_id=str(sid),
                            operation_id=record.operation_id,
                            key="fixture:unexpected-provider-response",
                            payload=b'{"owned":"wrong provider codec"}',
                            codec="composer.state.v1",
                        )
                        fixture_conn.execute(
                            update(effects)
                            .where(
                                effects.c.session_id == str(sid),
                                effects.c.operation_id == record.operation_id,
                                effects.c.effect_key == prepared.binding.effect_key,
                            )
                            .values(result_chunk_key="fixture:unexpected-provider-response")
                        )
                    else:
                        fixture_conn.execute(
                            update(effects)
                            .where(
                                effects.c.session_id == str(sid),
                                effects.c.operation_id == record.operation_id,
                                effects.c.effect_key == prepared.binding.effect_key,
                            )
                            .values(disposition=unsupported)
                        )
                prior_checks = len(checked_owners)
                with pytest.raises(AuditIntegrityError, match="storage-only provider observation requires an unadvanced prepared effect"):
                    authority.observe_provider_action(prepared.binding)
                assert len(checked_owners) == prior_checks + 1
                with engine.begin() as fixture_conn:
                    fixture_conn.execute(
                        update(effects)
                        .where(
                            effects.c.session_id == str(sid),
                            effects.c.operation_id == record.operation_id,
                            effects.c.effect_key == prepared.binding.effect_key,
                        )
                        .values(disposition="prepared", result_chunk_key=None)
                    )
                    if unsupported == "wrong_codec":
                        fixture_conn.execute(
                            delete(chunks).where(
                                chunks.c.session_id == str(sid),
                                chunks.c.operation_id == record.operation_id,
                                chunks.c.chunk_key == "fixture:unexpected-provider-response",
                            )
                        )
                with engine.connect() as conn:
                    assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
                    assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
                assert authority.observe_provider_action(prepared.binding) == first
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("entrypoint", ("generic", "provider"))
@pytest.mark.parametrize("cancelled", (False, True))
def test_new_provider_preparation_refuses_owed_p4_scope_without_writes(operation_store, entrypoint, cancelled):
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
    from elspeth.web.sessions.continuation import (
        _OWED_P4_SCOPE_KEY,
        _OWED_P4_SEAL,
        _owed_continuation_p4_active,
        _OwedP4Scope,
        prepare_effect_on_connection,
        prepare_provider_effect_on_connection,
        require_continuation_mutation,
    )
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import metadata

    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    context = running.session_operation_context
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        with locked_session_transaction(engine, str(sid)) as conn:
            assert require_continuation_mutation(conn, context) is True
            original_rows = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            positive = conn.begin_nested()
            try:
                binding = prepare_provider_effect_on_connection(
                    conn,
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                produced = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
                assert produced.checkpoint_id != selected.checkpoint_id
            finally:
                positive.rollback()
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == original_rows
            # Reuse the actual producer's complete result, restoring only the pre-append input cursor.
            # This is a proposed input, never a committed checkpoint receipt or fabricated authority.
            next_stack = decode_continuation_snapshot(produced.payload_json).model_copy(update={"cursor": selected.cursor})
            next_snapshot_json = encode_continuation_snapshot(next_stack)
            if entrypoint == "generic":
                direct_positive = conn.begin_nested()
                try:
                    direct = prepare_effect_on_connection(
                        conn,
                        running,
                        effect_key=binding.effect_key,
                        input_sha256=binding.input_sha256,
                        owner="provider",
                        next_snapshot_json=next_snapshot_json,
                        expected_checkpoint_id=selected.checkpoint_id,
                        expected_cursor=selected.cursor,
                    )
                    assert direct.checkpoint_id != selected.checkpoint_id
                finally:
                    direct_positive.rollback()
                assert {
                    table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables
                } == original_rows
            if cancelled:
                conn.execute(
                    update(jobs)
                    .where(
                        jobs.c.session_id == str(sid),
                        jobs.c.operation_id == record.operation_id,
                    )
                    .values(cancel_requested_at=database_now(conn))
                )
                with pytest.raises(ComposerOperationCancelledDuringTurn):
                    require_continuation_mutation(conn, context)
            # Fixture-only construction of the real nominal active-transaction scope.
            # This does not claim a real P4 was issued or grant fresh provider eligibility.
            conn.info[_OWED_P4_SCOPE_KEY] = _OwedP4Scope(conn, context, _OWED_P4_SEAL)
            try:
                assert _owed_continuation_p4_active(conn, context) is True
                assert require_continuation_mutation(conn, context) is True
                before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
                with pytest.raises(AuditIntegrityError, match="new continuation preparation cannot use owed P4 authority"):
                    if entrypoint == "generic":
                        prepare_effect_on_connection(
                            conn,
                            running,
                            effect_key=binding.effect_key,
                            input_sha256=binding.input_sha256,
                            owner="provider",
                            next_snapshot_json=next_snapshot_json,
                            expected_checkpoint_id=selected.checkpoint_id,
                            expected_cursor=selected.cursor,
                        )
                    else:
                        prepare_provider_effect_on_connection(
                            conn,
                            running,
                            family=0,
                            input_sha256="a" * 64,
                            snapshot_json=selected.payload_json,
                            expected_checkpoint_id=selected.checkpoint_id,
                            expected_cursor=selected.cursor,
                        )
                # Inside the live owner transaction: outer rollback cannot hide an early mutation.
                assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
            finally:
                del conn.info[_OWED_P4_SCOPE_KEY]
    finally:
        repository.release(context)
